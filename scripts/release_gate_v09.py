from __future__ import annotations

import argparse
import json
from pathlib import Path


def evaluate(artifacts: Path, *, minimum_fit_rate: float = 0.80) -> dict:
    timing_path = artifacts / "pipeline_timing_summary.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path.is_file() else {}
    page_files = sorted(artifacts.glob("*_intelligent_page.json"))
    rendered = 0
    fit_ok = 0
    review_issues = 0
    render_failures = 0
    ocr_retry_count = 0
    qwen_time = 0.0

    for path in page_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        render_review = payload.get("render_review", {})
        typesetting = render_review.get("typesetting", {})
        rendered += int(typesetting.get("reviewed_count", 0) or 0)
        fit_ok += int(typesetting.get("fit_without_manual_resize_count", 0) or 0)
        review_issues += int(render_review.get("ai_review", {}).get("issue_count", 0) or 0)
        if typesetting.get("issue_count", 0):
            render_failures += int(typesetting.get("issue_count", 0) or 0)
        ocr_retry_count += int(payload.get("ocr_attempts", {}).get("retry_summary", {}).get("attempt_count", 0) or 0)

    for image in timing.get("images", []):
        stages = image.get("stages", {})
        qwen_time += float(stages.get("translate_seconds", 0.0) or 0.0)
        if not page_files:
            retry = image.get("retry", {})
            ocr_retry_count += int(retry.get("attempt_count", 0) or 0)

    fit_rate = fit_ok / rendered if rendered else 0.0
    passed = rendered > 0 and fit_rate >= minimum_fit_rate and render_failures == 0
    return {
        "passed": passed,
        "minimum_fit_rate": minimum_fit_rate,
        "fit_rate": round(fit_rate, 4),
        "fit_without_manual_resize_count": fit_ok,
        "reviewed_dialogue_groups": rendered,
        "total_time_seconds": timing.get("total_seconds", 0.0),
        "ocr_retries": ocr_retry_count,
        "qwen_or_translation_time_seconds": round(qwen_time, 3),
        "render_failures": render_failures,
        "review_count": review_issues,
        "page_artifacts": len(page_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Hydra Manga TL v0.9 release gate from pipeline artifacts.")
    parser.add_argument("artifacts", type=Path, help="Project artifacts folder containing pipeline_timing_summary.json")
    parser.add_argument("--minimum-fit-rate", type=float, default=0.80)
    args = parser.parse_args()
    report = evaluate(args.artifacts, minimum_fit_rate=args.minimum_fit_rate)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
