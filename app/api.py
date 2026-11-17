"""Servidor HTTP e API REST com separação estrita entre rotas públicas e administrativas."""

from __future__ import annotations

import base64
import contextvars
import json
import mimetypes
import secrets
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from flask import Flask, Response, request

from .auth import (
    authenticate_admin_user,
    create_session_token,
    extract_session_from_headers,
    is_secure_cookie_env,
    normalize_nusp,
    parse_session_token,
    verify_admin_login,
    verify_session_token,
)
from .config import DEFAULT_XLSX_PATH, SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, STATIC_DIR
from .db import DEFAULT_USAGE_RULES, db_session, ensure_default_map_stations
from .importer import import_xlsx_into_db
from .labels import build_labels_docx, build_labels_pdf, normalize_label_pages
from .network_excel import build_network_xlsx, parse_network_xlsx
from .requests_doc import (
    build_request_docx,
    build_request_pdf,
    incorporate_material_request,
    normalize_material_request,
)
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


_CURRENT_ADMIN_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("_CURRENT_ADMIN_ACTOR", default="")
ONLINE_SESSION_TTL_SECONDS = 75
ALLOWED_ADMIN_TABS = {
    "dashboard",
    "computadores",
    "mapa",
    "simulador",
    "criterios",
    "pessoas",
    "alocacoes",
    "monitores",
    "etiquetas",
    "rede",
    "solicitacoes",
    "parametros",
}


def _log_audit(
    conn: Any,
    action: str,
    target: str = "",
    details: str = "",
    actor: Optional[str] = None,
) -> None:
    who = actor if actor is not None else (_CURRENT_ADMIN_ACTOR.get() or "")
    conn.execute(
        "INSERT INTO audit_log(timestamp, action, target, details, actor) VALUES (?, ?, ?, ?, ?)",
        (_now_iso(), action, target, details, who),
    )


def _needs_admin_bootstrap(conn: Any) -> bool:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM admin_users").fetchone()
    return (int(row["cnt"] or 0) if row is not None else 0) == 0


def _resolve_session_claims(conn: Any, claims: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not claims:
        return None
    uid = int(claims.get("uid") or 0)
    sid = str(claims.get("sid") or "")
    if uid <= 0:
        nome = str(claims.get("nome") or "Administrador")
        return {"uid": 0, "nusp": "", "nome": nome, "sid": sid, "actor_label": nome}

    _expire_overdue_dismissals(conn)
    user_row = conn.execute(
        "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE id = ?",
        (uid,),
    ).fetchone()
    if not user_row or not int(user_row["is_active"] or 0):
        return None
    if sid:
        sess_row = conn.execute(
            "SELECT logged_out_at FROM admin_sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()
        if sess_row and (sess_row["logged_out_at"] or "").strip():
            return None
    nusp = str(user_row["numero_usp"] or "")
    nome = str(user_row["nome"] or "")
    return {
        "uid": int(user_row["id"]),
        "nusp": nusp,
        "nome": nome,
        "sid": sid,
        "actor_label": f"{nome} ({nusp})" if nusp else nome,
    }


def _touch_admin_session(conn: Any, admin_info: Dict[str, Any], active_tab: str = "") -> None:
    sid = str(admin_info.get("sid") or "")
    if not sid:
        return
    now = _now_iso()
    tab = active_tab.strip() if active_tab and active_tab.strip() in ALLOWED_ADMIN_TABS else ""
    if tab:
        conn.execute(
            """
            UPDATE admin_sessions
            SET last_seen_at = ?, active_tab = ?
            WHERE session_id = ? AND (logged_out_at IS NULL OR logged_out_at = '')
            """,
            (now, tab, sid),
        )
    else:
        conn.execute(
            """
            UPDATE admin_sessions
            SET last_seen_at = ?
            WHERE session_id = ? AND (logged_out_at IS NULL OR logged_out_at = '')
            """,
            (now, sid),
        )


def _perform_admin_login(conn: Any, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Optional[str]]:
    _expire_overdue_dismissals(conn)
    nusp_raw = body.get("numero_usp", "")
    bootstrap_name = str(body.get("nome") or "").strip()
    pw = str(body.get("password") or "")
    active_tab = str(body.get("active_tab") or "dashboard").strip()
    if active_tab not in ALLOWED_ADMIN_TABS:
        active_tab = "dashboard"

    user, err_msg, code = authenticate_admin_user(conn, nusp_raw, pw, bootstrap_name)
    if not user:
        clean_nusp = normalize_nusp(nusp_raw)
        if code == "INACTIVE_NUSP" and clean_nusp:
            inactive_u = conn.execute(
                "SELECT id, numero_usp, nome FROM admin_users WHERE numero_usp = ?",
                (clean_nusp,),
            ).fetchone()
            pending_ret = None
            if inactive_u:
                pending_ret = conn.execute(
                    """
                    SELECT id, target_user_id, target_numero_usp, target_nome,
                           requested_by_user_id, requested_by_nome, reason,
                           COALESCE(request_type, 'dismissal') AS request_type,
                           status, created_at, expires_at, responded_at
                    FROM admin_dismissal_requests
                    WHERE target_user_id = ?
                      AND COALESCE(request_type, 'dismissal') = 'reactivation'
                      AND status = 'pending'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (int(inactive_u["id"]),),
                ).fetchone()
            _log_audit(
                conn,
                "ADMIN_LOGIN_INACTIVE",
                f"NUSP {clean_nusp}",
                "Tentativa de login com conta administrativa desativada",
                actor=f"{inactive_u['nome']} ({clean_nusp})" if inactive_u else clean_nusp,
            )
            return (
                HTTPStatus.FORBIDDEN,
                {
                    "error": err_msg,
                    "needs_bootstrap": False,
                    "code": "ADMIN_REACTIVATION_REQUIRED",
                    "numero_usp": clean_nusp,
                    "nome": inactive_u["nome"] if inactive_u else "",
                    "pending_return": _format_dismissal_row(pending_ret) if pending_ret else None,
                },
                None,
            )
        actor = clean_nusp or "Não identificado"
        _log_audit(conn, "ADMIN_LOGIN_FAILED", clean_nusp or "auth", f"{code}: {err_msg}", actor=actor)
        return (
            HTTPStatus.UNAUTHORIZED,
            {"error": err_msg, "needs_bootstrap": _needs_admin_bootstrap(conn), "code": code},
            None,
        )

    sid = secrets.token_hex(16)
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO admin_sessions(
            session_id, user_id, numero_usp, nome, active_tab, logged_in_at, last_seen_at, logged_out_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '')
        """,
        (sid, user["id"], user["numero_usp"], user["nome"], active_tab, now, now),
    )
    actor_label = f"{user['nome']} ({user['numero_usp']})"
    login_note = "1º administrador cadastrado no bootstrap" if code == "BOOTSTRAP_OK" else "Sessão administrativa iniciada"
    _log_audit(conn, "ADMIN_LOGIN", f"NUSP {user['numero_usp']}", login_note, actor=actor_label)
    token = create_session_token(
        {"uid": int(user["id"]), "nusp": user["numero_usp"], "nome": user["nome"], "sid": sid}
    )
    pending_dismissals = _get_pending_dismissals_for_user(conn, int(user["id"]))
    return (
        HTTPStatus.OK,
        {
            "is_admin": True,
            "needs_bootstrap": False,
            "user": {
                "id": int(user["id"]),
                "numero_usp": user["numero_usp"],
                "nome": user["nome"],
                "session_id": sid,
            },
            "pending_dismissals": pending_dismissals,
        },
        token,
    )


def _perform_admin_logout(conn: Any, raw_claims: Optional[Dict[str, Any]]) -> None:
    resolved = _resolve_session_claims(conn, raw_claims)
    sid = str((raw_claims or {}).get("sid") or "")
    if sid:
        conn.execute(
            "UPDATE admin_sessions SET logged_out_at = ? WHERE session_id = ?",
            (_now_iso(), sid),
        )
    if resolved:
        _log_audit(
            conn,
            "ADMIN_LOGOUT",
            f"NUSP {resolved['nusp']}" if resolved.get("nusp") else "auth",
            "Sessão administrativa encerrada",
            actor=resolved.get("actor_label") or "",
        )


def _get_collab_payload(conn: Any, admin_info: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SESSION_TTL_SECONDS)).isoformat()
    sess_rows = conn.execute(
        """
        SELECT session_id, user_id, numero_usp, nome, active_tab, last_seen_at
        FROM admin_sessions
        WHERE (logged_out_at IS NULL OR logged_out_at = '')
          AND last_seen_at >= ?
        ORDER BY last_seen_at DESC
        """,
        (cutoff,),
    ).fetchall()

    my_uid = int(admin_info.get("uid") or 0)
    my_sid = str(admin_info.get("sid") or "")
    seen_uids: set[int] = set()
    online_admins = []
    for r in sess_rows:
        uid = int(r["user_id"] or 0)
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        is_self = (bool(my_sid) and r["session_id"] == my_sid) or (my_uid > 0 and uid == my_uid)
        online_admins.append(
            {
                "user_id": uid,
                "numero_usp": r["numero_usp"] or "",
                "nome": r["nome"] or "",
                "active_tab": r["active_tab"] or "dashboard",
                "last_seen_at": r["last_seen_at"] or "",
                "is_self": is_self,
            }
        )

    other_online_count = sum(1 for item in online_admins if not item["is_self"])
    msg_rows = conn.execute(
        """
        SELECT id, user_id, numero_usp, author_name, message, created_at
        FROM admin_chat_messages
        ORDER BY id DESC
        LIMIT 60
        """
    ).fetchall()
    messages = [
        {
            "id": int(m["id"]),
            "user_id": int(m["user_id"] or 0),
            "numero_usp": m["numero_usp"] or "",
            "author_name": m["author_name"] or "",
            "message": m["message"] or "",
            "created_at": m["created_at"] or "",
            "is_self": (my_uid > 0 and int(m["user_id"] or 0) == my_uid),
        }
        for m in reversed(msg_rows)
    ]
    return {
        "online_admins": online_admins,
        "other_online_count": other_online_count,
        "messages": messages,
        "latest_message_id": int(messages[-1]["id"]) if messages else 0,
        "pending_dismissals": _get_pending_dismissals_for_user(conn, my_uid) if my_uid > 0 else [],
    }


def _expire_overdue_dismissals(conn: Any) -> None:
    now = _now_iso()
    overdue = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               expires_at
        FROM admin_dismissal_requests
        WHERE status = 'pending' AND expires_at != '' AND expires_at < ?
        ORDER BY id ASC
        """,
        (now,),
    ).fetchall()
    for r in overdue:
        d_id = int(r["id"])
        target_uid = int(r["target_user_id"])
        rtype = str(r["request_type"] or "dismissal").strip().lower()
        conn.execute(
            "UPDATE admin_dismissal_requests SET status = 'expired', responded_at = ? WHERE id = ?",
            (now, d_id),
        )
        if rtype == "dismissal":
            other_cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM admin_users WHERE is_active = 1 AND id != ?",
                (target_uid,),
            ).fetchone()
            if int((other_cnt["cnt"] if other_cnt else 0) or 0) > 0:
                conn.execute("UPDATE admin_users SET is_active = 0 WHERE id = ?", (target_uid,))
                conn.execute(
                    "UPDATE admin_sessions SET logged_out_at = ? WHERE user_id = ? AND (logged_out_at IS NULL OR logged_out_at = '')",
                    (now, target_uid),
                )
                _log_audit(
                    conn,
                    "EXPIRE_DISMISSAL_DEACTIVATED",
                    f"NUSP {r['target_numero_usp']}",
                    f"7 dias sem resposta; {r['target_nome']} desativado automaticamente — Motivo: {r['reason']}",
                    actor="Sistema (7 dias)",
                )
            else:
                _log_audit(
                    conn,
                    "EXPIRE_ADMIN_DISMISSAL",
                    f"NUSP {r['target_numero_usp']}",
                    f"Expirada após 7 dias sem outro administrador ativo para manter o sistema ({r['target_nome']})",
                    actor="Sistema (7 dias)",
                )
        elif rtype == "resignation":
            _log_audit(
                conn,
                "EXPIRE_ADMIN_RESIGNATION",
                f"NUSP {r['target_numero_usp']}",
                f"Pedido voluntário de demissão de {r['target_nome']} expirou após 7 dias sem aprovação; acesso mantido",
                actor="Sistema (7 dias)",
            )


def _format_dismissal_row(r: Any) -> Dict[str, Any]:
    return {
        "id": int(r["id"]),
        "target_user_id": int(r["target_user_id"]),
        "target_numero_usp": r["target_numero_usp"] or "",
        "target_nome": r["target_nome"] or "",
        "requested_by_user_id": int(r["requested_by_user_id"] or 0),
        "requested_by_nome": r["requested_by_nome"] or "",
        "reason": r["reason"] or "",
        "request_type": str(r["request_type"] or "dismissal"),
        "status": r["status"] or "pending",
        "created_at": r["created_at"] or "",
        "expires_at": r["expires_at"] or "",
        "responded_at": r["responded_at"] or "",
    }


def _get_pending_dismissals_for_user(conn: Any, user_id: int) -> list[Dict[str, Any]]:
    if user_id <= 0:
        return []
    _expire_overdue_dismissals(conn)
    rows = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        WHERE status = 'pending'
        ORDER BY id DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        rtype = str(r["request_type"] or "dismissal").strip().lower()
        tid = int(r["target_user_id"])
        if rtype == "dismissal" and tid == user_id:
            out.append(_format_dismissal_row(r))
        elif rtype in ("resignation", "reactivation") and tid != user_id:
            out.append(_format_dismissal_row(r))
    return out


def _get_admin_users_payload(conn: Any) -> Dict[str, Any]:
    _expire_overdue_dismissals(conn)
    rows = conn.execute(
        """
        SELECT id, numero_usp, nome, is_active, created_at, created_by
        FROM admin_users
        ORDER BY is_active DESC, nome ASC, id ASC
        """
    ).fetchall()
    d_rows = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        ORDER BY id DESC
        LIMIT 60
        """
    ).fetchall()
    dismissals = [_format_dismissal_row(r) for r in d_rows]
    pending_dismissal_by_target: Dict[int, Dict[str, Any]] = {}
    pending_resignation_by_target: Dict[int, Dict[str, Any]] = {}
    pending_reactivation_by_target: Dict[int, Dict[str, Any]] = {}
    for d in dismissals:
        if d["status"] != "pending":
            continue
        tid = d["target_user_id"]
        rtype = d["request_type"]
        if rtype == "dismissal" and tid not in pending_dismissal_by_target:
            pending_dismissal_by_target[tid] = d
        elif rtype == "resignation" and tid not in pending_resignation_by_target:
            pending_resignation_by_target[tid] = d
        elif rtype == "reactivation" and tid not in pending_reactivation_by_target:
            pending_reactivation_by_target[tid] = d
    return {
        "items": [
            {
                "id": int(r["id"]),
                "numero_usp": r["numero_usp"],
                "nome": r["nome"],
                "is_active": bool(int(r["is_active"] or 0)),
                "created_at": r["created_at"] or "",
                "created_by": r["created_by"] or "",
                "pending_dismissal": pending_dismissal_by_target.get(int(r["id"])),
                "pending_resignation": pending_resignation_by_target.get(int(r["id"])),
                "pending_reactivation": pending_reactivation_by_target.get(int(r["id"])),
            }
            for r in rows
        ],
        "dismissals": dismissals,
    }


def _create_admin_user_record(conn: Any, body: Dict[str, Any], creator_label: str) -> Dict[str, Any]:
    nusp = normalize_nusp(body.get("numero_usp"))
    nome = " ".join(str(body.get("nome") or "").split())
    if len(nusp) < 4 or len(nusp) > 15:
        raise ValueError("Número USP inválido (informe de 4 a 15 dígitos).")
    if len(nome) < 2:
        raise ValueError("Informe o nome do administrador (ex.: Cleiton).")
    existing = conn.execute(
        "SELECT id FROM admin_users WHERE numero_usp = ?",
        (nusp,),
    ).fetchone()
    if existing:
        raise ValueError("Este Número USP já está cadastrado como administrador.")
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO admin_users(numero_usp, nome, is_active, created_at, created_by)
        VALUES (?, ?, 1, ?, ?)
        """,
        (nusp, nome, now, creator_label),
    )
    _log_audit(conn, "CREATE_ADMIN_USER", f"NUSP {nusp}", nome)
    return {
        "ok": True,
        "item": {
            "id": int(cur.lastrowid),
            "numero_usp": nusp,
            "nome": nome,
            "is_active": True,
            "created_at": now,
            "created_by": creator_label,
        },
    }


def _update_admin_user_record(
    conn: Any,
    user_id: int,
    body: Dict[str, Any],
    current_uid: int = 0,
) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT id, numero_usp, nome, is_active, created_at, created_by FROM admin_users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        raise LookupError("Administrador não encontrado.")

    if "is_active" in body:
        raise PermissionError("Alterações de acesso de administradores devem ser feitas via solicitação de demissão.")

    if current_uid <= 0 or user_id != current_uid:
        raise PermissionError("Apenas o próprio administrador pode editar seu Número USP e nome.")

    new_nusp = normalize_nusp(body["numero_usp"]) if "numero_usp" in body else str(row["numero_usp"] or "")
    if len(new_nusp) < 4 or len(new_nusp) > 15:
        raise ValueError("Número USP inválido (informe de 4 a 15 dígitos).")
    if new_nusp != row["numero_usp"]:
        dup = conn.execute(
            "SELECT id FROM admin_users WHERE numero_usp = ? AND id != ?",
            (new_nusp, user_id),
        ).fetchone()
        if dup:
            raise ValueError("Este Número USP já está cadastrado para outro administrador.")

    nome = " ".join(str(body["nome"]).split()) if "nome" in body else str(row["nome"] or "")
    if len(nome) < 2:
        raise ValueError("Informe um nome válido para o administrador (mínimo 2 caracteres).")

    conn.execute(
        "UPDATE admin_users SET numero_usp = ?, nome = ? WHERE id = ?",
        (new_nusp, nome, user_id),
    )
    conn.execute(
        "UPDATE admin_sessions SET numero_usp = ?, nome = ? WHERE user_id = ? AND (logged_out_at IS NULL OR logged_out_at = '')",
        (new_nusp, nome, user_id),
    )
    _log_audit(conn, "UPDATE_ADMIN_USER", f"NUSP {new_nusp}", nome)
    return {
        "ok": True,
        "item": {
            "id": user_id,
            "numero_usp": new_nusp,
            "nome": nome,
            "is_active": bool(int(row["is_active"] or 0)),
            "created_at": row["created_at"] or "",
            "created_by": row["created_by"] or "",
        },
        "user": {"id": user_id, "numero_usp": new_nusp, "nome": nome},
    }


def _create_admin_dismissal_record(
    conn: Any,
    target_uid: int,
    body: Dict[str, Any],
    requester: Dict[str, Any],
) -> Dict[str, Any]:
    _expire_overdue_dismissals(conn)
    req_uid = int(requester.get("uid") or 0)
    req_nome = str(requester.get("nome") or requester.get("actor_label") or "Administrador")
    if req_uid > 0 and target_uid == req_uid:
        raise ValueError("Você não pode solicitar a demissão de si mesmo.")

    target = conn.execute(
        "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE id = ?",
        (target_uid,),
    ).fetchone()
    if not target:
        raise LookupError("Administrador não encontrado.")
    if not int(target["is_active"] or 0):
        raise ValueError("Este administrador já está inativo.")

    reason = " ".join(str(body.get("reason") or "").split())
    if len(reason) < 5:
        raise ValueError("Informe o motivo da demissão (mínimo de 5 caracteres).")

    existing_pending = conn.execute(
        """
        SELECT id FROM admin_dismissal_requests
        WHERE target_user_id = ? AND status = 'pending'
          AND COALESCE(request_type, 'dismissal') IN ('dismissal', 'resignation')
        """,
        (target_uid,),
    ).fetchone()
    if existing_pending:
        raise ValueError("Já existe uma solicitação de desligamento pendente para este administrador.")

    created_dt = datetime.now(timezone.utc)
    created_at = created_dt.isoformat()
    expires_at = (created_dt + timedelta(days=7)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO admin_dismissal_requests(
            target_user_id, target_numero_usp, target_nome,
            requested_by_user_id, requested_by_nome, reason,
            request_type, status, created_at, expires_at, responded_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'dismissal', 'pending', ?, ?, '')
        """,
        (
            target_uid,
            target["numero_usp"],
            target["nome"],
            req_uid,
            req_nome,
            reason,
            created_at,
            expires_at,
        ),
    )
    _log_audit(
        conn,
        "REQUEST_ADMIN_DISMISSAL",
        f"NUSP {target['numero_usp']}",
        f"{target['nome']} — Motivo: {reason}",
    )
    row = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        WHERE id = ?
        """,
        (int(cur.lastrowid),),
    ).fetchone()
    return {"ok": True, "dismissal": _format_dismissal_row(row)}


def _create_admin_resignation_record(
    conn: Any,
    body: Dict[str, Any],
    requester: Dict[str, Any],
) -> Dict[str, Any]:
    _expire_overdue_dismissals(conn)
    req_uid = int(requester.get("uid") or 0)
    if req_uid <= 0:
        raise ValueError("Sessão administrativa não identificada.")
    target = conn.execute(
        "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE id = ?",
        (req_uid,),
    ).fetchone()
    if not target or not int(target["is_active"] or 0):
        raise ValueError("Conta administrativa não está ativa.")

    other_cnt = conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_users WHERE is_active = 1 AND id != ?",
        (req_uid,),
    ).fetchone()
    if int((other_cnt["cnt"] if other_cnt else 0) or 0) == 0:
        raise ValueError("Cadastre outro administrador ativo antes de pedir demissão.")

    existing_pending = conn.execute(
        """
        SELECT id FROM admin_dismissal_requests
        WHERE target_user_id = ? AND status = 'pending'
          AND COALESCE(request_type, 'dismissal') IN ('dismissal', 'resignation')
        """,
        (req_uid,),
    ).fetchone()
    if existing_pending:
        raise ValueError("Já existe uma solicitação de desligamento pendente para sua conta.")

    reason = " ".join(str(body.get("reason") or "").split()) or "Pedido voluntário de demissão"
    created_dt = datetime.now(timezone.utc)
    created_at = created_dt.isoformat()
    expires_at = (created_dt + timedelta(days=7)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO admin_dismissal_requests(
            target_user_id, target_numero_usp, target_nome,
            requested_by_user_id, requested_by_nome, reason,
            request_type, status, created_at, expires_at, responded_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'resignation', 'pending', ?, ?, '')
        """,
        (
            req_uid,
            target["numero_usp"],
            target["nome"],
            req_uid,
            target["nome"],
            reason,
            created_at,
            expires_at,
        ),
    )
    _log_audit(
        conn,
        "REQUEST_ADMIN_RESIGNATION",
        f"NUSP {target['numero_usp']}",
        f"{target['nome']} pediu demissão (prazo de 7 dias p/ aprovação) — Motivo: {reason}",
    )
    row = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        WHERE id = ?
        """,
        (int(cur.lastrowid),),
    ).fetchone()
    return {"ok": True, "dismissal": _format_dismissal_row(row)}


def _perform_admin_return_request(conn: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    _expire_overdue_dismissals(conn)
    pw = str(body.get("password") or "")
    if not verify_admin_login(conn, pw):
        raise PermissionError("Número USP não autorizado ou senha incorreta.")
    nusp = normalize_nusp(body.get("numero_usp"))
    if len(nusp) < 4 or len(nusp) > 15:
        raise ValueError("Informe um Número USP válido (4 a 15 dígitos).")

    user_row = conn.execute(
        "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE numero_usp = ?",
        (nusp,),
    ).fetchone()
    if user_row is None:
        raise PermissionError("Este Número USP não está cadastrado no sistema.")
    if int(user_row["is_active"] or 0):
        raise ValueError("Sua conta administrativa já está ativa. Faça o login normalmente.")

    reason = " ".join(str(body.get("reason") or "").split())
    if len(reason) < 5:
        raise ValueError("Informe o motivo para retornar (mínimo de 5 caracteres).")

    uid = int(user_row["id"])
    now = _now_iso()
    actor_label = f"{user_row['nome']} ({user_row['numero_usp']})"
    existing = conn.execute(
        """
        SELECT id FROM admin_dismissal_requests
        WHERE target_user_id = ?
          AND COALESCE(request_type, 'dismissal') = 'reactivation'
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (uid,),
    ).fetchone()
    if existing:
        req_id = int(existing["id"])
        conn.execute(
            "UPDATE admin_dismissal_requests SET reason = ?, created_at = ? WHERE id = ?",
            (reason, now, req_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO admin_dismissal_requests(
                target_user_id, target_numero_usp, target_nome,
                requested_by_user_id, requested_by_nome, reason,
                request_type, status, created_at, expires_at, responded_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'reactivation', 'pending', ?, '', '')
            """,
            (
                uid,
                user_row["numero_usp"],
                user_row["nome"],
                uid,
                user_row["nome"],
                reason,
                now,
            ),
        )
        req_id = int(cur.lastrowid)

    _log_audit(
        conn,
        "REQUEST_ADMIN_RETURN",
        f"NUSP {user_row['numero_usp']}",
        f"{user_row['nome']} solicitou retorno — Motivo: {reason}",
        actor=actor_label,
    )
    row = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        WHERE id = ?
        """,
        (req_id,),
    ).fetchone()
    return {
        "ok": True,
        "request": _format_dismissal_row(row),
        "message": "Solicitação de retorno enviada. Aguarde a aprovação de um administrador ativo.",
    }


def _respond_admin_dismissal_record(
    conn: Any,
    dismissal_id: int,
    body: Dict[str, Any],
    current_uid: int,
) -> Dict[str, Any]:
    _expire_overdue_dismissals(conn)
    row = conn.execute(
        """
        SELECT id, target_user_id, target_numero_usp, target_nome,
               requested_by_user_id, requested_by_nome, reason,
               COALESCE(request_type, 'dismissal') AS request_type,
               status, created_at, expires_at, responded_at
        FROM admin_dismissal_requests
        WHERE id = ?
        """,
        (dismissal_id,),
    ).fetchone()
    if not row:
        raise LookupError("Solicitação não encontrada.")

    rtype = str(row["request_type"] or "dismissal").strip().lower()
    target_uid = int(row["target_user_id"])
    if current_uid <= 0:
        raise PermissionError("Sessão administrativa inválida.")
    if rtype == "dismissal" and target_uid != current_uid:
        raise PermissionError("Apenas o administrador destinatário pode aceitar ou recusar esta demissão.")
    if rtype in ("resignation", "reactivation") and target_uid == current_uid:
        raise PermissionError("Outro administrador ativo precisa aprovar ou recusar esta solicitação.")
    if row["status"] != "pending":
        raise ValueError(f"Esta solicitação já foi encerrada ({row['status']}).")

    decision = str(body.get("decision") or "").strip().lower()
    if decision not in ("accept", "reject"):
        raise ValueError("Decisão inválida: informe 'accept' ou 'reject'.")

    now = _now_iso()
    if rtype == "reactivation":
        if decision == "accept":
            conn.execute(
                "UPDATE admin_dismissal_requests SET status = 'accepted', responded_at = ? WHERE id = ?",
                (now, dismissal_id),
            )
            conn.execute("UPDATE admin_users SET is_active = 1 WHERE id = ?", (target_uid,))
            _log_audit(
                conn,
                "APPROVE_ADMIN_RETURN",
                f"NUSP {row['target_numero_usp']}",
                f"Retorno aprovado para {row['target_nome']} — Motivo: {row['reason']}",
            )
            return {"ok": True, "status": "accepted", "logged_out": False, "request_type": rtype}
        conn.execute(
            "UPDATE admin_dismissal_requests SET status = 'rejected', responded_at = ? WHERE id = ?",
            (now, dismissal_id),
        )
        _log_audit(
            conn,
            "REJECT_ADMIN_RETURN",
            f"NUSP {row['target_numero_usp']}",
            f"Retorno recusado para {row['target_nome']}",
        )
        return {"ok": True, "status": "rejected", "logged_out": False, "request_type": rtype}

    if decision == "accept":
        other_cnt = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_users WHERE is_active = 1 AND id != ?",
            (target_uid,),
        ).fetchone()
        if int((other_cnt["cnt"] if other_cnt else 0) or 0) == 0:
            raise ValueError("Não há outro administrador ativo para manter o acesso ao sistema.")
        conn.execute(
            "UPDATE admin_dismissal_requests SET status = 'accepted', responded_at = ? WHERE id = ?",
            (now, dismissal_id),
        )
        conn.execute("UPDATE admin_users SET is_active = 0 WHERE id = ?", (target_uid,))
        conn.execute(
            "UPDATE admin_sessions SET logged_out_at = ? WHERE user_id = ? AND (logged_out_at IS NULL OR logged_out_at = '')",
            (now, target_uid),
        )
        action_name = "ACCEPT_ADMIN_DISMISSAL" if rtype == "dismissal" else "APPROVE_ADMIN_RESIGNATION"
        detail_msg = (
            f"Aceita por {row['target_nome']} — Motivo: {row['reason']}"
            if rtype == "dismissal"
            else f"Pedido de demissão de {row['target_nome']} aprovado — Motivo: {row['reason']}"
        )
        _log_audit(conn, action_name, f"NUSP {row['target_numero_usp']}", detail_msg)
        return {
            "ok": True,
            "status": "accepted",
            "logged_out": current_uid == target_uid,
            "request_type": rtype,
        }

    conn.execute(
        "UPDATE admin_dismissal_requests SET status = 'rejected', responded_at = ? WHERE id = ?",
        (now, dismissal_id),
    )
    action_rej = "REJECT_ADMIN_DISMISSAL" if rtype == "dismissal" else "REJECT_ADMIN_RESIGNATION"
    detail_rej = (
        f"Recusada por {row['target_nome']}"
        if rtype == "dismissal"
        else f"Pedido de demissão de {row['target_nome']} recusado"
    )
    _log_audit(conn, action_rej, f"NUSP {row['target_numero_usp']}", detail_rej)
    return {"ok": True, "status": "rejected", "logged_out": False, "request_type": rtype}


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
    """Atualiza faixas de RAM e prioridades por categoria (e pesos/regras quando informados)."""
    has_weights = "pesos" in body
    parsed_weights: Dict[str, float] = {}
    diff_min = 3.0
    peso_hd = 0.25
    escala_log = 60.0

    if has_weights:
        pesos = body.get("pesos")
        if not isinstance(pesos, dict):
            raise ValueError("Campo 'pesos' inválido.")
        w_keys = {
            "cpu": ("peso_cpu", "Peso CPU", "pesos"),
            "ram": ("peso_ram", "Peso RAM", "pesos"),
            "armazenamento": ("peso_armazenamento", "Peso Armazenamento", "pesos"),
            "gpu": ("peso_gpu", "Peso GPU", "pesos"),
        }
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
    else:
        sm_row = conn.execute("SELECT `value` FROM system_params WHERE `key` = 'score_maximo'").fetchone()
        try:
            score_max = float(sm_row["value"]) if sm_row else 10.0
        except (TypeError, ValueError):
            score_max = 10.0
        if score_max <= 0 or score_max > 100:
            score_max = 10.0

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

    if has_weights:
        w_keys = {
            "cpu": ("peso_cpu", "Peso CPU", "pesos"),
            "ram": ("peso_ram", "Peso RAM", "pesos"),
            "armazenamento": ("peso_armazenamento", "Peso Armazenamento", "pesos"),
            "gpu": ("peso_gpu", "Peso GPU", "pesos"),
        }
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
    audit_detail = (
        (
            f"Pesos CPU={parsed_weights['peso_cpu']*100:.1f}% RAM={parsed_weights['peso_ram']*100:.1f}% "
            f"Arm={parsed_weights['peso_armazenamento']*100:.1f}% GPU={parsed_weights['peso_gpu']*100:.1f}%; "
            f"{len(ram_entries)} faixas RAM; {len(cat_entries)} categorias"
        )
        if has_weights
        else f"{len(ram_entries)} faixas RAM; {len(cat_entries)} categorias"
    )
    _log_audit(conn, "UPDATE_CRITERIA", "rules", audit_detail)


def _update_system_params(conn: Any, updates: Dict[str, Any]) -> None:
    """Atualiza parâmetros numéricos e privacidade com validação consistente."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Campo 'params' deve ser um objeto chave-valor não vazio.")

    current_rows = conn.execute("SELECT `key`, `value` FROM system_params").fetchall()
    current = {r["key"]: r["value"] for r in current_rows}
    weight_keys = ("peso_cpu", "peso_ram", "peso_armazenamento", "peso_gpu")
    normalized_updates: Dict[str, str] = {}

    if any(k in updates for k in weight_keys):
        w_vals: Dict[str, float] = {}
        for k in weight_keys:
            raw_v = updates.get(k, current.get(k, "0"))
            try:
                v = float(str(raw_v).replace(",", "."))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Valor inválido para '{k}'.") from exc
            if v < 0.0 or v > 1.0:
                raise ValueError(f"O peso '{k}' deve estar entre 0% e 100%.")
            w_vals[k] = round(v, 6)
            if k in updates:
                normalized_updates[k] = f"{w_vals[k]:g}"
        w_sum = sum(w_vals.values())
        if abs(w_sum - 1.0) > 0.002:
            raise ValueError(f"A soma dos 4 pesos deve ser 100% (atual: {w_sum * 100:.1f}%).")

    numeric_bounds = {
        "diferenca_minima_upgrade": (0.0, 100.0, True, "Diferença mínima para upgrade"),
        "armaz_peso_hd": (0.0, 2.0, True, "Peso relativo do HD"),
        "armaz_escala_log": (0.0, 10000.0, False, "Fator de suavização log"),
        "score_maximo": (0.0, 100.0, False, "Score máximo componente"),
    }
    for k, val in updates.items():
        if k in weight_keys:
            continue
        sval = str(val).strip()
        if k in numeric_bounds:
            lo, hi, allow_lo, label = numeric_bounds[k]
            try:
                nv = float(sval.replace(",", "."))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Valor numérico inválido para {label}.") from exc
            if (nv < lo if allow_lo else nv <= lo) or nv > hi:
                raise ValueError(f"{label} fora do intervalo permitido.")
            normalized_updates[k] = f"{round(nv, 6):g}"
        elif k == "public_anonymize_names":
            if sval not in ("Sim", "Não"):
                raise ValueError("Valor inválido para anonimização pública.")
            normalized_updates[k] = sval
        else:
            normalized_updates[k] = sval

    for k, v in normalized_updates.items():
        conn.execute("UPDATE system_params SET `value` = ? WHERE `key` = ?", (v, k))
    _log_audit(conn, "UPDATE_PARAMS", "system_params", f"Chaves atualizadas: {list(normalized_updates.keys())}")


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


def _format_material_request_row(r: Any) -> Dict[str, Any]:
    d = dict(r)
    fallback_date = str(d.get("created_at") or "")[:10]
    try:
        raw_items = json.loads(d.get("items_json") or "[]")
    except Exception:
        raw_items = []
    items = []
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            item_copy = dict(it)
            if not item_copy.get("adicionado_em") and fallback_date:
                item_copy["adicionado_em"] = fallback_date
            items.append(item_copy)
    return {
        "id": d["id"],
        "created_at": d["created_at"],
        "orientador": d.get("orientador") or "",
        "laboratorio": d.get("laboratorio") or "",
        "data_solicitacao": d.get("data_solicitacao") or "",
        "items": items,
        "total_valor": d.get("total_valor"),
        "justificativa": d.get("justificativa") or "",
    }


def _fetch_material_request_row(conn: Any, req_id: int) -> Any:
    return conn.execute(
        """
        SELECT id, created_at, orientador, laboratorio, data_solicitacao,
               items_json, total_valor, justificativa
        FROM material_requests
        WHERE id = ?
        """,
        (req_id,),
    ).fetchone()


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
    history = [_format_material_request_row(r) for r in rows]

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

    def _extract_raw_claims(self) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(SESSION_COOKIE_NAME) or extract_session_from_headers(
            request.headers.get("Cookie")
        )
        return parse_session_token(token or "")

    def _resolve_request_admin(self, touch: bool = False) -> Optional[Dict[str, Any]]:
        claims = self._extract_raw_claims()
        if not claims:
            self._admin_user = None
            return None
        with db_session() as conn:
            resolved = _resolve_session_claims(conn, claims)
            if resolved and touch:
                _touch_admin_session(conn, resolved)
        self._admin_user = resolved
        return resolved

    def _is_admin(self) -> bool:
        return self._resolve_request_admin(touch=False) is not None

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
        resolved = self._resolve_request_admin(touch=True)
        if not resolved:
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
                claims = self._extract_raw_claims()
                with db_session() as conn:
                    needs_bootstrap = _needs_admin_bootstrap(conn)
                    resolved = _resolve_session_claims(conn, claims)
                    if resolved:
                        _touch_admin_session(conn, resolved)
                        pending_dismissals = _get_pending_dismissals_for_user(conn, resolved["uid"])
                    else:
                        pending_dismissals = []
                user_out = (
                    {
                        "id": resolved["uid"],
                        "numero_usp": resolved["nusp"],
                        "nome": resolved["nome"],
                        "session_id": resolved["sid"],
                    }
                    if resolved
                    else None
                )
                self._send_json(
                    {
                        "is_admin": resolved is not None,
                        "needs_bootstrap": needs_bootstrap,
                        "user": user_out,
                        "pending_dismissals": pending_dismissals,
                    }
                )
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
                tok = _CURRENT_ADMIN_ACTOR.set((getattr(self, "_admin_user", None) or {}).get("actor_label") or "")
                try:
                    self._route_admin_get(path, qs)
                finally:
                    _CURRENT_ADMIN_ACTOR.reset(tok)
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
                with db_session() as conn:
                    status, payload, token = _perform_admin_login(conn, body)
                self._send_json(payload, status=status, cookie_token=token)
                return self._response  # type: ignore[return-value]

            if path == "/api/auth/request-return":
                body = self._read_json_body()
                with db_session() as conn:
                    res = _perform_admin_return_request(conn, body)
                self._send_json(res, status=HTTPStatus.CREATED)
                return self._response  # type: ignore[return-value]

            if path == "/api/auth/logout":
                claims = self._extract_raw_claims()
                with db_session() as conn:
                    _perform_admin_logout(conn, claims)
                self._send_json({"is_admin": False}, clear_cookie=True)
                return self._response  # type: ignore[return-value]

            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                body = self._read_json_body()
                tok = _CURRENT_ADMIN_ACTOR.set((getattr(self, "_admin_user", None) or {}).get("actor_label") or "")
                try:
                    self._route_admin_post(path, body)
                finally:
                    _CURRENT_ADMIN_ACTOR.reset(tok)
                return self._response  # type: ignore[return-value]

            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
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
                tok = _CURRENT_ADMIN_ACTOR.set((getattr(self, "_admin_user", None) or {}).get("actor_label") or "")
                try:
                    self._route_admin_put(path, body)
                finally:
                    _CURRENT_ADMIN_ACTOR.reset(tok)
                return self._response  # type: ignore[return-value]
            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._response  # type: ignore[return-value]

    # ---------------- DELETE ----------------
    def handle_delete(self, path: str) -> Response:
        try:
            if path.startswith("/api/admin/"):
                if not self._require_admin():
                    return self._response  # type: ignore[return-value]
                tok = _CURRENT_ADMIN_ACTOR.set((getattr(self, "_admin_user", None) or {}).get("actor_label") or "")
                try:
                    self._route_admin_delete(path)
                finally:
                    _CURRENT_ADMIN_ACTOR.reset(tok)
                return self._response  # type: ignore[return-value]
            self._send_json({"error": "Endpoint não encontrado."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
        except LookupError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
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
                        "SELECT id, timestamp, action, target, details, COALESCE(actor, '') AS actor FROM audit_log ORDER BY id DESC LIMIT 15"
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
                        "SELECT id, timestamp, action, target, details, COALESCE(actor, '') AS actor FROM audit_log ORDER BY id DESC LIMIT 100"
                    ).fetchall()
                ]
                self._send_json({"items": rows})
                return

            if path == "/api/admin/collab":
                admin_info = getattr(self, "_admin_user", None) or {}
                tab_arg = (qs.get("tab", [""])[0] or "").strip()
                if tab_arg:
                    _touch_admin_session(conn, admin_info, active_tab=tab_arg)
                self._send_json(_get_collab_payload(conn, admin_info))
                return

            if path == "/api/admin/users":
                self._send_json(_get_admin_users_payload(conn))
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

            if path == "/api/admin/collab/heartbeat":
                admin_info = getattr(self, "_admin_user", None) or {}
                _touch_admin_session(conn, admin_info, active_tab=str(body.get("active_tab") or ""))
                self._send_json(_get_collab_payload(conn, admin_info))
                return

            if path == "/api/admin/collab/chat":
                admin_info = getattr(self, "_admin_user", None) or {}
                msg = " ".join(str(body.get("message") or "").split())
                if not msg:
                    raise ValueError("Digite uma mensagem para enviar.")
                if len(msg) > 1000:
                    raise ValueError("Mensagem muito longa (máximo de 1000 caracteres).")
                _touch_admin_session(conn, admin_info, active_tab=str(body.get("active_tab") or ""))
                conn.execute(
                    """
                    INSERT INTO admin_chat_messages(user_id, numero_usp, author_name, message, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(admin_info.get("uid") or 0),
                        str(admin_info.get("nusp") or ""),
                        str(admin_info.get("nome") or "Administrador"),
                        msg,
                        _now_iso(),
                    ),
                )
                self._send_json(_get_collab_payload(conn, admin_info), status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/users":
                creator = (getattr(self, "_admin_user", None) or {}).get("actor_label") or "Administrador"
                res = _create_admin_user_record(conn, body, creator)
                self._send_json(res, status=HTTPStatus.CREATED)
                return

            if path.startswith("/api/admin/users/") and path.endswith("/dismiss"):
                target_uid = int(path.split("/")[4])
                res = _create_admin_dismissal_record(
                    conn,
                    target_uid,
                    body,
                    getattr(self, "_admin_user", None) or {},
                )
                self._send_json(res, status=HTTPStatus.CREATED)
                return

            if path == "/api/admin/resignations":
                res = _create_admin_resignation_record(
                    conn,
                    body,
                    getattr(self, "_admin_user", None) or {},
                )
                self._send_json(res, status=HTTPStatus.CREATED)
                return

            if path.startswith("/api/admin/dismissals/") and path.endswith("/respond"):
                dismissal_id = int(path.split("/")[4])
                current_uid = int((getattr(self, "_admin_user", None) or {}).get("uid") or 0)
                res = _respond_admin_dismissal_record(conn, dismissal_id, body, current_uid)
                self._send_json(res, clear_cookie=bool(res.get("logged_out")))
                return

            if path == "/api/admin/requests":
                payload = body
                if body.get("reset_item_dates") and isinstance(body.get("items"), list):
                    payload = {
                        **body,
                        "items": [
                            {k: v for k, v in it.items() if k != "adicionado_em"}
                            if isinstance(it, dict)
                            else it
                            for it in body["items"]
                        ],
                    }
                req = normalize_material_request(payload, default_item_date=_today_str())
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
                saved = _fetch_material_request_row(conn, int(cur.lastrowid))
                self._send_json(
                    {"ok": True, "id": cur.lastrowid, "request": _format_material_request_row(saved)},
                    status=HTTPStatus.CREATED,
                )
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

            if path.startswith("/api/admin/allocations/"):
                aloc_pk = int(path.split("/")[4])
                al = conn.execute(
                    "SELECT id, id_alocacao, pessoa_nome, pc_id_pc FROM allocations WHERE id = ?",
                    (aloc_pk,),
                ).fetchone()
                if not al:
                    raise LookupError("Alocação não encontrada.")
                obs = (body.get("observacoes") or "").strip()
                conn.execute("UPDATE allocations SET observacoes = ? WHERE id = ?", (obs, aloc_pk))
                _log_audit(
                    conn,
                    "UPDATE_ALLOCATION_OBS",
                    al["id_alocacao"],
                    f"{al['pessoa_nome']} ({al['pc_id_pc']}) · Obs={obs or '(vazia)'}",
                )
                self._send_json({"ok": True, "observacoes": obs})
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

            if path.startswith("/api/admin/requests/"):
                req_pk = int(path.split("/")[4])
                existing = _fetch_material_request_row(conn, req_pk)
                if not existing:
                    self._send_json({"error": "Solicitação não encontrada."}, status=HTTPStatus.NOT_FOUND)
                    return
                req = incorporate_material_request(dict(existing), body, _today_str())
                conn.execute(
                    """
                    UPDATE material_requests
                    SET orientador = ?, laboratorio = ?, data_solicitacao = ?,
                        items_json = ?, total_valor = ?, justificativa = ?
                    WHERE id = ?
                    """,
                    (
                        req["orientador"],
                        req["laboratorio"],
                        req["data_solicitacao"],
                        json.dumps(req["items"], ensure_ascii=False),
                        req["total_valor"],
                        req["justificativa"],
                        req_pk,
                    ),
                )
                _log_audit(
                    conn,
                    "UPDATE_MATERIAL_REQUEST",
                    f"req#{req_pk}",
                    f"{len(req['items'])} itens · Total={req['total_fmt'] or '-'}",
                )
                updated = _fetch_material_request_row(conn, req_pk)
                self._send_json({"ok": True, "id": req_pk, "request": _format_material_request_row(updated)})
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
                _update_system_params(conn, body.get("params") or {})
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

            if path.startswith("/api/admin/users/"):
                user_pk = int(path.split("/")[4])
                current_uid = int((getattr(self, "_admin_user", None) or {}).get("uid") or 0)
                res = _update_admin_user_record(conn, user_pk, body, current_uid=current_uid)
                self._send_json(res)
                return

            self._send_json({"error": "Rota PUT administrativa não encontrada."}, status=HTTPStatus.NOT_FOUND)

    def _route_admin_delete(self, path: str) -> None:
        with db_session() as conn:
            if path.startswith("/api/admin/users/"):
                raise PermissionError(
                    "A exclusão unilateral foi substituída pela solicitação de demissão com prazo de 7 dias."
                )
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
