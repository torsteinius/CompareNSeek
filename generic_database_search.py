#!/usr/bin/env python3
"""
Generic value discovery and search engine for CompareNSeek.

This module is deliberately separate from the existing Oracle/DWH profiler. The
interface is small enough that Oracle, SQL Server and SQLite adapters can expose
the same behavior without changing the Streamlit workflow.
"""

from __future__ import annotations

import random
import sqlite3
import string
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


@dataclass(frozen=True)
class ColumnRef:
    database: str
    schema: str | None
    table: str
    column: str
    data_type: str | None = None

    @property
    def label(self) -> str:
        schema_part = f"{self.schema}." if self.schema else ""
        return f"{schema_part}{self.table}.{self.column}"


@dataclass(frozen=True)
class UniqueValue:
    value: str
    value_kind: str
    source: ColumnRef
    occurrence_count: int


@dataclass(frozen=True)
class ValueHit:
    value: str
    value_kind: str
    source: ColumnRef
    hit: ColumnRef
    hit_count: int


class DatabaseAdapter(ABC):
    """Small database surface needed by the generic search engine."""

    database_name: str

    @abstractmethod
    def list_columns(self) -> list[ColumnRef]:
        """Return searchable columns."""

    @abstractmethod
    def find_unique_values_in_column(
        self,
        column: ColumnRef,
        min_length: int,
        limit: int,
    ) -> list[UniqueValue]:
        """Return distinctive values from one column."""

    @abstractmethod
    def count_value_hits(self, column: ColumnRef, value: str, value_kind: str, max_hits: int) -> int:
        """Count exact hits for one value in one column, capped by max_hits."""

    def close(self) -> None:
        return None


def value_kind(data_type: str | None) -> str:
    dt = (data_type or "").lower()
    if any(token in dt for token in ("int", "real", "float", "double", "decimal", "numeric", "number")):
        return "number"
    if any(token in dt for token in ("date", "time", "timestamp")):
        return "date"
    return "text"


def should_search_value(value: Any, kind: str, min_length: int) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if kind == "number":
        return text not in {"0", "1"} and len(text) >= min_length
    if kind == "date":
        return len(text) >= 8
    return len(text) >= min_length


def fingerprint_score(value: Any, kind: str, min_length: int) -> float:
    """
    Score values that are big and distinctive enough to work as fingerprints.

    The goal is not "longest possible". A good value is usually medium/long,
    contains enough entropy, and is unlikely to be a common status, city, year,
    amount, or short sequence number.
    """
    text = str(value or "").strip()
    if not should_search_value(text, kind, min_length):
        return 0.0

    length = len(text)
    unique_chars = len(set(text.lower()))
    alpha = sum(1 for char in text if char.isalpha())
    digit = sum(1 for char in text if char.isdigit())
    separators = sum(1 for char in text if not char.isalnum() and not char.isspace())
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", text) if token]

    if length <= 64:
        length_score = min(length / 24, 1.0)
    else:
        length_score = max(0.45, 1.0 - ((length - 64) / 160))
    entropy_score = min(unique_chars / 14, 1.0)
    mix_score = 0.0
    if alpha and digit:
        mix_score += 0.25
    if separators:
        mix_score += 0.15
    if len(tokens) >= 2:
        mix_score += 0.10

    score = (0.45 * length_score) + (0.40 * entropy_score) + mix_score

    if kind == "number":
        if length < max(min_length, 5):
            score *= 0.35
        if re.fullmatch(r"\d{4}", text):
            score *= 0.25

    if kind == "text":
        lowered = text.lower()
        common_values = {
            "oslo",
            "bergen",
            "trondheim",
            "active",
            "inactive",
            "unknown",
            "true",
            "false",
            "ja",
            "nei",
            "yes",
            "no",
        }
        if lowered in common_values:
            score *= 0.15
        if length > 80:
            score *= 0.70
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            score *= 0.72
        if text.count("'") + text.count('"') >= 4 and ":" in text:
            score *= 0.78

    return round(min(score, 1.0), 4)


class SQLiteDatabaseAdapter(DatabaseAdapter):
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.database_name = self.db_path.name
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def list_columns(self) -> list[ColumnRef]:
        rows = self.conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        columns: list[ColumnRef] = []
        for row in rows:
            table = str(row["name"])
            for col in self.conn.execute(f"PRAGMA table_info({sqlite_ident(table)})").fetchall():
                columns.append(
                    ColumnRef(
                        database=self.database_name,
                        schema=None,
                        table=table,
                        column=str(col["name"]),
                        data_type=str(col["type"] or ""),
                    )
                )
        return columns

    def find_unique_values_in_column(
        self,
        column: ColumnRef,
        min_length: int,
        limit: int,
    ) -> list[UniqueValue]:
        kind = value_kind(column.data_type)
        col_sql = sqlite_ident(column.column)
        table_sql = sqlite_ident(column.table)
        rows = self.conn.execute(
            f"""
            SELECT CAST({col_sql} AS TEXT) AS value_text, COUNT(*) AS occurrence_count
            FROM {table_sql}
            WHERE {col_sql} IS NOT NULL
              AND TRIM(CAST({col_sql} AS TEXT)) != ''
            GROUP BY {col_sql}
            HAVING COUNT(*) = 1
            ORDER BY
                CASE WHEN LENGTH(CAST({col_sql} AS TEXT)) >= ? THEN 0 ELSE 1 END,
                ABS(LENGTH(CAST({col_sql} AS TEXT)) - 24),
                value_text
            LIMIT ?
            """,
            (int(min_length), int(limit) * 50,),
        ).fetchall()

        candidates: list[tuple[float, UniqueValue]] = []
        for row in rows:
            value = str(row["value_text"])
            score = fingerprint_score(value, kind, min_length)
            if score > 0:
                candidates.append(
                    (
                        score,
                        UniqueValue(
                            value=value,
                            value_kind=kind,
                            source=column,
                            occurrence_count=int(row["occurrence_count"]),
                        ),
                    )
                )
        candidates.sort(key=lambda item: (-item[0], item[1].value))
        return [value for _score, value in candidates[:limit]]

    def count_value_hits(self, column: ColumnRef, value: str, value_kind: str, max_hits: int) -> int:
        if value_kind != value_kind_for_column(column):
            return 0
        col_sql = sqlite_ident(column.column)
        table_sql = sqlite_ident(column.table)
        rows = self.conn.execute(
            f"""
            SELECT COUNT(*) AS hit_count
            FROM (
                SELECT 1
                FROM {table_sql}
                WHERE CAST({col_sql} AS TEXT) = ?
                LIMIT ?
            )
            """,
            (str(value), int(max_hits) + 1),
        ).fetchone()
        return int(rows["hit_count"]) if rows else 0


class GenericDatabaseSearch:
    """Find distinctive values in one database, then search for them broadly."""

    def __init__(self, adapter: DatabaseAdapter):
        self.adapter = adapter

    def find_unique_values(
        self,
        min_length: int = 5,
        max_values_per_column: int = 5,
        max_columns: int | None = None,
        columns: list[ColumnRef] | None = None,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        searchable_columns = columns if columns is not None else self.adapter.list_columns()
        if max_columns is not None:
            searchable_columns = searchable_columns[: int(max_columns)]

        for column in searchable_columns:
            for item in self.adapter.find_unique_values_in_column(column, min_length, max_values_per_column):
                rows.append(unique_value_to_row(item))

        return pd.DataFrame(rows)

    def search_values(
        self,
        values: pd.DataFrame | Iterable[UniqueValue | dict[str, Any]],
        max_hits_per_column: int = 100,
        max_search_values: int | None = None,
        columns: list[ColumnRef] | None = None,
    ) -> pd.DataFrame:
        search_values = normalize_search_values(values)
        if max_search_values is not None:
            search_values = search_values[: int(max_search_values)]

        searchable_columns = columns if columns is not None else self.adapter.list_columns()
        rows: list[dict[str, Any]] = []
        for item in search_values:
            for column in searchable_columns:
                hit_count = self.adapter.count_value_hits(column, item.value, item.value_kind, max_hits_per_column)
                if hit_count > 0:
                    rows.append(value_hit_to_row(ValueHit(item.value, item.value_kind, item.source, column, hit_count)))

        return pd.DataFrame(rows)

    def search_single_value(
        self,
        value: str,
        value_kind: str = "text",
        max_hits_per_column: int = 100,
        columns: list[ColumnRef] | None = None,
    ) -> pd.DataFrame:
        source = ColumnRef(
            database=self.adapter.database_name,
            schema=None,
            table="manual",
            column="value",
            data_type=value_kind,
        )
        unique = UniqueValue(
            value=str(value),
            value_kind=value_kind,
            source=source,
            occurrence_count=1,
        )
        return self.search_values(
            [unique],
            max_hits_per_column=max_hits_per_column,
            max_search_values=1,
            columns=columns,
        )


class SystematicDatabaseSearch:
    """
    Batch runner for the same engine used by the micromanagement surface.

    The interactive UI chooses a few columns manually. This runner is for the
    systematic DWH -> source database flow where every relevant DWH column is
    profiled for fingerprints and searched in a broader destination surface.
    """

    def __init__(self, source_adapter: DatabaseAdapter, destination_adapter: DatabaseAdapter):
        self.source = GenericDatabaseSearch(source_adapter)
        self.destination = GenericDatabaseSearch(destination_adapter)

    def run(
        self,
        source_columns: list[ColumnRef],
        destination_columns: list[ColumnRef],
        min_length: int = 5,
        max_values_per_column: int = 5,
        max_hits_per_column: int = 100,
        max_search_values_per_source_column: int | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        unique_frames: list[pd.DataFrame] = []
        hit_frames: list[pd.DataFrame] = []

        for source_column in source_columns:
            unique_df = self.source.find_unique_values(
                min_length=min_length,
                max_values_per_column=max_values_per_column,
                columns=[source_column],
            )
            if unique_df.empty:
                continue

            hits_df = self.destination.search_values(
                unique_df,
                max_hits_per_column=max_hits_per_column,
                max_search_values=max_search_values_per_source_column,
                columns=destination_columns,
            )
            unique_frames.append(unique_df)
            if not hits_df.empty:
                hit_frames.append(hits_df)

        all_unique = pd.concat(unique_frames, ignore_index=True) if unique_frames else pd.DataFrame()
        all_hits = pd.concat(hit_frames, ignore_index=True) if hit_frames else pd.DataFrame()
        return all_unique, all_hits


def value_kind_for_column(column: ColumnRef) -> str:
    return value_kind(column.data_type)


def unique_value_to_row(item: UniqueValue) -> dict[str, Any]:
    return {
        "value": item.value,
        "value_kind": item.value_kind,
        "source_database": item.source.database,
        "source_schema": item.source.schema,
        "source_table": item.source.table,
        "source_column": item.source.column,
        "source_data_type": item.source.data_type,
        "source_field": item.source.label,
        "occurrence_count": item.occurrence_count,
        "value_length": len(item.value),
        "fingerprint_score": fingerprint_score(item.value, item.value_kind, min_length=1),
    }


def value_hit_to_row(item: ValueHit) -> dict[str, Any]:
    return {
        "value": item.value,
        "value_kind": item.value_kind,
        "source_field": item.source.label,
        "hit_field": item.hit.label,
        "hit_table": item.hit.table,
        "hit_column": item.hit.column,
        "hit_data_type": item.hit.data_type,
        "hit_count": item.hit_count,
        "same_column": item.source.table == item.hit.table and item.source.column == item.hit.column,
    }


def normalize_search_values(values: pd.DataFrame | Iterable[UniqueValue | dict[str, Any]]) -> list[UniqueValue]:
    if isinstance(values, pd.DataFrame):
        records = values.to_dict("records")
    else:
        records = list(values)

    out: list[UniqueValue] = []
    seen: set[tuple[str, str]] = set()
    for item in records:
        if isinstance(item, UniqueValue):
            value = item
        else:
            source = ColumnRef(
                database=str(item.get("source_database") or ""),
                schema=item.get("source_schema"),
                table=str(item.get("source_table") or ""),
                column=str(item.get("source_column") or ""),
                data_type=item.get("source_data_type"),
            )
            value = UniqueValue(
                value=str(item.get("value") or ""),
                value_kind=str(item.get("value_kind") or "text"),
                source=source,
                occurrence_count=int(item.get("occurrence_count") or 1),
            )
        key = (value.value_kind, value.value)
        if value.value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def sqlite_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def create_random_sqlite_database(path: str | Path, seed: int = 42) -> Path:
    """Create a small database with overlapping random values for demos/tests."""
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    rnd = random.Random(seed)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            customer_code TEXT,
            name TEXT,
            city TEXT,
            external_ref TEXT
        );

        CREATE TABLE contracts (
            contract_id INTEGER PRIMARY KEY,
            contract_no TEXT,
            customer_code TEXT,
            amount REAL,
            rare_marker TEXT
        );

        CREATE TABLE invoices (
            invoice_id INTEGER PRIMARY KEY,
            invoice_no TEXT,
            contract_no TEXT,
            customer_code TEXT,
            amount REAL,
            reference_text TEXT
        );

        CREATE TABLE audit_log (
            audit_id INTEGER PRIMARY KEY,
            entity_ref TEXT,
            payload TEXT,
            operator TEXT
        );
        """
    )

    shared_customer_codes = [f"CUST-{1000 + i}" for i in range(8)]
    shared_contracts = [f"CTR-{rnd.randrange(100000, 999999)}" for _ in range(6)]
    rare_markers = [random_token(rnd, "UNIQ") for _ in range(12)]

    for i in range(1, 31):
        customer_code = shared_customer_codes[i % len(shared_customer_codes)]
        external_ref = rare_markers[i % len(rare_markers)] if i <= 12 else random_token(rnd, "EXT")
        con.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?)",
            (i, customer_code, f"Customer {i}", rnd.choice(["Oslo", "Bergen", "Trondheim"]), external_ref),
        )

    for i in range(1, 25):
        contract_no = shared_contracts[i % len(shared_contracts)]
        marker = rare_markers[i % len(rare_markers)] if i <= 12 else random_token(rnd, "CON")
        con.execute(
            "INSERT INTO contracts VALUES (?, ?, ?, ?, ?)",
            (i, contract_no, shared_customer_codes[i % len(shared_customer_codes)], round(rnd.uniform(5000, 90000), 2), marker),
        )

    for i in range(1, 35):
        contract_no = shared_contracts[i % len(shared_contracts)]
        con.execute(
            "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?)",
            (
                i,
                f"INV-{2026}-{i:04d}",
                contract_no,
                shared_customer_codes[i % len(shared_customer_codes)],
                round(rnd.uniform(1000, 40000), 2),
                rare_markers[i % len(rare_markers)] if i <= 12 else f"Invoice for {contract_no}",
            ),
        )

    for i, marker in enumerate(rare_markers, start=1):
        con.execute(
            "INSERT INTO audit_log VALUES (?, ?, ?, ?)",
            (i, marker, f"{{'marker':'{marker}','status':'checked'}}", rnd.choice(["system", "etl", "analyst"])),
        )

    con.commit()
    con.close()
    return db_path


def random_token(rnd: random.Random, prefix: str) -> str:
    body = "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    return f"{prefix}-{body}"
