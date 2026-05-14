# HTML to Reader PDF

Local web app for turning browser-saved `.html` files into cleaner reader-mode PDFs.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B app.py
```

Then open:

```text
http://127.0.0.1:5050
```

## Workflow

1. Enter a path to a saved `.html`/`.htm` file, or pick one from the suggestions under `html/`.
2. Review the automatically cleaned reader preview.
3. Toggle content blocks on or off in the left panel, or click blocks in the preview.
4. Choose the output directory and PDF filename.
5. Click `Generate PDF`.

The default output directory is `pdf/`.

## Notes

- JavaScript, forms, iframes, active embeds, navigation-like regions, ads, cookie prompts, and social/share widgets are removed before preview.
- Images are included when they are part of selected content and can be removed by deselecting their block.
- Uploading a single HTML file works, but browser-saved asset folders are only preserved when the app is given the original file path on disk.
- ReportLab is the default PDF engine because it does not need extra native graphics libraries on macOS.

## Architecture

The app is a small synchronous Flask application. There is no database; loaded
documents live in the process-local `SESSIONS` dictionary until the server is
restarted.

Important files:

- `app.py`: Flask routes, HTML cleanup, content extraction, image resolution,
  and PDF generation.
- `templates/index.html`: single-page UI shell.
- `static/app.js`: browser-side load, preview, block selection, and generate
  actions.
- `static/styles.css`: UI and reader-preview styling.
- `html/`: optional local archive of saved browser pages.
- `pdf/`: default PDF output directory.
- `.uploads/`: temporary storage for single uploaded HTML files.
- `.image_cache/`: per-session cache for remote images discovered in
  `srcset`, `data-srcset`, or similar attributes.

### Request Flow

1. `GET /` renders the UI and provides known `.html`/`.htm` files from `html/`.
2. `POST /api/load` accepts either a filesystem path or an uploaded file.
3. `extract_blocks()` reads the file, cleans it with BeautifulSoup/lxml,
   chooses a likely article root, and splits meaningful content into
   `ReaderBlock` objects.
4. The UI renders those blocks as a reader preview. Every block can be toggled
   on or off before PDF generation.
5. `POST /api/generate` receives the selected block IDs and writes the PDF to
   the chosen output directory.

### Extraction Heuristics

The cleanup phase removes active content first: scripts, forms, iframes,
embeds, styles, and similar tags. It then removes likely clutter based on tag
names and common class/id/role labels such as `nav`, `ad`, `cookie`,
`sidebar`, `share`, `related`, and `subscribe`.

The article root is chosen by scoring candidate containers. Paragraphs and
headings increase the score; link-heavy regions are penalized because they are
often menus or related-link lists.

### Image Handling

Images can come from several places in browser-saved pages:

- local `src` paths into the saved `*_files` folder;
- lazy-loading fields like `data-src`;
- responsive fields like `srcset` and `data-srcset`;
- still-online remote versions of the same image.

The extractor collects all of these candidates, tries the largest remote
`srcset` image first, caches successful downloads under `.image_cache/`, and
falls back to locally saved assets. Local assets are served through
`/asset/<session>` so the UI does not need direct filesystem paths.

### PDF Rendering

ReportLab is the default PDF engine. It maps the selected reader blocks to
ReportLab flowables: headings, paragraphs, lists, tables, preformatted blocks,
and images. This keeps output stable and reader-like instead of trying to
reproduce arbitrary website CSS.

WeasyPrint support is still present behind `HTML2PDF_USE_WEASYPRINT=1`, but it
requires native libraries on macOS. When enabled and importable, `/api/generate`
uses the HTML-based reader document path instead of the ReportLab flowable
renderer.

### Development Notes

- The app is designed for trusted local use, not production hosting.
- `SESSIONS` is in-memory and not concurrency-hardened.
- Remote image downloads are opportunistic and intentionally fail soft.
- The extraction rules are heuristic; most future improvements should happen
  in `is_noise()`, `candidate_roots()`, `text_score()`, and image candidate
  selection.
