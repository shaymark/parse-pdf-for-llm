"""Entry point. Multiplexes between two roles:

  * Default: launch the GUI app.
  * `--run-script <path> [args...]`: re-exec ourselves as a Python
    interpreter that runs the given script. This is needed because in a
    PyInstaller `--onefile` bundle, `sys.executable` points to the
    wrapper binary (not Python), so the runner can't spawn the original
    `parse_pdf.py` via `[sys.executable, script]` — it would just spawn
    another copy of the GUI. The wrapper recognizes `--run-script` and
    becomes a script runner instead.
"""
from __future__ import annotations

import runpy
import sys


def _run_script(script: str, argv: list[str]) -> int:
    sys.argv = [script] + argv
    try:
        runpy.run_path(script, run_name="__main__")
        return 0
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        return _run_script(sys.argv[2], sys.argv[3:])
    from gui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
