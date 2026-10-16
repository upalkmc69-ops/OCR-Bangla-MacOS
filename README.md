# Bangla PDF to DOC OCR System

A Python-based system that converts Bangla PDF files into editable DOCX documents using Tesseract OCR.

## Overview

This system performs Optical Character Recognition (OCR) on Bangla PDF documents and outputs them as Microsoft Word (.docx) files with preserved text structure.

**Pipeline:**
```
PDF → PyMuPDF renders pages to PNG → Tesseract OCR (Bangla lang) → python-docx
```

## Prerequisites

- Python 3.8 or higher
- ARM64 Linux (Android) environment
- Tesseract OCR with Bangla language data

## Installation

The system bundles a standalone Tesseract binary and libraries, avoiding the need for system package installation.

```bash
# Clone or download this repository
cd /workspace/ocr-bangla

# Python dependencies (install with pip)
pip install -r requirements.txt

# Or install individually:
pip install pytesseract Pillow python-docx PyMuPDF lxml packaging
```

## Usage

### Basic Usage

```bash
cd /workspace/ocr-bangla
python3 src/ocr_bangla.py input_pdf.pdf
```

This creates `input_pdf.docx` in the same directory.

### Advanced Usage

```bash
# Specify output file
python3 src/ocr_bangla.py input.pdf -o output.docx

# Higher DPI for better quality (slower)
python3 src/ocr_bangla.py input.pdf --dpi 400

# Different language (for mixed content)
python3 src/ocr_bangla.py input.pdf --lang ben+eng
```

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output` | Output DOCX file path | Auto-generated from input |
| `--dpi` | Image rendering DPI | 300 |
| `--lang` | OCR language(s) | ben (Bangla) |
| `--psm` | Page segmentation mode | 6 |

## Directory Structure

```
workspace/ocr-bangla/
├── bin/
│   └── tesseract                 # Standalone Tesseract binary (v4.1.1)
├── lib/
│   └── *.so                      # Required shared libraries (libtesseract, leptonica, etc.)
├── tessdata/
│   ├── ben.traineddata           # Bangla language model (~1.8 MB)
│   ├── eng.traineddata           # English language model (~4.1 MB)
│   └── osd.traineddata           # Orientation & Script detection (~10.6 MB)
├── src/
│   └── ocr_bangla.py             # Main OCR script
├── ocr-bangla                    # Launcher script (optional)
├── requirements.txt              # Python dependencies
├── README.md                     # This file
└── pyproject.toml                # Build configuration
```

## How It Works

### PDF to Images
Uses PyMuPDF (fitz) to render each PDF page at 300 DPI to a PNG image. This handles both:
- Scanned PDF document images
- Text-based PDFs (rendered as images for OCR)

### OCR Processing
Tesseract 4.1.1 performs OCR on each rendered image using the Bangla (`ben`) language model. The traineddata is bundled locally to avoid system dependencies.

### DOCX Generation
The extracted text is assembled into a DOCX file using python-docx, with:
- Title page
- Page markers for reference
- Preserved text structure (paragraphs)

## Notes

- For scanned documents, ensure the PDF pages are clear and well-scanned for best OCR results
- Mixed Bangla-English documents: use `--lang ben+eng`
- Higher DPI settings improve accuracy but increase processing time
- The bundled Tesseract uses the `ben.traineddata` from the official tessdata repository

## Troubleshooting

**"Tesseract not found" error:**
- Ensure `TESSDATA_PREFIX` environment variable points to the `tessdata` directory
- Verify `tesseract_cmd` is set correctly in the script

**Poor OCR accuracy:**
- Try different `--psm` values (1-13): `--psm 3` for auto page segmentation
- Convert to higher DPI: `--dpi 400` or higher
- For document images, try pre-processing with image enhancement

## License

This project uses open-source tools (Tesseract, PyMuPDF, python-docx, Pillow).
