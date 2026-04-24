---
name: parse-pdf
description: Use when the user wants to extract or ask questions about the contents of a PDF — especially technical PDFs with tables and figures (datasheets, hardware manuals, specs, papers). Runs the `parse-pdf` CLI to produce an agent-friendly folder, then routes queries through that folder's README/index.json instead of reading the PDF directly. Triggers on requests like "what does table 17 say", "find signal X in this datasheet", "summarize this paper", or any question about a `.pdf` file.
---

# parse-pdf skill

This skill turns a PDF into a folder of small, structured files (per-page markdown, per-table markdown, signals.csv, figure descriptions, JSON indices) so you can answer questions about the PDF without ever reading the PDF directly.

## When to invoke

The user mentions a `.pdf` file and wants to know something about it: a value, a table, a figure, a signal name, a section. Before you do anything else with that PDF, run this skill.

## Step 1 — Parse the PDF (only if not already parsed)

Check whether `out/<pdf_stem>/` exists in the current working directory. If it does, skip to Step 2.

If not, run:

```bash
parse-pdf <path/to/file.pdf> --all
```

Optional flags worth adding when available:

- `--vision-tables` — re-extract every table from its rendered PNG using a local vision LLM (Ollama + `qwen2.5vl:7b`). Much better empty-cell handling for spec tables (Min/Typ/Max), and correctly handles vertically merged cells. Adds ~10-30s per table.
- `--describe-figures` — generate vision-LLM descriptions of every figure so you can "read" them without loading the PNG.

If `parse-pdf` is not on PATH, the skill folder ships a bundled copy of the
script — invoke it directly:

```bash
python "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/skills/parse-pdf}/parse_pdf.py" <path/to/file.pdf> --all
```

Or install once and forget:

```bash
pip install parse-pdf-for-llm           # core
pip install parse-pdf-for-llm[vision]   # adds Ollama for --vision-tables
```

The script needs `pymupdf` and `pymupdf4llm` (always) and `ollama` (only for the
vision flags). If those aren't installed, install them first:

```bash
pip install pymupdf pymupdf4llm           # core deps
pip install ollama                        # only if using --vision-tables / --describe-figures
```

## Step 2 — Read the output's README first

Always start with:

```
out/<pdf_stem>/README.md
```

That file documents the file map for **this specific PDF** (number of pages, signals, figures, tables) and tells you which file to open for which kind of query. Trust it — it's machine-generated alongside the data.

## Step 3 — Route the query

Use the smallest file that can answer the question. Token cost matters.

| Query type | File to read |
|---|---|
| Signal / pin / pad lookup ("what is GPIO_X used for?") | `signals.csv` (grep) |
| Specific table by number ("Table 17") | `table_17_*.md` (direct read — self-contained) |
| Specific figure by number ("Figure 3") | `figure_03_*.md` (vision-LLM description — **NOT the PNG** unless text in the figure is mission-critical and the description says it was unreadable) |
| Full content of one page | `page_NNN.md` (cleaned) |
| "What's on page N?" | `index.json` for headings/tables/figures, then `page_NNN.md` if needed |
| Translating "page N (printed)" ↔ PDF page | `pages.json` |
| Cross-references ("what figure goes with table 17?") | `tables.json` `related_figures` field, or `figures.json` `related_tables` |

### Hard rules

- **Never** read `out/<pdf_stem>/document.md`. It's the full concatenation, intended for embedding pipelines. Reading it interactively wastes tens of thousands of tokens.
- **Never** read `out/<pdf_stem>/*.png` unless you've already opened the matching `.md` and concluded it's insufficient.
- **Never** re-read the original PDF with the `Read` tool when a parsed output exists. The whole point of the parsed folder is that it's cheaper and better-structured.
- If a `table_NN_*.md` file starts with `_Extracted by vision LLM_`, the empty cells are reliable. If it doesn't, treat empty cells with mild suspicion (the text-based parse can wrongly fill them) and consult the matching `table_NN_*.png` if precision matters.

## Step 4 — Answer with citations

When you give the user an answer derived from the parsed PDF, cite the source file (e.g. "Table 17 (`out/foo/table_17_*.md`), row R16"). This makes it easy for the user to verify.
