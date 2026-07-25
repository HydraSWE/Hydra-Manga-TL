# Hydra Manga TL

![Hydra Manga TL logo](assets/logos/mainlogo.png)

Hydra Manga TL is a local-first Windows desktop application for translating,
reconstructing, reviewing, and exporting manga pages as editable English PNGs.
It keeps source images untouched and stores OCR, translations, manual regions,
review decisions, and rendered output in a versioned project.

## Current version

**Hydra Manga TL v0.8.0** is finished and currently under polishing. The
previous **v0.7.0** milestone introduced HydraMangaAi and remains under
development as the optional private bridge layer.

| Version | Status | Milestone |
| --- | --- | --- |
| **v0.8.0** | Finished, under polishing | Unified translation pipeline, persistent OCR runtime, shared translation and render services, bounded SmartOCR retries, cancellation, cache normalization, and resilient manual rerendering |
| **v0.7.0** | Under development | **HydraMangaAi**: private correction capture, approval queues, training-data gates, model promotion metadata, and AI Center integration |
| **v0.6.0** | Completed foundation | Unified desktop workspace, selected-page translation, manual text boxes, editing, review, speech, and export |

English is currently the output language. Japanese and Chinese source text are
supported, while detected Latin-script content can be preserved.

## What v0.8.0 provides

- Imports JPG, JPEG, PNG, WEBP, TIFF, and BMP files or recursive folders.
- Runs OCR through one application-lifetime PaddleOCR worker.
- Uses Fast, Balanced, and Maximum retry budgets with uncertain OCR routed to
  Review instead of silently accepting weak text.
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

## HydraMangaAi — v0.7.0 under development

HydraMangaAi is the private, optional learning layer introduced in the v0.7.0
milestone. It remains under development as the bridge between approved human
corrections and future task-specific AI models. When the ignored
`HydraMangaAi` package is present, Hydra can capture local correction drafts for
OCR, translation, bubble, layout, cleaning, and quality tasks.

Drafts do not become training data automatically. **Approve Bubble** and
**Approve Page** explicitly admit corrections to immutable training snapshots.
AI Center exposes dataset counts, promotion gates, training state, and active
model metadata. Runtime data defaults to:

```text
D:\HydraMangaAiData
```

The finished v0.8.0 OCR/translation/render pipeline remains authoritative while
polishing continues. HydraMangaAi only becomes active for a task after a
candidate has sufficient approved data and passes its promotion gate. Removing
the private package disables learning without disabling manga translation.

## Command-line diagnostics

OCR:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.cli samples --output outputs\phase1
```

Translate OCR results:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.translate_cli outputs\phase1 --output outputs\phase2 --target en
```

Reconstruct translated pages:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.phase3_cli outputs\phase2 --output outputs\phase3 --policy complete
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

The completed v0.8.0 unified-pipeline checkpoint passes 140 automated tests,
including native OCR worker startup, queue cancellation, shutdown, Qt control
states, exact manual bounds, and render rollback coverage.

Build a Windows one-folder distribution only when preparing a release:

```powershell
.\scripts\build_windows_dist.ps1
```

Use `-SkipInpaintRuntime` for a diagnostic build without the optional isolated
LaMa helper.

## Troubleshooting

### PowerShell cannot run `Activate.ps1`

Activation is optional. Use the explicit
`.\.venv\Scripts\python` commands shown above.

### `No module named ...`

Confirm that Hydra is running through the repository virtual environment, then
repeat the dependency installation command.

### The first request appears slow

Initial OCR and local translation model downloads and warm-up can take time.
Inspect the terminal and `%LOCALAPPDATA%\Hydra Manga TL\logs\app.log`.

### A bubble was missed

Select **Add Text Box** and draw one rectangle around the intended source text.
Hydra OCRs the crop, translates it through the shared runtime, and renders the
English text inside that exact rectangle.

### A result is uncertain

Use **Next OCR Issue** for source-recognition problems and
**Next Review Issue** for translation or rendering concerns. v0.8.0 deliberately
routes uncertain content to Review.

## License

Hydra Manga TL is available under the [MIT License](LICENSE).
