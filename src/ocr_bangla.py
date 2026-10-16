#!/usr/bin/env python3
import os
import sys
from PIL import Image
import pytesseract
import tkinter as tk
from tkinter import filedialog, messagebox
import fitz  # PyMuPDF for high-speed, zero-dependency PDF rendering

# Map the logic pointer to the MacPorts structural folder hierarchy
pytesseract.pytesseract.tesseract_cmd = '/opt/local/bin/tesseract'

def get_tessdata_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, 'tessdata')

def process_file_ocr(file_path):
    """
    Accepts either an image or a PDF. Extracts Bengali text and saves
    a matching text file in the same directory.
    """
    if not os.path.exists(file_path):
        return f"Error: File not found at {file_path}"
    
    # Construct the output file path in the exact same folder
    base_dir = os.path.dirname(file_path)
    file_name = os.path.splitext(os.path.basename(file_path))[0]
    output_txt_path = os.path.join(base_dir, f"{file_name}_extracted.txt")
    
    tessdata_path = get_tessdata_path()
    custom_config = f'--tessdata-dir "{tessdata_path}" --psm 3'
    ext = os.path.splitext(file_path)[1].lower()
    
    extracted_pages = []

    try:
        # HANDLING PDF FILES
        if ext == '.pdf':
            doc = fitz.open(file_path)
            total_pages = len(doc)
            
            for page_num in range(total_pages):
                page = doc.load_page(page_num)
                # Render page to high-quality image data (300 DPI equivalent)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                
                # Perform Bengali OCR on this page
                text = pytesseract.image_to_string(img, lang='ben', config=custom_config)
                
                page_header = f"--- PAGE {page_num + 1} / {total_pages} ---"
                extracted_pages.append(f"{page_header}\n{text.strip()}\n")
            doc.close()
            
        # HANDLING STANDARD IMAGE FILES
        elif ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
            with Image.open(file_path) as img:
                text = pytesseract.image_to_string(img, lang='ben', config=custom_config)
                extracted_pages.append(text.strip())
        else:
            return f"Unsupported file format: {ext}"

        # Combine all extracted pages
        final_output = "\n".join(extracted_pages).strip()
        
        if not final_output:
            return "OCR completed, but no readable Bangla text was detected."
            
        # Write the combined string directly to a file in the same folder
        with open(output_txt_path, "w", encoding="utf-8") as f:
            f.write(final_output)
            
        return f"Success! Bengali text extracted and saved to:\n\n{output_txt_path}"

    except Exception as error:
        return f"Processing Error: {str(error)}"

def run_gui_app():
    root = tk.Tk()
    root.withdraw()
    
    # Updated to open both images and PDF documents natively
    file_path = filedialog.askopenfilename(
        title="Select PDF or Image for Bangla OCR Processing",
        filetypes=[("Supported Files", "*.pdf *.png *.jpg *.jpeg *.bmp *.tiff")]
    )
    
    if file_path:
        status_message = process_file_ocr(file_path)
        messagebox.showinfo("OCR Status", status_message)
    else:
        messagebox.showinfo("Cancelled", "No file selected.")

if __name__ == '__main__':
    # Supports running via terminal argument or as a standard double-click Mac App
    if len(sys.argv) > 1:
        print(process_file_ocr(sys.argv[1]))
    else:
        run_gui_app()

