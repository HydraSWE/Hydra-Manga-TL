# Hydra Manga TL

![Hydra Manga TL logo](assets/logos/mainlogo.png)

Hydra Manga TL is a local-first Windows desktop application for translating,
reconstructing, reviewing, and exporting manga pages as editable translated
images and comic-ready documents.
It keeps source images untouched and stores OCR, translations, manual regions,
review decisions, and rendered output in a versioned project.

## Current version

**Hydra Manga TL v1** is the current release line. It extends the
unified OCR/translation/render pipeline with verified stage-level resume,
provider-safe Smart Translation, large-project responsiveness, multi-target
output, generalized editor undo/redo, reading-order controls, expanded export
formats, runtime diagnostics, polished batch selection, a refreshed Project
Home, a searchable recent-project library, and a cleaner workspace UI.
HydraMangaAi remains an optional private bridge layer when the separate package
is present.

| Version | Status | Milestone |
| --- | --- | --- |
| **v1** | Current release | Manifest-v3 pipeline resume, centralized planning foundation, verified OCR/translation/render reuse, automatic schema migration and backward compatibility, provider-safe Smart Translation, large-project workspace responsiveness, refreshed Project Home and workspace UI, searchable recent-project library, multi-target artifacts, PDF/CBZ/WebP export, generalized undo/redo, editable reading order, filmstrip batch-selection polish, diagnostics bundles, detailed GPU/native status, configurable app-data storage, and the complete v0.9.0 workspace/pipeline foundation |
| **v0.9.0** | Completed foundation | Branded startup warmup, collapsible filmstrip, pinned identity tile, rectangle/polygon Region Tool, interactive translated-text layout editing, global text normalization, domain package layout, unified request pipeline, and persistent OCR/render services |
| **v0.8.0-alpha** | Completed baseline | Unified translation pipeline, persistent OCR runtime, shared translation and render services, bounded SmartOCR retries, cancellation, cache normalization, and resilient manual rerendering |
| **v0.7.0** | Optional/private bridge milestone | **HydraMangaAi**: private correction capture, approval queues, training-data gates, model promotion metadata, and AI Center integration |
| **v0.6.0** | Completed foundation | Unified desktop workspace, selected-page translation, manual text boxes, editing, review, speech, and export |

English, Spanish, French, German, Italian, and Portuguese are available output
targets. Japanese and Chinese source text are supported, while detected
Latin-script content can be preserved. Provider/model language-pair support
still applies.

## What v1 Adds

- Extends the existing chapter manifest to version 3 with source, input, and
  output SHA-256 digests; application/settings identity; provider/model
  identity; completion timestamps; and per-stage errors.
- Reuses OCR, translation, or completed rendering only when the corresponding
  source, artifacts, and policy fingerprints still match. A verified
  translation can be reused while only rendering and review are rebuilt.
- Preserves deterministic Translation Memory and legacy JSON cache contracts;
  source-region geometry remains metadata rather than a Translation Memory
  lookup key.
- Adds target-isolated translation, rendering, editing, manifest, timing, and
  export state for English, Spanish, French, German, Italian, and Portuguese.
- Adds centralized pipeline planning and validation foundations so resume,
  invalidation, and stage reuse decisions stay in one pipeline authority.
- Adds provider-safe Fast worker concurrency for Smart Translation. User worker
  requests are honored within selected-provider limits, including conservative
  Groq and local-model caps.
- Improves large-project responsiveness with async project opening, deferred
  restore, chunked filmstrip building, batched thumbnail loading, and targeted
  progress refreshes.
- Adds bounded editor undo/redo for text/style edits, manual-region changes,
  polygon redraw, automatic-region suppression/restoration, layout movement,
  project style, and approval state.
- Adds editable numbered reading order, reset to automatic order, typography
  fit preview, and review filters for untranslated text, residual source
  script, overflow, missing glyphs, low OCR confidence, and provider fallback.
- Polishes filmstrip keyboard batching: `Ctrl+Left/Right` grows the batch,
  `Ctrl+Space` toggles the current page, and selection survives refresh/reorder.
- Adds PDF export alongside PNG/JPEG/WebP folders and ZIP/CBZ archives, with
  target-aware output names.
- Adds redacted diagnostics bundles, fatal exception logging, and detailed
  NVIDIA device, driver, compute capability, VRAM, utilization, Torch,
  llama.cpp, Paddle, dependency, and load-test status.
- Makes the Hydra application-data root configurable while preserving external
  projects and exports during recent-project cleanup.
- Refreshes Project Home with clearer import actions, polished recent-project
  cards, and a compact landing view that keeps the latest projects visible.
- Adds a searchable recent-project library through `View All`, while keeping
  the landing page limited to the most recent entries.
- Refreshes the workspace chrome with grouped controls, icon-assisted actions,
  canvas headers, status labels, and cleaner filmstrip and inspector styling.
- Polishes startup and project import loading surfaces with clearer staged
  progress feedback.
- Adds an automated project compatibility and migration system. Safely rejects
  future-schema projects, prompts users before upgrading old schemas,
  automatically creates ZIP backups before migrating, and restores the original
  project if migration fails or encounters corruption.

## Core Pipeline Included In v1

- Imports JPG, JPEG, PNG, WEBP, TIFF, and BMP files or recursive folders.
- Runs OCR through one application-lifetime PaddleOCR worker.
- Uses Fast, Balanced, and Maximum retry budgets with uncertain OCR routed to
  Review instead of silently accepting weak text.
- In **Fast** mode, the app still prepares pages through the OCR/Reading stage
  first. The visible Fast worker counter refers to the later parallel
  translation stage, so it may stay hidden or inactive while pages are still
  being read.
- Translates all pending pages, selected pages, or a user-drawn manual region.
- Sends batch, selected, manual, and review work through typed request states:
  queued, OCR, translating, rendering, done, failed, and cancelled.
- Reuses one configured translation manager until its engine, model, glossary,
  or provider configuration changes.
- Supports local MarianMT and optional Local Qwen GGUF translation.
- Supports opt-in Groq, Google Translate, Gemini, and DeepSeek engines.
- Serializes rendering through one shared application-level render queue.
- Removes source lettering, reconstructs backgrounds, and fits English text
  inside detected or manually selected bounds.
- Keeps manual text inside the exact rectangle drawn by the user.
- Rolls back a newly added manual region if rendering fails or is cancelled
  before rendering begins.
- Shows original and translated pages side by side with selectable overlays.
- Supports text, font, size, color, alignment, placement, and bubble-type edits.
- Supports target switching without overwriting another target's artifacts or
  editor state.
- Removes automatic blocks and restores them later.
- Provides OCR and translation review queues.
- Autosaves projects and exports folders, PDF, ZIP, or CBZ without modifying
  source images.

## Historical baseline: v0.8.0-alpha

v0.8.0-alpha was the completed unified-pipeline checkpoint that established the
current OCR, translation, render, cache, cancellation, and manual-rerender
architecture. v1 keeps that pipeline and the v0.9.0 workspace foundation
active while adding recoverability, editor workflow, diagnostics, multi-target
state, provider-safe scheduling, large-project responsiveness, and expanded
export work.

For a compact product listing, see [catalog.md](catalog.md). For architecture,
data contracts, and contributor guidance, see [project.md](project.md).

## Requirements

- Windows 10 or Windows 11, 64-bit.
- 64-bit Python 3.12 for source development.
- Git or a downloaded repository archive.
- Internet access for dependency installation and initial model downloads.
- Several gigabytes of free space for Python packages and model caches.

A GPU is optional. PaddleOCR and MarianMT can run on CPU, although processing
will be slower. Local Qwen requires a compatible GGUF model and native runtime;
selecting it does not by itself guarantee that a particular machine can create
a working model context.

Cloud engines are opt-in and may have provider-controlled quotas or charges.
Hydra does not silently switch a selected cloud engine to a local engine.
Credentials are stored through Windows Credential Manager rather than inside
project files.

## Install from source

Open PowerShell:

```powershell
git clone https://github.com/HydraSWE/Hydra-Manga-TL.git
cd Hydra-Manga-TL
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

For release packaging, prefer the build script instead of manually installing
packages from VS Code. The script creates and syncs both `.venv` and
`.venv-inpaint`, routes pip temporary downloads into the repository `build`
folder, and avoids filling the default Windows temp drive during large CUDA
wheel downloads.

Start the desktop application:

```powershell
.\.venv\Scripts\python main.py
```

The equivalent module command is:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl
```

## Normal workflow

1. Drop manga files or a folder onto Project Home, or select **Import Manga**.
2. Reopen recent work from the landing cards, or use **View All** to search
   the recent-project library.
3. Select pages and choose **Translate Selected**, or choose
   **Translate All Pending**.
4. Review completed pages while later queued work continues.
5. Correct OCR, translation, or typography in the inspector and select
   **Apply & Rerender**.
6. For missed text, select **Add Text Box** and draw one rectangle on the
   original page.
7. Use **Next OCR Issue** and **Next Review Issue** to inspect uncertain blocks.
8. Select **Export** and choose a separate destination folder.

The unified Cancel action covers batch work, manual OCR/translation, and manual
renders that are still waiting in the render queue. A render already in progress
is allowed to finish safely.

## Project and log locations

Hydra defaults to:

```text
%LOCALAPPDATA%\Hydra Manga TL
```

You can change this data folder from **Settings**. New projects, logs, caches,
and Translation Memory use the selected folder; existing project folders are
not moved automatically.

Projects:

```text
%LOCALAPPDATA%\Hydra Manga TL\projects
```

Application log:

```text
%LOCALAPPDATA%\Hydra Manga TL\logs\app.log
```

Global OCR, translation, and model caches also live under the Hydra Manga TL
application-data root. Project artifacts stay separate from source images.

## Optional LaMa art-text cleanup

Hydra can use an isolated LaMa/iopaint helper for text printed directly on
background artwork:

```powershell
.\scripts\build_inpaint_runtime.ps1
```

The helper is written under `runtime\inpaint`. If it is unavailable, Hydra uses
the standard OpenCV cleanup path and records an `inpaint_warning` in the render
report.

## HydraMangaAi optional bridge

HydraMangaAi was introduced during the v0.7.0 milestone and remains the private,
optional learning layer for approved human corrections and future task-specific
AI models. When the ignored
`HydraMangaAi` package is present, Hydra can capture local correction drafts for
OCR, translation, bubble, layout, cleaning, and quality tasks.

Drafts do not become training data automatically. **Approve Bubble** and
**Approve Page** explicitly admit corrections to immutable training snapshots.
AI Center exposes dataset counts, promotion gates, training state, and active
model metadata. Runtime data defaults to:

```text
D:\HydraMangaAiData
```

The v1 OCR/translation/render pipeline remains authoritative. HydraMangaAi
only becomes active for a task after a candidate has sufficient approved data
and passes its promotion gate. Removing the private package disables learning
without disabling manga translation.

## Command-line diagnostics

OCR:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.project.discovery samples --output outputs\phase1
```

Translate OCR results:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.translation.cli outputs\phase1 --output outputs\phase2 --target en
```

Reconstruct translated pages:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.phase.phase3 outputs\phase2 --output outputs\phase3 --policy complete
```

Unified runtime smoke:

```powershell
.\.venv\Scripts\python scripts\smoke_unified_runtime.py --project "path\to\project.json" --image-count 2 --engine marian
```

The smoke harness creates a disposable project and leaves the supplied project,
source images, and source artifacts unchanged.

## Development and validation

Run the complete test suite:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Run tests from the repository root, not from inside the `tests` folder. Python
module names use dots and omit `.py`:

```powershell
.\.venv\Scripts\python -m unittest tests.test_parallel_scheduler
.\.venv\Scripts\python -m unittest tests.test_parallel_scheduler.ParallelPageSchedulerTests
.\.venv\Scripts\python -m unittest tests.test_parallel_scheduler.ParallelPageSchedulerTests.test_worker_tiers_and_override
```

The v1 test plan covers native OCR worker startup, queue cancellation,
shutdown, Qt control states, exact manual bounds, render rollback, startup
progress, project migration, filmstrip selection/persistence, manual source
geometry, editor history, reading order, multi-target isolation, target-aware
exports, manifest-v3 digest/policy invalidation, translation-only rerender,
diagnostics redaction, and GPU/native status.

Full-suite status can vary while the branch is dirty; validate the current
checkout before packaging a release.

Build a Windows one-folder distribution only when preparing a release:

```powershell
.\scripts\build_windows_dist.ps1
```

The release build script owns the packaging environments:

- `.venv` installs `requirements.txt` for the main desktop application.
- `.venv-inpaint` installs `requirements-inpaint.txt` for the isolated
  LaMa/iopaint helper.
- `runtime\inpaint\hydra-inpaint.exe` is built from `.venv-inpaint` and then
  bundled by PyInstaller.
- pip temporary downloads are routed to `build\pip-temp` and
  `build\pip-temp-inpaint` so multi-gigabyte wheels do not use the default
  `%TEMP%` location.

Do not use VS Code's Python Envs package installer for release dependency
syncs. It may run plain `pip install -r requirements.txt` through the default
Windows temp directory, which can fail on large CUDA packages even when the
repository drive has enough space.

Use `-SkipInpaintRuntime` for a diagnostic build without the optional isolated
LaMa helper. After a successful build, `build\pip-temp`,
`build\pip-temp-inpaint`, `build\pip-cache`, `build\pip-cache-inpaint`,
`build\HydraMangaTL`, and `build\inpaint-helper` can be deleted to recover
space. Keep `.venv`, `.venv-inpaint`, `runtime\inpaint`, and
`dist\Hydra Manga TL` unless you intentionally want the next build to download
and recreate them.

## Windows installer

The current v1 Inno Setup release is a complete offline installer. It
contains the full PyInstaller one-folder distribution, including local and
cloud translation support, so users do not download runtime components during
setup.

A smaller web/bootstrap installer with independently selectable Cloud/Core,
Qwen Local, and Marian Local downloads is planned for a later release. It is
not part of v1.

## Troubleshooting

### PowerShell cannot run `Activate.ps1`

Activation is optional. Use the explicit
`.\.venv\Scripts\python` commands shown above.

### `No module named ...`

Confirm that Hydra is running through the repository virtual environment, then
repeat the dependency installation command.

### The first request appears slow

Initial OCR and local translation model downloads and warm-up can take time.
Inspect the terminal and the app log in the configured data folder. By default:
`%LOCALAPPDATA%\Hydra Manga TL\logs\app.log`.

### A bubble was missed

Select **Add Text Box** and draw one rectangle around the intended source text.
Hydra OCRs the crop, translates it through the shared runtime, and renders the
English text inside that exact rectangle.

### A result is uncertain

Use **Next OCR Issue** for source-recognition problems and
**Next Review Issue** for translation or rendering concerns. Hydra deliberately
routes uncertain content to Review.

## License

Hydra Manga TL is available under the [MIT License](LICENSE).
