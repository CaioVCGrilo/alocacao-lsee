"""Geração de etiquetas LSEE (PCs e Monitores) em .docx e PDF 1:1 (grade Pimaco A4 3x6)."""

from __future__ import annotations

import copy
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pymupdf

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
BASE_DOCX_PATH = ASSETS_DIR / "etiqueta_base.docx"
STATIC_LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "etiqueta_logo.png"

ROWS_PER_PAGE = 6
COLS_PER_PAGE = 3
MAX_PAGES = 20

# Conversões exatas OOXML: 1 dxa = 1/20 pt, 1 pt = 12700 EMU
PAGE_W_PT = 11906 / 20.0
PAGE_H_PT = 16838 / 20.0
MARGIN_TOP_PT = 482 / 20.0
MARGIN_LEFT_PT = 397 / 20.0
COL_W_PT = 3742 / 20.0
ROW_H_PT = 2637 / 20.0
CELL_MAR_LEFT_PT = 71 / 20.0

LINE_EMU = (37671, 511810, 2214245, 0)
LOGO_EMU = (90376, 114935, 971550, 316230)
SPECS_EMU = (22754, 539618, 2214245, 1094109)
CODE_EMU = (1127125, 53340, 1172845, 412115)

INSET_H_EMU = 91440
INSET_V_EMU = 45720
INSET_H_PT = INSET_H_EMU / 12700.0
INSET_V_PT = INSET_V_EMU / 12700.0

LINE_RGB = (79 / 255.0, 129 / 255.0, 189 / 255.0)  # accent1 #4F81BD
CODE_RGB = (0.0, 112 / 255.0, 192 / 255.0)  # #0070C0

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_WP14 = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_A14 = "http://schemas.microsoft.com/office/drawing/2010/main"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS_XML = "http://www.w3.org/XML/1998/namespace"

for _pfx, _uri in (
    ("w", NS_W),
    ("r", NS_R),
    ("wp", NS_WP),
    ("wp14", NS_WP14),
    ("a", NS_A),
    ("a14", NS_A14),
    ("pic", NS_PIC),
    ("wps", NS_WPS),
    ("mc", NS_MC),
):
    ET.register_namespace(_pfx, _uri)


def _w(tag: str) -> str:
    return f"{{{NS_W}}}{tag}"


def _wp(tag: str) -> str:
    return f"{{{NS_WP}}}{tag}"


def _a(tag: str) -> str:
    return f"{{{NS_A}}}{tag}"


def _wps(tag: str) -> str:
    return f"{{{NS_WPS}}}{tag}"


def _pic(tag: str) -> str:
    return f"{{{NS_PIC}}}{tag}"


def _mc(tag: str) -> str:
    return f"{{{NS_MC}}}{tag}"


def _load_pdf_fonts() -> Tuple[pymupdf.Font, pymupdf.Font]:
    """Carrega Times New Roman (regular e negrito) com fallback seguro."""
    win_fonts = Path(r"C:\Windows\Fonts")
    reg_path = win_fonts / "times.ttf"
    bold_path = win_fonts / "timesbd.ttf"
    if reg_path.is_file() and bold_path.is_file():
        return pymupdf.Font(fontfile=str(reg_path)), pymupdf.Font(fontfile=str(bold_path))
    return pymupdf.Font("tiro"), pymupdf.Font("tibo")


def normalize_label_pages(payload: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    """Valida e normaliza a estrutura de páginas e células recebida da API."""
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise ValueError("Informe ao menos uma página com etiquetas para gerar.")
    if len(raw_pages) > MAX_PAGES:
        raise ValueError(f"Máximo de {MAX_PAGES} páginas por arquivo.")

    normalized_pages: List[List[Dict[str, Any]]] = []
    total_labels = 0
    for p_idx, raw_cells in enumerate(raw_pages):
        if not isinstance(raw_cells, list):
            raise ValueError(f"Página {p_idx + 1} inválida.")
        seen_coords: set[Tuple[int, int]] = set()
        page_cells: List[Dict[str, Any]] = []
        for cell in raw_cells:
            if not isinstance(cell, dict):
                raise ValueError(f"Item de etiqueta inválido na página {p_idx + 1}.")
            try:
                row = int(cell["row"])
                col = int(cell["col"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Posição linha/coluna inválida.") from exc
            if not (0 <= row < ROWS_PER_PAGE and 0 <= col < COLS_PER_PAGE):
                raise ValueError(f"Posição ({row + 1}, {col + 1}) fora da grade 6x3.")
            if (row, col) in seen_coords:
                raise ValueError(f"Posição duplicada ({row + 1}, {col + 1}) na página {p_idx + 1}.")
            seen_coords.add((row, col))

            code = str(cell.get("code") or "").strip()
            raw_lines = cell.get("lines") or []
            if not isinstance(raw_lines, list):
                raise ValueError("Linhas da etiqueta devem formar uma lista.")
            lines: List[Dict[str, str]] = []
            for ln in raw_lines[:6]:
                if isinstance(ln, dict):
                    lbl = str(ln.get("label") or "").strip()
                    val = str(ln.get("value") or "").strip()
                    if lbl or val:
                        lines.append({"label": lbl, "value": val})
            if not code and not lines:
                continue
            page_cells.append({"row": row, "col": col, "code": code, "lines": lines})
            total_labels += 1
        normalized_pages.append(page_cells)

    if total_labels == 0:
        raise ValueError("Adicione pelo menos uma etiqueta na folha antes de exportar.")
    return normalized_pages


def _code_font_pt(code: str, bold_font: pymupdf.Font) -> float:
    """Mantém 22 pt padrão e reduz levemente apenas se o código exceder a largura útil."""
    if not code:
        return 22.0
    max_w = (CODE_EMU[2] / 12700.0) - 2.0 * INSET_H_PT
    w22 = bold_font.text_length(code, fontsize=22.0)
    if w22 <= max_w or w22 <= 0:
        return 22.0
    return max(12.0, round(22.0 * (max_w - 0.5) / w22, 1))


def _make_rpr(bold: bool = False, sz_half_pt: int = 22, color_hex: str | None = None) -> ET.Element:
    rpr = ET.Element(_w("rPr"))
    rfonts = ET.SubElement(
        rpr,
        _w("rFonts"),
        {_w("ascii"): "Times New Roman", _w("hAnsi"): "Times New Roman", _w("cs"): "Times New Roman"},
    )
    _ = rfonts
    if bold:
        ET.SubElement(rpr, _w("b"))
    if color_hex:
        ET.SubElement(rpr, _w("color"), {_w("val"): color_hex})
    ET.SubElement(rpr, _w("sz"), {_w("val"): str(sz_half_pt)})
    ET.SubElement(rpr, _w("szCs"), {_w("val"): str(sz_half_pt)})
    return rpr


def _make_anchor_header(rel_height: int, doc_id: int, name: str, pos_emu: Tuple[int, int, int, int], behind: str = "0", lock_aspect: bool = False, rel_size_from: str = "margin") -> ET.Element:
    px, py, cx, cy = pos_emu
    anchor = ET.Element(
        _wp("anchor"),
        {
            "distT": "0",
            "distB": "0",
            "distL": "114300",
            "distR": "114300",
            "simplePos": "0",
            "relativeHeight": str(rel_height),
            "behindDoc": behind,
            "locked": "0",
            "layoutInCell": "1",
            "allowOverlap": "1",
        },
    )
    ET.SubElement(anchor, _wp("simplePos"), {"x": "0", "y": "0"})
    pos_h = ET.SubElement(anchor, _wp("positionH"), {"relativeFrom": "column"})
    ET.SubElement(pos_h, _wp("posOffset")).text = str(px)
    pos_v = ET.SubElement(anchor, _wp("positionV"), {"relativeFrom": "paragraph"})
    ET.SubElement(pos_v, _wp("posOffset")).text = str(py)
    ET.SubElement(anchor, _wp("extent"), {"cx": str(cx), "cy": str(cy)})
    eff_r = "14605" if cy == 0 else "0"
    eff_tb = "19050" if cy == 0 else "0"
    eff_b = "7620" if lock_aspect else eff_tb
    ET.SubElement(anchor, _wp("effectExtent"), {"l": "0", "t": eff_tb, "r": eff_r, "b": eff_b})
    ET.SubElement(anchor, _wp("wrapNone"))
    ET.SubElement(anchor, _wp("docPr"), {"id": str(doc_id), "name": name})
    cnv = ET.SubElement(anchor, _wp("cNvGraphicFramePr"))
    ET.SubElement(cnv, _a("graphicFrameLocks"), {"noChangeAspect": "1"} if lock_aspect else {})
    return anchor


def _append_rel_size(anchor: ET.Element, rel_from: str) -> None:
    srh = ET.SubElement(anchor, f"{{{NS_WP14}}}sizeRelH", {"relativeFrom": rel_from})
    ET.SubElement(srh, f"{{{NS_WP14}}}pctWidth").text = "0"
    srv = ET.SubElement(anchor, f"{{{NS_WP14}}}sizeRelV", {"relativeFrom": rel_from})
    ET.SubElement(srv, f"{{{NS_WP14}}}pctHeight").text = "0"


def _make_line_run(doc_id: int, rel_height: int) -> ET.Element:
    run = ET.Element(_w("r"))
    rpr = ET.SubElement(run, _w("rPr"))
    ET.SubElement(rpr, _w("noProof"))
    mc = ET.SubElement(run, _mc("AlternateContent"))
    choice = ET.SubElement(mc, _mc("Choice"), {"Requires": "wps"})
    drawing = ET.SubElement(choice, _w("drawing"))
    anchor = _make_anchor_header(rel_height, doc_id, f"Conector reto {doc_id}", LINE_EMU, behind="0")
    graphic = ET.SubElement(anchor, _a("graphic"))
    gdata = ET.SubElement(graphic, _a("graphicData"), {"uri": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"})
    wsp = ET.SubElement(gdata, _wps("wsp"))
    ET.SubElement(wsp, _wps("cNvCnPr"))
    sp_pr = ET.SubElement(wsp, _wps("spPr"))
    xfrm = ET.SubElement(sp_pr, _a("xfrm"))
    ET.SubElement(xfrm, _a("off"), {"x": "0", "y": "0"})
    ET.SubElement(xfrm, _a("ext"), {"cx": str(LINE_EMU[2]), "cy": str(LINE_EMU[3])})
    prst = ET.SubElement(sp_pr, _a("prstGeom"), {"prst": "line"})
    ET.SubElement(prst, _a("avLst"))
    ET.SubElement(sp_pr, _a("ln"), {"w": "38100", "cmpd": "thinThick"})
    style = ET.SubElement(wsp, _wps("style"))
    ln_ref = ET.SubElement(style, _a("lnRef"), {"idx": "1"})
    ET.SubElement(ln_ref, _a("schemeClr"), {"val": "accent1"})
    fill_ref = ET.SubElement(style, _a("fillRef"), {"idx": "0"})
    ET.SubElement(fill_ref, _a("schemeClr"), {"val": "accent1"})
    eff_ref = ET.SubElement(style, _a("effectRef"), {"idx": "0"})
    ET.SubElement(eff_ref, _a("schemeClr"), {"val": "accent1"})
    f_ref = ET.SubElement(style, _a("fontRef"), {"idx": "minor"})
    ET.SubElement(f_ref, _a("schemeClr"), {"val": "tx1"})
    ET.SubElement(wsp, _wps("bodyPr"))
    drawing.append(anchor)
    return run


def _make_logo_run(doc_id: int, rel_height: int) -> ET.Element:
    run = ET.Element(_w("r"))
    rpr = ET.SubElement(run, _w("rPr"))
    ET.SubElement(rpr, _w("noProof"))
    drawing = ET.SubElement(run, _w("drawing"))
    anchor = _make_anchor_header(rel_height, doc_id, f"Imagem {doc_id}", LOGO_EMU, behind="1", lock_aspect=True)
    graphic = ET.SubElement(anchor, _a("graphic"))
    gdata = ET.SubElement(graphic, _a("graphicData"), {"uri": "http://schemas.openxmlformats.org/drawingml/2006/picture"})
    pic = ET.SubElement(gdata, _pic("pic"))
    nv = ET.SubElement(pic, _pic("nvPicPr"))
    ET.SubElement(nv, _pic("cNvPr"), {"id": "0", "name": "logoLSEE.png"})
    ET.SubElement(nv, _pic("cNvPicPr"))
    bf = ET.SubElement(pic, _pic("blipFill"))
    blip = ET.SubElement(bf, _a("blip"), {f"{{{NS_R}}}embed": "rId5", "cstate": "print"})
    ext_lst = ET.SubElement(blip, _a("extLst"))
    ext = ET.SubElement(ext_lst, _a("ext"), {"uri": "{28A0092B-C50C-407E-A947-70E740481C1C}"})
    ET.SubElement(ext, f"{{{NS_A14}}}useLocalDpi", {"val": "0"})
    stretch = ET.SubElement(bf, _a("stretch"))
    ET.SubElement(stretch, _a("fillRect"))
    sp_pr = ET.SubElement(pic, _pic("spPr"))
    xfrm = ET.SubElement(sp_pr, _a("xfrm"))
    ET.SubElement(xfrm, _a("off"), {"x": "0", "y": "0"})
    ET.SubElement(xfrm, _a("ext"), {"cx": str(LOGO_EMU[2]), "cy": str(LOGO_EMU[3])})
    prst = ET.SubElement(sp_pr, _a("prstGeom"), {"prst": "rect"})
    ET.SubElement(prst, _a("avLst"))
    _append_rel_size(anchor, "page")
    drawing.append(anchor)
    return run


def _make_textbox_run(
    doc_id: int,
    rel_height: int,
    pos_emu: Tuple[int, int, int, int],
    paragraphs: List[ET.Element],
) -> ET.Element:
    run = ET.Element(_w("r"))
    rpr = ET.SubElement(run, _w("rPr"))
    ET.SubElement(rpr, _w("noProof"))
    mc = ET.SubElement(run, _mc("AlternateContent"))
    choice = ET.SubElement(mc, _mc("Choice"), {"Requires": "wps"})
    drawing = ET.SubElement(choice, _w("drawing"))
    anchor = _make_anchor_header(rel_height, doc_id, f"Caixa de Texto {doc_id}", pos_emu, behind="0")
    graphic = ET.SubElement(anchor, _a("graphic"))
    gdata = ET.SubElement(graphic, _a("graphicData"), {"uri": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"})
    wsp = ET.SubElement(gdata, _wps("wsp"))
    cnv = ET.SubElement(wsp, _wps("cNvSpPr"), {"txBox": "1"})
    ET.SubElement(cnv, _a("spLocks"), {"noChangeArrowheads": "1"})
    sp_pr = ET.SubElement(wsp, _wps("spPr"), {"bwMode": "auto"})
    xfrm = ET.SubElement(sp_pr, _a("xfrm"))
    ET.SubElement(xfrm, _a("off"), {"x": "0", "y": "0"})
    ET.SubElement(xfrm, _a("ext"), {"cx": str(pos_emu[2]), "cy": str(pos_emu[3])})
    prst = ET.SubElement(sp_pr, _a("prstGeom"), {"prst": "rect"})
    ET.SubElement(prst, _a("avLst"))
    ET.SubElement(sp_pr, _a("noFill"))
    ln = ET.SubElement(sp_pr, _a("ln"), {"w": "9525"})
    ET.SubElement(ln, _a("noFill"))
    ET.SubElement(ln, _a("miter"), {"lim": "800000"})
    ET.SubElement(ln, _a("headEnd"))
    ET.SubElement(ln, _a("tailEnd"))

    txbx = ET.SubElement(wsp, _wps("txbx"))
    content = ET.SubElement(txbx, _w("txbxContent"))
    for p_el in paragraphs:
        content.append(p_el)

    body_pr = ET.SubElement(
        wsp,
        _wps("bodyPr"),
        {
            "rot": "0",
            "vert": "horz",
            "wrap": "square",
            "lIns": str(INSET_H_EMU),
            "tIns": str(INSET_V_EMU),
            "rIns": str(INSET_H_EMU),
            "bIns": str(INSET_V_EMU),
            "anchor": "t",
            "anchorCtr": "0",
        },
    )
    ET.SubElement(body_pr, _a("noAutofit"))
    _append_rel_size(anchor, "margin")
    drawing.append(anchor)
    return run


def _build_specs_paragraphs(lines: List[Dict[str, str]]) -> List[ET.Element]:
    out: List[ET.Element] = []
    for ln in lines:
        p_el = ET.Element(_w("p"))
        ppr = ET.SubElement(p_el, _w("pPr"))
        ET.SubElement(ppr, _w("spacing"), {_w("after"): "120"})
        ppr.append(_make_rpr(bold=False, sz_half_pt=22))

        lbl = ln.get("label", "")
        val = ln.get("value", "")
        if lbl:
            r_lbl = ET.SubElement(p_el, _w("r"))
            r_lbl.append(_make_rpr(bold=True, sz_half_pt=22))
            t_lbl = ET.SubElement(r_lbl, _w("t"))
            t_lbl.text = lbl
        if val:
            text = f" {val}" if lbl else val
            r_val = ET.SubElement(p_el, _w("r"))
            r_val.append(_make_rpr(bold=False, sz_half_pt=22))
            t_val = ET.SubElement(r_val, _w("t"), {f"{{{NS_XML}}}space": "preserve"})
            t_val.text = text
        out.append(p_el)
    return out


def _build_code_paragraphs(code: str, bold_font: pymupdf.Font) -> List[ET.Element]:
    pt = _code_font_pt(code, bold_font)
    sz_half = int(round(pt * 2))
    p_el = ET.Element(_w("p"))
    ppr = ET.SubElement(p_el, _w("pPr"))
    ET.SubElement(ppr, _w("spacing"), {_w("after"): "0", _w("line"): "240", _w("lineRule"): "auto"})
    ppr.append(_make_rpr(bold=True, sz_half_pt=sz_half, color_hex="0070C0"))
    run = ET.SubElement(p_el, _w("r"))
    run.append(_make_rpr(bold=True, sz_half_pt=sz_half, color_hex="0070C0"))
    t_el = ET.SubElement(run, _w("t"), {f"{{{NS_XML}}}space": "preserve"})
    t_el.text = code
    return [p_el]


def build_labels_docx(pages: List[List[Dict[str, Any]]]) -> bytes:
    """Gera arquivo .docx mantendo exatamente o gabarito Pimaco 3x6 de etiquetaPCs/etiquetaMonitores."""
    if not BASE_DOCX_PATH.is_file():
        raise FileNotFoundError(f"Gabarito base não encontrado: {BASE_DOCX_PATH}")

    _, bold_font = _load_pdf_fonts()
    with zipfile.ZipFile(BASE_DOCX_PATH, "r") as zin:
        doc_root = ET.fromstring(zin.read("word/document.xml"))
        body = doc_root.find(_w("body"))
        if body is None:
            raise RuntimeError("Gabarito DOCX inválido: sem elemento body.")

        tbl_proto = copy.deepcopy(body.find(_w("tbl")))
        sect_pr = copy.deepcopy(body.find(_w("sectPr")))
        vanish_p = copy.deepcopy(body.findall(_w("p"))[-1])

        # Limpa filhos do body mantendo apenas tabelas geradas por página + vanish_p + sectPr
        for child in list(body):
            body.remove(child)

        doc_id = 1
        rel_h = 251660288
        for p_idx, cells in enumerate(pages):
            tbl = copy.deepcopy(tbl_proto)
            trs = tbl.findall(f"./{_w('tr')}")
            for cell_data in cells:
                tc = trs[cell_data["row"]].findall(f"./{_w('tc')}")[cell_data["col"]]
                p_el = tc.find(f"./{_w('p')}")
                for r in list(p_el.findall(f"./{_w('r')}")):
                    p_el.remove(r)

                p_el.append(_make_line_run(doc_id, rel_h))
                doc_id += 1
                rel_h += 1024
                p_el.append(_make_logo_run(doc_id, rel_h))
                doc_id += 1
                rel_h += 1024
                p_el.append(
                    _make_textbox_run(
                        doc_id,
                        rel_h,
                        SPECS_EMU,
                        _build_specs_paragraphs(cell_data["lines"]),
                    )
                )
                doc_id += 1
                rel_h += 1024
                p_el.append(
                    _make_textbox_run(
                        doc_id,
                        rel_h,
                        CODE_EMU,
                        _build_code_paragraphs(cell_data["code"], bold_font),
                    )
                )
                doc_id += 1
                rel_h += 1024

            body.append(tbl)
            if p_idx < len(pages) - 1:
                brk_p = ET.Element(_w("p"))
                bppr = ET.SubElement(brk_p, _w("pPr"))
                ET.SubElement(
                    bppr,
                    _w("spacing"),
                    {_w("before"): "0", _w("after"): "0", _w("line"): "20", _w("lineRule"): "exact"},
                )
                brpr = ET.SubElement(bppr, _w("rPr"))
                ET.SubElement(brpr, _w("sz"), {_w("val"): "2"})
                ET.SubElement(brpr, _w("szCs"), {_w("val"): "2"})
                br_r = ET.SubElement(brk_p, _w("r"))
                ET.SubElement(br_r, _w("br"), {_w("type"): "page"})
                body.append(brk_p)

        body.append(vanish_p)
        body.append(sect_pr)

        out_buf = io.BytesIO()
        new_doc_xml = ET.tostring(doc_root, encoding="utf-8", xml_declaration=True)
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = new_doc_xml if item.filename == "word/document.xml" else zin.read(item.filename)
                zout.writestr(item, data)
        return out_buf.getvalue()


def _wrap_tokens(tokens: List[Tuple[str, bool]], font_reg: pymupdf.Font, font_bold: pymupdf.Font, fontsize: float, max_w: float) -> List[List[Tuple[str, bool]]]:
    """Quebra uma linha com trechos negrito/normal em múltiplas linhas se ultrapassar max_w."""
    words: List[Tuple[str, bool]] = []
    for txt, is_b in tokens:
        if not txt:
            continue
        # Preserva espaços atrelados às palavras
        parts = txt.split(" ")
        for idx, part in enumerate(parts):
            piece = part if idx == len(parts) - 1 else part + " "
            if piece:
                words.append((piece, is_b))

    wrapped: List[List[Tuple[str, bool]]] = [[]]
    cur_w = 0.0
    for word, is_b in words:
        f = font_bold if is_b else font_reg
        w_len = f.text_length(word, fontsize=fontsize)
        if wrapped[-1] and cur_w + w_len > max_w:
            wrapped.append([])
            cur_w = 0.0
            word_draw = word.lstrip()
            w_len = f.text_length(word_draw, fontsize=fontsize)
            wrapped[-1].append((word_draw, is_b))
            cur_w += w_len
        else:
            wrapped[-1].append((word, is_b))
            cur_w += w_len
    return [line for line in wrapped if line]


def _layout_specs(lines: List[Dict[str, str]], font_reg: pymupdf.Font, font_bold: pymupdf.Font, box_w: float, box_h: float) -> Tuple[float, List[List[List[Tuple[str, bool]]]]]:
    """Calcula layout mantendo 11 pt padrão e ajustando apenas se exceder altura da caixa."""
    usable_w = box_w - 2.0 * INSET_H_PT
    usable_h = box_h - 2.0 * INSET_V_PT
    for size in (11.0, 10.5, 10.0, 9.5, 9.0, 8.5):
        para_blocks: List[List[List[Tuple[str, bool]]]] = []
        total_lines = 0
        for ln in lines:
            tokens: List[Tuple[str, bool]] = []
            lbl = ln.get("label", "")
            val = ln.get("value", "")
            if lbl:
                tokens.append((lbl, True))
            if val:
                tokens.append((f" {val}" if lbl else val, False))
            if not tokens:
                continue
            wrapped = _wrap_tokens(tokens, font_reg, font_bold, size, usable_w)
            para_blocks.append(wrapped)
            total_lines += len(wrapped)
        line_h = size * 1.15
        after_gap = 6.0 if size >= 10.5 else 4.5
        need_h = total_lines * line_h + max(0, len(para_blocks) - 1) * after_gap
        if need_h <= usable_h or size == 8.5:
            return size, para_blocks
    return 11.0, []


def build_labels_pdf(pages: List[List[Dict[str, Any]]]) -> bytes:
    """Gera PDF vetorial A4 em escala 1:1 com medidas exatas em pontos do gabarito Pimaco 3x6."""
    if not STATIC_LOGO_PATH.is_file():
        raise FileNotFoundError(f"Logo da etiqueta não encontrado: {STATIC_LOGO_PATH}")

    logo_bytes = STATIC_LOGO_PATH.read_bytes()
    font_reg, font_bold = _load_pdf_fonts()
    doc = pymupdf.open()

    try:
        for cells in pages:
            page = doc.new_page(width=PAGE_W_PT, height=PAGE_H_PT)
            tw_code = pymupdf.TextWriter(page.rect)
            tw_body = pymupdf.TextWriter(page.rect)

            for cell in cells:
                cell_x = MARGIN_LEFT_PT + cell["col"] * COL_W_PT
                cell_y = MARGIN_TOP_PT + cell["row"] * ROW_H_PT
                col_x = cell_x + CELL_MAR_LEFT_PT

                # 1. Logo do LSEE
                lx = col_x + LOGO_EMU[0] / 12700.0
                ly = cell_y + LOGO_EMU[1] / 12700.0
                lw = LOGO_EMU[2] / 12700.0
                lh = LOGO_EMU[3] / 12700.0
                page.insert_image(pymupdf.Rect(lx, ly, lx + lw, ly + lh), stream=logo_bytes, keep_proportion=False)

                # 2. Linha horizontal dupla fino-grosso (cmpd="thinThick", w=3pt, #4F81BD)
                hx1 = col_x + LINE_EMU[0] / 12700.0
                hx2 = hx1 + LINE_EMU[2] / 12700.0
                hy = cell_y + LINE_EMU[1] / 12700.0
                page.draw_line((hx1, hy - 0.9), (hx2, hy - 0.9), color=LINE_RGB, width=0.65)
                page.draw_line((hx1, hy + 0.7), (hx2, hy + 0.7), color=LINE_RGB, width=1.45)

                # 3. Código em destaque azul (#0070C0, Times New Roman Bold)
                code = cell.get("code") or ""
                if code:
                    cx_box = col_x + CODE_EMU[0] / 12700.0 + INSET_H_PT
                    cy_box = cell_y + CODE_EMU[1] / 12700.0 + INSET_V_PT
                    c_size = _code_font_pt(code, font_bold)
                    baseline_y = cy_box + c_size * 0.88
                    tw_code.append((cx_box, baseline_y), code, font=font_bold, fontsize=c_size)

                # 4. Caixa de texto com especificações (Times New Roman 11 pt)
                specs = cell.get("lines") or []
                if specs:
                    sx = col_x + SPECS_EMU[0] / 12700.0 + INSET_H_PT
                    sy = cell_y + SPECS_EMU[1] / 12700.0 + INSET_V_PT
                    sw = SPECS_EMU[2] / 12700.0
                    sh = SPECS_EMU[3] / 12700.0
                    f_size, blocks = _layout_specs(specs, font_reg, font_bold, sw, sh)
                    line_h = f_size * 1.15
                    after_gap = 6.0 if f_size >= 10.5 else 4.5
                    cur_y = sy + f_size * 0.88
                    for block in blocks:
                        for w_line in block:
                            cur_x = sx
                            for seg_text, is_b in w_line:
                                f = font_bold if is_b else font_reg
                                tw_body.append((cur_x, cur_y), seg_text, font=f, fontsize=f_size)
                                cur_x += f.text_length(seg_text, fontsize=f_size)
                            cur_y += line_h
                        cur_y += after_gap

            tw_code.write_text(page, color=CODE_RGB)
            tw_body.write_text(page, color=(0.0, 0.0, 0.0))

        doc.subset_fonts()
        return doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
