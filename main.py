"""Hydra Manga TL desktop application entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _apply_qwen_runtime_defaults() -> None:
    """Make the local Qwen GGUF runtime default to the same settings as the CLI example."""
    defaults = {
        "QWEN_N_CTX": "2048",
        "QWEN_N_BATCH": "128",
        "QWEN_N_UBATCH": "64",
        "QWEN_N_GPU_LAYERS": "-1",
        "QWEN_FLASH_ATTN": "on",
        "QWEN_OFFLOAD_KQV": "on",
        "QWEN_OP_OFFLOAD": "on",
        "QWEN_TYPE_K": "q4_0",
        "QWEN_TYPE_V": "q4_0",
        "QWEN_N_THREADS": "4",
        "QWEN_N_THREADS_BATCH": "4",
        "QWEN_VERBOSE": "false",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def _activate_project_runtime() -> None:
    """Relaunch global-Python invocations with the project virtual environment."""
    if getattr(sys, "frozen", False) or sys.prefix != sys.base_prefix:
        return
    root = Path(__file__).resolve().parent
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        raise RuntimeError(
            "Hydra Manga TL dependencies are not installed. Run: "
            "python -m venv .venv; .\\.venv\\Scripts\\python -m pip install -r requirements.txt"
        )
    completed = subprocess.run([str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(completed.returncode)


_apply_qwen_runtime_defaults()
_activate_project_runtime()

from hydra_manga_tl.application import MangaApplication


def main() -> int:
    startup_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    try:
        return MangaApplication(startup_path=startup_path).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
