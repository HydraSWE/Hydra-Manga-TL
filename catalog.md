# Hydra Manga TL — v1 Catalog

## Translate manga without giving up control

Hydra Manga TL is a local-first Windows workspace that converts Japanese and
Chinese manga pages into reviewable translated images, archives, and PDFs. It
combines OCR, translation, artwork reconstruction, typesetting, manual
correction, review, and export without overwriting source images.

## At a glance

| | |
| --- | --- |
| Current version | **v1 — Recoverability, Editing, Diagnostics, Multi-Target Output, Smart Translation, Large-Project Stability, and UI Refresh** |
| Current status | **Current release** |
| Pipeline status | **Manifest-v3 safe resume and planning foundation included in v1** |
| Optional bridge | **HydraMangaAi private package** |
| Historical baseline | **0.8.0-alpha — Unified Translation Pipeline** |
| Historical bridge milestone | **0.7.0 — HydraMangaAi** |
| Platform | Windows 10/11, 64-bit |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP |
| Source text | Japanese, Chinese, preserved Latin script |
| Output targets | English, Spanish, French, German, Italian, Portuguese |
| Export | PNG/JPEG/WebP folders, ZIP/CBZ archives, PDF |
| Local engines | MarianMT; optional Local Qwen GGUF |
| Cloud engines | Opt-in Groq, Google Translate, Gemini, DeepSeek |
| Processing model | Startup coordinator plus shared OCR, translation, request, cache, and render services |
| Project safety | Autosaved, reversible, source images untouched |
| Installer | Complete offline package; selectable Cloud/Qwen/Marian downloads deferred |

## v1 — Current Release

- Manifest-v3 source/input/output digests, policy identities, timestamps, and
  stage error summaries.
- Centralized pipeline planning and validation foundations for safer resume,
  invalidation, and stage reuse.
- Verified OCR, translation-only, and completed-render resume; page state alone
  is never trusted as proof of a valid artifact.
- Provider-safe Smart Translation worker limits, including conservative Groq and
  local-model caps.
- Large-project responsiveness improvements for opening, restoring, filmstrip
  loading, thumbnails, and progress refreshes.
- Refreshed Project Home with polished recent-project cards and clearer import
  actions.
- Searchable recent-project library through `View All`, while the landing page
  stays focused on the latest entries.
- Refreshed workspace chrome with grouped controls, icon-assisted actions,
  canvas headers, status labels, and cleaner filmstrip and inspector styling.
- Polished startup and project import loading screens with staged progress
  feedback.
- Automated project schema migration, backward compatibility guards, ZIP backups,
  and safe rollback for failed migrations.
- Deterministic Translation Memory and legacy JSON cache compatibility.
- Isolated translation, rendering, editing, timing, manifest, and export state
  for every output target.
- Generalized bounded undo/redo plus editable numbered reading order.
- Typography fit preview and focused review filters.
- `Ctrl+Left/Right` batch growth, `Ctrl+Space` toggle, selection persistence,
  Select Pending, and Clear Selection filmstrip actions.
- PDF, target-aware folder, WebP, ZIP, and CBZ export.
- NVIDIA device/driver/VRAM/utilization plus Torch, llama.cpp, Paddle, native
  dependency, and allocation/load diagnostics.
- Redacted diagnostics bundles and durable fatal exception logging.
- Configurable application-data root with exports-safe project cleanup.
- The branded startup, identity tile, polygon Region Tool, text-layout editing,
  shared normalization, and domain package foundation from v0.9.0.
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
cancellation, and manual-rerender pipeline. v0.9.0 added the startup, workspace,
Region Tool, layout, identity, and package foundation. All remain active in
v1.

## HydraMangaAi optional bridge

The optional private HydraMangaAi layer was introduced in the v0.7.0 milestone
and remains separate from the main package:

- Local correction-draft capture.
- Explicit bubble and page approval.
- Immutable training snapshots.
- Dataset-readiness and promotion gates.
- AI Center status and training controls.
- Model version and rollback metadata.

HydraMangaAi is optional. The v1 deterministic OCR, translation, and render
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

The v1 validation plan covers startup, project migration, filmstrip batch
selection, identity tile, layout editor, Region Tool, global normalization,
native OCR worker startup, queue cancellation, exact-box rendering, rollback,
review queues, manifest digest/policy invalidation, verified translation
rerender, multi-target isolation, target-aware export, diagnostics redaction,
and GPU/native status.
Validate the current checkout before packaging because this branch may contain
other in-progress changes.

For installation and usage, read [README.md](README.md). For architecture and
project contracts, read [project.md](project.md).
