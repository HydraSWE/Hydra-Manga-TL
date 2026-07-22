# Hydra Manga TL — Catalog

## Translate manga without giving up control

Hydra Manga TL is a local-first Windows desktop workspace that turns Japanese or
Chinese manga pages into editable English PNGs. It combines OCR, translation,
artwork reconstruction, typesetting, review, and export in one application while
keeping the original images untouched.

## At a glance

| | |
| --- | --- |
| Version | 0.6.0, active development |
| Platform | Windows 10/11, 64-bit |
| Price | No purchase flow is included in this repository |
| Input | JPG, JPEG, PNG, WEBP, TIFF, BMP |
| Source text | Japanese, Chinese, and preserved Latin-script text |
| Output language | English |
| Export | PNG |
| Processing | Local after required models are downloaded |
| Installation | Windows setup EXE distribution; source installation also supported |

## Highlights

- Import individual pages or an entire nested chapter folder.
- Automatically compare OCR candidates and detect the page language.
- Translate Japanese and Chinese dialogue locally to English.
- Rebuild text areas and fit translated lettering in the original locations.
- Review original and translated pages side by side as the queue runs.
- Correct wording, typography, color, alignment, and placement per text block.
- Draw a manual text box for missed or incorrectly grouped dialogue.
- Remove unwanted automatic blocks and restore them later.
- Reopen autosaved projects and export without touching source files.

## Before installing

The intended end-user distribution is a Windows setup EXE that installs the
complete desktop application. The source workflow remains available to developers
and advanced users. Initial model downloads require an internet connection and
several gigabytes of free disk space. A GPU is optional; CPU processing is
supported but slower.

Hydra does not currently include an automatic updater, cloud translation account,
or multi-language output selector.
Automatic results should be reviewed before publication.

## After installing

Launch Hydra, drop in manga images, and choose **Translate All Pending**. Review
each page in the workspace, use **Add Text Box** for missed areas, apply any text
or style corrections, and export the completed PNG files to a separate folder.

Projects and logs live under `%LOCALAPPDATA%\Hydra Manga TL`. The first job takes
longer while OCR and translation models download; later jobs reuse the local cache.

For complete installation and troubleshooting instructions, read [README.md](README.md).
For implementation details and current limitations, read [project.md](project.md).
