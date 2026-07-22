# Hydra Manga TL — Project Guide

## Project summary

Hydra Manga TL is a local-first Windows desktop application for translating and
re-typesetting manga pages. Version 0.6.0 extends the unified workspace with
selected-page jobs, local-first provider adapters, manga localization, speech,
partial manual translation, and bubble-aware typesetting. Version 0.5.0 unified the original staged prototypes
into a two-screen experience: Project Home for import and recent work, followed
by a focused original/translated review workspace.

The product is designed around three rules:

1. Never overwrite the user's source images.
2. Keep automatic results editable and reversible.
3. Preserve enough intermediate data to reopen, review, and rerender a project.

## Current product scope

| Area | Current behavior |
| --- | --- |
| Platform | Windows desktop application built with PySide6 |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP; files or recursive folders |
| OCR | PaddleOCR candidates for Chinese, Japanese, and English |
| Translation | Local Marian Japanese-to-English and Chinese-to-English models |
| Latin text | Preserved when English/Latin-script content is detected |
| Reconstruction | Polygon masking, background inpainting, and fitted replacement text |
| Review | Side-by-side canvases, overlays, page filmstrip, progress, and review states |
| Editing | Text, replace/skip, font, size, color, alignment, and X/Y offset |
| Recovery | Manual text boxes, automatic-block removal, and restoration |
| Persistence | Autosaved, versioned `project.json` plus per-page artifacts |
| Export | PNG files in a user-selected folder, preserving relative paths |

English is currently the only output language. The quality selector is stored in
the project, but the processing pipeline does not yet apply different Fast,
Balanced, or Maximum strategies.

## User journey

1. The user imports images from Project Home.
2. Hydra creates a project under `%LOCALAPPDATA%\Hydra Manga TL\projects` and
   records the source paths without copying over or modifying the originals.
3. A background worker analyzes each pending page, performs OCR, groups regions,
   translates supported text, and renders a translated page.
4. Finished pages become reviewable while the remaining queue continues.
5. The user corrects automatic blocks or draws a manual region for missed text.
6. Edits trigger a non-destructive rerender and are saved to the project.
7. Export copies completed renders to a destination chosen by the user.

## Architecture

| Module | Responsibility |
| --- | --- |
| `application.py` | Qt application bootstrap, branding, logging, and window lifecycle |
| `ui.py` | Project Home, workspace, canvases, filmstrip, inspector, and progress UI |
| `workspace.py` | Project orchestration, recent projects, edits, rerendering, and export |
| `project.py` | Unified versioned project schema |
| `pipeline.py` | Background OCR, retry, translation, reconstruction, and cancellation |
| `ocr.py` | PaddleOCR adapters, model selection, and focused selection retries |
| `language.py` | Unicode-script evidence and OCR model-fit scoring |
| `layout.py` | OCR region grouping and manual-region composition |
| `translation.py` | Marian model loading, local translation, and review flags |
| `renderer.py` / `phase3_cli.py` | Masking, inpainting, text fitting, and render reports |
| `manual_region.py` | User-drawn region OCR/translation service |
| `paths.py` | Application-owned project, settings, recent, and log paths |

The UI talks to the shared `WorkspaceManager`, which owns the current
`MangaProject` and coordinates background services. The pipeline emits progress
and page-level results through Qt signals so the interface remains responsive.

## Project data

Each unified project contains a versioned `project.json` and an `artifacts`
folder. Image records retain:

- Original source and relative paths.
- Processing and review status.
- Detected source language.
- OCR, translation, rendered-image, and preview artifact paths.
- Per-block editor overrides.
- Manual text regions and suppressed automatic blocks.

The application also recognizes older Phase 2/Phase 4 result structures and
imports them into the unified project format.

## Runtime and model behavior

Dependencies are installed from `requirements.txt`. OCR uses PaddleOCR and
PaddlePaddle. Translation uses PyTorch, Transformers, SentencePiece, and local
Helsinki-NLP Marian checkpoints. Models are loaded lazily, so first use requires
internet access and is slower than later runs.

The current translation pairs are:

- Japanese to English: `Helsinki-NLP/opus-mt-ja-en`
- Chinese to English: `Helsinki-NLP/opus-mt-zh-en`

Unsupported detected source languages fail explicitly rather than being sent to
an external service. No cloud translation provider is part of the current app.

## Repository layout

```text
assets/              Application icons and logos
hydra_manga_tl/      Desktop app, pipeline, data model, and legacy CLI modules
samples/             Sample manga/comic images for local diagnostics
tests/               Unit and UI-focused regression tests
main.py              Preferred desktop entry point
requirements.txt     Python runtime and development dependencies
README.md            Installation and user guide
catelog.md           Concise product catalog entry
project.md           This technical and project-status guide
```

Generated `outputs`, model caches, virtual environments, and Python bytecode are
excluded through `.gitignore`.

## Development workflow

Create the local environment as described in `README.md`, then run:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Useful focused checks include:

```powershell
.\.venv\Scripts\python -m unittest tests.test_pipeline tests.test_workspace -v
.\.venv\Scripts\python -m unittest tests.test_ui tests.test_renderer -v
```

When changing the UI or pipeline, preserve the non-destructive storage boundary,
keep long-running model work off the UI thread, and add regression coverage for
project migration or artifact-contract changes.

## Known limitations and roadmap

Current limitations:

- Windows is the only documented and tested desktop target.
- Output is English only.
- Automatic translation and reconstruction still require human review.
- Decorative lettering, sound effects, unusual fonts, and complex artwork can
  need manual correction.
- Quality choices are visible and persisted but are not yet connected to
  distinct pipeline policies.
- Model packages and checkpoints require substantial disk space.
- Automatic updating is not implemented yet.

The next product-level milestones should prioritize finalizing the Windows setup
EXE and release packaging, meaningful quality profiles, broader language support,
and a documented release/versioning process.
