from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QPolygonF


def make_dock_icon(size: int = 1024) -> QPixmap:
    """Renders the FlowDownload app icon: a macOS-style rounded square with a
    blue-to-violet gradient and a bold download glyph (arrow + tray bar)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    corner_radius = size * 0.225
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), corner_radius, corner_radius)

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#6d8bff"))
    gradient.setColorAt(1.0, QColor("#6a4bff"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawPath(path)

    # Subtle inner highlight near the top, like most macOS Big Sur+ icons.
    highlight = QLinearGradient(0, 0, 0, size * 0.5)
    highlight.setColorAt(0.0, QColor(255, 255, 255, 40))
    highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(highlight)
    painter.drawPath(path)

    # Download glyph: arrow shaft + head, plus a tray bar underneath.
    painter.setBrush(QColor("white"))
    cx = size / 2
    shaft_width = size * 0.11
    shaft_top = size * 0.24
    shaft_bottom = size * 0.52
    painter.drawRoundedRect(
        QRectF(cx - shaft_width / 2, shaft_top, shaft_width, shaft_bottom - shaft_top),
        shaft_width / 2, shaft_width / 2,
    )

    arrow_half = size * 0.19
    arrow_top = size * 0.46
    arrow_tip = size * 0.66
    arrow = QPolygonF([
        QPointF(cx - arrow_half, arrow_top),
        QPointF(cx + arrow_half, arrow_top),
        QPointF(cx, arrow_tip),
    ])
    painter.drawPolygon(arrow)

    bar_width = size * 0.46
    bar_height = size * 0.075
    bar_y = size * 0.76
    painter.drawRoundedRect(
        QRectF(cx - bar_width / 2, bar_y, bar_width, bar_height),
        bar_height / 2, bar_height / 2,
    )

    painter.end()
    return pixmap
