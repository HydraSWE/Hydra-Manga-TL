# Hydra Manga TL — v0.9.0 Catalog

## Translate manga without giving up control

Hydra Manga TL is a local-first Windows workspace that converts Japanese and
Chinese manga pages into reviewable English PNGs. It combines OCR, translation,
artwork reconstruction, typesetting, manual correction, review, and export
without overwriting source images.

## At a glance

| | |
| --- | --- |
| Current version | **0.9.0 — Startup, Region, Layout, and Identity** |
| Current status | **Current development** |
| Pipeline status | **Included in v0.9.0** |
| Optional bridge | **HydraMangaAi private package** |
| Historical baseline | **0.8.0-alpha — Unified Translation Pipeline** |
| Historical bridge milestone | **0.7.0 — HydraMangaAi** |
| Platform | Windows 10/11, 64-bit |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP |
| Source text | Japanese, Chinese, preserved Latin script |
| Output | English PNG |
| Local engines | MarianMT; optional Local Qwen GGUF |
| Cloud engines | Opt-in Groq, Google Translate, Gemini, DeepSeek |
| Processing model | Startup coordinator plus shared OCR, translation, request, cache, and render services |
| Project safety | Autosaved, reversible, source images untouched |

## v0.9.0 — current development

- Branded Hydra warmup screen with version, staged progress, completed-stage
  history, warnings, and fatal-error exit path.
- Core-ready startup handoff while OCR and selected local translation warmups
  may continue safely in the background.
- Local-only provider validation at startup; no network checks for cloud
  credentials or hosted model names.
- Collapsible Filmstrip section with per-project persistence.
- Pinned Hydra identity thumbnail before Page 1, with a scalable preview dialog
  and no participation in page ordering, counts, translation, deletion, or
  export.
- Upgraded high-contrast Hydra identity thumbnail for full-size preview and
  72x78 filmstrip readability.
- Rectangle/polygon Region Tool with source-geometry persistence and
  `manual_rect` compatibility.
- Current-canvas translated-text layout editing for position and fit before
  rerender.
- Shared normalization for escaped Unicode, punctuation, odd spaces,
  zero-width characters, and repeated punctuation.
- Domain package layout for UI, OCR, translation, phase, project, and core code.
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

## v0.8.0-alpha — historical baseline

v0.8.0-alpha completed the unified OCR, translation, request, cache, render,
cancellation, and manual-rerender pipeline. Those capabilities are still part of
v0.9.0; the newer line adds startup polish, workspace controls, Region Tool
upgrades, layout editing, identity assets, and package organization.

## HydraMangaAi optional bridge

The optional private HydraMangaAi layer was introduced in the v0.7.0 milestone
and remains separate from the main package:

- Local correction-draft capture.
- Explicit bubble and page approval.
- Immutable training snapshots.
- Dataset-readiness and promotion gates.
- AI Center status and training controls.
- Model version and rollback metadata.

HydraMangaAi is optional. The v0.9.0 deterministic OCR, translation, and render
pipeline remains fully operational when the private package is absent, disabled,
or still waiting for approved training data.

## Typical workflow

1. Import pages or a chapter folder.
2. Run **Translate Selected** or **Translate All Pending**.
3. Review completed pages and issue queues.
4. Correct text or typography and rerender.
5. Draw or adjust a Region Tool rectangle or polygon for missed dialogue.
6. Export completed pages to a separate folder.

Projects and logs default to `%LOCALAPPDATA%\Hydra Manga TL`; the data folder
can be changed from Settings. Initial model downloads require internet access;
later jobs reuse local caches. A GPU is optional, and hosted engines require
user-supplied credentials.

## Validation

The v0.9.0 validation plan covers startup, project migration, filmstrip,
identity tile, layout editor, Region Tool, global normalization, package import
paths, native OCR worker startup, queue cancellation, manual exact-box
rendering, failure rollback, review queues, translation-manager reuse, and OCR
worker reuse.
Validate the current checkout before packaging because this branch may contain
other in-progress changes.

For installation and usage, read [README.md](README.md). For architecture and
project contracts, read [project.md](project.md).
