"""Toplevel script editor for parse_pdf.py.

Bare-bones text view (monospace, no syntax highlight) with four buttons:
Save, Reload from disk, Reset to bundled, Open in system editor.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from gui import settings


class ScriptEditor(tk.Toplevel):
    def __init__(self, master: tk.Misc, script_path: Path) -> None:
        super().__init__(master)
        self.title(f"parse_pdf.py — {script_path}")
        self.geometry("980x720")
        self.script_path = script_path

        bar = ttk.Frame(self, padding=6)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Save", command=self._save).pack(side="left")
        ttk.Button(bar, text="Reload from disk", command=self._reload).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Reset to bundled", command=self._reset).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Open in system editor", command=self._open_external).pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body,
            wrap="none",
            undo=True,
            font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 11),
        )
        yscroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        try:
            content = self.script_path.read_text(encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Reload failed", str(e), parent=self)
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_reset()
        self._set_status(f"Loaded {self.script_path}")

    def _save(self) -> None:
        try:
            content = self.text.get("1.0", "end-1c")
            self.script_path.write_text(content, encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return
        self._set_status(f"Saved {self.script_path}")

    def _reset(self) -> None:
        if not messagebox.askyesno(
            "Reset to bundled?",
            "Overwrite your edits with the bundled parse_pdf.py?",
            parent=self,
        ):
            return
        try:
            settings.reset_script_copy()
        except Exception as e:
            messagebox.showerror("Reset failed", str(e), parent=self)
            return
        self._reload()
        self._set_status("Reset to bundled.")

    def _open_external(self) -> None:
        path = str(self.script_path)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Open failed", str(e), parent=self)

    def _set_status(self, msg: str) -> None:
        self.status.configure(text=msg)
