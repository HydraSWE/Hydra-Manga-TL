# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata


PROJECT_ROOT = Path(SPECPATH)

# PaddleOCR and PaddleX discover inference pipelines and operators dynamically.
# Collect their package data, native binaries, and submodules explicitly so the
# frozen application behaves like the source installation.
datas = [(str(PROJECT_ROOT / "assets"), "assets")]
binaries = []
hiddenimports = []

# Optional helper runtime for LaMa/iopaint title-background cleanup. Keep it
# outside the main Python environment because iopaint pins Pillow 9.5.0 while
# Hydra's renderer uses the main app's newer Pillow line.
INPAINT_RUNTIME = PROJECT_ROOT / "runtime" / "inpaint"
if INPAINT_RUNTIME.exists():
    datas.append((str(INPAINT_RUNTIME), "runtime/inpaint"))
else:
    print("Optional inpaint runtime not bundled; build it with scripts/build_inpaint_runtime.ps1")

for package in ("paddle", "paddleocr", "paddlex"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# Japanese speech converts ambiguous Kanji to kana before handing text to the
# Windows voice. Collect the native tokenizer and its offline small dictionary.
for package in ("sudachipy", "sudachidict_small"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# Keyring discovers Windows Credential Manager backends dynamically.
keyring_datas, keyring_binaries, keyring_hiddenimports = collect_all("keyring")
datas += keyring_datas
binaries += keyring_binaries
hiddenimports += keyring_hiddenimports

# These libraries inspect their installed distribution metadata at runtime.
# In particular, PaddleX validates its OCR extras through importlib.metadata;
# collecting only the importable packages makes a frozen build look as though
# the OCR dependencies are missing.
for distribution in (
    "paddlepaddle",
    "paddleocr",
    "paddlex",
    "imagesize",
    "opencv-contrib-python",
    "pyclipper",
    "pypdfium2",
    "python-bidi",
    "shapely",
    "torch",
    "transformers",
    "keyring",
    "SudachiPy",
    "SudachiDict-small",
):
    datas += copy_metadata(distribution)


a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Hydra Manga TL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "icons" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Hydra Manga TL",
)
