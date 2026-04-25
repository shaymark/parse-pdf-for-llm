# Packaging

This folder holds the PyInstaller spec for the desktop GUI and (optional)
icons for each platform.

## Build locally

```bash
pip install -e ".[gui,vision]" pyinstaller
pyinstaller packaging/parse_pdf_gui.spec --clean --noconfirm
```

Outputs:

- macOS → `dist/parse-pdf-gui.app`
- Linux → `dist/parse-pdf-gui`
- Windows → `dist/parse-pdf-gui.exe`

## Icons (optional)

Drop platform-specific icons here:

- `icon.icns` — macOS
- `icon.ico` — Windows
- `icon.png` — Linux

The spec auto-detects what's present; if no icon is found the binary uses
PyInstaller's default.
