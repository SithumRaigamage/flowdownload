"""Blend a Qt window's macOS title bar into the page below it.

Qt has no API for this, so it talks to the window's NSWindow through the
Objective-C runtime with ctypes (no PyObjC dependency): a transparent title
bar with no title text, the window background set to the page colour, and
the window's appearance (light/dark) matching the app's theme, so the
traffic-light buttons and any system drawing match too.

Everything is best-effort: on another OS, or if any call fails, the window
just keeps its standard title bar.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

from PySide6.QtGui import QGuiApplication

_NS_WINDOW_TITLE_HIDDEN = 1

_objc = None
if sys.platform == "darwin":
    try:
        _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("AppKit"))  # NSColor, NSAppearance
    except (OSError, AttributeError, TypeError):
        _objc = None


def _send(receiver, selector: str, *args, argtypes=(), restype=ctypes.c_void_p):
    # objc_msgSend must be called through a correctly typed prototype
    # (it is not variadic on arm64), so build one per call shape.
    proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
    send = proto(ctypes.cast(_objc.objc_msgSend, ctypes.c_void_p).value)
    return send(receiver, _objc.sel_registerName(selector.encode()), *args)


def _ns_string(text: str):
    cls = _objc.objc_getClass(b"NSString")
    return _send(cls, "stringWithUTF8String:", text.encode(), argtypes=(ctypes.c_char_p,))


def blend_title_bar(qt_window, background_hex: str, dark: bool) -> bool:
    """Make qt_window's title bar take the page's colour. True if applied."""
    # winId() is an NSView* only on the real macOS ("cocoa") platform; under
    # e.g. the "offscreen" one the tests use, messaging it would crash.
    if _objc is None or QGuiApplication.platformName() != "cocoa":
        return False
    try:
        view = int(qt_window.winId())  # an NSView* on macOS
        window = _send(view, "window")
        if not window:
            return False
        _send(window, "setTitlebarAppearsTransparent:", True, argtypes=(ctypes.c_bool,), restype=None)
        _send(window, "setTitleVisibility:", _NS_WINDOW_TITLE_HIDDEN, argtypes=(ctypes.c_long,), restype=None)

        r, g, b = (int(background_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        color = _send(_objc.objc_getClass(b"NSColor"), "colorWithSRGBRed:green:blue:alpha:", r, g, b, 1.0,
                      argtypes=(ctypes.c_double,) * 4)
        _send(window, "setBackgroundColor:", color, argtypes=(ctypes.c_void_p,), restype=None)

        name = _ns_string("NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua")
        appearance = _send(_objc.objc_getClass(b"NSAppearance"), "appearanceNamed:", name,
                           argtypes=(ctypes.c_void_p,))
        _send(window, "setAppearance:", appearance, argtypes=(ctypes.c_void_p,), restype=None)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
