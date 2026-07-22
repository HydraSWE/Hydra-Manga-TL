"""Phase 2 translation command line application."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .layout import classify_text_group, group_regions
from .translation import MarianTranslator, translate_regions, translated_region_dict


def load_inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".json" and path.name != "report.json" else []
    return sorted(item for item in path.glob("*.json") if item.name != "report.json")


def run(input_path: Path, output: Path, target: str, provider=None) -> dict:
    files = load_inputs(input_path)
    if not files:
        raise ValueError("No Phase 1 JSON results were found.")
    output.mkdir(parents=True, exist_ok=True)
    provider = provider or MarianTranslator()
    report_images: list[dict] = []
    csv_rows: list[dict] = []

    for image_index, path in enumerate(files, 1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        language = payload["language"]
        print(f"[{image_index}/{len(files)}] Translating {Path(payload['source']).name} ({language} -> {target}) ...", flush=True)
        groups = group_regions(payload["regions"])
        group_regions_payload = []
        translatable_indices = []
        classifications = []
        for group_index, group in enumerate(groups):
            confidence = min(payload["regions"][index - 1]["confidence"] for index in group.member_indices)
            polygon = [
                [group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]],
                [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]],
            ]
            classification = classify_text_group(group, payload["regions"], payload.get("model_language", "japan"))
            classifications.append(classification)
            if classification.kind in {"dialogue", "narration"}:
                translatable_indices.append(group_index)
                group_regions_payload.append({
                    "text": group.text,
                    "confidence": confidence,
                    "polygon": polygon,
                })
        translated = translate_regions(group_regions_payload, language, target, provider)
        translated_by_group = dict(zip(translatable_indices, translated))
        translated_groups = []
        for group_index, group in enumerate(groups):
            classification = classifications[group_index]
            translated_region = translated_by_group.get(group_index)
            if translated_region is None:
                item = {
                    "index": group_index + 1,
                    "original_text": group.text,
                    "literal_text": group.text,
                    "translated_text": "",
                    "ocr_confidence": min(payload["regions"][index - 1]["confidence"] for index in group.member_indices),
                    "polygon": [
                        [group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]],
                        [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]],
                    ],
                    "status": "preserved",
                    "review_reasons": classification.reasons or [f"{classification.kind}_preserved"],
                    "provider": getattr(provider, "cache_identity", "unknown"),
                    "model": "",
                    "localization_style": "Manga",
                    "translation_quality": "good",
                    "alternatives": [],
                    "localization_note": "",
                }
            else:
                item = translated_region_dict(translated_region)
                item["index"] = group_index + 1
                item["review_reasons"] = list(dict.fromkeys(item.get("review_reasons", []) + classification.reasons))
                if item["review_reasons"]:
                    item["status"] = "review"
                    item["translation_quality"] = "review"
            item["member_region_indices"] = group.member_indices
            item["direction"] = group.direction
            item["source_polygons"] = [payload["regions"][index - 1]["polygon"] for index in group.member_indices]
            item["source_member_texts"] = [str(payload["regions"][index - 1].get("text", "")) for index in group.member_indices]
            translated_groups.append(item)
        result = {
            "source": payload["source"],
            "source_language": language,
            "target_language": target,
            "ocr_model_language": payload["model_language"],
            "source_regions": payload["regions"],
            "translation_groups": translated_groups,
        }
        destination = output / f"{path.stem}_translated_{target}.json"
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        review_count = sum(region.status == "review" for region in translated)
        report_images.append({
            "file": Path(payload["source"]).name,
            "source_language": language,
            "target_language": target,
            "source_regions": len(payload["regions"]),
            "translation_groups": len(translated),
            "review_required": review_count,
            "output": destination.name,
        })
        for region, group in zip(translated, groups):
            csv_rows.append({
                "image": Path(payload["source"]).name,
                "region": region.index,
                "source_region_indices": ",".join(map(str, group.member_indices)),
                "source_language": language,
                "original_text": region.original_text,
                "translated_text": region.translated_text,
                "ocr_confidence": f"{region.ocr_confidence:.4f}",
                "status": region.status,
                "review_reasons": ",".join(region.review_reasons),
            })
        print(f"  {len(translated)} regions; {review_count} flagged for review")

    report = {"target_language": target, "count": len(report_images), "images": report_images}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "translations.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["image", "region", "source_region_indices", "source_language", "original_text", "translated_text", "ocr_confidence", "status", "review_reasons"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate Phase 1 OCR results with local models.")
    parser.add_argument("input", type=Path, help="Phase 1 output folder or JSON file")
    parser.add_argument("--output", type=Path, default=Path("outputs/phase2"))
    parser.add_argument("--target", default="en", choices=["en"], help="Target language")
    args = parser.parse_args()
    try:
        run(args.input, args.output, args.target)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
