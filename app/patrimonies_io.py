"""Importação e exportação CSV para o cadastro de patrimônios do LSEE."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

_MAX_CSV_FILE_BYTES = 5 * 1024 * 1024


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_header(header: str) -> str:
    clean = _strip_accents(header or "").lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", clean).strip("_")


def _clean_text(val: Any) -> str:
    if val is None:
        return ""
    return " ".join(str(val).strip().split())


def parse_date_to_iso(raw: Any) -> str:
    """Normaliza datas DD/MM/AAAA ou AAAA-MM-DD para o formato ISO AAAA-MM-DD."""
    s = _clean_text(raw)
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return s
    return s


def format_iso_date_br(iso_date: Any) -> str:
    """Formata AAAA-MM-DD para DD/MM/AAAA."""
    s = _clean_text(iso_date)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def parse_brl_currency(raw: Any) -> Optional[float]:
    """Converte valores monetários em formato brasileiro (1.200,50) ou internacional (1200.50)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return round(val, 2) if val >= 0 else None
    s = _clean_text(raw)
    if not s:
        return None
    s = re.sub(r"R\$", "", s, flags=re.IGNORECASE).strip()
    s = s.replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = float(s)
    except ValueError as exc:
        raise ValueError(f"Valor monetário inválido: '{raw}'.") from exc
    if val < 0:
        raise ValueError("O valor pago pelo item não pode ser negativo.")
    return round(val, 2)


def format_brl_currency(val: Any) -> str:
    """Formata número float no padrão monetário brasileiro sem símbolo (ex.: 1.200,50)."""
    if val is None or val == "":
        return ""
    try:
        num = float(val)
    except (TypeError, ValueError):
        return ""
    formatted = f"{num:,.2f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def shorten_patrimony_location(raw: str) -> str:
    """Extrai um local curto (número da sala + descrição curta) de endereços extensos do Mercúrio/USP."""
    text = _clean_text(raw)
    if not text:
        return ""

    # Se não parece ser o endereço completo padrão da USP, preserva como está
    if "Av." not in text and "Bloco:" not in text and "Local:" not in text:
        return text

    code_match = re.match(r"^\((\d+)\)", text)
    room_code = code_match.group(1) if code_match else ""

    bloco_match = re.search(r"Bloco:\s*([^,]+)", text, flags=re.IGNORECASE)
    bloco = _clean_text(bloco_match.group(1)) if bloco_match else ""

    andar_match = re.search(r"Andar:\s*([^,]+)", text, flags=re.IGNORECASE)
    andar_raw = _clean_text(andar_match.group(1)) if andar_match else ""
    andar_num = re.search(r"(\d+)", andar_raw)
    andar = f"{andar_num.group(1)}º andar" if andar_num else ""

    local_match = re.search(r"Local:\s*(.+)$", text, flags=re.IGNORECASE)
    desc = ""
    if local_match:
        local_tail = _clean_text(local_match.group(1))
        parts = [p.strip() for p in local_tail.split(",") if p.strip()]
        if len(parts) >= 2:
            desc = parts[-1]
        elif parts:
            desc = parts[0]

    if desc:
        desc_lower = desc.lower()
        if "laboratório de pesquisa" in desc_lower or "laboratorio de pesquisa" in desc_lower:
            desc = "Lab. de pesquisa"
        elif desc_lower == "sala de trabalho":
            desc = "Sala de trabalho"
        else:
            desc = desc[0].upper() + desc[1:]

    context = ""
    if bloco and bloco.upper() != "SEL":
        context = f"Bloco {bloco}"
    elif bloco and andar:
        context = f"{bloco} · {andar}"
    elif bloco:
        context = bloco
    elif andar:
        context = andar

    if room_code:
        head = f"Sala {room_code}"
        if desc and context:
            return f"{head} — {desc} ({context})"
        if desc:
            return f"{head} — {desc}"
        if context:
            return f"{head} ({context})"
        return head

    return desc or text


def _build_description_from_usp_row(norm_row: Dict[str, str], nome_item: str) -> str:
    """Monta uma breve descrição a partir de Marca, Modelo, Tipo e Nº de Série (ou ClasseMat)."""
    marca = _clean_text(norm_row.get("marca"))
    modelo = _clean_text(norm_row.get("modelo"))
    tipo = _clean_text(norm_row.get("tipo"))
    serie = _clean_text(norm_row.get("no_serie") or norm_row.get("n_serie") or norm_row.get("numero_serie"))

    parts: List[str] = []
    seen_lower: set[str] = set()
    nome_lower = nome_item.lower()
    for val in (marca, modelo, tipo):
        if not val:
            continue
        vl = val.lower()
        if vl in seen_lower or vl == nome_lower:
            continue
        seen_lower.add(vl)
        parts.append(val)

    if serie:
        parts.append(f"S/N: {serie}")

    if parts:
        return " · ".join(parts)

    fallback = _clean_text(norm_row.get("classemat") or norm_row.get("grupomat"))
    if fallback and fallback.lower() != nome_lower:
        return fallback
    return ""


def _decode_csv_bytes(raw_bytes: bytes) -> str:
    if len(raw_bytes) > _MAX_CSV_FILE_BYTES:
        raise ValueError("Arquivo CSV excede o limite máximo de 5 MB.")
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Não foi possível decodificar o arquivo CSV (use UTF-8 ou ANSI/Windows-1252).")


def _detect_delimiter(first_line: str) -> str:
    semi = first_line.count(";")
    comma = first_line.count(",")
    if semi >= comma and semi > 0:
        return ";"
    if comma > 0:
        return ","
    return ";"


def _patrimony_key(numero: Any) -> str:
    return _clean_text(numero).lower()


def parse_patrimonies_csv(raw_bytes: bytes, source_name: str = "") -> List[Dict[str, Any]]:
    """Lê CSV de patrimônios (formato relatório USP/Mercúrio ou exportação do próprio painel)."""
    text = _decode_csv_bytes(raw_bytes)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("O arquivo CSV está vazio ou não possui registros.")

    delimiter = _detect_delimiter(lines[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("Cabeçalho CSV não identificado.")

    header_map = {fn: _norm_header(fn) for fn in reader.fieldnames if fn is not None}
    norm_headers = set(header_map.values())

    pat_keys = ("no_patrimonio", "numero_patrimonio", "numero_do_patrimonio", "n_patrimonio", "patrimonio")
    name_keys = ("nome", "nome_item", "nome_do_item", "item")
    if not any(k in norm_headers for k in pat_keys):
        raise ValueError("Coluna 'No.Patrimônio' (ou 'numero_patrimonio') não encontrada no CSV.")
    if not any(k in norm_headers for k in name_keys):
        raise ValueError("Coluna 'Nome' (ou 'nome_item') não encontrada no CSV.")

    records: List[Dict[str, Any]] = []
    seen_lines: Dict[str, List[int]] = {}
    display_num: Dict[str, str] = {}
    for line_num, raw_row in enumerate(reader, start=2):
        norm_row: Dict[str, str] = {}
        for orig_k, val in raw_row.items():
            if orig_k is None:
                continue
            nk = header_map.get(orig_k) or _norm_header(orig_k)
            norm_row[nk] = _clean_text(val)

        numero = next((norm_row[k] for k in pat_keys if norm_row.get(k)), "")
        nome_item = next((norm_row[k] for k in name_keys if norm_row.get(k)), "")
        if not numero or not nome_item:
            continue

        key = _patrimony_key(numero)
        display_num.setdefault(key, numero)
        seen_lines.setdefault(key, []).append(line_num)

        direct_desc = next(
            (
                norm_row[k]
                for k in ("descricao", "breve_descricao", "descricao_breve")
                if norm_row.get(k)
            ),
            "",
        )
        descricao = direct_desc or _build_description_from_usp_row(norm_row, nome_item)

        responsavel = next((norm_row[k] for k in ("responsavel",) if norm_row.get(k)), "")
        data_inc = parse_date_to_iso(
            next(
                (
                    norm_row[k]
                    for k in ("data_incorporacao", "data_de_incorporacao", "dt_incorporacao")
                    if norm_row.get(k)
                ),
                "",
            )
        )
        fim_garantia = parse_date_to_iso(
            next(
                (
                    norm_row[k]
                    for k in ("venc_garantia", "fim_garantia", "fim_da_garantia", "garantia")
                    if norm_row.get(k)
                ),
                "",
            )
        )
        valor_raw = next(
            (
                norm_row[k]
                for k in ("valor", "valor_pago", "valor_pago_pelo_item")
                if norm_row.get(k)
            ),
            "",
        )
        valor_pago = parse_brl_currency(valor_raw) if valor_raw else None
        origem = next((norm_row[k] for k in ("origem", "origem_do_item") if norm_row.get(k)), "")
        local_raw = next(
            (
                norm_row[k]
                for k in ("local_item", "local_do_item", "local", "endereco_local")
                if norm_row.get(k)
            ),
            "",
        )
        local_item = shorten_patrimony_location(local_raw)

        records.append(
            {
                "numero_patrimonio": numero,
                "nome_item": nome_item,
                "descricao": descricao,
                "responsavel": responsavel,
                "data_incorporacao": data_inc,
                "fim_garantia": fim_garantia,
                "valor_pago": valor_pago,
                "origem": origem,
                "local_item": local_item,
            }
        )

    dups = [(display_num[k], lns) for k, lns in seen_lines.items() if len(lns) > 1]
    if dups:
        details = "; ".join(
            f"{num} (linhas {', '.join(str(x) for x in lns)})" for num, lns in dups[:10]
        )
        extra = f" e mais {len(dups) - 10}" if len(dups) > 10 else ""
        src_lbl = f" no arquivo '{source_name}'" if source_name else " no arquivo CSV"
        raise ValueError(
            f"Não é permitido haver dois patrimônios com o mesmo número{src_lbl}. "
            f"Duplicado(s): {details}{extra}. Corrija o arquivo antes de importar."
        )

    if not records:
        raise ValueError("Nenhum registro válido de patrimônio foi encontrado no CSV.")

    return records


def merge_patrimony_batches(
    batches: Sequence[List[Dict[str, Any]]],
    source_names: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Unifica múltiplos lotes de CSV e gera erro caso haja números de patrimônio repetidos."""
    merged: List[Dict[str, Any]] = []
    seen_sources: Dict[str, List[str]] = {}
    display_num: Dict[str, str] = {}

    for idx, batch in enumerate(batches):
        src = (
            str(source_names[idx]).strip()
            if source_names and idx < len(source_names) and str(source_names[idx]).strip()
            else f"arquivo #{idx + 1}"
        )
        for rec in batch:
            num = _clean_text(rec.get("numero_patrimonio"))
            if not num:
                continue
            key = _patrimony_key(num)
            display_num.setdefault(key, num)
            seen_sources.setdefault(key, []).append(src)
            if len(seen_sources[key]) == 1:
                merged.append(rec)

    dups = [(display_num[k], srcs) for k, srcs in seen_sources.items() if len(srcs) > 1]
    if dups:
        details = "; ".join(f"{num} ({', '.join(srcs)})" for num, srcs in dups[:10])
        extra = f" e mais {len(dups) - 10}" if len(dups) > 10 else ""
        raise ValueError(
            "Não é permitido haver dois patrimônios com o mesmo número na importação. "
            f"Duplicado(s): {details}{extra}. Remova as duplicidades antes de importar."
        )

    return merged


def _records_differ(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    str_fields = (
        "nome_item",
        "descricao",
        "responsavel",
        "data_incorporacao",
        "fim_garantia",
        "origem",
        "local_item",
    )
    for f in str_fields:
        if _clean_text(existing.get(f)) != _clean_text(incoming.get(f)):
            return True
    old_val = existing.get("valor_pago")
    new_val = incoming.get("valor_pago")
    if (old_val is None) != (new_val is None):
        return True
    if old_val is not None and new_val is not None:
        if abs(float(old_val) - float(new_val)) > 0.005:
            return True
    return False


def diff_and_upsert_patrimonies(
    conn: Any,
    records: List[Dict[str, Any]],
    now_iso: str,
    apply_changes: bool = True,
) -> Dict[str, Any]:
    """Compara registros com o MySQL: impede duplicidades, atualiza existentes e insere apenas novos."""
    seen_in_batch: set[str] = set()
    dup_in_batch: List[str] = []
    for rec in records:
        num = _clean_text(rec.get("numero_patrimonio"))
        key = _patrimony_key(num)
        if not key:
            continue
        if key in seen_in_batch:
            if num not in dup_in_batch:
                dup_in_batch.append(num)
        else:
            seen_in_batch.add(key)
    if dup_in_batch:
        raise ValueError(
            "Não é permitido haver dois patrimônios com o mesmo número na importação. "
            f"Duplicado(s): {', '.join(dup_in_batch[:10])}."
        )

    existing_rows = conn.execute(
        """
        SELECT id, numero_patrimonio, nome_item, descricao, responsavel,
               data_incorporacao, fim_garantia, valor_pago, origem, local_item
        FROM patrimonies
        """
    ).fetchall()
    existing_map: Dict[str, Dict[str, Any]] = {}
    for r in existing_rows:
        key = _patrimony_key(r["numero_patrimonio"])
        if key in existing_map:
            raise ValueError(
                f"Foram encontrados registros duplicados no banco para o patrimônio '{r['numero_patrimonio']}'. "
                "Remova a duplicidade antes de importar."
            )
        existing_map[key] = dict(r)

    added: List[str] = []
    updated: List[str] = []
    unchanged = 0
    counts_by_resp: Dict[str, int] = {}

    to_insert: List[Tuple[Any, ...]] = []
    to_update: List[Tuple[Any, ...]] = []
    for rec in records:
        num = _clean_text(rec["numero_patrimonio"])
        key = _patrimony_key(num)
        resp = rec["responsavel"] or "(Sem responsável)"
        counts_by_resp[resp] = counts_by_resp.get(resp, 0) + 1
        old = existing_map.get(key)
        if old is None:
            added.append(num)
            to_insert.append(
                (
                    num,
                    rec["nome_item"],
                    rec["descricao"],
                    rec["responsavel"],
                    rec["data_incorporacao"],
                    rec["fim_garantia"],
                    rec["valor_pago"],
                    rec["origem"],
                    rec["local_item"],
                    now_iso,
                )
            )
        elif _records_differ(old, rec) or _clean_text(old.get("numero_patrimonio")) != num:
            updated.append(num)
            to_update.append(
                (
                    num,
                    rec["nome_item"],
                    rec["descricao"],
                    rec["responsavel"],
                    rec["data_incorporacao"],
                    rec["fim_garantia"],
                    rec["valor_pago"],
                    rec["origem"],
                    rec["local_item"],
                    now_iso,
                    int(old["id"]),
                )
            )
        else:
            unchanged += 1

    if apply_changes:
        if to_insert:
            conn.executemany(
                """
                INSERT INTO patrimonies(
                    numero_patrimonio, nome_item, descricao, responsavel,
                    data_incorporacao, fim_garantia, valor_pago, origem, local_item, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                to_insert,
            )
        if to_update:
            conn.executemany(
                """
                UPDATE patrimonies
                SET numero_patrimonio = ?, nome_item = ?, descricao = ?, responsavel = ?,
                    data_incorporacao = ?, fim_garantia = ?, valor_pago = ?, origem = ?,
                    local_item = ?, updated_at = ?
                WHERE id = ?
                """,
                to_update,
            )

    return {
        "total": len(records),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "counts_by_responsavel": counts_by_resp,
    }


def build_patrimonies_csv(rows: Sequence[Dict[str, Any]]) -> bytes:
    """Exporta a lista de patrimônios em CSV (UTF-8 com BOM, separado por ';') pronto para Excel."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Número do Patrimônio",
            "Nome do Item",
            "Breve Descrição",
            "Responsável",
            "Data de Incorporação",
            "Fim da Garantia",
            "Valor Pago",
            "Origem do Item",
            "Local do Item",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                _clean_text(r.get("numero_patrimonio")),
                _clean_text(r.get("nome_item")),
                _clean_text(r.get("descricao")),
                _clean_text(r.get("responsavel")),
                format_iso_date_br(r.get("data_incorporacao")),
                format_iso_date_br(r.get("fim_garantia")),
                format_brl_currency(r.get("valor_pago")),
                _clean_text(r.get("origem")),
                _clean_text(r.get("local_item")),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")
