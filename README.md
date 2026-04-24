# parse-pdf-for-llm

Turn a PDF into a folder of small, structured files an LLM can route through cheaply.

Built for technical PDFs that mix text, tables, and figures — datasheets, hardware
manuals, specs — where the usual "read the whole PDF into context" approach burns
tokens and still misses values inside merged table cells.

## What it produces

For each input PDF, you get an output folder like:

```
out/<pdf_stem>/
├── README.md                       # ← agent reads this first
├── index.json                      # per-page headings/tables/figures
├── pages.json                      # pdf_page ↔ printed_page map
├── signals.csv                     # one row per signal (pinout tables)
├── tables.json                     # 1 entry per table, with cross-links to figures
├── figures.json                    # 1 entry per figure, with cross-links to tables
├── page_001.md / .txt / .raw.md    # cleaned / plain / debug per-page text
├── table_NN_<slug>.md              # self-contained markdown of one table
├── table_NN_<slug>.png             # cropped image of the same table
├── figure_NN_<slug>.png            # cropped figure image
├── figure_NN_<slug>.md             # vision-LLM description of the figure
└── document.md                     # full concat (for RAG / embeddings only)
```

The emitted `README.md` documents the file map for that specific PDF — the agent
reads it and routes its queries (table lookup → `table_NN_*.md`, signal lookup →
grep `signals.csv`, etc.) without ever opening `document.md`.

## Install

```bash
pip install parse-pdf-for-llm           # core
pip install parse-pdf-for-llm[vision]   # adds ollama for --vision-tables / --describe-figures
```

For the vision features you also need a local Ollama install with a vision model:

```bash
brew install ollama   # or see https://ollama.com
ollama pull qwen2.5vl:7b
```

Tested on Apple M4 Pro (works for 8B-class vision models). Should also work on
Linux and on x86 with a discrete GPU.

## Usage

### Parse a PDF

```bash
parse-pdf path/to/file.pdf --all                      # extract every page
parse-pdf path/to/file.pdf --pages 14                 # one page
parse-pdf path/to/file.pdf --pages 10-20 --pages 30   # ranges + singles
parse-pdf path/to/file.pdf --search "GPIO_DISP_B2_08" # find a term, no extraction
```

### Add vision-LLM features

```bash
# Re-extract every table from its rendered PNG (much better empty-cell handling
# for spec tables, correctly handles vertically merged cells).
parse-pdf path/to/file.pdf --all --vision-tables

# Generate vision-LLM descriptions of every figure (so an agent can "read" them
# without opening the PNG).
parse-pdf path/to/file.pdf --all --describe-figures
```

### Iterate on an existing output without re-extracting

```bash
parse-pdf path/to/file.pdf --vision-tables-only        # re-run vision tables only
parse-pdf path/to/file.pdf --describe-only             # re-run figure descriptions only
parse-pdf path/to/file.pdf --describe-only --redescribe  # overwrite existing
```

## Use with Claude Code

This repo ships with a Skill at `.claude/skills/parse-pdf/SKILL.md`. Claude Code
will auto-load it when the user mentions a PDF.

To install globally so it's available in any project:

```bash
mkdir -p ~/.claude/skills
cp -r .claude/skills/parse-pdf ~/.claude/skills/
```

Or as a project-local skill, just have `.claude/skills/parse-pdf/` in the repo.

## Use with Codex

Codex doesn't have a Skills equivalent, but `AGENTS.md` works. Add this to your
project's `AGENTS.md` (or `~/.codex/AGENTS.md` for global):

```
When asked about a `.pdf` file in this repo, first run
`parse-pdf <file.pdf> --all` (and `--vision-tables` if Ollama with
qwen2.5vl:7b is installed). Then read `out/<pdf_stem>/README.md` and
follow its routing instructions instead of reading the PDF directly.
Do not read `out/<stem>/document.md` interactively.
```

## Use with any other agent

The script's emitted `out/<stem>/README.md` *is* the integration. Any agent that
reads that file will know how to navigate the rest. You only need to nudge the
agent to run `parse-pdf` first.

## How merged-cell handling works

PDFs visually merge cells vertically (e.g. a "Group" column written once and
applying to the rows below). Markdown can't express that. Two passes handle it:

1. **Text pass** (default): a sparsity-aware forward-fill copies values down
   *only* in columns that look merged (very few non-empty values relative to
   total rows). Spec columns (Min/Typ/Max) keep empties empty.
2. **Vision pass** (`--vision-tables`): each table is rendered as a PNG and
   re-extracted by a vision LLM. The prompt distinguishes truly empty cells
   from merged cells (which get repeated on every spanned row). Output is
   shape-validated against the text pass before replacing it.

## License

MIT — see [LICENSE](LICENSE).
