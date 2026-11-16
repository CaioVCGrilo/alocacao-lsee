"""Geração de solicitações de auxílio / material de consumo (modelo EESC-USP) em .docx e PDF."""

from __future__ import annotations

import copy
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
BASE_DOCX_PATH = ASSETS_DIR / "solicitacao_base.docx"
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "img" / "eesc_usp_logo.png"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_XML = "http://www.w3.org/XML/1998/namespace"

PT_MONTHS = (
    "",
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)

MIN_TABLE_ROWS = 8
MAX_TABLE_ROWS = 15


def _w(tag: str) -> str:
    return f"{{{NS_W}}}{tag}"


def _parse_money(raw: Any) -> Optional[float]:
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v < 0:
            raise ValueError("O valor do material não pode ser negativo.")
        return v
    s = str(raw).strip().replace("R$", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError as exc:
        raise ValueError(f"Valor monetário inválido: '{raw}'.") from exc
    if v < 0:
        raise ValueError("O valor do material não pode ser negativo.")
    return v


def _parse_qty_number(raw: Any) -> Optional[float]:
    s = " ".join(str(raw or "").strip().split())
    if not s:
        return None
    m = re.match(r"^(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return v if v >= 0 else None


def _recompute_total(items: List[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    total = 0.0
    has_any_price = False
    for it in items:
        v = it.get("valor")
        if v is not None:
            has_any_price = True
            total += float(v)
    if not has_any_price:
        return None, ""
    rounded = round(total, 2)
    return rounded, format_brl(rounded)


def format_brl(val: Optional[float]) -> str:
    if val is None:
        return ""
    formatted = f"{val:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def format_request_date(raw_date: str) -> str:
    s = (raw_date or "").strip()
    if not s:
        return "São Carlos,______ de _____________ de _______"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        yr, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            return f"São Carlos, {dy:02d} de {PT_MONTHS[mo]} de {yr}"
    return f"São Carlos, {s}"


def normalize_material_request(
    body: Dict[str, Any],
    default_item_date: Optional[str] = None,
) -> Dict[str, Any]:
    orientador = str(body.get("orientador") or "").strip()
    laboratorio = str(body.get("laboratorio") or "").strip()
    data_solicitacao = str(body.get("data_solicitacao") or "").strip()
    justificativa = str(body.get("justificativa") or "").strip()

    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Lista de materiais inválida.")
    if len(raw_items) > MAX_TABLE_ROWS:
        raise ValueError(f"Máximo de {MAX_TABLE_ROWS} itens por solicitação.")

    items: List[Dict[str, Any]] = []
    for row in raw_items:
        if not isinstance(row, dict):
            raise ValueError("Item de material inválido.")
        mat = str(row.get("material") or "").strip()
        qtd = str(row.get("quantidade") or "").strip()
        pat = str(row.get("patrimonio") or "").strip()
        unit_num = _parse_money(row.get("valor_unitario"))
        val_num = _parse_money(row.get("valor"))
        if val_num is None and unit_num is not None:
            qty_num = _parse_qty_number(qtd)
            if qty_num is not None:
                val_num = round(unit_num * qty_num, 2)

        raw_item_date = str(row.get("adicionado_em") or "").strip()
        item_date = (
            raw_item_date
            if re.match(r"^\d{4}-\d{2}-\d{2}$", raw_item_date)
            else (default_item_date or "")
        )

        if not mat and not qtd and not pat and val_num is None and unit_num is None:
            continue
        if not mat:
            raise ValueError("Informe a descrição do material em todas as linhas preenchidas.")

        item_obj: Dict[str, Any] = {
            "material": mat,
            "quantidade": qtd,
            "patrimonio": pat,
            "valor": round(val_num, 2) if val_num is not None else None,
            "valor_fmt": format_brl(val_num),
        }
        if unit_num is not None:
            item_obj["valor_unitario"] = round(unit_num, 2)
        if item_date:
            item_obj["adicionado_em"] = item_date
        items.append(item_obj)

    if not items and not justificativa and not orientador:
        raise ValueError("Preencha ao menos um item de material antes de gerar ou salvar.")

    if data_solicitacao and not re.match(r"^\d{4}-\d{2}-\d{2}$", data_solicitacao):
        # Valida data livre razoável sem impedir texto curto
        if len(data_solicitacao) > 60:
            raise ValueError("Data inválida.")

    total_valor, total_fmt = _recompute_total(items)
    return {
        "orientador": orientador,
        "laboratorio": laboratorio,
        "data_solicitacao": data_solicitacao,
        "data_texto": format_request_date(data_solicitacao),
        "items": items,
        "total_valor": total_valor,
        "total_fmt": total_fmt,
        "justificativa": justificativa,
    }


def incorporate_material_request(
    existing_row: Dict[str, Any],
    body: Dict[str, Any],
    today_str: str,
) -> Dict[str, Any]:
    """Atualiza uma solicitação salva preservando as datas originais dos itens existentes."""
    import json

    fallback_date = str(existing_row.get("created_at") or "")[:10]
    try:
        raw_existing_items = json.loads(existing_row.get("items_json") or "[]")
    except Exception:
        raw_existing_items = []

    existing_items: List[Dict[str, Any]] = []
    if isinstance(raw_existing_items, list):
        for it in raw_existing_items:
            if not isinstance(it, dict):
                continue
            item_copy = dict(it)
            if not item_copy.get("adicionado_em") and re.match(r"^\d{4}-\d{2}-\d{2}$", fallback_date):
                item_copy["adicionado_em"] = fallback_date
            if item_copy.get("valor") is not None and not item_copy.get("valor_fmt"):
                item_copy["valor_fmt"] = format_brl(float(item_copy["valor"]))
            existing_items.append(item_copy)

    merge_mode = str(body.get("merge_mode") or "replace").strip().lower()
    if merge_mode == "append":
        new_req = normalize_material_request(body, default_item_date=today_str)
        combined = existing_items + new_req["items"]
        if len(combined) > MAX_TABLE_ROWS:
            raise ValueError(
                f"A solicitação combinada teria {len(combined)} itens (máximo: {MAX_TABLE_ROWS} por formulário)."
            )
        orientador = str(existing_row.get("orientador") or "").strip() or new_req["orientador"]
        laboratorio = str(existing_row.get("laboratorio") or "").strip() or new_req["laboratorio"]
        data_sol = str(existing_row.get("data_solicitacao") or "").strip() or new_req["data_solicitacao"]
        ex_just = str(existing_row.get("justificativa") or "").strip()
        nw_just = new_req["justificativa"]
        if not nw_just or nw_just in ex_just:
            justificativa = ex_just
        elif not ex_just:
            justificativa = nw_just
        else:
            justificativa = f"{ex_just}\n{nw_just}"
        total_valor, total_fmt = _recompute_total(combined)
        return {
            "orientador": orientador,
            "laboratorio": laboratorio,
            "data_solicitacao": data_sol,
            "data_texto": format_request_date(data_sol),
            "items": combined,
            "total_valor": total_valor,
            "total_fmt": total_fmt,
            "justificativa": justificativa,
        }

    return normalize_material_request(body, default_item_date=today_str)


def _load_pdf_fonts() -> Tuple[pymupdf.Font, pymupdf.Font]:
    win_fonts = Path(r"C:\Windows\Fonts")
    reg_path = win_fonts / "times.ttf"
    bold_path = win_fonts / "timesbd.ttf"
    if reg_path.is_file() and bold_path.is_file():
        return pymupdf.Font(fontfile=str(reg_path)), pymupdf.Font(fontfile=str(bold_path))
    return pymupdf.Font("tiro"), pymupdf.Font("tibo")


def _make_p(
    text: str,
    align: str = "left",
    bold: bool = False,
    sz_half_pt: int = 22,
    line_twips: int = 260,
) -> ET.Element:
    p = ET.Element(_w("p"))
    ppr = ET.SubElement(p, _w("pPr"))
    ET.SubElement(ppr, _w("spacing"), {_w("before"): "20", _w("after"): "20", _w("line"): str(line_twips), _w("lineRule"): "auto"})
    if align != "left":
        ET.SubElement(ppr, _w("jc"), {_w("val"): align})
    r = ET.SubElement(p, _w("r"))
    rpr = ET.SubElement(r, _w("rPr"))
    ET.SubElement(
        rpr,
        _w("rFonts"),
        {_w("ascii"): "Times New Roman", _w("hAnsi"): "Times New Roman", _w("cs"): "Times New Roman"},
    )
    if bold:
        ET.SubElement(rpr, _w("b"))
    ET.SubElement(rpr, _w("sz"), {_w("val"): str(sz_half_pt)})
    ET.SubElement(rpr, _w("szCs"), {_w("val"): str(sz_half_pt)})
    t = ET.SubElement(r, _w("t"))
    t.set(f"{{{NS_XML}}}space", "preserve")
    t.text = text
    return p


def _set_cell_text(
    tc: ET.Element,
    text: str,
    align: str = "left",
    bold: bool = False,
    sz_half_pt: int = 22,
) -> None:
    tc_pr = tc.find(_w("tcPr"))
    for p in list(tc.findall(_w("p"))):
        tc.remove(p)
    lines = text.splitlines() if text else [""]
    for ln in lines:
        tc.append(_make_p(ln, align=align, bold=bold, sz_half_pt=sz_half_pt))
    if tc_pr is not None and list(tc)[0] is not tc_pr:
        tc.remove(tc_pr)
        tc.insert(0, tc_pr)


def build_request_docx(req: Dict[str, Any]) -> bytes:
    """Preenche o template oficial solicitacao_base.docx mantendo o brasão e bordas USP/EESC."""
    if not BASE_DOCX_PATH.is_file():
        raise FileNotFoundError(f"Template base não encontrado: {BASE_DOCX_PATH}")

    with zipfile.ZipFile(BASE_DOCX_PATH, "r") as zin:
        doc_xml = zin.read("word/document.xml")
        other_entries = {
            name: zin.read(name) for name in zin.namelist() if name != "word/document.xml"
        }

    root = ET.fromstring(doc_xml)
    body = root.find(_w("body"))
    assert body is not None

    tables = body.findall(_w("tbl"))
    # tables[1]: Orientador / Laboratório
    id_rows = tables[1].findall(_w("tr"))
    for row_idx, val in ((0, req["orientador"]), (1, req["laboratorio"])):
        if val:
            tc = id_rows[row_idx].find(_w("tc"))
            ps = tc.findall(_w("p"))
            target_p = ps[-1] if ps else ET.SubElement(tc, _w("p"))
            r = ET.SubElement(target_p, _w("r"))
            rpr = ET.SubElement(r, _w("rPr"))
            ET.SubElement(
                rpr,
                _w("rFonts"),
                {_w("ascii"): "Times New Roman", _w("hAnsi"): "Times New Roman", _w("cs"): "Times New Roman"},
            )
            ET.SubElement(rpr, _w("sz"), {_w("val"): "24"})
            ET.SubElement(rpr, _w("szCs"), {_w("val"): "24"})
            t = ET.SubElement(r, _w("t"))
            t.set(f"{{{NS_XML}}}space", "preserve")
            t.text = f"  {val}"

    # tables[2]: MATERIAL SOLICITADO
    mat_tbl = tables[2]
    mat_trs = mat_tbl.findall(_w("tr"))
    template_item_tr = copy.deepcopy(mat_trs[2])
    total_tr = mat_trs[-1]
    items = req["items"]

    # Expande linhas caso haja mais que 8 itens
    needed_rows = max(MIN_TABLE_ROWS, len(items))
    current_data_rows = len(mat_trs) - 3  # exclui título, cabeçalho e Total
    while current_data_rows < needed_rows:
        new_tr = copy.deepcopy(template_item_tr)
        insert_pos = list(mat_tbl).index(total_tr)
        mat_tbl.insert(insert_pos, new_tr)
        current_data_rows += 1

    mat_trs = mat_tbl.findall(_w("tr"))
    for i in range(needed_rows):
        tr = mat_trs[2 + i]
        tcs = tr.findall(_w("tc"))
        item = items[i] if i < len(items) else None
        if item:
            _set_cell_text(tcs[0], item["material"], align="left", sz_half_pt=22)
            _set_cell_text(tcs[1], item["quantidade"], align="center", sz_half_pt=22)
            _set_cell_text(tcs[2], item["patrimonio"], align="center", sz_half_pt=22)
            _set_cell_text(tcs[3], item["valor_fmt"], align="right", sz_half_pt=22)

    if req["total_fmt"]:
        tot_tcs = total_tr.findall(_w("tc"))
        _set_cell_text(tot_tcs[3], req["total_fmt"], align="right", bold=True, sz_half_pt=24)

    # tables[3]: JUSTIFICATIVA
    just_tbl = tables[3]
    just_trs = just_tbl.findall(_w("tr"))
    if req["justificativa"]:
        just_tc = just_trs[1].find(_w("tc"))
        _set_cell_text(just_tc, req["justificativa"], align="left", sz_half_pt=23)

    # Parágrafo da data ("São Carlos,...")
    for p in body.findall(_w("p")):
        txt = "".join(t.text or "" for t in p.findall(f".//{_w('t')}"))
        if "São Carlos" in txt:
            ppr = p.find(_w("pPr"))
            for child in list(p):
                if child is not ppr:
                    p.remove(child)
            r = ET.SubElement(p, _w("r"))
            rpr = ET.SubElement(r, _w("rPr"))
            ET.SubElement(
                rpr,
                _w("rFonts"),
                {_w("ascii"): "Times New Roman", _w("hAnsi"): "Times New Roman", _w("cs"): "Times New Roman"},
            )
            ET.SubElement(rpr, _w("sz"), {_w("val"): "24"})
            t = ET.SubElement(r, _w("t"))
            t.text = req["data_texto"]
            break

    new_doc_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, content in other_entries.items():
            zout.writestr(name, content)
        zout.writestr("word/document.xml", new_doc_bytes)
    return out.getvalue()


def _draw_double_box(page: pymupdf.Page, rect: pymupdf.Rect) -> None:
    page.draw_rect(rect, color=(0, 0, 0), width=0.6)
    inner = pymupdf.Rect(rect.x0 + 1.8, rect.y0 + 1.8, rect.x1 - 1.8, rect.y1 - 1.8)
    page.draw_rect(inner, color=(0, 0, 0), width=0.6)


def _wrap_text(text: str, font: pymupdf.Font, fontsize: float, max_width: float) -> List[str]:
    lines: List[str] = []
    for raw_line in (text or "").splitlines() or [""]:
        words = raw_line.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            cand = f"{cur} {w}"
            if font.text_length(cand, fontsize=fontsize) <= max_width:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def build_request_pdf(req: Dict[str, Any]) -> bytes:
    """Renderiza a solicitação em PDF A4 fiel ao formulário oficial da EESC/USP."""
    reg_font, bold_font = _load_pdf_fonts()
    page_w = 595.3
    page_h = 841.9
    margin_x = 76.0
    usable_w = page_w - 2 * margin_x

    doc = pymupdf.open()
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_font(fontname="tmr_reg", fontbuffer=reg_font.buffer)
    page.insert_font(fontname="tmr_bold", fontbuffer=bold_font.buffer)

    y = 54.0
    if LOGO_PATH.is_file():
        logo_rect = pymupdf.Rect(margin_x + 4, y, margin_x + 56, y + 54.8)
        page.insert_image(logo_rect, filename=str(LOGO_PATH), keep_proportion=True)

    hx = margin_x + 74
    for idx, hdr_line in enumerate(
        (
            "UNIVERSIDADE DE SÃO PAULO",
            "ESCOLA DE ENGENHARIA DE SÃO CARLOS",
            "DEPARTAMENTO DE ENGENHARIA ELÉTRICA E DE COMPUTAÇÃO",
            "PROGRAMA DE PÓS-GRADUAÇÃO EM ENGENHARIA ELÉTRICA",
        )
    ):
        page.insert_text(
            pymupdf.Point(hx, y + 14 + idx * 11.5),
            hdr_line,
            fontname="tmr_reg",
            fontsize=9.5,
            color=(0, 0, 0),
        )

    y += 76.0
    for title_line in ("SOLICITAÇÃO DE AUXÍLIO", "PARA LABORATÓRIO DE PESQUISA"):
        tw = bold_font.text_length(title_line, fontsize=13.5)
        page.insert_text(
            pymupdf.Point((page_w - tw) / 2.0, y),
            title_line,
            fontname="tmr_bold",
            fontsize=13.5,
            color=(0, 0, 0),
        )
        y += 17.0

    y += 6.0
    # Caixa Orientador / Laboratório
    id_row_h = 26.0
    id_box = pymupdf.Rect(margin_x, y, margin_x + usable_w, y + id_row_h * 2)
    _draw_double_box(page, id_box)
    page.draw_line(
        pymupdf.Point(margin_x, y + id_row_h),
        pymupdf.Point(margin_x + usable_w, y + id_row_h),
        color=(0, 0, 0),
        width=0.6,
    )
    for idx, (lbl, val) in enumerate(
        (("Orientador:", req["orientador"]), ("Laboratório:", req["laboratorio"]))
    ):
        ty = y + idx * id_row_h + 18.0
        page.insert_text(pymupdf.Point(margin_x + 6, ty), lbl, fontname="tmr_bold", fontsize=11, color=(0, 0, 0))
        if val:
            lw = bold_font.text_length(lbl + " ", fontsize=11)
            page.insert_text(pymupdf.Point(margin_x + 6 + lw, ty), val, fontname="tmr_reg", fontsize=11, color=(0, 0, 0))

    y = id_box.y1 + 18.0

    # Tabela MATERIAL SOLICITADO
    col_ws = [usable_w * 0.44, usable_w * 0.18, usable_w * 0.20, usable_w * 0.18]
    col_xs = [margin_x]
    for cw in col_ws:
        col_xs.append(col_xs[-1] + cw)

    title_h = 20.0
    hdr_h = 20.0
    items = req["items"]
    n_rows = max(MIN_TABLE_ROWS, len(items))

    row_heights: List[float] = []
    wrapped_mats: List[List[str]] = []
    for i in range(n_rows):
        if i < len(items):
            lines = _wrap_text(items[i]["material"], reg_font, 10.5, col_ws[0] - 10)
        else:
            lines = [""]
        wrapped_mats.append(lines)
        row_heights.append(max(21.0, len(lines) * 13.0 + 8.0))
    total_h = 24.0

    tbl_total_h = title_h + hdr_h + sum(row_heights) + total_h
    tbl_rect = pymupdf.Rect(margin_x, y, margin_x + usable_w, y + tbl_total_h)
    _draw_double_box(page, tbl_rect)

    # Título MATERIAL SOLICITADO
    t_mat = "MATERIAL SOLICITADO"
    tmw = reg_font.text_length(t_mat, fontsize=12)
    page.insert_text(pymupdf.Point((page_w - tmw) / 2.0, y + 14.5), t_mat, fontname="tmr_reg", fontsize=12)
    cur_y = y + title_h
    page.draw_line(pymupdf.Point(margin_x, cur_y), pymupdf.Point(margin_x + usable_w, cur_y), width=0.6)

    # Cabeçalho das colunas
    headers = ("Material", "Quantidade", "Patrimônio", "Valor R$")
    for ci, h_txt in enumerate(headers):
        hw = bold_font.text_length(h_txt, fontsize=11.5)
        hx_c = col_xs[ci] + (col_ws[ci] - hw) / 2.0
        page.insert_text(pymupdf.Point(hx_c, cur_y + 14.5), h_txt, fontname="tmr_bold", fontsize=11.5)
    grid_top = cur_y
    cur_y += hdr_h
    page.draw_line(pymupdf.Point(margin_x, cur_y), pymupdf.Point(margin_x + usable_w, cur_y), width=0.6)

    # Linhas de itens
    for i in range(n_rows):
        rh = row_heights[i]
        item = items[i] if i < len(items) else None
        if item:
            for li, ln in enumerate(wrapped_mats[i]):
                page.insert_text(pymupdf.Point(col_xs[0] + 5, cur_y + 14.0 + li * 13.0), ln, fontname="tmr_reg", fontsize=10.5)
            for ci, txt in ((1, item["quantidade"]), (2, item["patrimonio"])):
                if txt:
                    cw_t = reg_font.text_length(txt, fontsize=10.5)
                    cx_t = col_xs[ci] + max(4.0, (col_ws[ci] - cw_t) / 2.0)
                    page.insert_text(pymupdf.Point(cx_t, cur_y + 14.0), txt, fontname="tmr_reg", fontsize=10.5)
            if item["valor_fmt"]:
                vw = reg_font.text_length(item["valor_fmt"], fontsize=10.5)
                page.insert_text(pymupdf.Point(col_xs[4] - vw - 5, cur_y + 14.0), item["valor_fmt"], fontname="tmr_reg", fontsize=10.5)
        cur_y += rh
        page.draw_line(pymupdf.Point(margin_x, cur_y), pymupdf.Point(margin_x + usable_w, cur_y), width=0.5)

    # Linhas verticais internas
    for cx in col_xs[1:4]:
        page.draw_line(pymupdf.Point(cx, grid_top), pymupdf.Point(cx, cur_y + total_h), width=0.5)

    # Linha Total
    tot_lbl = "Total"
    tlw = bold_font.text_length(tot_lbl, fontsize=13.5)
    page.insert_text(pymupdf.Point(col_xs[3] - tlw - 6, cur_y + 17.0), tot_lbl, fontname="tmr_bold", fontsize=13.5)
    if req["total_fmt"]:
        tvw = bold_font.text_length(req["total_fmt"], fontsize=11.5)
        page.insert_text(pymupdf.Point(col_xs[4] - tvw - 5, cur_y + 17.0), req["total_fmt"], fontname="tmr_bold", fontsize=11.5)

    y = tbl_rect.y1 + 18.0
    obs_lines = _wrap_text(
        "Obs.: Numero de patrimônios dos bens para tonners e suprimentos de informática",
        reg_font,
        11.0,
        usable_w,
    )
    for li, ln in enumerate(obs_lines):
        page.insert_text(pymupdf.Point(margin_x + 4, y + li * 14.0), ln, fontname="tmr_reg", fontsize=11.5, color=(0.9, 0.0, 0.0))

    y += len(obs_lines) * 14.0 + 14.0

    # Quadro JUSTIFICATIVA e rodapé
    just_lines = _wrap_text(req["justificativa"], reg_font, 11.0, usable_w - 14) if req["justificativa"] else []
    just_hdr_h = 24.0
    max_just_bottom = page_h - 102.0
    just_body_h = max(64.0, len(just_lines) * 14.5 + 16.0)
    just_rect = pymupdf.Rect(margin_x, y, margin_x + usable_w, min(max_just_bottom, y + just_hdr_h + just_body_h))
    _draw_double_box(page, just_rect)
    jlbl = "JUSTIFICATIVA"
    jlw = bold_font.text_length(jlbl, fontsize=11.5)
    page.insert_text(pymupdf.Point((page_w - jlw) / 2.0, y + 16.5), jlbl, fontname="tmr_bold", fontsize=11.5)
    page.draw_line(pymupdf.Point(margin_x, y + just_hdr_h), pymupdf.Point(margin_x + usable_w, y + just_hdr_h), width=0.6)
    for li, ln in enumerate(just_lines):
        ly = y + just_hdr_h + 15.0 + li * 14.5
        if ly < just_rect.y1 - 4.0:
            page.insert_text(pymupdf.Point(margin_x + 7, ly), ln, fontname="tmr_reg", fontsize=11.0)

    # Data e Assinatura
    y_foot = just_rect.y1 + 24.0
    page.insert_text(pymupdf.Point(margin_x + usable_w * 0.45, y_foot), req["data_texto"], fontname="tmr_reg", fontsize=11.0)
    y_sig = min(page_h - 28.0, y_foot + 44.0)
    sig_x0 = margin_x + usable_w * 0.50
    sig_x1 = margin_x + usable_w * 0.96
    page.draw_line(pymupdf.Point(sig_x0, y_sig), pymupdf.Point(sig_x1, y_sig), width=0.7)
    sig_lbl = "Assinatura"
    slw = reg_font.text_length(sig_lbl, fontsize=10.5)
    page.insert_text(pymupdf.Point((sig_x0 + sig_x1 - slw) / 2.0, y_sig + 13.0), sig_lbl, fontname="tmr_reg", fontsize=10.5)

    return doc.tobytes(deflate=True)
