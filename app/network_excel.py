"""Exportação e importação OOXML (.xlsx) para a lista de IPs / dispositivos de rede."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Tuple
from xml.sax.saxutils import escape

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

CATEGORY_ORDER = ["Computadores", "Notebook", "Impressoras", "Servidores", "Porta"]

_MAX_XLSX_RAW_BYTES = 5 * 1024 * 1024
_MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
_MAX_ZIP_ENTRIES = 100


def _natural_sort_key(text: str) -> Tuple[Any, ...]:
    parts = re.split(r"(\d+)", (text or "").strip().lower())
    out: List[Any] = []
    for p in parts:
        if not p:
            continue
        out.append((0, int(p)) if p.isdigit() else (1, p))
    return tuple(out)


def _col_name(col_idx_1based: int) -> str:
    n = col_idx_1based
    chars: List[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(ord("A") + rem))
    return "".join(reversed(chars))


def _col_letters(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def _normalize_equipment_name(name: str) -> str:
    clean = " ".join((name or "").strip().split())
    m = re.match(r"^pc\s+(\d+)$", clean, flags=re.IGNORECASE)
    if m:
        return f"PC {m.group(1).zfill(3) if len(m.group(1)) <= 3 else m.group(1)}"
    return clean


def _extract_dhcp_and_obs(raw_obs: str) -> Tuple[str, str]:
    """Separa 'IP pego DHCP: x.x.x.x' caso esteja em observacoes para preencher coluna DHCP."""
    obs = (raw_obs or "").strip()
    m = re.search(r"IP\s+pego\s+DHCP:\s*([^\s;|,]+)", obs, flags=re.IGNORECASE)
    if not m:
        return "", obs
    dhcp = m.group(1).strip()
    cleaned = (obs[: m.start()] + obs[m.end() :]).strip(" ;|,.-")
    return dhcp, cleaned


def build_network_xlsx(rows: List[Dict[str, Any]]) -> bytes:
    """Gera planilha .xlsx pronta para Excel/Calc mantendo a organização por categorias usada com a Roseli."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        cat = str(r.get("categoria") or "Computadores").strip() or "Computadores"
        grouped.setdefault(cat, []).append(r)

    ordered_cats = [c for c in CATEGORY_ORDER if c in grouped] + sorted(
        c for c in grouped if c not in CATEGORY_ORDER
    )

    sheet_rows: List[Tuple[int, List[Tuple[int, str, int]]]] = []
    r_idx = 1
    # Linha 1: título discreto + data de geração
    stamp = datetime.now().strftime("%d/%m/%Y")
    sheet_rows.append(
        (
            r_idx,
            [
                (1, f"LSEE · Lista de IPs e MACs (atualizada em {stamp})", 2),
                (2, "", 2),
                (3, "", 2),
                (4, "", 2),
                (5, "", 2),
            ],
        )
    )
    r_idx += 1

    # Linha 2: cabeçalhos principais compatíveis com Lista de IPs.xlsx
    sheet_rows.append(
        (
            r_idx,
            [
                (1, "Número", 1),
                (2, "Endereço IP", 1),
                (3, "ENDEREÇO do MAC", 1),
                (4, "IP pego DHCP", 1),
                (5, "Observações", 1),
            ],
        )
    )
    r_idx += 1

    for cat in ordered_cats:
        items = sorted(
            grouped[cat],
            key=lambda x: _natural_sort_key(str(x.get("equipamento") or "")),
        )
        # Linha de seção da categoria
        sheet_rows.append(
            (
                r_idx,
                [
                    (1, cat, 2),
                    (2, "", 2),
                    (3, "", 2),
                    (4, "", 2),
                    (5, "", 2),
                ],
            )
        )
        r_idx += 1

        for item in items:
            eq = str(item.get("equipamento") or "").strip()
            ip = str(item.get("ip") or "").strip()
            mac = str(item.get("mac") or "").strip()
            raw_obs = str(item.get("observacoes") or "").strip()
            dhcp_val, obs_val = _extract_dhcp_and_obs(raw_obs)
            if cat.lower().startswith("impress") and ip and not dhcp_val:
                dhcp_val = ip
            sheet_rows.append(
                (
                    r_idx,
                    [
                        (1, eq, 3),
                        (2, ip, 3),
                        (3, mac, 3),
                        (4, dhcp_val, 3),
                        (5, obs_val, 3),
                    ],
                )
            )
            r_idx += 1

    rows_xml_parts: List[str] = []
    for row_num, cells in sheet_rows:
        cell_xml = []
        for col_idx, text, style_id in cells:
            ref = f"{_col_name(col_idx)}{row_num}"
            escaped = escape(text or "")
            cell_xml.append(
                f'<c r="{ref}" t="inlineStr" s="{style_id}"><is><t xml:space="preserve">{escaped}</t></is></c>'
            )
        rows_xml_parts.append(f'<row r="{row_num}">{"".join(cell_xml)}</row>')

    sheet1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetViews><sheetView workbookViewId=\"0\"/></sheetViews>"
        "<sheetFormatPr defaultRowHeight=\"16.5\"/>"
        "<cols>"
        '<col min="1" max="1" width="32" customWidth="1"/>'
        '<col min="2" max="2" width="30" customWidth="1"/>'
        '<col min="3" max="3" width="22" customWidth="1"/>'
        '<col min="4" max="4" width="18" customWidth="1"/>'
        '<col min="5" max="5" width="34" customWidth="1"/>'
        "</cols>"
        f'<sheetData>{"".join(rows_xml_parts)}</sheetData>'
        "</worksheet>"
    )

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="11"/><name val="Calibri"/><color rgb="FF1E293B"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/><color rgb="FF1B365D"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1B365D"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFCBD5E1"/></left>
      <right style="thin"><color rgb="FFCBD5E1"/></right>
      <top style="thin"><color rgb="FFCBD5E1"/></top>
      <bottom style="thin"><color rgb="FFCBD5E1"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"><alignment vertical="center"/></xf>
  </cellXfs>
</styleSheet>"""

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

    root_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Lista_de_IPs" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""

    wb_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1_xml)
    return buf.getvalue()


def _safe_xml_from_zip(zf: zipfile.ZipFile, member_path: str) -> ET.Element:
    """Lê membro XML de um ZIP validando traversal, tamanho descompactado e DTD/entidades."""
    clean = posixpath.normpath(member_path.lstrip("/"))
    if clean.startswith("..") or ".." in clean.split("/") or clean not in zf.namelist():
        raise ValueError("Estrutura interna da planilha inválida.")
    info = zf.getinfo(clean)
    if info.file_size > _MAX_XLSX_UNCOMPRESSED_BYTES:
        raise ValueError("Planilha excede o limite de tamanho permitido.")
    data = zf.read(clean)
    if len(data) > _MAX_XLSX_UNCOMPRESSED_BYTES:
        raise ValueError("Planilha excede o limite de tamanho permitido.")
    if b"<!" in data:
        upper = data.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise ValueError("XML com declarações DTD/entidades externas não é permitido.")
    return ET.fromstring(data)


def _read_sheets_from_bytes(raw_bytes: bytes) -> List[Tuple[str, Dict[int, Dict[str, str]]]]:
    if len(raw_bytes) > _MAX_XLSX_RAW_BYTES:
        raise ValueError("Arquivo .xlsx excede o limite máximo de 5 MB.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("Arquivo .xlsx inválido ou corrompido.") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise ValueError("Arquivo .xlsx excede a quantidade máxima de entradas internas.")
        if sum(i.file_size for i in infos) > _MAX_XLSX_UNCOMPRESSED_BYTES:
            raise ValueError("Conteúdo descompactado da planilha excede o limite de 20 MB.")

        strings: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sst = _safe_xml_from_zip(zf, "xl/sharedStrings.xml")
            for si in sst.findall(f"{{{NS_MAIN}}}si"):
                strings.append("".join(t.text or "" for t in si.findall(f".//{{{NS_MAIN}}}t")))

        wb = _safe_xml_from_zip(zf, "xl/workbook.xml")
        rels = {
            r.attrib["Id"]: r.attrib["Target"]
            for r in _safe_xml_from_zip(zf, "xl/_rels/workbook.xml.rels")
        }

        out: List[Tuple[str, Dict[int, Dict[str, str]]]] = []
        for sh in wb.findall(f".//{{{NS_MAIN}}}sheet"):
            name = sh.attrib["name"]
            target = rels[sh.attrib[f"{{{NS_REL}}}id"]]
            tpath = target.lstrip("/") if target.startswith(("xl/", "/xl/")) else "xl/" + target.lstrip("/")
            root = _safe_xml_from_zip(zf, tpath)

            rows_map: Dict[int, Dict[str, str]] = {}
            for row in root.findall(f".//{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row"):
                rnum = int(row.attrib["r"])
                rmap: Dict[str, str] = {}
                for cell in row.findall(f"{{{NS_MAIN}}}c"):
                    col = _col_letters(cell.attrib.get("r", ""))
                    ctype = cell.attrib.get("t")
                    v_el = cell.find(f"{{{NS_MAIN}}}v")
                    is_el = cell.find(f"{{{NS_MAIN}}}is")
                    val: str | None = None
                    if is_el is not None:
                        val = "".join(t.text or "" for t in is_el.findall(f".//{{{NS_MAIN}}}t"))
                    elif v_el is not None and v_el.text is not None:
                        val = strings[int(v_el.text)] if ctype == "s" else v_el.text
                    if val is not None:
                        cleaned = " ".join(str(val).strip().split())
                        if cleaned:
                            rmap[col] = cleaned
                if rmap:
                    rows_map[rnum] = rmap
            out.append((name, rows_map))
        return out


def _find_header_row(rows_map: Dict[int, Dict[str, str]]) -> Tuple[int, Dict[str, str]] | None:
    for rnum in sorted(rows_map):
        if rnum > 15:
            break
        row = rows_map[rnum]
        vals_lower = {col: v.lower() for col, v in row.items()}
        col_eq = next((c for c, v in vals_lower.items() if any(k in v for k in ("número", "numero", "equipamento"))), "")
        col_ip = next((c for c, v in vals_lower.items() if "ip" in v and "dhcp" not in v), "")
        if col_eq and col_ip:
            col_mac = next((c for c, v in vals_lower.items() if "mac" in v), "")
            col_dhcp = next((c for c, v in vals_lower.items() if "dhcp" in v), "")
            col_obs = next((c for c, v in vals_lower.items() if "observ" in v), "")
            col_cat = next((c for c, v in vals_lower.items() if "categ" in v), "")
            return rnum, {
                "cat": col_cat,
                "eq": col_eq,
                "ip": col_ip,
                "mac": col_mac,
                "dhcp": col_dhcp,
                "obs": col_obs,
            }
    return None


def parse_network_xlsx(raw_bytes: bytes) -> Dict[str, Any]:
    """Lê a aba mais recente de IPs de uma planilha (formato Roseli ou exportado do sistema)."""
    sheets = _read_sheets_from_bytes(raw_bytes)
    if not sheets:
        raise ValueError("Nenhuma aba encontrada na planilha.")

    candidates: List[Tuple[int, str, Dict[int, Dict[str, str]], int, Dict[str, str]]] = []
    for name, rows_map in sheets:
        found = _find_header_row(rows_map)
        if not found:
            continue
        hdr_row, col_map = found
        nl = name.lower()
        score = 0
        if "2sem" in nl or "atual" in nl or "lista_de_ips" in nl:
            score += 100
        if "até" in nl or "ate_" in nl or "antig" in nl:
            score -= 20
        score += len(rows_map)
        candidates.append((score, name, rows_map, hdr_row, col_map))

    if not candidates:
        raise ValueError(
            "Não encontrei cabeçalho com colunas 'Número'/'Equipamento' e 'Endereço IP' na planilha."
        )

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, sheet_name, rows_map, hdr_row, cols = candidates[0]

    known_sections = {
        "computadores": "Computadores",
        "computador": "Computadores",
        "notebook": "Notebook",
        "notebooks": "Notebook",
        "impressoras": "Impressoras",
        "impressora": "Impressoras",
        "servidores": "Servidores",
        "servidor": "Servidores",
        "porta": "Porta",
        "rede": "Rede",
        "roteadores": "Roteadores",
        "switches": "Switches",
    }

    current_cat = "Computadores"
    base_cols = dict(cols)
    records: List[Dict[str, str]] = []
    counts_by_cat: Dict[str, int] = {}

    for rnum in sorted(rows_map):
        if rnum <= hdr_row:
            continue
        row = rows_map[rnum]
        raw_eq = row.get(cols["eq"], "").strip()
        raw_cat = row.get(cols["cat"], "").strip() if cols["cat"] else ""
        ip = row.get(cols["ip"], "").strip()
        mac = row.get(cols["mac"], "").strip() if cols["mac"] else ""

        if not raw_eq:
            continue
        if not raw_cat and not ip and not mac and raw_eq.lower() in known_sections:
            remaining = {c: v for c, v in row.items() if c != cols["eq"] and v.strip()}
            if not remaining or all(
                any(k in v.lower() for k in ("dhcp", "observ"))
                for v in remaining.values()
            ):
                current_cat = known_sections[raw_eq.lower()]
                cols = dict(base_cols)
                for c, v in remaining.items():
                    vl = v.lower()
                    if "dhcp" in vl and not cols["dhcp"]:
                        cols["dhcp"] = c
                    elif "observ" in vl and not cols["obs"]:
                        cols["obs"] = c
                continue

        dhcp = row.get(cols["dhcp"], "").strip() if cols["dhcp"] else ""
        obs = row.get(cols["obs"], "").strip() if cols["obs"] else ""

        cat = raw_cat or current_cat
        eq = _normalize_equipment_name(raw_eq)
        if dhcp and dhcp != ip and "dhcp" not in obs.lower():
            obs = f"IP pego DHCP: {dhcp}" if not obs else f"{obs}; IP pego DHCP: {dhcp}"

        records.append(
            {
                "categoria": cat,
                "equipamento": eq,
                "ip": ip,
                "mac": mac,
                "observacoes": obs,
            }
        )
        counts_by_cat[cat] = counts_by_cat.get(cat, 0) + 1

    if not records:
        raise ValueError("Nenhum equipamento encontrado na planilha selecionada.")

    return {
        "sheet_name": sheet_name,
        "records": records,
        "counts_by_cat": counts_by_cat,
    }
