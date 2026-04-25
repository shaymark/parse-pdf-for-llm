# tools/

Standalone helpers that orbit around the main `parse-pdf` workflow but
aren't part of the core CLI.

## `table-image-to-md.html`

A single-file HTML page that converts one table screenshot into Markdown
using a local Ollama vision model — same prompt as `parse_pdf.py`'s
`--vision-tables`.

**Use it when:**
- You snipped a single table from somewhere and just want clean Markdown.
- You want to iterate on the prompt without rerunning the whole pipeline.

**How:**

1. Make sure Ollama is running with **CORS enabled**, otherwise the browser
   refuses to talk to it:
   ```bash
   OLLAMA_ORIGINS='*' ollama serve
   ```
   On macOS, if Ollama runs as a launchd service, set it once:
   ```bash
   launchctl setenv OLLAMA_ORIGINS '*'
   # then quit & reopen the Ollama menubar app
   ```
2. Pull a vision model (one-time):
   ```bash
   ollama pull qwen2.5vl:7b
   ```
3. Open `tools/table-image-to-md.html` in a browser (double-click works).
4. Drop a PNG/JPG, paste from the clipboard (⌘V), or click the drop zone
   to browse. Hit **Run** (or ⌘/Ctrl+Enter).

**Features:**
- Live model dropdown — pulls from `http://localhost:11434/api/tags` and
  surfaces vision-capable models (anything with `vl` or `vision` in the
  name) first.
- Editable prompt with a "Reset to default" button. Edits persist in
  `localStorage`.
- Optional caption field — appended to the prompt for additional context
  (mirrors the `caption=` arg in `vision_extract_table()`).
- Streaming output — Markdown appears as the model emits it.
- **Copy** button (clipboard) and **Save .md** button (downloads as
  `<image-name>.md`).
