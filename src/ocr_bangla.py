#!/usr/bin/env python3
import os
import sys
import threading  # Prevent UI freeze during long document processing
from PIL import Image
import pytesseract
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Import python-docx features for absolute canvas rendering
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# Map the logic pointer to the MacPorts structural folder hierarchy
pytesseract.pytesseract.tesseract_cmd = '/opt/local/bin/tesseract'

def get_tessdata_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, 'tessdata')

def create_floating_textbox(doc, text, left_inch, top_inch, width_inch, height_inch):
    """
    Injects an advanced, absolute-positioned, borderless floating Text Box shape
    directly into the document's XML structure to map OCR coordinates perfectly.
    """
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    
    # 1. Open XML Namespace definitions for drawings and shapes
    ns_wps = "http://microsoft.com"
    ns_wpc = "http://microsoft.com"
    ns_v = "urn:schemas-microsoft-com:vml"
    ns_o = "urn:schemas-microsoft-com:office:office"
    
    # Convert inches to structural EMUs (English Metric Units: 1 inch = 914400 EMUs)
    emu_left = int(left_inch * 914400)
    emu_top = int(top_inch * 914400)
    emu_width = int(width_inch * 914400)
    emu_height = int(height_inch * 914400)

    # 2. Build the low-level OpenXML node structure for a modern floating shape container
    drawing = OxmlElement('w:drawing')
    anchor = OxmlElement('wp:anchor')
    anchor.set(qn('wp:distT'), '0')
    anchor.set(qn('wp:distB'), '0')
    anchor.set(qn('wp:distL'), '0')
    anchor.set(qn('wp:distR'), '0')
    anchor.set(qn('wp:simplePos'), '0')
    anchor.set(qn('wp:relativeHeight'), '251658240')
    anchor.set(qn('wp:behindDoc'), '0')
    anchor.set(qn('wp:locked'), '0')
    anchor.set(qn('wp:layoutInCell'), '1')
    anchor.set(qn('wp:allowOverlap'), '1')

    # Position absolutely relative to the page margin canvas
    simplePos = OxmlElement('wp:simplePos')
    simplePos.set(qn('x'), '0')
    simplePos.set(qn('y'), '0')
    anchor.append(simplePos)

    positionH = OxmlElement('wp:positionH')
    positionH.set(qn('relativeFrom'), 'margin')
    posOffsetH = OxmlElement('wp:posOffset')
    posOffsetH.text = str(emu_left)
    positionH.append(posOffsetH)
    anchor.append(positionH)

    positionV = OxmlElement('wp:positionV')
    positionV.set(qn('relativeFrom'), 'margin')
    posOffsetV = OxmlElement('wp:posOffset')
    posOffsetV.text = str(emu_top)
    positionV.append(posOffsetV)
    anchor.append(positionV)

    extent = OxmlElement('wp:extent')
    extent.set(qn('cx'), str(emu_width))
    extent.set(qn('cy'), str(emu_height))
    anchor.append(extent)

    effectExtent = OxmlElement('wp:effectExtent')
    effectExtent.set(qn('l'), '0')
    effectExtent.set(qn('t'), '0')
    effectExtent.set(qn('r'), '0')
    effectExtent.set(qn('b'), '0')
    anchor.append(effectExtent)

    docPr = OxmlElement('wp:docPr')
    docPr.set(qn('id'), '1')
    docPr.set(qn('name'), 'Text Box Container')
    anchor.append(docPr)

    cNvGraphicFramePr = OxmlElement('wp:cNvGraphicFramePr')
    graphicFrameLocks = OxmlElement('a:graphicFrameLocks')
    graphicFrameLocks.set(qn('xmlns:a'), 'http://openxmlformats.org')
    graphicFrameLocks.set(qn('noChangeAspect'), '1')
    cNvGraphicFramePr.append(graphicFrameLocks)
    anchor.append(cNvGraphicFramePr)

    graphic = OxmlElement('a:graphic')
    graphic.set(qn('xmlns:a'), 'http://openxmlformats.org')
    graphicData = OxmlElement('a:graphicData')
    graphicData.set(qn('uri'), 'http://microsoft.com')

    wsp = OxmlElement('wps:wsp')
    wsp.set(qn('xmlns:wps'), ns_wps)
    cNvPr = OxmlElement('wps:cNvPr')
    cNvPr.set(qn('id'), '0')
    cNvPr.set(qn('name'), 'TextBoxShape')
    wsp.append(cNvPr)

    spPr = OxmlElement('wps:spPr')
    xfrm = OxmlElement('a:xfrm')
    off = OxmlElement('a:off')
    off.set(qn('x'), '0')
    off.set(qn('y'), '0')
    ext_size = OxmlElement('a:ext')
    ext_size.set(qn('cx'), str(emu_width))
    ext_size.set(qn('cy'), str(emu_height))
    xfrm.append(off)
    xfrm.append(ext_size)
    spPr.append(xfrm)

    prstGeom = OxmlElement('a:prstGeom')
    prstGeom.set(qn('prst'), 'rect')
    avLst = OxmlElement('a:avLst')
    prstGeom.append(avLst)
    spPr.append(prstGeom)

    # Make the text box transparent with no borders
    ln = OxmlElement('a:ln')
    noFillLn = OxmlElement('a:noFill')
    ln.append(noFillLn)
    spPr.append(ln)
    wsp.append(spPr)

    # 3. Inject standard Word paragraphs containing text into the shape object
    txbx = OxmlElement('wps:txbx')
    txbxContent = OxmlElement('w:txbxContent')
    
    tb_p = OxmlElement('w:p')
    tb_pPr = OxmlElement('w:pPr')
    # Minimize text box internal padding structures
    spacing = OxmlElement('w:spacing')
    spacing.set(qn('w:after'), '0')
    spacing.set(qn('w:line'), '240')
    spacing.set(qn('w:lineRule'), 'auto')
    tb_pPr.append(spacing)
    tb_p.append(tb_pPr)
    
    tb_r = OxmlElement('w:run')
    tb_rPr = OxmlElement('w:rPr')
    
    # Configure font fallback metrics for clean Bangla visual render stability
    rFonts = OxmlElement('w:rFonts')
    rFonts.set(qn('w:ascii'), 'Kalpurush')
    rFonts.set(qn('w:hAnsi'), 'Kalpurush')
    rFonts.set(qn('w:cs'), 'Kalpurush')
    tb_rPr.append(rFonts)
    
    sz = OxmlElement('w:sz')
    sz.set(qn('w:val'), '22')  # 11pt font scale
    tb_rPr.append(sz)
    
    tb_r.append(tb_rPr)
    tb_t = OxmlElement('w:t')
    tb_t.text = text
    tb_r.append(tb_t)
    tb_p.append(tb_r)
    
    txbxContent.append(tb_p)
    txbx.append(txbxContent)
    wsp.append(txbx)

    graphicData.append(wsp)
    graphic.append(graphicData)
    anchor.append(graphic)
    drawing.append(anchor)
    run._r.append(drawing)


class OCRApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bangla & English OCR Platform")
        self.root.geometry("450x200")
        self.root.resizable(False, False)
        
        self.root.eval('tk::PlaceWindow . center')
        
        self.label_info = tk.Label(
            self.root,
            text="Select a PDF or Image to extract Bangla & English text.\nOutputs are automatically saved as Word documents (.docx).",
            font=("Arial", 11),
            pady=15
        )
        self.label_info.pack()

        self.status_label = tk.Label(self.root, text="", font=("Arial", 10, "italic"), fg="blue")
        self.status_label.pack(pady=2)

        self.progress_bar = ttk.Progressbar(self.root, orient='horizontal', length=350, mode='determinate')
        self.progress_bar.pack(pady=10)

        self.btn_select = tk.Button(self.root, text="Select & Process File", command=self.start_file_selection, height=2, width=20)
        self.btn_select.pack(pady=10)

    def start_file_selection(self):
        file_path = filedialog.askopenfilename(
            title="Select PDF or Image for OCR Processing",
            filetypes=[("Supported Files", "*.pdf *.png *.jpg *.jpeg *.bmp *.tiff")]
        )
        if file_path:
            self.btn_select.config(state='disabled')
            self.progress_bar['value'] = 0
            threading.Thread(target=self.threaded_ocr_process, args=(file_path,), daemon=True).start()

    def process_image_layout(self, img, doc_out, custom_config):
        """Processes an image using image_to_data to capture layout coordinates."""
        # Query Tesseract for raw block data instead of plain text strings
        data = pytesseract.image_to_data(img, lang='ben+eng', config=custom_config, output_type=pytesseract.Output.DICT)
        
        img_w, img_h = img.size
        # Map pixels to default targets (Standard letter width 8.5 inches minus default margins)
        target_page_width_in = 6.5
        scale = target_page_width_in / img_w
        
        n_boxes = len(data['level'])
        has_content = False
        
        # Group text fragments belonging to the exact same block layer line hierarchy
        current_block = -1
        current_line = -1
        line_text = []
        x_min, y_min, x_max, y_max = 0, 0, 0, 0
        
        for i in range(n_boxes):
            # level 5 corresponds to structured phrase text segments
            if data['level'][i] == 5:
                text = data['text'][i].strip()
                if not text:
                    continue
                
                b_num = data['block_num'][i]
                l_num = data['line_num'][i]
                
                x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                
                if b_num == current_block and l_num == current_line:
                    line_text.append(text)
                    x_min = min(x_min, x)
                    y_min = min(y_min, y)
                    x_max = max(x_max, x + w)
                    y_max = max(y_max, y + h)
                else:
                    if line_text:
                        full_str = " ".join(line_text)
                        create_floating_textbox(
                            doc_out, full_str,
                            x_min * scale, y_min * scale,
                            (x_max - x_min) * scale, (y_max - y_min) * scale
                        )
                        has_content = True
                    
                    line_text = [text]
                    x_min, y_min, x_max, y_max = x, y, x + w, y + h
