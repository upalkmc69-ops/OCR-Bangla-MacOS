"""GUI tests driven through a tkinter stub (no display required).

These cover the failure modes that make the real app look like "it doesn't
launch": Tk calls from the worker thread, silent exceptions, missing components.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tk_stub  # noqa: E402

tk_stub.install()

import ocr_bangla  # noqa: E402
import ocr_engine as engine  # noqa: E402
from test_ocr_engine import TestProcessing  # noqa: E402


def usable_environment(**overrides) -> engine.Environment:
    env = engine.Environment(
        tesseract_cmd="/opt/homebrew/bin/tesseract",
        tesseract_version="tesseract 5.4.1",
        tessdata_dir=Path("/tmp/tessdata"),
        tessdata_source="bundled",
        languages=["ben", "eng", "osd"],
    )
    env.checks = overrides.pop("checks", [engine.Check("Tesseract binary", "ok", "tesseract 5.4.1")])
    for key, value in overrides.items():
        setattr(env, key, value)
    return env


def broken_environment() -> engine.Environment:
    env = engine.Environment(tesseract_cmd=None, tesseract_error="not found")
    env.checks = [
        engine.Check("Tesseract binary", "fail", "tesseract was not found", "brew install tesseract")
    ]
    return env


class TestWindowStartup(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tk_stub.install()
        self.app = ocr_bangla.OCRApp(usable_environment())
        self.addCleanup(self.app.root.destroy)

    def test_window_opens_without_tk_eval(self) -> None:
        """`tk::PlaceWindow` is an undocumented Tcl proc that is missing on some Tk
        builds and aborts startup before the window appears — never rely on it."""
        self.state.pumps_after(5)
        self.assertEqual(self.state.evals, [])
        self.assertIn(("Tk", "lift", "MainThread"), self.state.widget_calls)

    def test_healthy_environment_shows_no_warning_banner(self) -> None:
        self.assertEqual(self.app.banner.cget("text"), "")
        self.assertEqual(str(self.app.btn_select.cget("state") or "normal"), "normal")

    def test_broken_environment_warns_immediately(self) -> None:
        app = ocr_bangla.OCRApp(broken_environment())
        self.addCleanup(app.root.destroy)
        self.assertIn("problem", app.banner.cget("text"))
        # Diagnostics should open on its own shortly after launch.
        app.run()
        self.state.pumps_after(20)
        self.assertIsNotNone(getattr(app, "_diagnostics_window", None))
        self.assertEqual(self.state.dialogs, [])

    def test_diagnostics_window_contains_report(self) -> None:
        self.app.show_diagnostics()
        window = self.app._diagnostics_window
        self.assertTrue(window.winfo_exists())

        texts = [
            widget
            for widget in _walk(window)
            if isinstance(widget, tk_stub._Widget) and "ScrolledText" in type(widget).__name__
        ]
        self.assertTrue(texts, "diagnostics window has no report widget")
        self.assertIn("RESULT:", texts[0].content)


class TestProcessingFlow(unittest.TestCase):
    """Full click → worker thread → dialog flow, with Tesseract faked."""

    def setUp(self) -> None:
        self.state = tk_stub.install()
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-gui-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Reuse the engine test fakes (fake tesseract binary + tessdata).
        self.base = TestProcessing("test_image_is_ocrd")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)

        self.env = engine.detect_environment(include_gui_checks=False)
        self.assertTrue(self.env.ok, engine.format_report(self.env))

        self._original_detect = engine.detect_environment
        engine.detect_environment = lambda *a, **k: self.env
        self.addCleanup(setattr, engine, "detect_environment", self._original_detect)

        self.app = ocr_bangla.OCRApp(self.env)
        self.addCleanup(self.app.root.destroy)
        self.app.root.after = self._schedule  # deterministic pump for this test

    def _schedule(self, delay, callback=None, *args):
        self.state.scheduled.append((delay, (lambda: callback(*args)) if callback else None))
        return "after"

    def wait_for_worker(self, app, timeout: float = 60.0) -> None:
        app.worker.join(timeout)
        self.assertFalse(app.worker.is_alive(), "OCR worker did not finish")
        self.state.pumps_after(40)

    def test_successful_run_updates_ui_and_writes_docx(self) -> None:
        image = self.base.make_image("receipt.png")
        self.state.open_dialog_result = str(image)

        self.app.start_file_selection()
        self.wait_for_worker(self.app)

        output = image.with_name("receipt_extracted.docx")
        self.assertTrue(output.exists())

        self.assertEqual(str(self.app.progress_bar.cget("value")), "100")
        self.assertIn("receipt_extracted.docx", self.app.status_label.cget("text"))
        kinds = [kind for kind, _title, _message in self.state.dialogs]
        self.assertEqual(kinds, ["askyesno"])
        self.assertIn("Saved to", self.state.dialogs[0][2])
        self.assertIn("1 page", self.state.dialogs[0][2])

    def test_worker_thread_never_touches_tk(self) -> None:
        """The original code called widget.config() from the worker → macOS crash."""
        image = self.base.make_image("thread.png")
        self.app.start_processing(image)
        self.wait_for_worker(self.app)

        offenders = self.state.worker_touches()
        self.assertEqual(offenders, [], f"Tk touched off the main thread: {offenders}")

    def test_progress_messages_reach_the_ui(self) -> None:
        image = self.base.make_image("progress.png")
        self.app.start_processing(image)
        self.wait_for_worker(self.app)

        statuses = [call for call in self.state.widget_calls if call[1] == "configure"]
        self.assertTrue(statuses)
        self.assertIn("Done", self.app.status_label.cget("text"))
        self.assertEqual(str(self.app.progress_bar.cget("value")), "100")

    def test_cancel_re_enables_the_button(self) -> None:
        pdf = self.base.make_scanned_pdf("long.pdf", pages=6)
        self.app.start_processing(pdf)
        self.app.cancel_processing()
        self.wait_for_worker(self.app)

        self.assertEqual(str(self.app.btn_select.cget("state") or "normal"), "normal")
        self.assertEqual(str(self.app.btn_cancel.cget("state")), "disabled")
        self.assertFalse((self.tmp / "long_extracted.docx").exists())

    def test_missing_components_open_diagnostics_instead_of_working(self) -> None:
        # e.g. Tesseract was removed after the window opened
        self.env = broken_environment()
        self.app.env = self.env
        self.app._refresh_banner()
        image = self.base.make_image("blocked.png")
        self.app.start_processing(image)

        self.assertIsNone(self.app.worker, "OCR must not start without Tesseract")
        self.assertTrue(self.app._diagnostics_window.winfo_exists())
        self.assertIn("problem", self.app.banner.cget("text"))

    def test_processing_error_is_shown_as_a_dialog_not_a_crash(self) -> None:
        original = engine.process_file

        def boom(*args, **kwargs):
            raise engine.OCRError("simulated tesseract failure")

        engine.process_file = boom
        self.addCleanup(setattr, engine, "process_file", original)

        self.app.start_processing(self.base.make_image("boom.png"))
        self.wait_for_worker(self.app)

        errors = [dialog for dialog in self.state.dialogs if dialog[0] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("simulated tesseract failure", errors[0][2])
        self.assertEqual(str(self.app.btn_select.cget("state") or "normal"), "normal")

    def test_unexpected_exception_is_reported_with_traceback(self) -> None:
        original = engine.process_file

        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        engine.process_file = boom
        self.addCleanup(setattr, engine, "process_file", original)

        self.app.start_processing(self.base.make_image("kaboom.png"))
        self.wait_for_worker(self.app)

        errors = [dialog for dialog in self.state.dialogs if dialog[0] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("kaboom", errors[0][2])


class TestDialogs(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tk_stub.install()
        self.app = ocr_bangla.OCRApp(usable_environment())
        self.addCleanup(self.app.root.destroy)

    def test_about_reports_versions(self) -> None:
        self.app._show_about()
        kind, _title, message = self.state.dialogs[-1]
        self.assertEqual(kind, "info")
        self.assertIn("tesseract 5.4.1", message)
        self.assertIn("Bangla OCR", message)
        self.assertIn("ben", message)

    def test_copy_report_button_fills_the_clipboard(self) -> None:
        self.app.show_diagnostics()
        buttons = [w for w in _walk(self.app._diagnostics_window) if type(w).__name__ == "ttk.Button"]
        copy_button = next(button for button in buttons if button.cget("text") == "Copy Report")
        copy_button.cget("command")()
        self.assertIn("RESULT:", self.state.copy_buffer)
        self.assertIn("clipboard", self.app.status_label.cget("text").lower())


def _walk(widget, seen=None):
    """Yield the widget and any stubbed children recorded on it."""
    seen = seen if seen is not None else set()
    if id(widget) in seen:
        return
    seen.add(id(widget))
    yield widget
    for child in getattr(widget, "_children", []) or []:
        yield from _walk(child, seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
