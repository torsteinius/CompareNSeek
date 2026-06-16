#!/usr/bin/env python3
"""
Streamlit MVP for CompareNSeek.

Run:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import io
import importlib.metadata as importlib_metadata
import importlib.util
import os
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from generic_database_search import (
    ColumnRef,
    DatabaseAdapter,
    GenericDatabaseSearch,
    OracleDatabaseAdapter,
    SQLiteDatabaseAdapter,
    SQLServerDatabaseAdapter,
    create_random_sqlite_database,
)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "compare_seek.sqlite"
GENERIC_TEST_DB_PATH = DATA_DIR / "generic_random_test.sqlite"
DATABASE_TYPES = ["SQLite", "Oracle", "SQL Server"]
CONNECTION_SYSTEMS = ["SQLite", "IFS", "DWH", "Lydia", "Custom"]
CONNECTION_ENVIRONMENTS = ["local", "test", "preprod", "prod", "custom"]

TARGET_STATUSES = [
    "unknown",
    "not_analyzed",
    "candidates_found",
    "needs_review",
    "confirmed",
    "no_candidates",
    "not_in_new_source",
    "deprecated",
]

MATCH_STATUSES = [
    "candidate",
    "preferred_candidate",
    "confirmed",
    "rejected",
    "needs_review",
    "weak_candidate",
    "manual_candidate",
]

STATUS_ICON = {
    "confirmed": "🟩",
    "candidates_found": "🟨",
    "preferred_candidate": "🟨",
    "needs_review": "🟦",
    "no_candidates": "🟥",
    "not_analyzed": "⬜",
    "unknown": "⬜",
    "not_in_new_source": "⬛",
    "deprecated": "⬛",
    "candidate": "🟨",
    "manual_candidate": "🟨",
    "weak_candidate": "🟧",
    "rejected": "⬛",
}

MANUAL_MATCH_STATUSES = {"confirmed", "rejected", "preferred_candidate", "needs_review", "manual_candidate"}


def load_key_value_file(path: Path, override: bool = False) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value


def bootstrap_local_environment() -> None:
    load_key_value_file(APP_DIR / ".env")
    load_key_value_file(APP_DIR / "setup.txt")


def read_text_lines(path: Path) -> list[str]:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin1"):
        try:
            return data.decode(encoding).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").splitlines()


def load_suspect_oracle_tables(owner: str = "IFSAPP") -> list[str]:
    paths = [
        APP_DIR / "ifs_knowledge" / "suspects.txt",
        APP_DIR / "suspects.txt",
    ]
    owner = owner.upper()
    tables: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        for raw_line in read_text_lines(path):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            line_owner: str | None = None
            table_name = line
            if "." in line:
                owner_part, table_part = line.split(".", 1)
                line_owner = owner_part.strip().strip('"[]').upper() or None
                table_name = table_part
            if line_owner is not None and line_owner != owner:
                continue
            table_name = table_name.strip().strip('"[]').upper()
            if table_name and table_name not in seen:
                seen.add(table_name)
                tables.append(table_name)
        if tables:
            break
    return tables


def ensure_divclasses_on_syspath() -> None:
    if any((Path(path) / "DivClasses").exists() for path in sys.path if path):
        return

    for parent in (APP_DIR, *APP_DIR.parents):
        try:
            siblings = [p for p in parent.iterdir() if p.is_dir()]
        except OSError:
            siblings = []
        candidates = [parent, *siblings]
        for candidate in candidates:
            if (candidate / "DivClasses").exists():
                candidate_text = str(candidate)
                if candidate_text not in sys.path:
                    sys.path.insert(0, candidate_text)
                return


def env_miljo_value(environment: Any) -> str:
    value = str(environment or "test").strip().upper()
    aliases = {
        "LOCAL": "LOCAL",
        "TEST": "TEST",
        "PREPROD": "PREPROD",
        "PROD": "PROD",
        "CUSTOM": "CUSTOM",
    }
    return aliases.get(value, value or "TEST")


def profile_secret_prefix(profile: dict[str, Any]) -> str:
    configured = str(profile.get("config_prefix") or "").strip()
    if configured:
        return configured if configured.endswith("_") else f"{configured}_"
    if profile.get("db_type") == "Oracle":
        return "ORACLE_"
    system_name = str(profile.get("system_name") or "").strip()
    return f"{system_name.upper()}_" if system_name else ""


def configure_profile_environment(profile: dict[str, Any]) -> None:
    prefix = profile_secret_prefix(profile)
    if not prefix:
        return
    miljo_key = f"{prefix}MILJO"
    os.environ[miljo_key] = env_miljo_value(profile.get("environment"))


def now_sql() -> str:
    return "CURRENT_TIMESTAMP"


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(col).strip().lower().replace(" ", "_") for col in out.columns]
    return out


def first_present(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = clean_value(row.get(name))
        if value not in (None, ""):
            return value
    return default


def to_float(value: Any) -> float | None:
    value = clean_value(value)
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    value = clean_value(value)
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def read_uploaded_csv(uploaded_file: Any) -> pd.DataFrame:
    data = uploaded_file.getvalue()
    encodings = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin1")
    last_error: Exception | None = None

    for encoding in encodings:
        for sep in (";", ",", "\t"):
            try:
                df = pd.read_csv(io.BytesIO(data), sep=sep, encoding=encoding)
            except (UnicodeDecodeError, UnicodeError, pd.errors.ParserError) as exc:
                last_error = exc
                continue
            if len(df.columns) > 1:
                return normalize_columns(df)

    for encoding in encodings:
        try:
            return normalize_columns(pd.read_csv(io.BytesIO(data), encoding=encoding))
        except (UnicodeDecodeError, UnicodeError, pd.errors.ParserError) as exc:
            last_error = exc

    raise ValueError(f"Could not read uploaded CSV/TXT file. Last error: {last_error}")


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA journal_mode = WAL;")
    return con


def init_db() -> None:
    con = get_connection()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS target_field (
            target_field_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_system TEXT NOT NULL DEFAULT 'DWH',
            target_schema TEXT,
            target_table TEXT NOT NULL,
            target_column TEXT NOT NULL,
            target_data_type TEXT,
            business_name TEXT,
            description TEXT,
            row_count INTEGER,
            null_count INTEGER,
            null_ratio REAL,
            distinct_count INTEGER,
            sample_values TEXT,
            overall_status TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            UNIQUE(target_system, target_schema, target_table, target_column)
        );

        CREATE TABLE IF NOT EXISTS source_field (
            source_field_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_system TEXT NOT NULL DEFAULT 'IFS',
            source_schema TEXT,
            source_table TEXT NOT NULL,
            source_column TEXT NOT NULL,
            source_data_type TEXT,
            row_count INTEGER,
            null_count INTEGER,
            null_ratio REAL,
            distinct_count INTEGER,
            sample_values TEXT,
            min_value TEXT,
            max_value TEXT,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            UNIQUE(source_system, source_schema, source_table, source_column)
        );

        CREATE TABLE IF NOT EXISTS field_match (
            field_match_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_field_id INTEGER NOT NULL,
            source_field_id INTEGER,
            total_score REAL,
            name_score REAL,
            type_score REAL,
            value_score REAL,
            pattern_score REAL,
            cardinality_score REAL,
            null_profile_score REAL,
            rank_no INTEGER,
            status TEXT NOT NULL DEFAULT 'candidate',
            confidence TEXT DEFAULT 'unknown',
            match_reason TEXT,
            comment TEXT,
            created_by TEXT DEFAULT 'system',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_by TEXT,
            reviewed_at TEXT,
            FOREIGN KEY (target_field_id) REFERENCES target_field(target_field_id),
            FOREIGN KEY (source_field_id) REFERENCES source_field(source_field_id),
            UNIQUE(target_field_id, source_field_id)
        );

        CREATE TABLE IF NOT EXISTS mapping_note (
            mapping_note_id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_field_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (target_field_id) REFERENCES target_field(target_field_id)
        );

        CREATE TABLE IF NOT EXISTS scan_run (
            scan_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            source_system TEXT,
            target_system TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS connection_profile (
            connection_profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            db_type TEXT NOT NULL,
            system_name TEXT NOT NULL DEFAULT 'Custom',
            environment TEXT NOT NULL DEFAULT 'custom',
            sqlite_path TEXT,
            config_prefix TEXT,
            default_schema TEXT,
            oracle_owner TEXT,
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        );
        """
    )
    con.commit()
    ensure_default_connection_profiles()


def query_df(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_connection(), params=params)


def scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur = get_connection().execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def ensure_default_connection_profiles() -> None:
    con = get_connection()
    default_profiles = [
        {
            "name": "Local SQLite testdata",
            "db_type": "SQLite",
            "system_name": "SQLite",
            "environment": "local",
            "sqlite_path": str(GENERIC_TEST_DB_PATH),
            "config_prefix": None,
            "default_schema": None,
            "oracle_owner": None,
            "notes": "Default lokal testdatabase for micromanagement-sok.",
            "is_active": 1,
        },
        {
            "name": "IFS Oracle",
            "db_type": "Oracle",
            "system_name": "IFS",
            "environment": "test",
            "sqlite_path": None,
            "config_prefix": "ORACLE_",
            "default_schema": "IFSAPP",
            "oracle_owner": "IFSAPP",
            "notes": "Bruker DivClasses.OracleBaseCls; secrets hentes av klassen/servermiljoet.",
            "is_active": 1,
        },
        {
            "name": "IFS Oracle preprod",
            "db_type": "Oracle",
            "system_name": "IFS",
            "environment": "preprod",
            "sqlite_path": None,
            "config_prefix": "ORACLE2_",
            "default_schema": "IFSAPP",
            "oracle_owner": "IFSAPP",
            "notes": "Preprod IFS via ORACLE2_. Ligger i secrets, men passord kan vaere uavklart.",
            "is_active": 0,
        },
        {
            "name": "DWH SQL Server",
            "db_type": "SQL Server",
            "system_name": "DWH",
            "environment": "test",
            "sqlite_path": None,
            "config_prefix": "PFTSQL_",
            "default_schema": "mart_m",
            "oracle_owner": None,
            "notes": "Bruker DivClasses.SQLServerBaseCls med prefix PFTSQL_.",
            "is_active": 1,
        },
        {
            "name": "Lydia SQL Server",
            "db_type": "SQL Server",
            "system_name": "Lydia",
            "environment": "prod",
            "sqlite_path": None,
            "config_prefix": "LYDIA_",
            "default_schema": None,
            "oracle_owner": None,
            "notes": "Bruker DivClasses.SQLServerBaseCls med Lydia-prefix fra serverens secrets.",
            "is_active": 1,
        },
    ]
    con.executemany(
        """
        INSERT OR IGNORE INTO connection_profile (
            name, db_type, system_name, environment, sqlite_path, config_prefix,
            default_schema, oracle_owner, notes, is_active
        )
        VALUES (
            :name, :db_type, :system_name, :environment, :sqlite_path, :config_prefix,
            :default_schema, :oracle_owner, :notes, :is_active
        )
        """,
        default_profiles,
    )
    con.executemany(
        """
        UPDATE connection_profile
        SET environment = ?,
            config_prefix = ?,
            is_active = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE name = ?
          AND system_name = ?
          AND notes = ?
        """,
        [
            (
                "test",
                "ORACLE_",
                1,
                "IFS Oracle",
                "IFS",
                "Bruker DivClasses.OracleBaseCls; secrets hentes av klassen/servermiljoet.",
            ),
            (
                "test",
                "PFTSQL_",
                1,
                "DWH SQL Server",
                "DWH",
                "Bruker DivClasses.SQLServerBaseCls med prefix PFTSQL_.",
            ),
            (
                "prod",
                "LYDIA_",
                1,
                "Lydia SQL Server",
                "Lydia",
                "Bruker DivClasses.SQLServerBaseCls med Lydia-prefix fra serverens secrets.",
            ),
        ],
    )
    con.execute(
        """
        UPDATE connection_profile
        SET default_schema = COALESCE(NULLIF(default_schema, ''), 'IFSAPP'),
            oracle_owner = COALESCE(NULLIF(oracle_owner, ''), 'IFSAPP'),
            updated_at = CURRENT_TIMESTAMP
        WHERE db_type = 'Oracle'
          AND system_name = 'IFS'
        """
    )
    con.commit()


def get_connection_profiles(active_only: bool = True, db_type: str | None = None) -> pd.DataFrame:
    where = []
    params: list[Any] = []
    if active_only:
        where.append("is_active = 1")
    if db_type:
        where.append("db_type = ?")
        params.append(db_type)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return query_df(
        f"""
        SELECT *
        FROM connection_profile
        {where_sql}
        ORDER BY
            CASE environment
                WHEN 'local' THEN 1
                WHEN 'test' THEN 2
                WHEN 'preprod' THEN 3
                WHEN 'prod' THEN 4
                ELSE 9
            END,
            system_name,
            name
        """,
        tuple(params),
    )


def upsert_connection_profile(values: dict[str, Any]) -> None:
    con = get_connection()
    con.execute(
        """
        INSERT INTO connection_profile (
            name, db_type, system_name, environment, sqlite_path, config_prefix,
            default_schema, oracle_owner, notes, is_active
        )
        VALUES (
            :name, :db_type, :system_name, :environment, :sqlite_path, :config_prefix,
            :default_schema, :oracle_owner, :notes, :is_active
        )
        ON CONFLICT(name) DO UPDATE SET
            db_type = excluded.db_type,
            system_name = excluded.system_name,
            environment = excluded.environment,
            sqlite_path = excluded.sqlite_path,
            config_prefix = excluded.config_prefix,
            default_schema = excluded.default_schema,
            oracle_owner = excluded.oracle_owner,
            notes = excluded.notes,
            is_active = excluded.is_active,
            updated_at = CURRENT_TIMESTAMP
        """,
        values,
    )
    con.commit()


def set_connection_profile_active(connection_profile_id: int, is_active: bool) -> None:
    get_connection().execute(
        """
        UPDATE connection_profile
        SET is_active = ?, updated_at = CURRENT_TIMESTAMP
        WHERE connection_profile_id = ?
        """,
        (1 if is_active else 0, connection_profile_id),
    )
    get_connection().commit()


def connection_profile_label(row: pd.Series | dict[str, Any]) -> str:
    name = row["name"]
    db_type = row["db_type"]
    system_name = row.get("system_name") if isinstance(row, dict) else row["system_name"]
    environment = row.get("environment") if isinstance(row, dict) else row["environment"]
    return f"{name} ({system_name}/{environment}/{db_type})"


def upsert_target_field(row: dict[str, Any]) -> int:
    con = get_connection()
    values = {
        "target_system": first_present(row, "target_system", default="DWH"),
        "target_schema": first_present(row, "target_schema", "dwh_schema", "schema_name"),
        "target_table": first_present(row, "target_table", "dwh_table", "table_name"),
        "target_column": first_present(row, "target_column", "dwh_column", "column_name"),
        "target_data_type": first_present(row, "target_data_type", "dwh_data_type", "data_type"),
        "business_name": first_present(row, "business_name"),
        "description": first_present(row, "description"),
        "row_count": to_int(first_present(row, "row_count")),
        "null_count": to_int(first_present(row, "null_count")),
        "null_ratio": to_float(first_present(row, "null_ratio")),
        "distinct_count": to_int(first_present(row, "distinct_count", "num_distinct")),
        "sample_values": first_present(row, "sample_values", "sample_values_json", "sample_value"),
        "overall_status": first_present(row, "overall_status", "status", default="unknown"),
    }
    if not values["target_table"] or not values["target_column"]:
        raise ValueError("Target import requires target_table and target_column.")
    if values["overall_status"] not in TARGET_STATUSES:
        values["overall_status"] = "unknown"

    existing_id = scalar(
        """
        SELECT target_field_id
        FROM target_field
        WHERE target_system = ?
          AND COALESCE(target_schema, '') = COALESCE(?, '')
          AND target_table = ?
          AND target_column = ?
        """,
        (values["target_system"], values["target_schema"], values["target_table"], values["target_column"]),
    )
    if existing_id:
        con.execute(
            """
            UPDATE target_field
            SET target_data_type = COALESCE(:target_data_type, target_data_type),
                business_name = COALESCE(:business_name, business_name),
                description = COALESCE(:description, description),
                row_count = COALESCE(:row_count, row_count),
                null_count = COALESCE(:null_count, null_count),
                null_ratio = COALESCE(:null_ratio, null_ratio),
                distinct_count = COALESCE(:distinct_count, distinct_count),
                sample_values = COALESCE(:sample_values, sample_values),
                updated_at = CURRENT_TIMESTAMP
            WHERE target_field_id = :target_field_id
            """,
            {**values, "target_field_id": existing_id},
        )
        con.commit()
        return int(existing_id)

    cur = con.execute(
        """
        INSERT INTO target_field (
            target_system, target_schema, target_table, target_column, target_data_type,
            business_name, description, row_count, null_count, null_ratio, distinct_count,
            sample_values, overall_status
        )
        VALUES (
            :target_system, :target_schema, :target_table, :target_column, :target_data_type,
            :business_name, :description, :row_count, :null_count, :null_ratio, :distinct_count,
            :sample_values, :overall_status
        )
        """,
        values,
    )
    con.commit()
    return int(cur.lastrowid)


def upsert_source_field(row: dict[str, Any]) -> int:
    con = get_connection()
    values = {
        "source_system": first_present(row, "source_system", default="IFS"),
        "source_schema": first_present(row, "source_schema", "oracle_owner", "owner"),
        "source_table": first_present(row, "source_table", "oracle_table", "table_name"),
        "source_column": first_present(row, "source_column", "oracle_column", "column_name"),
        "source_data_type": first_present(row, "source_data_type", "oracle_data_type", "data_type"),
        "row_count": to_int(first_present(row, "row_count", "counted_rows", "num_rows_est")),
        "null_count": to_int(first_present(row, "null_count")),
        "null_ratio": to_float(first_present(row, "null_ratio")),
        "distinct_count": to_int(first_present(row, "distinct_count", "num_distinct")),
        "sample_values": first_present(row, "sample_values", "sample_value"),
        "min_value": first_present(row, "min_value"),
        "max_value": first_present(row, "max_value"),
    }
    if not values["source_table"] or not values["source_column"]:
        raise ValueError("Source import requires source_table and source_column.")

    existing_id = scalar(
        """
        SELECT source_field_id
        FROM source_field
        WHERE source_system = ?
          AND COALESCE(source_schema, '') = COALESCE(?, '')
          AND source_table = ?
          AND source_column = ?
        """,
        (values["source_system"], values["source_schema"], values["source_table"], values["source_column"]),
    )
    if existing_id:
        con.execute(
            """
            UPDATE source_field
            SET source_data_type = COALESCE(:source_data_type, source_data_type),
                row_count = COALESCE(:row_count, row_count),
                null_count = COALESCE(:null_count, null_count),
                null_ratio = COALESCE(:null_ratio, null_ratio),
                distinct_count = COALESCE(:distinct_count, distinct_count),
                sample_values = COALESCE(:sample_values, sample_values),
                min_value = COALESCE(:min_value, min_value),
                max_value = COALESCE(:max_value, max_value),
                updated_at = CURRENT_TIMESTAMP
            WHERE source_field_id = :source_field_id
            """,
            {**values, "source_field_id": existing_id},
        )
        con.commit()
        return int(existing_id)

    cur = con.execute(
        """
        INSERT INTO source_field (
            source_system, source_schema, source_table, source_column, source_data_type,
            row_count, null_count, null_ratio, distinct_count, sample_values, min_value, max_value
        )
        VALUES (
            :source_system, :source_schema, :source_table, :source_column, :source_data_type,
            :row_count, :null_count, :null_ratio, :distinct_count, :sample_values, :min_value, :max_value
        )
        """,
        values,
    )
    con.commit()
    return int(cur.lastrowid)


def upsert_field_match(row: dict[str, Any]) -> int:
    con = get_connection()
    values = {
        "target_field_id": row["target_field_id"],
        "source_field_id": row.get("source_field_id"),
        "total_score": to_float(first_present(row, "total_score")),
        "name_score": to_float(first_present(row, "name_score")),
        "type_score": to_float(first_present(row, "type_score", "datatype_score")),
        "value_score": to_float(first_present(row, "value_score", "value_overlap_score")),
        "pattern_score": to_float(first_present(row, "pattern_score")),
        "cardinality_score": to_float(first_present(row, "cardinality_score")),
        "null_profile_score": to_float(first_present(row, "null_profile_score")),
        "rank_no": to_int(first_present(row, "rank_no", "rn")),
        "status": first_present(row, "match_status", "field_match_status", "status", default="candidate"),
        "confidence": first_present(row, "confidence", "mapping_status", default="unknown"),
        "match_reason": first_present(row, "match_reason", "reason"),
        "comment": first_present(row, "comment"),
        "created_by": first_present(row, "created_by", default="system"),
    }
    if values["status"] not in MATCH_STATUSES:
        values["status"] = "candidate"

    con.execute(
        """
        INSERT INTO field_match (
            target_field_id, source_field_id, total_score, name_score, type_score, value_score,
            pattern_score, cardinality_score, null_profile_score, rank_no, status, confidence,
            match_reason, comment, created_by
        )
        VALUES (
            :target_field_id, :source_field_id, :total_score, :name_score, :type_score, :value_score,
            :pattern_score, :cardinality_score, :null_profile_score, :rank_no, :status, :confidence,
            :match_reason, :comment, :created_by
        )
        ON CONFLICT(target_field_id, source_field_id) DO UPDATE SET
            total_score = COALESCE(excluded.total_score, field_match.total_score),
            name_score = COALESCE(excluded.name_score, field_match.name_score),
            type_score = COALESCE(excluded.type_score, field_match.type_score),
            value_score = COALESCE(excluded.value_score, field_match.value_score),
            pattern_score = COALESCE(excluded.pattern_score, field_match.pattern_score),
            cardinality_score = COALESCE(excluded.cardinality_score, field_match.cardinality_score),
            null_profile_score = COALESCE(excluded.null_profile_score, field_match.null_profile_score),
            rank_no = COALESCE(excluded.rank_no, field_match.rank_no),
            confidence = COALESCE(excluded.confidence, field_match.confidence),
            match_reason = COALESCE(excluded.match_reason, field_match.match_reason),
            comment = COALESCE(field_match.comment, excluded.comment),
            status = CASE
                WHEN field_match.status IN ('confirmed', 'rejected', 'preferred_candidate', 'needs_review', 'manual_candidate')
                    THEN field_match.status
                ELSE excluded.status
            END
        """,
        values,
    )
    con.execute(
        """
        UPDATE target_field
        SET overall_status = 'candidates_found', updated_at = CURRENT_TIMESTAMP
        WHERE target_field_id = ?
          AND overall_status IN ('unknown', 'not_analyzed')
        """,
        (values["target_field_id"],),
    )
    con.commit()
    return int(scalar(
        """
        SELECT field_match_id FROM field_match
        WHERE target_field_id = ? AND source_field_id IS ?
        """,
        (values["target_field_id"], values["source_field_id"]),
    ))


def import_target_fields_from_df(df: pd.DataFrame) -> int:
    count = 0
    for row in normalize_columns(df).to_dict("records"):
        if first_present(row, "target_table", "dwh_table", "table_name") and first_present(row, "target_column", "dwh_column", "column_name"):
            upsert_target_field(row)
            count += 1
    return count


def import_source_fields_from_df(df: pd.DataFrame) -> int:
    count = 0
    for row in normalize_columns(df).to_dict("records"):
        if first_present(row, "source_table", "oracle_table", "table_name") and first_present(row, "source_column", "oracle_column", "column_name"):
            upsert_source_field(row)
            count += 1
    return count


def import_matches_from_df(df: pd.DataFrame) -> int:
    count = 0
    for row in normalize_columns(df).to_dict("records"):
        if first_present(row, "mapping_status") == "no_candidate" and not first_present(row, "oracle_table", "source_table"):
            target_id = upsert_target_field(row)
            get_connection().execute(
                """
                UPDATE target_field
                SET overall_status = CASE
                    WHEN overall_status IN ('confirmed', 'not_in_new_source', 'deprecated') THEN overall_status
                    ELSE 'no_candidates'
                END,
                updated_at = CURRENT_TIMESTAMP
                WHERE target_field_id = ?
                """,
                (target_id,),
            )
            get_connection().commit()
            count += 1
            continue

        if not first_present(row, "target_table", "dwh_table") or not first_present(row, "target_column", "dwh_column"):
            continue
        if not first_present(row, "source_table", "oracle_table") or not first_present(row, "source_column", "oracle_column"):
            continue

        target_id = upsert_target_field(row)
        source_id = upsert_source_field(row)
        row["target_field_id"] = target_id
        row["source_field_id"] = source_id
        upsert_field_match(row)
        count += 1
    return count


def get_dashboard_summary() -> pd.DataFrame:
    return query_df(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN overall_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
            SUM(CASE WHEN overall_status = 'candidates_found' THEN 1 ELSE 0 END) AS candidates_found,
            SUM(CASE WHEN overall_status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
            SUM(CASE WHEN overall_status IN ('no_candidates', 'not_in_new_source') THEN 1 ELSE 0 END) AS gaps,
            SUM(CASE WHEN overall_status IN ('unknown', 'not_analyzed') THEN 1 ELSE 0 END) AS unknown
        FROM target_field
        """
    )


def get_table_summary() -> pd.DataFrame:
    df = query_df(
        """
        SELECT
            target_table AS dwh_table,
            COUNT(*) AS total_fields,
            SUM(CASE WHEN overall_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
            SUM(CASE WHEN overall_status = 'candidates_found' THEN 1 ELSE 0 END) AS candidates_found,
            SUM(CASE WHEN overall_status = 'needs_review' THEN 1 ELSE 0 END) AS review,
            SUM(CASE WHEN overall_status IN ('no_candidates', 'not_in_new_source') THEN 1 ELSE 0 END) AS gaps,
            SUM(CASE WHEN overall_status IN ('unknown', 'not_analyzed') THEN 1 ELSE 0 END) AS unknown,
            ROUND(100.0 * SUM(CASE WHEN overall_status = 'confirmed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS coverage_pct
        FROM target_field
        GROUP BY target_table
        ORDER BY gaps DESC, unknown DESC, coverage_pct ASC
        """
    )
    if df.empty:
        return df
    df["indicator"] = df["coverage_pct"].apply(lambda pct: "🟩" if pct >= 80 else "🟨" if pct >= 50 else "🟥")
    return df


def get_fields_for_table(target_table: str, status_filter: str | None = None) -> pd.DataFrame:
    params: list[Any] = [target_table]
    status_sql = ""
    if status_filter == "Kun hull":
        status_sql = "AND tf.overall_status IN ('no_candidates', 'not_in_new_source')"
    elif status_filter == "Kun kandidater":
        status_sql = "AND tf.overall_status = 'candidates_found'"
    elif status_filter == "Kun bekreftet":
        status_sql = "AND tf.overall_status = 'confirmed'"
    elif status_filter == "Kun trenger vurdering":
        status_sql = "AND tf.overall_status = 'needs_review'"
    elif status_filter == "Kun ukjent":
        status_sql = "AND tf.overall_status IN ('unknown', 'not_analyzed')"

    df = query_df(
        f"""
        SELECT
            tf.target_field_id,
            tf.overall_status,
            tf.target_column AS field,
            tf.target_data_type AS data_type,
            sf.source_system,
            sf.source_schema,
            sf.source_table,
            sf.source_column,
            fm.total_score,
            fm.status AS match_status,
            fm.comment
        FROM target_field tf
        LEFT JOIN field_match fm
            ON fm.field_match_id = (
                SELECT fm2.field_match_id
                FROM field_match fm2
                WHERE fm2.target_field_id = tf.target_field_id
                  AND fm2.status != 'rejected'
                ORDER BY
                    CASE fm2.status
                        WHEN 'confirmed' THEN 1
                        WHEN 'preferred_candidate' THEN 2
                        WHEN 'candidate' THEN 3
                        WHEN 'manual_candidate' THEN 4
                        ELSE 9
                    END,
                    fm2.total_score DESC
                LIMIT 1
            )
        LEFT JOIN source_field sf ON sf.source_field_id = fm.source_field_id
        WHERE tf.target_table = ?
        {status_sql}
        ORDER BY
            CASE tf.overall_status
                WHEN 'no_candidates' THEN 1
                WHEN 'needs_review' THEN 2
                WHEN 'candidates_found' THEN 3
                WHEN 'unknown' THEN 4
                WHEN 'not_analyzed' THEN 5
                WHEN 'confirmed' THEN 6
                ELSE 9
            END,
            tf.target_column
        """,
        tuple(params),
    )
    if not df.empty:
        df["status"] = df["overall_status"].map(STATUS_ICON).fillna("") + " " + df["overall_status"]
        df["best_candidate"] = df.apply(format_source_row, axis=1)
    return df


def get_target_field(target_field_id: int) -> dict[str, Any] | None:
    row = get_connection().execute(
        "SELECT * FROM target_field WHERE target_field_id = ?",
        (target_field_id,),
    ).fetchone()
    return dict(row) if row else None


def get_matches_for_field(target_field_id: int) -> pd.DataFrame:
    df = query_df(
        """
        SELECT
            fm.*,
            sf.source_system,
            sf.source_schema,
            sf.source_table,
            sf.source_column,
            sf.source_data_type,
            sf.row_count,
            sf.null_ratio,
            sf.distinct_count,
            sf.sample_values
        FROM field_match fm
        LEFT JOIN source_field sf ON sf.source_field_id = fm.source_field_id
        WHERE fm.target_field_id = ?
        ORDER BY
            CASE fm.status
                WHEN 'confirmed' THEN 1
                WHEN 'preferred_candidate' THEN 2
                WHEN 'candidate' THEN 3
                WHEN 'manual_candidate' THEN 4
                WHEN 'needs_review' THEN 5
                WHEN 'weak_candidate' THEN 6
                WHEN 'rejected' THEN 9
                ELSE 8
            END,
            fm.total_score DESC
        """,
        (target_field_id,),
    )
    if not df.empty:
        df["source_field"] = df.apply(format_source_row, axis=1)
        df["status_label"] = df["status"].map(STATUS_ICON).fillna("") + " " + df["status"]
    return df


def add_mapping_note(target_field_id: int, note: str, created_by: str | None = None) -> None:
    note = (note or "").strip()
    if not note:
        return
    con = get_connection()
    con.execute(
        "INSERT INTO mapping_note (target_field_id, note, created_by) VALUES (?, ?, ?)",
        (target_field_id, note, created_by),
    )
    con.commit()


def get_notes(target_field_id: int) -> pd.DataFrame:
    return query_df(
        """
        SELECT created_at, created_by, note
        FROM mapping_note
        WHERE target_field_id = ?
        ORDER BY created_at DESC
        """,
        (target_field_id,),
    )


def confirm_match(field_match_id: int, reviewed_by: str | None = None, comment: str | None = None) -> None:
    con = get_connection()
    row = con.execute("SELECT target_field_id FROM field_match WHERE field_match_id = ?", (field_match_id,)).fetchone()
    if not row:
        return
    con.execute(
        """
        UPDATE field_match
        SET status = 'confirmed',
            comment = COALESCE(?, comment),
            reviewed_by = ?,
            reviewed_at = CURRENT_TIMESTAMP
        WHERE field_match_id = ?
        """,
        (comment or None, reviewed_by or None, field_match_id),
    )
    con.execute(
        """
        UPDATE target_field
        SET overall_status = 'confirmed', updated_at = CURRENT_TIMESTAMP
        WHERE target_field_id = ?
        """,
        (row["target_field_id"],),
    )
    con.commit()


def update_match_status(field_match_id: int, status: str, comment: str | None = None, reviewed_by: str | None = None) -> None:
    if status not in MATCH_STATUSES:
        return
    con = get_connection()
    con.execute(
        """
        UPDATE field_match
        SET status = ?,
            comment = COALESCE(?, comment),
            reviewed_by = COALESCE(?, reviewed_by),
            reviewed_at = CURRENT_TIMESTAMP
        WHERE field_match_id = ?
        """,
        (status, comment or None, reviewed_by or None, field_match_id),
    )
    row = con.execute("SELECT target_field_id FROM field_match WHERE field_match_id = ?", (field_match_id,)).fetchone()
    if row and status == "preferred_candidate":
        con.execute(
            "UPDATE target_field SET overall_status = 'candidates_found', updated_at = CURRENT_TIMESTAMP WHERE target_field_id = ? AND overall_status != 'confirmed'",
            (row["target_field_id"],),
        )
    elif row and status == "needs_review":
        con.execute(
            "UPDATE target_field SET overall_status = 'needs_review', updated_at = CURRENT_TIMESTAMP WHERE target_field_id = ? AND overall_status != 'confirmed'",
            (row["target_field_id"],),
        )
    con.commit()


def update_target_status(target_field_id: int, status: str, comment: str | None = None, created_by: str | None = None) -> None:
    if status not in TARGET_STATUSES:
        return
    con = get_connection()
    con.execute(
        "UPDATE target_field SET overall_status = ?, updated_at = CURRENT_TIMESTAMP WHERE target_field_id = ?",
        (status, target_field_id),
    )
    con.commit()
    if comment:
        add_mapping_note(target_field_id, comment, created_by)


def search_source_fields(query: str = "") -> pd.DataFrame:
    query = (query or "").strip()
    if query:
        like = f"%{query.lower()}%"
        params = (like, like, like, like)
        where = """
        WHERE lower(COALESCE(source_system, '')) LIKE ?
           OR lower(COALESCE(source_schema, '')) LIKE ?
           OR lower(source_table) LIKE ?
           OR lower(source_column) LIKE ?
        """
    else:
        params = ()
        where = ""
    df = query_df(
        f"""
        SELECT *
        FROM source_field
        {where}
        ORDER BY source_schema, source_table, source_column
        LIMIT 500
        """,
        params,
    )
    if not df.empty:
        df["source_field"] = df.apply(format_source_row, axis=1)
    return df


def add_manual_candidate(target_field_id: int, source_field_id: int, comment: str | None = None) -> None:
    upsert_field_match(
        {
            "target_field_id": target_field_id,
            "source_field_id": source_field_id,
            "status": "manual_candidate",
            "confidence": "manual",
            "comment": comment,
            "created_by": "user",
        }
    )


def export_mapping(mode: str) -> pd.DataFrame:
    where = ""
    if mode == "Kun confirmed":
        where = "WHERE fm.status = 'confirmed'"
    elif mode == "Confirmed + preferred_candidate":
        where = "WHERE fm.status IN ('confirmed', 'preferred_candidate')"
    elif mode == "Alle kandidater":
        where = "WHERE fm.field_match_id IS NOT NULL"
    elif mode == "Alle hull":
        where = "WHERE tf.overall_status IN ('no_candidates', 'not_in_new_source', 'unknown', 'not_analyzed', 'needs_review')"

    return query_df(
        f"""
        SELECT
            tf.target_system,
            tf.target_schema,
            tf.target_table,
            tf.target_column,
            tf.target_data_type,
            sf.source_system,
            sf.source_schema,
            sf.source_table,
            sf.source_column,
            sf.source_data_type,
            COALESCE(fm.status, tf.overall_status) AS status,
            fm.confidence,
            fm.total_score,
            COALESCE(fm.comment, latest_note.note) AS comment,
            fm.reviewed_by,
            fm.reviewed_at
        FROM target_field tf
        LEFT JOIN field_match fm
            ON fm.field_match_id = (
                SELECT fm2.field_match_id
                FROM field_match fm2
                WHERE fm2.target_field_id = tf.target_field_id
                ORDER BY
                    CASE fm2.status
                        WHEN 'confirmed' THEN 1
                        WHEN 'preferred_candidate' THEN 2
                        WHEN 'candidate' THEN 3
                        WHEN 'manual_candidate' THEN 4
                        ELSE 9
                    END,
                    fm2.total_score DESC
                LIMIT 1
            )
        LEFT JOIN source_field sf ON sf.source_field_id = fm.source_field_id
        LEFT JOIN (
            SELECT mn.target_field_id, mn.note
            FROM mapping_note mn
            JOIN (
                SELECT target_field_id, MAX(created_at) AS created_at
                FROM mapping_note
                GROUP BY target_field_id
            ) latest
              ON latest.target_field_id = mn.target_field_id
             AND latest.created_at = mn.created_at
        ) latest_note ON latest_note.target_field_id = tf.target_field_id
        {where}
        ORDER BY tf.target_table, tf.target_column
        """
    )


def format_source_row(row: pd.Series | dict[str, Any]) -> str:
    system = clean_value(row.get("source_system")) or "IFS"
    schema = clean_value(row.get("source_schema"))
    table = clean_value(row.get("source_table"))
    column = clean_value(row.get("source_column"))
    parts = [system]
    if schema:
        parts.append(str(schema))
    if table:
        parts.append(str(table))
    if column:
        parts.append(str(column))
    return ".".join(parts) if len(parts) > 1 else "-"


def format_target_field(row: dict[str, Any]) -> str:
    parts = [row.get("target_system") or "DWH"]
    if row.get("target_schema"):
        parts.append(row["target_schema"])
    parts.extend([row["target_table"], row["target_column"]])
    return ".".join(parts)


def percent(value: Any) -> str:
    value = to_float(value)
    if value is None:
        return "-"
    if value <= 1:
        value *= 100
    return f"{value:.1f} %"


def rerun() -> None:
    st.rerun()


def page_dashboard() -> None:
    st.title("CompareNSeek")
    summary = get_dashboard_summary()
    row = summary.iloc[0].fillna(0).to_dict() if not summary.empty else {}
    cols = st.columns(6)
    cols[0].metric("Totalt", int(row.get("total", 0)))
    cols[1].metric("🟩 Bekreftet", int(row.get("confirmed", 0)))
    cols[2].metric("🟨 Kandidater", int(row.get("candidates_found", 0)))
    cols[3].metric("🟦 Fagavklaring", int(row.get("needs_review", 0)))
    cols[4].metric("🟥 Hull", int(row.get("gaps", 0)))
    cols[5].metric("⬜ Ukjent", int(row.get("unknown", 0)))

    st.subheader("Status per DWH-tabell")
    table_df = get_table_summary()
    if table_df.empty:
        st.info("Ingen target fields er importert ennå.")
        return
    st.dataframe(table_df, use_container_width=True, hide_index=True)


def page_import() -> None:
    st.title("Import")
    st.caption(f"SQLite: {DB_PATH}")

    target_upload = st.file_uploader("Target fields CSV", type=["csv", "txt"], key="target_csv")
    if target_upload and st.button("Importer target fields"):
        df = read_uploaded_csv(target_upload)
        count = import_target_fields_from_df(df)
        st.success(f"Importerte/oppdaterte {count} target fields.")

    source_upload = st.file_uploader("Source fields CSV", type=["csv", "txt"], key="source_csv")
    if source_upload and st.button("Importer source fields"):
        df = read_uploaded_csv(source_upload)
        count = import_source_fields_from_df(df)
        st.success(f"Importerte/oppdaterte {count} source fields.")

    match_upload = st.file_uploader("Field matches CSV", type=["csv", "txt"], key="match_csv")
    if match_upload and st.button("Importer field matches"):
        df = read_uploaded_csv(match_upload)
        count = import_matches_from_df(df)
        st.success(f"Importerte/oppdaterte {count} match-rader.")

    st.subheader("CompareAndSeek-rapporter")
    st.write("Typiske filer: `03_dwh_columns.csv`, `02_oracle_columns.csv`, `07_best_mapping_per_dwh_column.csv` eller `09_dwh_column_mapping_summary.csv`.")


def page_table_explorer() -> None:
    st.title("Table Explorer")
    tables = query_df("SELECT DISTINCT target_table FROM target_field ORDER BY target_table")
    if tables.empty:
        st.info("Importer target fields først.")
        return

    selected = st.selectbox("DWH-tabell", tables["target_table"].tolist())
    status_filter = st.radio(
        "Filter",
        ["Alle", "Kun hull", "Kun kandidater", "Kun bekreftet", "Kun trenger vurdering", "Kun ukjent"],
        horizontal=True,
    )
    df = get_fields_for_table(selected, status_filter)
    if df.empty:
        st.info("Ingen felter for valgt filter.")
        return

    show = df[["status", "field", "data_type", "best_candidate", "total_score", "match_status", "comment"]]
    st.dataframe(show, use_container_width=True, hide_index=True)

    labels = {f"{r.field} ({r.overall_status})": int(r.target_field_id) for r in df.itertuples()}
    field_label = st.selectbox("Velg felt", list(labels.keys()))
    if st.button("Åpne Field Detail"):
        st.session_state["selected_target_field_id"] = labels[field_label]
        st.session_state["page"] = "Field Detail"
        rerun()


def page_field_detail() -> None:
    st.title("Field Detail")
    fields = query_df(
        """
        SELECT target_field_id, target_table || '.' || target_column || ' (' || overall_status || ')' AS label
        FROM target_field
        ORDER BY target_table, target_column
        """
    )
    if fields.empty:
        st.info("Importer target fields først.")
        return

    selected_id = st.session_state.get("selected_target_field_id")
    ids = fields["target_field_id"].astype(int).tolist()
    index = ids.index(selected_id) if selected_id in ids else 0
    label = st.selectbox("Target field", fields["label"].tolist(), index=index)
    target_id = int(fields.loc[fields["label"] == label, "target_field_id"].iloc[0])
    st.session_state["selected_target_field_id"] = target_id
    target = get_target_field(target_id)
    if not target:
        st.warning("Fant ikke valgt target field.")
        return

    st.subheader(format_target_field(target))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", f"{STATUS_ICON.get(target['overall_status'], '')} {target['overall_status']}")
    c2.metric("Datatype", target.get("target_data_type") or "-")
    c3.metric("Nullandel", percent(target.get("null_ratio")))
    c4.metric("Unike verdier", target.get("distinct_count") or "-")
    if target.get("sample_values"):
        st.text_area("Eksempelverdier", str(target["sample_values"]), height=90, disabled=True)

    reviewed_by = st.text_input("Reviewed by", value=st.session_state.get("reviewed_by", ""))
    st.session_state["reviewed_by"] = reviewed_by
    action_comment = st.text_area("Kommentar for neste handling", height=80)

    matches = get_matches_for_field(target_id)
    st.subheader("Kandidater")
    if matches.empty:
        st.info("Ingen kandidater registrert for dette feltet.")
    else:
        show_cols = [
            "field_match_id",
            "rank_no",
            "source_field",
            "total_score",
            "status_label",
            "confidence",
            "match_reason",
        ]
        st.dataframe(matches[show_cols], use_container_width=True, hide_index=True)

        options = {f"{r.source_field} | {r.status} | {r.total_score}": int(r.field_match_id) for r in matches.itertuples()}
        selected_match_label = st.selectbox("Velg kandidat", list(options.keys()))
        match_id = options[selected_match_label]
        selected_match = matches.loc[matches["field_match_id"] == match_id].iloc[0]

        score_cols = st.columns(6)
        score_cols[0].metric("Name", selected_match.get("name_score") or "-")
        score_cols[1].metric("Type", selected_match.get("type_score") or "-")
        score_cols[2].metric("Value", selected_match.get("value_score") or "-")
        score_cols[3].metric("Pattern", selected_match.get("pattern_score") or "-")
        score_cols[4].metric("Cardinality", selected_match.get("cardinality_score") or "-")
        score_cols[5].metric("Null profile", selected_match.get("null_profile_score") or "-")

        b1, b2, b3, b4 = st.columns(4)
        if b1.button("Bekreft kandidat"):
            confirm_match(match_id, reviewed_by, action_comment)
            st.success("Kandidat bekreftet.")
            rerun()
        if b2.button("Marker som beste"):
            update_match_status(match_id, "preferred_candidate", action_comment, reviewed_by)
            st.success("Kandidat markert som beste kandidat.")
            rerun()
        if b3.button("Avvis kandidat"):
            update_match_status(match_id, "rejected", action_comment, reviewed_by)
            st.success("Kandidat avvist.")
            rerun()
        if b4.button("Send til fag"):
            update_match_status(match_id, "needs_review", action_comment, reviewed_by)
            st.success("Sendt til fagavklaring.")
            rerun()

    st.subheader("Target-status")
    s1, s2, s3 = st.columns(3)
    if s1.button("Marker som hull"):
        update_target_status(target_id, "no_candidates", action_comment, reviewed_by)
        rerun()
    if s2.button("Finnes ikke i ny kilde"):
        update_target_status(target_id, "not_in_new_source", action_comment, reviewed_by)
        rerun()
    if s3.button("Utgått/deprecated"):
        update_target_status(target_id, "deprecated", action_comment, reviewed_by)
        rerun()

    with st.expander("Legg til manuell kandidat"):
        sources = search_source_fields(st.text_input("Søk source field", key="manual_source_search"))
        if sources.empty:
            st.caption("Ingen source fields funnet.")
        else:
            source_options = {f"{r.source_field} ({r.source_data_type or '-'})": int(r.source_field_id) for r in sources.itertuples()}
            source_label = st.selectbox("Source field", list(source_options.keys()))
            manual_comment = st.text_input("Kommentar", key="manual_comment")
            if st.button("Legg til manuell kandidat"):
                add_manual_candidate(target_id, source_options[source_label], manual_comment)
                st.success("Manuell kandidat lagt til.")
                rerun()

    notes = get_notes(target_id)
    if not notes.empty:
        st.subheader("Notater")
        st.dataframe(notes, use_container_width=True, hide_index=True)


def page_gap_list() -> None:
    st.title("Gap List")
    tables = query_df("SELECT DISTINCT target_table FROM target_field ORDER BY target_table")
    table_filter = st.selectbox("Tabell", ["Alle"] + tables["target_table"].tolist() if not tables.empty else ["Alle"])
    statuses = st.multiselect(
        "Status",
        ["no_candidates", "unknown", "not_analyzed", "needs_review"],
        default=["no_candidates", "unknown", "not_analyzed", "needs_review"],
    )
    sort_by = st.selectbox("Sorter", ["Laveste score", "Flest nuller", "Tabell/felt"])
    if not statuses:
        st.info("Velg minst én status.")
        return

    params: list[Any] = statuses.copy()
    table_sql = ""
    if table_filter != "Alle":
        table_sql = "AND tf.target_table = ?"
        params.append(table_filter)
    order_sql = {
        "Laveste score": "best_score ASC NULLS FIRST",
        "Flest nuller": "tf.null_ratio DESC NULLS LAST",
        "Tabell/felt": "tf.target_table, tf.target_column",
    }[sort_by]
    placeholders = ",".join("?" for _ in statuses)
    df = query_df(
        f"""
        SELECT
            tf.target_field_id,
            tf.target_table,
            tf.target_column,
            tf.overall_status,
            tf.null_ratio,
            MAX(fm.total_score) AS best_score,
            latest_note.note AS comment
        FROM target_field tf
        LEFT JOIN field_match fm ON fm.target_field_id = tf.target_field_id
        LEFT JOIN (
            SELECT mn.target_field_id, mn.note
            FROM mapping_note mn
            JOIN (
                SELECT target_field_id, MAX(created_at) AS created_at
                FROM mapping_note
                GROUP BY target_field_id
            ) latest
              ON latest.target_field_id = mn.target_field_id
             AND latest.created_at = mn.created_at
        ) latest_note ON latest_note.target_field_id = tf.target_field_id
        WHERE tf.overall_status IN ({placeholders})
        {table_sql}
        GROUP BY tf.target_field_id
        ORDER BY {order_sql}
        """,
        tuple(params),
    )
    if df.empty:
        st.info("Ingen hull for valgt filter.")
        return
    df["status"] = df["overall_status"].map(STATUS_ICON).fillna("") + " " + df["overall_status"]
    st.dataframe(
        df[["target_table", "target_column", "status", "best_score", "null_ratio", "comment"]],
        use_container_width=True,
        hide_index=True,
    )
    labels = {f"{r.target_table}.{r.target_column} ({r.overall_status})": int(r.target_field_id) for r in df.itertuples()}
    selected = st.selectbox("Velg felt", list(labels.keys()))
    if st.button("Åpne valgt felt"):
        st.session_state["selected_target_field_id"] = labels[selected]
        st.session_state["page"] = "Field Detail"
        rerun()


def page_source_explorer() -> None:
    st.title("Source Explorer")
    query = st.text_input("Søk i schema/table/column")
    df = search_source_fields(query)
    if df.empty:
        st.info("Ingen source fields funnet.")
        return
    st.dataframe(
        df[["source_field_id", "source_field", "source_data_type", "row_count", "null_ratio", "distinct_count", "sample_values"]],
        use_container_width=True,
        hide_index=True,
    )

    targets = query_df(
        """
        SELECT target_field_id, target_table || '.' || target_column AS label
        FROM target_field
        ORDER BY target_table, target_column
        """
    )
    if targets.empty:
        return
    with st.expander("Legg source field inn som manuell kandidat"):
        source_options = {f"{r.source_field} ({r.source_field_id})": int(r.source_field_id) for r in df.itertuples()}
        target_options = {f"{r.label} ({r.target_field_id})": int(r.target_field_id) for r in targets.itertuples()}
        source_label = st.selectbox("Source", list(source_options.keys()))
        target_label = st.selectbox("Target", list(target_options.keys()))
        comment = st.text_input("Kommentar")
        if st.button("Legg til kandidat"):
            add_manual_candidate(target_options[target_label], source_options[source_label], comment)
            st.success("Manuell kandidat lagt til.")


def create_generic_adapter(profile: dict[str, Any]) -> DatabaseAdapter | None:
    bootstrap_local_environment()
    configure_profile_environment(profile)
    db_type = profile.get("db_type")
    if db_type == "SQLite":
        path = Path(profile.get("sqlite_path") or "").expanduser()
        if not path.exists():
            return None
        return SQLiteDatabaseAdapter(path)
    if db_type == "SQL Server":
        ensure_divclasses_on_syspath()
        try:
            from DivClasses.SQLServerBase import SqlServerBaseCls
        except ImportError as exc:
            raise RuntimeError("Fant ikke DivClasses.SQLServerBase i dette miljoet.") from exc

        prefix = profile_secret_prefix(profile)
        db = SqlServerBaseCls(prefix=prefix)
        db.connect()
        return SQLServerDatabaseAdapter(
            db,
            prefix=prefix,
            default_schema=getattr(db, "default_schema", None),
        )
    if db_type == "Oracle":
        ensure_divclasses_on_syspath()
        try:
            from DivClasses.OracleBase import OracleBaseCls
        except ImportError as exc:
            raise RuntimeError("Fant ikke DivClasses.OracleBase i dette miljoet.") from exc

        prefix = profile_secret_prefix(profile)
        owner = str(profile.get("oracle_owner") or profile.get("default_schema") or "IFSAPP")
        db = OracleBaseCls(prefix=prefix, owner_default=owner)
        db.connect()
        suspect_tables = load_suspect_oracle_tables(owner)
        return OracleDatabaseAdapter(db, owner=owner, table_names=suspect_tables)
    return None


def profile_from_label(profiles: pd.DataFrame, label: str) -> dict[str, Any] | None:
    if profiles.empty:
        return None
    labels = {connection_profile_label(row): row.to_dict() for _, row in profiles.iterrows()}
    return labels.get(label)


def test_connection_profile(profile: dict[str, Any]) -> dict[str, Any]:
    label = connection_profile_label(profile)
    adapter: DatabaseAdapter | None = None
    try:
        adapter = create_generic_adapter(profile)
        if adapter is None:
            return {
                "connection": label,
                "status": "FEIL",
                "message": "Profilen kunne ikke apnes. Sjekk sti/prefix/oppsett.",
                "columns": None,
            }

        columns = adapter.list_columns()
        return {
            "connection": label,
            "status": "OK",
            "message": f"Koblet til {adapter.database_name}.",
            "columns": len(columns),
        }
    except Exception as exc:
        return {
            "connection": label,
            "status": "FEIL",
            "message": str(exc),
            "columns": None,
        }
    finally:
        if adapter is not None:
            adapter.close()


def show_connection_status_panel(profiles: pd.DataFrame) -> None:
    if profiles.empty:
        return

    st.subheader("Connection status")
    labels = {connection_profile_label(row): row.to_dict() for _, row in profiles.iterrows()}
    selected = st.selectbox("Test connection", list(labels.keys()), key="connection_status_profile")
    c1, c2 = st.columns([1, 1])

    if c1.button("Test valgt connection"):
        st.session_state["connection_status_rows"] = [test_connection_profile(labels[selected])]

    if c2.button("Test alle aktive"):
        active_rows = [row.to_dict() for _, row in profiles[profiles["is_active"] == 1].iterrows()]
        st.session_state["connection_status_rows"] = [test_connection_profile(row) for row in active_rows]

    rows = st.session_state.get("connection_status_rows")
    if rows:
        status_df = pd.DataFrame(rows)
        st.dataframe(status_df, use_container_width=True, hide_index=True)

        failed = status_df[status_df["status"] != "OK"]
        if failed.empty:
            st.success("Alle testede connections svarer.")
        else:
            st.warning("En eller flere connections feilet. Se meldingene i tabellen.")
            for row in failed.itertuples(index=False):
                st.error(f"{row.connection}: {row.message}")


def package_status_rows() -> list[dict[str, Any]]:
    packages = [
        ("streamlit", "streamlit", True, "Kjerneapp"),
        ("pandas", "pandas", True, "Dataframes"),
        ("oracledb", "oracledb", False, "Oracle-driver"),
        ("cx_Oracle", "cx-Oracle", False, "Oracle-driver fallback"),
        ("pyodbc", "pyodbc", False, "SQL Server-driver"),
        ("pymssql", "pymssql", False, "SQL Server-driver fallback"),
    ]
    rows: list[dict[str, Any]] = []
    for module_name, package_name, required, purpose in packages:
        found = importlib.util.find_spec(module_name) is not None
        version = ""
        if found:
            try:
                version = importlib_metadata.version(package_name)
            except importlib_metadata.PackageNotFoundError:
                version = "installert"
        rows.append(
            {
                "status": "OK" if found else "Mangler",
                "module": module_name,
                "package": package_name,
                "version": version,
                "required": "Ja" if required else "Ved behov",
                "purpose": purpose,
            }
        )
    return rows


def show_python_environment_panel() -> None:
    with st.expander("Python/miljo-test", expanded=False):
        rows = package_status_rows()
        st.caption("Miljoet som faktisk kjorer denne Streamlit-appen.")
        st.code(
            "\n".join(
                [
                    f"Python: {sys.executable}",
                    f"Versjon: {platform.python_version()}",
                    f"Arbeidsmappe: {os.getcwd()}",
                    f"Appfil: {APP_DIR / 'streamlit_app.py'}",
                    f"Install: \"{sys.executable}\" -m pip install -r \"{APP_DIR / 'requirements.txt'}\"",
                ]
            ),
            language="text",
        )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        has_oracle_driver = any(row["status"] == "OK" for row in rows if row["module"] in {"oracledb", "cx_Oracle"})
        has_mssql_driver = any(row["status"] == "OK" for row in rows if row["module"] in {"pyodbc", "pymssql"})
        if not has_oracle_driver:
            st.info("Oracle krever `oracledb` eller `cx_Oracle` i samme Python som kjorer Streamlit.")
        if not has_mssql_driver:
            st.info("SQL Server krever `pyodbc` eller `pymssql` i samme Python som kjorer Streamlit.")


def page_connections() -> None:
    st.title("Connections")
    st.caption("Holder styr pa navngitte databaseprofiler. Hemmeligheter lagres fortsatt i miljo/setup, ikke i GUI-et.")

    profiles = get_connection_profiles(active_only=False)
    if profiles.empty:
        st.info("Ingen connection-profiler finnes enna.")
    else:
        show = profiles[
            [
                "connection_profile_id",
                "name",
                "db_type",
                "system_name",
                "environment",
                "sqlite_path",
                "config_prefix",
                "default_schema",
                "oracle_owner",
                "is_active",
                "notes",
            ]
        ].copy()
        show["is_active"] = show["is_active"].astype(bool)
        st.dataframe(show, use_container_width=True, hide_index=True)
        show_connection_status_panel(profiles)
        show_python_environment_panel()

    st.subheader("Legg til eller oppdater")
    with st.form("connection_profile_form"):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Navn", value="Local SQLite testdata")
        db_type = c2.selectbox("Database type", DATABASE_TYPES)
        system_name = c3.selectbox("System", CONNECTION_SYSTEMS)

        c4, c5, c6 = st.columns(3)
        environment = c4.selectbox("Miljo", CONNECTION_ENVIRONMENTS)
        config_prefix = c5.text_input("Config prefix", help="For SQL Server kan dette matche prefix i SqlServerBaseCls, f.eks. PFTSQL_.")
        default_schema = c6.text_input("Default schema")

        sqlite_path = st.text_input("SQLite path", value=str(GENERIC_TEST_DB_PATH))
        oracle_owner = st.text_input("Oracle owner", value="IFSAPP", help="Brukes som owner_default for Oracle/IFS-profiler.")
        notes = st.text_area("Notater", height=80)
        is_active = st.checkbox("Aktiv", value=True)

        if st.form_submit_button("Lagre connection"):
            if not name.strip():
                st.error("Navn er pakrevd.")
            else:
                upsert_connection_profile(
                    {
                        "name": name.strip(),
                        "db_type": db_type,
                        "system_name": system_name,
                        "environment": environment,
                        "sqlite_path": sqlite_path.strip() if db_type == "SQLite" else None,
                        "config_prefix": config_prefix.strip() or None,
                        "default_schema": default_schema.strip() or None,
                        "oracle_owner": oracle_owner.strip() or None,
                        "notes": notes.strip() or None,
                        "is_active": 1 if is_active else 0,
                    }
                )
                st.success("Connection lagret.")
                rerun()

    profiles = get_connection_profiles(active_only=False)
    if not profiles.empty:
        with st.expander("Aktiver/deaktiver"):
            labels = {connection_profile_label(row): int(row["connection_profile_id"]) for _, row in profiles.iterrows()}
            selected = st.selectbox("Connection", list(labels.keys()))
            b1, b2 = st.columns(2)
            if b1.button("Aktiver"):
                set_connection_profile_active(labels[selected], True)
                rerun()
            if b2.button("Deaktiver"):
                set_connection_profile_active(labels[selected], False)
                rerun()


def unique_in_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def selectbox_valid(label: str, options: list[str], key: str, index: int = 0) -> str:
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]
    if not options:
        options = [""]
    return st.selectbox(label, options, index=min(index, len(options) - 1), key=key)


def adapter_default_schema(adapter: DatabaseAdapter) -> str | None:
    if isinstance(adapter, SQLServerDatabaseAdapter):
        return adapter.default_schema
    if isinstance(adapter, OracleDatabaseAdapter):
        return adapter.owner
    return None


def select_generic_search_surface(
    label: str,
    adapter: DatabaseAdapter,
    key_prefix: str,
    allow_all_columns: bool = False,
) -> tuple[list[ColumnRef], list[ColumnRef]]:
    object_type_map = {
        "Tabeller": ["TABLE"],
        "Views": ["VIEW"],
    }

    if isinstance(adapter, (SQLServerDatabaseAdapter, OracleDatabaseAdapter)):
        selected_object_label = selectbox_valid(
            f"{label} 1) Views/tabeller",
            list(object_type_map.keys()),
            key=f"{key_prefix}_object_type",
        )

        schema_options = adapter.list_schemas()
        current_schema = adapter_default_schema(adapter)
        schema_values = list(schema_options)
        if current_schema and current_schema not in schema_values:
            schema_values.insert(0, current_schema)
        if not schema_values and current_schema:
            schema_values = [current_schema]

        selected_schema = current_schema
        if schema_values:
            default_index = schema_values.index(current_schema) if current_schema in schema_values else 0
            selected_schema = selectbox_valid(
                f"{label} 2) Schema",
                schema_values,
                key=f"{key_prefix}_schema",
                index=default_index,
            )
        elif current_schema:
            st.caption(f"{label} schema: {current_schema}")

        adapter.configure_metadata_filter(
            schema=selected_schema,
            object_types=object_type_map[selected_object_label],
        )
    else:
        st.caption(f"{label} 1) Views/tabeller: Tabeller")
        selected_schema = None

    columns = adapter.list_columns()
    tables = sorted({column.table for column in columns})
    st.caption(f"Etter filter: {len(tables)} objekter, {len(columns)} kolonner")

    if not columns:
        st.info(f"Ingen kolonner funnet for {label.lower()} med valgt filter.")
        return columns, []

    selected_table = selectbox_valid(
        f"{label} 3) Tabell",
        ["Velg tabell"] + tables,
        key=f"{key_prefix}_table",
    )
    if selected_table == "Velg tabell":
        return columns, []

    table_columns = [column for column in columns if column.table == selected_table]
    column_names = unique_in_order([column.column for column in table_columns])
    column_options = ["Velg kolonne"] + column_names
    if allow_all_columns:
        column_options.insert(1, "Alle kolonner i valgt tabell")

    selected_column = selectbox_valid(
        f"{label} 4) Kolonne",
        column_options,
        key=f"{key_prefix}_column",
    )
    if selected_column == "Velg kolonne":
        return columns, []
    if selected_column == "Alle kolonner i valgt tabell":
        return columns, table_columns
    return columns, [column for column in table_columns if column.column == selected_column]


def select_generic_search_scope(
    label: str,
    adapter: DatabaseAdapter,
    key_prefix: str,
) -> list[ColumnRef]:
    if isinstance(adapter, (SQLServerDatabaseAdapter, OracleDatabaseAdapter)):
        schema_options = adapter.list_schemas()
        current_schema = adapter_default_schema(adapter)
        schema_values = list(schema_options)
        if current_schema and current_schema not in schema_values:
            schema_values.insert(0, current_schema)
        if not schema_values and current_schema:
            schema_values = [current_schema]

        selected_schema = current_schema
        if schema_values:
            default_index = schema_values.index(current_schema) if current_schema in schema_values else 0
            selected_schema = selectbox_valid(
                f"{label} schema",
                schema_values,
                key=f"{key_prefix}_scope_schema",
                index=default_index,
            )
        elif current_schema:
            st.caption(f"{label} schema: {current_schema}")

        adapter.configure_metadata_filter(
            schema=selected_schema,
            object_types=["TABLE", "VIEW"],
        )
        st.caption("Soker i alle tabeller/views i valgt schema.")
    else:
        st.caption("Soker i alle tabeller i valgt database.")

    columns = adapter.list_columns()
    tables = sorted({column.table for column in columns})
    st.caption(f"Sokeomrade: {len(tables)} objekter, {len(columns)} kolonner")
    if not columns:
        if isinstance(adapter, OracleDatabaseAdapter) and adapter.table_names:
            st.warning(
                "Destinasjonen har 0 kolonner etter Oracle suspects-filteret. "
                "Sjekk at tabellene i ifs_knowledge/suspects.txt finnes i valgt schema."
            )
            with st.expander("Vis suspects-filter"):
                st.write(list(adapter.table_names))
        else:
            st.info(f"Ingen kolonner funnet for {label.lower()} med valgt scope.")
    return columns


def reset_session_keys(*keys: str) -> None:
    for key in keys:
        st.session_state.pop(key, None)


def reset_when_signature_changes(signature_key: str, signature: tuple[Any, ...], keys_to_reset: tuple[str, ...]) -> None:
    old_signature = st.session_state.get(signature_key)
    if old_signature is not None and old_signature != signature:
        reset_session_keys(*keys_to_reset)
    st.session_state[signature_key] = signature


def page_generic_db_search() -> None:
    st.title("Micromanagement Search")
    st.caption("Velg kilde for fingerprints og destinasjon for søk. Kilde og destinasjon kan være samme database.")

    with st.expander("Testdata"):
        c1, c2 = st.columns([2, 1])
        test_db_path = c1.text_input("SQLite testdatabase", value=str(GENERIC_TEST_DB_PATH))
        seed = c2.number_input("Random seed", min_value=1, max_value=999999, value=42, step=1)
        if st.button("Lag random SQLite-testdatabase"):
            path = create_random_sqlite_database(test_db_path, seed=int(seed))
            st.success(f"Laget testdatabase: {path}")

    source_panel, destination_panel = st.columns(2)
    active_profiles = get_connection_profiles(active_only=True)
    if active_profiles.empty:
        st.info("Legg inn minst en aktiv connection pa Connections-siden.")
        return
    profile_labels = [connection_profile_label(row) for _, row in active_profiles.iterrows()]

    with source_panel:
        st.subheader("Kilde")
        source_label = st.selectbox("Kilde connection", profile_labels, key="generic_source_profile")
        source_profile = profile_from_label(active_profiles, source_label)

    with destination_panel:
        st.subheader("Destinasjon")
        same_as_source = st.checkbox("Bruk samme database som kilde", value=True)
        if same_as_source:
            destination_profile = source_profile
            st.caption(source_label)
        else:
            destination_label = st.selectbox("Destinasjon connection", profile_labels, key="generic_destination_profile")
            destination_profile = profile_from_label(active_profiles, destination_label)

    settings = st.columns(3)
    min_length = settings[0].number_input("Min tekstlengde", min_value=1, max_value=80, value=5)
    max_values_per_column = settings[1].number_input("Unike per kolonne", min_value=1, max_value=50, value=5)
    max_columns = settings[2].number_input("Maks kolonner", min_value=1, max_value=1000, value=100)

    if not source_profile or not destination_profile:
        st.info("Velg kilde og destinasjon.")
        return

    try:
        source_adapter = create_generic_adapter(source_profile)
        if (
            source_profile
            and destination_profile
            and source_profile.get("connection_profile_id") == destination_profile.get("connection_profile_id")
        ):
            destination_adapter = source_adapter
        else:
            destination_adapter = create_generic_adapter(destination_profile)
    except Exception as exc:
        st.error(f"Klarte ikke apne connection: {exc}")
        return
    if source_adapter is None:
        st.info("Kildeprofilen kan ikke apnes. Sjekk connection-profilen.")
        return
    if destination_adapter is None:
        st.info("Destinasjonsprofilen kan ikke apnes. Sjekk connection-profilen.")
        source_adapter.close()
        return

    try:
        source_select, destination_select = st.columns(2)
        with source_select:
            selected_source_columns: list[ColumnRef]
            source_columns, selected_source_columns = select_generic_search_surface(
                "Kilde",
                source_adapter,
                "generic_source",
            )
        with destination_select:
            destination_columns = select_generic_search_scope(
                "Destinasjon",
                destination_adapter,
                "generic_destination",
            )

        source_signature = (
            source_profile.get("connection_profile_id"),
            tuple(column.label for column in selected_source_columns),
            int(min_length),
            int(max_values_per_column),
            int(max_columns),
        )
        destination_signature = (
            destination_profile.get("connection_profile_id"),
            bool(same_as_source),
            st.session_state.get("generic_destination_scope_schema"),
        )
        reset_when_signature_changes(
            "generic_source_signature",
            source_signature,
            ("generic_unique_values", "generic_value_hits"),
        )
        reset_when_signature_changes(
            "generic_destination_signature",
            destination_signature,
            ("generic_value_hits",),
        )

        source_engine = GenericDatabaseSearch(source_adapter)
        destination_engine = GenericDatabaseSearch(destination_adapter)

        metric_cols = st.columns(2)
        metric_cols[0].metric("Kildekolonner etter filter", len(source_columns))
        metric_cols[1].metric("Destinasjonskolonner etter filter", len(destination_columns))
        if isinstance(source_adapter, OracleDatabaseAdapter) and source_adapter.table_names:
            metric_cols[0].caption(f"Oracle suspects-filter: {len(source_adapter.table_names)} tabeller")
        if isinstance(destination_adapter, OracleDatabaseAdapter) and destination_adapter.table_names:
            metric_cols[1].caption(f"Oracle suspects-filter: {len(destination_adapter.table_names)} tabeller")

        if not selected_source_columns:
            st.info("Velg kilde: type, schema, tabell og kolonne forst.")
        if not destination_columns:
            reset_session_keys("generic_value_hits")
            st.info("Velg destinasjon: connection/database og eventuelt schema.")

        if st.button("1) Finn unike verdier", disabled=not selected_source_columns):
            unique_df = source_engine.find_unique_values(
                min_length=int(min_length),
                max_values_per_column=int(max_values_per_column),
                max_columns=int(max_columns),
                columns=selected_source_columns,
            )
            st.session_state["generic_unique_values"] = unique_df

        unique_df = st.session_state.get("generic_unique_values")
        if unique_df is not None:
            st.subheader("Unike verdier")
            if unique_df.empty:
                st.info("Fant ingen unike verdier med valgt filter.")
            else:
                unique_df = unique_df.copy()
                unique_df["value_choice"] = unique_df.apply(
                    lambda row: f"{row['value']} | {row['source_field']} | score={row['fingerprint_score']}",
                    axis=1,
                )
                st.dataframe(
                    unique_df[
                        [
                            "value",
                            "value_kind",
                            "source_field",
                            "source_data_type",
                            "occurrence_count",
                            "value_length",
                            "fingerprint_score",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

                selected_value_label = st.selectbox(
                    "Velg unik verdi fra listen",
                    unique_df["value_choice"].tolist(),
                )
                selected_value_row = unique_df.loc[unique_df["value_choice"] == selected_value_label].iloc[0]
                manual_value = st.text_input(
                    "Verdi å lete etter",
                    value=str(selected_value_row["value"]),
                    help="Velg fra listen over eller lim inn en verdi fra et annet sted.",
                )
                manual_kind = st.selectbox(
                    "Verditype",
                    ["text", "number", "date"],
                    index=["text", "number", "date"].index(str(selected_value_row.get("value_kind") or "text")),
                )

                if st.button("2) Let etter valgt verdi", disabled=not destination_columns):
                    hits_df = destination_engine.search_single_value(
                        manual_value,
                        value_kind=manual_kind,
                        max_hits_per_column=100,
                        columns=destination_columns,
                    )
                    st.session_state["generic_value_hits"] = hits_df

        hits_df = st.session_state.get("generic_value_hits")
        if hits_df is not None:
            st.subheader("Treff")
            if hits_df.empty:
                st.info("Ingen treff funnet.")
            else:
                st.dataframe(
                    hits_df[
                        [
                            "hit_table",
                            "hit_column",
                        ]
                    ].drop_duplicates(),
                    use_container_width=True,
                    hide_index=True,
                )
    except Exception as exc:
        st.error(f"Soket stoppet: {exc}")
    finally:
        source_adapter.close()
        if destination_adapter is not source_adapter:
            destination_adapter.close()


def page_export() -> None:
    st.title("Export")
    mode = st.selectbox(
        "Eksportvalg",
        ["Kun confirmed", "Confirmed + preferred_candidate", "Alle kandidater", "Alle hull", "Full rapport"],
    )
    df = export_mapping(mode)
    st.dataframe(df, use_container_width=True, hide_index=True)
    csv_data = df.to_csv(index=False, sep=";", encoding="utf-8-sig")
    st.download_button(
        "Last ned CSV",
        csv_data,
        file_name=f"compare_seek_mapping_{mode.lower().replace(' ', '_').replace('+', 'plus')}.csv",
        mime="text/csv",
    )


def main() -> None:
    bootstrap_local_environment()
    st.set_page_config(page_title="CompareNSeek", layout="wide")
    init_db()

    pages = {
        "Dashboard": page_dashboard,
        "Connections": page_connections,
        "Import": page_import,
        "Table Explorer": page_table_explorer,
        "Field Detail": page_field_detail,
        "Gap List": page_gap_list,
        "Source Explorer": page_source_explorer,
        "Micromanagement Search": page_generic_db_search,
        "Export": page_export,
    }
    default_page = st.session_state.get("page", "Dashboard")
    if default_page not in pages:
        default_page = "Dashboard"
    selected_page = st.sidebar.radio("Navigasjon", list(pages.keys()), index=list(pages.keys()).index(default_page))
    st.session_state["page"] = selected_page
    st.sidebar.caption(f"Database: {DB_PATH.relative_to(APP_DIR)}")
    pages[selected_page]()


if __name__ == "__main__":
    main()
