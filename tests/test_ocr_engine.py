"""End-to-end tests for the OCR engine using a fake `tesseract` binary.

Run with:  python -m unittest discover -s tests -v

The tests never need a real Tesseract install or a display server, so they also
run in CI containers (like the one this repo is developed in).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ocr_engine as engine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BANGLA_SAMPLE = "বাংলা টেস্ট নথি"

FAKE_TESSERACT = Template(
    textwrap.dedent(
        """\
        #!$python
        import os, sys

        args = sys.argv[1:]
        log = os.environ.get("FAKE_TESSERACT_LOG")

        def record(entry):
            if log:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(entry + "\\n")

        if "--version" in args:
            print("tesseract 5.4.1")
            print(" leptonica-1.84.1")
            sys.exit(0)

        tessdata = None
        if "--tessdata-dir" in args:
            tessdata = args[args.index("--tessdata-dir") + 1]

        def available(directory):
            found = []
            for code in ("ben", "eng", "osd"):
                path = os.path.join(directory, code + ".traineddata")
                if os.path.isfile(path) and os.path.getsize(path) > 65536:
                    found.append(code)
            return found

        if "--list-langs" in args:
            record("list-langs")
            langs = available(tessdata) if tessdata else ["ben", "eng", "osd"]
            if "ben" not in langs:
                sys.stderr.write("Error opening data file\\n")
                sys.exit(1)
            print("List of available languages in %s (%d):" % (tessdata or "/usr/share/tessdata", len(langs)))
            print("\\n".join(langs))
            sys.exit(0)

        record("ocr")
        if "$mode" == "fail":
            sys.stderr.write("Error opening data file %s\\n" % os.path.join(tessdata or "", "ben.traineddata"))
            sys.exit(1)

        if len(args) < 2:
            sys.stderr.write("bad invocation: %r\\n" % (args,))
            sys.exit(64)

        source, outbase = args[0], args[1]
        with open(source, "rb") as handle:
            payload = handle.read()
        text = "বাংলা টেস্ট নথি\\nBangla OCR output page"
        with open(outbase + ".txt", "w", encoding="utf-8") as handle:
            handle.write(text + "\\n")
        sys.exit(0)
        """
    )
)


def write_traineddata(path: Path, size: int = 120_000) -> Path:
    """Create a plausible (non-placeholder) .traineddata file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x01" + os.urandom(size - 4))
    return path


class FakeTesseractCase(unittest.TestCase):
    """Base class wiring BANGLA_OCR_TESSERACT/BANGLA_OCR_TESSDATA to fakes."""

    mode = "ok"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.tesseract = self.bin_dir / "tesseract"
        self.tesseract.write_text(FAKE_TESSERACT.substitute(python=sys.executable, mode=self.mode))
        self.tesseract.chmod(0o755)

        self.tessdata = self.tmp / "tessdata"
        for code in ("ben", "eng", "osd"):
            write_traineddata(self.tessdata / f"{code}.traineddata")

        self.log_path = self.tmp / "tesseract.log"
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("BANGLA_OCR_TESSERACT", "BANGLA_OCR_TESSDATA", "FAKE_TESSERACT_LOG")
        }
        os.environ["BANGLA_OCR_TESSERACT"] = str(self.tesseract)
        os.environ["BANGLA_OCR_TESSDATA"] = str(self.tessdata)
        os.environ["FAKE_TESSERACT_LOG"] = str(self.log_path)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- helpers ---------------------------------------------------------
    def tesseract_calls(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").split()

    def make_text_pdf(self, name: str = "text.pdf", body: str = "Hello Bangla document") -> Path:
        fitz = engine.pymupdf()

        path = self.tmp / name
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), body, fontsize=16)
        doc.save(path)
        doc.close()
        return path

    def make_scanned_pdf(self, name: str = "scan.pdf", pages: int = 2) -> Path:
        fitz = engine.pymupdf()
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (900, 1200), "white")
        ImageDraw.Draw(image).text((60, 200), "scan", fill="black")
        buffer = self.tmp / "page.png"
        image.save(buffer)

        path = self.tmp / name
        doc = fitz.open()
        for _ in range(pages):
            page = doc.new_page(width=612, height=792)
            page.insert_image(fitz.Rect(0, 0, 612, 792), filename=str(buffer))
        doc.save(path)
        doc.close()
        return path

    def make_image(self, name: str = "shot.png", size: tuple[int, int] = (400, 300)) -> Path:
        from PIL import Image

        path = self.tmp / name
        Image.new("RGB", size, "white").save(path)
        return path


class TestEnvironmentDetection(FakeTesseractCase):
    def test_bundled_lfs_pointers_are_detected(self) -> None:
        """The repository ships Git LFS pointers; that must be diagnosed, not ignored."""
        state, detail = engine.traineddata_status(REPO_ROOT / "tessdata" / "ben.traineddata")
        self.assertEqual(state, "lfs-pointer", detail)

    def test_traineddata_status_classifies_files(self) -> None:
        good = write_traineddata(self.tmp / "good.traineddata")
        self.assertEqual(engine.traineddata_status(good)[0], "ok")
        self.assertEqual(engine.traineddata_status(self.tmp / "nope.traineddata")[0], "missing")
        tiny = self.tmp / "tiny.traineddata"
        tiny.write_bytes(b"x" * 10)
        self.assertEqual(engine.traineddata_status(tiny)[0], "too-small")
        empty = self.tmp / "empty.traineddata"
        empty.write_bytes(b"")
        self.assertEqual(engine.traineddata_status(empty)[0], "empty")

    def test_finds_tesseract_from_env_override(self) -> None:
        self.assertEqual(engine.find_tesseract(), str(self.tesseract))

    def test_find_tessdata_prefers_complete_directory(self) -> None:
        directory, source = engine.find_tessdata()
        self.assertEqual(Path(directory).resolve(), self.tessdata.resolve())
        self.assertEqual(source, "environment")

    def test_environment_is_usable(self) -> None:
        env = engine.detect_environment(include_gui_checks=False)
        self.assertTrue(env.ok, engine.format_report(env))
        self.assertIn("ben", env.languages)
        self.assertEqual(env.lang_string.split("+")[:2], ["ben", "eng"])
        self.assertEqual(env.blocking_checks, [])
        self.assertIn("--tessdata-dir", env.tesseract_config())

    def test_missing_tesseract_fails_with_actionable_fix(self) -> None:
        os.environ["BANGLA_OCR_TESSERACT"] = str(self.tmp / "does-not-exist")
        original = engine.TESSERACT_SEARCH_PATHS
        engine.TESSERACT_SEARCH_PATHS = ()
        self.addCleanup(setattr, engine, "TESSERACT_SEARCH_PATHS", original)
        empty_bin = self.tmp / "empty-bin"
        empty_bin.mkdir(exist_ok=True)
        saved_path = os.environ.get("PATH")
        os.environ["PATH"] = str(empty_bin)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", saved_path or ""))

        env = engine.detect_environment(include_gui_checks=False)
        self.assertFalse(env.ok)
        names = {check.name: check for check in env.blocking_checks}
        self.assertIn("Tesseract binary", names)
        self.assertIn("brew install tesseract", names["Tesseract binary"].fix)

    def test_lfs_tessdata_is_reported_with_fix(self) -> None:
        pointer_dir = self.tmp / "pointer-tessdata"
        pointer_dir.mkdir()
        for code in ("ben", "eng"):
            (pointer_dir / f"{code}.traineddata").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{'0' * 64}\nsize 1789439\n"
            )
        os.environ["BANGLA_OCR_TESSDATA"] = str(pointer_dir)

        env = engine.detect_environment(include_gui_checks=False)
        report = engine.format_report(env)
        # The pointer directory must never be used as the tessdata source.
        self.assertNotEqual(env.tessdata_source, "environment")
        self.assertIn("Git LFS pointer", report)
        self.assertIn("fetch_tessdata.sh", report)
        self.assertIn("git lfs pull", report)

    def test_languages_split_across_directories_are_merged(self) -> None:
        """MacPorts data layout: ben in one dir, eng in another (or in the bundle)."""
        ben_only = self.tmp / "macports-tessdata"
        ben_only.mkdir()
        write_traineddata(ben_only / "ben.traineddata")
        eng_only = self.tmp / "other-tessdata"
        eng_only.mkdir()
        write_traineddata(eng_only / "eng.traineddata")

        os.environ["BANGLA_OCR_TESSDATA"] = str(ben_only)
        original = engine.SYSTEM_TESSDATA_DIRS
        engine.SYSTEM_TESSDATA_DIRS = (str(eng_only),)
        self.addCleanup(setattr, engine, "SYSTEM_TESSDATA_DIRS", original)

        env = engine.detect_environment(include_gui_checks=False)
        self.assertTrue(env.ok, engine.format_report(env))
        self.assertEqual(env.tessdata_source, "combined")
        self.assertIn("ben", env.languages)
        self.assertIn("eng", env.languages)
        self.assertEqual(env.lang_string.split("+")[:2], ["ben", "eng"])

        merged = Path(env.tessdata_dir)
        self.assertTrue((merged / "ben.traineddata").exists())
        self.assertTrue((merged / "eng.traineddata").exists())
        self.assertFalse(any(ch.isspace() for ch in str(merged)))
        self.assertIn("Combined language data", engine.format_report(env))

    def test_missing_english_is_reported_with_macports_fix(self) -> None:
        ben_only = self.tmp / "ben-only"
        ben_only.mkdir()
        write_traineddata(ben_only / "ben.traineddata")
        os.environ["BANGLA_OCR_TESSDATA"] = str(ben_only)
        original = engine.SYSTEM_TESSDATA_DIRS
        engine.SYSTEM_TESSDATA_DIRS = ()
        self.addCleanup(setattr, engine, "SYSTEM_TESSDATA_DIRS", original)

        env = engine.detect_environment(include_gui_checks=False)
        report = engine.format_report(env)
        self.assertTrue(env.ok)
        self.assertEqual(env.lang_string, "ben")
        self.assertIn("English language data", report)
        self.assertIn("tesseract-eng", report)
        self.assertIn("Add that model", report)

    def _as_package_manager(self, macports: bool, homebrew: bool):
        """Pretend this machine has only the given package manager(s)."""
        self.addCleanup(setattr, engine, "macports_prefix", engine.macports_prefix)
        self.addCleanup(setattr, engine, "homebrew_prefix", engine.homebrew_prefix)
        engine.macports_prefix = (lambda: engine.Path("/opt/local")) if macports else (lambda: None)
        engine.homebrew_prefix = (lambda: engine.Path("/opt/homebrew")) if homebrew else (lambda: None)

    def test_hints_never_mention_brew_on_a_macports_only_mac(self) -> None:
        self._as_package_manager(macports=True, homebrew=False)
        language = engine.language_pack_fix()
        self.assertNotIn("brew", language)
        self.assertIn("sudo port install tesseract-ben tesseract-eng", language)
        self.assertIn("selfupdate", language)  # outdated port trees are common
        self.assertIn("fetch_tessdata.sh", language)  # works without any package manager

        tesseract = engine.tesseract_install_fix()
        self.assertNotIn("brew", tesseract)
        self.assertIn("port install tesseract", tesseract)

        tk = engine.tkinter_install_fix()
        self.assertNotIn("brew", tk)
        self.assertIn(f"py{sys.version_info.major}{sys.version_info.minor}-tkinter", tk)

    def test_hints_never_mention_macports_on_a_homebrew_only_mac(self) -> None:
        self._as_package_manager(macports=False, homebrew=True)
        language = engine.language_pack_fix()
        self.assertNotIn("port install", language)
        self.assertIn("brew install tesseract-lang", language)
        self.assertIn("fetch_tessdata.sh", language)

    def test_macports_only_machine_gets_macports_fixes_in_the_report(self) -> None:
        self._as_package_manager(macports=True, homebrew=False)
        ben_only = self.tmp / "ben-only-report"
        ben_only.mkdir()
        write_traineddata(ben_only / "ben.traineddata")
        os.environ["BANGLA_OCR_TESSDATA"] = str(ben_only)
        original = engine.SYSTEM_TESSDATA_DIRS
        engine.SYSTEM_TESSDATA_DIRS = ()
        self.addCleanup(setattr, engine, "SYSTEM_TESSDATA_DIRS", original)

        report = engine.format_report(engine.detect_environment(include_gui_checks=False))
        self.assertNotIn("brew", report)
        self.assertIn("sudo port install tesseract-ben tesseract-eng", report)
        self.assertIn("fetch_tessdata.sh", report)

    def test_report_ends_with_numbered_next_steps(self) -> None:
        env = engine.Environment(tesseract_cmd=None)
        env.checks = [
            engine.Check("Tesseract binary", "fail", "not found", "brew install tesseract"),
            engine.Check("Language data (tessdata)", "fail", "missing", "run fetch script"),
            engine.Check("Log directory", "ok", "fine"),
        ]
        report = engine.format_report(env)
        self.assertIn("Next steps (in order):", report)
        self.assertIn("  1. brew install tesseract", report)
        self.assertIn("  2. run fetch script", report)
        self.assertNotIn("  3.", report)

    def test_tessdata_with_spaces_is_made_shell_safe(self) -> None:
        spaced = self.tmp / "Application Support" / "Bangla OCR" / "tessdata"
        spaced.mkdir(parents=True)
        for code in ("ben", "eng"):
            write_traineddata(spaced / f"{code}.traineddata")
        os.environ["BANGLA_OCR_TESSDATA"] = str(spaced)

        env = engine.detect_environment(include_gui_checks=False)
        config = env.tesseract_config()
        import shlex

        parts = shlex.split(config)
        tessdata_arg = parts[parts.index("--tessdata-dir") + 1]
        self.assertNotIn(" ", tessdata_arg)
        self.assertTrue(Path(tessdata_arg).is_dir())
        self.assertTrue((Path(tessdata_arg) / "ben.traineddata").exists())

    def test_frozen_bundle_layouts_are_found(self) -> None:
        """PyInstaller 6 puts onedir data in Contents/Frameworks, older builds in
        Contents/Resources — both must be discovered inside a .app bundle."""
        cases = {
            "pyinstaller6": ("Frameworks/tessdata", "Frameworks"),
            "legacy-resources": ("Resources/tessdata", "Frameworks"),
            "plain-onedir": ("tessdata", ""),
        }
        os.environ.pop("BANGLA_OCR_TESSDATA", None)
        saved = {name: getattr(sys, name, None) for name in ("frozen", "_MEIPASS", "executable")}
        try:
            for case, (relative, meipass_suffix) in cases.items():
                with self.subTest(case=case):
                    bundle = self.tmp / f"{case}.app" / "Contents"
                    data_dir = bundle / relative
                    data_dir.mkdir(parents=True, exist_ok=True)
                    for code in ("ben", "eng"):
                        write_traineddata(data_dir / f"{code}.traineddata")

                    sys.frozen = True
                    sys._MEIPASS = str(bundle / meipass_suffix) if meipass_suffix else str(data_dir)
                    sys.executable = str(bundle / "MacOS" / "Bangla OCR")
                    if not meipass_suffix:
                        sys.executable = str(data_dir / "Bangla OCR")

                    directory, source = engine.find_tessdata()
                    self.assertIsNotNone(directory, f"{case}: no tessdata found")
                    self.assertEqual(Path(directory).resolve(), data_dir.resolve(), case)
                    self.assertEqual(source, "bundled")
        finally:
            for name, value in saved.items():
                if value is None:
                    try:
                        delattr(sys, name)
                    except AttributeError:
                        pass
                else:
                    setattr(sys, name, value)

    def test_diagnostics_can_skip_gui_checks(self) -> None:
        env = engine.detect_environment(include_gui_checks=False)
        self.assertNotIn("tkinter (GUI toolkit)", [check.name for check in env.checks])


class TestProcessing(FakeTesseractCase):
    def test_scanned_pdf_is_ocrd_page_by_page(self) -> None:
        pdf = self.make_scanned_pdf(pages=2)
        events: list[tuple[float, str]] = []

        result = engine.process_file(pdf, engine.detect_environment(include_gui_checks=False), on_progress=lambda f, m: events.append((f, m)))

        self.assertTrue(result.output.exists())
        self.assertEqual(result.pages, 2)
        self.assertEqual(result.ocr_pages, 2)
        self.assertGreater(result.characters, 0)
        self.assertEqual(self.tesseract_calls().count("ocr"), 2)
        self.assertEqual(events[-1][0], 1.0)

        from docx import Document

        text = "\n".join(p.text for p in Document(result.output).paragraphs)
        self.assertIn("বাংলা", text)

    def test_text_pdf_skips_ocr(self) -> None:
        pdf = self.make_text_pdf(body="Already selectable text layer")
        env = engine.detect_environment(include_gui_checks=False)

        result = engine.process_file(pdf, env, prefer_embedded_text=True)

        self.assertEqual(result.embedded_pages, 1)
        self.assertEqual(result.ocr_pages, 0)
        self.assertNotIn("ocr", self.tesseract_calls())
        self.assertEqual(result.output.name, "text_extracted.docx")

    def test_text_pdf_can_force_ocr(self) -> None:
        pdf = self.make_text_pdf()
        env = engine.detect_environment(include_gui_checks=False)
        result = engine.process_file(pdf, env, prefer_embedded_text=False)
        self.assertEqual(result.ocr_pages, 1)
        self.assertEqual(result.embedded_pages, 0)

    def test_image_is_ocrd(self) -> None:
        image = self.make_image()
        result = engine.process_file(image, engine.detect_environment(include_gui_checks=False))
        self.assertTrue(result.output.exists())
        self.assertEqual(result.output.name, "shot_extracted.docx")
        self.assertEqual(result.pages, 1)
        self.assertEqual(self.tesseract_calls().count("ocr"), 1)

    def test_multipage_tiff_is_ocrd_frame_by_frame(self) -> None:
        from PIL import Image

        path = self.tmp / "multi.tiff"
        frames = [Image.new("RGB", (300, 200), "white") for _ in range(3)]
        frames[0].save(path, save_all=True, append_images=frames[1:])
        result = engine.process_file(path, engine.detect_environment(include_gui_checks=False))
        self.assertEqual(result.pages, 3)
        self.assertEqual(self.tesseract_calls().count("ocr"), 3)

    def test_unsupported_format_is_explained(self) -> None:
        path = self.tmp / "notes.docx"
        path.write_text("x")
        with self.assertRaises(engine.UnsupportedFormat):
            engine.process_file(path, engine.detect_environment(include_gui_checks=False))

    def test_cancellation_stops_without_output(self) -> None:
        pdf = self.make_scanned_pdf(pages=4)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(engine.Cancelled):
            engine.process_file(
                pdf, engine.detect_environment(include_gui_checks=False), cancel_event=cancel
            )
        self.assertFalse((self.tmp / "scan_extracted.docx").exists())
        self.assertEqual(self.tesseract_calls().count("ocr"), 0)

    def test_missing_file_is_an_ocr_error(self) -> None:
        with self.assertRaises(engine.OCRError):
            engine.process_file(self.tmp / "ghost.pdf", engine.detect_environment(include_gui_checks=False))


class TestFailureModes(FakeTesseractCase):
    mode = "fail"

    def test_tesseract_data_error_becomes_actionable_message(self) -> None:
        image = self.make_image()
        env = engine.detect_environment(include_gui_checks=False)
        with self.assertRaises(engine.OCRError) as raised:
            engine.process_file(image, env)
        message = str(raised.exception)
        self.assertIn("language data", message)
        self.assertIn("fetch_tessdata.sh", message)


class TestNoTextFound(FakeTesseractCase):
    def test_blank_ocr_output_raises_clear_error(self) -> None:
        empty_driver = self.tmp / "empty-ocr"
        empty_driver.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('tesseract 5.4.1'); sys.exit(0)\n"
            "if '--list-langs' in args:\n"
            "    print('List of available languages (3):'); print('ben'); print('eng'); sys.exit(0)\n"
            "open(args[1] + '.txt', 'w').close()\n"
        )
        empty_driver.chmod(0o755)
        os.environ["BANGLA_OCR_TESSERACT"] = str(empty_driver)

        env = engine.detect_environment(include_gui_checks=False)
        with self.assertRaises(engine.NoTextFound):
            engine.process_file(self.make_image(), env)


class TestCommandLine(unittest.TestCase):
    """The CLI must work even where tkinter is not installed (headless/CI)."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "src" / "ocr_bangla.py"), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )

    def test_diagnose_prints_a_report(self) -> None:
        result = self.run_cli("--diagnose")
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertIn("RESULT:", result.stdout)
        self.assertIn("diagnostics", result.stdout)

    def test_version(self) -> None:
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn(engine.APP_VERSION, result.stdout)

    def test_gui_startup_failure_is_reported_not_crashed(self) -> None:
        """Without tkinter the process must exit cleanly with a message."""
        result = self.run_cli()
        if "tkinter" in (result.stderr + result.stdout).lower() or result.returncode != 0:
            self.assertNotIn("Traceback (most recent call last)", result.stderr)
            self.assertIn("tk", (result.stderr + result.stdout).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
