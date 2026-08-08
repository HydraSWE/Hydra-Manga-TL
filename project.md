# Hydra Manga TL — v1 Project Guide

## Project summary

Hydra Manga TL is a non-destructive Windows desktop pipeline for OCR,
Japanese/Chinese manga translation, artwork reconstruction, typesetting,
review, manual correction, and multi-format export.

The v1 release line is the current runtime and documentation target.
Batch, selected-page, manual-region, and review work share application-lifetime
OCR and translation services, one serialized render queue, normalized progress
states, cooperative cancellation, stable cache contracts, startup warmup,
responsive filmstrip controls, identity presentation, region-tool upgrades,
layout editing, shared text normalization, strict stage-level resume,
target-isolated artifacts, provider-safe Smart Translation, large-project
responsiveness, generalized editor history, diagnostics, and the domain package
layout. The current UI layer also includes the refreshed Project Home,
searchable recent-project library, workspace chrome cleanup, and staged loading
surfaces.

Status: v1 is the current release line. v0.9.0 remains the completed
workspace/package foundation, v0.8.0-alpha remains the unified-pipeline
baseline, and v0.7.0 remains the HydraMangaAi bridge milestone.

## Release history

| Version | Status | Scope |
| --- | --- | --- |
| **v1 — Recoverability, Smart Translation, Large-Project Stability, Diagnostics, Multi-Target Output, and UI Refresh** | Current release | Manifest-v3 safe resume, centralized planning and validation foundation, verified OCR/translation/render reuse, automated schema migration and compatibility, provider-safe Fast worker limits, large-project workspace responsiveness, refreshed Project Home, searchable recent-project library, grouped workspace controls, loading-screen polish, generalized undo/redo, editable reading order, review filters, batch-selection polish, multi-target artifacts, PDF/CBZ/WebP export, diagnostics bundles, GPU/native-runtime details, configurable app-data storage, and the complete v0.9.0 foundation |
| **v0.9.0 — Startup, Region, Layout, Identity, and Package Layout** | Completed foundation | Branded startup warmup, core-ready handoff, collapsible filmstrip, pinned identity tile, polygon Region Tool, text-layout editing, shared normalization, lazy imports, and UI/OCR/translation/phase/project/core packages |
| **v0.8.0-alpha — Unified Pipeline** | Completed baseline | Persistent OCR worker, SmartOCR retry budgets, typed requests, one translation manager, one render queue, grouped chapter execution, cancellation, normalized caches, exact manual bounds, rollback-safe rendering |
| **v0.7.0 — HydraMangaAi** | Optional/private bridge milestone | Optional private correction capture, explicit approval queues, training snapshots, readiness gates, promotion metadata, rollback records, and AI Center |
| **v0.6.0 — Unified Workspace** | Completed foundation | Project Home, selected-page translation, manual regions, editor overrides, speech, review, autosave, and export |
| **v0.5.0 — Desktop Consolidation** | Completed foundation | Consolidated staged prototypes into the Project Home and review workspace |

## Product invariants

1. Source images are never overwritten.
2. Manual translated text stays inside the selected `manual_rect`.
3. Failed or cancelled queued manual rendering restores project state.
4. PaddleOCR belongs to one application-lifetime OCR runtime.
5. Translation uses one configured manager at a time.
6. All rendering is serialized through one application-level queue.
7. Manual work remains asynchronous and does not block the Qt event loop.
8. Uncertain OCR or translation is routed to Review.
9. Existing page and selection caches remain physically separate behind a
   shared cache-key facade.
10. Identity artwork is UI-only and never enters project image IDs, page
    counts, ordering, translation queues, deletion, or export.
11. Text layout adjustments are render-only until applied and do not mutate OCR
    source geometry.
12. A manifest state alone never proves that a checkpoint is reusable; source,
    input, output, application, and policy identities must validate.
13. Deterministic Translation Memory lookup and legacy JSON cache keys remain
    backward compatible.
14. Every output target owns isolated translation, render, edit, timing, and
    manifest state while OCR remains shared.

## Current scope

| Area | v1 behavior |
| --- | --- |
| Platform | Windows 10/11 desktop application using PySide6 |
| Startup | Branded splash, staged progress, refreshed loading presentation, core-ready handoff, and background warmups |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP; individual files or recursive folders |
| OCR | Shared PaddleOCR runtime with Japanese, Chinese, and Latin-script evidence |
| OCR quality | Fast = 0 retries, Balanced = 1, Maximum = 3 |
| Translation | MarianMT, optional Local Qwen GGUF, Groq, Google Translate, Gemini, DeepSeek |
| Output targets | English, Spanish, French, German, Italian, Portuguese |
| Requests | Batch, selected, manual, and review |
| States | Queued, OCR, translating, rendering, done, failed, cancelled |
| Rendering | One serialized queue for batch, manual, editor, and review work |
| Review | OCR/translation queues plus untranslated, residual-script, overflow, missing-glyph, low-OCR, and provider-fallback filters |
| Editing | Wording, replace/skip, font, size, color, alignment, offsets, bubble type, text layout, reading order, and bounded undo/redo |
| Recovery | Rectangle/polygon manual regions, automatic-block suppression/restoration, rollback |
| Workspace UI | Refreshed Project Home, searchable recent-project library, grouped workspace controls, canvas headers, status labels, collapsible filmstrip, pinned identity tile, keyboard batch growth, Select Pending, and Clear Selection |
| Persistence | Versioned `project.json`, target-isolated state, manifest-v3 stage contracts, global caches, and automated schema migrations with ZIP backups |
| Diagnostics | Redacted support bundle, fatal logs, timings, GPU/VRAM/native backend details |
| Export | Target-aware PNG/JPEG/WebP folders, ZIP/CBZ archives, and PDF |

Automatic output remains reviewable and editable because decorative lettering,
unusual artwork, ambiguous source text, and provider language-pair limitations
may require human judgment.

## Unified request flow

```text
UI / WorkspaceManager
        |
        v
TranslationRequest
  batch | selected | manual | review
        |
        v
TranslationQueue (serialized owner)
        |
        +--> OCRService --> OCRRuntimeManager --> one Paddle worker
        |
        +--> TranslationRuntime --> one TranslationEngineManager
        |
        +--> RenderQueue --> one active render
        |
        v
project artifacts, review state, UI refresh
```

Batch and selected-page requests are submitted as ordered groups. One chapter
coordinator preserves page order, chapter OCR language learning,
`ContextEngine` state, translation concurrency bounds, and page-level timing.
Manual requests OCR only the selected crop and retain exact user geometry.

## Core modules

| Module | Responsibility |
| --- | --- |
| `core/application.py` | Qt bootstrap, lazy imports, startup coordination, branding, logging, warm-up, and orderly shutdown |
| `core/startup.py` | Splash screen, startup progress signals, warnings, completion, and fatal-error presentation |
| `core/assets.py` | Single packaged/source asset-root lookup path |
| `core/diagnostics.py` / `core/gpu.py` | Fatal logging, redacted support bundles, hardware/VRAM and native-backend diagnostics |
| `ui/` | Project Home, recent-project library, workspace, canvases, collapsible filmstrip, identity tile, editor, review navigation, loading surfaces, unified Cancel |
| `project/workspace.py` | Project orchestration, manual transactions, rollback, recent projects, export |
| `project/model.py` | Versioned project and image schema, including per-project filmstrip visibility |
| `translation/requests.py` | Typed translation and render request contracts |
| `translation/queue.py` | Serialized request ownership, grouped execution, states, cancellation |
| `translation/runtime.py` | Application-lifetime translation manager and configuration reuse |
| `phase/render_queue.py` | Serialized rendering, queued cancellation, completion/failure correlation |
| `phase/pipeline.py` | Reusable OCR, dialogue, translation, payload, render, and chapter stages |
| `phase/job_manifest.py` | Manifest-v3 stage contracts, artifact fingerprints, invalidation, errors, and stale recovery |
| `ocr/runtime.py` / `ocr/worker.py` | App-lifetime subprocess lifecycle and telemetry |
| `ocr/service.py` | Shared page and selection OCR entry points |
| `ocr/manager.py` | Retry budgets, candidate scoring, uncertainty and Review policy |
| `translation/cache_store.py` | Shared stable keys for page and selection caches |
| `translation/memory/` / `translation/phrase_memory/` | Deterministic exact memory and approved reusable phrase suggestions |
| `phase/context_engine.py` | Chapter context and prior-page memory |
| `translation/engines/` | Marian, Qwen GGUF, and opt-in remote page engines |
| `phase/renderer.py` / `phase/phase3.py` | Masking, cleanup, text fitting, exact manual placement, reports |
| `phase/review.py` / `ocr/review.py` | Translation, render, and OCR review support |
| `core/ai_bridge.py` | Optional HydraMangaAi integration |

## OCR lifecycle and retry policy

`OCRRuntimeManager` owns one reusable worker client for the application. Page,
manual-selection, and review OCR go through `OCRService`; they do not create
independent PaddleOCR owners.

`SmartOCRManager` evaluates confidence, script fit, suspicious digits, short
vertical text, and tiny regions. Retry limits are hard quality-profile budgets:

| Quality | Retry budget |
| --- | --- |
| Fast | 0 |
| Balanced | 1 |
| Maximum | 3 |

Focused retry may use one `color2` prediction. Remaining uncertainty is recorded
in metadata and sent to Review.

## Translation lifecycle

`TranslationRuntime` owns exactly one `TranslationEngineManager`. The manager is
reused while engine, model, Qwen path/name, provider models, and glossary remain
unchanged. When configuration changes, a replacement is prepared before the old
manager unloads.

Cloud engines are lazy and isolated: choosing a cloud engine does not load
Marian or Qwen at startup and a failed cloud request does not silently construct
a local fallback. Local Qwen availability must be proven by successful context
creation and generation, not GPU-layer offload logs alone.

Startup checks validate configured cloud-provider credentials and model names
locally without network requests. Marian warmup remains isolated from Qwen and
cloud engines; configured local Qwen can warm only through the shared
translation runtime when its model path is valid.

Page translation is cached by engine identity, language pair, dialogue, context,
glossary, and relevant configuration. Translation memory also deduplicates exact
repeated strings.

## Manual-region transaction

1. The canvas normalizes and validates the selected rectangle.
2. A typed manual request enters `TranslationQueue`.
3. `OCRService.analyze_selection` OCRs only the crop.
4. The shared translation runtime translates the composed region.
5. Workspace state adds a `ManualRegion` and suppresses overlapping automatic
   groups.
6. A `RenderRequest` enters the shared render queue.
7. Success saves paths and refreshes the page.
8. Failure or queued cancellation removes the new region and restores status.

The renderer treats `manual_rect` as authoritative through
`manual_exact_bounds`.

The Region Tool stores polygon-capable source geometry while retaining
`manual_rect` compatibility for existing project data and exact-box rendering.
Rectangle remains the default drawing mode, and source geometry stays separate
from render-only text layout.

## Startup lifecycle

```text
QApplication
        |
        v
StartupSplash
        |
        v
StartupCoordinator
  paths/logging -> settings/assets -> local provider validation
        |
        v
renderer registries -> hidden main window -> project restore
        |
        v
OCR/local translation warmups -> core-ready handoff
```

Fatal core initialization failures replace progress with a concise error and
Exit action. OCR and selected local-model warmups may continue after the main
window appears; failures are nonfatal, reported through the status bar, and
retried lazily on first use through the existing runtimes.

## Project Home and workspace UI

Project Home presents import actions, recent projects, and the searchable
recent-project library without changing project data contracts. The compact
landing list shows the latest entries, while `View All` opens the larger
searchable grid for older recent projects.

The workspace refresh is chrome-only: grouped command areas, icon-assisted
buttons, canvas headers, status labels, and cleaner filmstrip and inspector
styling. These visual changes do not add a separate sidebar, change translation
flow, or alter project schema.

## Cancellation and shutdown

- A queued translation request is removed before execution.
- OCR/translation cancellation is cooperative at stage boundaries.
- Cancelling any page in a grouped batch cancels the chapter token.
- A queued manual render can be cancelled and rolled back.
- An already-running render is allowed to finish safely.
- Application shutdown cancels queued work, waits for active boundaries, then
  unloads translation and OCR runtimes.

The GUI Cancel button routes through this unified behavior for both pipeline and
manual work.

## Project data

Each project contains `project.json` and an `artifacts` directory. Image records
retain:

- Source and imported relative paths.
- Processing, failure, cancellation, and review status.
- OCR, translation, intelligent-page, timing, preview, and rendered paths.
- Editor overrides and suppressed automatic groups.
- Manual regions with source polygons, exact rectangle, translation, confidence,
  direction, status, and review reasons.
- Per-project `filmstrip_visible`, defaulting to `true` for older projects.
- HydraMangaAi subject and approval identifiers when the private layer is used.
- Per-target translation, rendering, editor, and manual-region state.
- Optional manual reading order while preserving automatic layout order.

Shared OCR/preprocessing artifacts remain at project scope. Target-specific
translation JSON, renders, timings, intelligent-page output, and
`chapter_job_manifest.json` live under `artifacts/targets/<target>/`.

Manifest version 3 records named source, input, and output artifact paths,
sizes, and SHA-256 digests; application/settings identities; translation
provider/model identity; completion timestamps; and stage errors. Older
manifests remain readable but cannot independently authorize checkpoint reuse.

The pinned Hydra identity tile is a control in the workspace only. It is not
serialized as an image record and never participates in ordering, selection
indices, translation, deletion, page counts, or export.

Application-owned global data defaults to:

```text
%LOCALAPPDATA%\Hydra Manga TL
```

The data folder can be changed from Settings.

Separate OCR, page-translation, and manual-selection cache files are retained
for migration safety while sharing normalized cache-key helpers.

Projects opened from older schemas (e.g., schema 7 to 8) trigger a safe migration
flow. The system prompts the user, creates an automatic ZIP backup of the project
directory, applies deterministic schema transformations, and rolls back to the
backup if migration or subsequent loading fails. Future schemas are gracefully
rejected to prevent data loss.

## HydraMangaAi optional bridge

HydraMangaAi is an optional private package connected through `core/ai_bridge.py`.
It was introduced in the v0.7.0 milestone and captures correction drafts, but
requires explicit approval before data enters training snapshots. This layer
remains under development as the bridge from approved review data to future
task-specific models. Readiness is task-specific and data-gated; candidate
models must pass promotion rules before they can become active. The v1
OCR/translation/render pipeline remains authoritative.

Removing HydraMangaAi leaves the translation product operational.

## Repository layout

```text
assets/                       Icons, logos, and identity thumbnail
hydra_manga_tl/               Desktop application and pipeline
hydra_manga_tl/core/          App runtime, settings, paths, startup, state
hydra_manga_tl/ocr/           OCR engine, manager, service, worker, review
hydra_manga_tl/phase/         Pipeline, rendering, layout, preprocessing
hydra_manga_tl/project/       Project model, workspace, import/export
hydra_manga_tl/translation/   Translation service, queues, cache, providers
hydra_manga_tl/ui/            Qt windows, dialogs, canvas, filmstrip
scripts/                      Runtime smoke, benchmark, release, and build tools
samples/                      Diagnostic manga images
tests/                        Unit, integration, native-worker, and offscreen Qt tests
main.py                       Desktop entry point
requirements.txt             Runtime and development dependencies
README.md                     Installation and user guide
catalog.md                    Concise product catalog
project.md                    Architecture and project guide
```

## Validation status

The completed v0.8.0-alpha checkpoint included 140 passing automated tests plus
native OCR worker startup, shared runtime serialization, queue cancellation,
shutdown, Qt control-state checks, exact manual bounds, render rollback
coverage, and disposable Groq/Marian runtime smokes.

The v1 validation plan includes:

- Native Paddle worker startup and response.
- Shared-runtime manager reuse and serialization tests.
- Grouped queue order and cancellation tests.
- Manual cancellation after OCR and before rendering.
- Idle and cancellation-pending shutdown tests.
- Offscreen Qt control-state and task-message validation.
- Real Groq and Marian disposable runtime smokes.
- A combined Marian session covering Translate Selected, manual translation,
  forced render rollback, a two-page batch with one cache hit and one miss, and
  both review queues.

The runtime smoke never modifies its supplied source project or source images.

Additional v1-focused validation covers:

- Splash visibility before heavy imports and hidden main-window construction.
- Ordered, monotonic startup progress and nonfatal warmup warnings.
- Startup with missing recent projects, malformed settings, absent cloud
  credentials, and missing local models.
- Project schema migration for `filmstrip_visible`.
- Immediate per-project filmstrip persistence and project switching.
- Identity tile exclusion from page IDs, selection indices, drag/drop payloads,
  translation queues, deletion, counts, and export.
- Collapse/expand behavior while thumbnails or translation work continue.
- Full-size and 72x78 identity-thumbnail readability.
- Domain package import paths for UI, OCR, translation, phase, project, and core.
- Manifest-v3 contract serialization, legacy-manifest safety, source/input/output
  corruption detection, policy invalidation, and stage error recording.
- Translation-only checkpoint reuse followed by render/review reconstruction.
- Multi-target project/editor/artifact isolation and target-aware export names.
- Filmstrip keyboard batch growth, toggle/range behavior, stable-ID selection,
  reading-order persistence/reset, and generalized editor history.
- PDF page ordering, WebP/ZIP/CBZ export, diagnostics redaction, and GPU/native
  runtime status.

## Development commands

Install dependencies and run tests:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Compile-check Python sources:

```powershell
.\.venv\Scripts\python -m compileall -q hydra_manga_tl scripts
```

Run a two-page mixed-cache runtime smoke:

```powershell
.\.venv\Scripts\python scripts\smoke_unified_runtime.py --project "path\to\project.json" --image-count 2 --engine marian
```

Build/package only as an explicitly authorized release action:

```powershell
.\scripts\build_windows_dist.ps1
```

## Current limitations

- Windows is the documented and tested desktop target.
- Output targets are English, Spanish, French, German, Italian, and Portuguese;
  individual providers and local models may support only a subset of pairs.
- Automatic results may require review or correction.
- Decorative text and complex artwork may use fallback cleanup.
- Model packages and caches require substantial disk space.
- Local Qwen depends on compatible native hardware/runtime support.
- Automatic application updates are not implemented.
- v1 ships as a complete offline installer. Selectable Cloud/Core, Qwen,
  and Marian web-downloaded runtime components are deferred.
- Human visual acceptance, packaged-build verification, and remaining polish are
  release steps separate from the current v1 implementation.
