"""Autenticação do administrador via hash scrypt pós-quântico (512 bits) e sessão HMAC-SHA512."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from typing import Any, Dict, Optional, Tuple

from .config import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from .db import db_session

_PQ_PREFIX = "pq_scrypt"
_PQ_N = 32768
_PQ_R = 8
_PQ_P = 1
_PQ_DKLEN = 64  # 512 bits -> 256 bits de segurança pós-quântica (Grover)

_SESSION_SECRET_LOCK = threading.Lock()
_CACHED_SESSION_SECRET: Optional[bytes] = None


def hash_password(password: str) -> str:
    """Gera hash resistente a ataques quânticos (scrypt 512-bit + salt 256-bit)."""
    salt = secrets.token_bytes(32)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_PQ_N,
        r=_PQ_R,
        p=_PQ_P,
        dklen=_PQ_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return f"{_PQ_PREFIX}${_PQ_N}${_PQ_R}${_PQ_P}${salt.hex()}${digest.hex()}"


def _needs_rehash(stored_hash: str) -> bool:
    return not stored_hash.startswith(f"{_PQ_PREFIX}$")


def verify_password_hash(password: str, stored_hash: str) -> bool:
    """Compara senha em tempo constante com o hash scrypt armazenado."""
    try:
        if stored_hash.startswith(f"{_PQ_PREFIX}$"):
            _, n_s, r_s, p_s, salt_hex, digest_hex = stored_hash.split("$", 5)
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        else:
            salt_hex, digest_hex = stored_hash.split("$", 1)
            n, r, p = 16384, 8, 1
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False

    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=64 * 1024 * 1024,
    )
    return hmac.compare_digest(actual, expected)


def has_admin_password(conn: Any) -> bool:
    """Verifica se já existe senha de administrador configurada."""
    row = conn.execute(
        "SELECT `value` FROM admin_config WHERE `key` = 'password_hash'"
    ).fetchone()
    return row is not None and bool(row["value"])


def set_admin_password(conn: Any, password: str) -> None:
    """Define ou atualiza o hash de senha do administrador no MySQL."""
    if len(password) < 6:
        raise ValueError("A senha do administrador deve ter pelo menos 6 caracteres.")
    hashed = hash_password(password)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO admin_config(`key`, `value`) VALUES('password_hash', ?)
        ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
        """,
        (hashed,),
    )
    conn.execute(
        """
        INSERT INTO admin_config(`key`, `value`) VALUES('password_updated_at', ?)
        ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
        """,
        (now,),
    )


def ensure_admin_password(conn: Any) -> Optional[str]:
    """Garante senha inicial gerada aleatoriamente caso não exista hash no banco."""
    if has_admin_password(conn):
        return None
    generated = secrets.token_urlsafe(12)
    set_admin_password(conn, generated)
    return generated


def verify_admin_login(conn: Any, password: str) -> bool:
    """Valida a senha submetida e atualiza hashes legados para o formato pós-quântico."""
    if not password:
        return False
    row = conn.execute(
        "SELECT `value` FROM admin_config WHERE `key` = 'password_hash'"
    ).fetchone()
    if row is None:
        return False
    stored_hash = row["value"]
    if not verify_password_hash(password, stored_hash):
        return False
    if _needs_rehash(stored_hash):
        set_admin_password(conn, password)
    return True


def get_session_secret() -> bytes:
    """Obtém a chave HMAC-SHA512 de sessão via env ou tabela admin_config no MySQL."""
    global _CACHED_SESSION_SECRET
    env_secret = os.environ.get("LSEE_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")

    if _CACHED_SESSION_SECRET is not None:
        return _CACHED_SESSION_SECRET

    with _SESSION_SECRET_LOCK:
        if _CACHED_SESSION_SECRET is not None:
            return _CACHED_SESSION_SECRET
        with db_session() as conn:
            row = conn.execute(
                "SELECT `value` FROM admin_config WHERE `key` = 'session_secret'"
            ).fetchone()
            if row is not None and row["value"]:
                _CACHED_SESSION_SECRET = row["value"].encode("utf-8")
                return _CACHED_SESSION_SECRET
            generated = secrets.token_hex(64)  # 512 bits
            conn.execute(
                """
                INSERT INTO admin_config(`key`, `value`) VALUES('session_secret', ?)
                ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
                """,
                (generated,),
            )
            _CACHED_SESSION_SECRET = generated.encode("utf-8")
            return _CACHED_SESSION_SECRET


def normalize_nusp(raw: Any) -> str:
    """Mantém apenas dígitos do Número USP para comparação consistente."""
    return re.sub(r"\D", "", str(raw or ""))


def authenticate_admin_user(
    conn: Any,
    numero_usp: Any,
    password: str,
    bootstrap_name: str = "",
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Valida a senha administrativa única e garante que o Número USP esteja cadastrado."""
    nusp = normalize_nusp(numero_usp)
    if not verify_admin_login(conn, password):
        return None, "Número USP não autorizado ou senha incorreta.", "BAD_PASSWORD"

    if len(nusp) < 4 or len(nusp) > 15:
        return None, "Informe um Número USP válido (4 a 15 dígitos).", "INVALID_NUSP"

    row_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM admin_users").fetchone()
    total_admins = int(row_cnt["cnt"] or 0) if row_cnt is not None else 0
    if total_admins == 0:
        clean_name = " ".join(str(bootstrap_name or "").split())
        if len(clean_name) < 2:
            return (
                None,
                "Primeiro acesso: informe também seu Nome junto ao Número USP para cadastrar o 1º administrador.",
                "BOOTSTRAP_NAME_REQUIRED",
            )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO admin_users(numero_usp, nome, is_active, created_at, created_by)
            VALUES (?, ?, 1, ?, 'bootstrap')
            """,
            (nusp, clean_name, now),
        )
        created = conn.execute(
            "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE numero_usp = ?",
            (nusp,),
        ).fetchone()
        return dict(created), "", "BOOTSTRAP_OK"

    user_row = conn.execute(
        "SELECT id, numero_usp, nome, is_active FROM admin_users WHERE numero_usp = ?",
        (nusp,),
    ).fetchone()
    if user_row is None:
        return None, "Acesso negado: este Número USP não está cadastrado como administrador.", "UNREGISTERED_NUSP"
    if not int(user_row["is_active"] or 0):
        return (
            None,
            "Sua conta administrativa está desativada. Informe o motivo para solicitar seu retorno.",
            "INACTIVE_NUSP",
        )
    return dict(user_row), "", "OK"


def create_session_token(
    claims: Optional[Dict[str, Any]] = None,
    ttl: int = SESSION_TTL_SECONDS,
) -> str:
    """Emite token de sessão assinado com HMAC-SHA512 (com identificação opcional de ADM)."""
    expiry = int(time.time()) + ttl
    nonce = secrets.token_hex(32)
    if claims:
        raw_claims = json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        b64_claims = base64.urlsafe_b64encode(raw_claims).decode("ascii").rstrip("=")
        payload = f"{expiry}.{nonce}.{b64_claims}"
    else:
        payload = f"{expiry}.{nonce}"
    sig = hmac.new(get_session_secret(), payload.encode("utf-8"), hashlib.sha512).hexdigest()
    return f"{payload}.{sig}"


def parse_session_token(token: str) -> Optional[Dict[str, Any]]:
    """Valida assinatura HMAC-SHA512 e expiração do token e retorna os dados do ADM."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) not in (3, 4):
        return None
    expiry_str = parts[0]
    sig = parts[-1]
    payload = ".".join(parts[:-1])
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if expiry < int(time.time()):
        return None
    expected_sig = hmac.new(
        get_session_secret(), payload.encode("utf-8"), hashlib.sha512
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if len(parts) == 3:
        return {"uid": 0, "nusp": "", "nome": "Administrador", "sid": ""}
    try:
        padded = parts[2] + "=" * (-len(parts[2]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return {
            "uid": int(decoded.get("uid") or 0),
            "nusp": str(decoded.get("nusp") or ""),
            "nome": str(decoded.get("nome") or "Administrador"),
            "sid": str(decoded.get("sid") or ""),
        }
    except Exception:
        return None


def verify_session_token(token: str) -> bool:
    """Valida assinatura HMAC-SHA512 e expiração do token de sessão."""
    return parse_session_token(token) is not None


def extract_session_from_headers(cookie_header: Optional[str]) -> Optional[str]:
    """Extrai o valor do cookie de sessão do cabeçalho HTTP Cookie."""
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel is not None else None


def is_secure_cookie_env() -> bool:
    return os.environ.get("VERCEL") == "1" or os.environ.get("LSEE_SECURE_COOKIE") == "1"
