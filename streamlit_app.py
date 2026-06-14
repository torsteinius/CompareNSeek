#!/usr/bin/env python3
"""
Streamlit MVP for CompareNSeek.

Run:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from generic_database_search import (
    ColumnRef,
    DatabaseAdapter,
    GenericDatabaseSearch,
    SQLiteDatabaseAdapter,
    create_random_sqlite_database,
)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "compare_seek.sqlite"
GENERIC_TEST_DB_PATH = DATA_DIR / "generic_random_test.sqlite"
DATABASE_TYPES = ["SQLite", "Oracle", "SQL Server"]

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
        """
    )
    con.commit()


def query_df(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_connection(), params=params)


def scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur = get_connection().execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


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


def create_generic_adapter(db_type: str, db_path: str) -> DatabaseAdapter | None:
    if db_type == "SQLite":
        path = Path(db_path).expanduser()
        if not path.exists():
            return None
        return SQLiteDatabaseAdapter(path)
    return None


def select_generic_columns(
    label: str,
    columns: list[ColumnRef],
    key_prefix: str,
    allow_all_columns: bool = True,
) -> list[ColumnRef]:
    if not columns:
        st.info(f"Ingen kolonner funnet for {label.lower()}.")
        return []

    tables = sorted({column.table for column in columns})
    selected_table = st.selectbox(f"{label} tabell", ["Alle tabeller"] + tables, key=f"{key_prefix}_table")
    table_columns = columns if selected_table == "Alle tabeller" else [c for c in columns if c.table == selected_table]

    column_names = sorted({column.column for column in table_columns})
    column_options = ["Alle kolonner"] + column_names if allow_all_columns else column_names
    selected_column = st.selectbox(f"{label} kolonne", column_options, key=f"{key_prefix}_column")
    if selected_column == "Alle kolonner":
        return table_columns
    return [c for c in table_columns if c.column == selected_column]


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
    with source_panel:
        st.subheader("Kilde")
        source_db_type = st.selectbox("Kilde database type", DATABASE_TYPES, key="generic_source_type")
        source_db_path = st.text_input("Kilde SQLite path", value=str(GENERIC_TEST_DB_PATH), key="generic_source_path")
        if source_db_type != "SQLite":
            st.warning("Denne database-typen er ikke koblet til generisk adapter ennå.")

    with destination_panel:
        st.subheader("Destinasjon")
        destination_db_type = st.selectbox("Destinasjon database type", DATABASE_TYPES, key="generic_destination_type")
        same_as_source = st.checkbox("Bruk samme database som kilde", value=True)
        if same_as_source:
            destination_db_type = source_db_type
            destination_db_path = source_db_path
            st.caption(destination_db_path)
        else:
            destination_db_path = st.text_input(
                "Destinasjon SQLite path",
                value=str(GENERIC_TEST_DB_PATH),
                key="generic_destination_path",
            )
        if destination_db_type != "SQLite":
            st.warning("Denne database-typen er ikke koblet til generisk adapter ennå.")

    settings = st.columns(3)
    min_length = settings[0].number_input("Min tekstlengde", min_value=1, max_value=80, value=5)
    max_values_per_column = settings[1].number_input("Unike per kolonne", min_value=1, max_value=50, value=5)
    max_columns = settings[2].number_input("Maks kolonner", min_value=1, max_value=1000, value=100)

    source_adapter = create_generic_adapter(source_db_type, source_db_path)
    destination_adapter = create_generic_adapter(destination_db_type, destination_db_path)
    if source_adapter is None:
        st.info("Velg en eksisterende SQLite-fil som kilde, eller lag testdatabasen først.")
        return
    if destination_adapter is None:
        st.info("Velg en eksisterende SQLite-fil som destinasjon.")
        source_adapter.close()
        return

    try:
        source_engine = GenericDatabaseSearch(source_adapter)
        destination_engine = GenericDatabaseSearch(destination_adapter)
        source_columns = source_adapter.list_columns()
        destination_columns = destination_adapter.list_columns()

        metric_cols = st.columns(2)
        metric_cols[0].metric("Kildekolonner", len(source_columns))
        metric_cols[1].metric("Destinasjonskolonner", len(destination_columns))

        source_select, destination_select = st.columns(2)
        with source_select:
            selected_source_columns = select_generic_columns("Kilde", source_columns, "generic_source")
        with destination_select:
            selected_destination_columns = select_generic_columns(
                "Destinasjon",
                destination_columns,
                "generic_destination",
            )

        if st.button("1) Finn unike verdier"):
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

                if st.button("2) Let etter valgt verdi"):
                    hits_df = destination_engine.search_single_value(
                        manual_value,
                        value_kind=manual_kind,
                        max_hits_per_column=100,
                        columns=selected_destination_columns,
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
    st.set_page_config(page_title="CompareNSeek", layout="wide")
    init_db()

    pages = {
        "Dashboard": page_dashboard,
        "Import": page_import,
        "Table Explorer": page_table_explorer,
        "Field Detail": page_field_detail,
        "Gap List": page_gap_list,
        "Source Explorer": page_source_explorer,
        "Micromanagement Search": page_generic_db_search,
        "Export": page_export,
    }
    default_page = st.session_state.get("page", "Dashboard")
    selected_page = st.sidebar.radio("Navigasjon", list(pages.keys()), index=list(pages.keys()).index(default_page))
    st.session_state["page"] = selected_page
    st.sidebar.caption(f"Database: {DB_PATH.relative_to(APP_DIR)}")
    pages[selected_page]()


if __name__ == "__main__":
    main()
