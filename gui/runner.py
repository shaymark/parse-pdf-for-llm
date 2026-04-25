"""Run parse_pdf.py as a subprocess and stream its stdout into a queue.

The GUI polls the queue from the Tk main loop via `root.after(...)` so the
UI stays responsive without asyncio.
"""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Sentinel events on the queue. Anything else is a raw stdout line.
EVT_DONE = "__DONE__"        # followed by ":<returncode>"
EVT_OUTPUT_DIR = "__OUT__"   # followed by ":<path>"
EVT_PAGE = "__PAGE__"        # followed by ":<n>"
EVT_TOTAL = "__TOTAL__"      # followed by ":<n>"


_RE_PAGE = re.compile(r"^\s*page\s+(\d+):")
_RE_OPENED = re.compile(r"^Opened\s+.+\u2014\s+(\d+)\s+pages")  # em-dash
_RE_OPENED_FALLBACK = re.compile(r"^Opened\s+.+?(\d+)\s+pages")
_RE_OUTDIR = re.compile(r"^Output in:\s+(.+)$")


@dataclass
class RunSpec:
    """Resolved command-line arguments for a single run."""
    script: Path
    pdf: Path
    out_dir: str
    page_mode: str          # "all" | "pages" | "search"
    pages: str              # comma- or space-separated; only when page_mode == "pages"
    search: str             # only when page_mode == "search"
    vision_tables: bool
    vision_tables_only: bool
    describe_figures: bool
    describe_only: bool
    redescribe: bool
    model: str

    def build_argv(self) -> list[str]:
        # Inside a PyInstaller bundle, sys.executable is the wrapper exe
        # (not python). Spawning [sys.executable, script] would launch a
        # second GUI. Use our --run-script multiplex instead.
        if getattr(sys, "frozen", False):
            argv: list[str] = [sys.executable, "--run-script", str(self.script), str(self.pdf)]
        else:
            argv = [sys.executable, "-u", str(self.script), str(self.pdf)]
        if self.page_mode == "all":
            argv.append("--all")
        elif self.page_mode == "pages" and self.pages.strip():
            for chunk in re.split(r"[\s,]+", self.pages.strip()):
                if chunk:
                    argv += ["--pages", chunk]
        elif self.page_mode == "search" and self.search.strip():
            argv += ["--search", self.search.strip()]
        if self.out_dir and self.out_dir.strip():
            argv += ["--out", self.out_dir.strip()]
        if self.vision_tables:
            argv.append("--vision-tables")
        if self.vision_tables_only:
            argv.append("--vision-tables-only")
        if self.describe_figures:
            argv.append("--describe-figures")
        if self.describe_only:
            argv.append("--describe-only")
        if self.redescribe:
            argv.append("--redescribe")
        if self.model and self.model.strip():
            argv += ["--describe-model", self.model.strip()]
        return argv


class Runner:
    """Owns one subprocess + one reader thread + one event queue."""

    def __init__(self) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, spec: RunSpec) -> None:
        if self.is_running:
            raise RuntimeError("A run is already in progress.")
        argv = spec.build_argv()
        self.q.put(f"$ {' '.join(self._quote(a) for a in argv)}\n")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            creationflags=creationflags,
            cwd=str(Path.cwd()),
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        if not self.is_running or self._proc is None:
            return
        try:
            if os.name == "nt":
                self._proc.terminate()
            else:
                self._proc.send_signal(signal.SIGTERM)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self.q.put(line)
                self._scan_line(line)
        except Exception as e:
            self.q.put(f"[runner error] {e}\n")
        rc = self._proc.wait() if self._proc else -1
        self.q.put(f"{EVT_DONE}:{rc}")

    def _scan_line(self, line: str) -> None:
        s = line.rstrip("\n")
        m = _RE_PAGE.match(s)
        if m:
            self.q.put(f"{EVT_PAGE}:{m.group(1)}")
            return
        m = _RE_OPENED.match(s) or _RE_OPENED_FALLBACK.match(s)
        if m:
            self.q.put(f"{EVT_TOTAL}:{m.group(1)}")
            return
        m = _RE_OUTDIR.match(s)
        if m:
            self.q.put(f"{EVT_OUTPUT_DIR}:{m.group(1).strip()}")
            return

    @staticmethod
    def _quote(s: str) -> str:
        if not s or any(c in s for c in ' "\'\t'):
            return '"' + s.replace('"', '\\"') + '"'
        return s
