# Hydra Manga TL

![Hydra Manga TL logo](assets/logos/mainlogo.png)

Hydra Manga TL is a local-first Windows desktop application for translating,
reconstructing, reviewing, and exporting manga pages as editable English PNGs.
It keeps source images untouched and stores OCR, translations, manual regions,
review decisions, and rendered output in a versioned project.

## Current version

**Hydra Manga TL v0.9.0** is the current development line. It includes the
unified OCR/translation/render pipeline plus startup polish, responsive
workspace controls, manual region upgrades, layout editing, package
reorganization, and brand identity improvements. HydraMangaAi remains an
optional private bridge layer when the separate package is present.

| Version | Status | Milestone |
| --- | --- | --- |
| **v0.9.0** | Current development | Branded startup warmup, collapsible filmstrip, pinned identity tile, rectangle/polygon Region Tool, interactive translated-text layout editing, global text normalization, startup-safe lazy imports, domain package layout, unified request pipeline, persistent OCR runtime, shared translation/render services, cancellation, cache normalization, and resilient manual rerendering |
| **v0.8.0-alpha** | Completed baseline | Unified translation pipeline, persistent OCR runtime, shared translation and render services, bounded SmartOCR retries, cancellation, cache normalization, and resilient manual rerendering |
| **v0.7.0** | Optional/private bridge milestone | **HydraMangaAi**: private correction capture, approval queues, training-data gates, model promotion metadata, and AI Center integration |
| **v0.6.0** | Completed foundation | Unified desktop workspace, selected-page translation, manual text boxes, editing, review, speech, and export |

English is currently the output language. Japanese and Chinese source text are
supported, while detected Latin-script content can be preserved.

## What v0.9.0 adds

- Shows a branded Hydra startup warmup screen before heavy UI, OCR,
  translation, and renderer imports finish.
- Reports ordered startup phases for settings, assets, local provider
  validation, renderer setup, workspace restore, and background warmups.
- Keeps OCR and selected local translation warmups nonfatal after the
  core-ready handoff, with status-bar warnings and lazy retry on first use.
- Keeps Qwen/cloud startup isolated from Marian warmup; cloud provider checks
  remain local and make no network requests.
- Adds a collapsible Filmstrip section whose expanded state is saved per
  project and restored when projects are reopened or switched.
- Pins a Hydra identity tile before Page 1 in the filmstrip without adding it
  to image IDs, page counts, drag/drop, translation queues, deletion, or
  export.
- Opens a scalable identity preview dialog when the identity tile is clicked.
- Upgrades `assets/thumbnail/hydra.png` into a high-contrast Hydra identity
  thumbnail that remains recognizable at the filmstrip preview size.
- Extends manual regions with rectangle and polygon source geometry while
  preserving compatibility with `manual_rect`.
- Adds interactive translated-text layout adjustment on the current canvas
  without changing OCR/source geometry until rerender.
- Normalizes escaped Unicode, curly punctuation, odd spaces, zero-width
  characters, and repeated punctuation through shared text boundaries.

## Core pipeline included in v0.9.0

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
- Removes automatic blocks and restores them later.
- Provides OCR and translation review queues.
- Autosaves projects and exports PNG files without modifying source images.

## Historical baseline: v0.8.0-alpha

v0.8.0-alpha was the completed unified-pipeline checkpoint that established the
current OCR, translation, render, cache, cancellation, and manual-rerender
architecture. v0.9.0 keeps that pipeline as part of the active app while adding
startup, workspace, Region Tool, layout, identity, and package-layout work.

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
2. Select pages and choose **Translate Selected**, or choose
   **Translate All Pending**.
3. Review completed pages while later queued work continues.
4. Correct OCR, translation, or typography in the inspector and select
   **Apply & Rerender**.
5. For missed text, select **Add Text Box** and draw one rectangle on the
   original page.
6. Use **Next OCR Issue** and **Next Review Issue** to inspect uncertain blocks.
7. Select **Export** and choose a separate destination folder.

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

The v0.9.0 OCR/translation/render pipeline remains authoritative. HydraMangaAi
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

The v0.9.0 test plan covers native OCR worker startup, queue cancellation,
shutdown, Qt control states, exact manual bounds, render rollback, startup
progress, application import laziness, project migration, per-project filmstrip
persistence, identity-tile exclusion from page order, manual region geometry,
layout editing, package import paths, and global text normalization.

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
