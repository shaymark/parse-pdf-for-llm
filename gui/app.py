"""Tk root window for parse-pdf GUI.

Layout (top→bottom):
  1. PDF picker / drop zone
  2. Options frame (radio: all/pages, search, out dir, vision/describe checks, model)
  3. Run/Cancel/Edit buttons
  4. Log Text widget with auto-scroll toggle
  5. Status bar with "Reveal out folder" button
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from gui import ollama_models, settings
from gui.editor import ScriptEditor
from gui.runner import (
    EVT_DONE,
    EVT_OUTPUT_DIR,
    EVT_PAGE,
    EVT_TOTAL,
    RunSpec,
    Runner,
)

# tkinterdnd2 is optional. Even when the package imports cleanly its
# native tkdnd dylib may fail to load (e.g. tkinterdnd2 0.4.3 ships only
# x86_64 dylibs on macOS, so it can't load against arm64 Tk). In that
# case we silently fall back to a Browse-button-only experience.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _DND_IMPORT_OK = True
except Exception:
    _DND_IMPORT_OK = False


PAD = 6


class App:
    def __init__(self) -> None:
        self._dnd_enabled = False
        self.root: tk.Tk
        # Always start with a plain tk.Tk(); then OPT-IN to DnD by
        # loading the tkdnd Tcl package into that same interpreter.
        # This avoids TkinterDnD.Tk() raising mid-construction and
        # leaving a half-built root behind — which would break later
        # textvariable bindings for StringVars created on the second
        # root (filename / output dir entries silently went blank).
        self.root = tk.Tk()
        if _DND_IMPORT_OK:
            try:
                self.root.tk.call("package", "require", "tkdnd")
                # Mix in tkinterdnd2's instance methods on the root.
                from tkinterdnd2 import TkinterDnD as _TkDnD  # type: ignore
                self.root.TkdndVersion = self.root.tk.call(  # type: ignore[attr-defined]
                    "package", "present", "tkdnd"
                )
                # Bind drop_target_register / dnd_bind onto Misc so all
                # widgets can use them, exactly like TkinterDnD.Tk does.
                tk.Misc.drop_target_register = _TkDnD.DnDWrapper.drop_target_register  # type: ignore[attr-defined]
                tk.Misc.dnd_bind = _TkDnD.DnDWrapper.dnd_bind  # type: ignore[attr-defined]
                self._dnd_enabled = True
            except Exception:
                # tkdnd can't load (arch mismatch, missing lib, etc.).
                # Fall through with DnD disabled; Browse-button works.
                self._dnd_enabled = False
        self.root.title("parse-pdf")

        self.cfg = settings.load()
        self.root.geometry(self.cfg.get("geometry", "900x720"))

        # Tk variables ------------------------------------------------------
        self.var_pdf = tk.StringVar(value=self.cfg.get("last_pdf_path", ""))
        self.var_out = tk.StringVar(value=self.cfg.get("out_dir", "out"))
        self.var_page_mode = tk.StringVar(value=self.cfg.get("page_mode", "all"))
        self.var_pages = tk.StringVar(value=self.cfg.get("pages", ""))
        self.var_search = tk.StringVar(value=self.cfg.get("search", ""))
        self.var_vt = tk.BooleanVar(value=self.cfg.get("vision_tables", False))
        self.var_vto = tk.BooleanVar(value=self.cfg.get("vision_tables_only", False))
        self.var_df = tk.BooleanVar(value=self.cfg.get("describe_figures", False))
        self.var_do = tk.BooleanVar(value=self.cfg.get("describe_only", False))
        self.var_redesc = tk.BooleanVar(value=self.cfg.get("redescribe", False))
        self.var_model = tk.StringVar(value=self.cfg.get("model", "qwen2.5vl:7b"))
        self.var_follow = tk.BooleanVar(value=self.cfg.get("follow_log", True))
        self.var_status = tk.StringVar(value="Ready.")

        self.runner = Runner()
        self.total_pages: int | None = None
        self.last_out_dir: str | None = None

        self._build_ui()
        self._refresh_models_async()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_queue)

        # Bring window to the foreground. On macOS, Tk windows from a
        # double-clicked .app sometimes open hidden behind other apps;
        # this incantation forces the window forward without leaving it
        # always-on-top.
        self._bring_to_front()

    def _bring_to_front(self) -> None:
        # macOS: switch the app's NSApplication into a regular foreground
        # role and activate it. Without this, a Tk window from a
        # PyInstaller .app bundle can render in the background and the
        # user never sees it. AppKit/PyObjC ships with PyInstaller's
        # macOS build by default.
        if sys.platform == "darwin":
            try:
                from AppKit import (
                    NSApp,
                    NSApplication,
                    NSApplicationActivationPolicyRegular,
                )
                NSApplication.sharedApplication()
                NSApp().setActivationPolicy_(NSApplicationActivationPolicyRegular)
                NSApp().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        # Tk-level fallback (no-op on platforms where the above worked).
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=PAD)
        main.pack(fill="both", expand=True)

        # 1. Drop zone -----------------------------------------------------
        drop_frame = ttk.LabelFrame(main, text="PDF", padding=PAD)
        drop_frame.pack(fill="x")

        # Top row: drop target + Browse button
        top = ttk.Frame(drop_frame)
        top.pack(fill="x")

        self.drop_label = tk.Label(
            top,
            text=self._drop_text(),
            relief="ridge",
            bd=2,
            padx=20,
            pady=18,
            anchor="center",
            justify="center",
        )
        self.drop_label.pack(side="left", fill="x", expand=True)
        self.drop_label.bind("<Button-1>", lambda e: self._browse_pdf())

        if self._dnd_enabled:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self._on_drop)

        ttk.Button(top, text="Browse…", command=self._browse_pdf).pack(
            side="left", padx=(PAD, 0)
        )

        # Second row: full picked path (truncated from the left so the
        # filename is always visible).
        path_row = ttk.Frame(drop_frame)
        path_row.pack(fill="x", pady=(PAD // 2, 0))
        ttk.Label(path_row, text="File:").pack(side="left")
        self.path_label = ttk.Label(
            path_row,
            textvariable=self.var_pdf,
            foreground="#444",
            anchor="w",
        )
        self.path_label.pack(side="left", padx=(PAD, 0), fill="x", expand=True)

        # 2. Options -------------------------------------------------------
        opts = ttk.LabelFrame(main, text="Options", padding=PAD)
        opts.pack(fill="x", pady=(PAD, 0))

        # Page mode radios
        row = ttk.Frame(opts)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="All pages", variable=self.var_page_mode, value="all").pack(
            side="left"
        )
        ttk.Radiobutton(row, text="Pages:", variable=self.var_page_mode, value="pages").pack(
            side="left", padx=(PAD * 2, 0)
        )
        ttk.Entry(row, textvariable=self.var_pages, width=30).pack(side="left", padx=(PAD, 0))
        ttk.Label(row, text="(e.g. 14 or 10-20 or 1,3,5)", foreground="#666").pack(
            side="left", padx=(PAD, 0)
        )

        # Search row
        srow = ttk.Frame(opts)
        srow.pack(fill="x", pady=(PAD // 2, 0))
        ttk.Radiobutton(
            srow, text="Search:", variable=self.var_page_mode, value="search"
        ).pack(side="left")
        ttk.Entry(srow, textvariable=self.var_search, width=40).pack(
            side="left", padx=(PAD, 0)
        )
        ttk.Label(srow, text="(prints matching pages, no extraction)", foreground="#666").pack(
            side="left", padx=(PAD, 0)
        )

        # Out dir
        orow = ttk.Frame(opts)
        orow.pack(fill="x", pady=(PAD, 0))
        ttk.Label(orow, text="Output dir:").pack(side="left")
        # Browse button packed first on the right, then the entry takes
        # all remaining space — long paths now stretch with the window.
        ttk.Button(orow, text="Browse…", command=self._browse_out).pack(
            side="right", padx=(PAD, 0)
        )
        self.out_entry = ttk.Entry(orow, textvariable=self.var_out)
        self.out_entry.pack(side="left", padx=(PAD, 0), fill="x", expand=True)

        # Vision / describe checkboxes
        crow = ttk.Frame(opts)
        crow.pack(fill="x", pady=(PAD, 0))
        ttk.Checkbutton(crow, text="--vision-tables", variable=self.var_vt).pack(side="left")
        ttk.Checkbutton(crow, text="--vision-tables-only", variable=self.var_vto).pack(
            side="left", padx=(PAD * 2, 0)
        )
        crow2 = ttk.Frame(opts)
        crow2.pack(fill="x")
        ttk.Checkbutton(crow2, text="--describe-figures", variable=self.var_df).pack(side="left")
        ttk.Checkbutton(crow2, text="--describe-only", variable=self.var_do).pack(
            side="left", padx=(PAD * 2, 0)
        )
        ttk.Checkbutton(crow2, text="--redescribe", variable=self.var_redesc).pack(
            side="left", padx=(PAD * 2, 0)
        )

        # Model
        mrow = ttk.Frame(opts)
        mrow.pack(fill="x", pady=(PAD, 0))
        ttk.Label(mrow, text="Ollama model:").pack(side="left")
        self.model_combo = ttk.Combobox(
            mrow, textvariable=self.var_model, values=[self.var_model.get()], width=24
        )
        self.model_combo.pack(side="left", padx=(PAD, 0))
        ttk.Button(mrow, text="Refresh", command=self._refresh_models_async).pack(
            side="left", padx=(PAD, 0)
        )

        # 3. Run / Cancel / Edit ------------------------------------------
        btns = ttk.Frame(main, padding=(0, PAD))
        btns.pack(fill="x")
        self.btn_run = ttk.Button(btns, text="Run", command=self._on_run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(PAD, 0))
        ttk.Button(btns, text="View / Edit Script…", command=self._open_editor).pack(
            side="left", padx=(PAD, 0)
        )
        ttk.Checkbutton(btns, text="Auto-scroll log", variable=self.var_follow).pack(
            side="right"
        )

        # 4. Log -----------------------------------------------------------
        log_frame = ttk.LabelFrame(main, text="Log", padding=PAD)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_frame,
            wrap="word",
            height=18,
            font=("Menlo", 10) if sys.platform == "darwin" else ("Consolas", 10),
        )
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 5. Status bar ----------------------------------------------------
        sbar = ttk.Frame(main)
        sbar.pack(fill="x", pady=(PAD, 0))
        ttk.Label(sbar, textvariable=self.var_status).pack(side="left")
        self.btn_reveal = ttk.Button(
            sbar, text="Reveal out folder", command=self._reveal_out
        )
        self.btn_reveal.pack(side="right")

    def _drop_text(self) -> str:
        if self._dnd_enabled:
            return "Drop a PDF here\nor click to browse"
        return "Click to browse for a PDF"

    # ------------------------------------------------------------------
    # File picking
    # ------------------------------------------------------------------
    def _browse_pdf(self) -> None:
        initial = self.cfg.get("last_pdf_dir") or str(Path.home())
        path = filedialog.askopenfilename(
            title="Choose a PDF",
            initialdir=initial,
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if path:
            self._set_pdf(path)

    def _browse_out(self) -> None:
        initial = self.var_out.get() or str(Path.cwd())
        path = filedialog.askdirectory(title="Choose output dir", initialdir=initial)
        if path:
            self.var_out.set(path)
            # Scroll the Entry to the right so the folder name (end of
            # the path) is visible, not the leading directories.
            try:
                self.out_entry.xview_moveto(1.0)
            except Exception:
                pass
            self._set_status(f"Output → {path}")

    def _on_drop(self, event: tk.Event) -> None:
        # event.data is a brace-quoted, space-separated list of paths
        raw = event.data
        path = self._parse_dnd_path(raw)
        if path and path.lower().endswith(".pdf"):
            self._set_pdf(path)
        elif path:
            messagebox.showwarning("Not a PDF", f"Ignored {path}")

    @staticmethod
    def _parse_dnd_path(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        # Take first path if multiple were dropped
        if "} {" in raw:
            raw = raw.split("} {", 1)[0]
        return raw

    def _set_pdf(self, path: str) -> None:
        self.var_pdf.set(path)
        self.cfg["last_pdf_path"] = path
        self.cfg["last_pdf_dir"] = str(Path(path).parent)
        # Show the chosen filename inside the drop zone so it's obvious
        # something was picked.
        self.drop_label.configure(text=f"✓ {Path(path).name}\n(click to change)")
        self._set_status(f"Loaded {Path(path).name}")

    # ------------------------------------------------------------------
    # Ollama model list
    # ------------------------------------------------------------------
    def _refresh_models_async(self) -> None:
        # Capture the current model value on the main thread; Tk variables
        # are not thread-safe so the worker can't call .get() itself.
        current = self.var_model.get()
        threading.Thread(
            target=self._refresh_models, args=(current,), daemon=True
        ).start()

    def _refresh_models(self, current: str) -> None:
        models = ollama_models.list_models()
        if current and current not in models:
            models = [current] + models
        # Schedule UI update on main thread
        self.root.after(0, lambda: self.model_combo.configure(values=models))

    # ------------------------------------------------------------------
    # Run / cancel
    # ------------------------------------------------------------------
    def _on_run(self) -> None:
        pdf = self.var_pdf.get().strip()
        if not pdf or not Path(pdf).exists():
            messagebox.showerror("No PDF", "Pick a PDF first.")
            return
        try:
            script = settings.ensure_script_copy()
        except Exception as e:
            messagebox.showerror("Script not found", str(e))
            return

        spec = RunSpec(
            script=script,
            pdf=Path(pdf),
            out_dir=self.var_out.get(),
            page_mode=self.var_page_mode.get(),
            pages=self.var_pages.get(),
            search=self.var_search.get(),
            vision_tables=self.var_vt.get(),
            vision_tables_only=self.var_vto.get(),
            describe_figures=self.var_df.get(),
            describe_only=self.var_do.get(),
            redescribe=self.var_redesc.get(),
            model=self.var_model.get(),
        )

        self._clear_log()
        self.total_pages = None
        self.last_out_dir = None

        try:
            self.runner.start(spec)
        except Exception as e:
            messagebox.showerror("Failed to start", str(e))
            return

        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self._set_status("Running…")
        self._save_settings()

    def _on_cancel(self) -> None:
        if not self.runner.is_running:
            return
        self.runner.cancel()
        self._set_status("Cancelling…")

    # ------------------------------------------------------------------
    # Queue draining + log display
    # ------------------------------------------------------------------
    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.runner.q.get_nowait()
                self._handle_event(item)
        except Exception:
            pass
        self.root.after(50, self._drain_queue)

    def _handle_event(self, item: str) -> None:
        if item.startswith(EVT_DONE + ":"):
            rc = item.split(":", 1)[1]
            self._on_finished(int(rc))
            return
        if item.startswith(EVT_TOTAL + ":"):
            self.total_pages = int(item.split(":", 1)[1])
            return
        if item.startswith(EVT_PAGE + ":"):
            n = item.split(":", 1)[1]
            if self.total_pages:
                self._set_status(f"Running (page {n} of {self.total_pages})")
            else:
                self._set_status(f"Running (page {n})")
            return
        if item.startswith(EVT_OUTPUT_DIR + ":"):
            self.last_out_dir = item.split(":", 1)[1]
            return
        # Plain output line
        self._append_log(item)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if self.var_follow.get():
            self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _on_finished(self, rc: int) -> None:
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        if rc == 0:
            self._set_status("Done.")
            self._save_settings()
        elif rc < 0 or rc == 130 or rc == 143:
            self._set_status(f"Cancelled (rc={rc}).")
        else:
            self._set_status(f"Failed (rc={rc}).")

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _set_status(self, msg: str) -> None:
        self.var_status.set(msg)

    def _open_editor(self) -> None:
        try:
            script = settings.ensure_script_copy()
        except Exception as e:
            messagebox.showerror("Script not found", str(e))
            return
        ScriptEditor(self.root, script)

    def _reveal_out(self) -> None:
        # Prefer the per-run output dir extracted from the script's
        # "Output in: …" line. Fall back to the configured output root,
        # creating it if it doesn't exist yet so the button still does
        # something useful before the first run.
        candidates = []
        if self.last_out_dir:
            candidates.append(self.last_out_dir)
        if self.var_out.get().strip():
            candidates.append(self.var_out.get().strip())
        candidates.append(str(Path.cwd()))

        path = None
        for c in candidates:
            p = Path(c).expanduser()
            if p.exists():
                path = str(p)
                break
        if path is None:
            # Try creating the configured out root.
            try:
                p = Path(self.var_out.get().strip() or "out").expanduser()
                p.mkdir(parents=True, exist_ok=True)
                path = str(p)
            except Exception as e:
                messagebox.showerror("Cannot reveal", str(e))
                return

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def _save_settings(self) -> None:
        self.cfg.update({
            "last_pdf_path": self.var_pdf.get(),
            "last_pdf_dir": str(Path(self.var_pdf.get()).parent) if self.var_pdf.get() else self.cfg.get("last_pdf_dir", ""),
            "out_dir": self.var_out.get(),
            "page_mode": self.var_page_mode.get(),
            "pages": self.var_pages.get(),
            "search": self.var_search.get(),
            "vision_tables": self.var_vt.get(),
            "vision_tables_only": self.var_vto.get(),
            "describe_figures": self.var_df.get(),
            "describe_only": self.var_do.get(),
            "redescribe": self.var_redesc.get(),
            "model": self.var_model.get(),
            "follow_log": self.var_follow.get(),
            "geometry": self.root.geometry(),
        })
        settings.save(self.cfg)

    def _on_close(self) -> None:
        try:
            if self.runner.is_running:
                self.runner.cancel()
        except Exception:
            pass
        self._save_settings()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
