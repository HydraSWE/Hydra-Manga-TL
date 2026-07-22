# Hydra Manga TL

![Hydra Manga TL logo](assets/logos/mainlogo.png)

Hydra Manga TL is a Windows desktop workspace for translating manga and comic
pages into English. It brings OCR, local machine translation, text removal,
typesetting, review, and PNG export into one non-destructive project workflow.

Import a page, a selection of images, or an entire chapter. Hydra detects the
source text, translates supported pages, reconstructs the artwork, and keeps the
original and translated versions side by side while you review the result.

> **Current release:** 0.6.0 — active development. English is the only output language today. 
> Japanese and Chinese translation are supported; detected
> Latin-script text is preserved.

## What Hydra does

- Imports JPG, JPEG, PNG, WEBP, TIFF, and BMP files, including nested folders.
- Runs Japanese, Chinese, and English OCR candidates and selects the strongest result.
- Translates Japanese and Chinese text to English with local Marian models.
- Translates one selected page, multiple selected pages, or all pending pages.
- Supports optional Google, Gemini, Groq, and DeepSeek providers while keeping local translation as the default.
- Applies manga localization with project glossaries and ambiguity review flags.
- Removes source lettering, reconstructs the background, and typesets translated text.
- Shows original and translated pages together with selectable OCR overlays.
- Lets you edit translation, font, size, color, alignment, and X/Y placement per block.
- Adds and translates a manual text box even before the rest of the page is processed.
- Plays selected original text with installed Windows Japanese or Chinese voices.
- Uses bubble-aware placement, balanced wrapping, and Manga, Comic, or Novel text presets.
- Removes unwanted automatic blocks and restores them later if needed.
- Autosaves versioned projects without changing the source images.
- Exports translated pages as PNG while preserving the imported folder structure.

For a compact capability listing, see [catelog.md](catelog.md). For architecture,
project status, and contributor notes, see [project.md](project.md).

## Before you install

Hydra Manga TL is being prepared for distribution through a Windows setup EXE
that installs the complete desktop application. The source workflow below remains
available for development and advanced users.

You will need:

- Windows 10 or Windows 11, 64-bit.
- 64-bit Python. The current project is tested with Python 3.12.
- Git, or a downloaded ZIP of this repository.
- An internet connection during installation and the first OCR/translation run.
- Several gigabytes of free space for Python packages and downloaded AI models.

A dedicated GPU is optional. The application can run on CPU, but OCR and
translation will be slower. Source images are never overwritten. Processing is
local after the required OCR and translation models have been downloaded.

Local Marian translation and Windows speech do not require an API key. Hosted
services are strictly opt-in and may have provider-controlled quotas or charges;
Hydra never switches to a cloud provider automatically. API keys are stored in
Windows Credential Manager rather than project or settings files.

## Install

Open Windows PowerShell and run:

```powershell
git clone https://github.com/HydraSWE/Hydra-Manga-TL.git
cd Hydra-Manga-TL
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

If the repository is already on your computer, start with `cd` and the path to
the repository, then run the final three commands.

### Optional LaMa title cleanup

Hydra can remove title or narration text printed directly on background art with
a separate LaMa/iopaint helper runtime. This runtime is intentionally separate
from the main app environment because iopaint pins older Pillow packages that
conflict with Hydra's normal renderer.

For source runs:

```powershell
.\scripts\build_inpaint_runtime.ps1
```

The helper is installed to:

```text
runtime\inpaint
```

If it is missing, Hydra still runs and falls back to the standard OpenCV
background cleanup. Render reports will include an `inpaint_warning` explaining
that the LaMa runtime is unavailable.

For packaged builds, `scripts\build_windows_dist.ps1` also builds
`runtime\inpaint\hydra-inpaint.exe`, a standalone helper executable that the
frozen Hydra app can launch without sharing Python packages with the main app.

## Start Hydra

From the repository folder:

```powershell
.\.venv\Scripts\python main.py
```

With the virtual environment activated, this is equivalent:

```powershell
python -m hydra_manga_tl
```

## After installation

The first translation can take longer because PaddleOCR and Hugging Face model
files are downloaded and cached. Keep the terminal open while the application is
running so startup or model errors remain visible.

Your normal workflow is:

1. Drop manga images or a folder onto the Project Home screen, or use **Import Manga**.
2. Select one or more pages and choose **Translate Selected**, or choose
   **Translate All Pending** for the complete queue.
3. Review completed pages while later pages continue in the background.
4. Select a text block to correct its translation or typesetting, then choose
   **Apply & Rerender**.
5. If OCR missed a bubble, choose **Add Text Box** and draw around that text on
   the original page. Remove or restore automatic blocks as needed.
6. Choose **Export** and select a separate output folder.

Hydra creates and autosaves working projects here:

```text
%LOCALAPPDATA%\Hydra Manga TL\projects
```

Application logs are written here:

```text
%LOCALAPPDATA%\Hydra Manga TL\logs\app.log
```

Project records, OCR data, translations, previews, and render artifacts stay in
the application data folder. Exported PNG files go only to the folder you choose.

## Updating

From the repository folder:

```powershell
git pull
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Existing unified projects are versioned and loaded through `project.json`. Keep a
backup of important project folders while the application is in active development.

## Troubleshooting

### PowerShell cannot run `Activate.ps1`

Activation is not required; all commands above call the virtual environment's
Python executable directly. You can continue using that form.

### `No module named ...`

Make sure you launch with `.\.venv\Scripts\python`, then repeat the dependency
installation command. Do not launch the project with an unrelated Python installation.

### The first translation appears stuck

The initial OCR and translation model downloads can be large. Check the terminal
and `%LOCALAPPDATA%\Hydra Manga TL\logs\app.log` for download or network errors.

### A bubble was missed or grouped incorrectly

Translate the page first, select **Add Text Box**, and draw one rectangle around
the intended text area. Hydra OCRs and translates it as an independent manual
block. You can also remove an incorrect automatic block and restore it later.

## Advanced command-line tools

The desktop workspace is the main product. The underlying stages remain callable
for diagnostics and development.

OCR and language detection:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.cli samples --output outputs\phase1
```

Translate OCR results to English:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.translate_cli outputs\phase1 --output outputs\phase2 --target en
```

Reconstruct translated images:

```powershell
.\.venv\Scripts\python -m hydra_manga_tl.phase3_cli outputs\phase2 --output outputs\phase3 --policy complete
```

These tools produce JSON reports and intermediate artifacts without modifying
the input images. The legacy Phase 4 editor entry point is retained for opening
older Phase 2 results, but new work should use `main.py`.

## Development

Run the test suite:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Build a Windows one-folder distribution:

```powershell
.\scripts\build_windows_dist.ps1
```

That command builds `runtime\inpaint` first, then runs PyInstaller. If you need a
smaller diagnostic build without LaMa title cleanup, use:

```powershell
.\scripts\build_windows_dist.ps1 -SkipInpaintRuntime
```

The frozen app looks for the inpaint helper in this order:

1. `HYDRA_INPAINT_PYTHON`
2. `runtime\inpaint\hydra-inpaint.exe` beside the exe
3. `runtime\inpaint\python.exe` or `runtime\inpaint\Scripts\python.exe` beside the exe
4. the same paths inside a PyInstaller bundle
5. source checkout `.venv-inpaint\Scripts\python.exe`

## License

Hydra Manga TL is available under the [MIT License](LICENSE).
