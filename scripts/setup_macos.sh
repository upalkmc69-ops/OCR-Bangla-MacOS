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

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: this Python cannot open windows — tkinter/Tk is missing or broken.

  Homebrew Python:    brew install python python-tk@3.13
  python.org Python:  https://www.python.org/downloads/macos/   (includes Tk 8.6)

  Avoid /usr/bin/python3: Apple's Tk 8.5.9 crashes on recent macOS.
EOF
    exit 1
fi
echo "    tkinter/Tk: OK"

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
    for candidate in /opt/homebrew/bin/tesseract /usr/local/bin/tesseract /opt/local/bin/tesseract; do
        [[ -x "$candidate" ]] && break
    done
    if [[ -x "${candidate:-}" ]]; then
        echo "    found $candidate (not on PATH, the app searches there anyway)"
    else
        cat >&2 <<'EOF'
    warning: Tesseract is not installed. The app will launch but cannot read text.

      Homebrew:  brew install tesseract tesseract-lang
      MacPorts:  sudo port install tesseract tesseract-ben tesseract-eng
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
missing = [c for c in ocr_engine.REQUIRED_LANGS if c not in env.languages]
print("missing:" + ",".join(missing) if missing else "ok")
PY
}

STATE="$(language_state || echo "unknown")"
if [[ "$STATE" == "ok" ]]; then
    echo "    Bangla + English language data: OK"
else
    warn "language data incomplete ($STATE) — downloading ben/eng/osd into ./tessdata"
    if ./scripts/fetch_tessdata.sh; then
        STATE="$(language_state || echo "unknown")"
    fi
    if [[ "$STATE" != "ok" ]]; then
        cat >&2 <<'EOF'
    warning: language data is still incomplete. Install it manually:

      MacPorts:  sudo port install tesseract-ben tesseract-eng
      Homebrew:  brew install tesseract-lang
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
