"""Camada de acesso ao banco MySQL e inicialização de schema."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .config import get_db_config

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


class RowDict(dict):
    """Dicionário compatível com acesso por nome de coluna e por índice inteiro."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _qmark_to_pyformat(sql: str) -> str:
    """Converte placeholders '?' em '%s' fora de literais de string SQL."""
    if "?" not in sql:
        return sql
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class MySQLCursorWrapper:
    """Wrapper sobre cursor PyMySQL retornando RowDict."""

    def __init__(self, cursor: DictCursor) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> Optional[RowDict]:
        row = self._cursor.fetchone()
        return RowDict(row) if row is not None else None

    def fetchall(self) -> list[RowDict]:
        return [RowDict(r) for r in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[RowDict]:
        for r in self._cursor:
            yield RowDict(r)


class MySQLConnectionWrapper:
    """Wrapper de conexão PyMySQL com interface compatível com execute/executemany."""

    def __init__(self, raw_conn: pymysql.connections.Connection) -> None:
        self._conn = raw_conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> MySQLCursorWrapper:
        cur = self._conn.cursor()
        converted = _qmark_to_pyformat(sql)
        if params:
            cur.execute(converted, tuple(params))
        else:
            cur.execute(converted)
        return MySQLCursorWrapper(cur)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> MySQLCursorWrapper:
        cur = self._conn.cursor()
        converted = _qmark_to_pyformat(sql)
        cur.executemany(converted, [tuple(p) for p in seq_of_params])
        return MySQLCursorWrapper(cur)

    def executescript(self, script: str) -> None:
        cur = self._conn.cursor()
        for stmt in script.split(";"):
            cleaned = stmt.strip()
            if cleaned:
                cur.execute(cleaned)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS admin_config (
        `key` VARCHAR(191) PRIMARY KEY,
        `value` TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS system_params (
        `key` VARCHAR(191) PRIMARY KEY,
        label VARCHAR(255) NOT NULL,
        `value` TEXT NOT NULL,
        category VARCHAR(100) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS score_thresholds (
        id INT AUTO_INCREMENT PRIMARY KEY,
        metric VARCHAR(100) NOT NULL,
        min_value DOUBLE NOT NULL,
        score DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        name VARCHAR(191) PRIMARY KEY,
        priority_group INT,
        participa_upgrade TINYINT NOT NULL DEFAULT 0,
        precisa_pc TINYINT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS processors (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(191) NOT NULL UNIQUE,
        threads INT NOT NULL,
        multi DOUBLE NOT NULL,
        single DOUBLE NOT NULL,
        indice_cpu DOUBLE NOT NULL,
        fonte TEXT,
        data_referencia VARCHAR(100),
        observacoes TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS computers (
        id INT AUTO_INCREMENT PRIMARY KEY,
        id_pc VARCHAR(100),
        patrimonio VARCHAR(100),
        processor_name VARCHAR(191),
        ram_gb DOUBLE,
        ssd_gb DOUBLE,
        hd_gb DOUBLE,
        possui_gpu VARCHAR(100),
        tipo_de_uso VARCHAR(100),
        status_manual VARCHAR(100),
        localizacao VARCHAR(191),
        projeto VARCHAR(255),
        observacoes TEXT,
        score_cpu_legado DOUBLE,
        score_armaz_legado DOUBLE,
        INDEX idx_computers_id_pc (id_pc)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS people (
        id INT AUTO_INCREMENT PRIMARY KEY,
        id_pessoa VARCHAR(100) NOT NULL UNIQUE,
        nome VARCHAR(255) NOT NULL,
        categoria VARCHAR(100) NOT NULL,
        ano_entrada INT,
        semestre_entrada INT,
        situacao VARCHAR(100) NOT NULL,
        data_saida VARCHAR(50),
        observacoes TEXT,
        data_ultimo_upgrade VARCHAR(50)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS allocations (
        id INT AUTO_INCREMENT PRIMARY KEY,
        id_alocacao VARCHAR(100) NOT NULL UNIQUE,
        pessoa_nome VARCHAR(255) NOT NULL,
        id_pessoa VARCHAR(100),
        pc_id_pc VARCHAR(100) NOT NULL,
        data_inicio VARCHAR(50),
        data_fim VARCHAR(50),
        tipo VARCHAR(100) NOT NULL,
        motivo_inicio TEXT,
        motivo_fim TEXT,
        observacoes TEXT,
        origem VARCHAR(100),
        INDEX idx_allocations_pc (pc_id_pc),
        INDEX idx_allocations_pessoa (id_pessoa)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS monitors (
        id INT AUTO_INCREMENT PRIMARY KEY,
        id_monitor VARCHAR(100) NOT NULL UNIQUE,
        patrimonio VARCHAR(100),
        marca_modelo VARCHAR(255),
        tipo VARCHAR(100),
        tamanho VARCHAR(100),
        status VARCHAR(100),
        localizacao VARCHAR(191),
        associado_pc VARCHAR(100),
        projeto VARCHAR(255),
        observacoes TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS network_devices (
        id INT AUTO_INCREMENT PRIMARY KEY,
        categoria VARCHAR(100) NOT NULL,
        equipamento VARCHAR(255) NOT NULL,
        ip VARCHAR(100),
        mac VARCHAR(100),
        observacoes TEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS upgrade_decisions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        pc_oferta VARCHAR(100) NOT NULL,
        id_pessoa VARCHAR(100) NOT NULL,
        decisao VARCHAR(100) NOT NULL,
        observacoes TEXT,
        created_at VARCHAR(100) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS map_stations (
        station_code VARCHAR(100) PRIMARY KEY,
        room VARCHAR(100) NOT NULL,
        station_kind VARCHAR(100) NOT NULL,
        occupant_mode VARCHAR(100) NOT NULL,
        id_pessoa VARCHAR(100),
        custom_label VARCHAR(255),
        pc_id_pc VARCHAR(100),
        observacoes TEXT,
        updated_at VARCHAR(100)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS site_contents (
        `key` VARCHAR(191) PRIMARY KEY,
        content LONGTEXT NOT NULL,
        updated_at VARCHAR(100)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS material_requests (
        id INT AUTO_INCREMENT PRIMARY KEY,
        created_at VARCHAR(100) NOT NULL,
        orientador VARCHAR(255),
        laboratorio VARCHAR(255),
        data_solicitacao VARCHAR(100),
        items_json LONGTEXT NOT NULL,
        total_valor DOUBLE,
        justificativa LONGTEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        numero_usp VARCHAR(32) NOT NULL UNIQUE,
        nome VARCHAR(255) NOT NULL,
        is_active TINYINT NOT NULL DEFAULT 1,
        created_at VARCHAR(100) NOT NULL,
        created_by VARCHAR(255) DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_sessions (
        session_id VARCHAR(64) PRIMARY KEY,
        user_id INT NOT NULL,
        numero_usp VARCHAR(32) NOT NULL,
        nome VARCHAR(255) NOT NULL,
        active_tab VARCHAR(100) DEFAULT 'dashboard',
        logged_in_at VARCHAR(100) NOT NULL,
        last_seen_at VARCHAR(100) NOT NULL,
        logged_out_at VARCHAR(100) DEFAULT '',
        INDEX idx_admin_sessions_seen (last_seen_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_chat_messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        numero_usp VARCHAR(32) NOT NULL,
        author_name VARCHAR(255) NOT NULL,
        message TEXT NOT NULL,
        created_at VARCHAR(100) NOT NULL,
        INDEX idx_admin_chat_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_dismissal_requests (
        id INT AUTO_INCREMENT PRIMARY KEY,
        target_user_id INT NOT NULL,
        target_numero_usp VARCHAR(32) NOT NULL,
        target_nome VARCHAR(255) NOT NULL,
        requested_by_user_id INT NOT NULL,
        requested_by_nome VARCHAR(255) NOT NULL,
        reason TEXT NOT NULL,
        request_type VARCHAR(32) NOT NULL DEFAULT 'dismissal',
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        created_at VARCHAR(100) NOT NULL,
        expires_at VARCHAR(100) NOT NULL,
        responded_at VARCHAR(100) DEFAULT '',
        INDEX idx_admin_dismissal_target (target_user_id, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        timestamp VARCHAR(100) NOT NULL,
        action VARCHAR(100) NOT NULL,
        target VARCHAR(255),
        details TEXT,
        actor VARCHAR(255) DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


def get_connection() -> MySQLConnectionWrapper:
    """Abre conexão MySQL usando variáveis de ambiente (.env / Vercel)."""
    cfg = get_db_config()
    raw_conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )
    return MySQLConnectionWrapper(raw_conn)


@contextmanager
def db_session() -> Iterator[MySQLConnectionWrapper]:
    """Gerenciador de contexto transacional para operações no MySQL."""
    ensure_schema_initialized()
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DEFAULT_MAP_STATIONS = [
    # Doutorado (36,13 m2 -> D01..D11)
    ("D01", "Doutorado", "pesquisa", "person", "P011", "", ""),
    ("D02", "Doutorado", "pesquisa", "person", "P012", "", ""),
    ("D03", "Doutorado", "pesquisa", "vazio", "", "", ""),
    ("D04", "Doutorado", "pesquisa", "livre", "", "", ""),
    ("D05", "Doutorado", "pesquisa", "person", "P020", "Cleiton", ""),
    ("D06", "Doutorado", "pesquisa", "livre", "", "", ""),
    ("D07", "Doutorado", "pesquisa", "person", "P008", "", ""),
    ("D08", "Doutorado", "pesquisa", "person", "P005", "", ""),
    ("D09", "Doutorado", "pesquisa", "vazio", "", "", ""),
    ("D10", "Doutorado", "pesquisa", "person", "P016", "Matheus", ""),
    ("D11", "Doutorado", "pesquisa", "person", "P015", "", ""),
    # Mestrado (42,97 m2 -> M00..M14)
    ("M00", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M01", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M02", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M03", "Mestrado", "pesquisa", "person", "P028", "Junio R.", ""),
    ("M04", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M05", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M06", "Mestrado", "ic", "pc_ic", "", "", ""),
    ("M07", "Mestrado", "pesquisa", "person", "P023", "", ""),
    ("M08", "Mestrado", "pesquisa", "person", "P027", "Juan C.", ""),
    ("M09", "Mestrado", "pesquisa", "person", "P026", "Carlos", ""),
    ("M10", "Mestrado", "pesquisa", "person", "P025", "Juan M.", ""),
    ("M11", "Mestrado", "pesquisa", "person", "P029", "", ""),
    ("M12", "Mestrado", "pesquisa", "person", "P024", "", ""),
    ("M13", "Mestrado", "pesquisa", "person", "P031", "Garcia M.", ""),
    ("M14", "Mestrado", "pesquisa", "person", "P030", "Diego", ""),
    # Doc. P. e D. (29,48 m2 -> P01..P07)
    ("P01", "Doc. P. e D.", "pesquisa", "person", "P013", "Moisés", ""),
    ("P02", "Doc. P. e D.", "pesquisa", "person", "P006", "Daniel", ""),
    ("P03", "Doc. P. e D.", "pesquisa", "person", "P019", "Solon", ""),
    ("P04", "Doc. P. e D.", "pesquisa", "person", "P007", "Gabriella Santos", ""),
    ("P05", "Doc. P. e D.", "pesquisa", "person", "P017", "Talita", ""),
    ("P06", "Doc. P. e D.", "pesquisa", "person", "P009", "", ""),
    ("P07", "Doc. P. e D.", "pesquisa", "person", "P014", "", ""),
    # Sala de Equipamentos (Sala do Servidor)
    ("PC 083", "Sala do Servidor", "servidor", "servidor", "", "PC 083", "PC083"),
    ("PC Hardware", "Sala do Servidor", "servidor", "servidor", "", "PC Hardware", ""),
    ("PC 082", "Sala do Servidor", "servidor", "servidor", "", "PC 082", "PC082"),
    ("PC 094", "Sala do Servidor", "servidor", "servidor", "", "PC 094", "PC094"),
    ("PC 095", "Sala do Servidor", "servidor", "servidor", "", "PC 095", "PC095"),
]


def ensure_default_map_stations(conn: MySQLConnectionWrapper) -> None:
    """Garante todas as estações fixas do mapa sem sobrescrever ajustes manuais."""
    conn.executemany(
        """
        INSERT IGNORE INTO map_stations(
            station_code, room, station_kind, occupant_mode, id_pessoa, custom_label, pc_id_pc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        DEFAULT_MAP_STATIONS,
    )


DEFAULT_USAGE_RULES = """Regras internas do LSEE quanto ao uso dos computadores

1- O usuário do computador tem total liberdade para instalar softwares gratuitos.

2- Evite a instalação de software pagos, os quais não temos as licenças no laboratório. Favor verificar com o pessoal responsável pelos softwares sobre as licenças disponíveis.

3- O usuário pode trocar a senha padrão de acesso ao computador. Contudo, não altere ou exclua a conta de administrador.
Essa conta é necessária para que possamos limpar e reconfigurar o computador, uma vez que você não for mais utilizá-lo.

4- Evite formatar o computador. Caso necessário, informe a equipe de alocação de recursos para fazermos isso com os técnicos da USP.

Boa pesquisa!

Atenciosamente,
Equipe de alocação de recursos"""


def ensure_default_params(conn: MySQLConnectionWrapper) -> None:
    """Garante parâmetros do Cenário B, conteúdos padrão e padroniza localizações conhecidas."""
    conn.execute("DELETE FROM system_params WHERE `key` = 'bonus_ssd_hd'")
    defaults = [
        ("armaz_peso_hd", "Peso relativo do HD (Armazenamento)", "0.25", "armazenamento"),
        ("armaz_escala_log", "Fator de suavização log (GB_eq)", "60", "armazenamento"),
        ("score_maximo", "Score máximo componente", "10", "armazenamento"),
    ]
    for key, label, val, cat in defaults:
        conn.execute(
            "INSERT IGNORE INTO system_params(`key`, label, `value`, category) VALUES (?, ?, ?, ?)",
            (key, label, val, cat),
        )
    for tbl in ("computers", "monitors"):
        conn.execute(f"UPDATE {tbl} SET localizacao = 'Sala de Reuniões' WHERE localizacao = 'Sala de reunião'")
        conn.execute(
            f"UPDATE {tbl} SET localizacao = 'Sala do Servidor' WHERE localizacao = 'Sala do servidor e equipamentos'"
        )
    ensure_default_map_stations(conn)
    conn.execute(
        "INSERT IGNORE INTO site_contents(`key`, content, updated_at) VALUES (?, ?, '')",
        ("usage_rules", DEFAULT_USAGE_RULES),
    )


def ensure_schema_columns(conn: MySQLConnectionWrapper) -> None:
    """Adiciona colunas novas em tabelas já existentes sem perda de dados."""
    try:
        conn.execute("SELECT actor FROM audit_log LIMIT 1").fetchone()
    except Exception:
        conn.execute("ALTER TABLE audit_log ADD COLUMN actor VARCHAR(255) DEFAULT ''")
    try:
        conn.execute("SELECT request_type FROM admin_dismissal_requests LIMIT 1").fetchone()
    except Exception:
        conn.execute(
            "ALTER TABLE admin_dismissal_requests ADD COLUMN request_type VARCHAR(32) NOT NULL DEFAULT 'dismissal'"
        )


def init_schema() -> None:
    """Cria as tabelas no MySQL caso ainda não existam e garante parâmetros padrão."""
    global _SCHEMA_READY
    conn = get_connection()
    try:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
        ensure_schema_columns(conn)
        ensure_default_params(conn)
        conn.commit()
        _SCHEMA_READY = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema_initialized() -> None:
    """Garante que o schema foi inicializado uma vez por processo."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if not _SCHEMA_READY:
            init_schema()
