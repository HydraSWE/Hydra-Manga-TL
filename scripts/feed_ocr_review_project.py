from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hydra_manga_tl.ocr_review import create_ocr_review_project


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an OCR-only Hydra review project from image samples.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--name", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    project = create_ocr_review_project(args.source, args.name or None, args.limit)
    print(project.project_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
