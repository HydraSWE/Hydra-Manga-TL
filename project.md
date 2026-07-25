# Hydra Manga TL — v0.8.0 Project Guide

## Project summary

Hydra Manga TL is a non-destructive Windows desktop pipeline for OCR,
Japanese/Chinese-to-English manga translation, artwork reconstruction,
typesetting, review, manual correction, and PNG export.

The v0.8.0 milestone replaces separate translation and rendering owners with a
unified request architecture. Batch, selected-page, manual-region, and review
work share application-lifetime OCR and translation services, one serialized
render queue, normalized progress states, cooperative cancellation, and stable
cache contracts.

Status: v0.8.0 is finished and currently under polishing. v0.7.0 is the
HydraMangaAi bridge milestone and remains under development.

## Release history

| Version | Status | Scope |
| --- | --- | --- |
| **v0.8.0 — Unified Pipeline** | Finished, under polishing | Persistent OCR worker, SmartOCR retry budgets, typed requests, one translation manager, one render queue, grouped chapter execution, cancellation, normalized caches, exact manual bounds, rollback-safe rendering |
| **v0.7.0 — HydraMangaAi** | Under development | Optional private correction capture, explicit approval queues, training snapshots, readiness gates, promotion metadata, rollback records, and AI Center |
| **v0.6.0 — Unified Workspace** | Completed foundation | Project Home, selected-page translation, manual regions, editor overrides, speech, review, autosave, and export |
| **v0.5.0 — Desktop Consolidation** | Completed foundation | Consolidated staged prototypes into the Project Home and review workspace |

## Product invariants

1. Source images are never overwritten.
2. Manual English stays inside the selected `manual_rect`.
3. Failed or cancelled queued manual rendering restores project state.
4. PaddleOCR belongs to one application-lifetime OCR runtime.
5. Translation uses one configured manager at a time.
6. All rendering is serialized through one application-level queue.
7. Manual work remains asynchronous and does not block the Qt event loop.
8. Uncertain OCR or translation is routed to Review.
9. Existing page and selection caches remain physically separate behind a
   shared cache-key facade.

## Current scope

| Area | v0.8.0 behavior |
| --- | --- |
| Platform | Windows 10/11 desktop application using PySide6 |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP; individual files or recursive folders |
| OCR | Shared PaddleOCR runtime with Japanese, Chinese, and Latin-script evidence |
| OCR quality | Fast = 0 retries, Balanced = 1, Maximum = 3 |
| Translation | MarianMT, optional Local Qwen GGUF, Groq, Google Translate, Gemini, DeepSeek |
| Requests | Batch, selected, manual, and review |
| States | Queued, OCR, translating, rendering, done, failed, cancelled |
| Rendering | One serialized queue for batch, manual, editor, and review work |
| Review | OCR issue queue, translation issue queue, render review metadata |
| Editing | Wording, replace/skip, font, size, color, alignment, offsets, bubble type |
| Recovery | Manual regions, automatic-block suppression/restoration, rollback |
| Persistence | Versioned `project.json` plus per-page artifacts and global caches |
| Export | English PNG files preserving imported relative paths |

English is the current output language. Automatic output remains reviewable and
editable because decorative lettering, unusual artwork, and ambiguous source
text may require human judgment.

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
| `application.py` | Qt bootstrap, branding, logging, warm-up, and orderly shutdown |
| `ui.py` | Project Home, workspace, canvases, filmstrip, editor, review navigation, unified Cancel |
| `workspace.py` | Project orchestration, manual transactions, rollback, recent projects, export |
| `project.py` | Versioned project and image schema |
| `translation_requests.py` | Typed translation and render request contracts |
| `translation_queue.py` | Serialized request ownership, grouped execution, states, cancellation |
| `translation_runtime.py` | Application-lifetime translation manager and configuration reuse |
| `render_queue.py` | Serialized rendering, queued cancellation, completion/failure correlation |
| `pipeline.py` | Reusable OCR, dialogue, translation, payload, render, and chapter stages |
| `ocr_runtime.py` / `ocr_worker.py` | App-lifetime subprocess lifecycle and telemetry |
| `ocr_service.py` | Shared page and selection OCR entry points |
| `ocr_manager.py` | Retry budgets, candidate scoring, uncertainty and Review policy |
| `translation_cache_store.py` | Shared stable keys for page and selection caches |
| `context_engine.py` | Chapter context and prior-page memory |
| `translation_engines/` | Marian, Qwen GGUF, and opt-in remote page engines |
| `renderer.py` / `phase3_cli.py` | Masking, cleanup, text fitting, exact manual placement, reports |
| `review.py` / `ocr_review.py` | Translation, render, and OCR review support |
| `ai_bridge.py` | Optional v0.7.0 HydraMangaAi integration |

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
- HydraMangaAi subject and approval identifiers when the private layer is used.

Application-owned global data lives under:

```text
%LOCALAPPDATA%\Hydra Manga TL
```

Separate OCR, page-translation, and manual-selection cache files are retained
for migration safety while sharing normalized cache-key helpers.

## HydraMangaAi — v0.7.0 under development

HydraMangaAi is an optional private package connected through `ai_bridge.py`.
It captures correction drafts but requires explicit approval before data enters
training snapshots. This layer remains under development as the bridge from
approved review data to future task-specific models. Readiness is task-specific
and data-gated; candidate models must pass promotion rules before they can
become active. The finished v0.8.0 OCR/translation/render pipeline remains
authoritative while polishing continues.

Removing HydraMangaAi leaves the translation product operational.

## Repository layout

```text
assets/                       Icons and logos
hydra_manga_tl/               Desktop application and pipeline
hydra_manga_tl/translation_engines/
                              Translation providers and manager
scripts/                      Runtime smoke, benchmark, release, and build tools
samples/                      Diagnostic manga images
tests/                        Unit, integration, native-worker, and offscreen Qt tests
main.py                       Desktop entry point
requirements.txt             Runtime and development dependencies
README.md                     Installation and user guide
catalog.md                    Concise product catalog
project.md                    Architecture and project guide
TODO.md                       Completed unified-pipeline validation ledger
```

## Validation status

The completed v0.8.0 checkpoint includes:

- 140 passing automated tests.
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
- English is the only output language.
- Automatic results may require review or correction.
- Decorative text and complex artwork may use fallback cleanup.
- Model packages and caches require substantial disk space.
- Local Qwen depends on compatible native hardware/runtime support.
- Automatic application updates are not implemented.
- Human visual acceptance, packaged-build verification, and remaining polish are
  release steps separate from the finished v0.8.0 implementation.
