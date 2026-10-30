#!/usr/bin/env bash
#
# One-shot macOS setup for the Bangla OCR app.
#
#   ./scripts/setup_macos.sh
#
# It will:
#   1. verify that your `python3` can open windows (tkinter/Tk),
#   2. create ./venv and install the Python dependencies,
#   3. check Tesseract + Bangla/English language data and fix what it can,
#   4. print the final diagnostics report and tell you how to launch.
#
# Works with MacPorts, Homebrew, or neither — the language models can be
# downloaded straight into ./tessdata without any package manager.
#
# Safe to re-run: existing parts are reused.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
VENV="$REPO_ROOT/venv"
VPY="$VENV/bin/python"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

have_port() { [[ -x /opt/local/bin/port || -x /opt/pkg/bin/port ]]; }
have_brew() { [[ -x /opt/homebrew/bin/brew || -x /usr/local/bin/brew ]]; }

# Package-manager hints, shown only for managers that are actually installed.
pm_tesseract_hint() {
    if have_port; then
        echo "      MacPorts:  sudo port selfupdate && sudo port install tesseract tesseract-ben tesseract-eng"
    fi
    if have_brew; then
        echo "      Homebrew:  brew install tesseract tesseract-lang"
    fi
    if ! have_port && ! have_brew; then
        echo "      MacPorts:  sudo port install tesseract tesseract-ben tesseract-eng"
        echo "         or:     brew install tesseract tesseract-lang"
    fi
}

pm_language_hint() {
    if have_port; then
        echo "      MacPorts:  sudo port selfupdate && sudo port install tesseract-ben tesseract-eng"
    fi
    if have_brew; then
        echo "      Homebrew:  brew install tesseract-lang"
    fi
    if ! have_port && ! have_brew; then
        echo "      MacPorts:  sudo port install tesseract-ben tesseract-eng"
        echo "         or:     brew install tesseract-lang"
    fi
    echo "      or run:    ./scripts/fetch_tessdata.sh   (no package manager needed)"
}

pm_python_hint() {
    if have_port; then
        echo "      MacPorts:  sudo port install $PYTAG-tkinter"
    fi
    if have_brew; then
        echo "      Homebrew:  brew install python-tk@$PYVER"
    fi
}

usage() {
    cat <<'EOF'
One-shot macOS setup for the Bangla OCR app.

Usage:
  ./scripts/setup_macos.sh

Environment overrides:
  PYTHON=/path/to/python3   interpreter used to create the venv

Steps: check tkinter -> create ./venv -> install requirements
       -> check Tesseract + ben/eng language data -> print diagnostics.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
    warn "This script is written for macOS ($(uname -s) detected); continuing anyway."
fi

# ---------------------------------------------------------------------------
say "1/4  Checking $PYTHON"
# ---------------------------------------------------------------------------
command -v "$PYTHON" >/dev/null 2>&1 || die "error: '$PYTHON' not found. Install Python 3.12+ from https://www.python.org/downloads/macos/"

"$PYTHON" - <<'PY' || die "error: Python 3.9+ is required."
import sys
if sys.version_info < (3, 9):
    sys.exit(1)
print(f"    Python {sys.version.split()[0]} at {sys.executable}")
PY

PYVER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PYTAG="$("$PYTHON" -c 'import sys; print(f"py{sys.version_info.major}{sys.version_info.minor}")')"

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    cat >&2 <<EOF
error: this Python cannot open windows — tkinter/Tk is missing or broken.

      python.org Python:  https://www.python.org/downloads/macos/   (includes Tk 8.6)
$(pm_python_hint)

      Avoid /usr/bin/python3: Apple's Tk 8.5.9 crashes on recent macOS.
EOF
    exit 1
fi
echo "    tkinter/Tk: OK"
if have_port; then
    echo "    package manager: MacPorts ($( [[ -x /opt/local/bin/port ]] && echo /opt/local || echo /opt/pkg ))"
elif have_brew; then
    echo "    package manager: Homebrew ($( [[ -x /opt/homebrew/bin/brew ]] && echo /opt/homebrew || echo /usr/local ))"
else
    echo "    package manager: none — language models will be downloaded into ./tessdata"
fi

# ---------------------------------------------------------------------------
say "2/4  Python environment"
# ---------------------------------------------------------------------------
if [[ ! -x "$VPY" ]]; then
    echo "    creating $VENV"
    "$PYTHON" -m venv "$VENV"
else
    echo "    reusing $VENV"
fi

"$VPY" -m pip install --quiet --upgrade pip
echo "    installing requirements.txt"
"$VPY" -m pip install --quiet -r requirements.txt
echo "    dependencies installed"

# ---------------------------------------------------------------------------
say "3/4  Tesseract and language data"
# ---------------------------------------------------------------------------
if command -v tesseract >/dev/null 2>&1; then
    echo "    $("tesseract" --version 2>&1 | head -n 1) — $(command -v tesseract)"
else
    found=""
    for candidate in /opt/local/bin/tesseract /opt/homebrew/bin/tesseract /usr/local/bin/tesseract; do
        [[ -x "$candidate" ]] && found="$candidate" && break
    done
    if [[ -n "$found" ]]; then
        echo "    found $found (not on PATH; the app searches there anyway)"
    else
        cat >&2 <<EOF
    warning: Tesseract is not installed. The app will launch but cannot read text.
$(pm_tesseract_hint)
EOF
    fi
fi

language_state() {
    "$VPY" - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
import ocr_engine

env = ocr_engine.detect_environment(include_gui_checks=False)
missing = [code for code in ocr_engine.REQUIRED_LANGS if code not in env.languages]
print("missing:" + ",".join(missing) if missing else "ok")
PY
}

STATE="$(language_state || echo "unknown")"
if [[ "$STATE" == "ok" ]]; then
    echo "    Bangla + English language data: OK"
else
    warn "language data incomplete ($STATE) — downloading ben/eng/osd into ./tessdata"
    ./scripts/fetch_tessdata.sh || true
    STATE="$(language_state || echo "unknown")"
    if [[ "$STATE" != "ok" ]]; then
        cat >&2 <<EOF
    warning: language data is still incomplete. Install it with either:
$(pm_language_hint)
EOF
    fi
fi

# ---------------------------------------------------------------------------
say "4/4  Diagnostics"
# ---------------------------------------------------------------------------
set +e
"$VPY" src/ocr_bangla.py --diagnose
STATUS=$?
set -e

echo ""
if [[ $STATUS -eq 0 ]]; then
    cat <<EOF
Setup complete. Launch the app with:

  source venv/bin/activate
  python3 src/ocr_bangla.py

Or build the double-clickable bundle:

  ./scripts/build_app.sh && open "dist/Bangla OCR.app"
EOF
else
    cat >&2 <<EOF
Setup finished with blocking problems (see the report above).

  Re-run this script after fixing them, or read:
  https://github.com/upalkmc69-ops/OCR-Bangla-MacOS#troubleshooting
EOF
fi
exit $STATUS
