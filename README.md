# Bangla PDF & Image OCR App (macOS)

A native macOS Python utility to extract Bengali text from multi-page PDFs and standard image assets. The application automatically processes documents page-by-page and saves a compiled text file (`_extracted.txt`) in the exact same directory as your input file.

## Prerequisites (macOS)
The app uses the local Mac infrastructure to run Tesseract. Install the core OCR binaries natively via MacPorts:
```bash
sudo port install tesseract tesseract-ben leptonica libpng libjpeg-turbo tiff webp openjpeg
```

## Local Development Setup
If you want to run or test the script from the source directory, set up your Python environment using these commands:

```bash
# Activate your environment
source venv/bin/activate

# Initialize pip if missing
python3 -m ensurepip --default-pip

# Install dependencies
pip install -r requirements.txt
```

## Running the Application
To launch the native macOS file picker dialog box using Python:
```bash
python3 src/ocr_bangla.py
```

## Building the Standalone `.app` Bundle
To re-compile the standalone, double-clickable Mac app bundle inside the `dist/` directory using PyInstaller, run:
```bash
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed --add-data "tessdata:tessdata" src/ocr_bangla.py
```

## Directory Structure
```text
ocr-bangla/
├── tessdata/           # Bundled Language Models handled by Git LFS
│   ├── ben.traineddata
│   ├── eng.traineddata
│   └── osd.traineddata
├── src/
│   └── ocr_bangla.py   # Main OCR & PDF pipeline controller script
├── .gitignore          # Keeps build and environment caches off GitHub
├── requirements.txt    # Python library stack (Pillow, PyMuPDF, pytesseract)
└── README.md           # This documentation file
```

