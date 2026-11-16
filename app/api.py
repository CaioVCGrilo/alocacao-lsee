"""Servidor HTTP e API REST com separação estrita entre rotas públicas e administrativas."""

from __future__ import annotations

import base64
import json
import mimetypes
import webbrowser
from datetime import date, datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from flask import Flask, Response, request

from .auth import (
    create_session_token,
    extract_session_from_headers,
    is_secure_cookie_env,
    verify_admin_login,
    verify_session_token,
)
from .config import DEFAULT_XLSX_PATH, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, STATIC_DIR
from .db import DEFAULT_USAGE_RULES, db_session, ensure_default_map_stations
from .importer import import_xlsx_into_db
from .labels import build_labels_docx, build_labels_pdf, normalize_label_pages
from .network_excel import build_network_xlsx, parse_network_xlsx
from .requests_doc import build_request_docx, build_request_pdf, normalize_material_request
from .scoring import build_score_summary, compute_state, simulate_upgrade


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _public_person_label(nome: str, id_pessoa: str, anonymize: bool) -> str:
    if not anonymize:
        return nome
    if id_pessoa:
        return id_pessoa
    parts = [p[0].upper() + "." for p in (nome or "").split() if p]
    return " ".join(parts) if parts else "Membro"


def _log_audit(conn: Any, action: str, target: str = "", details: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(timestamp, action, target, details) VALUES (?, ?, ?, ?)",
        (_now_iso(), action, target, details),
    )


def _to_opt_float(val: Any) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return None
    return float(val)


def _to_opt_int(val: Any) -> Optional[int]:
    if val is None or str(val).strip() == "":
        return None
    return int(float(val))


ALLOWED_LOCATIONS = (
    "Mestrado",
    "Doutorado",
    "Doc. P. e D.",
    "Sala do Servidor",
    "Sala de Reuniões",
)


def _validate_localizacao(val: Any) -> str:
    loc = (str(val) if val is not None else "").strip()
    if loc and loc not in ALLOWED_LOCATIONS:
        raise ValueError(
            "Localização inválida. Escolha uma das opções: " + ", ".join(ALLOWED_LOCATIONS) + "."
        )
    return loc


def _validate_semestre(val: Any) -> Optional[int]:
    sem = _to_opt_int(val)
    if sem is not None and sem not in (1, 2):
        raise ValueError("Semestre de entrada deve ser 1 ou 2.")
    return sem


ALLOWED_MAP_MODES = ("person", "livre", "pc_ic", "servidor", "vazio")
PUBLIC_VISIBLE_PC_STATUSES = ("Em uso", "Livre")


def _normalize_pc_key(pc_id: Any) -> str:
    return "".join(str(pc_id or "").strip().split()).upper()


def _serialize_map_pc(pc_obj: Optional[Dict[str, Any]], source: str, admin_mode: bool) -> Optional[Dict[str, Any]]:
    if not pc_obj:
        return None
    if not admin_mode and pc_obj.get("status") not in PUBLIC_VISIBLE_PC_STATUSES:
        return None
    return {
        "id_pc": pc_obj["id_pc"],
        "processador": pc_obj["processador"],
        "ram_gb": pc_obj["ram_gb"],
        "ssd_gb": pc_obj["ssd_gb"],
        "hd_gb": pc_obj["hd_gb"],
        "possui_gpu": pc_obj["possui_gpu"],
        "tipo_de_uso": pc_obj["tipo_de_uso"],
        "status": pc_obj["status"],
        "localizacao": pc_obj["localizacao"],
        "score_total": pc_obj["score_total_fmt"],
        "source": source,
    }


def _build_map_payload(conn: Any, state: Dict[str, Any], admin_mode: bool) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT station_code, room, station_kind, occupant_mode, id_pessoa,
               custom_label, pc_id_pc, observacoes, updated_at
        FROM map_stations
        """
    ).fetchall()
    if not rows:
        ensure_default_map_stations(conn)
        rows = conn.execute(
            """
            SELECT station_code, room, station_kind, occupant_mode, id_pessoa,
                   custom_label, pc_id_pc, observacoes, updated_at
            FROM map_stations
            """
        ).fetchall()

    anon = bool(state["rules"]["public_anonymize_names"]) and not admin_mode
    people_by_id = {p["id_pessoa"]: p for p in state["people"] if p.get("id_pessoa")}
    comp_by_norm = {
        _normalize_pc_key(c["id_pc"]): c for c in state["computers"] if c.get("id_pc")
    }

    assigned_station_by_person: Dict[str, str] = {}
    for r in rows:
        pid = (r["id_pessoa"] or "").strip()
        if r["occupant_mode"] == "person" and pid and pid not in assigned_station_by_person:
            assigned_station_by_person[pid] = r["station_code"]

    stations = []
    summary = {
        "total": len(rows),
        "person": 0,
        "livre": 0,
        "pc_ic": 0,
        "servidor": 0,
        "vazio": 0,
        "pessoas_ativos_alocados": 0,
        "pessoas_ativas_sem_posto": 0,
    }

    for r in rows:
        mode = r["occupant_mode"] or "vazio"
        if mode in summary:
            summary[mode] += 1

        pid = (r["id_pessoa"] or "").strip()
        custom_label = (r["custom_label"] or "").strip()
        explicit_pc = (r["pc_id_pc"] or "").strip()
        person_obj = people_by_id.get(pid) if mode == "person" and pid else None

        person_payload = None
        if mode == "person" and pid:
            if person_obj:
                nome_publico = _public_person_label(person_obj["nome"], person_obj["id_pessoa"], anon)
                short_or_name = person_obj["id_pessoa"] if anon else (custom_label or person_obj["nome"])
                linked_pc_obj = comp_by_norm.get(_normalize_pc_key(person_obj.get("pc_principal")))
                show_principal = admin_mode or (
                    linked_pc_obj is not None and linked_pc_obj.get("status") in PUBLIC_VISIBLE_PC_STATUSES
                )
                person_payload = {
                    "id_pessoa": person_obj["id_pessoa"],
                    "display_name": short_or_name,
                    "full_name": nome_publico,
                    "categoria": person_obj["categoria"],
                    "entrada": (
                        f"{person_obj['ano_entrada']}/{person_obj['semestre_entrada']}"
                        if person_obj.get("ano_entrada")
                        else ""
                    ),
                    "situacao": person_obj["situacao"],
                    "pc_principal": person_obj["pc_principal"] if show_principal else "",
                    "score_pc_principal": person_obj["score_pc_principal_fmt"] if show_principal else "",
                    "quantidade_pcs": person_obj["quantidade_pcs"] if show_principal else 0,
                }
            else:
                person_payload = {
                    "id_pessoa": pid,
                    "display_name": pid if anon else (custom_label or pid),
                    "full_name": pid if anon else (custom_label or pid),
                    "categoria": "",
                    "entrada": "",
                    "situacao": "Desconhecido",
                    "pc_principal": "",
                    "score_pc_principal": "",
                    "quantidade_pcs": 0,
                }

        pc_obj = None
        pc_source = ""
        if explicit_pc:
            pc_obj = comp_by_norm.get(_normalize_pc_key(explicit_pc))
            if pc_obj:
                pc_source = "direct"
        if pc_obj is None and person_obj and person_obj.get("pc_principal"):
            pc_obj = comp_by_norm.get(_normalize_pc_key(person_obj["pc_principal"]))
            if pc_obj:
                pc_source = "allocation"

        pc_payload = _serialize_map_pc(pc_obj, pc_source, admin_mode)

        if mode == "person":
            if anon:
                display_label = pid or r["station_code"]
            else:
                display_label = custom_label or (person_obj["nome"] if person_obj else pid) or r["station_code"]
        elif mode == "livre":
            display_label = "Livre"
        elif mode == "pc_ic":
            display_label = custom_label or (pc_payload["id_pc"] if pc_payload else "PC IC")
        elif mode == "servidor":
            display_label = custom_label or r["station_code"]
        else:
            display_label = custom_label or ""

        if mode == "person" and person_payload:
            secondary_label = (pc_payload["id_pc"] if pc_payload else "") or (
                "" if anon else person_payload["id_pessoa"]
            )
        elif pc_payload:
            secondary_label = pc_payload["id_pc"]
        elif explicit_pc and admin_mode:
            secondary_label = explicit_pc
        else:
            secondary_label = ""

        station_item: Dict[str, Any] = {
            "station_code": r["station_code"],
            "room": r["room"],
            "station_kind": r["station_kind"],
            "occupant_mode": mode,
            "display_label": display_label,
            "secondary_label": secondary_label,
            "person": person_payload,
            "pc": pc_payload,
        }
        if admin_mode:
            station_item["raw_id_pessoa"] = pid
            station_item["raw_custom_label"] = custom_label
            station_item["raw_pc_id_pc"] = explicit_pc
            station_item["observacoes"] = (r["observacoes"] or "").strip()
            station_item["updated_at"] = (r["updated_at"] or "").strip()
        stations.append(station_item)

    unplaced_active_people = []
    for p in state["people"]:
        if p.get("situacao") != "Ativo":
            continue
        if p["id_pessoa"] in assigned_station_by_person:
            summary["pessoas_ativos_alocados"] += 1
            continue
        summary["pessoas_ativas_sem_posto"] += 1
        unplaced_active_people.append(
            {
                "id_pessoa": p["id_pessoa"],
                "nome": _public_person_label(p["nome"], p["id_pessoa"], anon),
                "categoria": p["categoria"],
                "entrada": f"{p['ano_entrada']}/{p['semestre_entrada']}" if p.get("ano_entrada") else "",
                "pc_principal": p["pc_principal"],
                "score_pc_principal": p["score_pc_principal_fmt"],
            }
        )

    payload: Dict[str, Any] = {
        "anonymized": anon,
        "admin": admin_mode,
        "svg_base_url": "/static/img/layout_lsee_base.svg",
        "summary": summary,
        "stations": stations,
        "unplaced_active_people": unplaced_active_people,
    }

    if admin_mode:
        payload["people_options"] = [
            {
                "id_pessoa": p["id_pessoa"],
                "nome": p["nome"],
                "situacao": p["situacao"],
                "categoria": p["categoria"],
                "pc_principal": p["pc_principal"],
                "assigned_station": assigned_station_by_person.get(p["id_pessoa"], ""),
            }
            for p in sorted(
                state["people"],
                key=lambda item: (0 if item.get("situacao") == "Ativo" else 1, item.get("nome", "").lower()),
            )
        ]
        payload["computer_options"] = [
            {
                "id_pc": c["id_pc"],
                "status": c["status"],
                "tipo_de_uso": c["tipo_de_uso"],
                "localizacao": c["localizacao"],
                "processador": c["processador"],
                "score_total": c["score_total_fmt"],
                "usuario_atual": c["usuario_atual"],
            }
            for c in sorted(state["computers"], key=lambda item: item.get("id_pc", ""))
            if c.get("id_pc")
        ]

    return payload


def _resolve_pc_in_db(conn: Any, raw_pc_id: str) -> str:
    clean = raw_pc_id.strip()
    if not clean:
        return ""
    target_norm = _normalize_pc_key(clean)
    rows = conn.execute("SELECT id_pc FROM computers").fetchall()
    for r in rows:
        if _normalize_pc_key(r["id_pc"]) == target_norm:
            return r["id_pc"]
    raise ValueError(f"Computador '{clean}' não encontrado no cadastro.")


def _update_map_station(conn: Any, station_code: str, body: Dict[str, Any]) -> Dict[str, Any]:
    existing = conn.execute(
        "SELECT station_code, occupant_mode FROM map_stations WHERE station_code = ?",
        (station_code,),
    ).fetchone()
    if not existing:
        raise ValueError(f"Estação '{station_code}' não encontrada no mapa.")

    mode = str(body.get("occupant_mode") or existing["occupant_mode"] or "vazio").strip()
    if mode not in ALLOWED_MAP_MODES:
        raise ValueError(f"Modo inválido '{mode}'. Use: {', '.join(ALLOWED_MAP_MODES)}.")

    id_pessoa = str(body.get("id_pessoa") or "").strip()
    custom_label = str(body.get("custom_label") or "").strip()
    raw_pc = str(body.get("pc_id_pc") or "").strip()
    observacoes = str(body.get("observacoes") or "").strip()
    move_from_previous = bool(body.get("move_from_previous", True))

    moved_from = []
    now = _now_iso()

    if mode == "person":
        if not id_pessoa:
            raise ValueError("Selecione uma pessoa para o modo 'Pessoa alocada'.")
        person_row = conn.execute(
            "SELECT id_pessoa, nome FROM people WHERE id_pessoa = ?",
            (id_pessoa,),
        ).fetchone()
        if not person_row:
            raise ValueError(f"Pessoa '{id_pessoa}' não encontrada no cadastro.")

        previous_rows = conn.execute(
            "SELECT station_code FROM map_stations WHERE id_pessoa = ? AND station_code != ?",
            (id_pessoa, station_code),
        ).fetchall()
        if previous_rows:
            moved_from = [r["station_code"] for r in previous_rows]
            if not move_from_previous:
                raise ValueError(
                    f"Pessoa '{id_pessoa}' já está alocada em {', '.join(moved_from)}."
                )
            conn.execute(
                """
                UPDATE map_stations
                SET occupant_mode = 'livre', id_pessoa = '', custom_label = '', updated_at = ?
                WHERE id_pessoa = ? AND station_code != ?
                """,
                (now, id_pessoa, station_code),
            )
    else:
        id_pessoa = ""
        if mode in ("livre", "vazio"):
            custom_label = ""

    pc_id_pc = _resolve_pc_in_db(conn, raw_pc) if raw_pc else ""

    conn.execute(
        """
        UPDATE map_stations
        SET occupant_mode = ?, id_pessoa = ?, custom_label = ?, pc_id_pc = ?, observacoes = ?, updated_at = ?
        WHERE station_code = ?
        """,
        (mode, id_pessoa, custom_label, pc_id_pc, observacoes, now, station_code),
    )
    details = f"mode={mode}, pessoa={id_pessoa or '-'}, pc={pc_id_pc or '-'}"
    if moved_from:
        details += f", movido_de={','.join(moved_from)}"
    _log_audit(conn, "UPDATE_MAP_STATION", station_code, details)
    return {"station_code": station_code, "moved_from": moved_from}


def _swap_map_stations(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    source = str(body.get("source") or "").strip()
    target = str(body.get("target") or "").strip()
    if not source or not target or source == target:
        raise ValueError("Selecione duas estações distintas para realizar a troca.")

    row_a = conn.execute(
        """
        SELECT station_code, occupant_mode, id_pessoa, custom_label, pc_id_pc, observacoes
        FROM map_stations WHERE station_code = ?
        """,
        (source,),
    ).fetchone()
    row_b = conn.execute(
        """
        SELECT station_code, occupant_mode, id_pessoa, custom_label, pc_id_pc, observacoes
        FROM map_stations WHERE station_code = ?
        """,
        (target,),
    ).fetchone()
    if not row_a:
        raise ValueError(f"Estação de origem '{source}' não encontrada no mapa.")
    if not row_b:
        raise ValueError(f"Estação de destino '{target}' não encontrada no mapa.")

    now = _now_iso()
    conn.execute(
        """
        UPDATE map_stations
        SET occupant_mode = ?, id_pessoa = ?, custom_label = ?, pc_id_pc = ?, observacoes = ?, updated_at = ?
        WHERE station_code = ?
        """,
        (
            row_b["occupant_mode"],
            row_b["id_pessoa"] or "",
            row_b["custom_label"] or "",
            row_b["pc_id_pc"] or "",
            row_b["observacoes"] or "",
            now,
            source,
        ),
    )
    conn.execute(
        """
        UPDATE map_stations
        SET occupant_mode = ?, id_pessoa = ?, custom_label = ?, pc_id_pc = ?, observacoes = ?, updated_at = ?
        WHERE station_code = ?
        """,
        (
            row_a["occupant_mode"],
            row_a["id_pessoa"] or "",
            row_a["custom_label"] or "",
            row_a["pc_id_pc"] or "",
            row_a["observacoes"] or "",
            now,
            target,
        ),
    )
    _log_audit(conn, "SWAP_MAP_STATIONS", f"{source} <-> {target}", "Troca de ocupantes entre estações do mapa")
    return {"source": source, "target": target}


ADMIN_PC_STATUS_ORDER = {
    "": 0,
    "Em uso": 0,
    "Livre": 0,
    "Manutenção": 1,
    "Indisponível": 2,
    "Despatrimoniado": 3,
}


def _admin_pc_status_rank(pc: Dict[str, Any]) -> Tuple[int, int]:
    """Ordena PCs no modo admin: Automático -> Manutenção -> Indisponível -> Despatrimoniado."""
    manual = (pc.get("status_manual") or "").strip()
    eff = (pc.get("status") or "").strip()
    key = manual if manual and manual in ADMIN_PC_STATUS_ORDER else eff
    return (ADMIN_PC_STATUS_ORDER.get(key, 0), int(pc.get("id") or 0))


def _update_rules(conn: Any, body: Dict[str, Any]) -> None:
    """Atualiza pesos das notas, regras gerais, faixas de RAM e prioridades por categoria."""
    pesos = body.get("pesos")
    if not isinstance(pesos, dict):
        raise ValueError("Campo 'pesos' inválido.")
    w_keys = {
        "cpu": ("peso_cpu", "Peso CPU", "pesos"),
        "ram": ("peso_ram", "Peso RAM", "pesos"),
        "armazenamento": ("peso_armazenamento", "Peso Armazenamento", "pesos"),
        "gpu": ("peso_gpu", "Peso GPU", "pesos"),
    }
    parsed_weights: Dict[str, float] = {}
    for k, (db_key, _, _) in w_keys.items():
        try:
            v = float(pesos[k])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Peso '{k}' inválido.") from exc
        if v < 0.0 or v > 1.0:
            raise ValueError(f"Peso '{k}' deve estar entre 0% e 100%.")
        parsed_weights[db_key] = round(v, 6)

    weight_sum = sum(parsed_weights.values())
    if abs(weight_sum - 1.0) > 0.002:
        raise ValueError(f"A soma dos 4 pesos deve ser 100% (atual: {weight_sum * 100:.1f}%).")

    try:
        diff_min = float(body.get("diferenca_minima_upgrade", 3.0))
        peso_hd = float(body.get("armaz_peso_hd", 0.25))
        escala_log = float(body.get("armaz_escala_log", 60.0))
        score_max = float(body.get("score_maximo", 10.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Parâmetros numéricos inválidos.") from exc

    if diff_min < 0:
        raise ValueError("Diferença mínima para upgrade deve ser maior ou igual a zero.")
    if peso_hd < 0 or peso_hd > 2.0:
        raise ValueError("Peso relativo do HD deve estar entre 0 e 2.")
    if escala_log <= 0:
        raise ValueError("Fator de suavização log deve ser maior que zero.")
    if score_max <= 0 or score_max > 100:
        raise ValueError("Score máximo componente deve ser maior que zero (até 100).")

    raw_ram = body.get("ram_thresholds")
    if not isinstance(raw_ram, list) or not raw_ram:
        raise ValueError("Informe ao menos uma faixa de RAM mínima.")
    ram_entries: list[tuple[float, float]] = []
    seen_ram: set[float] = set()
    for row in raw_ram:
        if not isinstance(row, dict):
            raise ValueError("Faixa de RAM inválida.")
        try:
            mv = float(row["min_value"])
            sc = float(row["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Valores inválidos na faixa de RAM.") from exc
        if mv < 0:
            raise ValueError("RAM mínima não pode ser negativa.")
        if sc < 0 or sc > score_max:
            raise ValueError(f"Score da faixa de RAM ({mv:g} GB) deve estar entre 0 e {score_max:g}.")
        if mv in seen_ram:
            raise ValueError(f"Faixa de RAM duplicada: {mv:g} GB.")
        seen_ram.add(mv)
        ram_entries.append((mv, sc))
    ram_entries.sort(key=lambda item: item[0])

    raw_cats = body.get("categories")
    if not isinstance(raw_cats, list) or not raw_cats:
        raise ValueError("Lista de categorias inválida.")
    existing_cats = {r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()}
    cat_entries: list[tuple[Optional[int], int, int, str]] = []
    for cat in raw_cats:
        if not isinstance(cat, dict):
            raise ValueError("Categoria inválida.")
        name = str(cat.get("name") or "").strip()
        if not name:
            raise ValueError("Nome da categoria é obrigatório.")
        if name not in existing_cats:
            raise ValueError(f"Categoria desconhecida: '{name}'.")
        raw_grp = cat.get("priority_group")
        if raw_grp in (None, ""):
            grp: Optional[int] = None
        else:
            try:
                grp = int(raw_grp)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Grupo inválido para '{name}'.") from exc
            if grp < 1 or grp > 9:
                raise ValueError(f"Grupo de prioridade para '{name}' deve ser entre 1 e 9 ou vazio.")
        part_up = 1 if cat.get("participa_upgrade") else 0
        prec_pc = 1 if cat.get("precisa_pc") else 0
        cat_entries.append((grp, part_up, prec_pc, name))

    param_rows = [
        *[
            (db_key, lbl, f"{parsed_weights[db_key]:g}", cat)
            for db_key, lbl, cat in w_keys.values()
        ],
        ("diferenca_minima_upgrade", "Diferença mínima para upgrade", f"{diff_min:g}", "regras"),
        ("armaz_peso_hd", "Peso relativo do HD (Armazenamento)", f"{peso_hd:g}", "armazenamento"),
        ("armaz_escala_log", "Fator de suavização log (GB_eq)", f"{escala_log:g}", "armazenamento"),
        ("score_maximo", "Score máximo componente", f"{score_max:g}", "armazenamento"),
    ]
    for key, lbl, val, cat in param_rows:
        conn.execute(
            """
            INSERT INTO system_params(`key`, label, `value`, category)
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
            """,
            (key, lbl, val, cat),
        )

    conn.execute("DELETE FROM score_thresholds WHERE metric = 'ram'")
    conn.executemany(
        "INSERT INTO score_thresholds(metric, min_value, score) VALUES ('ram', ?, ?)",
        ram_entries,
    )
    conn.executemany(
        "UPDATE categories SET priority_group = ?, participa_upgrade = ?, precisa_pc = ? WHERE name = ?",
        cat_entries,
    )
    _log_audit(
        conn,
        "UPDATE_CRITERIA",
        "rules",
        (
            f"Pesos CPU={parsed_weights['peso_cpu']*100:.1f}% RAM={parsed_weights['peso_ram']*100:.1f}% "
            f"Arm={parsed_weights['peso_armazenamento']*100:.1f}% GPU={parsed_weights['peso_gpu']*100:.1f}%; "
            f"{len(ram_entries)} faixas RAM; {len(cat_entries)} categorias"
        ),
    )


def _handle_network_import_xlsx(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Pré-visualiza ou importa lista de IPs a partir de um arquivo .xlsx enviado em base64."""
    b64 = str(body.get("content_base64") or "").strip()
    if not b64:
        raise ValueError("Envie o arquivo .xlsx para importação.")
    if "," in b64 and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        raw_bytes = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError("Conteúdo do arquivo .xlsx inválido.") from exc
    if len(raw_bytes) > 5_000_000:
        raise ValueError("Arquivo .xlsx muito grande (limite: 5 MB).")

    parsed = parse_network_xlsx(raw_bytes)
    new_records = parsed["records"]

    def _norm_key(cat: str, eq: str) -> tuple[str, str]:
        return (" ".join((cat or "").lower().split()), " ".join((eq or "").lower().split()))

    existing_rows = conn.execute(
        "SELECT categoria, equipamento, ip, mac, observacoes FROM network_devices"
    ).fetchall()
    old_map = {_norm_key(r["categoria"], r["equipamento"]): dict(r) for r in existing_rows}
    new_map = {_norm_key(r["categoria"], r["equipamento"]): r for r in new_records}

    added = [r["equipamento"] for k, r in new_map.items() if k not in old_map]
    removed = [r["equipamento"] for k, r in old_map.items() if k not in new_map]
    updated = []
    unchanged = 0
    for k, nr in new_map.items():
        if k not in old_map:
            continue
        orow = old_map[k]
        if (
            (orow.get("ip") or "") != nr["ip"]
            or (orow.get("mac") or "").lower() != nr["mac"].lower()
            or (orow.get("observacoes") or "") != nr["observacoes"]
        ):
            updated.append(nr["equipamento"])
        else:
            unchanged += 1

    preview_only = bool(body.get("preview_only"))
    if not preview_only:
        conn.execute("DELETE FROM network_devices")
        conn.executemany(
            "INSERT INTO network_devices(categoria, equipamento, ip, mac, observacoes) VALUES (?, ?, ?, ?, ?)",
            [
                (r["categoria"], r["equipamento"], r["ip"], r["mac"], r["observacoes"])
                for r in new_records
            ],
        )
        filename = str(body.get("filename") or "Lista de IPs.xlsx").strip()
        _log_audit(
            conn,
            "IMPORT_NETWORK_XLSX",
            filename,
            f"Aba '{parsed['sheet_name']}': {len(new_records)} registros ({len(added)} novos, {len(updated)} atualizados, {len(removed)} removidos)",
        )

    return {
        "ok": True,
        "applied": not preview_only,
        "sheet_name": parsed["sheet_name"],
        "total": len(new_records),
        "counts_by_cat": parsed["counts_by_cat"],
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "preview": new_records[:120],
    }


def _get_requests_payload(conn: Any) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, created_at, orientador, laboratorio, data_solicitacao,
               items_json, total_valor, justificativa
        FROM material_requests
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()
    history = []
    for r in rows:
        try:
            items = json.loads(r["items_json"] or "[]")
        except Exception:
            items = []
        history.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "orientador": r["orientador"] or "",
                "laboratorio": r["laboratorio"] or "",
                "data_solicitacao": r["data_solicitacao"] or "",
                "items": items,
                "total_valor": r["total_valor"],
                "justificativa": r["justificativa"] or "",
            }
        )

    pc_opts = conn.execute(
        "SELECT id_pc, patrimonio, tipo_de_uso, localizacao FROM computers WHERE id_pc IS NOT NULL ORDER BY id_pc"
    ).fetchall()
    mon_opts = conn.execute(
        "SELECT id_monitor, patrimonio, marca_modelo FROM monitors ORDER BY id_monitor"
    ).fetchall()
    equipments = [
        {
            "label": f"{r['id_pc']} ({r['tipo_de_uso'] or 'PC'}) — Patr. {r['patrimonio']}",
            "patrimonio": (r["patrimonio"] or "").strip(),
            "tipo": "PC",
        }
        for r in pc_opts
        if (r["patrimonio"] or "").strip()
    ] + [
        {
            "label": f"{r['id_monitor']} ({r['marca_modelo'] or 'Monitor'}) — Patr. {r['patrimonio']}",
            "patrimonio": (r["patrimonio"] or "").strip(),
            "tipo": "Monitor",
        }
        for r in mon_opts
        if (r["patrimonio"] or "").strip()
    ]
    return {"items": history, "equipment_options": equipments}


def _free_simulator_pcs(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Retorna apenas PCs com status 'Livre' ordenados por score decrescente."""
    pcs = [
        {
            "id_pc": c["id_pc"],
            "processador": c["processador"],
            "status": c["status"],
            "tipo_de_uso": c["tipo_de_uso"],
            "score_total": c["score_total_fmt"],
        }
        for c in state["computers"]
        if c["id_pc"] and c["status"] == "Livre"
    ]
    pcs.sort(
        key=lambda c: (-(c["score_total"] if c["score_total"] is not None else -1.0), c["id_pc"])
    )
    return pcs


def _resolve_free_pc_id(
    state: Dict[str, Any],
    requested_pc_id: str,
    available_pcs: list[Dict[str, Any]],
) -> str:
    free_ids = {c["id_pc"] for c in available_pcs}
    if requested_pc_id and requested_pc_id in free_ids:
        return requested_pc_id
    for top_pc in state["dashboard"]["top_pcs_livres"]:
        if top_pc["id_pc"] in free_ids:
            return top_pc["id_pc"]
    return available_pcs[0]["id_pc"] if available_pcs else ""


class LSEEController:
    """Controlador HTTP agnóstico para páginas estáticas e endpoints REST."""

    def __init__(self) -> None:
        self._response: Optional[Response] = None

    def _is_admin(self) -> bool:
        token = request.cookies.get(SESSION_COOKIE_NAME) or extract_session_from_headers(
            request.headers.get("Cookie")
        )
        return verify_session_token(token or "")

    def _send_json(
        self,
        payload: Any,
        status: int = HTTPStatus.OK,
        cookie_token: Optional[str] = None,
        clear_cookie: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        resp = Response(body, status=int(status), mimetype="application/json")
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        secure = request.is_secure or is_secure_cookie_env()
        if cookie_token is not None:
            resp.set_cookie(
                SESSION_COOKIE_NAME,
                cookie_token,
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                samesite="Lax",
                path="/",
                secure=secure,
            )
        elif clear_cookie:
            resp.set_cookie(
                SESSION_COOKIE_NAME,
                "",
                max_age=0,
                httponly=True,
                samesite="Lax",
                path="/",
                secure=secure,
            )
        self._response = resp

    def _send_binary(
        self,
        content: bytes,
        content_type: str,
        filename: str,
        status: int = HTTPStatus.OK,
    ) -> None:
        resp = Response(content, status=int(status), mimetype=content_type)
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        self._response = resp

    def _read_json_body(self) -> Dict[str, Any]:
        raw = request.get_data(cache=True)
        if not raw or len(raw) > 2_000_000:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Corpo JSON deve ser um objeto.")
        return data

    def _require_admin(self) -> bool:
        if not self._is_admin():
            self._send_json(
                {"error": "Acesso restrito. Autentique-se como administrador."},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False
        return True

    def _serve_static(self, rel_path: str) -> Response:
        clean = rel_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / clean).resolve()
        static_root = STATIC_DIR.resolve()
        if not target.is_relative_to(static_root) or not target.is_file():
            self._send_json({"error": "Arquivo não encontrado."}, status=HTTPStatus.NOT_FOUND)
            return self._response  # type: ignore[return-value]
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix.lower() == ".svg":
            ctype = "image/svg+xml"
        ctype = ctype or "application/octet-stream"
        content_type = f"{ctype}; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ctype
        resp = Response(target.read_bytes(), status=HTTPStatus.OK, mimetype=ctype)
        resp.headers["Content-Type"] = content_type
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # ---------------- GET ----------------
    def handle_get(self, path: str, qs: Dict[str, list[str]]) -> Response:
        if not path.startswith("/api/"):
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            return self._serve_static("index.html")

        try:
            if path == "/api/auth/status":
                self._send_json({"is_admin": self._is_admin()})
                return self._response  # type: ignore[return-value]

            if path == "/api/public/dashboard":
                self._handle_public_dashboard()
                return self._response  # type: ignore[return-value]

            if path == "/api/public/computers":
                self._handle_public_computers()
                return self._response  # type: ignore[return-value]

            if path == "/api/public/simulator":
                pc_id = (qs.get("pc", [""])[0] or "").strip()
                self._handle_public_simulator(pc_id)
                return self._response  # type: ignore[return-value]

            if path == "/api/public/score-info":
                self._handle_public_score_info()
                return self._response  # type: ignore[return-value]
            if path == "/api/public/map":
                self._handle_public_map()
                return self._response  # type: ignore[return-value]

            if path == "/api/public/usage-rules":
                with db_session() as conn:
                    row = conn.execute(
                        "SELECT content, updated_at FROM site_contents WHERE `key` = 'usage_rules'"
                    ).fetchone()
                self._send_json(
                    {
                        "content": row["content"] if row else DEFAULT_USAGE_RULES,
                        "updated_at": (row["updated_at"] or "") if row else "",
                    }
                )
                return self._response  # type: ignore[return-value]

            # Rotas administrativas (exigem sessão válida)
            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                self._route_admin_get(path, qs)
                return self._response  # type: ignore[return-value]

            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._response  # type: ignore[return-value]

    # ---------------- POST ----------------
    def handle_post(self, path: str) -> Response:
        try:
            if path == "/api/auth/login":
                body = self._read_json_body()
                pw = str(body.get("password", ""))
                with db_session() as conn:
                    ok = verify_admin_login(conn, pw)
                    if not ok:
                        _log_audit(conn, "ADMIN_LOGIN_FAILED", "auth", "Tentativa com senha incorreta")
                        self._send_json({"error": "Senha inválida."}, status=HTTPStatus.UNAUTHORIZED)
                        return self._response  # type: ignore[return-value]
                    _log_audit(conn, "ADMIN_LOGIN", "auth", "Sessão administrativa iniciada")
                token = create_session_token()
                self._send_json({"is_admin": True}, cookie_token=token)
                return self._response  # type: ignore[return-value]

            if path == "/api/auth/logout":
                self._send_json({"is_admin": False}, clear_cookie=True)
                return self._response  # type: ignore[return-value]

            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                body = self._read_json_body()
                self._route_admin_post(path, body)
                return self._response  # type: ignore[return-value]

            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._response  # type: ignore[return-value]

    # ---------------- PUT ----------------
    def handle_put(self, path: str) -> Response:
        try:
            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                body = self._read_json_body()
                self._route_admin_put(path, body)
                return self._response  # type: ignore[return-value]
            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._response  # type: ignore[return-value]

    # ---------------- DELETE ----------------
    def handle_delete(self, path: str) -> Response:
        try:
            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                self._route_admin_delete(path)
                return self._response  # type: ignore[return-value]
            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._response  # type: ignore[return-value]

    # ---------------- PUBLIC HANDLERS ----------------
    def _handle_public_dashboard(self) -> None:
        with db_session() as conn:
            state = compute_state(conn)
        dash = state["dashboard"]
        anon = state["rules"]["public_anonymize_names"]

        sem_pc = [
            {
                "ordem": i + 1,
                "id_pessoa": p["id_pessoa"],
                "nome": _public_person_label(p["nome"], p["id_pessoa"], anon),
                "categoria": p["categoria"],
                "entrada": f"{p['ano_entrada']}/{p['semestre_entrada']}" if p["ano_entrada"] else "",
                "situacao": p["situacao"],
            }
            for i, p in enumerate(dash["pessoas_sem_pc_lista"])
        ]

        fila = [
            {
                "ordem": p["ordem_prioridade"],
                "empate": p["empate_prioridade"],
                "id_pessoa": p["id_pessoa"],
                "nome": _public_person_label(p["nome"], p["id_pessoa"], anon),
                "categoria": p["categoria"],
                "entrada": f"{p['ano_entrada']}/{p['semestre_entrada']}" if p["ano_entrada"] else "",
                "pc_principal": p["pc_principal"],
                "score_pc_principal": p["score_pc_principal_fmt"],
            }
            for p in dash["fila_prioridade"]
        ]

        self._send_json(
            {
                "anonymized": anon,
                "cards": {
                    "pcs_individuais": dash["pcs_individuais"],
                    "em_uso": dash["em_uso"],
                    "livres": dash["livres"],
                    "manutencao": dash["manutencao"],
                    "pessoas_sem_pc": dash["pessoas_sem_pc"],
                },
                "top_pcs_livres": dash["top_pcs_livres"],
                "pessoas_sem_pc_lista": sem_pc,
                "fila_prioridade": fila,
            }
        )

    def _handle_public_computers(self) -> None:
        with db_session() as conn:
            state = compute_state(conn)
        anon = state["rules"]["public_anonymize_names"]
        public_pcs = []
        for c in state["computers"]:
            if not c["id_pc"] or c["status"] not in ("Em uso", "Livre"):
                continue
            public_pcs.append(
                {
                    "id_pc": c["id_pc"],
                    "processador": c["processador"],
                    "ram_gb": c["ram_gb"],
                    "ssd_gb": c["ssd_gb"],
                    "hd_gb": c["hd_gb"],
                    "possui_gpu": c["possui_gpu"],
                    "tipo_de_uso": c["tipo_de_uso"],
                    "status": c["status"],
                    "localizacao": c["localizacao"],
                    "score_total": c["score_total_fmt"],
                    "usuario_atual": (
                        _public_person_label(c["usuario_atual"], c["usuario_atual_id"], anon)
                        if c["usuario_atual"]
                        else ""
                    ),
                }
            )
        self._send_json({"anonymized": anon, "items": public_pcs})

    def _handle_public_simulator(self, pc_id: str) -> None:
        with db_session() as conn:
            state = compute_state(conn)
        anon = state["rules"]["public_anonymize_names"]
        available_pcs = _free_simulator_pcs(state)
        pc_id = _resolve_free_pc_id(state, pc_id, available_pcs)

        sim = simulate_upgrade(state, pc_id) if pc_id else {"pc": None, "rows": []}
        public_rows = []
        for r in sim["rows"]:
            if r["situacao"] != "Ativo":
                continue
            public_rows.append(
                {
                    "ordem": r["ordem"],
                    "id_pessoa": r["id_pessoa"],
                    "pessoa": _public_person_label(r["pessoa"], r["id_pessoa"], anon),
                    "categoria": r["categoria"],
                    "entrada": r["entrada"],
                    "pc_principal": r["pc_principal"],
                    "score_atual": r["score_atual_fmt"],
                    "score_oferecido": r["score_oferecido_fmt"],
                    "ganho": r["ganho_fmt"],
                    "resultado": r["resultado"],
                    "ultimo_upgrade": r["ultimo_upgrade"],
                }
            )
        self._send_json(
            {
                "anonymized": anon,
                "available_pcs": available_pcs,
                "pc": sim.get("pc"),
                "diff_min_upgrade": state["rules"]["diff_min_upgrade"],
                "rows": public_rows,
            }
        )

    def _handle_public_score_info(self) -> None:
        with db_session() as conn:
            state = compute_state(conn)
        self._send_json(build_score_summary(state))

    def _handle_public_map(self) -> None:
        with db_session() as conn:
            state = compute_state(conn)
            payload = _build_map_payload(conn, state, admin_mode=False)
        self._send_json(payload)

    def _handle_admin_rules(self, conn: Any) -> None:
        state = compute_state(conn)
        r = state["rules"]
        summary = build_score_summary(state)
        self._send_json(
            {
                **summary,
                "diferenca_minima_upgrade": r["diff_min_upgrade"],
                "cpu_ref": state["dashboard"]["cpu_ref"],
                "thresholds": {
                    k: [{"min_value": mv, "score": sc} for mv, sc in v]
                    for k, v in r["thresholds"].items()
                    if k == "ram"
                },
                "categories": list(r["categories"].values()),
            }
        )

    # ---------------- ADMIN ROUTING ----------------
    def _route_admin_get(self, path: str, qs: Dict[str, Any]) -> None:
        with db_session() as conn:
            if path == "/api/admin/rules":
                self._handle_admin_rules(conn)
                return

            if path == "/api/admin/overview":
                state = compute_state(conn)
                logs = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, timestamp, action, target, details FROM audit_log ORDER BY id DESC LIMIT 15"
                    ).fetchall()
                ]
                self._send_json(
                    {
                        "dashboard": state["dashboard"],
                        "recent_logs": logs,
                    }
                )
                return

            if path == "/api/admin/computers":
                state = compute_state(conn)
                ordered_pcs = sorted(state["computers"], key=_admin_pc_status_rank)
                self._send_json(
                    {
                        "items": ordered_pcs,
                        "processors": [p["name"] for p in state["processors"]],
                    }
                )
                return

            if path == "/api/admin/people":
                state = compute_state(conn)
                self._send_json(
                    {
                        "items": state["people"],
                        "categories": list(state["rules"]["categories"].keys()),
                    }
                )
                return

            if path == "/api/admin/allocations":
                state = compute_state(conn)
                self._send_json(
                    {
                        "items": state["allocations"],
                        "people": [{"id_pessoa": p["id_pessoa"], "nome": p["nome"]} for p in state["people"]],
                        "pcs": [
                            {"id_pc": c["id_pc"], "status": c["status"], "tipo_de_uso": c["tipo_de_uso"]}
                            for c in state["computers"]
                            if c["id_pc"]
                        ],
                    }
                )
                return

            if path == "/api/admin/simulator":
                pc_id = (qs.get("pc", [""])[0] or "").strip()
                state = compute_state(conn)
                pcs = _free_simulator_pcs(state)
                pc_id = _resolve_free_pc_id(state, pc_id, pcs)
                sim = simulate_upgrade(state, pc_id) if pc_id else {"pc": None, "rows": [], "diff_min_upgrade": state["rules"]["diff_min_upgrade"]}
                sim.setdefault("diff_min_upgrade", state["rules"]["diff_min_upgrade"])
                decisions = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, pc_oferta, id_pessoa, decisao, observacoes, created_at FROM upgrade_decisions ORDER BY id DESC LIMIT 30"
                    ).fetchall()
                ]
                sim["available_pcs"] = pcs
                sim["decisions"] = decisions
                self._send_json(sim)
                return

            if path == "/api/admin/monitors":
                state = compute_state(conn)
                pc_users = {c["id_pc"]: c["usuario_atual"] for c in state["computers"] if c["id_pc"]}
                rows = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT id, id_monitor, patrimonio, marca_modelo, tipo, tamanho,
                               status, localizacao, associado_pc, projeto, observacoes
                        FROM monitors
                        ORDER BY id
                        """
                    ).fetchall()
                ]
                for r in rows:
                    r["usuario_associado"] = pc_users.get(r["associado_pc"] or "", "")
                self._send_json({"items": rows, "pcs": sorted(pc_users.keys())})
                return

            if path == "/api/admin/processors":
                state = compute_state(conn)
                self._send_json({"items": state["processors"]})
                return

            if path == "/api/admin/network":
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, categoria, equipamento, ip, mac, observacoes FROM network_devices ORDER BY categoria, equipamento"
                    ).fetchall()
                ]
                self._send_json({"items": rows})
                return

            if path == "/api/admin/network/export-xlsx":
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT categoria, equipamento, ip, mac, observacoes FROM network_devices ORDER BY id"
                    ).fetchall()
                ]
                content = build_network_xlsx(rows)
                _log_audit(conn, "EXPORT_NETWORK_XLSX", f"{len(rows)} equips", "Lista de IPs exportada em Excel (.xlsx)")
                self._send_binary(
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    f"Lista_de_IPs_LSEE_{_today_str()}.xlsx",
                )
                return

            if path == "/api/admin/requests":
                self._send_json(_get_requests_payload(conn))
                return

            if path == "/api/admin/params":
                state = compute_state(conn)
                self._send_json(
                    {
                        "params": state["rules"]["params_meta"],
                        "thresholds": state["rules"]["thresholds_rows"],
                        "categories": list(state["rules"]["categories"].values()),
                    }
                )
                return

            if path == "/api/admin/map":
                state = compute_state(conn)
                self._send_json(_build_map_payload(conn, state, admin_mode=True))
                return

            if path == "/api/admin/audit":
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, timestamp, action, target, details FROM audit_log ORDER BY id DESC LIMIT 100"
                    ).fetchall()
                ]
                self._send_json({"items": rows})
                return

            if path == "/api/admin/release-tutorial":
                steps = [
                    {
                        "title": "Passo 1: Fazer login como administrador",
                        "details": [
                            "Acesse a conta Administrador do Windows utilizando a senha padrão interna do laboratório (consulte a equipe responsável pela alocação).",
                        ],
                    },
                    {
                        "title": "Passo 2: Excluir o Usuário LSEE antigo",
                        "details": [
                            "Pressione Windows + X e selecione Gerenciamento do Computador.",
                            "Vá em Usuários e Grupos Locais > Usuários.",
                            "Clique com o botão direito em LSEE e selecione Excluir.",
                        ],
                    },
                    {
                        "title": "Passo 3: Excluir os dados antigos e limpar o D:",
                        "details": [
                            "Navegue até C:\\Users e exclua a pasta LSEE com Shift + Del.",
                            "Formate o disco D: caso ele exista.",
                            "Reinicie o computador.",
                        ],
                    },
                    {
                        "title": "Passo 4: Criar o novo Usuário LSEE (Administrador)",
                        "details": [
                            "Abra novamente Gerenciamento do Computador > Usuários e Grupos Locais > Usuários.",
                            "Clique com o botão direito e escolha Novo Usuário (usuário LSEE; configure senha nunca expira / sem troca obrigatória).",
                            "Clique em Criar e feche a janela.",
                            "Vá em Grupos > Administradores > Adicionar, digite LSEE, clique em Verificar Nomes, OK, Aplicar e OK.",
                            "Reinicie o computador.",
                        ],
                    },
                    {
                        "title": "Passo 5: Fazer login no novo usuário LSEE",
                        "details": [
                            "Entre na conta LSEE recém-criada e desinstale softwares não padrões instalados pelo usuário anterior.",
                        ],
                    },
                    {
                        "title": "Passo 6: Executar Limpeza de Disco",
                        "details": [
                            "Abra Limpeza de Disco no Menu Iniciar > OK.",
                            "Clique em Limpar arquivos do sistema > OK, marque todas as opções e confirme em OK.",
                        ],
                    },
                    {
                        "title": "Passo 7: Criar 2 pontos de restauração",
                        "details": [
                            "No Menu Iniciar, abra Criar ponto de restauração.",
                            "Selecione o Disco C > Configurar > marque Ativar > Aplicar > OK.",
                            "Clique em Criar e gere 2 pontos com os nomes: \"PC limpo\" e \"PC limpo 2\".",
                        ],
                    },
                    {
                        "title": "Passo 8: Desativar modo de recuperação após falta de energia",
                        "details": [
                            "Abra o Prompt de Comandos (CMD) como Administrador e execute o comando abaixo:",
                        ],
                        "command": "bcdedit /set {current} recoveryenabled no",
                    },
                ]
                self._send_json({"steps": steps})
                return

            self._send_json({"error": "Rota administrativa não encontrada."}, status=HTTPStatus.NOT_FOUND)

    def _route_admin_post(self, path: str, body: Dict[str, Any]) -> None:
        with db_session() as conn:
            if path in ("/api/admin/labels/pdf", "/api/admin/labels/docx"):
                pages = normalize_label_pages(body)
                total_labels = sum(len(p) for p in pages)
                if path.endswith("/pdf"):
                    content = build_labels_pdf(pages)
                    _log_audit(conn, "GENERATE_LABELS_PDF", f"{len(pages)}p/{total_labels}etq", "Etiquetas geradas em PDF 1:1")
                    self._send_binary(content, "application/pdf", f"etiquetas_lsee_{_today_str()}.pdf")
                else:
                    content = build_labels_docx(pages)
                    _log_audit(conn, "GENERATE_LABELS_DOCX", f"{len(pages)}p/{total_labels}etq", "Etiquetas geradas em Word (.docx)")
                    self._send_binary(
                        content,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        f"etiquetas_lsee_{_today_str()}.docx",
                    )
                return

            if path == "/api/admin/computers":
                id_pc = (body.get("id_pc") or "").strip() or None
                if id_pc:
                    dup = conn.execute("SELECT id FROM computers WHERE id_pc = ?", (id_pc,)).fetchone()
                    if dup:
                        raise ValueError(f"Já existe um computador com ID_PC '{id_pc}'.")
                cur = conn.execute(
                    """
                    INSERT INTO computers(
                        id_pc, patrimonio, processor_name, ram_gb, ssd_gb, hd_gb,
                        possui_gpu, tipo_de_uso, status_manual, localizacao, projeto, observacoes,
                        score_cpu_legado, score_armaz_legado
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        id_pc,
                        (body.get("patrimonio") or "").strip(),
                        (body.get("processador") or "").strip(),
                        _to_opt_float(body.get("ram_gb")),
                        _to_opt_float(body.get("ssd_gb")),
                        _to_opt_float(body.get("hd_gb")),
                        (body.get("possui_gpu") or "").strip(),
                        (body.get("tipo_de_uso") or "").strip(),
                        (body.get("status_manual") or "").strip(),
                        _validate_localizacao(body.get("localizacao")),
                        (body.get("projeto") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        _to_opt_float(body.get("score_cpu_legado")),
                        _to_opt_float(body.get("score_armaz_legado")),
                    ),
                )
                _log_audit(conn, "CREATE_COMPUTER", id_pc or f"row#{cur.lastrowid}", "Computador cadastrado")
                self._send_json({"id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/people":
                id_pessoa = (body.get("id_pessoa") or "").strip()
                nome = (body.get("nome") or "").strip()
                if not id_pessoa or not nome:
                    raise ValueError("ID_Pessoa e Nome são obrigatórios.")
                cur = conn.execute(
                    """
                    INSERT INTO people(
                        id_pessoa, nome, categoria, ano_entrada, semestre_entrada,
                        situacao, data_saida, observacoes, data_ultimo_upgrade
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        id_pessoa,
                        nome,
                        (body.get("categoria") or "Mestrado").strip(),
                        _to_opt_int(body.get("ano_entrada")),
                        _validate_semestre(body.get("semestre_entrada")),
                        (body.get("situacao") or "Ativo").strip(),
                        (body.get("data_saida") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        (body.get("data_ultimo_upgrade") or "").strip(),
                    ),
                )
                _log_audit(conn, "CREATE_PERSON", id_pessoa, f"Pessoa cadastrada: {nome}")
                self._send_json({"id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/allocations":
                id_pessoa = (body.get("id_pessoa") or "").strip()
                pc_id_pc = (body.get("pc_id_pc") or "").strip()
                tipo = (body.get("tipo") or "Principal").strip()
                if not id_pessoa or not pc_id_pc:
                    raise ValueError("Pessoa e PC são obrigatórios.")

                person = conn.execute("SELECT id_pessoa, nome FROM people WHERE id_pessoa = ?", (id_pessoa,)).fetchone()
                if not person:
                    raise ValueError("Pessoa não encontrada.")
                pc = conn.execute("SELECT id_pc FROM computers WHERE id_pc = ?", (pc_id_pc,)).fetchone()
                if not pc:
                    raise ValueError("PC não encontrado.")
                active_on_pc = conn.execute(
                    "SELECT id_alocacao, pessoa_nome FROM allocations WHERE pc_id_pc = ? AND (data_fim IS NULL OR data_fim = '')",
                    (pc_id_pc,),
                ).fetchone()
                if active_on_pc:
                    raise ValueError(
                        f"O PC {pc_id_pc} já está em uso por {active_on_pc['pessoa_nome']} ({active_on_pc['id_alocacao']})."
                    )

                next_num = (conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] or 0) + 1
                id_aloc = f"ALO{next_num:04d}"
                while conn.execute("SELECT 1 FROM allocations WHERE id_alocacao = ?", (id_aloc,)).fetchone():
                    next_num += 1
                    id_aloc = f"ALO{next_num:04d}"

                cur = conn.execute(
                    """
                    INSERT INTO allocations(
                        id_alocacao, pessoa_nome, id_pessoa, pc_id_pc,
                        data_inicio, data_fim, tipo, motivo_inicio, motivo_fim,
                        observacoes, origem
                    ) VALUES (?, ?, ?, ?, ?, '', ?, ?, '', ?, 'Sistema Web')
                    """,
                    (
                        id_aloc,
                        person["nome"],
                        person["id_pessoa"],
                        pc_id_pc,
                        (body.get("data_inicio") or _today_str()).strip(),
                        tipo,
                        (body.get("motivo_inicio") or "Alocação manual").strip(),
                        (body.get("observacoes") or "").strip(),
                    ),
                )
                _log_audit(conn, "CREATE_ALLOCATION", id_aloc, f"{person['nome']} -> {pc_id_pc} ({tipo})")
                self._send_json({"id": cur.lastrowid, "id_alocacao": id_aloc}, status=HTTPStatus.CREATED)
                return

            if path.startswith("/api/admin/allocations/") and path.endswith("/close"):
                aloc_id = int(path.split("/")[4])
                al = conn.execute("SELECT id_alocacao, pessoa_nome, pc_id_pc, data_fim FROM allocations WHERE id = ?", (aloc_id,)).fetchone()
                if not al:
                    raise ValueError("Alocação não encontrada.")
                if (al["data_fim"] or "").strip():
                    raise ValueError("Alocação já está encerrada.")
                data_fim = (body.get("data_fim") or _today_str()).strip()
                motivo_fim = (body.get("motivo_fim") or "Encerramento pelo administrador").strip()
                conn.execute(
                    "UPDATE allocations SET data_fim = ?, motivo_fim = ? WHERE id = ?",
                    (data_fim, motivo_fim, aloc_id),
                )
                _log_audit(conn, "CLOSE_ALLOCATION", al["id_alocacao"], f"{al['pessoa_nome']} liberou {al['pc_id_pc']}")
                self._send_json({"ok": True})
                return

            if path == "/api/admin/simulator/commit":
                pc_oferta = (body.get("pc_oferta") or "").strip()
                id_pessoa = (body.get("id_pessoa") or "").strip()
                decisao = (body.get("decisao") or "").strip()
                efetivar = bool(body.get("efetivar", False))
                obs = (body.get("observacoes") or "").strip()
                if decisao not in ("Pendente", "Aceitou", "Recusou", "Desistiu"):
                    raise ValueError("Decisão inválida.")
                if not pc_oferta or not id_pessoa:
                    raise ValueError("PC e Pessoa são obrigatórios.")

                conn.execute(
                    """
                    INSERT INTO upgrade_decisions(pc_oferta, id_pessoa, decisao, observacoes, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pc_oferta, id_pessoa, decisao, obs, _now_iso()),
                )

                freed_pc = ""
                if decisao == "Aceitou" and efetivar:
                    person = conn.execute(
                        "SELECT id_pessoa, nome FROM people WHERE id_pessoa = ?", (id_pessoa,)
                    ).fetchone()
                    if not person:
                        raise ValueError("Pessoa não encontrada.")
                    busy = conn.execute(
                        "SELECT id_alocacao, pessoa_nome FROM allocations WHERE pc_id_pc = ? AND (data_fim IS NULL OR data_fim = '')",
                        (pc_oferta,),
                    ).fetchone()
                    if busy:
                        raise ValueError(f"O PC {pc_oferta} já possui alocação ativa ({busy['pessoa_nome']}).")

                    today = _today_str()
                    prev = conn.execute(
                        """
                        SELECT id, id_alocacao, pc_id_pc FROM allocations
                        WHERE (id_pessoa = ? OR pessoa_nome = ?)
                          AND tipo = 'Principal'
                          AND (data_fim IS NULL OR data_fim = '')
                        """,
                        (person["id_pessoa"], person["nome"]),
                    ).fetchone()
                    if prev:
                        freed_pc = prev["pc_id_pc"]
                        conn.execute(
                            "UPDATE allocations SET data_fim = ?, motivo_fim = ? WHERE id = ?",
                            (today, f"Upgrade para {pc_oferta}", prev["id"]),
                        )

                    next_num = (conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] or 0) + 1
                    id_aloc = f"ALO{next_num:04d}"
                    while conn.execute("SELECT 1 FROM allocations WHERE id_alocacao = ?", (id_aloc,)).fetchone():
                        next_num += 1
                        id_aloc = f"ALO{next_num:04d}"

                    conn.execute(
                        """
                        INSERT INTO allocations(
                            id_alocacao, pessoa_nome, id_pessoa, pc_id_pc,
                            data_inicio, data_fim, tipo, motivo_inicio, motivo_fim,
                            observacoes, origem
                        ) VALUES (?, ?, ?, ?, ?, '', 'Principal', ?, '', ?, 'Simulador Web')
                        """,
                        (id_aloc, person["nome"], person["id_pessoa"], pc_oferta, today, "Upgrade / Cascata", obs),
                    )
                    conn.execute(
                        "UPDATE people SET data_ultimo_upgrade = ? WHERE id_pessoa = ?",
                        (today, person["id_pessoa"]),
                    )
                    _log_audit(
                        conn,
                        "UPGRADE_COMMIT",
                        pc_oferta,
                        f"{person['nome']} aceitou {pc_oferta}" + (f" (liberou {freed_pc})" if freed_pc else ""),
                    )
                else:
                    _log_audit(conn, "UPGRADE_DECISION", pc_oferta, f"{id_pessoa}: {decisao}")

                self._send_json({"ok": True, "freed_pc": freed_pc})
                return

            if path == "/api/admin/monitors":
                id_mon = (body.get("id_monitor") or "").strip()
                if not id_mon:
                    raise ValueError("ID_Monitor é obrigatório.")
                cur = conn.execute(
                    """
                    INSERT INTO monitors(
                        id_monitor, patrimonio, marca_modelo, tipo, tamanho,
                        status, localizacao, associado_pc, projeto, observacoes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        id_mon,
                        (body.get("patrimonio") or "").strip(),
                        (body.get("marca_modelo") or "").strip(),
                        (body.get("tipo") or "").strip(),
                        (body.get("tamanho") or "").strip(),
                        (body.get("status") or "Em uso").strip(),
                        _validate_localizacao(body.get("localizacao")),
                        (body.get("associado_pc") or "").strip(),
                        (body.get("projeto") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                    ),
                )
                _log_audit(conn, "CREATE_MONITOR", id_mon, "Monitor cadastrado")
                self._send_json({"id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/processors":
                name = (body.get("name") or "").strip()
                threads = _to_opt_int(body.get("threads"))
                multi = _to_opt_float(body.get("multi"))
                single = _to_opt_float(body.get("single"))
                if not name or not threads or multi is None or single is None:
                    raise ValueError("Nome, Threads, Multi e Single são obrigatórios.")
                indice_cpu = multi + single * threads
                cur = conn.execute(
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
                        (body.get("fonte") or "Cadastro manual").strip(),
                        (body.get("data_referencia") or _today_str()).strip(),
                        (body.get("observacoes") or "").strip(),
                    ),
                )
                _log_audit(conn, "CREATE_PROCESSOR", name, f"Índice_CPU={indice_cpu}")
                self._send_json({"id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/network":
                cat = (body.get("categoria") or "").strip()
                eq = (body.get("equipamento") or "").strip()
                if not cat or not eq:
                    raise ValueError("Categoria e Equipamento são obrigatórios.")
                cur = conn.execute(
                    "INSERT INTO network_devices(categoria, equipamento, ip, mac, observacoes) VALUES (?, ?, ?, ?, ?)",
                    (
                        cat,
                        eq,
                        (body.get("ip") or "").strip(),
                        (body.get("mac") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                    ),
                )
                _log_audit(conn, "CREATE_NETWORK", eq, f"{cat} - {body.get('ip', '')}")
                self._send_json({"id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/network/import-xlsx":
                res = _handle_network_import_xlsx(conn, body)
                self._send_json(res)
                return

            if path == "/api/admin/requests":
                req = normalize_material_request(body)
                now = _now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO material_requests(
                        created_at, orientador, laboratorio, data_solicitacao,
                        items_json, total_valor, justificativa
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        req["orientador"],
                        req["laboratorio"],
                        req["data_solicitacao"],
                        json.dumps(req["items"], ensure_ascii=False),
                        req["total_valor"],
                        req["justificativa"],
                    ),
                )
                _log_audit(
                    conn,
                    "CREATE_MATERIAL_REQUEST",
                    f"req#{cur.lastrowid}",
                    f"{len(req['items'])} itens · Total={req['total_fmt'] or '-'}",
                )
                self._send_json({"ok": True, "id": cur.lastrowid}, status=HTTPStatus.CREATED)
                return

            if path in ("/api/admin/requests/pdf", "/api/admin/requests/docx"):
                req = normalize_material_request(body)
                if path.endswith("/pdf"):
                    content = build_request_pdf(req)
                    _log_audit(conn, "EXPORT_REQUEST_PDF", f"{len(req['items'])} itens", "Solicitação exportada em PDF")
                    self._send_binary(content, "application/pdf", f"Solicitacao_Auxilio_LSEE_{_today_str()}.pdf")
                else:
                    content = build_request_docx(req)
                    _log_audit(conn, "EXPORT_REQUEST_DOCX", f"{len(req['items'])} itens", "Solicitação exportada em Word (.docx)")
                    self._send_binary(
                        content,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        f"Solicitacao_Auxilio_LSEE_{_today_str()}.docx",
                    )
                return

            if path == "/api/admin/map/swap":
                info = _swap_map_stations(conn, body)
                state = compute_state(conn)
                self._send_json({"ok": True, **info, "map": _build_map_payload(conn, state, admin_mode=True)})
                return

            if path == "/api/admin/reimport":
                counts = import_xlsx_into_db(conn, DEFAULT_XLSX_PATH)
                self._send_json({"ok": True, "counts": counts})
                return

            self._send_json({"error": "Rota POST administrativa não encontrada."}, status=HTTPStatus.NOT_FOUND)

    def _route_admin_put(self, path: str, body: Dict[str, Any]) -> None:
        with db_session() as conn:
            if path.startswith("/api/admin/computers/"):
                pc_pk = int(path.split("/")[4])
                id_pc = (body.get("id_pc") or "").strip() or None
                conn.execute(
                    """
                    UPDATE computers
                    SET id_pc = ?, patrimonio = ?, processor_name = ?, ram_gb = ?, ssd_gb = ?, hd_gb = ?,
                        possui_gpu = ?, tipo_de_uso = ?, status_manual = ?, localizacao = ?, projeto = ?, observacoes = ?
                    WHERE id = ?
                    """,
                    (
                        id_pc,
                        (body.get("patrimonio") or "").strip(),
                        (body.get("processador") or "").strip(),
                        _to_opt_float(body.get("ram_gb")),
                        _to_opt_float(body.get("ssd_gb")),
                        _to_opt_float(body.get("hd_gb")),
                        (body.get("possui_gpu") or "").strip(),
                        (body.get("tipo_de_uso") or "").strip(),
                        (body.get("status_manual") or "").strip(),
                        _validate_localizacao(body.get("localizacao")),
                        (body.get("projeto") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        pc_pk,
                    ),
                )
                _log_audit(conn, "UPDATE_COMPUTER", id_pc or f"row#{pc_pk}", "Dados atualizados")
                self._send_json({"ok": True})
                return

            if path.startswith("/api/admin/people/"):
                p_pk = int(path.split("/")[4])
                conn.execute(
                    """
                    UPDATE people
                    SET nome = ?, categoria = ?, ano_entrada = ?, semestre_entrada = ?,
                        situacao = ?, data_saida = ?, observacoes = ?, data_ultimo_upgrade = ?
                    WHERE id = ?
                    """,
                    (
                        (body.get("nome") or "").strip(),
                        (body.get("categoria") or "").strip(),
                        _to_opt_int(body.get("ano_entrada")),
                        _validate_semestre(body.get("semestre_entrada")),
                        (body.get("situacao") or "").strip(),
                        (body.get("data_saida") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        (body.get("data_ultimo_upgrade") or "").strip(),
                        p_pk,
                    ),
                )
                _log_audit(conn, "UPDATE_PERSON", f"row#{p_pk}", f"Atualizado: {body.get('nome', '')}")
                self._send_json({"ok": True})
                return

            if path.startswith("/api/admin/monitors/"):
                m_pk = int(path.split("/")[4])
                conn.execute(
                    """
                    UPDATE monitors
                    SET patrimonio = ?, marca_modelo = ?, tipo = ?, tamanho = ?, status = ?,
                        localizacao = ?, associado_pc = ?, projeto = ?, observacoes = ?
                    WHERE id = ?
                    """,
                    (
                        (body.get("patrimonio") or "").strip(),
                        (body.get("marca_modelo") or "").strip(),
                        (body.get("tipo") or "").strip(),
                        (body.get("tamanho") or "").strip(),
                        (body.get("status") or "").strip(),
                        _validate_localizacao(body.get("localizacao")),
                        (body.get("associado_pc") or "").strip(),
                        (body.get("projeto") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        m_pk,
                    ),
                )
                _log_audit(conn, "UPDATE_MONITOR", f"row#{m_pk}", "Monitor atualizado")
                self._send_json({"ok": True})
                return

            if path.startswith("/api/admin/processors/"):
                pr_pk = int(path.split("/")[4])
                threads = _to_opt_int(body.get("threads")) or 1
                multi = _to_opt_float(body.get("multi")) or 0.0
                single = _to_opt_float(body.get("single")) or 0.0
                indice_cpu = multi + single * threads
                conn.execute(
                    """
                    UPDATE processors
                    SET threads = ?, multi = ?, single = ?, indice_cpu = ?,
                        fonte = ?, data_referencia = ?, observacoes = ?
                    WHERE id = ?
                    """,
                    (
                        threads,
                        multi,
                        single,
                        indice_cpu,
                        (body.get("fonte") or "").strip(),
                        (body.get("data_referencia") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        pr_pk,
                    ),
                )
                _log_audit(conn, "UPDATE_PROCESSOR", f"row#{pr_pk}", f"Índice_CPU={indice_cpu}")
                self._send_json({"ok": True})
                return

            if path.startswith("/api/admin/network/"):
                net_pk = int(path.split("/")[4])
                conn.execute(
                    "UPDATE network_devices SET categoria = ?, equipamento = ?, ip = ?, mac = ?, observacoes = ? WHERE id = ?",
                    (
                        (body.get("categoria") or "").strip(),
                        (body.get("equipamento") or "").strip(),
                        (body.get("ip") or "").strip(),
                        (body.get("mac") or "").strip(),
                        (body.get("observacoes") or "").strip(),
                        net_pk,
                    ),
                )
                _log_audit(conn, "UPDATE_NETWORK", f"row#{net_pk}", "Registro de rede atualizado")
                self._send_json({"ok": True})
                return

            if path.startswith("/api/admin/map/stations/"):
                station_code = unquote(path[len("/api/admin/map/stations/"):]).strip()
                info = _update_map_station(conn, station_code, body)
                state = compute_state(conn)
                self._send_json({"ok": True, **info, "map": _build_map_payload(conn, state, admin_mode=True)})
                return

            if path == "/api/admin/rules":
                _update_rules(conn, body)
                self._send_json({"ok": True})
                return

            if path == "/api/admin/params":
                updates = body.get("params") or {}
                if not isinstance(updates, dict):
                    raise ValueError("Campo 'params' deve ser um objeto chave-valor.")
                for k, v in updates.items():
                    conn.execute("UPDATE system_params SET `value` = ? WHERE `key` = ?", (str(v).strip(), str(k)))
                _log_audit(conn, "UPDATE_PARAMS", "system_params", f"Chaves atualizadas: {list(updates.keys())}")
                self._send_json({"ok": True})
                return

            if path == "/api/admin/usage-rules":
                content = str(body.get("content") or "").strip()
                if not content:
                    raise ValueError("O texto das regras de uso não pode ficar vazio.")
                now = _now_iso()
                conn.execute(
                    """
                    INSERT INTO site_contents(`key`, content, updated_at) VALUES ('usage_rules', ?, ?)
                    ON DUPLICATE KEY UPDATE content = VALUES(content), updated_at = VALUES(updated_at)
                    """,
                    (content, now),
                )
                _log_audit(conn, "UPDATE_USAGE_RULES", "usage_rules", "Regras de uso atualizadas")
                self._send_json({"ok": True, "content": content, "updated_at": now})
                return

            self._send_json({"error": "Rota PUT administrativa não encontrada."}, status=HTTPStatus.NOT_FOUND)

    def _route_admin_delete(self, path: str) -> None:
        with db_session() as conn:
            if path.startswith("/api/admin/monitors/"):
                m_pk = int(path.split("/")[4])
                conn.execute("DELETE FROM monitors WHERE id = ?", (m_pk,))
                _log_audit(conn, "DELETE_MONITOR", f"row#{m_pk}", "Monitor removido")
                self._send_json({"ok": True})
                return
            if path.startswith("/api/admin/network/"):
                net_pk = int(path.split("/")[4])
                conn.execute("DELETE FROM network_devices WHERE id = ?", (net_pk,))
                _log_audit(conn, "DELETE_NETWORK", f"row#{net_pk}", "Dispositivo de rede removido")
                self._send_json({"ok": True})
                return
            if path.startswith("/api/admin/requests/"):
                req_pk = int(path.split("/")[4])
                conn.execute("DELETE FROM material_requests WHERE id = ?", (req_pk,))
                _log_audit(conn, "DELETE_MATERIAL_REQUEST", f"req#{req_pk}", "Solicitação removida")
                self._send_json({"ok": True})
                return
            self._send_json({"error": "Rota DELETE administrativa não encontrada."}, status=HTTPStatus.NOT_FOUND)


app = Flask(__name__, static_folder=None)


@app.route("/", defaults={"req_path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/<path:req_path>", methods=["GET", "POST", "PUT", "DELETE"])
def dispatch(req_path: str) -> Response:
    """Roteador WSGI único compatível com Vercel Serverless e execução local."""
    ctrl = LSEEController()
    path = "/" + req_path if req_path else "/"
    method = request.method.upper()
    if method == "GET":
        qs = {k: request.args.getlist(k) for k in request.args.keys()}
        return ctrl.handle_get(path, qs)
    if method == "POST":
        return ctrl.handle_post(path)
    if method == "PUT":
        return ctrl.handle_put(path)
    if method == "DELETE":
        return ctrl.handle_delete(path)
    ctrl._send_json({"error": "Método não permitido."}, status=HTTPStatus.METHOD_NOT_ALLOWED)
    return ctrl._response  # type: ignore[return-value]


def run_server(host: str, port: int, open_browser: bool = False) -> None:
    """Inicia o servidor HTTP Flask/Werkzeug localmente."""
    from werkzeug.serving import make_server

    try:
        server = make_server(host, port, app, threaded=True)
    except OSError as exc:
        raise SystemExit(
            f"Erro ao abrir {host}:{port} ({exc}). "
            "Já existe outro servidor rodando nessa porta. Execute com --restart para substituir."
        ) from exc

    open_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{open_host}:{port}"
    print(f"Servidor LSEE Alocação de Recursos rodando em {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando servidor...")
    finally:
        server.server_close()
