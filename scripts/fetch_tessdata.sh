#!/usr/bin/env bash
#
# Download real Tesseract language data (ben / eng / osd) into ./tessdata.
#
# Why this exists: the repository tracks tessdata/*.traineddata with Git LFS.
# A clone made without `git lfs` receives 132-byte pointer files instead of the
# real models, and Tesseract then fails with
#   "Error opening data file .../tessdata/ben.traineddata"
#
# Usage:
#   ./scripts/fetch_tessdata.sh                 # fast models (recommended)
#   ./scripts/fetch_tessdata.sh --repo best     # highest accuracy, slower
#   ./scripts/fetch_tessdata.sh --system        # copy from the installed Tesseract
#   ./scripts/fetch_tessdata.sh --dir PATH      # write somewhere else
#   ./scripts/fetch_tessdata.sh --force         # rewrite files that already look valid
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$REPO_ROOT/tessdata"
REPO="fast"
MODE="download"
FORCE=0
LANGS=(ben eng osd)

MIN_BYTES=65536

usage() {
    cat <<'EOF'
Download real Tesseract language data (ben / eng / osd) into ./tessdata.

Why this exists: the repository tracks tessdata/*.traineddata with Git LFS.
A clone made without `git lfs` receives 132-byte pointer files instead of the
real models, and Tesseract then fails with
  "Error opening data file .../tessdata/ben.traineddata"

Usage:
  ./scripts/fetch_tessdata.sh                 # fast models (recommended)
  ./scripts/fetch_tessdata.sh --repo best     # highest accuracy, slower
  ./scripts/fetch_tessdata.sh --system        # copy from the installed Tesseract
  ./scripts/fetch_tessdata.sh --dir PATH      # write somewhere else
  ./scripts/fetch_tessdata.sh --force         # rewrite files that already look valid
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir) TARGET_DIR="${2:?--dir needs a path}"; shift 2 ;;
        --repo) REPO="${2:?--repo needs fast|best}"; shift 2 ;;
        --system) MODE="system"; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

if [[ "$REPO" != "fast" && "$REPO" != "best" ]]; then
    echo "error: --repo must be 'fast' or 'best'" >&2
    exit 1
fi

pm_language_hint() {
    if [[ -x /opt/local/bin/port || -x /opt/pkg/bin/port ]]; then
        echo "  MacPorts:  sudo port selfupdate && sudo port install tesseract-ben tesseract-eng"
    elif [[ -x /opt/homebrew/bin/brew || -x /usr/local/bin/brew ]]; then
        echo "  Homebrew:  brew install tesseract-lang"
    else
        echo "  MacPorts:  sudo port install tesseract-ben tesseract-eng"
        echo "     or:     brew install tesseract-lang"
    fi
}

file_size() {
    # BSD stat (macOS) first, then GNU stat (Linux/CI)
    stat -f%z "$1" 2>/dev/null || stat -c%s "$1"
}

is_pointer() {
    [[ -f "$1" ]] && head -c 22 "$1" 2>/dev/null | grep -q "version https://git-lfs"
}

is_valid() {
    local file="$1"
    [[ -f "$file" ]] || return 1
    is_pointer "$file" && return 1
    [[ "$(file_size "$file")" -ge "$MIN_BYTES" ]] || return 1
    return 0
}

echo "==> Bangla OCR: Tesseract language data"
echo "    target: $TARGET_DIR"
echo "    source: $MODE ($REPO models)"
mkdir -p "$TARGET_DIR"

# ---------------------------------------------------------------------------
# Option A: copy from an already-installed Tesseract (no network needed).
# ---------------------------------------------------------------------------
if [[ "$MODE" == "system" ]]; then
    SOURCE_DIR=""
    for candidate in \
        "${TESSDATA_PREFIX:-}/tessdata" \
        "${TESSDATA_PREFIX:-}" \
        /opt/homebrew/share/tessdata \
        /usr/local/share/tessdata \
        /opt/local/share/tessdata
    do
        if [[ -n "$candidate" && -d "$candidate" ]] && is_valid "$candidate/ben.traineddata"; then
            SOURCE_DIR="$candidate"
            break
        fi
    done
    if [[ -z "$SOURCE_DIR" ]]; then
        echo "error: no installed Tesseract language data found." >&2
        echo "       Install it first:" >&2
        pm_language_hint >&2
        exit 1
    fi
    for lang in "${LANGS[@]}"; do
        if is_valid "$SOURCE_DIR/$lang.traineddata"; then
            cp -f "$SOURCE_DIR/$lang.traineddata" "$TARGET_DIR/$lang.traineddata"
            echo "    copied $lang from $SOURCE_DIR"
        fi
    done
    echo "==> Done. Language data is in $TARGET_DIR"
    exit 0
fi

# ---------------------------------------------------------------------------
# Option B: download from the official tesseract-ocr repository.
# ---------------------------------------------------------------------------
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    echo "error: neither curl nor wget is available." >&2
    exit 1
fi

download() {
    local url="$1" dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 20 --progress-bar -o "$dest.part" "$url" \
            || { rm -f "$dest.part"; return 1; }
    else
        wget -q --show-progress -O "$dest.part" "$url" \
            || { rm -f "$dest.part"; return 1; }
    fi
    mv -f "$dest.part" "$dest"
}

FAILED=0
for lang in "${LANGS[@]}"; do
    dest="$TARGET_DIR/$lang.traineddata"
    if [[ $FORCE -eq 0 ]] && is_valid "$dest"; then
        echo "    $lang: already present ($(file_size "$dest") bytes) — skipping"
        continue
    fi

    url="https://github.com/tesseract-ocr/tessdata_${REPO}/raw/main/${lang}.traineddata"
    echo "    $lang: downloading from tessdata_${REPO}…"
    if ! download "$url" "$dest"; then
        echo "    $lang: download failed — trying the raw.githubusercontent.com mirror" >&2
        if ! download "https://raw.githubusercontent.com/tesseract-ocr/tessdata_${REPO}/main/${lang}.traineddata" "$dest"; then
            echo "    $lang: FAILED" >&2
            FAILED=1
            continue
        fi
    fi

    if is_valid "$dest"; then
        echo "    $lang: ok ($(file_size "$dest") bytes)"
    else
        echo "    $lang: downloaded file looks invalid (still an LFS pointer?)" >&2
        FAILED=1
    fi
done

if [[ $FAILED -ne 0 ]]; then
    echo "" >&2
    echo "Some languages could not be installed. Alternatives:" >&2
    echo "Install the language data, then re-copy it with --system:" >&2
    pm_language_hint >&2
    exit 1
fi

echo ""
echo "==> Done. Language data is in $TARGET_DIR"
echo "    Verify with: python3 src/ocr_bangla.py --diagnose"
echo ""
echo "    note: tessdata/*.traineddata is tracked by Git LFS, so 'git status'"
echo "          will show these downloads as modified. Do not commit them:"
echo "          git restore tessdata"
