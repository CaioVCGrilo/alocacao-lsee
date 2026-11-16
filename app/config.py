"""Configurações da aplicação, carregamento de variáveis de ambiente e caminhos."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

STATIC_DIR = BASE_DIR / "static"
DEFAULT_XLSX_PATH = Path(
    os.environ.get(
        "LSEE_XLSX_PATH",
        BASE_DIR.parent / "Alocacao_Recursos_Lab_2026_2.xlsx",
    )
)

SESSION_COOKIE_NAME = "lsee_admin_session"
SESSION_TTL_SECONDS = 8 * 3600
DEFAULT_HOST = os.environ.get("LSEE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("LSEE_PORT", "8080"))


def get_db_config() -> dict:
    """Retorna os parâmetros de conexão MySQL a partir das variáveis de ambiente."""
    host = os.environ.get("DB_HOST", "").strip()
    user = os.environ.get("DB_USER", "").strip()
    password = os.environ.get("DB_PASSWORD", "")
    database = os.environ.get("DB_NAME", "").strip()
    port = int(os.environ.get("DB_PORT", "3306"))

    missing = [
        name
        for name, val in (
            ("DB_HOST", host),
            ("DB_USER", user),
            ("DB_PASSWORD", password),
            ("DB_NAME", database),
        )
        if not val
    ]
    if missing:
        raise RuntimeError(
            "Variáveis de ambiente do banco não configuradas: " + ", ".join(missing)
        )

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }
