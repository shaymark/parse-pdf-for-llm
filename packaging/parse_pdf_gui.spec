# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for parse-pdf-gui.

Build with:
    pyinstaller packaging/parse_pdf_gui.spec --clean --noconfirm

Outputs (per platform):
    dist/parse-pdf-gui            (linux, windows; .exe on windows)
    dist/parse-pdf-gui.app        (macOS bundle)
"""
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all

# Run from repo root so relative paths resolve.
ROOT = Path(SPECPATH).resolve().parent

block_cipher = None

# Bundle the source script so the GUI can copy it on first launch.
datas = [
    (str(ROOT / ".claude" / "skills" / "parse-pdf" / "parse_pdf.py"), "bundled_script"),
]
binaries = []
hiddenimports = ["tkinterdnd2", "platformdirs"]
if sys.platform == "darwin":
    hiddenimports += ["AppKit", "Foundation"]

# Collect the packages that parse_pdf.py imports. The runner re-execs the
# bundle with --run-script so parse_pdf.py loads inside this same Python
# environment; everything it imports must therefore be in the bundle.
for pkg in ("pymupdf", "pymupdf4llm", "ollama", "tkinterdnd2"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass


a = Analysis(
    [str(ROOT / "gui" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Per-platform icon (only attached if the file exists).
def _icon_for(plat: str) -> str | None:
    if plat == "darwin":
        cand = ROOT / "packaging" / "icon.icns"
    elif plat == "win32":
        cand = ROOT / "packaging" / "icon.ico"
    else:
        cand = ROOT / "packaging" / "icon.png"
    return str(cand) if cand.exists() else None


exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="parse-pdf-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_for(sys.platform),
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="parse-pdf-gui.app",
        icon=_icon_for("darwin"),
        bundle_identifier="com.shaymark.parsepdfgui",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.2.0",
            "CFBundleVersion": "0.2.0",
            # Allow the app to be opened from Finder by double-click on a PDF.
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PDF",
                    "CFBundleTypeRole": "Viewer",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
    )
