from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image, Flowable
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.pdfbase.pdfmetrics import stringWidth  # ← para medir ancho de texto
from flask import current_app

# --------------------------
# Utilidades de formato
# --------------------------

def _miles(n: float) -> str:
    """$ 12.345 (sin decimales, separador de miles con punto)"""
    s = f"{int(round(n, 0)):,}".replace(",", ".")
    return f"$ {s}"

def _fecha_ddmmyyyy(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d.day}/{d.month}/{d.year}"
    except Exception:
        return iso



# Subrayado grueso para el título (simula el “subrayado” del ejemplo)
class Underline(Flowable):
    def __init__(self, width, thickness=1.6, color=colors.black, space=2):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color
        self.space = space
        self.height = thickness + space
    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.space, self.width, self.space)


# --------------------------
# Bloques (encabezado, tabla)
# --------------------------

def _build_header(cliente: str, fecha_iso: str, page_width: float):
    styles = getSampleStyleSheet()

    # Estilos “grandes” como el ejemplo
    st_label = ParagraphStyle("st_label", parent=styles["Normal"], fontSize=18, leading=22)
    st_value = ParagraphStyle("st_value", parent=styles["Normal"], fontSize=24, leading=28, spaceAfter=6)
    st_title = ParagraphStyle("st_title", parent=styles["Normal"], fontSize=36, leading=38, alignment=TA_LEFT)

    # Logo más grande
    logo_path = Path(current_app.root_path) / "static" / "logo.png"
    # 55mm x 55mm, y damos un poco más de ancho a la primera columna para que no “apreté” el título
    logo_w = logo_h = 55 * mm
    col_logo = 58 * mm

    logo = Image(str(logo_path), width=logo_w, height=logo_h)

    # Título en 2 líneas con subrayado grueso
    title_block = []
    title_block.append(Paragraph("<b>ENTREGA DE</b>", st_title))
    title_block.append(Underline(110*mm, thickness=1.6, color=colors.black, space=3))
    title_block.append(Paragraph("<b>MERCADERIA</b>", st_title))

    nombre = Paragraph('<font size="18"><b>Nombre:</b></font>', st_label)
    nombre_val = Paragraph(f"<b>{cliente.upper()}</b>", st_value)
    fecha = Paragraph('<font size="18"><b>Fecha:</b></font>', st_label)
    fecha_val = Paragraph(f"<b>{_fecha_ddmmyyyy(fecha_iso)}</b>", st_value)

    info_tbl = Table([[nombre, nombre_val],
                      [fecha,  fecha_val]],
                     colWidths=[35*mm, 80*mm])
    info_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
    ]))

    right = Table([[Table([[item] for item in title_block], colWidths=[110*mm])],
                   [info_tbl]],
                  colWidths=[110*mm])
    right.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP")]))

    header = Table([[logo, right]], colWidths=[col_logo, page_width - col_logo])
    header.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    return header


def _build_items_table(items: List[Dict], width: float) -> Table:
    styles = getSampleStyleSheet()

    # Encabezado grande y centrado (igual)
    st_head = ParagraphStyle("st_head", parent=styles["Normal"], fontSize=20, leading=22, alignment=TA_CENTER)

    # ▶ LETRA DEL ÍTEM MÁS GRANDE
    st_cell = ParagraphStyle("st_cell", parent=styles["Normal"], fontSize=30, leading=30, alignment=TA_LEFT, spaceBefore=0, spaceAfter=0)
    st_num  = ParagraphStyle("st_num",  parent=styles["Normal"], fontSize=18, leading=21, alignment=TA_RIGHT)

    # Total row styles (igual que antes)
    st_total_left  = ParagraphStyle("st_total_left",  parent=styles["Normal"], fontSize=22, leading=24, alignment=TA_CENTER)
    st_total_right = ParagraphStyle("st_total_right", parent=styles["Normal"], fontSize=26, leading=28, alignment=TA_RIGHT)

    # ---- Anchos DINÁMICOS para evitar wraps en encabezados y TOTAL ----
    # Padding lateral de la tabla (6 pt a cada lado)
    PADDING_PT = 12

    # Cantidad: tomamos el más ancho (contenido y encabezado)
    qty_texts = [str(int(it.get("cantidad", 0))) for it in items] or ["0"]
    qty_texts.append("C")  # encabezado
    qty_w_pt = max(stringWidth(t, "Helvetica", st_head.fontSize if t == "C" else st_num.fontSize) for t in qty_texts) + PADDING_PT
    col_c = max(16 * mm, qty_w_pt)  # nunca menos de 16 mm

    # Precio: máximo entre encabezado y valores
    precio_texts = [ _miles(float(it.get("precio", 0))) for it in items ] or ["$ 0"]
    precio_texts.append("Precio")
    precio_w_pt = max(stringWidth(t, "Helvetica", st_head.fontSize if t == "Precio" else st_num.fontSize) for t in precio_texts) + PADDING_PT
    col_precio = max(26 * mm, precio_w_pt)  # mínimo 26 mm para no partir "Precio"

    # Total: considerar también el TOTAL FINAL en fuente 26
    total_texts = [ _miles(float(it.get("total", float(it.get("cantidad",0))*float(it.get("precio",0))))) for it in items ] or ["$ 0"]
    total_texts.append("Total")
    total_w_rows = max(stringWidth(t, "Helvetica", st_head.fontSize if t == "Total" else st_num.fontSize) for t in total_texts) + PADDING_PT
    total_w_grand = stringWidth(_miles(sum(float(it.get("total", float(it.get("cantidad",0))*float(it.get("precio",0)))) for it in items)), "Helvetica", st_total_right.fontSize) + PADDING_PT
    col_total = max(28 * mm, total_w_rows, total_w_grand)  # mínimo 28 mm y que entre el "Total Final"

    # El resto del ancho para "Artículo"
    col_art = max(60 * mm, width - (col_c + col_precio + col_total))

    # ---- Datos ----
    data = [
        [Paragraph("<b>Artículo</b>", st_head),
         Paragraph("<b>C</b>", st_head),
         Paragraph("<b>Precio</b>", st_head),
         Paragraph("<b>Total</b>", st_head)]
    ]

    total_val = 0.0
    for it in items:
        pieza  = str(it.get("pieza", "")).strip().replace("\n", "<br/>")
        cant   = int(it.get("cantidad", 0))
        precio = float(it.get("precio", 0))
        tot    = float(it.get("total", cant * precio))
        total_val += tot
        data.append([
            Paragraph(pieza, st_cell),             # ▶ ÍTEM con letra 18
            Paragraph(str(cant), st_head),         # centrado
            Paragraph(_miles(precio), st_num),
            Paragraph(_miles(tot), st_num),
        ])

    # Fila TOTAL (en una sola línea)
    idx_total = len(data)
    data.append([
        Paragraph("<b>Total Final:</b>", st_total_left),
        "", "",
        Paragraph(f"<b>{_miles(total_val)}</b>", st_total_right),
    ])

    tbl = Table(data, colWidths=[col_art, col_c, col_precio, col_total], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BOX", (0,0), (-1,-1), 1.4, colors.black),
        ("INNERGRID", (0,0), (-1,-2), 0.9, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.white),
        ("LINEBELOW", (0,0), (-1,0), 1.4, colors.black),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN",  (1,1), (1,-2), "CENTER"),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("SPAN", (0, idx_total), (2, idx_total)),
        ("LINEABOVE", (0, idx_total), (-1, idx_total), 1.6, colors.black),
        ("ALIGN", (0, idx_total), (2, idx_total), "CENTER"),
        ("RIGHTPADDING", (3, idx_total), (3, idx_total), 6),
    ]))
    return tbl


# --------------------------
# Generador principal
# --------------------------

def build_entrega_pdf(cliente: str, fecha_iso: str, items: List[Dict], out_path: Path) -> Tuple[bytes, str]:
    """
    Genera el PDF de la entrega con la estética del ejemplo.
    - Logo grande a la izquierda (data/logo.png|jpg)
    - Título grande subrayado
    - Nombre/Fecha grandes
    - Tabla con bordes gruesos; TOTAL integrado como última fila (con línea gruesa arriba)
    - Formato monetario $ 7.000
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=14*mm, rightMargin=14*mm,
        topMargin=10*mm, bottomMargin=14*mm,
        title=f"Entrega {cliente}"
    )
    page_w = A4[0] - doc.leftMargin - doc.rightMargin

    # Bloques
    header = _build_header(cliente, fecha_iso, page_w)
    items_tbl = _build_items_table(items, page_w)

    # Story: encabezado + tabla (el total ya va dentro de la tabla)
    story = [header, Spacer(0, 6), items_tbl]

    doc.build(story)
    return out_path.read_bytes(), str(out_path)
