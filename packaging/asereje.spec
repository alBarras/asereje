# PyInstaller spec for the ASEREJÉ desktop build (one-folder mode).
# Build:  pyinstaller --noconfirm packaging/asereje.spec   (from the repo root
# or anywhere — paths resolve relative to this file). See BUILDING.md.
#
# Before building, pre-fetch ffmpeg so it ships inside the bundle (the .app
# is read-only at runtime, so static_ffmpeg cannot download it on demand):
#   python -c "from static_ffmpeg import run; run.get_or_fetch_platform_executables_else_raise()"

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # repo root (spec lives in packaging/)

datas = [(str(ROOT / "static"), "static")]
# Package data reached via __file__-relative paths at runtime:
#  - demucs: remote model registry (files.txt / *.yaml)
#  - faster_whisper: silero VAD onnx asset
#  - static_ffmpeg: the pre-fetched ffmpeg/ffprobe binaries under bin/
for pkg in ("demucs", "faster_whisper", "static_ffmpeg"):
    datas += collect_data_files(pkg, include_py_files=False)

hiddenimports = collect_submodules("demucs")  # model classes resolved by name
# numpy 2 keeps numpy.core as a shim so old pickles (the demucs checkpoints)
# still unpickle; it's referenced only via pickle strings, so collect it
# explicitly. Harmless no-op on numpy 1.x (Intel-Mac build).
hiddenimports += collect_submodules("numpy.core")
if sys.platform == "darwin":
    hiddenimports += ["pystray._darwin"]
elif sys.platform == "win32":
    hiddenimports += ["pystray._win32"]

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="ASEREJE",
    console=False,
    icon=[str(ROOT / "packaging" / "icon.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="ASEREJE",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ASEREJE.app",
        icon=str(ROOT / "packaging" / "icon.icns"),
        bundle_identifier="cat.albertborras.asereje",
        info_plist={
            "CFBundleDisplayName": "ASEREJÉ",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            # Menu-bar-only app: the browser is the UI, the tray icon quits it.
            "LSUIElement": True,
        },
    )
