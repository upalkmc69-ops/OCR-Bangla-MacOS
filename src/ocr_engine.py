"""Core OCR engine for the Bangla OCR macOS app.

This module deliberately has **no tkinter dependency** so that it can be used
from the command line, from tests, and from the GUI.  Everything that can make
the app fail to start (missing Tesseract, broken ``tessdata``, unreadable
folders...) is surfaced here as a :class:`Check` instead of an exception, which
lets the GUI show a human-readable report instead of dying silently.

Run ``python3 src/ocr_bangla.py --diagnose`` to print the report.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("ocr_engine")

APP_NAME = "Bangla OCR"
APP_VERSION = "1.1.0"

#: Languages the app needs for Bangla + English documents.
REQUIRED_LANGS = ("ben", "eng")
#: Secondary languages used when present (orientation/script detection).
OPTIONAL_LANGS = ("osd",)

SUPPORTED_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".pnm",
    ".ppm",
    ".pgm",
}

#: A PDF page that already carries this many characters of real (selectable)
#: text is used as-is instead of being rasterised and OCR'd.
MIN_EMBEDDED_CHARS = 20

#: Hard limit for a single page/frame. A pathological image must not hang the app.
OCR_TIMEOUT_SECONDS = 600

#: Overrides, mainly for tests and power users.
ENV_TESSERACT = "BANGLA_OCR_TESSERACT"
ENV_TESSDATA = "BANGLA_OCR_TESSDATA"

#: Typical macOS install locations.  A Finder-launched .app gets
#: PATH=/usr/bin:/bin:/usr/sbin:/sbin, so ``shutil.which`` alone is not enough.
TESSERACT_SEARCH_PATHS = (
    "/opt/homebrew/bin/tesseract",  # Homebrew, Apple Silicon
    "/usr/local/bin/tesseract",  # Homebrew, Intel
    "/opt/local/bin/tesseract",  # MacPorts
    "/usr/bin/tesseract",
    "~/bin/tesseract",
    "~/.local/bin/tesseract",
)

SYSTEM_TESSDATA_DIRS = (
    "/opt/homebrew/share/tessdata",  # Homebrew, Apple Silicon
    "/usr/local/share/tessdata",  # Homebrew, Intel
    "/opt/local/share/tessdata",  # MacPorts
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tesseract-ocr/tessdata",
)

_GIT_LFS_SIGNATURE = b"version https://git-lfs"
_MIN_TRAINEDDATA_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """A single line of the diagnostics report."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""

    @property
    def icon(self) -> str:
        return {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}.get(self.status, "?   ")


@dataclass
class Environment:
    """Resolved runtime environment (Tesseract binary + language data)."""

    tesseract_cmd: str | None = None
    tesseract_version: str | None = None
    tesseract_error: str | None = None
    tessdata_dir: Path | None = None
    tessdata_source: str = "none"  # environment | bundled | system | combined | none
    languages: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    _config: str | None = field(default=None, repr=False)

    # -- convenience ------------------------------------------------------
    @property
    def ok(self) -> bool:
        return self.tesseract_cmd is not None and self.lang_string is not None

    @property
    def lang_string(self) -> str | None:
        wanted = [code for code in REQUIRED_LANGS if code in self.languages]
        if "ben" not in wanted:
            return None
        if "osd" in self.languages:
            wanted.append("osd")
        return "+".join(wanted)

    @property
    def blocking_checks(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    def tesseract_config(self) -> str:
        """``config`` string for pytesseract.

        pytesseract 0.3.13+ splits this with :func:`shlex.split`, so the path is
        quoted.  Paths containing spaces are additionally re-homed to a
        space-free alias directory for compatibility with older pytesseract
        releases, which used a plain ``str.split()``.
        """
        if self._config is not None:
            return self._config

        parts = ["--psm", "3"]
        if self.tessdata_dir is not None:
            safe_dir = _space_free_alias(self.tessdata_dir)
            parts += ["--tessdata-dir", shlex.quote(str(safe_dir))]
        self._config = " ".join(parts)
        return self._config


@dataclass
class ProcessResult:
    source: Path
    output: Path
    pages: int = 0
    characters: int = 0
    ocr_pages: int = 0
    embedded_pages: int = 0

    def summary(self) -> str:
        bits = [f"{self.pages} page{'s' if self.pages != 1 else ''}"]
        if self.embedded_pages:
            bits.append(f"{self.embedded_pages} with embedded text")
        if self.ocr_pages:
            bits.append(f"{self.ocr_pages} OCR'd")
        return f"{', '.join(bits)} — {self.characters:,} characters extracted"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class OCRError(Exception):
    """Base class for expected, user-presentable failures."""


class UnsupportedFormat(OCRError):
    pass


class NoTextFound(OCRError):
    pass


class Cancelled(OCRError):
    pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """Run a command with a hard timeout and no console-popping on macOS."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def traineddata_status(path: Path) -> tuple[str, str]:
    """Classify a ``*.traineddata`` file.

    Returns ``(state, human_detail)`` where state is one of
    ``ok``, ``missing``, ``lfs-pointer``, ``too-small``, ``empty``.
    """
    path = Path(path)
    if not path.is_file():
        return "missing", "file not found"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError as exc:  # permissions, I/O errors, ...
        return "missing", f"unreadable ({exc})"

    if not head:
        return "empty", "file is empty"
    if head.startswith(_GIT_LFS_SIGNATURE):
        return "lfs-pointer", f"{size}-byte Git LFS pointer, not a real model"
    if size < _MIN_TRAINEDDATA_BYTES:
        return "too-small", f"only {size} bytes — truncated or placeholder file"

    return "ok", f"{size / 1_048_576:.1f} MB"


def _space_free_alias(directory: Path) -> Path:
    """Return a symlink farm for *directory* if its path contains whitespace.

    Keeps ``--tessdata-dir`` usable regardless of pytesseract's argument
    splitting behaviour (``/Applications/Bangla OCR.app/...`` contains spaces).
    """
    directory = Path(directory)
    if not any(ch.isspace() for ch in str(directory)):
        return directory

    try:
        import hashlib

        digest = hashlib.sha1(str(directory).encode("utf-8")).hexdigest()[:10]
        alias = Path(tempfile.gettempdir()) / f"bangla-ocr-tessdata-{digest}"
        alias.mkdir(parents=True, exist_ok=True)
        for item in directory.iterdir():
            if item.is_file() and item.name.endswith(".traineddata"):
                target = alias / item.name
                if not target.exists():
                    target.symlink_to(item)
        return alias
    except OSError as exc:  # pragma: no cover - exotic filesystem failure
        LOG.warning("Could not build space-free tessdata alias: %s", exc)
        return directory


def bundled_tessdata_dirs() -> list[Path]:
    """Candidate locations of the language data shipped with this app."""
    candidates: list[Path] = []

    override = os.environ.get(ENV_TESSDATA)
    if override:
        candidates.append(_expand(override))

    if getattr(sys, "frozen", False):
        # PyInstaller: onedir .app puts data in Contents/Frameworks in
        # PyInstaller >= 6, but Contents/Resources in older versions and in
        # onefile builds the layout differs again.  Check every variant.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = Path(meipass)
            candidates += [
                base / "tessdata",
                base.parent / "Resources" / "tessdata",
                base.parent / "tessdata",
            ]
        exe = Path(sys.executable).resolve()
        candidates += [
            exe.parent / "tessdata",
            exe.parent.parent / "Resources" / "tessdata",
            exe.parent.parent / "Frameworks" / "tessdata",
            exe.parent.parent.parent / "Resources" / "tessdata",
        ]
    here = Path(__file__).resolve().parent
    candidates += [
        here / "tessdata",  # data copied next to the code
        here.parent / "tessdata",  # repository checkout (src/../tessdata)
        Path.cwd() / "tessdata",
    ]

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def system_tessdata_dirs() -> list[Path]:
    candidates = [_expand(p) for p in SYSTEM_TESSDATA_DIRS]

    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        base = _expand(prefix)
        candidates.insert(0, base)
        candidates.insert(1, base / "tessdata")
    return [p for p in candidates if p.is_dir()]


def _language_set(directory: Path) -> set[str]:
    """Which required languages are actually usable in *directory*."""
    return {
        code
        for code in REQUIRED_LANGS
        if traineddata_status(Path(directory) / f"{code}.traineddata")[0] == "ok"
    }


def _count_valid(directory: Path) -> int:
    return len(_language_set(directory))


def tessdata_candidates(explicit: str | None = None) -> list[tuple[Path, str]]:
    """Every tessdata directory worth looking at, best first, without duplicates.

    Order matters: an explicit/``BANGLA_OCR_TESSDATA`` directory wins over the
    copy that ships with the app, which wins over system installs.
    """
    groups: list[tuple[list[Path], str]] = []
    override = explicit or os.environ.get(ENV_TESSDATA)
    if override:
        groups.append(([_expand(override)], "environment"))
    groups.append((bundled_tessdata_dirs(), "bundled"))
    groups.append((system_tessdata_dirs(), "system"))

    candidates: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directories, source in groups:
        for directory in directories:
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            candidates.append((resolved, source))
    return candidates


def merge_tessdata(candidates: list[tuple[Path, str]]) -> tuple[Path | None, dict[str, Path]]:
    """Combine languages that live in *different* tessdata directories.

    MacPorts installs each language as its own port, so ``ben`` and ``eng`` can
    easily end up in separate directories (or one bundled, one system-wide).
    Tesseract only accepts a single ``--tessdata-dir``, so we build a small
    symlink farm that contains the best copy of every language.
    """
    chosen: dict[str, Path] = {}
    for directory, _source in candidates:
        for code in REQUIRED_LANGS + OPTIONAL_LANGS:
            if code in chosen:
                continue
            path = directory / f"{code}.traineddata"
            if path.is_file() and traineddata_status(path)[0] == "ok":
                chosen[code] = path
    if not chosen:
        return None, {}

    import hashlib  # noqa: PLC0415 - only needed here

    digest = hashlib.sha1(
        "|".join(f"{code}={path}" for code, path in sorted(chosen.items())).encode("utf-8")
    ).hexdigest()[:12]
    target = Path(tempfile.gettempdir()) / f"bangla-ocr-tessdata-{digest}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        # Drop stale entries (a language that is no longer available anywhere).
        for stale in target.glob("*.traineddata"):
            if stale.name[: -len(".traineddata")] not in chosen:
                stale.unlink(missing_ok=True)
        for code, source in chosen.items():
            link = target / f"{code}.traineddata"
            try:
                if link.is_symlink() or link.exists():
                    if link.is_symlink() and link.resolve() == source.resolve():
                        continue
                    link.unlink()
                link.symlink_to(source)
            except OSError:
                shutil.copy2(source, link)
    except OSError as exc:  # pragma: no cover - exotic filesystem failure
        LOG.warning("Could not build combined tessdata directory: %s", exc)
        best = next(iter(chosen.values())).parent
        return best, chosen
    LOG.info("Combined tessdata in %s from %s", target, {c: str(p) for c, p in chosen.items()})
    return target, chosen


def find_tessdata(explicit: str | None = None) -> tuple[Path | None, str]:
    """Locate a usable ``tessdata`` directory.

    Returns ``(path, source)``; ``source`` is ``bundled``, ``environment``,
    ``system`` or ``none``.
    """
    candidates = tessdata_candidates(explicit)

    # 1. A single directory that already has every required language.
    for directory, source in candidates:
        if _count_valid(directory) == len(REQUIRED_LANGS):
            return directory, source

    # 2. Split across directories (e.g. ben from MacPorts, eng from the bundle).
    combined, _chosen = merge_tessdata(candidates)
    if combined is not None and _count_valid(combined) == len(REQUIRED_LANGS):
        return combined, "combined"

    # 3. Partial data still beats nothing, but do not create a temp directory
    #    that holds exactly what one real directory already holds.
    if combined is not None and _count_valid(combined):
        best = max(candidates, key=lambda item: _count_valid(item[0]), default=None)
        if best is not None and _language_set(best[0]) == _language_set(combined):
            return best
        return combined, "combined"
    return None, "none"


def find_tesseract(explicit: str | None = None) -> str | None:
    """Locate the ``tesseract`` executable."""
    candidates: list[str] = []
    for value in (explicit, os.environ.get(ENV_TESSERACT), shutil.which("tesseract")):
        if value:
            candidates.append(str(value))
    candidates += [str(_expand(p)) for p in TESSERACT_SEARCH_PATHS]

    for candidate in candidates:
        path = Path(candidate)
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


def tesseract_version(cmd: str) -> tuple[str | None, str | None]:
    """Return ``(version, error)`` for a tesseract binary."""
    try:
        result = _run([cmd, "--version"], timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"cannot execute {cmd}: {exc}"

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        return None, message[0] if message else f"exit code {result.returncode}"

    first = (result.stdout or result.stderr or "").strip().splitlines()
    if not first:
        return None, "no version output"
    return first[0].strip(), None


def tesseract_languages(cmd: str, tessdata: Path | None = None) -> tuple[list[str], str | None]:
    """List languages available to *cmd* (optionally for a specific tessdata)."""
    args = [cmd]
    if tessdata is not None:
        args += ["--tessdata-dir", str(tessdata)]
    args.append("--list-langs")
    try:
        result = _run(args, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"{exc}"

    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if result.returncode != 0 and "List of available languages" not in output:
        first = output.strip().splitlines()
        return [], first[0] if first else f"exit code {result.returncode}"

    langs: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of available languages"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_+-]+", line):
            langs.append(line)
    return sorted(set(langs)), None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def _check_python() -> Check:
    version = platform.python_version()
    impl = platform.python_implementation()
    detail = f"{impl} {version} ({sys.executable})"
    if sys.version_info < (3, 9):
        return Check(
            "Python version",
            "fail",
            f"{detail} — Python 3.9 or newer is required",
            "Install Python 3.12 from https://www.python.org/downloads/macos/",
        )
    return Check("Python version", "ok", detail)


def _check_system_python() -> Check | None:
    """Apple's /usr/bin/python3 ships Tk 8.5.9, which is broken on modern macOS."""
    try:
        resolved = Path(sys.executable).resolve()
    except OSError:
        return None
    if sys.platform != "darwin":
        return None
    system_prefixes = ("/usr/bin/python3", "/Library/Developer/CommandLineTools")
    if not str(resolved).startswith(system_prefixes) and not str(sys.base_prefix).startswith(
        "/Library/Developer/CommandLineTools"
    ):
        return None
    return Check(
        "Python interpreter",
        "warn",
        f"Using Apple's system Python ({resolved}). It bundles Tk 8.5.9, which is "
        "known to crash or open a blank window on macOS 11+ and cannot be fixed by pip.",
        "Use python.org Python 3.12/3.13 or `brew install python python-tk@3.13`, "
        "then recreate the virtualenv.",
    )


def _check_tkinter() -> tuple[Check, str | None]:
    try:
        import tkinter  # noqa: PLC0415 - imported lazily on purpose
    except ImportError:
        return (
            Check(
                "tkinter (GUI toolkit)",
                "fail",
                "tkinter is not installed for this Python, so the app cannot open a "
                "window and exits immediately.",
                "Homebrew Python: `brew install python-tk@3.13` (match your Python "
                "version). python.org Python already includes it — reinstall if "
                "Tk support is missing. Never use /usr/bin/python3: its Tk 8.5.9 "
                "is broken on recent macOS.",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - any import-time failure is fatal here
        return (
            Check(
                "tkinter (GUI toolkit)",
                "fail",
                f"tkinter exists but failed to import: {exc}",
                "Reinstall the Tk bindings for your Python (see README troubleshooting).",
            ),
            None,
        )
    try:
        tk_version = str(tkinter.TkVersion)
    except Exception as exc:  # noqa: BLE001 - any import-time failure is fatal here
        return (
            Check(
                "tkinter (GUI toolkit)",
                "fail",
                f"tkinter exists but failed to import: {exc}",
                "Reinstall the Tk bindings for your Python (see README troubleshooting).",
            ),
            None,
        )
    if tk_version.startswith("8.5"):
        return (
            Check(
                "tkinter (GUI toolkit)",
                "warn",
                f"Tk {tk_version} detected. Tk 8.5 is deprecated on macOS and often "
                "shows a blank window or crashes on launch.",
                "Install Tk 8.6+: python.org Python 3.12+ or "
                "`brew install python-tk@3.13`.",
            ),
            tk_version,
        )
    return Check("tkinter (GUI toolkit)", "ok", f"Tk {tk_version}"), tk_version


def _check_dependencies() -> list[Check]:
    packages = (
        ("PIL", "Pillow", "Pillow"),
        ("pytesseract", "pytesseract", "pytesseract"),
        ("docx", "python-docx", "python-docx"),
        ("fitz", "PyMuPDF", "PyMuPDF"),
    )
    checks: list[Check] = []
    missing: list[str] = []
    detail_bits: list[str] = []
    for module, distribution, pip_name in packages:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(pip_name)
        else:
            try:
                from importlib import metadata

                version = metadata.version(distribution)
            except Exception:  # noqa: BLE001 - version is informational only
                version = "installed"
            detail_bits.append(f"{distribution} {version}")

    if missing:
        python = sys.executable
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        if in_venv:
            fix = f"Run:\n  {python} -m pip install -r requirements.txt"
        else:
            fix = (
                "Run (from the project folder):\n"
                "  python3 -m venv venv && source venv/bin/activate\n"
                "  python -m pip install -r requirements.txt\n"
                f"This Python has no virtualenv: {python}"
            )
        checks.append(
            Check("Python dependencies", "fail", "Missing: " + ", ".join(missing), fix)
        )
    else:
        checks.append(Check("Python dependencies", "ok", ", ".join(detail_bits)))
    return checks


def _check_pdf_backend_lxml() -> Check | None:
    """python-docx needs lxml; a broken lxml surfaces as a confusing ImportError."""
    try:
        import lxml  # noqa: F401,PLC0415 - presence check only

        return None
    except Exception as exc:  # noqa: BLE001
        return Check(
            "lxml (Word document writer)",
            "warn",
            f"lxml could not be imported: {exc}",
            "Run `pip install --force-reinstall lxml`.",
        )


def _check_tesseract(explicit: str | None) -> tuple[Check, Environment]:
    env = Environment()
    cmd = find_tesseract(explicit)

    if cmd is None:
        checked = ", ".join(str(_expand(p)) for p in TESSERACT_SEARCH_PATHS[:3])
        env.tesseract_error = "tesseract binary not found"
        return (
            Check(
                "Tesseract binary",
                "fail",
                "`tesseract` was not found on PATH or in the usual macOS locations "
                f"({checked}). The app can launch but cannot read any text.",
                "Install it: `brew install tesseract tesseract-lang` (Homebrew) or "
                "`sudo port install tesseract tesseract-ben` (MacPorts). Then relaunch "
                "the app. Set BANGLA_OCR_TESSERACT=/full/path/to/tesseract for a "
                "non-standard install.",
            ),
            env,
        )

    env.tesseract_cmd = cmd
    version, error = tesseract_version(cmd)
    if error:
        env.tesseract_error = error
        return (
            Check(
                "Tesseract binary",
                "fail",
                f"Found {cmd} but it will not run: {error}",
                "This is usually a broken/partial install or an architecture mismatch "
                "(e.g. Intel build on Apple Silicon). Reinstall with "
                "`brew reinstall tesseract` and remove the quarantine flag with "
                f"`xattr -d com.apple.quarantine {cmd}`.",
            ),
            env,
        )

    env.tesseract_version = version
    return Check("Tesseract binary", "ok", f"{version} — {cmd}"), env


def _lfs_pointer_checks(directories: list[Path], in_use: Path | None) -> list[Check]:
    """Warn about placeholder ``.traineddata`` files left behind by a bare clone."""
    checks: list[Check] = []
    seen: set[Path] = set()
    for directory in directories:
        try:
            resolved_dir = directory.resolve()
        except OSError:
            continue
        if resolved_dir in seen or not resolved_dir.is_dir():
            continue
        seen.add(resolved_dir)

        placeholders = []
        for code in REQUIRED_LANGS + OPTIONAL_LANGS:
            path = resolved_dir / f"{code}.traineddata"
            if path.exists() and traineddata_status(path)[0] == "lfs-pointer":
                placeholders.append(code)
        if not placeholders:
            continue

        used = in_use is not None and resolved_dir == Path(in_use).resolve()
        checks.append(
            Check(
                f"tessdata in {resolved_dir}",
                "warn",
                f"{', '.join(placeholders)} "
                + (
                    "is a Git LFS pointer file, not a real language model. "
                    if len(placeholders) == 1
                    else "are Git LFS pointer files, not real language models. "
                )
                + (
                    "This directory is in use, so OCR would fail with "
                    '"Error opening data file".'
                    if used
                    else "Another tessdata directory is used instead, but this copy stays broken."
                ),
                "Run `./scripts/fetch_tessdata.sh` (recommended), or "
                "`git lfs install && git lfs pull`, or install language data with "
                "`brew install tesseract-lang` / "
                "`sudo port install tesseract-ben tesseract-eng`.",
            )
        )
    return checks


def _check_tessdata(env: Environment) -> tuple[Check, list[Check]]:
    extra: list[Check] = []
    if env.tesseract_cmd is None:
        return Check("Language data (tessdata)", "warn", "Skipped — Tesseract is missing."), extra

    directory, source = find_tessdata(os.environ.get(ENV_TESSDATA))
    env.tessdata_source = source

    pointer_scan = list(bundled_tessdata_dirs())
    override = os.environ.get(ENV_TESSDATA)
    if override and _expand(override).is_dir():
        pointer_scan.append(_expand(override))
    extra += _lfs_pointer_checks(pointer_scan, directory)

    if directory is None:
        # Fall back to whatever Tesseract itself resolves, and report that.
        langs, error = tesseract_languages(env.tesseract_cmd)
        env.languages = langs
        if error:
            return (
                Check("Language data (tessdata)", "fail", f"Could not list languages: {error}"),
                extra,
            )
        if "ben" in langs:
            return (
                Check(
                    "Language data (tessdata)",
                    "ok",
                    f"Using Tesseract's built-in data directory — languages: {', '.join(langs)}",
                ),
                extra,
            )
        return (
            Check(
                "Language data (tessdata)",
                "fail",
                f"Bangla (ben) language data not found. Tesseract reports: "
                f"{', '.join(langs) or 'no languages'}",
                "Homebrew: `brew install tesseract-lang`. MacPorts installs each "
                "language separately: `sudo port install tesseract-ben tesseract-eng`. "
                "Or run `./scripts/fetch_tessdata.sh` to download "
                "ben/eng/osd into the bundled tessdata folder.",
            ),
            extra,
        )

    env.tessdata_dir = directory
    details: list[str] = []

    if source == "combined":
        _combined, origins = merge_tessdata(tessdata_candidates())
        if origins:
            extra.append(
                Check(
                    "Combined language data",
                    "ok",
                    "Languages are spread over several directories, so they are "
                    "merged into a temporary tessdata folder: "
                    + ", ".join(f"{code} from {path.parent}" for code, path in sorted(origins.items())),
                    "To simplify this, run `./scripts/fetch_tessdata.sh` so the app "
                    "carries ben/eng/osd itself.",
                )
            )

    # LFS placeholders are the #1 cause of "Error opening data file" from a
    # fresh clone; they were reported above by _lfs_pointer_checks().
    for code in REQUIRED_LANGS + OPTIONAL_LANGS:
        path = directory / f"{code}.traineddata"
        state, human = traineddata_status(path)
        if state == "ok":
            details.append(f"{code}: {human}")
        elif path.exists() and state != "lfs-pointer":
            details.append(f"{code}: {human}")

    langs, error = tesseract_languages(env.tesseract_cmd, directory)
    if error:
        extra.append(
            Check(
                "Tesseract language probe",
                "warn",
                f"`tesseract --list-langs` failed for {directory}: {error}",
            )
        )
    env.languages = langs or [
        code for code in REQUIRED_LANGS if traineddata_status(directory / f"{code}.traineddata")[0] == "ok"
    ]

    missing = [code for code in REQUIRED_LANGS if code not in env.languages]
    if "ben" in missing:
        return (
            Check(
                "Language data (tessdata)",
                "fail",
                f"Bangla data missing or unusable in {directory} "
                f"(reports: {', '.join(env.languages) or 'none'})",
                "Run `./scripts/fetch_tessdata.sh` (downloads ben/eng/osd from the "
                "official tesseract-ocr repository), or install the language packs: "
                "`brew install tesseract-lang` / "
                "`sudo port install tesseract-ben tesseract-eng`.",
            ),
            extra,
        )

    status = "warn" if "eng" in missing else "ok"
    note = f" ({source}) — {directory}"
    if missing:
        note += f"; missing: {', '.join(missing)}"
        extra.append(
            Check(
                "English language data",
                "warn",
                "Tesseract can only see Bangla right now, so English words inside "
                "your documents would be dropped.",
                "MacPorts: `sudo port install tesseract-eng` · Homebrew: "
                "`brew install tesseract-lang` · or run `./scripts/fetch_tessdata.sh`.",
            )
        )
    return Check("Language data (tessdata)", status, "Languages: " + ", ".join(env.languages) + note), extra


def _check_writable() -> Check:
    target = Path.home() / "Library" / "Logs" / APP_NAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "Log directory",
            "warn",
            f"Cannot write to {target}: {exc}",
            "Logging will fall back to the system temp folder.",
        )
    return Check("Log directory", "ok", str(target))


def _check_output_permissions() -> Check:
    desktop = Path.home() / "Desktop"
    probe_dir = desktop if desktop.is_dir() else Path(tempfile.gettempdir())
    try:
        with tempfile.NamedTemporaryFile(dir=probe_dir, prefix=".ocr-write-", suffix=".tmp"):
            pass
    except OSError as exc:
        return Check(
            "Output folder access",
            "warn",
            f"Cannot write to {probe_dir}: {exc}",
            "macOS privacy protection may be blocking access to Desktop/Documents. "
            "Allow the app under System Settings > Privacy & Security > Files and "
            "Folders, or save your files elsewhere.",
        )
    return Check("Output folder access", "ok", f"{probe_dir} is writable")


def detect_environment(
    tesseract: str | None = None, include_gui_checks: bool = True
) -> Environment:
    """Probe the machine and build a fully populated :class:`Environment`."""
    checks: list[Check] = []

    system_check = _check_system_python()
    if system_check is not None:
        checks.append(system_check)
    checks.append(_check_python())

    if include_gui_checks:
        tk_check, _ = _check_tkinter()
        checks.append(tk_check)

    dep_checks = _check_dependencies()
    checks += dep_checks
    lxml_check = _check_pdf_backend_lxml()
    if lxml_check:
        checks.append(lxml_check)

    tesseract_check, env = _check_tesseract(tesseract)
    checks.append(tesseract_check)

    tessdata_check, tessdata_extras = _check_tessdata(env)
    checks.append(tessdata_check)
    checks += tessdata_extras

    if env.ok:
        import_ok = all(check.status != "fail" for check in dep_checks)
        if import_ok:
            try:
                import pytesseract  # noqa: PLC0415

                pytesseract.pytesseract.tesseract_cmd = env.tesseract_cmd
            except Exception:  # noqa: BLE001 - reported by dependency checks
                pass

    checks.append(_check_writable())
    checks.append(_check_output_permissions())
    if sys.platform != "darwin":
        checks.insert(
            0,
            Check(
                "Operating system",
                "warn",
                f"{platform.platform()} — this app targets macOS; Tesseract is still used "
                "if present.",
            ),
        )

    env.checks = checks
    LOG.info("Environment: tesseract=%s data=%s langs=%s", env.tesseract_cmd, env.tessdata_dir, env.languages)
    return env


def format_report(env: Environment, include_next_steps: bool = True) -> str:
    """Plain-text diagnostics report (used by ``--diagnose`` and the GUI)."""
    lines = [
        f"{APP_NAME} {APP_VERSION} — diagnostics",
        f"macOS {platform.mac_ver()[0] or 'n/a'} · {platform.machine()} · Python {platform.python_version()}",
        f"frozen bundle: {'yes' if getattr(sys, 'frozen', False) else 'no'}"
        + (f" · _MEIPASS: {getattr(sys, '_MEIPASS', '')}" if getattr(sys, "frozen", False) else ""),
        "",
    ]
    for check in env.checks:
        lines.append(f"[{check.icon}] {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"       fix: {check.fix.replace(chr(10), chr(10) + '            ')}")
    failures = [c for c in env.checks if c.status == "fail"]
    warnings = [c for c in env.checks if c.status == "warn"]
    lines.append("")
    if failures:
        lines.append(f"RESULT: {len(failures)} blocking problem(s), {len(warnings)} warning(s).")
    elif warnings:
        lines.append(f"RESULT: ready to run, with {len(warnings)} warning(s).")
    else:
        lines.append("RESULT: everything looks good — Bangla + English OCR should work.")

    if include_next_steps:
        fixes: list[str] = []
        for check in env.checks:
            if check.status in ("fail", "warn") and check.fix and check.fix not in fixes:
                fixes.append(check.fix)
        if fixes:
            lines.append("")
            lines.append("Next steps (in order):")
            for position, fix in enumerate(fixes[:6], start=1):
                lines.append(f"  {position}. {fix.replace(chr(10), chr(10) + '     ')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OCR pipeline
# ---------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    text = text.replace("\x0c", "\n").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pymupdf():
    """Import PyMuPDF under its modern name, falling back to the legacy shim.

    PyMuPDF >= 1.24 exposes ``pymupdf``; ``fitz`` still works but warns on 2.x.
    """
    try:
        import pymupdf  # noqa: PLC0415

        return pymupdf
    except ImportError:  # pragma: no cover - very old PyMuPDF
        import fitz  # noqa: PLC0415

        return fitz


def _render_pdf_page(page, target_dpi: int = 300, max_edge: int = 4500):
    """Rasterise a PyMuPDF page into a Pillow image at a sane resolution."""
    fitz = pymupdf()  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    scale = target_dpi / 72.0
    longest = max(page.rect.width, page.rect.height) * scale
    if longest > max_edge:
        scale *= max_edge / longest
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(pix.n, "RGB")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _prepare_image(image):
    """Normalise an image for OCR (orientation, colour mode, small-image upscale)."""
    from PIL import Image, ImageOps  # noqa: PLC0415

    try:
        image = ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001 - exotic formats without EXIF
        pass
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    width, height = image.size
    if width == 0 or height == 0:
        raise NoTextFound("The selected image has no pixels.")
    # Bangla glyph clusters need more pixels than Latin text; upscale small inputs.
    if max(width, height) < 1000:
        factor = min(3.0, 1200 / max(width, height))
        if factor > 1.05:
            image = image.resize(
                (max(1, int(width * factor)), max(1, int(height * factor))),
                Image.LANCZOS,
            )
    return image


def _ocr_image(image, env: Environment) -> str:
    if not env.ok:
        raise OCRError("Tesseract or Bangla language data is unavailable — run diagnostics.")
    import pytesseract  # noqa: PLC0415

    pytesseract.pytesseract.tesseract_cmd = env.tesseract_cmd
    try:
        text = pytesseract.image_to_string(
            _prepare_image(image),
            lang=env.lang_string,
            config=env.tesseract_config(),
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRError(f"Tesseract executable not found: {exc}") from exc
    except pytesseract.TesseractError as exc:
        message = str(exc)
        if "Error opening data file" in message or "Failed loading language" in message:
            raise OCRError(
                "Tesseract could not load the language data in "
                f"{env.tessdata_dir}.\n\nThe bundled .traineddata files are probably Git LFS "
                "pointers. Run scripts/fetch_tessdata.sh (or `git lfs pull`), then retry."
            ) from exc
        raise OCRError(f"Tesseract failed: {message}") from exc
    return _clean_text(text)


def _add_page(document, text: str, first: bool) -> None:
    if not first:
        document.add_page_break()
    for block in re.split(r"\n\s*\n", text):
        document.add_paragraph(block)


def process_file(
    source: str | Path,
    env: Environment,
    on_progress=None,
    cancel_event: threading.Event | None = None,
    prefer_embedded_text: bool = True,
) -> ProcessResult:
    """OCR *source* (PDF or image) and write ``<name>_extracted.docx`` next to it.

    Args:
        on_progress: ``callable(fraction: float, message: str)``, invoked from the
            calling thread.  It must not touch Tk widgets directly.
        cancel_event: set it to abort between pages/frames.
        prefer_embedded_text: use a PDF page's own selectable text when it has
            any, instead of rasterising and OCR-ing it (much faster).
    """
    from docx import Document  # noqa: PLC0415

    source = Path(source)
    cancel = cancel_event or threading.Event()

    if not source.is_file():
        raise OCRError(f"File not found: {source}")

    suffix = source.suffix.lower()
    output_path = source.with_name(f"{source.stem}_extracted.docx")

    if suffix != ".pdf" and suffix not in SUPPORTED_IMAGE_SUFFIXES:
        raise UnsupportedFormat(
            f"Unsupported file type '{suffix or source.name}'. "
            "Supported: PDF, PNG, JPG/JPEG, BMP, TIFF, WEBP."
        )

    document = Document()
    result = ProcessResult(source=source, output=output_path)
    texts: list[str] = []

    def progress(fraction: float, message: str) -> None:
        if on_progress:
            try:
                on_progress(max(0.0, min(1.0, fraction)), message)
            except Exception:  # noqa: BLE001 - a broken callback must not kill OCR
                LOG.exception("progress callback failed")

    def check_cancel() -> None:
        if cancel.is_set():
            raise Cancelled("Processing cancelled.")

    if suffix == ".pdf":
        doc = pymupdf().open(source)
        try:
            total = doc.page_count
            if total == 0:
                raise OCRError("The selected PDF contains no pages.")
            LOG.info("PDF %s: %d page(s)", source.name, total)
            for index in range(total):
                check_cancel()
                progress(index / total, f"Reading page {index + 1} of {total}…")
                page = doc.load_page(index)

                text = ""
                if prefer_embedded_text:
                    try:
                        embedded = _clean_text(page.get_text("text") or "")
                    except Exception as exc:  # noqa: BLE001 - damaged text layer
                        LOG.warning("page %d text extraction failed: %s", index + 1, exc)
                        embedded = ""
                    if len(embedded) >= MIN_EMBEDDED_CHARS:
                        text = embedded
                        result.embedded_pages += 1

                if not text:
                    progress(
                        (index + 0.35) / total,
                        f"OCR page {index + 1} of {total} (Bangla + English)…",
                    )
                    text = _ocr_image(_render_pdf_page(page), env)
                    result.ocr_pages += 1

                texts.append(text)
                progress((index + 1) / total, f"Page {index + 1} of {total} done")
        finally:
            doc.close()
        result.pages = total

    else:
        if not env.ok:
            raise OCRError(
                "Tesseract or Bangla language data is unavailable — run diagnostics "
                "(menu: Tools ▸ Diagnostics)."
            )
        import PIL.ImageSequence as ImageSequence  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        with Image.open(source) as image:
            frames = getattr(image, "n_frames", 1)
            LOG.info("Image %s: %d frame(s) %s", source.name, frames, image.size)
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                check_cancel()
                progress(
                    index / max(frames, 1),
                    f"Running OCR on frame {index + 1} of {frames}…"
                    if frames > 1
                    else "Running OCR on image (Bangla + English)…",
                )
                texts.append(_ocr_image(frame, env))
                result.ocr_pages += 1
            result.pages = max(frames, 1)
        progress(1.0, "OCR complete")

    body = [text for text in texts if text.strip()]
    if not body:
        raise NoTextFound(
            "OCR finished but no readable Bangla or English text was detected.\n\n"
            "Try a higher-resolution scan (300 dpi or better) and make sure the page "
            "is not rotated. Diagnostics can confirm the language data is installed."
        )

    for position, text in enumerate(body):
        _add_page(document, text, first=position == 0)
    result.characters = sum(len(text) for text in body)

    try:
        document.save(output_path)
    except OSError as exc:
        raise OCRError(
            f"Could not write {output_path}:\n{exc}\n\n"
            "Check that the folder is writable and that the file is not open in Word."
        ) from exc

    LOG.info("Wrote %s (%s)", output_path, result.summary())
    return result
