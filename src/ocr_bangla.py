#!/usr/bin/env python3
import os
import sys
import threading  # Prevent UI freeze during long document processing
from PIL import Image
import pytesseract
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from docx import Document

# Map the logic pointer to the MacPorts structural folder hierarchy
pytesseract.pytesseract.tesseract_cmd = '/opt/local/bin/tesseract'

def get_tessdata_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, 'tessdata')


class OCRApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bangla & English OCR Platform")
        self.root.geometry("450x200")
        self.root.resizable(False, False)
        
        # Center the application window on screen
        self.root.eval('tk::PlaceWindow . center')
        
        # Interface Description
        self.label_info = tk.Label(
            self.root,
            text="Select a PDF or Image to extract Bangla & English text.\nOutputs are automatically saved as Word documents (.docx).",
            font=("Arial", 11),
            pady=15
        )
        self.label_info.pack()

        # Progress Status Text Label
        self.status_label = tk.Label(self.root, text="", font=("Arial", 10, "italic"), fg="blue")
        self.status_label.pack(pady=2)

        # Standard Native Ttk Progressbar
        self.progress_bar = ttk.Progressbar(self.root, orient='horizontal', length=350, mode='determinate')
        self.progress_bar.pack(pady=10)

        # Action File Browser Selection Button
        self.btn_select = tk.Button(self.root, text="Select & Process File", command=self.start_file_selection, height=2, width=20)
        self.btn_select.pack(pady=10)

    def start_file_selection(self):
        file_path = filedialog.askopenfilename(
            title="Select PDF or Image for OCR Processing",
            filetypes=[("Supported Files", "*.pdf *.png *.jpg *.jpeg *.bmp *.tiff")]
        )
        if file_path:
            # Lock the execution button so users can't trigger multiple threads
            self.btn_select.config(state='disabled')
            self.progress_bar['value'] = 0
            
            # Spin the OCR logic loop out into a safe background execution thread
            threading.Thread(target=self.threaded_ocr_process, args=(file_path,), daemon=True).start()

    def threaded_ocr_process(self, file_path):
        base_dir = os.path.dirname(file_path)
        file_name = os.path.splitext(os.path.basename(file_path))[0]
        output_doc_path = os.path.join(base_dir, f"{file_name}_extracted.docx")
        
        tessdata_path = get_tessdata_path()
        custom_config = f'--tessdata-dir "{tessdata_path}" --psm 3'
        ext = os.path.splitext(file_path)[1].lower()
        
        doc_out = Document()
        has_content = False

        try:
            if ext == '.pdf':
                import fitz  # PyMuPDF
                doc = fitz.open(file_path)
                total_pages = len(doc)
                
                if total_pages == 0:
                    self.finalize_process("Error: Selected PDF document contains no pages.", is_error=True)
                    return

                for page_num in range(total_pages):
                    # Safely update the progress context variables
                    percent_done = int(((page_num + 1) / total_pages) * 100)
                    self.status_label.config(text=f"Processing Page {page_num + 1} of {total_pages} ({percent_done}%)")
                    self.progress_bar['value'] = percent_done
                    self.root.update_idletasks()  # Force update the window animations

                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    
                    text = pytesseract.image_to_string(img, lang='ben+eng', config=custom_config).strip()
                    if text:
                        if has_content:
                            doc_out.add_page_break()
                        doc_out.add_paragraph(text)
                        has_content = True
                doc.close()
                
            elif ext in ['.png', '.jpg', '.jpeg', '.bmp', '.tiff']:
                self.status_label.config(text="Processing image file...")
                self.progress_bar['value'] = 50
                self.root.update_idletasks()
                
                with Image.open(file_path) as img:
                    text = pytesseract.image_to_string(img, lang='ben+eng', config=custom_config).strip()
                    if text:
                        doc_out.add_paragraph(text)
                        has_content = True
                
                self.progress_bar['value'] = 100
                self.root.update_idletasks()
            else:
                self.finalize_process(f"Unsupported file format: {ext}", is_error=True)
                return

            if not has_content:
                self.finalize_process("OCR completed, but no readable Bangla or English text was detected.", is_error=True)
                return
                
            doc_out.save(output_doc_path)
            self.finalize_process(f"Success! Text extracted and saved to:\n\n{output_doc_path}")

        except Exception as error:
            self.finalize_process(f"Processing Error: {str(error)}", is_error=True)

    def finalize_process(self, message, is_error=False):
        # Clean up labels, drop progress state back down, and release button locks safely
        self.status_label.config(text="")
        self.progress_bar['value'] = 0
        self.btn_select.config(state='normal')
        
        if is_error:
            messagebox.showerror("OCR Error", message)
        else:
            messagebox.showinfo("OCR Status", message)

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    # Launch interface loop safely
    app = OCRApp()
    app.run()
