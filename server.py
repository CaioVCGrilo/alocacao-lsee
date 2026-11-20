"""Ponto de entrada CLI do sistema web de alocação de recursos do LSEE."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import subprocess
import sys
import time

from datetime import datetime, timezone
from pathlib import Path

from app.api import run_server
from app.auth import ensure_admin_password, set_admin_password
from app.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_XLSX_PATH
from app.db import db_session, init_schema
from app.importer import import_xlsx_into_db
from app.patrimonies_io import (
    diff_and_upsert_patrimonies,
    merge_patrimony_batches,
    parse_patrimonies_csv,
)


def bootstrap(force_reimport: bool = False) -> None:
    """Inicializa schema, importa planilha na primeira execução e garante senha admin."""
    init_schema()
    with db_session() as conn:
        pc_count = conn.execute("SELECT COUNT(*) FROM computers").fetchone()[0]
        if force_reimport or pc_count == 0:
            if DEFAULT_XLSX_PATH.exists():
                counts = import_xlsx_into_db(conn, DEFAULT_XLSX_PATH)
                print(f"Dados importados de '{DEFAULT_XLSX_PATH.name}': {counts}")
            else:
                print(
                    f"Aviso: planilha padrão não encontrada em {DEFAULT_XLSX_PATH}.",
                    file=sys.stderr,
                )
        generated_pw = ensure_admin_password(conn)
        if generated_pw:
            print("=" * 64)
            print("SENHA INICIAL DE ADMINISTRADOR GERADA (ANOTE E ALTERE DEPOIS):")
            print(f"  Senha: {generated_pw}")
            print("Para alterar quando quiser: python server.py --set-password")
            print("=" * 64)


def handle_set_password() -> None:
    """Atualiza interativamente a senha de administrador no banco SQLite."""
    init_schema()
    pw1 = getpass.getpass("Nova senha de administrador (mín. 6 caracteres): ")
    pw2 = getpass.getpass("Confirme a nova senha: ")
    if pw1 != pw2:
        print("Erro: as senhas não conferem.", file=sys.stderr)
        sys.exit(1)
    with db_session() as conn:
        set_admin_password(conn, pw1)
    print("Senha de administrador atualizada com sucesso.")


def _stop_previous_lsee_servers(port: int) -> None:
    """Encerra instâncias anteriores do server.py do LSEE antes de subir na mesma porta."""
    my_pid = os.getpid()
    pids: set[int] = set()

    if sys.platform == "win32":
        ps_cmd = (
            f"$p = @(); "
            f"$p += (Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
            f"| Where-Object {{ $_.ProcessId -ne {my_pid} -and $_.CommandLine -match 'server\\.py' -and $_.CommandLine -match '--port\\s+{port}' }} "
            f"| Select-Object -ExpandProperty ProcessId); "
            f"$listen = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess; "
            f"foreach ($lp in $listen) {{ "
            f"  $cmd = (Get-CimInstance Win32_Process -Filter \"ProcessId=$lp\" -ErrorAction SilentlyContinue).CommandLine; "
            f"  if ($cmd -match 'server\\.py') {{ $p += $lp }} "
            f"}}; "
            f"$p | Sort-Object -Unique"
        )
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True,
                timeout=8,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) != my_pid:
                    pids.add(int(line))
        except Exception:
            pass

    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Encerrando instância anterior do servidor LSEE (PID {pid})...")
        except OSError:
            pass

    if pids:
        time.sleep(0.6)


def handle_import_patrimonies(csv_paths: list[str]) -> None:
    """Importa um ou mais arquivos CSV de patrimônios diretamente para o MySQL."""
    init_schema()
    batches = []
    source_names = []
    for raw_path in csv_paths:
        p = Path(raw_path)
        if not p.is_file():
            print(f"Erro: arquivo não encontrado: {p}", file=sys.stderr)
            sys.exit(1)
        recs = parse_patrimonies_csv(p.read_bytes(), source_name=p.name)
        print(f"Lido '{p.name}': {len(recs)} itens")
        batches.append(recs)
        source_names.append(p.name)
    merged = merge_patrimony_batches(batches, source_names=source_names)
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        res = diff_and_upsert_patrimonies(conn, merged, now_iso, apply_changes=True)
    print(
        f"Patrimônios incorporados no MySQL: {res['total']} processados "
        f"({len(res['added'])} novos, {len(res['updated'])} atualizados, {res['unchanged']} sem alteração)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor web LSEE - Alocação de Recursos")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Endereço bind (use 0.0.0.0 para rede local)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Porta HTTP (padrão: 8080)")
    parser.add_argument("--reimport", action="store_true", help="Reimporta os dados da planilha .xlsx")
    parser.add_argument("--set-password", action="store_true", help="Define/altera a senha de administrador")
    parser.add_argument(
        "--import-patrimonies",
        nargs="+",
        metavar="CSV",
        help="Importa um ou mais arquivos CSV de patrimônios para o MySQL",
    )
    parser.add_argument("--init-only", action="store_true", help="Inicializa banco/importação sem subir o servidor")
    parser.add_argument("--restart", action="store_true", help="Substitui instância anterior do servidor LSEE na mesma porta")
    parser.add_argument("--open-browser", action="store_true", help="Abre o navegador após iniciar o servidor")
    args = parser.parse_args()

    if args.set_password:
        handle_set_password()
        return

    if args.import_patrimonies:
        handle_import_patrimonies(args.import_patrimonies)
        if args.init_only:
            return

    bootstrap(force_reimport=args.reimport)
    if args.init_only:
        return

    if args.restart:
        _stop_previous_lsee_servers(args.port)

    run_server(args.host, args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    main()
