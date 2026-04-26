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
  parse-pdf <pdf_path> [--pages 14] [--pages 10-20] [--all]
  parse-pdf <pdf_path> --search "GPIO_DISP_B2_08"

(Or `python parse_pdf.py ...` if you prefer to invoke the bundled script
directly without installing the pip package.)
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


def forward_fill_tables(md: str, sparsity_threshold: float = 0.4) -> str:
    """Forward-fill PDF row-spans, but only in columns that look merged.

    PDFs visually merge cells vertically (a 'Group' or 'Voltage' column written
    once, applying to the rows beneath). Markdown can't express that — those
    cells come out empty.

    Naïvely filling every empty cell is dangerous: in a spec table (Min/Typ/Max),
    empty often genuinely means 'not specified', and propagating values from
    above is wrong. So we fill a column only when its non-empty cells are
    sparse (≤ `sparsity_threshold` of body rows), which is the visual signature
    of a merged-group column. We also refuse to fill with text that matches
    the column header — that catches cases where pymupdf4llm misparsed the
    caption as the header row and the real header leaked into the body.
    """
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        cells = _split_row(lines[i])
        if not cells or i + 1 >= n:
            out.append(lines[i])
            i += 1
            continue
        sep = _split_row(lines[i + 1])
        if not sep or not _is_separator_row(sep):
            out.append(lines[i])
            i += 1
            continue

        headers = cells
        n_cols = len(headers)
        body: list[list[str]] = []
        j = i + 2
        while j < n:
            row = _split_row(lines[j])
            if row is None:
                break
            body.append(row)
            j += 1

        out.append(lines[i])      # header row
        out.append(lines[i + 1])  # separator

        if not body:
            i = j
            continue

        # Pad / truncate rows to header width.
        for r in body:
            while len(r) < n_cols:
                r.append("")
            del r[n_cols:]

        # A column is "merged-group-like" if it has ≥ 1 value but ≤ threshold
        # of rows are non-empty.
        cap = max(1, int(len(body) * sparsity_threshold))
        fill_cols: set[int] = set()
        for col in range(n_cols):
            non_empty = sum(1 for r in body if r[col])
            if 0 < non_empty <= cap:
                fill_cols.add(col)

        def _is_bold_only(v: str) -> bool:
            v = v.strip()
            return bool(v) and v.startswith("**") and v.endswith("**") \
                and "**" not in v[2:-2]

        last = [""] * n_cols
        for r in body:
            non_empty = [c for c in r if c]
            looks_like_header = bool(non_empty) and all(
                _is_bold_only(c) for c in non_empty
            )
            if looks_like_header:
                # pymupdf4llm misparsed the caption as the header row and the
                # real header landed here. Don't fill it, don't seed `last`
                # from it (otherwise the bold header words leak downward).
                out.append("|" + "|".join(r) + "|")
                continue
            for col in range(n_cols):
                if col in fill_cols and not r[col] and last[col] \
                        and last[col] != headers[col] \
                        and not _is_bold_only(last[col]):
                    r[col] = last[col]
            last = list(r)
            out.append("|" + "|".join(r) + "|")

        i = j

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
# Match each Figure/Table caption; stop before the next caption keyword so
# two captions sharing one text block (common in this doc) split correctly.
_CAPTION_RE = re.compile(
    r"(?:^|\s)(Figure|Table)\s+(\d+)\s*[:\.]\s*(.+?)"
    r"(?=\s+(?:Figure|Table)\s+\d+\s*[:\.]|\s*$)",
    re.IGNORECASE,
)


def _detect_printed_page(txt: str) -> int | None:
    """Find the printed page number (in the running header/footer)."""
    lines = txt.splitlines()
    for ln in lines[:8] + lines[-8:]:
        m = _PRINTED_PAGE_RE.match(ln)
        if m:
            return int(m.group(1))
    return None


def _detect_table_bboxes(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Return bounding boxes of tables on the page, top-to-bottom.

    Uses PyMuPDF's table finder. Returns [] if the finder is unavailable
    or fails (older PyMuPDF, pages without rule-based tables).
    """
    try:
        finder = page.find_tables()
    except Exception:
        return []
    rects: list[pymupdf.Rect] = []
    for t in getattr(finder, "tables", []):
        # PyMuPDF can occasionally return a Table object whose cells list
        # is empty (some borderless / single-cell layouts trigger it).
        # Accessing .bbox in that case raises ValueError: min() arg is
        # empty. Skip such tables instead of crashing the whole page.
        try:
            bbox = t.bbox
        except (ValueError, AttributeError):
            continue
        if bbox is None:
            continue
        rects.append(pymupdf.Rect(*bbox))
    rects.sort(key=lambda r: r.y0)
    return rects


def _pair_captions_with_bboxes(captions: list[dict],
                               bboxes: list[pymupdf.Rect]) -> list[pymupdf.Rect | None]:
    """For each table caption, return the closest table bbox below it (or None).

    Captions appear above their table in this corpus. We allow up to 60pt of
    vertical gap, and a small (-20pt) overlap to absorb caption/table merging.
    Each bbox can be claimed at most once.
    """
    table_caps = [(i, c) for i, c in enumerate(captions) if c["type"] == "table"]
    pairing: dict[int, pymupdf.Rect | None] = {i: None for i, _ in table_caps}
    used: set[int] = set()
    for i, c in table_caps:
        cr = c["rect"]
        best, best_dy = None, float("inf")
        for j, bb in enumerate(bboxes):
            if j in used:
                continue
            dy = bb.y0 - cr.y1
            if dy < -20 or dy > 60:
                continue
            if dy < best_dy:
                best, best_dy = j, dy
        if best is not None:
            pairing[i] = bboxes[best]
            used.add(best)
    # Return only the table-caption pairings, in the original caption order.
    return [pairing[i] for i, _ in table_caps]


def _detect_captions(page: pymupdf.Page) -> list[dict]:
    """Find Figure/Table captions on the page with their bounding rects.

    Captions can be split across lines inside a single text block, or two
    captions can share one block (e.g. 'Figure 11: ... Table 17: ...'), so
    we collapse whitespace and `finditer` all matches in each block.
    """
    captions: list[dict] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
        normalised = re.sub(r"\s+", " ", text).strip()
        for m in _CAPTION_RE.finditer(normalised):
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


def find_recurring_image_xrefs(doc: pymupdf.Document, threshold: float = 0.3,
                               min_pages: int = 4) -> set[int]:
    """Return xrefs of images that appear on many pages — usually logos / headers.

    A xref is flagged if it occurs on >= max(min_pages, threshold * n_pages).
    """
    counts: Counter[int] = Counter()
    for page in doc:
        for img in page.get_images(full=True):
            counts[img[0]] += 1
    cutoff = max(min_pages, int(len(doc) * threshold))
    return {xref for xref, n in counts.items() if n >= cutoff}


def extract_page(doc: pymupdf.Document, page_num_1based: int, out_dir: Path,
                 used_fig_names: set[str], skip_xrefs: set[int] | None = None) -> dict:
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
        if skip_xrefs and xref in skip_xrefs:
            continue
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
    table_bboxes = _detect_table_bboxes(page)
    bbox_per_caption = _pair_captions_with_bboxes(captions, table_bboxes)
    table_caption_dicts = []
    for c, bb in zip(table_captions, bbox_per_caption):
        d = {"number": c["number"], "title": c["title"], "caption": c["caption"]}
        if bb is not None:
            d["bbox"] = [bb.x0, bb.y0, bb.x1, bb.y1]
        table_caption_dicts.append(d)

    # Cross-link: every figure carries the captions of tables on the same page
    # (and vice-versa, built when index.json is written).
    for f in figures:
        if f.get("file"):
            f["related_tables"] = table_caption_dicts

    return {
        "pdf_page": page_num_1based,
        "printed_page": printed_page,
        "md": md,
        "md_path": md_path,
        "txt_path": txt_path,
        "figures": figures,
        "table_captions": table_caption_dicts,
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
        figures = [{"file": f["file"], "figure_number": f.get("figure_number"),
                    "caption": f.get("caption")}
                   for f in r["figures"] if f.get("file")]
        entries.append({
            "pdf_page": r["pdf_page"],
            "printed_page": r["printed_page"],
            "md_file": r["md_path"].name,
            "headings": _extract_headings(md),
            "tables": _extract_table_summaries(md),
            "table_captions": r["table_captions"],
            "figures": figures,
            # Cross-link: every table-caption gets the figures on the same page,
            # and vice-versa (figures already carry related_tables in figures.json).
            "table_to_figures": {t["caption"]: [f["caption"] for f in figures if f["caption"]]
                                 for t in r["table_captions"]},
        })
    path = out_dir / "index.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _render_md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a markdown table from headers + rows."""
    out = ["|" + "|".join(headers) + "|",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("|" + "|".join(row) + "|")
    return "\n".join(out)


def write_tables_json(out_dir: Path, page_results: list[dict],
                      doc: pymupdf.Document | None = None) -> tuple[Path, int]:
    """Build tables.json + one self-contained markdown file per table.

    If `doc` is provided, also render each table's bounding box as a sibling
    PNG (`table_NN_<slug>.png`). The PNG is the input for `--vision-tables`
    and is also useful for human verification of the text-parsed table.
    """
    items = []
    used_names: set[str] = set()
    for r in page_results:
        md = r["md_path"].read_text(encoding="utf-8") if r["md_path"].exists() else ""
        md_tables = list(_iter_md_tables(md))
        related_figs = [{"file": f["file"], "caption": f.get("caption")}
                        for f in r["figures"] if f.get("file")]
        page = doc[r["pdf_page"] - 1] if doc is not None else None
        # Pair captions with markdown tables in order — the Nth caption on the
        # page typically matches the Nth markdown table on that page.
        for i, cap in enumerate(r["table_captions"]):
            md_table = md_tables[i] if i < len(md_tables) else None
            headers = [h.replace("<br>", " ").strip() for h in md_table[0]] if md_table else []
            rows = md_table[1] if md_table else []
            n_rows = len(rows)

            # Per-table file: table_NN_<slug>.md, self-contained.
            base = f"table_{cap['number']:02d}_{_slugify(cap['title'])}"
            name = f"{base}.md"
            if name in used_names:
                name = f"{base}_p{r['pdf_page']:03d}.md"
            used_names.add(name)
            body = (
                f"# {cap['caption']}\n\n"
                f"PDF page: {r['pdf_page']}  |  Printed page: {r['printed_page']}\n\n"
                + (_render_md_table(headers, rows) if headers else "_table content not parsed — see page markdown_")
                + "\n"
            )
            (out_dir / name).write_text(body, encoding="utf-8")

            png_name: str | None = None
            if page is not None and "bbox" in cap:
                bb = pymupdf.Rect(*cap["bbox"])
                margin = 8
                clip = pymupdf.Rect(bb.x0 - margin, bb.y0 - margin,
                                    bb.x1 + margin, bb.y1 + margin) & page.rect
                try:
                    pix = page.get_pixmap(clip=clip, dpi=200, alpha=False)
                    png_name = name.replace(".md", ".png")
                    pix.save(out_dir / png_name)
                    pix = None
                except Exception:
                    png_name = None

            item = {
                "table_number": cap["number"],
                "caption": cap["caption"],
                "title": cap["title"],
                "file": name,
                "page": r["pdf_page"],
                "printed_page": r["printed_page"],
                "headers": headers,
                "n_rows": n_rows,
                "related_figures": related_figs,
            }
            if png_name:
                item["image"] = png_name
            items.append(item)
    path = out_dir / "tables.json"
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return path, len(items)


# --- Vision-LLM table extraction ---------------------------------------------

_TABLE_VISION_PROMPT = """Extract this table EXACTLY as it appears in the image.
Output as a GitHub Flavored Markdown table.

STRICT RULES:
- COLUMN COUNT IS FIXED: Before writing anything, count the visible columns of the table. Every row you emit (header, separator, every body row) MUST have EXACTLY that number of `|`-separated cells. NEVER skip a column. If the row count doesn't match, redo it.
- The header row of the markdown table MUST be the visible column headers from the image.
- GRID LAYOUTS (BGA pinouts, package ballouts, matrix tables): If the image is a 2-D grid where ROWS are numbered (1, 2, 3, …) along one edge and COLUMNS are lettered (A, B, C, …) along the other edge, the letters are the column headers and the numbers are the first column of each row. The letter row is a header even when it is drawn at the BOTTOM of the image — emit it as the FIRST row of the markdown table.
- Output one markdown row per visible body row. Do not skip or merge rows.
- VERTICAL MERGES: A cell whose text visually spans MULTIPLE body rows (one piece of text centered across several rows, with NO horizontal line separating those rows in that column) is a vertical merge. Markdown cannot express vertical merges, so REPEAT that exact text in EVERY row the merge covers. Example: if the "Note" cell shows "Use with populated Trust Secure Element" centered across 7 rows of signals, every one of those 7 rows must have "Use with populated Trust Secure Element" in its Note column.
- HORIZONTALLY MERGED HEADERS: If a header cell spans multiple data columns (e.g. a single "Air Flow (m/s)" header above three data columns 0/1/2.5), put the spanning text in the FIRST column of the header row and leave the other spanned header cells empty.
- TRULY EMPTY CELLS: A cell with no text at all (not part of a merge) must be empty in the output. Never copy a value from a neighbouring cell to fill a genuinely empty cell. Distinguish carefully between "merged" (one text covering many rows — repeat) and "empty" (no text — leave empty).
- DEPOPULATED / N/A CELLS: A cell containing only a diagonal slash (/), an "X", a dash (—), or a blank-with-shading is an intentionally-empty position (e.g. a depopulated BGA ball). Emit it as an EMPTY cell `|  |` — do NOT omit the cell, do NOT shift later cells left. The column count rule above is non-negotiable.
- Do NOT invent columns. Do NOT add a "Notes" or "Comments" column.
- Preserve subscripts and superscripts as plain text (e.g. tPD, VDD, °C, ±, μ).
- Footnote markers like [a], [b], [e] are part of the cell text — keep them attached.
- If a column header in the image is a unit (e.g. "Unit"), keep it.
- Output ONLY the markdown table (header row, separator row, body rows). No commentary, no preamble, no code fences, no explanation after.
"""


def vision_extract_table(image_path: Path, *, caption: str | None = None,
                         hint_md: str | None = None,
                         model: str = "qwen2.5vl:7b") -> str:
    """Ask the vision LLM to re-extract a table from its rendered image.

    `hint_md` is intentionally ignored — early experiments showed the model
    regurgitates the (broken) text-parse instead of reading the image. The
    parameter is kept for API stability.
    """
    del hint_md
    import ollama
    parts = [_TABLE_VISION_PROMPT]
    if caption:
        parts.append(f"\nThe table caption (for context only) is: {caption}")
    prompt = "\n".join(parts)
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [str(image_path)]}],
        options={
            "temperature": 0.1,
            # Ollama defaults are tiny: num_predict=128 (truncates after
            # ≈10 short table rows) and num_ctx=2048 (overflows once the
            # image + this long prompt fill the window). Bump both so
            # long tables aren't silently cut off mid-row.
            "num_predict": 8192,
            "num_ctx": 16384,
        },
    )
    return resp["message"]["content"].strip()


def _normalize_table_widths(headers: list[str],
                            rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Make every row the same length as the widest row.

    PDFs often have a spanning title cell on the first row (e.g. "θJA vs. Air
    Flow (m/s)" spanning 3 data columns). Vision LLMs emit it as a 1-column
    header, which is invalid GFM — markdown viewers then drop everything past
    column 1. Padding the header up to the body width makes the markdown valid
    while preserving the spanning title in the first cell.
    """
    width = max([len(headers)] + [len(r) for r in rows], default=0)
    if width == 0:
        return headers, rows
    headers = headers + [""] * (width - len(headers))
    rows = [r + [""] * (width - len(r)) for r in rows]
    return headers, rows


def _parse_first_md_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    """Pull the first markdown table out of arbitrary text. Return (headers, rows)."""
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    text = re.sub(r"\s*```\s*$", "", text)
    headers: list[str] | None = None
    rows: list[list[str]] = []
    seen_sep = False
    for line in text.splitlines():
        cells = _split_row(line)
        if cells is None:
            if headers is not None and seen_sep and rows:
                break
            continue
        if headers is None:
            headers = cells
            continue
        if not seen_sep:
            if _is_separator_row(cells):
                seen_sep = True
            else:
                # Unexpected — second row isn't a separator. Bail.
                return None
            continue
        rows.append(cells)
    if not headers or not seen_sep:
        return None
    return headers, rows


def _validate_vision_table(parsed: tuple[list[str], list[list[str]]] | None,
                           expected_cols: int, expected_rows: int,
                           expected_headers: list[str] | None = None) -> bool:
    """Sanity-check the LLM output against the text-parsed shape.

    If the text-parsed headers are obviously broken (all identical — pymupdf4llm
    spread the caption across every column), don't trust them; the LLM output
    is almost certainly more correct, so skip the shape gate.
    """
    if parsed is None:
        return False
    headers, rows = parsed
    if not headers:
        return False
    text_parse_broken = bool(expected_headers) and len(set(expected_headers)) == 1
    if not text_parse_broken:
        if expected_cols and abs(len(headers) - expected_cols) > 1:
            return False
        if expected_rows:
            lo = max(1, int(expected_rows * 0.5) - 1)
            hi = int(expected_rows * 1.5) + 2
            if not (lo <= len(rows) <= hi):
                return False
    return True


def vision_extract_all_tables(out_dir: Path, model: str = "qwen2.5vl:7b",
                              overwrite: bool = False) -> tuple[int, int]:
    """Replace text-parsed tables with vision-LLM extractions where valid.

    Returns (n_replaced, n_attempted).
    """
    tables_json = out_dir / "tables.json"
    if not tables_json.exists():
        return (0, 0)
    items = json.loads(tables_json.read_text(encoding="utf-8"))
    n_done = 0
    n_attempted = 0
    for item in items:
        png = item.get("image")
        if not png:
            continue
        png_path = out_dir / png
        md_path = out_dir / item["file"]
        if not png_path.exists():
            continue
        if item.get("vision_extracted") and not overwrite:
            continue
        n_attempted += 1
        # Build hint from current md (the text-parsed body).
        hint = None
        if md_path.exists():
            hint = md_path.read_text(encoding="utf-8")
        try:
            print(f"  vision-extracting {item['file']} ...", flush=True)
            out = vision_extract_table(png_path, caption=item["caption"],
                                       hint_md=hint, model=model)
        except Exception as e:
            print(f"    failed: {e}")
            continue
        parsed = _parse_first_md_table(out)
        if not _validate_vision_table(parsed, len(item.get("headers") or []),
                                       item.get("n_rows") or 0,
                                       expected_headers=item.get("headers")):
            print(f"    rejected (shape mismatch); keeping text-parse")
            continue
        headers, rows = _normalize_table_widths(*parsed)
        body = (
            f"# {item['caption']}\n\n"
            f"PDF page: {item['page']}  |  Printed page: {item['printed_page']}\n\n"
            f"_Extracted by vision LLM ({model}) from {png}._\n\n"
            + _render_md_table(headers, rows)
            + "\n"
        )
        md_path.write_text(body, encoding="utf-8")
        item["headers"] = headers
        item["n_rows"] = len(rows)
        item["vision_extracted"] = True
        n_done += 1
    tables_json.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return n_done, n_attempted


def write_readme(out_dir: Path, pdf_name: str, n_pages: int, n_signals: int,
                 n_figures: int, n_tables: int) -> Path:
    """Write an agent-oriented README that tells a fresh agent where to look."""
    content = f"""# Extracted content for `{pdf_name}`

This folder was produced by `parse_pdf.py`. **Read this README first.**

## If you are an agent, start here

1. **Read `index.json`** (small). One entry per PDF page with: printed page \
number, headings, tables present, figures present, and file names. Use it to \
route queries — do not read the whole document blindly.
2. **For signal / pinout / pad lookups**: `grep` **`signals.csv`**. It has \
one row per signal with columns `pad, signal, cpu_ball, io, group, usage, \
voltage, page`. {n_signals} rows total.
3. **For a specific table** (e.g. "Table 17"): open its sibling \
`table_NN_*.md` file directly — it is self-contained (caption + full table). \
**`tables.json`** is the index if you only know the number or want to search.
4. **For a specific figure** (e.g. "Figure 3"): open its sibling `.md` file \
(e.g. `figure_03_*.md`). That file is a text description generated by a vision \
LLM — it lists labels, signal names, and connections visible in the figure, \
so you usually do not need to read the PNG. `figures.json` has the index of \
all {n_figures} figures with cross-links to tables on the same page.
5. **For a page's full content**: read `page_NNN.md` (cleaned) — not \
`document.md`, which is the concatenation of all pages and is expensive to \
read in full.
6. **To translate "page N (printed)" ↔ "PDF page"**: consult `pages.json`. \
The printed number is usually PDF page minus 1, but not always.

## File map

| File | What it is | When to use |
|---|---|---|
| `index.json` | Per-page headings/tables/figures/cross-links. | First stop, always. |
| `pages.json` | `{{pdf_page: printed_page}}` map. | Translating page numbers. |
| `signals.csv` | {n_signals} signal rows from every pinout table. | Signal name lookup. |
| `tables.json` | {n_tables} table entries (caption, page, headers, related figures, file). | Finding a specific table by number. |
| `table_NN_*.md` | Self-contained markdown of one table (caption + rows). | **Direct read** when you know the table number. If the file says "Extracted by vision LLM", the body came from the image — empty cells are reliable. |
| `table_NN_*.png` | Rendered crop of the table from the PDF page. | Verifying the markdown by eye, or feeding to a vision LLM. |
| `figures.json` | {n_figures} figure entries (caption, page, related tables, description). | Finding a specific figure by number. |
| `page_NNN.md` | Cleaned markdown of one PDF page (tables forward-filled, headers stripped). | Reading one page in detail. |
| `page_NNN.txt` | Plain text of the same page. | Fallback when markdown layout is garbled. |
| `page_NNN.raw.md` | Unprocessed pymupdf4llm output — debugging only. | When the cleaned version looks wrong. |
| `document.md` | All {n_pages} pages concatenated with `<!-- pdf_page=X printed_page=Y -->` anchors. | RAG / embedding pipelines. **Not for interactive use.** |
| `figure_NN_*.png` | Caption-named figure images. | Inspecting a figure visually. |
| `figure_NN_*.md` | Vision-LLM description of that figure. | **Read this instead of the PNG** unless text is missing. |
| `page_NNN_fig_NN.png` / `.md` | Figures without a detected `Figure N:` caption (icons, block fragments). | Same, but expect less-useful descriptions. |

## How tables get forward-filled

PDFs merge cells vertically (a group label written once, applying to rows \
below). Markdown cannot express that. Every `page_NNN.md` already has the \
merged values copied down, so every row stands alone. An agent does **not** \
need to scan upward to find the group name.

Example — the row for signal GPIO_DISP_B2_08 in `page_015.md`:

    |R16|GPIO_DISP_B2_08|-|IO|GPIO_DISP_B2|IO Muxing Options|1.8 V|

## How figure descriptions were produced

Each figure was classified (schematic, block diagram, mechanical, pinout, \
generic) and sent to a local vision LLM (default: `qwen2.5vl:7b` via Ollama) \
with a type-specific prompt plus the caption and same-page table text for \
context. The output is grounded in what the model could see; if small text \
was unreadable, the description will say so.

## Things to distrust

- Figure descriptions for dense mechanical drawings or tiny icons may be \
  vague — the **table on the same page** is almost always more reliable. \
  `figures.json[i].related_tables` points you to it.
- Pymupdf4llm's layout heuristics occasionally merge cells that should stay \
  apart or split rows mid-cell. If a row in `page_NNN.md` looks wrong, fall \
  back to `page_NNN.txt` (lossless but column-less) or `page_NNN.raw.md`.
- OCR was only used on pages that had no extractable text — figures embedded \
  in text pages were **not** OCR'd. If you need the letters inside a \
  schematic, the figure description file is your source.
"""
    path = out_dir / "README.md"
    path.write_text(content, encoding="utf-8")
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

_GENERIC_PROMPT = """\
You are looking at a figure from a hardware/embedded-systems datasheet.

Write a concise description (5–10 sentences) for an engineer who cannot see \
the image. Be factual. Cover, when visible:
- The kind of figure (block diagram, schematic, pinout, mechanical drawing, \
  photo, table, icon, etc.).
- The main components/blocks (chip names, modules, connectors).
- Every signal/net/label name you can read (list them verbatim, even partial).
- How blocks are connected (which signals go where), if relevant.
- Power rails, voltages, ground, units, dimensions when shown.

Do not invent details. If text is unreadable, say so. Output plain text only — \
no Markdown headings, no preamble like "This figure shows".
"""

_SCHEMATIC_PROMPT = """\
You are looking at a schematic / connection diagram from a hardware datasheet.

For an engineer who cannot see the image, list:
- Every component / chip / block by its label (e.g. "RT1170", "LPSDRAM").
- Every signal name visible (verbatim, including bus widths like A[12:0]).
- Every connection: "<source pin> → <destination pin>".
- Power rails and ground nets (e.g. VCC1V8, VDD, VSS).

Output as plain text. No headings, no preamble. Do not invent connections \
that are not visible. If unsure, say so.
"""

_BLOCK_DIAGRAM_PROMPT = """\
You are looking at a block diagram from a hardware datasheet.

For an engineer who cannot see the image, describe:
- Every named block (verbatim).
- Arrows / interfaces between blocks (which block talks to which, and via \
  which interface label if shown — e.g. "I2C", "SPI", "SEMC").
- External connectors or boundaries shown.
- Any colour-coding or grouping (e.g. green = pads, dashed = optional).

Output plain text only. No headings, no preamble. Do not invent links that \
are not visible.
"""

_MECHANICAL_PROMPT = """\
You are looking at a mechanical / dimensional drawing from a hardware datasheet.

For an engineer who cannot see the image, describe:
- The view (top, side, bottom, isometric, section A-A, etc.).
- Every dimension callout letter visible (A, B, C, C1, D1, …) and what it \
  measures spatially. Be precise about the two surfaces or features each \
  callout spans (e.g. "B is the thickness of the PCB layer between the top \
  copper and bottom copper").
- The vertical stack-up of layers if shown (e.g. carrier-board → solder → \
  module PCB → CPU → capacitors).
- Any numeric values or tolerances printed on the drawing.

Output plain text only. No headings, no preamble. Do not invent dimensions \
that are not visible. If a callout letter exists but its position is unclear, \
say so explicitly.
"""

_PINOUT_PROMPT = """\
You are looking at a pinout / package drawing from a hardware datasheet.

For an engineer who cannot see the image, describe:
- The package type (BGA, LGA, QFP, etc.) and ball/pin count if shown.
- The orientation marker (pin 1 indicator).
- Row/column labelling scheme (letters A, B, ... and numbers 1, 2, ...).
- Notable named pins or pin groups visible.

Output plain text only. No headings, no preamble. Do not invent pin names.
"""

_PROMPTS_BY_TYPE = {
    "schematic": _SCHEMATIC_PROMPT,
    "block_diagram": _BLOCK_DIAGRAM_PROMPT,
    "mechanical": _MECHANICAL_PROMPT,
    "pinout": _PINOUT_PROMPT,
    "generic": _GENERIC_PROMPT,
}


def classify_figure(caption: str | None) -> str:
    """Pick a prompt category from the figure caption."""
    if not caption:
        return "generic"
    c = caption.lower()
    if any(w in c for w in ("dimension", "side view", "top view", "bottom view",
                            "section", "mechanical", "drawing", "package")):
        if "package" in c or "ball" in c or "pin" in c:
            return "pinout"
        return "mechanical"
    if any(w in c for w in ("schematic", "connection", "circuit")):
        return "schematic"
    if "block diagram" in c or " block " in c or "structure" in c:
        return "block_diagram"
    if "pinout" in c or "ball" in c:
        return "pinout"
    return "generic"


def _build_figure_prompt(caption: str | None, related_tables: list[dict] | None,
                         page_md_excerpt: str | None) -> str:
    kind = classify_figure(caption)
    prompt = _PROMPTS_BY_TYPE[kind]
    extras: list[str] = []
    if caption:
        extras.append(f"The figure caption is: {caption}")
    if related_tables:
        joined = "; ".join(t["caption"] for t in related_tables)
        extras.append(f"Tables on the same page: {joined}. The dimension/label "
                      "letters in the figure may be defined in one of these tables.")
    if page_md_excerpt:
        extras.append("Relevant table content from the page (use it to ground "
                      "your reading of letters/labels — do NOT just copy values "
                      "from the table that aren't visible in the figure):\n"
                      f"{page_md_excerpt}")
    if extras:
        prompt = prompt + "\nContext:\n" + "\n\n".join(extras) + "\n"
    return prompt


def describe_figure(image_path: Path, *, caption: str | None = None,
                    related_tables: list[dict] | None = None,
                    page_md_excerpt: str | None = None,
                    model: str = "qwen2.5vl:7b") -> str:
    """Generate a description with a type-aware prompt and optional page context."""
    import ollama
    prompt = _build_figure_prompt(caption, related_tables, page_md_excerpt)
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [str(image_path)]}],
        options={
            "temperature": 0.2,
            # Same rationale as vision_extract_table — long figure
            # descriptions (especially block-diagram-style figures with
            # many labelled subblocks) hit the default 128-token cap.
            "num_predict": 4096,
            "num_ctx": 16384,
        },
    )
    return resp["message"]["content"].strip()


def _page_md_for_figure(out_dir: Path, page_num: int) -> str | None:
    """Return the markdown of the figure's page (truncated) for use as context."""
    f = out_dir / f"page_{page_num:03d}.md"
    if not f.exists():
        return None
    txt = f.read_text(encoding="utf-8").strip()
    return txt if len(txt) <= 4000 else txt[:4000] + "\n...[truncated]"


def describe_all_figures(out_dir: Path, model: str = "qwen2.5vl:7b", overwrite: bool = False) -> int:
    """Generate <figure>.md alongside every figure PNG. Skips existing unless overwrite."""
    fig_json = out_dir / "figures.json"
    by_file: dict[str, dict] = {}
    if fig_json.exists():
        for item in json.loads(fig_json.read_text(encoding="utf-8")):
            if item.get("file"):
                by_file[item["file"]] = item

    pngs = sorted(out_dir.glob("*.png"))
    n_done = 0
    for png in pngs:
        md_path = png.with_suffix(".md")
        if md_path.exists() and not overwrite:
            continue
        meta = by_file.get(png.name, {})
        caption = meta.get("caption")
        related = meta.get("related_tables")
        page_num = meta.get("page")
        excerpt = _page_md_for_figure(out_dir, page_num) if page_num else None
        try:
            kind = classify_figure(caption)
            print(f"  describing {png.name} [{kind}] ...", flush=True)
            text = describe_figure(png, caption=caption, related_tables=related,
                                   page_md_excerpt=excerpt, model=model)
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
    ap.add_argument("--vision-tables", action="store_true",
                    help="After extraction, re-extract every table from its rendered PNG using the vision LLM. Replaces the text-parsed table_NN_*.md only when the LLM output passes shape validation.")
    ap.add_argument("--vision-tables-only", action="store_true",
                    help="Skip extraction; only run vision-LLM table extraction over the existing out dir.")
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
    if args.vision_tables_only:
        if not out_dir_default.exists():
            print(f"--vision-tables-only needs an existing out dir: {out_dir_default}", file=sys.stderr)
            return 1
        print(f"Vision-extracting tables in {out_dir_default} with {args.describe_model}...")
        n_done, n_try = vision_extract_all_tables(out_dir_default, model=args.describe_model,
                                                   overwrite=True)
        print(f"Replaced {n_done}/{n_try} tables with vision-LLM extractions")
        return 0

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
    skip_xrefs = find_recurring_image_xrefs(doc)
    if skip_xrefs:
        print(f"Skipping {len(skip_xrefs)} recurring image xref(s) (logos / headers): {sorted(skip_xrefs)}")
    page_results: list[dict] = []
    for p in pages:
        if p < 1 or p > len(doc):
            print(f"  skip page {p} (out of range)")
            continue
        result = extract_page(doc, p, out_dir, used_fig_names, skip_xrefs=skip_xrefs)
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
    tables_path, n_tables = write_tables_json(out_dir, page_results, doc=doc)
    index_path = write_index_json(out_dir, page_results)
    doc_path = write_document_md(out_dir, page_results)
    readme_path = write_readme(out_dir, args.pdf.name, len(page_results), n_signals,
                               n_figs, n_tables)
    print(f"Wrote {pages_path.name} (page-number map)")
    print(f"Wrote {figs_path.name} ({n_figs} figures)")
    print(f"Wrote {tables_path.name} ({n_tables} tables)")
    print(f"Wrote {index_path.name} (per-page headings/tables/figures)")
    print(f"Wrote {doc_path.name} (concatenated document)")
    print(f"Wrote {readme_path.name} (agent-oriented guide)")

    if args.vision_tables:
        print(f"\nVision-extracting tables with {args.describe_model} (this can take a while)...")
        n_done, n_try = vision_extract_all_tables(out_dir, model=args.describe_model)
        print(f"Replaced {n_done}/{n_try} tables with vision-LLM extractions")

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
