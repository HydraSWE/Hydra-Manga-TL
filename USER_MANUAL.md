# Hydra Manga TL User Manual

This manual describes the current Hydra Manga TL v1 desktop workflow as it
exists in this repository. It is a user guide, not a developer architecture
document. For source setup and commands, see `README.md`. For implementation
and data contracts, see `project.md`.

Hydra Manga TL is a Windows desktop application for importing manga pages,
running OCR, translating detected text, reconstructing translated pages,
reviewing uncertain results, making manual corrections, and exporting finished
pages. Source images are kept untouched.

## 1. Start Screen

When the app opens, the Project Home screen shows:

- `Import Manga` - choose a folder of manga images.
- `Add Images` - choose individual image files.
- `Open Project` - open an existing `project.json`.
- Drag and drop - drop images or a folder directly onto the import area.
- `Recent Projects` - reopen a previously used project.
- `View All` - open the recent-project library, search by project name, and
  reopen older projects that are not shown in the compact landing list.
- `Clear History` - remove recent-project shortcuts and delete Hydra-owned
  project data folders that the app can verify. Exported files outside Hydra
  project data are not deleted.

Supported image inputs are JPG, JPEG, PNG, WEBP, TIFF, and BMP.

During import, Hydra scans supported images, reads metadata, prepares the
project, and loads previews. The loading screen shows staged progress while the
project is being prepared. The project is stored separately from the original
source images.

## 2. Workspace Layout

After opening a project, the workspace contains:

- A grouped top control bar for source language, target language, quality,
  text style, translation, settings, saving, and export.
- A page toolbar for navigation, review filters, reading order, manual regions,
  title reconstruction, and zoom.
- Two canvases with headers and status labels: `Original` and `Translated`.
- A filmstrip for page selection and page ordering.
- A right-side inspector with `Text Blocks` and `Image Info`.
- A translation job panel with progress, current page, and stage state.

The pinned `Hydra` tile in the filmstrip opens the Hydra identity preview. It
is UI-only and is not part of project pages, translation queues, page counts, or
exports.

## 3. Languages, Quality, and Style

The source language selector supports Japanese, Chinese, and Auto-detect.

The target language selector supports:

- English
- Spanish
- French
- German
- Italian
- Portuguese

Provider and model support can still limit what actually works for a selected
language pair.

Quality modes are:

- `Fast` - no OCR retry budget. Fast mode still runs OCR/Reading before
  parallel translation work begins.
- `Balanced` - one OCR retry budget.
- `Maximum` - three OCR retry budgets.

Text style options are:

- `Manga`
- `Comic`
- `Novel`

The style affects project text rendering defaults. Changing it is recorded in
the editor history and can be undone.

## 4. Translating Pages

Use the filmstrip to choose pages.

- `Translate All Pending` translates pages that are still pending.
- `Translate Selected` translates the selected filmstrip batch, or the current
  page when only one page is selected.
  - Note: batch translation avoids title reconstruction/inpaint for title-like
    regions, so title-style art text is rendered without the title cleanup pass.
- `Cancel` requests cancellation for active or queued work.

Hydra sends batch, selected-page, manual-region, and review work through shared
runtime services. Rendering is serialized through one render queue. A queued
render can be cancelled; a render already running is allowed to finish safely.

Completed pages can be reviewed while later queued work continues.

## 5. Filmstrip

The filmstrip shows project pages as thumbnails.

Common actions:

- Click a page to open it.
- Ctrl-click or Shift-click to select several pages.
- `Select pending` selects every page that can still be translated.
- `Clear` clears the selected batch without changing the open page.
- Drag pages in the filmstrip to reorder project pages when translation is not
  running.
- Ctrl+Left or Ctrl+Right grows the keyboard batch selection.
- Ctrl+Space toggles the current page selection.

The filmstrip can be collapsed or expanded. Settings can keep the current
per-project behavior or always open it collapsed.

## 6. Review Filters and Issue Navigation

The toolbar filter can show:

- `All blocks`
- `Untranslated`
- `Residual source`
- `Overflow`
- `Missing glyph`
- `Low OCR`
- `Provider fallback`

Use:

- `Next OCR Issue` to move to the next suspicious source-recognition block.
- `Next Review Issue` to move to the next translation or render review item.

Hydra intentionally routes uncertain OCR, translation, text fit, missing glyph,
and provider-fallback cases to review instead of silently treating them as
finished.

## 7. Reading Order

Reading order controls affect the order of text blocks on the current page.

- `Show Order` displays numbered order labels on the original and translated
  canvases.
- `Order Up` moves the selected block earlier in the reading order.
- `Order Down` moves the selected block later in the reading order.
- `Reset Order` clears the custom reading order and returns to the automatic
  order.

Use these controls when the detected sequence of bubbles is wrong. Reading
order edits are recorded in undo/redo history.

## 8. Text Block Editing

Select a block from the canvas or from the `Text Blocks` list in the inspector.

The inspector includes:

- `Original` - editable OCR/source text for the selected block.
- Speaker button - play or stop the original text when speech is available.
- `Translation` - editable translated text.
- `Confidence` - OCR confidence and review quality information.
- `Replace source text` - controls whether rendered translation replaces the
  source text.
- `Region type` - Dialogue, SFX, Sign, Credit, and Title where applicable.
- `Alignment` - Left, Center, or Right.
- `Font` - Arial, Arial Bold, Comic Sans MS, or Segoe UI.
- `Size` - Auto or a manual size from 1 to 120.
- `Preview` - text-fit preview information.
- `X` and `Y` - translation placement offsets.
- `Color` - rendered text color.

Use `Apply & Rerender` to save the selected block settings and rebuild the
translated page. Layout changes made by dragging/resizing the translated text
frame are staged until `Apply & Rerender` is used.

Use `Reset` to return the selected block to automatic text settings.

## 9. Manual Regions

Use `Region Tool` when automatic OCR missed a text area.

Modes:

- `Rectangle` - draw one rectangular region on the original page.
- `Polygon` - draw a polygon boundary and confirm it.

The default shortcut for cycling Region Tool modes is Ctrl+D. It can be changed
in Settings.

After a region is drawn, Hydra OCRs only the selected crop, translates it
through the configured manual engine path, adds a manual text block, suppresses
overlapping automatic blocks, and renders the translated page. Manual text is
kept inside the selected manual rectangle.

If manual rendering fails or is cancelled before rendering begins, Hydra rolls
back the newly added manual region.

For an existing manual block:

- `Redraw Shape` lets you draw a new boundary for that manual block.
- `Delete Manual` deletes the selected manual block and restores covered
  automatic blocks when available.

## 10. Title Reconstruction

Use `Title Reconstruction` for title-like lettering or artwork text that needs
special reconstruction.

Modes:

- `Rectangle`
- `Polygon`

The default shortcut for cycling Title Reconstruction modes is Ctrl+F. It can
be changed in Settings.

Title reconstruction creates a title-region request using the selected geometry.
The result appears as a title block when the request completes.

## 11. Removing and Restoring Blocks

For automatic blocks:

- `Remove Auto` removes the selected automatic block from the translated page.
- `Restore Auto` restores automatic blocks previously removed from that page.

For manual blocks:

- `Delete Manual` deletes the selected manual block.

Remove/delete actions ask for confirmation before changing the page.

## 12. Undo and Redo

Undo and redo are available through the standard shortcuts:

- Ctrl+Z - undo
- Ctrl+Y or the platform redo shortcut - redo

When focus is inside a text field, undo/redo applies to that field's text.
Otherwise Hydra uses editor history.

Hydra editor history covers:

- Text/style edits applied to blocks.
- Manual-region creation and deletion.
- Manual polygon redraw.
- Automatic-region suppression and restoration.
- Translated text layout movement and resize.
- Reading order changes.
- Project style changes.
- Approval state changes.

The history is limited to 200 changes and is cleared when switching projects or
closing the current project. Undo/redo restores editor state without creating a
second Translation Memory or learning event.

## 13. Approval and AI Center

The inspector includes:

- `Approve Bubble`
- `Approve Page OCR`
- `Approve Page Review`

These actions are for the optional HydraMangaAi bridge when that private
package is present. Approval is explicit: drafts do not become training data
automatically.

`AI Center` opens dataset and training-readiness information for the optional
bridge. If the private package is not available, normal OCR, translation,
rendering, editing, review, and export still work through the standard Hydra
pipeline.

## 14. Glossary

Use `Glossary` to define protected project terms.

The glossary format is one entry per line:

```text
source = English
```

Hydra reuses these spellings as project-level translation guidance.

## 15. Settings

Open `Settings` from the workspace top bar.

Translation settings:

- `Manual literal pass` - MarianMT (Local) or Google Cloud Translation.
- `Manual engine` - Local manga cleanup, Gemini, Groq, or DeepSeek.
- `Batch engine` - Groq, Google Translate, Gemini, Marian fallback, or Local
  Qwen.
- `Fallback` - optional fallback engine.
- `Fast workers` - Auto or 1 through 6 workers for Fast mode translation.

Automatic region settings:

- Translate titles automatically.
- Translate SFX automatically.
- Translate signs automatically.
- Translate credits automatically.

Local Qwen settings:

- Choose a known model package.
- Browse to a `.gguf` model.
- Download a model.
- Test the local engine.

GPU and native runtime:

- View NVIDIA/GPU/native backend status.
- Run `Test GPU runtime`.

Cloud models and keys:

- Configure Gemini, Groq, DeepSeek, and Google provider keys and model names.
- Keys are stored through the credential system instead of inside project
  files.
- Cloud services are optional and may have provider quotas or charges.

Workspace settings:

- Data folder for Hydra projects, logs, caches, and Translation Memory.
- Region shortcut.
- Title shortcut.
- Filmstrip opening behavior.
- Debug OCR crops and diagnostic overlays.
- Create diagnostics bundle.

Changing the data folder affects new projects, logs, caches, and memory. It
does not automatically move existing project folders.

## 16. Translation Memory and Phrase Memory

Translation Memory (TM):

- Global exact full-segment memory shared across projects.
- Can be enabled or disabled.
- Can automatically learn validated translations.
- Can store user translation edits as verified.
- Can prefer verified entries.
- Can import, export, or clear memory.
- Matching is exact only.

Phrase Memory (PM v1):

- Auto-learned sub-phrase constraints for terminology consistency.
- Can be enabled or disabled.
- Can automatically learn phrases.
- Can prefer verified phrases.
- Includes a manager for editing, verifying, deleting, importing, exporting,
  and clearing entries.

## 17. Export

Use `Export` from the workspace top bar.

Output types:

- Image folder
- ZIP archive
- CBZ comic archive
- PDF document

Image formats:

- PNG
- JPEG
- WebP

PDF export does not use the image-format selector.

Choose an export destination outside the source images when possible. Exported
files are separate from project data and do not modify original pages.

## 18. Project Data and Logs

By default Hydra uses:

```text
%LOCALAPPDATA%\Hydra Manga TL
```

Default projects folder:

```text
%LOCALAPPDATA%\Hydra Manga TL\projects
```

Default application log:

```text
%LOCALAPPDATA%\Hydra Manga TL\logs\app.log
```

The data root can be changed in Settings. Project artifacts stay separate from
source images. Target-specific translation, render, edit, timing, and export
state are isolated per target language.

## 19. Project Compatibility

Hydra projects are versioned.

When opening an older project that requires migration, Hydra prompts before
upgrading, creates a ZIP backup, applies the migration, and restores the
original project if migration fails.

When opening a future-schema project that requires a newer Hydra version, the
app rejects it instead of trying to load it unsafely.

## 20. Troubleshooting

If the first request is slow:

- Initial OCR, local translation, or local model warmup may take time.
- Check the job panel and the application log.

If a bubble was missed:

- Use `Region Tool`, draw the missing text area, and wait for the manual
  request to finish.

If source OCR is wrong:

- Select the block.
- Edit `Original`.
- Review with `Next OCR Issue` when needed.
- Use approval only if you want the optional learning bridge to record the
  correction.

If translated text does not fit:

- Select the block.
- Try Auto size, a smaller size, alignment changes, or layout movement on the
  translated canvas.
- Use `Apply & Rerender`.

If provider output is unavailable:

- Check Settings for provider keys, selected model names, fallback, and local
  Qwen model path.
- Run the relevant local engine or GPU test when using local Qwen.

If recent-project cleanup is used:

- Hydra deletes only verified Hydra-owned project data folders.
- Exported files outside Hydra project data are not deleted.

If a project opens with an upgrade prompt:

- Continue only if you want Hydra to migrate the project for the current
  version.
- Hydra creates a backup before migration.
