# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

try:
    from importlib.metadata import PackageNotFoundError
except ImportError:
    from importlib_metadata import PackageNotFoundError


PROJECT_ROOT = Path(SPECPATH)
SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
PYSIDE_ROOT = SITE_PACKAGES / "PySide6"
SHIBOKEN_ROOT = SITE_PACKAGES / "shiboken6"
BUILD_SUPPORT = PROJECT_ROOT / "scripts" / "build_support"


def ensure_qt_reported_path(name):
    source = PYSIDE_ROOT / name
    destination = SITE_PACKAGES / name
    if not source.is_dir() or destination.exists():
        return
    try:
        os.symlink(source, destination, target_is_directory=True)
    except OSError:
        shutil.copytree(source, destination, dirs_exist_ok=True)


for folder in (PYSIDE_ROOT, SHIBOKEN_ROOT):
    if folder.is_dir():
        normalized = str(folder.resolve())
        try:
            os.add_dll_directory(normalized)
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = normalized + os.pathsep + os.environ.get("PATH", "")
os.environ["HYDRA_BUILD_DLL_DIRS"] = os.pathsep.join(
    str(folder.resolve()) for folder in (PYSIDE_ROOT, SHIBOKEN_ROOT) if folder.is_dir()
)
if BUILD_SUPPORT.is_dir():
    os.environ["PYTHONPATH"] = str(BUILD_SUPPORT.resolve()) + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
for qt_path_name in ("plugins", "qml", "translations"):
    ensure_qt_reported_path(qt_path_name)

APP_VERSION = "1.0.0"
APP_STATUS = "current development"

# PaddleOCR and PaddleX discover inference pipelines and operators dynamically.
# Collect their package data, native binaries, and submodules explicitly so the
# frozen application behaves like the source installation.
datas = [(str(PROJECT_ROOT / "assets"), "assets")]
binaries = []
hiddenimports = []

for qt_plugin_name in ("platforms", "imageformats", "iconengines", "styles", "texttospeech", "tls"):
    qt_plugin_path = PYSIDE_ROOT / "plugins" / qt_plugin_name
    if qt_plugin_path.is_dir():
        binaries.append((str(qt_plugin_path), f"PySide6/plugins/{qt_plugin_name}"))

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
#
# Local Qwen loads llama_cpp through importlib at runtime and depends on native
# CUDA wheels being discoverable from the frozen _internal tree.
for package in (
    "sudachipy",
    "sudachidict_small",
    "llama_cpp",
    "nvidia",
    "sentencepiece",
    "sacremoses",
    "tokenizers",
    "safetensors",
):
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
    "sentencepiece",
    "sacremoses",
    "tokenizers",
    "safetensors",
    "llama-cpp-python",
    "nvidia-cuda-runtime-cu12",
    "nvidia-cublas-cu12",
    "keyring",
    "SudachiPy",
    "SudachiDict-small",
):
    try:
        datas += copy_metadata(distribution)
    except PackageNotFoundError:
        print(f"Optional metadata not found; skipping {distribution}")


a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torchvision",
        "torchvision._C",
        "iopaint",
        "gradio",
    ],
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
