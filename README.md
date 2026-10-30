# Bangla PDF & Image OCR App (macOS)

A native macOS desktop app that extracts **Bangla + English** text from PDFs and images
with Tesseract, page by page, and saves a Word document (`<name>_extracted.docx`) next to
your input file.

![status](https://img.shields.io/badge/platform-macOS-blue) ![status](https://img.shields.io/badge/python-3.9%2B-blue)

---

## Why it would not launch — and what was fixed

The previous version died *before* drawing a window, which on macOS looks like "nothing
happens". Each of these was reproduced and fixed:

| # | Problem in the old code | What the user saw | Fix |
|---|---|---|---|
| 1 | `ModuleNotFoundError: No module named 'tkinter'` — Homebrew Python ships **without** Tk unless `python-tk` is installed | App exits instantly, no window, no message (a `--windowed` `.app` has no console) | The app now catches this, explains it in a native dialog, and tells you the exact `brew install python-tk@…` command |
| 2 | `self.root.eval('tk::PlaceWindow . center')` — an **undocumented internal** Tcl proc (`library/tk.tcl`), not part of tkinter; it is missing in some Tk builds/versions | Startup dies with `TclError: invalid command name "tk::PlaceWindow"` before the window appears | Removed; the window is sized and raised with plain tkinter calls only |
| 3 | Hard-coded `pytesseract.pytesseract.tesseract_cmd = '/opt/local/bin/tesseract'` (MacPorts only) | GUI opens, then every job fails | Auto-discovers Tesseract: `PATH`, Homebrew (`/opt/homebrew`, `/usr/local`), MacPorts, `~/bin`, or `BANGLA_OCR_TESSERACT` |
| 4 | `tessdata/*.traineddata` are **Git LFS pointers** (132 bytes) in a plain clone | `Error opening data file …/ben.traineddata` | Diagnostics detect pointer files; `scripts/fetch_tessdata.sh` downloads real models; system language packs are used as a fallback |
| 5 | Widgets were updated from a worker thread (`label.config`, `progressbar`, `messagebox`) — tkinter is **not** thread-safe | Freezes, beach-balling, or a hard crash on the first page | Worker thread only posts to a `queue.Queue`; the main thread pumps it with `root.after()` |
| 6 | Any exception inside the GUI was swallowed (no console in `.app`) | Silent failure | Everything is logged to `~/Library/Logs/Bangla OCR/ocr-bangla.log`, unexpected errors pop a dialog, and `--diagnose` prints a full report |
| 7 | PyInstaller command put `tessdata` where the app never looked (PyInstaller 6 uses `Contents/Frameworks`) | Bundled `.app` could not find its data | Data lookup checks all bundle layouts; `scripts/build_app.sh` verifies the payload after building |
| 8 | Window opened **behind** Terminal | Looks like the app did not start | Window is raised and the process is activated on launch |

---

## Quick start (one command)

```bash
git clone https://github.com/upalkmc69-ops/OCR-Bangla-MacOS.git
cd OCR-Bangla-MacOS
./scripts/setup_macos.sh
```

It verifies that your `python3` can open windows, creates `./venv`, installs the
dependencies, checks Tesseract + Bangla/English language data, downloads the models if
they are missing, and finishes with the diagnostics report. Re-run it any time.

Then: `source venv/bin/activate && python3 src/ocr_bangla.py`

---

## 1. Install prerequisites (manual)

### Tesseract (required)

```bash
# MacPorts (every language is a separate port)
sudo port selfupdate
sudo port install tesseract tesseract-ben tesseract-eng

# or Homebrew
brew install tesseract tesseract-lang
```

> **No Homebrew? You do not need it.** Either use MacPorts, or skip package managers
> entirely and run `./scripts/fetch_tessdata.sh`, which downloads the language models
> into `tessdata/` (it only needs the `curl` that ships with macOS). The app's
> diagnostics also adapt: on a MacPorts-only Mac it never suggests `brew`.

> **MacPorts gotchas:**
> * `sudo port install tesseract` alone ships **no language data** — every language is
>   its own port. Install at least `tesseract-ben` *and* `tesseract-eng`, otherwise OCR
>   reads Bangla but silently drops English words.
> * An outdated port tree can fail or offer an old Tesseract; run `sudo port selfupdate`
>   first. If a port still will not install, use `./scripts/fetch_tessdata.sh` instead —
>   it does not touch MacPorts.

Verify: `tesseract --version` and `tesseract --list-langs` (must include `ben`).

### A Python that can open windows (required)

tkinter is **not** a pip package and Homebrew Python does not include it.

```bash
brew install python python-tk@3.13   # match your `python3 --version`
```

*Recommended:* the [python.org installer](https://www.python.org/downloads/macos/)
already bundles Tk 8.6 — nothing extra to install.

> **Do not use `/usr/bin/python3`.** Apple's system Python ships Tk 8.5.9, which is known
> to open a blank window or crash on macOS 11+. The app now warns about this.

### Python packages

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

---

## 2. Run it

```bash
source venv/bin/activate
python3 src/ocr_bangla.py
```

Always test the launch path first if something seems wrong:

```bash
python3 src/ocr_bangla.py --diagnose
```

That prints (and the in-app **Tools ▸ Diagnostics** window shows) every check:
Tk version, Tesseract binary, language data, permissions, and the exact fix for
whatever is broken. Exit code `0` = ready, `1` = something would block OCR.

### Language data

`tessdata/` is stored with Git LFS. If your clone contains 132-byte pointer files
(`head -c 40 tessdata/ben.traineddata` shows `version https://git-lfs…`), do **one** of:

```bash
git lfs install && git lfs pull        # if Git LFS is installed
./scripts/fetch_tessdata.sh            # download ben/eng/osd (recommended)
./scripts/fetch_tessdata.sh --system   # copy from the installed Tesseract
```

System-wide language data (the MacPorts `tesseract-ben` / `tesseract-eng` ports, or
`brew install tesseract-lang`) also works — the app falls back to it.

**Languages may live in different folders.** Tesseract only accepts one data directory,
so when `ben` comes from MacPorts and `eng` comes from the bundle (or vice versa), the app
merges the available models into a small temporary folder and reports it as
`(combined)` in diagnostics. That is normal and needs no action, but running
`./scripts/fetch_tessdata.sh` keeps everything in one place.

Language data is checked for three failure modes: a missing file, a truncated file, and a
Git LFS pointer (a 132-byte text stub) — the last one is what a normal `git clone`
produces here.

---

## 3. Building the standalone `.app`

```bash
python -m pip install -r requirements-dev.txt
./scripts/build_app.sh
open "dist/Bangla OCR.app"
```

The script refuses to package placeholder language data, bundles `tessdata` and the
docx/PyMuPDF backends correctly, ad-hoc signs the bundle, and smoke-tests the frozen
binary. Move the `.app` to `/Applications` freely — its language data travels with it.

> The app still needs the **Tesseract binary** on the machine (it is a native program with
> many shared libraries, so it is not embedded). `brew install tesseract tesseract-lang`.

---

## Output

| Input | Result |
|---|---|
| `report.pdf` (4 pages) | `report_extracted.docx` — one page break per source page |
| `scan.png` / `scan.tiff` | `scan_extracted.docx` (multi-page TIFFs: one page per frame) |

* Files are saved **next to the input file**.
* PDF pages that already contain selectable text are copied directly (much faster); uncheck
  the option in the window to force OCR on every page.
* Pages are rendered at 300 dpi and small images are upscaled — Bangla glyph clusters need
  more pixels than Latin text.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Double-clicking the app does nothing | Run `python3 src/ocr_bangla.py --diagnose` (or `"dist/Bangla OCR.app/Contents/MacOS/Bangla OCR" --diagnose`) to see the reason; check the log |
| "App is damaged / from an unidentified developer" | Right-click ▸ **Open**, or `xattr -dr com.apple.quarantine "dist/Bangla OCR.app"`. Locally built apps are ad-hoc signed only |
| `No module named '_tkinter'` | `brew install python-tk@<version>` (match your Python), then recreate the venv |
| Blank window / instant crash after launch | You are on Tk 8.5 (`/usr/bin/python3`) — install python.org Python 3.12+ or `brew install python-tk` |
| `Error opening data file … ben.traineddata` | Language data missing/placeholder → run `./scripts/fetch_tessdata.sh` |
| Works in Terminal, fails from Finder | Finder apps get `PATH=/usr/bin:/bin:/usr/sbin:/sbin`; the app searches Homebrew/MacPorts paths itself, or set `BANGLA_OCR_TESSERACT` |
| OCR output is empty | Scan at 300 dpi+, avoid rotated pages; confirm `ben` in `tesseract --list-langs` |
| Only Bangla recognised, English words dropped | English data missing. MacPorts: `sudo port install tesseract-eng`; Homebrew: `brew install tesseract-lang`; or `./scripts/fetch_tessdata.sh` |
| `port install tesseract-eng` fails on an old MacPorts | `sudo port selfupdate` first. If that is not possible, skip MacPorts: `./scripts/fetch_tessdata.sh` puts the models in `tessdata/` and the app prefers that complete folder |
| "Missing: Pillow, pytesseract, …" | Dependencies are not installed for *that* Python: `python3 -m pip install -r requirements.txt` inside the virtualenv the app runs in |
| "Operation not permitted" writing output | macOS privacy prompt — allow the app under System Settings ▸ Privacy & Security ▸ Files and Folders |

Logs: `~/Library/Logs/Bangla OCR/ocr-bangla.log` (Tools ▸ Reveal Log File).

### Environment overrides

| Variable | Purpose |
|---|---|
| `BANGLA_OCR_TESSERACT` | Full path to the `tesseract` executable |
| `BANGLA_OCR_TESSDATA` | Full path to a `tessdata` directory |
| `TESSDATA_PREFIX` | Honoured as a fallback, like Tesseract itself |

---

## Development

```bash
python -m unittest discover -s tests -v
```

The suite runs without a display or a real Tesseract install: a fake `tesseract` binary
exercises the whole PDF → raster → OCR → `.docx` pipeline, and a tkinter stub verifies that
no widget is touched off the main thread, that no `tk::PlaceWindow`/`eval` is used, and that
errors surface as dialogs rather than crashes.

### Layout

```text
ocr-bangla/
├── src/
│   ├── ocr_bangla.py      # Tk UI, CLI (--diagnose), logging
│   └── ocr_engine.py      # discovery, diagnostics, OCR pipeline (no tkinter)
├── tests/                 # unittest suite + fake tesseract + tkinter stub
├── scripts/
│   ├── setup_macos.sh     # one-shot: venv + deps + tesseract + language data
│   ├── build_app.sh       # verified PyInstaller .app build
│   └── fetch_tessdata.sh  # download real ben/eng/osd models
├── tessdata/              # language models (Git LFS)
├── requirements.txt
└── requirements-dev.txt
```
