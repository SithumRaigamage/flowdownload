# Releasing FlowDownload

Releases are built by GitHub Actions (`.github/workflows/release.yml`).
PyInstaller can't cross-compile, so each OS is built on its own runner:

| Runner | Output |
|---|---|
| `macos-14` | `FlowDownload-X.Y.Z-macos-arm64.dmg` |
| `macos-15-intel` | `FlowDownload-X.Y.Z-macos-x64.dmg` |
| `windows-latest` | `FlowDownload-X.Y.Z-windows-x64-setup.exe` (Inno Setup, `packaging/windows/`) |
| `ubuntu-22.04` | `FlowDownload-X.Y.Z-linux-x86_64.AppImage` + `.tar.gz` (`packaging/linux/`) |

A final job adds the Chrome extension zip and `SHA256SUMS.txt`, and
publishes everything as a GitHub Release. The release text comes from
`.github/release-body.md`, followed by the auto-generated list of changes.

## Cutting a release

```bash
./scripts/build_app.sh --version 1.3.0   # or just edit VERSION
git commit -am "release: v1.3.0"
git push origin main
git tag v1.3.0
git push origin v1.3.0
```

Then watch **Actions → Release**, which takes roughly 15–20 minutes. The
release appears at
<https://github.com/SithumRaigamage/flowdownload/releases>.

- The tag must match `VERSION`. If it doesn't, the build fails straight
  away instead of publishing mislabelled files.
- A tag with a suffix, such as `v1.3.0-rc1`, is published as a
  **pre-release**, so it isn't shown as "latest".
- **Dry run:** Actions → Release → **Run workflow** builds every installer
  and attaches them to the workflow run as artifacts, without publishing
  anything.

## If a build fails

Fix the problem on `main`, then move the tag and push it again:

```bash
git tag -f v1.3.0 && git push -f origin v1.3.0
```

If the release was already published, delete it first:
`gh release delete v1.3.0`.

## Signing (not set up yet)

Builds are unsigned, which is why users see the Gatekeeper and SmartScreen
warnings described in `docs/INSTALL.md`. Signing can be added later
without changing the overall workflow:

- **macOS:** Apple Developer Program, a Developer ID Application
  certificate, `codesign` with hardened runtime, then `xcrun notarytool`.
  The certificate and the Apple ID app password go in repository secrets.
- **Windows:** a code-signing certificate (for example, Azure Trusted
  Signing), applied with `signtool` to `FlowDownload.exe`,
  `flowdownload-host.exe` and the installer.
