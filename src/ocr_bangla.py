#!/usr/bin/env python3
"""Bangla & English OCR app for macOS.

Extracts Bangla + English text from PDFs and images with Tesseract and writes a
Word document (``<name>_extracted.docx``) next to the source file.

The script is safe to run on a Mac that is missing something: instead of dying
with a traceback (invisible in a double-clicked ``.app``), it reports what is
wrong and how to fix it.

    python3 src/ocr_bangla.py              # launch the GUI
    python3 src/ocr_bangla.py --diagnose   # print an environment report
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

try:  # allow both `python src/ocr_bangla.py` and `python -m ocr_bangla`
    from . import ocr_engine as engine
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ocr_engine as engine

APP_NAME = engine.APP_NAME
APP_VERSION = engine.APP_VERSION
LOG = logging.getLogger("ocr_bangla")


# ---------------------------------------------------------------------------
# Logging / fatal-error plumbing
# ---------------------------------------------------------------------------
def log_directory() -> Path:
    preferred = Path.home() / "Library" / "Logs" / APP_NAME
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(os.environ.get("TMPDIR", "/tmp")) / "bangla-ocr-logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def log_file_path() -> Path:
    return log_directory() / "ocr-bangla.log"


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass

    # A windowed .app has no console: sys.stderr may be None or a dead pipe.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def show_native_alert(title: str, message: str) -> None:
    """Show an error even when the GUI toolkit itself is unavailable."""
    LOG.error("%s: %s", title, message)
    if sys.platform == "darwin" and (getattr(sys, "frozen", False) or sys.stderr is None):
        script = (
            'display dialog {} with title {} buttons {{"OK"}} default button "OK" '
            'with icon stop'
        ).format(_applescript_literal(message), _applescript_literal(title))
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=120,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pass
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        print(f"{title}: {message}", file=stream)


def _applescript_literal(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\r\n", "\\n").replace("\n", "\\n").replace("\t", " ")
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Tk application
# ---------------------------------------------------------------------------
class OCRApp:
    """Tk GUI.  All widget access happens on the main thread only."""

    def __init__(self, env: engine.Environment):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.env = env
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.minsize(520, 340)
        self.root.geometry("560x360")
        self.root.resizable(True, False)

        self._build_menu()
        self._build_widgets()
        self._bind_shortcuts()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # macOS: a Tk window opened from Terminal often appears *behind* the
        # terminal, which looks exactly like "the app did not launch".
        self.root.after(80, self._bring_to_front)
        self.root.after(120, self._drain_events)

    # -- construction -----------------------------------------------------
    def _build_menu(self) -> None:
        tk = self.tk
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open File…", accelerator="Cmd+O", command=self.start_file_selection)
        file_menu.add_separator()
        file_menu.add_command(label="Close Window", accelerator="Cmd+W", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=False)
        tools_menu.add_command(label="Run Diagnostics…", accelerator="Cmd+D", command=self.show_diagnostics)
        tools_menu.add_command(label="Reveal Log File", command=self._reveal_log)
        tools_menu.add_command(label="Reveal Last Output", command=self._reveal_last_output)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        try:
            self.root.config(menu=menubar)
        except Exception:  # noqa: BLE001 - menus are cosmetic
            LOG.exception("could not attach menu bar")

    def _build_widgets(self) -> None:
        tk, ttk = self.tk, self.ttk

        container = ttk.Frame(self.root, padding=18)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="Bangla & English OCR",
            font=("Helvetica Neue", 20, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            container,
            text=(
                "Pick a PDF or image. Text is extracted with Tesseract and saved as "
                "\u201c<name>_extracted.docx\u201d next to your file."
            ),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        self.banner = tk.Label(
            container,
            text="",
            anchor="w",
            justify="left",
            wraplength=500,
            padx=8,
            pady=6,
        )
        # Remember the platform default before we repaint it (ttk.Frame does not
        # expose -background, so never read the parent's colour).
        self._banner_bg = self.banner.cget("background")
        self.banner.pack(fill="x")

        self.status_label = ttk.Label(container, text="Ready.", anchor="w")
        self.status_label.pack(fill="x", pady=(10, 4))

        self.progress_bar = ttk.Progressbar(container, orient="horizontal", mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x")

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(16, 0))

        self.btn_select = ttk.Button(
            buttons, text="Select & Process File", command=self.start_file_selection
        )
        self.btn_select.pack(side="left")

        self.btn_cancel = ttk.Button(buttons, text="Cancel", command=self.cancel_processing, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)

        ttk.Button(buttons, text="Diagnostics", command=self.show_diagnostics).pack(side="right")

        self.prefer_embedded = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            container,
            text="Use a PDF's own (selectable) text when available — much faster",
            variable=self.prefer_embedded,
        ).pack(anchor="w", pady=(14, 0))

        self._refresh_banner()

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Command-o>", lambda _event: self.start_file_selection())
        self.root.bind("<Command-d>", lambda _event: self.show_diagnostics())
        self.root.bind("<Command-w>", lambda _event: self._on_close())
        self.root.bind("<Escape>", lambda _event: self.cancel_processing())

    # -- startup helpers --------------------------------------------------
    def _bring_to_front(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(250, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
            # NOTE: deliberately no AppleScript/System Events call here — that
            # would trigger an "Automation" permission prompt on every launch.
        except Exception:  # noqa: BLE001 - purely cosmetic
            LOG.debug("could not raise window", exc_info=True)

    def _refresh_banner(self) -> None:
        failures = self.env.blocking_checks
        warnings = [check for check in self.env.checks if check.status == "warn"]
        if failures:
            self.banner.configure(
                text=(
                    f"⚠  {len(failures)} problem(s) will stop OCR from working — "
                    "open Diagnostics for the exact fix."
                ),
                bg="#ffe5e5",
                fg="#8a1c1c",
            )
        elif warnings:
            self.banner.configure(
                text=f"Ready, with {len(warnings)} warning(s). See Diagnostics for details.",
                bg="#fff8e1",
                fg="#7a5b00",
            )
        else:
            self.banner.configure(text="", bg=self._banner_bg, fg="#000000")

    # -- file processing --------------------------------------------------
    def start_file_selection(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        from tkinter import filedialog

        file_path = filedialog.askopenfilename(
            title="Select PDF or Image for OCR",
            filetypes=[
                ("PDF documents", "*.pdf"),
                ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                ("All supported files", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
            ],
        )
        if file_path:
            self.start_processing(file_path)

    def start_processing(self, file_path: str | Path) -> None:
        if self.worker and self.worker.is_alive():
            return

        # Re-probe only when something was wrong: the user may have just fixed it
        # (and probing on every click would stall the UI for a second).
        if not self.env.ok:
            self.env = engine.detect_environment()
            self._refresh_banner()
        if not self.env.ok:
            self.show_diagnostics(
                intro="Tesseract or the Bangla language data is missing, so OCR cannot run yet."
            )
            return

        # Read Tk variables on the main thread and hand the value to the worker;
        # touching tkinter objects from the worker is what crashes macOS Tk.
        prefer_embedded = bool(self.prefer_embedded.get())

        self.cancel_event.clear()
        self.btn_select.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress_bar.configure(value=0)
        self.status_label.configure(text="Starting…")

        self.worker = threading.Thread(
            target=self._run_ocr,
            args=(Path(file_path), prefer_embedded),
            daemon=True,
            name="ocr-worker",
        )
        self.worker.start()

    def _run_ocr(self, path: Path, prefer_embedded: bool) -> None:
        """Worker thread: never touches Tk widgets, only posts to the queue."""
        try:
            result = engine.process_file(
                path,
                self.env,
                on_progress=lambda fraction, message: self.events.put(("progress", fraction, message)),
                cancel_event=self.cancel_event,
                prefer_embedded_text=prefer_embedded,
            )
            self.events.put(("done", result))
        except engine.Cancelled:
            self.events.put(("cancelled", "Processing cancelled."))
        except engine.OCRError as exc:
            self.events.put(("error", str(exc)))
        except Exception:  # noqa: BLE001 - anything else must be visible to the user
            LOG.exception("unexpected OCR failure")
            self.events.put(("error", f"Unexpected error:\n\n{traceback.format_exc(limit=3)}"))

    def cancel_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_label.configure(text="Cancelling…")

    def _drain_events(self) -> None:
        """Main thread pump: apply worker results to the UI safely."""
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, fraction, message = event
                    self.progress_bar.configure(value=fraction * 100)
                    self.status_label.configure(text=message)
                elif kind == "done":
                    self._finish(event[1])
                elif kind == "cancelled":
                    self._reset_controls(event[1])
                elif kind == "error":
                    self._reset_controls("Failed.")
                    self._error_dialog("OCR Error", event[1])
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(120, self._drain_events)
            except Exception:  # noqa: BLE001 - window is closing
                LOG.debug("stopped UI event pump", exc_info=True)

    def _finish(self, result: engine.ProcessResult) -> None:
        from tkinter import messagebox

        self.last_output = result.output
        self.progress_bar.configure(value=100)
        self._reset_controls(f"Done — saved {result.output.name}")

        reveal = messagebox.askyesno(
            "OCR complete",
            f"{result.summary()}.\n\nSaved to:\n{result.output}\n\nShow it in Finder?",
            parent=self.root,
        )
        if reveal:
            self._reveal(result.output)

    def _reset_controls(self, status: str) -> None:
        self.btn_select.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.status_label.configure(text=status)

    def _error_dialog(self, title: str, message: str) -> None:
        from tkinter import messagebox

        LOG.error("%s: %s", title, message)
        messagebox.showerror(title, message, parent=self.root)

    # -- tools ------------------------------------------------------------
    def show_diagnostics(self, intro: str = "") -> None:
        tk = self.tk
        from tkinter import scrolledtext

        window = getattr(self, "_diagnostics_window", None)
        if window is not None and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_force()
            return

        window = tk.Toplevel(self.root)
        window.title(f"{APP_NAME} — Diagnostics")
        window.geometry("760x520")
        window.minsize(520, 320)
        self._diagnostics_window = window

        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)

        if intro:
            self.tk.Label(frame, text=intro, fg="#8a1c1c", anchor="w", justify="left", wraplength=700).pack(
                fill="x", pady=(0, 8)
            )

        text = scrolledtext.ScrolledText(
            frame, wrap="word", font=("Menlo", 11), height=20, borderwidth=1, relief="solid"
        )
        text.pack(fill="both", expand=True)
        self.env = engine.detect_environment()
        report = engine.format_report(self.env)
        text.insert("1.0", report)
        text.configure(state="disabled")

        buttons = self.ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))

        def copy_report() -> None:
            window.clipboard_clear()
            window.clipboard_append(report)
            self.status_label.configure(text="Diagnostics copied to clipboard.")

        self.ttk.Button(buttons, text="Copy Report", command=copy_report).pack(side="left")
        self.ttk.Button(buttons, text="Reveal Log", command=self._reveal_log).pack(side="left", padx=8)
        self.ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        self._refresh_banner()

    def _reveal_log(self) -> None:
        self._reveal(log_file_path())

    def _reveal_last_output(self) -> None:
        if self.last_output and self.last_output.exists():
            self._reveal(self.last_output)
        else:
            self._error_dialog("No output yet", "Process a file first.")

    def _reveal(self, path: Path) -> None:
        try:
            if sys.platform == "darwin" and path.exists():
                subprocess.run(["open", "-R", str(path)], check=False, timeout=10)
            else:
                from tkinter import messagebox

                messagebox.showinfo("Location", str(path), parent=self.root)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("could not reveal %s: %s", path, exc)

    def _show_about(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            "About",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "Bangla + English OCR for PDFs and images.\n"
            f"Tesseract: {self.env.tesseract_version or 'not found'}\n"
            f"Languages: {', '.join(self.env.languages) or 'none'}\n"
            f"Python {platform.python_version()} · Tk {self.tk.TkVersion}",
            parent=self.root,
        )

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            from tkinter import messagebox

            if not messagebox.askyesno(
                "Quit while processing?",
                "OCR is still running. Quit anyway?",
                parent=self.root,
            ):
                return
            self.cancel_event.set()
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        # If something essential is missing, put the fix in front of the user
        # immediately instead of letting the first click fail mysteriously.
        if self.env.blocking_checks:
            self.root.after(
                500,
                lambda: self.show_diagnostics(
                    intro="Some components needed for OCR are missing. "
                    "Details and fixes are listed below."
                ),
            )
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ocr_bangla",
        description="Bangla & English OCR for PDFs and images (macOS).",
    )
    parser.add_argument("--diagnose", action="store_true", help="print an environment report and exit")
    parser.add_argument("--tesseract", metavar="PATH", help="path to the tesseract executable")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    LOG.info("Starting %s %s on %s", APP_NAME, APP_VERSION, platform.platform())

    if args.diagnose:
        env = engine.detect_environment(args.tesseract)
        print(engine.format_report(env))
        return 1 if env.blocking_checks else 0

    if threading.current_thread() is not threading.main_thread():
        show_native_alert("Startup error", "The OCR app must be started from the main thread.")
        return 2

    try:
        import tkinter  # noqa: F401,PLC0415 - explicit, so the error message is precise
    except Exception as exc:  # noqa: BLE001 - no GUI toolkit, no GUI
        show_native_alert(
            "GUI toolkit missing",
            "This Python cannot open windows because tkinter/Tk is missing or broken:\n\n"
            f"{exc}\n\n"
            "Fix it with one of:\n"
            "  • python.org Python 3.12/3.13 (includes Tk 8.6)\n"
            "  • brew install python python-tk@3.13\n\n"
            "Do not use /usr/bin/python3 — its Tk 8.5.9 crashes on recent macOS.\n\n"
            f"Details: {log_file_path()}",
        )
        return 2

    env = engine.detect_environment(args.tesseract)
    for check in env.blocking_checks:
        LOG.error("blocking: %s — %s", check.name, check.detail)

    try:
        app = OCRApp(env)
    except Exception as exc:  # noqa: BLE001 - Tk/Tcl can fail while creating the window
        last_line = traceback.format_exc().strip().splitlines()[-1]
        LOG.error("could not create the main window: %s", exc)
        LOG.debug("window creation traceback", exc_info=True)  # full detail → log file
        show_native_alert(
            "Cannot open the window",
            "tkinter/Tk reported an error while creating the main window:\n\n"
            f"{last_line}\n\n"
            "This usually means Tk is broken or mismatched with your macOS version "
            "(Tk 8.5 on macOS 11+). Install python.org Python 3.12+ or "
            "`brew install python-tk@3.13`.\n\n"
            f"Full log: {log_file_path()}",
        )
        return 1

    # Tk swallows callback exceptions into a non-existent console; log them.
    def report_callback_exception(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        LOG.error("Tk callback failed", exc_info=(exc_type, exc_value, exc_tb))
        try:
            from tkinter import messagebox

            messagebox.showerror("Unexpected error", f"{exc_type.__name__}: {exc_value}")
        except Exception:  # noqa: BLE001
            pass

    app.root.report_callback_exception = report_callback_exception

    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        LOG.exception("fatal error in the UI loop")
        show_native_alert("Crash", f"The app stopped unexpectedly.\n\nSee {log_file_path()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
