# Hydra Manga TL — v0.8.0 Catalog

## Translate manga without giving up control

Hydra Manga TL is a local-first Windows workspace that converts Japanese and
Chinese manga pages into reviewable English PNGs. It combines OCR, translation,
artwork reconstruction, typesetting, manual correction, review, and export
without overwriting source images.

## At a glance

| | |
| --- | --- |
| Current version | **0.8.0 — Unified Translation Pipeline** |
| Current status | **Finished, under polishing** |
| Previous milestone | **0.7.0 — HydraMangaAi** |
| Previous status | **Under development** |
| Platform | Windows 10/11, 64-bit |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP |
| Source text | Japanese, Chinese, preserved Latin script |
| Output | English PNG |
| Local engines | MarianMT; optional Local Qwen GGUF |
| Cloud engines | Opt-in Groq, Google Translate, Gemini, DeepSeek |
| Processing model | Shared OCR, translation, request, cache, and render services |
| Project safety | Autosaved, reversible, source images untouched |

## v0.8.0 — finished, under polishing

- One application-lifetime PaddleOCR worker for batch, manual, and review work.
- SmartOCR quality budgets with uncertain text routed to Review.
- Unified batch, selected-page, manual-region, and review request contracts.
- One reusable translation manager and one serialized render queue.
- Ordered chapter processing with page context and cache reuse.
- Cooperative cancellation for queued and active-stage work.
- Safe queued-render cancellation and manual-region rollback.
- Exact manual text placement inside the user-drawn rectangle.
- Original/translated canvases, filmstrip, block inspector, and issue queues.
- Editable wording, fonts, size, color, alignment, position, and bubble type.
- Non-destructive PNG export that preserves imported relative paths.

## v0.7.0 — HydraMangaAi under development

The v0.7.0 milestone introduced the optional private HydraMangaAi layer, which
remains under development:

- Local correction-draft capture.
- Explicit bubble and page approval.
- Immutable training snapshots.
- Dataset-readiness and promotion gates.
- AI Center status and training controls.
- Model version and rollback metadata.

HydraMangaAi is optional. The finished v0.8.0 deterministic OCR, translation,
and render pipeline remains fully operational when the private package is
absent, disabled, or still waiting for approved training data.

## Typical workflow

1. Import pages or a chapter folder.
2. Run **Translate Selected** or **Translate All Pending**.
3. Review completed pages and issue queues.
4. Correct text or typography and rerender.
5. Draw an **Add Text Box** rectangle for missed dialogue.
6. Export completed pages to a separate folder.

Projects and logs live under `%LOCALAPPDATA%\Hydra Manga TL`. Initial model
downloads require internet access; later jobs reuse local caches. A GPU is
optional, and hosted engines require user-supplied credentials.

## Validation

The completed v0.8.0 checkpoint passes 140 automated tests plus real disposable
Groq and Marian runtime smokes. The combined Marian smoke covers selected-page
translation, manual exact-box rendering, failure rollback, a mixed-cache
two-page batch, review queues, one translation-manager generation, and one
unchanged OCR worker process.

For installation and usage, read [README.md](README.md). For architecture and
project contracts, read [project.md](project.md).
