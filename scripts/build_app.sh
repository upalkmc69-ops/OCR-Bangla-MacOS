#!/usr/bin/env bash
#
# Build "dist/Bangla OCR.app" with PyInstaller.
#
# The previous README command (`pyinstaller --onedir --windowed
# --add-data "tessdata:tessdata" src/ocr_bangla.py`) produces a bundle that
# starts and then fails to find its language data, and it gave the app no
# bundle identifier. This script:
#
#   1. refuses to build with placeholder (Git LFS pointer) language data,
#   2. bundles tessdata + docx templates and the PyMuPDF backend explicitly,
#   3. verifies the data really landed inside the .app,
#   4. ad-hoc signs the bundle so macOS does not block it,
#   5. smoke-tests the frozen binary with --diagnose.
#
# Usage:
#   ./scripts/build_app.sh              # build
#   ./scripts/build_app.sh --skip-check # build even if language data is missing
#
set -euo pipefail

usage() {
    cat <<'EOF'
Build "dist/Bangla OCR.app" with PyInstaller.

Unlike the old README one-liner, this build:
  1. refuses to package placeholder (Git LFS pointer) language data,
  2. bundles tessdata, docx templates and the PyMuPDF backend explicitly,
  3. verifies the language data really landed inside the .app,
  4. ad-hoc signs the bundle so macOS does not block it,
  5. smoke-tests the frozen binary with --diagnose.

Usage:
  ./scripts/build_app.sh              # build
  ./scripts/build_app.sh --skip-check # build even if language data is missing
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP_NAME="Bangla OCR"
BUNDLE_ID="org.banglaocr.desktop"
PYTHON="${PYTHON:-python3}"
SKIP_CHECK=0
DIST="$REPO_ROOT/dist"
BUILD="$REPO_ROOT/build"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-check) SKIP_CHECK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: .app bundles can only be built on macOS." >&2
    exit 1
fi

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "error: PyInstaller is not installed for $PYTHON." >&2
    echo "       $PYTHON -m pip install 'pyinstaller>=6.9'" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Fail early on placeholder language data.
# ---------------------------------------------------------------------------
if [[ $SKIP_CHECK -eq 0 ]]; then
    if ! "$PYTHON" - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
import ocr_engine

env = ocr_engine.detect_environment(include_gui_checks=False)
print(ocr_engine.format_report(env))
blocking = [c for c in env.checks if c.status == "fail" and c.name != "tkinter (GUI toolkit)"]
sys.exit(1 if blocking else 0)
PY
    then
        echo "" >&2
        echo "error: refusing to build a bundle that cannot do OCR." >&2
        echo "       Fix the problems above (usually: ./scripts/fetch_tessdata.sh)," >&2
        echo "       or pass --skip-check to build anyway." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 2. Build.
# ---------------------------------------------------------------------------
rm -rf "$BUILD" "$DIST"

ARGS=(
    --noconfirm
    --clean
    --onedir
    --windowed
    --name "$APP_NAME"
    --osx-bundle-identifier "$BUNDLE_ID"
    --add-data "tessdata:tessdata"
    --collect-data docx
    --hidden-import pymupdf
    --hidden-import fitz
    --hidden-import PIL.ImageSequence
    --exclude-module pytest
    --distpath "$DIST"
    --workpath "$BUILD"
    --specpath "$BUILD"
)

if [[ -f "assets/AppIcon.icns" ]]; then
    ARGS+=(--icon "assets/AppIcon.icns")
fi

echo "==> Building $APP_NAME.app"
"$PYTHON" -m PyInstaller "${ARGS[@]}" src/ocr_bangla.py

APP_BUNDLE="$DIST/$APP_NAME.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
    echo "error: PyInstaller did not produce $APP_BUNDLE" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Verify the payload (PyInstaller 6 puts onedir data in Contents/Frameworks,
#    older versions in Contents/Resources — accept either).
# ---------------------------------------------------------------------------
echo "==> Verifying bundled language data"
FOUND=""
for candidate in \
    "$APP_BUNDLE/Contents/Frameworks/tessdata" \
    "$APP_BUNDLE/Contents/Resources/tessdata" \
    "$APP_BUNDLE/Contents/MacOS/tessdata"
do
    if [[ -f "$candidate/ben.traineddata" ]]; then
        FOUND="$candidate"
        break
    fi
done

if [[ -z "$FOUND" ]]; then
    echo "error: ben.traineddata was not bundled into the .app." >&2
    exit 1
fi

BEN_SIZE=$(( $(stat -f%z "$FOUND/ben.traineddata") ))
if [[ "$BEN_SIZE" -lt 65536 ]]; then
    echo "error: bundled ben.traineddata is only $BEN_SIZE bytes (Git LFS pointer?)." >&2
    echo "       Run ./scripts/fetch_tessdata.sh and rebuild." >&2
    exit 1
fi
echo "    $FOUND ($(du -sh "$FOUND" | cut -f1))"

# ---------------------------------------------------------------------------
# 4. Ad-hoc code signature (avoids "damaged app" prompts on Apple Silicon).
# ---------------------------------------------------------------------------
if command -v codesign >/dev/null 2>&1; then
    echo "==> Ad-hoc signing"
    codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null \
        || echo "    warning: ad-hoc signing failed (the app can still run locally)"
fi

# ---------------------------------------------------------------------------
# 5. Smoke-test the frozen bundle.
# ---------------------------------------------------------------------------
echo "==> Smoke test (--diagnose)"
BIN="$APP_BUNDLE/Contents/MacOS/$APP_NAME"
if [[ -x "$BIN" ]]; then
    if "$BIN" --diagnose | tail -n 4; then
        echo "    frozen binary answers --diagnose"
    else
        echo "    warning: smoke test failed; check build/$APP_NAME/warn-$APP_NAME.txt" >&2
    fi
fi

cat <<EOF

==> Build complete: $APP_BUNDLE

Run it now:
  open "$APP_BUNDLE"

First launch notes:
  • macOS may ask to confirm an app from an unidentified developer:
    right-click the app ▸ Open, or allow it under
    System Settings ▸ Privacy & Security.
  • The app still needs Tesseract installed (brew install tesseract tesseract-lang).
    Move the .app anywhere — including /Applications — without breaking the
    language data, which now travels inside the bundle.
EOF
