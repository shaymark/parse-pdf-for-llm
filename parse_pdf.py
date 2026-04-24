"""
Parse a PDF that contains text, tables, and component-schematic images.

Strategy:
  1. pymupdf4llm  -> markdown (preserves tables as | col | col |)
  2. PyMuPDF      -> raw text blocks + image extraction per page
  3. Output is written under ./out/<pdf_stem>/
       page_<N>.md      Markdown of page (tables preserved)
       page_<N>.txt     Plain text fallback
       page_<N>_img_<i>.<ext>   Embedded images (schematics, diagrams)

Usage:
  python parse_pdf.py <pdf_path> [--pages 14] [--pages 10-20] [--all]
  python parse_pdf.py <pdf_path> --search "GPIO_DISP_B2_08"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf  # PyMuPDF
import pymupdf4llm


# Strikethrough in technical PDFs is virtually always a layout-extraction
# artefact (table rules / tiny glyphs misread as crossed-out text). Drop it all.
_STRIKE_NOISE = re.compile(r"~~[^~]*~~")
# Repeated <br> with only noise between them collapse to a single one,
# then trailing <br> at cell end are removed.
_BR_RUNS = re.compile(r"(?:<br>\s*){2,}")
_TRAIL_BR_IN_CELL = re.compile(r"(?:\s*<br>\s*)+(?=\||$)")


def clean_markdown(md: str) -> str:
    """Strip pymupdf4llm noise: strike-through artefacts and stray <br> runs."""
    md = _STRIKE_NOISE.sub("", md)
    md = _BR_RUNS.sub("<br>", md)
    md = _TRAIL_BR_IN_CELL.sub("", md)
    # Collapse runs of spaces inside cells, but keep newlines between rows.
    md = re.sub(r"[ \t]{2,}", " ", md)
    md = forward_fill_tables(md)
    return md


def _split_row(line: str) -> list[str] | None:
    """Return cells of a markdown table row, or None if not a table row."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """True for the |---|---|... row beneath a header."""
    return bool(cells) and all(c and set(c) <= set("-:") for c in cells)


def forward_fill_tables(md: str) -> str:
    """Fill empty cells from the row above — recovers PDF row-spans.

    PDFs visually merge cells vertically (e.g. a 'Group' column written once
    and applying to the rows beneath). Markdown can't express that, so
    pymupdf4llm leaves the cells empty. This pass copies the last non-empty
    value down so each row stands alone.
    """
    lines = md.split("\n")
    out: list[str] = []
    last: list[str] = []
    state = "outside"  # outside | header_seen | in_body

    for line in lines:
        cells = _split_row(line)
        if cells is None:
            out.append(line)
            state, last = "outside", []
            continue

        if state == "outside":
            out.append(line)
            state, last = "header_seen", []
            continue

        if state == "header_seen":
            if _is_separator_row(cells):
                out.append(line)
                state = "in_body"
            else:
                # Two header rows in a row, or no separator: bail out.
                out.append(line)
                state, last = "outside", []
            continue

        # in_body
        if not last:
            last = list(cells)
        else:
            for i, c in enumerate(cells):
                if c == "" and i < len(last) and last[i]:
                    cells[i] = last[i]
            last = list(cells)
        out.append("|" + "|".join(cells) + "|")

    return "\n".join(out)


def parse_page_arg(spec: str) -> list[int]:
    """Accepts '14', '10-20', '1,3,5', or mixed -> sorted list of 1-based ints."""
    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(chunk))
    return sorted(pages)


_PRINTED_PAGE_RE = re.compile(r"^\s*Page\s+(\d+)\s*$", re.IGNORECASE)
_CAPTION_RE = re.compile(r"^(Figure|Table)\s+(\d+)\s*[:\.]\s*(.+?)\s*$", re.IGNORECASE)


def _detect_printed_page(txt: str) -> int | None:
    """Find the printed page number (in the running header/footer)."""
    lines = txt.splitlines()
    for ln in lines[:8] + lines[-8:]:
        m = _PRINTED_PAGE_RE.match(ln)
        if m:
            return int(m.group(1))
    return None


def _detect_captions(page: pymupdf.Page) -> list[dict]:
    """Find Figure/Table captions on the page with their bounding rects.

    Captions can be split across lines inside a single text block (the label
    'Figure 10:' on one line and the title on the next), so collapse the
    block's whitespace before matching.
    """
    captions: list[dict] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
        normalised = re.sub(r"\s+", " ", text).strip()
        m = _CAPTION_RE.match(normalised)
        if m:
            captions.append({
                "type": m.group(1).lower(),
                "number": int(m.group(2)),
                "title": m.group(3).strip(),
                "caption": f"{m.group(1).title()} {m.group(2)}: {m.group(3).strip()}",
                "rect": pymupdf.Rect(x0, y0, x1, y1),
            })
    return captions


def _nearest_caption_below(rect: pymupdf.Rect, captions: list[dict], kind: str,
                           max_distance: float = 80, max_overlap: float = 25) -> dict | None:
    """Return the closest caption of `kind` near the bottom edge of `rect`.

    The caption may slightly overlap the image's reported bottom (PDF image
    bounding boxes often extend past the visible content), so allow up to
    `max_overlap` points of negative dy.
    """
    best = None
    best_dy = float("inf")
    for c in captions:
        if c["type"] != kind:
            continue
        cr = c["rect"]
        dy = cr.y0 - rect.y1
        if dy < -max_overlap or dy > max_distance:
            continue
        if cr.x0 >= rect.x1 or cr.x1 <= rect.x0:
            continue
        if dy < best_dy:
            best, best_dy = c, dy
    return best


def _slugify(s: str, max_len: int = 50) -> str:
    s = re.sub(r"[^\w\-]+", "_", s).strip("_").lower()
    return s[:max_len].rstrip("_") or "untitled"


def extract_page(doc: pymupdf.Document, page_num_1based: int, out_dir: Path,
                 used_fig_names: set[str]) -> dict:
    """Extract markdown, plain text, captions, and cropped figures for a single page."""
    out_dir.mkdir(parents=True, exist_ok=True)
    idx = page_num_1based - 1
    page = doc[idx]

    # 1. Markdown.
    try:
        md = pymupdf4llm.to_markdown(doc, pages=[idx], show_progress=False)
    except Exception as e:
        md = f"<!-- pymupdf4llm failed: {e} -->\n"
    md_clean = clean_markdown(md)
    md_path = out_dir / f"page_{page_num_1based:03d}.md"
    md_path.write_text(md_clean, encoding="utf-8")
    raw_path = out_dir / f"page_{page_num_1based:03d}.raw.md"
    raw_path.write_text(md, encoding="utf-8")

    # 2. Plain text.
    txt = page.get_text("text")
    txt_path = out_dir / f"page_{page_num_1based:03d}.txt"
    txt_path.write_text(txt, encoding="utf-8")

    # 3. Per-page metadata.
    printed_page = _detect_printed_page(txt)
    captions = _detect_captions(page)

    # 4. Cropped figures (caption-aware bottom edge + caption-derived filenames).
    figures: list[dict] = []
    margin_side = 6
    margin_top = 6
    fallback_bottom = 40
    fig_seq = 0
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for rect in rects:
            fig_seq += 1
            cap = _nearest_caption_below(rect, captions, "figure")
            if cap:
                bottom = cap["rect"].y1 + 4
                base = f"figure_{cap['number']:02d}_{_slugify(cap['title'])}"
            else:
                bottom = rect.y1 + fallback_bottom
                base = f"page_{page_num_1based:03d}_fig_{fig_seq:02d}"
            name = f"{base}.png"
            if name in used_fig_names:
                name = f"{base}_p{page_num_1based:03d}_{fig_seq}.png"
            used_fig_names.add(name)

            clip = pymupdf.Rect(rect.x0 - margin_side, rect.y0 - margin_top,
                                rect.x1 + margin_side, bottom) & page.rect
            try:
                pix = page.get_pixmap(clip=clip, dpi=200, alpha=False)
                pix.save(out_dir / name)
                pix = None
                figures.append({
                    "file": name,
                    "page": page_num_1based,
                    "printed_page": printed_page,
                    "figure_number": cap["number"] if cap else None,
                    "caption": cap["caption"] if cap else None,
                })
            except Exception as e:
                figures.append({"file": None, "error": str(e)})

    table_captions = [c for c in captions if c["type"] == "table"]

    return {
        "pdf_page": page_num_1based,
        "printed_page": printed_page,
        "md": md,
        "md_path": md_path,
        "txt_path": txt_path,
        "figures": figures,
        "table_captions": [{"number": c["number"], "title": c["title"], "caption": c["caption"]}
                           for c in table_captions],
    }


_PAGE_NUM = re.compile(r"\bPage\s+\d+\b", re.IGNORECASE)


def _normalize_for_recurrence(s: str) -> str:
    """Treat 'Page 14' and 'Page 15' as the same line for counting purposes."""
    return _PAGE_NUM.sub("Page #", s.strip())


def strip_recurring_header_footer(out_dir: Path, head_lines: int = 6, foot_lines: int = 6, threshold: float = 0.5) -> int:
    """Remove lines that recur on >= threshold of pages within the top/bottom N lines.

    Operates on cleaned page_NNN.md and page_NNN.txt — leaves .raw.md alone
    so the original output is preserved for debugging.
    """
    md_files = sorted(p for p in out_dir.glob("page_[0-9]*.md") if not p.name.endswith(".raw.md"))
    if len(md_files) < 3:
        return 0

    page_lines: dict[Path, list[str]] = {}
    head_counter: Counter[str] = Counter()
    foot_counter: Counter[str] = Counter()

    for f in md_files:
        lines = f.read_text(encoding="utf-8").splitlines()
        page_lines[f] = lines
        non_empty_head = [_normalize_for_recurrence(ln) for ln in lines[:head_lines] if ln.strip()]
        non_empty_foot = [_normalize_for_recurrence(ln) for ln in lines[-foot_lines:] if ln.strip()]
        head_counter.update(set(non_empty_head))
        foot_counter.update(set(non_empty_foot))

    cutoff = max(2, int(len(md_files) * threshold))
    recurring = {ln for ln, n in head_counter.items() if n >= cutoff}
    recurring |= {ln for ln, n in foot_counter.items() if n >= cutoff}
    if not recurring:
        return 0

    def strip_file(path: Path, lines: list[str]) -> None:
        kept = [ln for ln in lines if _normalize_for_recurrence(ln) not in recurring]
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    for f, lines in page_lines.items():
        strip_file(f, lines)
        txt = f.with_suffix(".txt")
        if txt.exists():
            strip_file(txt, txt.read_text(encoding="utf-8").splitlines())

    return len(recurring)


# --- Signal-table extraction --------------------------------------------------

# Map normalised header text -> canonical CSV column name.
# Normalisation: lowercase, remove <br>, spaces, slashes, dots.
_HEADER_ALIASES = {
    "modulepad": "pad",
    "pad": "pad",
    "signal": "signal",
    "signalname": "signal",
    "cpuball": "cpu_ball",
    "ball": "cpu_ball",
    "io": "io",
    "direction": "io",
    "group": "group",
    "descriptionusage": "usage",
    "usage": "usage",
    "description": "usage",
    "voltagelevel": "voltage",
    "voltage": "voltage",
}
_CANONICAL_COLS = ["pad", "signal", "cpu_ball", "io", "group", "usage", "voltage"]


def _norm_header(h: str) -> str:
    h = h.replace("<br>", "").replace("/", "").replace(".", "")
    return re.sub(r"\s+", "", h).lower()


def _iter_md_tables(md: str):
    """Yield (headers, rows) for each markdown table in md."""
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        cells = _split_row(lines[i])
        if cells and i + 1 < len(lines):
            sep = _split_row(lines[i + 1])
            if sep and _is_separator_row(sep):
                headers = cells
                rows = []
                j = i + 2
                while j < len(lines):
                    row = _split_row(lines[j])
                    if not row:
                        break
                    rows.append(row)
                    j += 1
                yield headers, rows
                i = j
                continue
        i += 1


def _is_signal_table(headers: list[str]) -> bool:
    norm = [_norm_header(h) for h in headers]
    has_signal = any("signal" in h for h in norm)
    has_pad_or_ball = any(("pad" in h) or ("ball" in h) for h in norm)
    return has_signal and has_pad_or_ball


def extract_signals_csv(out_dir: Path) -> tuple[Path, int]:
    """Walk cleaned page MDs, parse signal tables, write signals.csv."""
    md_files = sorted(p for p in out_dir.glob("page_[0-9]*.md") if not p.name.endswith(".raw.md"))
    out_rows: list[dict] = []

    for f in md_files:
        page_num = int(f.stem.split("_")[1])
        md = f.read_text(encoding="utf-8")
        for headers, rows in _iter_md_tables(md):
            if not _is_signal_table(headers):
                continue
            col_idx: dict[str, int] = {}
            for idx, h in enumerate(headers):
                key = _HEADER_ALIASES.get(_norm_header(h))
                if key and key not in col_idx:
                    col_idx[key] = idx
            for row in rows:
                rec = {c: "" for c in _CANONICAL_COLS}
                for col, idx in col_idx.items():
                    if idx < len(row):
                        val = row[idx].replace("<br>", " ").strip()
                        rec[col] = re.sub(r"\s+", " ", val)
                if not rec["signal"]:
                    continue
                rec["page"] = page_num
                out_rows.append(rec)

    csv_path = out_dir / "signals.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CANONICAL_COLS + ["page"])
        w.writeheader()
        w.writerows(out_rows)
    return csv_path, len(out_rows)


# --- Cross-page artefacts -----------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _extract_headings(md: str) -> list[dict]:
    headings = []
    for line in md.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            headings.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return headings


def _extract_table_summaries(md: str) -> list[dict]:
    """List tables in the markdown with their headers and row count."""
    tables = []
    for headers, rows in _iter_md_tables(md):
        tables.append({
            "headers": [h.replace("<br>", " ").strip() for h in headers],
            "n_rows": len(rows),
        })
    return tables


def write_pages_json(out_dir: Path, page_results: list[dict]) -> Path:
    mapping = {str(r["pdf_page"]): r["printed_page"] for r in page_results}
    path = out_dir / "pages.json"
    path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    return path


def write_figures_json(out_dir: Path, page_results: list[dict]) -> tuple[Path, int]:
    items = []
    for r in page_results:
        for f in r["figures"]:
            if f.get("file"):
                items.append(f)
    path = out_dir / "figures.json"
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return path, len(items)


def write_index_json(out_dir: Path, page_results: list[dict]) -> Path:
    entries = []
    for r in page_results:
        md = r["md_path"].read_text(encoding="utf-8") if r["md_path"].exists() else ""
        entries.append({
            "pdf_page": r["pdf_page"],
            "printed_page": r["printed_page"],
            "md_file": r["md_path"].name,
            "headings": _extract_headings(md),
            "tables": _extract_table_summaries(md),
            "table_captions": r["table_captions"],
            "figures": [
                {"file": f["file"], "figure_number": f.get("figure_number"), "caption": f.get("caption")}
                for f in r["figures"] if f.get("file")
            ],
        })
    path = out_dir / "index.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def write_document_md(out_dir: Path, page_results: list[dict]) -> Path:
    parts = ["<!-- Concatenated document. Each section is one PDF page. -->\n"]
    for r in page_results:
        md = r["md_path"].read_text(encoding="utf-8") if r["md_path"].exists() else ""
        printed = r["printed_page"] if r["printed_page"] is not None else "?"
        parts.append(f"\n<!-- pdf_page={r['pdf_page']} printed_page={printed} -->\n")
        parts.append(md.rstrip() + "\n")
    path = out_dir / "document.md"
    path.write_text("".join(parts), encoding="utf-8")
    return path


# --- Vision-LLM figure descriptions (local, via Ollama) ---------------------

_DESCRIBE_PROMPT = """\
You are looking at a figure from a hardware/embedded-systems datasheet.

Write a concise description (5–10 sentences) for an engineer who cannot see \
the image. Be factual. Cover, when visible:
- The kind of figure (block diagram, schematic, pinout, mechanical drawing, \
  photo, table, etc.).
- The main components/blocks (chip names, modules, connectors).
- Every signal/net/label name you can read (list them verbatim, even partial).
- How blocks are connected (which signals go where), if relevant.
- Power rails, voltages, ground, units, dimensions when shown.

Do not invent details. If text is unreadable, say so. Output plain text only — \
no Markdown headings, no preamble like "This figure shows".
"""


def describe_figure(image_path: Path, model: str = "qwen2.5vl:7b") -> str:
    """Return a description of the image generated by a local vision LLM via Ollama."""
    import ollama  # local import so the script still runs without it
    resp = ollama.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": _DESCRIBE_PROMPT,
            "images": [str(image_path)],
        }],
        options={"temperature": 0.2},
    )
    return resp["message"]["content"].strip()


def describe_all_figures(out_dir: Path, model: str = "qwen2.5vl:7b", overwrite: bool = False) -> int:
    """Generate <figure>.md alongside every figure PNG. Skips existing unless overwrite."""
    pngs = sorted([p for p in out_dir.glob("*.png")])
    n_done = 0
    for png in pngs:
        md_path = png.with_suffix(".md")
        if md_path.exists() and not overwrite:
            continue
        try:
            print(f"  describing {png.name} ...", flush=True)
            text = describe_figure(png, model=model)
            md_path.write_text(text + "\n", encoding="utf-8")
            n_done += 1
        except Exception as e:
            print(f"    failed: {e}")
    return n_done


def attach_figure_descriptions(out_dir: Path) -> int:
    """Read each figure_*.md and merge its text into figures.json under 'description'."""
    fig_json = out_dir / "figures.json"
    if not fig_json.exists():
        return 0
    items = json.loads(fig_json.read_text(encoding="utf-8"))
    n = 0
    for item in items:
        f = item.get("file")
        if not f:
            continue
        md = (out_dir / f).with_suffix(".md")
        if md.exists():
            item["description"] = md.read_text(encoding="utf-8").strip()
            n += 1
    fig_json.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return n


def search_pdf(doc: pymupdf.Document, term: str) -> list[tuple[int, str]]:
    """Return list of (page_1based, snippet) for pages containing term."""
    hits = []
    pat = re.compile(re.escape(term), re.IGNORECASE)
    for i in range(len(doc)):
        text = doc[i].get_text("text")
        if pat.search(text):
            # Take a few lines around first match for context.
            lines = text.splitlines()
            for j, line in enumerate(lines):
                if pat.search(line):
                    snippet = "\n".join(lines[max(0, j - 1): j + 4])
                    hits.append((i + 1, snippet))
                    break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--pages", action="append", default=[], help="Page spec: '14' or '10-20' or '1,3,5'. Repeatable.")
    ap.add_argument("--all", action="store_true", help="Extract every page.")
    ap.add_argument("--search", help="Print pages containing the given term (case-insensitive).")
    ap.add_argument("--out", type=Path, default=Path("out"), help="Output root (default: ./out)")
    ap.add_argument("--describe-figures", action="store_true",
                    help="After extraction, run a local vision LLM (Ollama) to describe each figure.")
    ap.add_argument("--describe-only", action="store_true",
                    help="Skip extraction; only run figure descriptions over existing PNGs in out dir.")
    ap.add_argument("--describe-model", default="qwen2.5vl:7b",
                    help="Ollama model name for figure descriptions (default: qwen2.5vl:7b).")
    ap.add_argument("--redescribe", action="store_true",
                    help="Regenerate figure descriptions even if a .md already exists.")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    doc = pymupdf.open(args.pdf)
    print(f"Opened {args.pdf.name} — {len(doc)} pages")

    if args.search:
        hits = search_pdf(doc, args.search)
        if not hits:
            print(f"No matches for {args.search!r}.")
        else:
            print(f"{len(hits)} page(s) match {args.search!r}:")
            for page_num, snippet in hits:
                print(f"\n--- page {page_num} ---")
                print(snippet)
        return 0

    out_dir_default = args.out / args.pdf.stem
    if args.describe_only:
        if not out_dir_default.exists():
            print(f"--describe-only needs an existing out dir: {out_dir_default}", file=sys.stderr)
            return 1
        print(f"Describing figures in {out_dir_default} with {args.describe_model}...")
        n = describe_all_figures(out_dir_default, model=args.describe_model, overwrite=args.redescribe)
        print(f"Generated {n} new figure description(s)")
        n_attached = attach_figure_descriptions(out_dir_default)
        print(f"Attached {n_attached} description(s) to figures.json")
        return 0

    pages: list[int] = []
    for spec in args.pages:
        pages.extend(parse_page_arg(spec))
    if args.all:
        pages = list(range(1, len(doc) + 1))
    pages = sorted(set(pages))

    if not pages:
        print("Nothing to do. Pass --pages, --all, or --search.", file=sys.stderr)
        return 1

    out_dir = args.out / args.pdf.stem
    used_fig_names: set[str] = set()
    page_results: list[dict] = []
    for p in pages:
        if p < 1 or p > len(doc):
            print(f"  skip page {p} (out of range)")
            continue
        result = extract_page(doc, p, out_dir, used_fig_names)
        page_results.append(result)
        print(f"  page {p}: {result['md_path'].name} "
              f"(printed={result['printed_page']}, {len(result['figures'])} figures)")

    n_stripped = strip_recurring_header_footer(out_dir)
    if n_stripped:
        print(f"\nStripped {n_stripped} recurring header/footer line(s) across pages")

    csv_path, n_signals = extract_signals_csv(out_dir)
    print(f"Wrote {n_signals} signal rows to {csv_path.name}")

    pages_path = write_pages_json(out_dir, page_results)
    figs_path, n_figs = write_figures_json(out_dir, page_results)
    index_path = write_index_json(out_dir, page_results)
    doc_path = write_document_md(out_dir, page_results)
    print(f"Wrote {pages_path.name} (page-number map)")
    print(f"Wrote {figs_path.name} ({n_figs} figures)")
    print(f"Wrote {index_path.name} (per-page headings/tables/figures)")
    print(f"Wrote {doc_path.name} (concatenated document)")

    if args.describe_figures:
        print(f"\nDescribing figures with {args.describe_model} (this can take a while)...")
        n = describe_all_figures(out_dir, model=args.describe_model, overwrite=args.redescribe)
        print(f"Generated {n} new figure description(s)")
        n_attached = attach_figure_descriptions(out_dir)
        print(f"Attached {n_attached} description(s) to figures.json")

    print(f"\nOutput in: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
