"""Importador direto do arquivo .xlsx para o MySQL usando apenas stdlib."""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _col_letters(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha())


def _excel_date_to_iso(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    try:
        num = float(s)
        if 20000 <= num <= 80000:
            dt = datetime(1899, 12, 30) + timedelta(days=int(num))
            return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return s


def _to_float(val: Any) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).strip())
    except ValueError:
        return None


def _to_int(val: Any) -> Optional[int]:
    if val is None or str(val).strip() == "":
        return None
    try:
        return int(float(str(val).strip()))
    except ValueError:
        return None


_LOCATION_ALIASES = {
    "Sala de reunião": "Sala de Reuniões",
    "Sala do servidor e equipamentos": "Sala do Servidor",
}


def _normalize_location(val: Any) -> str:
    raw = (str(val) if val is not None else "").strip()
    return _LOCATION_ALIASES.get(raw, raw)


def _read_xlsx_sheets(xlsx_path: Path) -> Dict[str, Dict[int, Dict[str, str]]]:
    """Lê todas as abas de um .xlsx retornando {nome_aba: {linha: {col: valor}}}."""
    with zipfile.ZipFile(xlsx_path, "r") as zf:
        strings: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sst.findall("s:si", NS):
                strings.append("".join(t.text or "" for t in si.findall(".//s:t", NS)))

        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = {
            r.attrib["Id"]: r.attrib["Target"]
            for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        }

        result: Dict[str, Dict[int, Dict[str, str]]] = {}
        rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for sh in wb.findall(".//s:sheet", NS):
            name = sh.attrib["name"]
            target = rels[sh.attrib[rel_attr]]
            tpath = target.lstrip("/") if target.startswith(("xl/", "/xl/")) else "xl/" + target.lstrip("/")
            root = ET.fromstring(zf.read(tpath))

            rows_map: Dict[int, Dict[str, str]] = {}
            for row in root.findall(".//s:sheetData/s:row", NS):
                rnum = int(row.attrib["r"])
                rmap: Dict[str, str] = {}
                for cell in row.findall("s:c", NS):
                    ref = cell.attrib.get("r", "")
                    col = _col_letters(ref)
                    ctype = cell.attrib.get("t")
                    v_el = cell.find("s:v", NS)
                    is_el = cell.find("s:is", NS)
                    val: Optional[str] = None
                    if v_el is not None and v_el.text is not None:
                        val = strings[int(v_el.text)] if ctype == "s" else v_el.text
                    elif is_el is not None:
                        val = "".join(t.text or "" for t in is_el.findall(".//s:t", NS))
                    if val is not None:
                        rmap[col] = val
                if rmap:
                    rows_map[rnum] = rmap
            result[name] = rows_map
        return result


def import_xlsx_into_db(conn: Any, xlsx_path: Path) -> Dict[str, int]:
    """Substitui os dados operacionais no MySQL pelos da planilha .xlsx (sem dados confidenciais)."""
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Planilha não encontrada: {xlsx_path}")

    sheets = _read_xlsx_sheets(xlsx_path)

    # Preserva preferência de privacidade e parâmetros de armazenamento se já existirem
    existing_params = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT `key`, `value` FROM system_params WHERE `key` IN ('public_anonymize_names', 'armaz_peso_hd', 'armaz_escala_log')"
        ).fetchall()
    }
    anon_value = existing_params.get("public_anonymize_names", "Não")
    armaz_peso_hd_value = existing_params.get("armaz_peso_hd", "0.25")
    armaz_escala_value = existing_params.get("armaz_escala_log", "60")

    # Limpa tabelas de dados (preserva admin_config e audit_log)
    for tbl in (
        "system_params",
        "score_thresholds",
        "categories",
        "processors",
        "computers",
        "people",
        "allocations",
        "monitors",
        "network_devices",
        "upgrade_decisions",
    ):
        conn.execute(f"DELETE FROM {tbl}")

    counts: Dict[str, int] = {}

    # 1. Parâmetros
    p_sheet = sheets.get("Parâmetros", {})
    param_rows = [
        ("diferenca_minima_upgrade", "Diferença mínima para upgrade", p_sheet.get(5, {}).get("B", "3"), "regras"),
        ("recusa_altera_prioridade", "Recusa altera prioridade?", p_sheet.get(6, {}).get("B", "Não"), "regras"),
        ("egresso_participa_upgrade", "Egresso participa de upgrade?", p_sheet.get(7, {}).get("B", "Não"), "regras"),
        ("pc_especial_cascata", "PC Especial participa da cascata?", p_sheet.get(8, {}).get("B", "Não"), "regras"),
        ("pc_compartilhado_ic_participa", "PC Compartilhado IC participa?", p_sheet.get(9, {}).get("B", "Não"), "regras"),
        ("pc_sala_reuniao_participa", "PC Sala de Reunião participa?", p_sheet.get(10, {}).get("B", "Não"), "regras"),
        ("peso_cpu", "Peso CPU", str(_to_float(p_sheet.get(5, {}).get("E")) or 0.45), "pesos"),
        ("peso_ram", "Peso RAM", str(_to_float(p_sheet.get(6, {}).get("E")) or 0.30), "pesos"),
        ("peso_armazenamento", "Peso Armazenamento", str(_to_float(p_sheet.get(7, {}).get("E")) or 0.225), "pesos"),
        ("peso_gpu", "Peso GPU", str(_to_float(p_sheet.get(8, {}).get("E")) or 0.025), "pesos"),
        ("armaz_peso_hd", "Peso relativo do HD (Armazenamento)", armaz_peso_hd_value, "armazenamento"),
        ("armaz_escala_log", "Fator de suavização log (GB_eq)", armaz_escala_value, "armazenamento"),
        ("score_maximo", "Score máximo componente", p_sheet.get(22, {}).get("H", "10"), "armazenamento"),
        ("public_anonymize_names", "Anonimizar nomes na visão pública (exibir ID Pessoa)?", anon_value, "privacidade"),
    ]
    conn.executemany(
        "INSERT INTO system_params(`key`, label, `value`, category) VALUES (?, ?, ?, ?)",
        param_rows,
    )

    thresh_rows = []
    for rnum in range(15, 24):
        row = p_sheet.get(rnum, {})
        if _to_float(row.get("A")) is not None and _to_float(row.get("B")) is not None:
            thresh_rows.append(("ram", float(row["A"]), float(row["B"])))
        if _to_float(row.get("D")) is not None and _to_float(row.get("E")) is not None:
            thresh_rows.append(("hd", float(row["D"]), float(row["E"])))
        if _to_float(row.get("G")) is not None and _to_float(row.get("H")) is not None:
            thresh_rows.append(("ssd", float(row["G"]), float(row["H"])))
    conn.executemany(
        "INSERT INTO score_thresholds(metric, min_value, score) VALUES (?, ?, ?)",
        thresh_rows,
    )

    cat_rows = []
    for rnum in range(15, 20):
        row = p_sheet.get(rnum, {})
        cname = (row.get("M") or "").strip()
        if cname:
            grp = _to_int(row.get("N"))
            part = 1 if (row.get("O") or "").strip() == "Sim" else 0
            prec = 1 if (row.get("P") or "").strip() == "Sim" else 0
            cat_rows.append((cname, grp, part, prec))
    conn.executemany(
        "INSERT INTO categories(name, priority_group, participa_upgrade, precisa_pc) VALUES (?, ?, ?, ?)",
        cat_rows,
    )
    counts["parametros"] = len(param_rows)

    # 2. Processadores
    proc_sheet = sheets.get("Processadores", {})
    proc_count = 0
    for rnum in sorted(proc_sheet):
        if rnum < 3:
            continue
        row = proc_sheet[rnum]
        name = (row.get("A") or "").strip()
        threads = _to_int(row.get("B"))
        multi = _to_float(row.get("C"))
        single = _to_float(row.get("D"))
        if not name or threads is None or multi is None or single is None:
            continue
        indice_cpu = multi + single * threads
        conn.execute(
            """
            INSERT INTO processors(name, threads, multi, single, indice_cpu, fonte, data_referencia, observacoes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                threads,
                multi,
                single,
                indice_cpu,
                (row.get("F") or "").strip(),
                _excel_date_to_iso(row.get("G")),
                (row.get("H") or "").strip(),
            ),
        )
        proc_count += 1
    counts["processadores"] = proc_count

    # 3. Computadores
    pc_sheet = sheets.get("Computadores", {})
    pc_count = 0
    for rnum in sorted(pc_sheet):
        if rnum < 3:
            continue
        row = pc_sheet[rnum]
        if not any((row.get(k) or "").strip() for k in "ABCDEFGHIJK"):
            continue
        conn.execute(
            """
            INSERT INTO computers(
                id_pc, patrimonio, processor_name, ram_gb, ssd_gb, hd_gb,
                possui_gpu, tipo_de_uso, status_manual, localizacao, observacoes,
                score_cpu_legado, score_armaz_legado
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (row.get("A") or "").strip() or None,
                (row.get("B") or "").strip(),
                (row.get("C") or "").strip(),
                _to_float(row.get("D")),
                _to_float(row.get("E")),
                _to_float(row.get("F")),
                (row.get("G") or "").strip(),
                (row.get("H") or "").strip(),
                (row.get("AA") or "").strip(),
                _normalize_location(row.get("J")),
                (row.get("K") or "").strip(),
                _to_float(row.get("U")),
                _to_float(row.get("V")),
            ),
        )
        pc_count += 1
    counts["computadores"] = pc_count

    # 4. Pessoas
    ppl_sheet = sheets.get("Pessoas", {})
    ppl_count = 0
    for rnum in sorted(ppl_sheet):
        if rnum < 3:
            continue
        row = ppl_sheet[rnum]
        pid = (row.get("A") or "").strip()
        nome = (row.get("B") or "").strip()
        if not pid and not nome:
            continue
        conn.execute(
            """
            INSERT INTO people(
                id_pessoa, nome, categoria, ano_entrada, semestre_entrada,
                situacao, data_saida, observacoes, data_ultimo_upgrade
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                nome,
                (row.get("C") or "").strip(),
                _to_int(row.get("D")),
                _to_int(row.get("E")),
                (row.get("F") or "").strip(),
                _excel_date_to_iso(row.get("G")),
                (row.get("H") or "").strip(),
                _excel_date_to_iso(row.get("U")),
            ),
        )
        ppl_count += 1
    counts["pessoas"] = ppl_count

    # 5. Alocações
    aloc_sheet = sheets.get("Alocações", {})
    aloc_count = 0
    for rnum in sorted(aloc_sheet):
        if rnum < 3:
            continue
        row = aloc_sheet[rnum]
        pessoa = (row.get("B") or "").strip()
        pc_code = (row.get("D") or "").strip()
        if not pessoa and not pc_code:
            continue
        id_aloc = (row.get("A") or "").strip() or f"ALO{rnum - 2:04d}"
        conn.execute(
            """
            INSERT INTO allocations(
                id_alocacao, pessoa_nome, id_pessoa, pc_id_pc,
                data_inicio, data_fim, tipo, motivo_inicio, motivo_fim,
                observacoes, origem
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id_aloc,
                pessoa,
                (row.get("C") or "").strip(),
                pc_code,
                _excel_date_to_iso(row.get("E")),
                _excel_date_to_iso(row.get("F")),
                (row.get("G") or "").strip(),
                (row.get("H") or "").strip(),
                (row.get("I") or "").strip(),
                (row.get("J") or "").strip(),
                (row.get("L") or "").strip(),
            ),
        )
        aloc_count += 1
    counts["alocacoes"] = aloc_count

    # 6. Monitores
    mon_sheet = sheets.get("Monitores", {})
    mon_count = 0
    for rnum in sorted(mon_sheet):
        if rnum < 3:
            continue
        row = mon_sheet[rnum]
        id_mon = (row.get("A") or "").strip()
        marca = (row.get("C") or "").strip()
        if not id_mon and not marca:
            continue
        conn.execute(
            """
            INSERT INTO monitors(
                id_monitor, patrimonio, marca_modelo, tamanho,
                status, localizacao, associado_pc, observacoes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id_mon or f"MON{rnum - 2:03d}",
                (row.get("B") or "").strip(),
                marca,
                (row.get("D") or "").strip(),
                (row.get("E") or "").strip(),
                _normalize_location(row.get("F")),
                (row.get("G") or "").strip(),
                (row.get("I") or "").strip(),
            ),
        )
        mon_count += 1
    counts["monitores"] = mon_count

    # 7. Rede
    rede_sheet = sheets.get("Rede", {})
    rede_count = 0
    for rnum in sorted(rede_sheet):
        if rnum < 4:
            continue
        row = rede_sheet[rnum]
        cat = (row.get("A") or "").strip()
        eq = (row.get("B") or "").strip()
        if not cat and not eq:
            continue
        conn.execute(
            """
            INSERT INTO network_devices(categoria, equipamento, ip, mac, observacoes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                cat,
                eq,
                (row.get("C") or "").strip(),
                (row.get("D") or "").strip(),
                (row.get("E") or "").strip(),
            ),
        )
        rede_count += 1
    counts["rede"] = rede_count

    # 8. Licenças Office
    conn.execute(
        "INSERT INTO audit_log(timestamp, action, target, details) VALUES (?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            "IMPORT_XLSX",
            xlsx_path.name,
            f"Importação concluída: {counts}",
        ),
    )
    return counts
