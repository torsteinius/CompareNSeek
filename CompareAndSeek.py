#!/usr/bin/env python3
"""
ifs_oracle_source_profiler.py

Formål:
- Bygge et lokalt metadata-bibliotek over IFS/Oracle-kilden.
- Finne tabeller med data.
- Hente kolonner, radantall/estimert radantall og 10 eksempelrader.
- Sammenligne DWH/Lydia-felter mot Oracle/IFS-tabeller og kolonner.
- Lage en stadig bedre oversikt for migrering fra Lydia-DWH til IFS-master.

Avhengigheter:
    pip install oracledb pyodbc pandas python-dotenv

Miljøvariabler:
    ORACLE_USER
    ORACLE_PASSWORD
    ORACLE_DSN

    SQL Server/DWH bruker samme DivClasses.SQLServerBase-oppsett som LydiaSQLDataOutput.py.
    Lokale verdier kan legges i setup.txt, som holdes utenfor git.

Kjøring:
    python ifs_oracle_source_profiler.py --min-rows 1 --sample-rows 10
    python ifs_oracle_source_profiler.py --schemas IFSAPP --min-rows 10
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = None
for parent in THIS_FILE.parents:
    if (parent / "DivClasses").exists():
        REPO_ROOT = parent
        break

if REPO_ROOT is not None and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("RUN_LOCAL", "True")

from DivClasses.OracleBase import OracleBaseCls
from DivClasses.SQLServerBase import SqlServerBaseCls


# =============================================================================
# KONFIG
# =============================================================================

OUT_DIR = Path("ifs_profile_output")
DB_PATH = OUT_DIR / "ifs_source_profile.sqlite"
REPORT_DIR = OUT_DIR / "reports"
SAMPLE_DIR = OUT_DIR / "samples"

DEFAULT_SCHEMAS: list[str] = ["IFSAPP"]

# Viktige DWH-felt fra Lydia-migreringen/rapportene.
# Dette er startlisten. Den kan utvides automatisk fra DWH-tabeller.
BUSINESS_FIELDS = [
    "Avtalenr",
    "Avtalenavn",
    "Avtalegruppe",
    "KontraktFra",
    "KontraktTil",
    "Signaturdato",
    "Overtagelsesdato",
    "LeverandorNavn",
    "LeverandorOrgnr",
    "LeietakerNavn",
    "LeietakerNr",
    "LokNr",
    "LokNavn",
    "Byggnavn",
    "Gateadresse",
    "Gateadresse2",
    "Postnr",
    "Poststed",
    "Websak",
    "OppsigelsePeriode",
    "OppsigelsesPeriodeMnd",
    "HarOppsigelsesklausul",
    "OppsigelsesVilkar",
    "Opsjonmulighet_Kode",
    "Opsjonsbeskrivelse",
    "Opsjonsfrist_Dato",
    "Opsjonsutlop_Dato",
    "Egnethet_1_5",
    "Politispesifikt_Areal_m2",
    "Fellesareal_m2",
    "Innv_Parkering_m2",
    "Renhold_Kode",
    "Renhold_Beskrivelse",
    "Kantine_Kode",
    "Kantine_Beskrivelse",
    "Energi_Inkl_Felleskost_Kode",
    "Off_Lokasjon_Kode",
    "Forvaltningskategori_Kode",
    "Investering_InnevAr",
    "Investering_Ar2",
    "Investering_Ar3",
    "Investering_Ar4",
    "Ny_Leie",
    "Nytt_Areal",
    "Gyldig_Fra_Ar",
    "AntallLeieLinjer",
    "Aarsleie_Leid_Areal_Kr",
    "Felleskost_AKonto_Kr",
    "KPI_Intervall_Mnd",
    "KPI_Andel_Prosent",
    "KPI_Indeks",
    "Merknad",
    "StatusId",
]

# Synonymer er viktig. Her kan vi legge på mer etter hvert.
SYNONYMS: dict[str, list[str]] = {
    "avtale": ["agreement", "contract", "lease", "rental", "supplier_agreement"],
    "avtalenr": ["agreement_no", "contract_no", "contract_id", "agreement_id", "lease_no", "num"],
    "avtalenavn": ["agreement_name", "contract_name", "description", "name"],
    "kontrakt": ["contract", "agreement", "lease"],
    "fra": ["from", "start", "valid_from", "date_from", "start_date"],
    "til": ["to", "end", "valid_to", "date_to", "end_date", "expire"],
    "leverandor": ["supplier", "vendor", "landlord", "party", "company"],
    "leietaker": ["customer", "tenant", "lessee", "org_unit"],
    "lokasjon": ["location", "site", "place", "object"],
    "bygg": ["building", "property", "facility"],
    "adresse": ["address", "addr", "street"],
    "postnr": ["postal", "zip", "post_code"],
    "poststed": ["city", "postal_area"],
    "websak": ["case", "case_ref", "reference", "doc", "document"],
    "oppsigelse": ["termination", "notice", "cancel"],
    "opsjon": ["option", "renewal", "extension"],
    "areal": ["area", "m2", "sqm", "square"],
    "parkering": ["parking", "garage"],
    "renhold": ["cleaning"],
    "kantine": ["canteen", "cafeteria"],
    "energi": ["energy", "electricity", "power"],
    "investering": ["investment", "capex"],
    "leie": ["rent", "lease_amount", "rental_amount"],
    "felleskost": ["common_cost", "service_charge", "on_account"],
    "kpi": ["index", "cpi", "price_index", "regulation"],
    "status": ["state", "status"],
    "prosjekt": ["project"],
}


# =============================================================================
# HJELP
# =============================================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("_")


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def tokenize(value: str) -> set[str]:
    if not value:
        return set()
    raw = re.sub(r"([a-z])([A-Z])", r"\1_\2", value)
    raw = raw.replace("ø", "o").replace("å", "a").replace("æ", "ae")
    tokens = re.split(r"[^A-Za-z0-9]+", raw.lower())
    return {t for t in tokens if t and len(t) >= 2}


def expanded_tokens(name: str) -> set[str]:
    tokens = tokenize(name)
    out = set(tokens)
    for t in list(tokens):
        out.update(SYNONYMS.get(t, []))
    return out


def sqlserver_ident(value: str) -> str:
    return f"[{value.replace(']', ']]')}]"


def oracle_ident(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) + chr(34))}"'


def value_kind(data_type: str) -> str:
    dt = (data_type or "").lower()
    if any(x in dt for x in ["int", "decimal", "numeric", "float", "real", "money", "number"]):
        return "number"
    if any(x in dt for x in ["date", "time", "timestamp"]):
        return "date"
    return "text"


def value_complexity(value: Any) -> float:
    text = norm_text(value)
    if not text:
        return 0.0

    length_score = min(len(text) / 40, 1.0)
    has_alpha = any(c.isalpha() for c in text)
    has_digit = any(c.isdigit() for c in text)
    has_sep = any(not c.isalnum() and not c.isspace() for c in text)
    token_count = len(tokenize(text))
    mix_score = 0.0
    if has_alpha and has_digit:
        mix_score += 0.25
    if has_sep:
        mix_score += 0.20
    if token_count >= 2:
        mix_score += 0.15
    return min((0.60 * length_score) + mix_score, 1.0)


def should_probe_value(value: Any, kind: str) -> bool:
    text = norm_text(value)
    if not text or text.startswith("<"):
        return False
    if kind == "text":
        return len(text) >= 3
    if kind == "number":
        return re.fullmatch(r"-?\d+([.,]\d+)?", text) is not None and text not in {"0", "1"}
    if kind == "date":
        return len(text) >= 8
    return len(text) >= 3


def hash_row(row: dict[str, Any]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def qname(owner: str, table_name: str) -> str:
    return f'"{owner}"."{table_name}"'


# =============================================================================
# SQLITE LOKALT BIBLIOTEK
# =============================================================================

def init_local_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL;")

    con.executescript(
        """
        create table if not exists oracle_tables (
            owner text not null,
            table_name text not null,
            num_rows_est integer,
            counted_rows integer,
            last_analyzed text,
            table_type text,
            scan_status text,
            scanned_at text,
            primary key (owner, table_name)
        );

        create table if not exists oracle_columns (
            owner text not null,
            table_name text not null,
            column_name text not null,
            column_id integer,
            data_type text,
            data_length integer,
            data_precision integer,
            data_scale integer,
            nullable text,
            num_distinct integer,
            density real,
            sample_value text,
            scanned_at text,
            primary key (owner, table_name, column_name)
        );

        create table if not exists oracle_samples (
            owner text not null,
            table_name text not null,
            sample_no integer not null,
            row_hash text not null,
            row_json text not null,
            scanned_at text,
            primary key (owner, table_name, sample_no)
        );

        create table if not exists dwh_columns (
            schema_name text not null,
            table_name text not null,
            column_name text not null,
            data_type text,
            ordinal_position integer,
            sample_values_json text,
            scanned_at text,
            primary key (schema_name, table_name, column_name)
        );

        create table if not exists dwh_value_fingerprints (
            dwh_schema text not null,
            dwh_table text not null,
            dwh_column text not null,
            data_type text,
            value_kind text,
            value_text text not null,
            occurrence_count integer,
            selector text,
            complexity_score real,
            scanned_at text,
            primary key (dwh_schema, dwh_table, dwh_column, value_text, selector)
        );

        create table if not exists oracle_value_hits (
            dwh_schema text not null,
            dwh_table text not null,
            dwh_column text not null,
            dwh_value_text text not null,
            oracle_owner text not null,
            oracle_table text not null,
            oracle_column text not null,
            oracle_data_type text,
            match_kind text,
            hit_count integer,
            sample_value text,
            scanned_at text,
            primary key (
                dwh_schema, dwh_table, dwh_column, dwh_value_text,
                oracle_owner, oracle_table, oracle_column
            )
        );

        create table if not exists mapping_candidates (
            dwh_schema text,
            dwh_table text,
            dwh_column text,
            oracle_owner text,
            oracle_table text,
            oracle_column text,
            name_score real,
            datatype_score real,
            value_overlap_score real,
            total_score real,
            reason text,
            scanned_at text,
            primary key (dwh_schema, dwh_table, dwh_column, oracle_owner, oracle_table, oracle_column)
        );
        """
    )
    con.commit()
    return con


# =============================================================================
# TILKOBLINGER
# =============================================================================

def connect_oracle(owner_default: str = "IFSAPP") -> OracleBaseCls:
    """
    Returnerer OracleBaseCls, ikke rå oracledb-connection.

    OracleBaseCls håndterer:
    - ORACLE_USER / ORACLE_PASSWORD
    - ORACLE_DSN eller ORACLE_HOST/ORACLE_PORT/ORACLE_SERVICE_NAME
    - oracledb / cx_Oracle driver
    - context manager
    """
    return OracleBaseCls(owner_default=owner_default)


def connect_dwh(prefix: str = "") -> SqlServerBaseCls:
    return SqlServerBaseCls(prefix=prefix)


def load_setup_file(path: Path) -> None:
    if not path.exists():
        return

    with path.open("r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


# =============================================================================
# ORACLE-PROFILERING
# =============================================================================

def get_oracle_tables(
    db: OracleBaseCls,
    schemas: list[str],
    name_like: str | None = None,
) -> pd.DataFrame:
    params: dict[str, Any] = {}
    schema_placeholders = []
    for i, s in enumerate(schemas):
        key = f"schema_{i}"
        schema_placeholders.append(f":{key}")
        params[key] = s.upper()

    where = [f"OWNER in ({','.join(schema_placeholders)})"]

    if name_like:
        where.append("upper(TABLE_NAME) like :name_like")
        params["name_like"] = f"%{name_like.upper()}%"

    sql = f"""
        select
            OWNER,
            TABLE_NAME,
            NUM_ROWS,
            LAST_ANALYZED,
            'TABLE' as TABLE_TYPE
        from ALL_TABLES
        where {' and '.join(where)}
          and NESTED = 'NO'
          and SECONDARY = 'N'
        order by OWNER, TABLE_NAME
    """
    return pd.read_sql(sql, db.conn, params=params)


def get_oracle_columns(
    db: OracleBaseCls,
    schemas: list[str],
) -> pd.DataFrame:
    params: dict[str, Any] = {}
    schema_placeholders = []
    for i, s in enumerate(schemas):
        key = f"schema_{i}"
        schema_placeholders.append(f":{key}")
        params[key] = s.upper()

    sql = f"""
        select
            OWNER,
            TABLE_NAME,
            COLUMN_NAME,
            COLUMN_ID,
            DATA_TYPE,
            DATA_LENGTH,
            DATA_PRECISION,
            DATA_SCALE,
            NULLABLE,
            NUM_DISTINCT,
            DENSITY
        from ALL_TAB_COLUMNS
        where OWNER in ({','.join(schema_placeholders)})
        order by OWNER, TABLE_NAME, COLUMN_ID
    """
    return pd.read_sql(sql, db.conn, params=params)


def count_rows_safe(
    db: OracleBaseCls,
    owner: str,
    table_name: str,
    timeout_seconds: int = 20,
) -> int | None:
    """
    Enkel COUNT(*). For veldig store tabeller kan dette være dyrt.
    Førsteversjon: vi prøver, men feiler kontrollert.
    Senere kan vi legge på DBMS_STATS/parallel/sample-estimat.
    """
    sql = f"select count(*) from {qname(owner, table_name)}"
    try:
        start = time.time()
        rows = db.fetchall(sql)
        elapsed = time.time() - start
        if elapsed > timeout_seconds:
            print(f"  ⚠️ COUNT tok {elapsed:.1f}s: {owner}.{table_name}")
        return int(rows[0][0]) if rows else None
    except Exception as e:
        print(f"  ⚠️ Kunne ikke telle {owner}.{table_name}: {str(e)[:120]}")
        return None


def sample_rows_safe(
    db: OracleBaseCls,
    owner: str,
    table_name: str,
    sample_rows: int,
) -> list[dict[str, Any]]:
    sql = f"""
        select *
        from {qname(owner, table_name)}
        fetch first {int(sample_rows)} rows only
    """
    cur = None
    try:
        cur = db.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            item = {}
            for c, v in zip(cols, row):
                if hasattr(v, "read"):
                    item[c] = "<LOB>"
                else:
                    item[c] = v
            rows.append(item)
        return rows
    except Exception as e:
        print(f"  ⚠️ Kunne ikke sample {owner}.{table_name}: {str(e)[:120]}")
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


def profile_oracle(
    db: OracleBaseCls,
    local: sqlite3.Connection,
    schemas: list[str],
    min_rows: int,
    sample_rows: int,
    force_count: bool,
) -> None:
    print("Henter Oracle-tabeller ...")
    tables = get_oracle_tables(db, schemas)
    columns = get_oracle_columns(db, schemas)
    scanned_at = now_iso()

    print(f"Fant {len(tables)} Oracle-tabeller og {len(columns)} kolonner.")

    col_group = {
        (r.OWNER, r.TABLE_NAME): []
        for r in columns.itertuples(index=False)
    }
    for r in columns.itertuples(index=False):
        col_group.setdefault((r.OWNER, r.TABLE_NAME), []).append(r)

    for r in tables.itertuples(index=False):
        owner = r.OWNER
        table_name = r.TABLE_NAME
        est = int(r.NUM_ROWS) if pd.notna(r.NUM_ROWS) else None

        should_count = force_count or est is None or est <= max(min_rows * 5, 1000)
        counted = count_rows_safe(db, owner, table_name) if should_count else None

        row_basis = counted if counted is not None else est
        if row_basis is None:
            status = "unknown_rows"
        elif row_basis >= min_rows:
            status = "has_data"
        else:
            status = "too_few_rows"

        local.execute(
            """
            insert or replace into oracle_tables
            (owner, table_name, num_rows_est, counted_rows, last_analyzed, table_type, scan_status, scanned_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                table_name,
                est,
                counted,
                str(r.LAST_ANALYZED) if pd.notna(r.LAST_ANALYZED) else None,
                r.TABLE_TYPE,
                status,
                scanned_at,
            ),
        )

        for c in col_group.get((owner, table_name), []):
            local.execute(
                """
                insert or replace into oracle_columns
                (owner, table_name, column_name, column_id, data_type, data_length,
                 data_precision, data_scale, nullable, num_distinct, density, sample_value, scanned_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    table_name,
                    c.COLUMN_NAME,
                    int(c.COLUMN_ID) if pd.notna(c.COLUMN_ID) else None,
                    c.DATA_TYPE,
                    int(c.DATA_LENGTH) if pd.notna(c.DATA_LENGTH) else None,
                    int(c.DATA_PRECISION) if pd.notna(c.DATA_PRECISION) else None,
                    int(c.DATA_SCALE) if pd.notna(c.DATA_SCALE) else None,
                    c.NULLABLE,
                    int(c.NUM_DISTINCT) if pd.notna(c.NUM_DISTINCT) else None,
                    float(c.DENSITY) if pd.notna(c.DENSITY) else None,
                    None,
                    scanned_at,
                ),
            )

        local.commit()

        if status == "has_data":
            print(f"Sampler {owner}.{table_name} rows={row_basis}")
            samples = sample_rows_safe(db, owner, table_name, sample_rows)
            for i, row in enumerate(samples, start=1):
                local.execute(
                    """
                    insert or replace into oracle_samples
                    (owner, table_name, sample_no, row_hash, row_json, scanned_at)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner,
                        table_name,
                        i,
                        hash_row(row),
                        json.dumps(row, ensure_ascii=False, default=str),
                        scanned_at,
                    ),
                )

            # legg første ikke-null sampleverdi per kolonne inn i oracle_columns
            sample_value_by_col: dict[str, str] = {}
            for row in samples:
                for col, val in row.items():
                    if col not in sample_value_by_col and val not in (None, ""):
                        sample_value_by_col[col] = str(val)[:500]

            for col, val in sample_value_by_col.items():
                local.execute(
                    """
                    update oracle_columns
                    set sample_value = ?
                    where owner = ? and table_name = ? and column_name = ?
                    """,
                    (val, owner, table_name, col),
                )

            local.commit()


# =============================================================================
# DWH-PROFILERING
# =============================================================================

def discover_dwh_tables(
    dwh: Any,
    dwh_schema: str,
    object_prefixes: list[str],
) -> list[str]:
    if not object_prefixes:
        return []

    like_sql = " or ".join("TABLE_NAME like ?" for _ in object_prefixes)
    params = [dwh_schema] + [f"{prefix.replace('_', '[_]')}%" for prefix in object_prefixes]
    sql = f"""
        select TABLE_NAME
        from INFORMATION_SCHEMA.TABLES
        where TABLE_SCHEMA = ?
          and ({like_sql})
        order by TABLE_NAME
    """
    df = pd.read_sql(sql, dwh, params=params)
    return [str(v) for v in df["TABLE_NAME"].tolist()]


def collect_dwh_fingerprints_for_column(
    dwh: Any,
    schema_name: str,
    table_name: str,
    column_name: str,
    data_type: str,
    limit_per_selector: int,
) -> list[dict[str, Any]]:
    schema_sql = sqlserver_ident(schema_name)
    table_sql = sqlserver_ident(table_name)
    column_sql = sqlserver_ident(column_name)
    value_expr = f"cast({column_sql} as nvarchar(4000))"
    base_from = f"from {schema_sql}.{table_sql} where {column_sql} is not null"

    selectors = [
        (
            "frequent",
            f"""
            select top ({int(limit_per_selector)})
                {value_expr} as value_text,
                count(*) as occurrence_count
            {base_from}
            group by {value_expr}
            order by count(*) desc
            """,
        ),
        (
            "rare_complex",
            f"""
            select top ({int(limit_per_selector)})
                {value_expr} as value_text,
                count(*) as occurrence_count
            {base_from}
            group by {value_expr}
            order by count(*) asc, len({value_expr}) desc
            """,
        ),
    ]

    kind = value_kind(data_type)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for selector, sql in selectors:
        try:
            df = pd.read_sql(sql, dwh)
        except Exception as e:
            by_key[(selector, f"<fingerprint_error: {str(e)[:120]}>")] = {
                "value_text": f"<fingerprint_error: {str(e)[:120]}>",
                "occurrence_count": None,
                "selector": selector,
                "complexity_score": 0.0,
                "value_kind": kind,
            }
            continue

        for row in df.itertuples(index=False):
            value_text = str(row.value_text)
            if not should_probe_value(value_text, kind):
                continue
            by_key[(selector, norm_text(value_text))] = {
                "value_text": value_text[:4000],
                "occurrence_count": int(row.occurrence_count),
                "selector": selector,
                "complexity_score": value_complexity(value_text),
                "value_kind": kind,
            }

    return list(by_key.values())


def load_dwh_columns(
    dwh: Any,
    local: sqlite3.Connection,
    dwh_schema: str,
    dwh_tables: list[str],
    sample_values_per_column: int = 20,
    fingerprint_values_per_column: int = 8,
) -> None:
    scanned_at = now_iso()
    if not dwh_tables:
        print("Ingen DWH-tabeller funnet/oppgitt.")
        return

    table_filter = ",".join("?" for _ in dwh_tables)

    sql_cols = f"""
        select
            TABLE_SCHEMA,
            TABLE_NAME,
            COLUMN_NAME,
            DATA_TYPE,
            ORDINAL_POSITION
        from INFORMATION_SCHEMA.COLUMNS
        where TABLE_SCHEMA = ?
          and TABLE_NAME in ({table_filter})
        order by TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
    """

    params = [dwh_schema] + dwh_tables
    cols = pd.read_sql(sql_cols, dwh, params=params)

    for r in cols.itertuples(index=False):
        sample_values = []
        fingerprints = collect_dwh_fingerprints_for_column(
            dwh=dwh,
            schema_name=r.TABLE_SCHEMA,
            table_name=r.TABLE_NAME,
            column_name=r.COLUMN_NAME,
            data_type=r.DATA_TYPE,
            limit_per_selector=max(sample_values_per_column, fingerprint_values_per_column),
        )
        sample_values = [
            fp["value_text"]
            for fp in sorted(
                fingerprints,
                key=lambda item: (
                    item["selector"] != "frequent",
                    -(item["occurrence_count"] or 0),
                    -item["complexity_score"],
                ),
            )[:sample_values_per_column]
        ]

        local.execute(
            """
            insert or replace into dwh_columns
            (schema_name, table_name, column_name, data_type, ordinal_position, sample_values_json, scanned_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.TABLE_SCHEMA,
                r.TABLE_NAME,
                r.COLUMN_NAME,
                r.DATA_TYPE,
                int(r.ORDINAL_POSITION),
                json.dumps(sample_values, ensure_ascii=False),
                scanned_at,
            ),
        )

        for fp in sorted(
            fingerprints,
            key=lambda item: (
                item["selector"] != "rare_complex",
                -item["complexity_score"],
                item["occurrence_count"] or 0,
            ),
        )[: fingerprint_values_per_column * 2]:
            local.execute(
                """
                insert or replace into dwh_value_fingerprints
                (dwh_schema, dwh_table, dwh_column, data_type, value_kind,
                 value_text, occurrence_count, selector, complexity_score, scanned_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.TABLE_SCHEMA,
                    r.TABLE_NAME,
                    r.COLUMN_NAME,
                    r.DATA_TYPE,
                    fp["value_kind"],
                    fp["value_text"],
                    fp["occurrence_count"],
                    fp["selector"],
                    round(float(fp["complexity_score"]), 4),
                    scanned_at,
                ),
            )
    local.commit()


# =============================================================================
# VERDIBASERT IFS-SØK
# =============================================================================

def oracle_kind(data_type: str) -> str:
    dt = (data_type or "").lower()
    if any(x in dt for x in ["number", "float", "binary_double", "binary_float"]):
        return "number"
    if any(x in dt for x in ["date", "timestamp"]):
        return "date"
    if any(x in dt for x in ["char", "varchar", "nvarchar"]):
        return "text"
    return "other"


def parse_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(norm_text(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def date_probe(value: str) -> str | None:
    text = norm_text(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    match = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    if match:
        day, month, year = match.group(0).split(".")
        return f"{year}-{month}-{day}"
    return None


def build_oracle_value_predicate(
    column_name: str,
    oracle_data_type: str,
    probe_kind: str,
    probe_value: str,
) -> tuple[str, dict[str, Any], str] | None:
    col = oracle_ident(column_name)
    okind = oracle_kind(oracle_data_type)

    if probe_kind == "text" and okind == "text":
        return f"{col} = :probe_value", {"probe_value": probe_value}, "text_exact"

    if probe_kind == "number" and okind == "number":
        number_value = parse_decimal(probe_value)
        if number_value is None:
            return None
        return f"{col} = :probe_value", {"probe_value": number_value}, "number_exact"

    if probe_kind == "date" and okind == "date":
        probe_date = date_probe(probe_value)
        if probe_date is None:
            return None
        return (
            f"to_char({col}, 'YYYY-MM-DD') = :probe_value",
            {"probe_value": probe_date},
            "date_exact",
        )

    return None


def search_oracle_value_hits(
    db: OracleBaseCls,
    local: sqlite3.Connection,
    max_oracle_columns: int,
    probes_per_dwh_column: int,
    max_hits_per_probe: int,
) -> None:
    scanned_at = now_iso()
    fingerprints = pd.read_sql(
        """
        select *
        from dwh_value_fingerprints
        where value_text not like '<%'
        order by dwh_table, dwh_column, complexity_score desc, occurrence_count asc
        """,
        local,
    )
    if fingerprints.empty:
        print("Ingen DWH-fingeravtrykk å søke etter i IFS.")
        return

    oracle_cols = pd.read_sql(
        """
        select c.*, t.scan_status, coalesce(t.counted_rows, t.num_rows_est, 0) as row_basis
        from oracle_columns c
        join oracle_tables t
          on t.owner = c.owner
         and t.table_name = c.table_name
        where t.scan_status in ('has_data', 'unknown_rows')
        order by
            case t.scan_status when 'has_data' then 0 else 1 end,
            coalesce(c.num_distinct, 999999999),
            row_basis desc
        """,
        local,
    )
    if max_oracle_columns > 0:
        oracle_cols = oracle_cols.head(max_oracle_columns)

    compatible_cols = [
        row
        for row in oracle_cols.itertuples(index=False)
        if oracle_kind(row.data_type) in {"text", "number", "date"}
    ]

    print(
        "Søker etter DWH-verdi-fingeravtrykk i IFS "
        f"({len(fingerprints)} verdier, {len(compatible_cols)} Oracle-kolonner) ..."
    )

    grouped = fingerprints.groupby(["dwh_schema", "dwh_table", "dwh_column"], sort=False)
    for (_schema, _table, _column), group in grouped:
        probes = group.head(probes_per_dwh_column)
        for probe in probes.itertuples(index=False):
            for o in compatible_cols:
                predicate = build_oracle_value_predicate(
                    column_name=o.column_name,
                    oracle_data_type=o.data_type,
                    probe_kind=probe.value_kind,
                    probe_value=probe.value_text,
                )
                if predicate is None:
                    continue

                where_sql, params, match_kind = predicate
                params = {**params, "hit_limit": int(max_hits_per_probe) + 1}
                sql_count = f"""
                    select count(*)
                    from (
                        select 1
                        from {qname(o.owner, o.table_name)}
                        where {where_sql}
                          and rownum <= :hit_limit
                    )
                """

                try:
                    rows = db.fetchall(sql_count, params)
                    hit_count = int(rows[0][0]) if rows else 0
                except Exception:
                    continue

                if hit_count <= 0:
                    continue

                sample_value = probe.value_text
                local.execute(
                    """
                    insert or replace into oracle_value_hits
                    (dwh_schema, dwh_table, dwh_column, dwh_value_text,
                     oracle_owner, oracle_table, oracle_column, oracle_data_type,
                     match_kind, hit_count, sample_value, scanned_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        probe.dwh_schema,
                        probe.dwh_table,
                        probe.dwh_column,
                        probe.value_text,
                        o.owner,
                        o.table_name,
                        o.column_name,
                        o.data_type,
                        match_kind,
                        hit_count,
                        sample_value,
                        scanned_at,
                    ),
                )
        local.commit()


# =============================================================================
# MATCHING / SCORING
# =============================================================================

def datatype_score(dwh_type: str, oracle_type: str) -> float:
    d = (dwh_type or "").lower()
    o = (oracle_type or "").lower()

    text_d = any(x in d for x in ["char", "text", "varchar", "nvarchar"])
    text_o = any(x in o for x in ["char", "clob", "varchar"])

    num_d = any(x in d for x in ["int", "decimal", "numeric", "float", "real", "money"])
    num_o = any(x in o for x in ["number", "float", "binary_double", "binary_float"])

    date_d = any(x in d for x in ["date", "time"])
    date_o = any(x in o for x in ["date", "timestamp"])

    if text_d and text_o:
        return 1.0
    if num_d and num_o:
        return 1.0
    if date_d and date_o:
        return 1.0
    if text_d and num_o:
        return 0.25
    if num_d and text_o:
        return 0.25
    return 0.4


def name_score(dwh_column: str, oracle_table: str, oracle_column: str) -> tuple[float, str]:
    dwh_tokens = expanded_tokens(dwh_column)
    ora_col_tokens = expanded_tokens(oracle_column)
    ora_table_tokens = expanded_tokens(oracle_table)

    col_overlap = dwh_tokens & ora_col_tokens
    table_overlap = dwh_tokens & ora_table_tokens

    if not dwh_tokens:
        return 0.0, "no_tokens"

    score = 0.0
    if col_overlap:
        score += 0.75 * (len(col_overlap) / max(len(dwh_tokens), 1))
    if table_overlap:
        score += 0.25 * (len(table_overlap) / max(len(dwh_tokens), 1))

    # Direkte delstreng gir ekstra score
    dn = "".join(sorted(dwh_tokens))
    on = "".join(sorted(ora_col_tokens))
    if dwh_column.lower() in oracle_column.lower() or oracle_column.lower() in dwh_column.lower():
        score += 0.25

    return min(score, 1.0), f"col_overlap={sorted(col_overlap)} table_overlap={sorted(table_overlap)}"


def value_overlap_score(dwh_samples: list[str], oracle_sample_value: str | None) -> float:
    if not dwh_samples or not oracle_sample_value:
        return 0.0

    ov = norm_text(oracle_sample_value)
    if not ov:
        return 0.0

    # Ikke prøv for generiske korte verdier
    if len(ov) < 3:
        return 0.0

    best = 0.0
    for dv in dwh_samples:
        ndv = norm_text(dv)
        if not ndv or len(ndv) < 3:
            continue

        if ndv == ov:
            best = max(best, 1.0)
        elif ndv in ov or ov in ndv:
            best = max(best, 0.7)
        else:
            d_tokens = tokenize(ndv)
            o_tokens = tokenize(ov)
            if d_tokens and o_tokens:
                overlap = len(d_tokens & o_tokens) / max(len(d_tokens | o_tokens), 1)
                best = max(best, min(overlap, 0.5))
    return best


def build_mapping_candidates(local: sqlite3.Connection, min_total_score: float = 0.25) -> None:
    scanned_at = now_iso()

    dwh_cols = pd.read_sql("select * from dwh_columns", local)
    ora_cols = pd.read_sql(
        """
        select c.*, t.num_rows_est, t.counted_rows, t.scan_status
        from oracle_columns c
        join oracle_tables t
          on t.owner = c.owner
         and t.table_name = c.table_name
        where t.scan_status in ('has_data', 'unknown_rows')
        """,
        local,
    )
    value_hits = pd.read_sql(
        """
        select
            dwh_schema,
            dwh_table,
            dwh_column,
            oracle_owner,
            oracle_table,
            oracle_column,
            count(*) as matched_probe_values,
            sum(hit_count) as total_hit_count
        from oracle_value_hits
        group by
            dwh_schema, dwh_table, dwh_column,
            oracle_owner, oracle_table, oracle_column
        """,
        local,
    )
    hit_lookup = {
        (
            r.dwh_schema,
            r.dwh_table,
            r.dwh_column,
            r.oracle_owner,
            r.oracle_table,
            r.oracle_column,
        ): (int(r.matched_probe_values), int(r.total_hit_count or 0))
        for r in value_hits.itertuples(index=False)
    }

    print(f"Scorer {len(dwh_cols)} DWH-kolonner mot {len(ora_cols)} Oracle-kolonner ...")

    for d in dwh_cols.itertuples(index=False):
        try:
            dwh_samples = json.loads(d.sample_values_json or "[]")
        except Exception:
            dwh_samples = []

        for o in ora_cols.itertuples(index=False):
            ns, reason = name_score(d.column_name, o.table_name, o.column_name)
            if ns < 0.05:
                # billig filtrering: ignorer åpenbart irrelevante par
                continue

            ds = datatype_score(d.data_type, o.data_type)
            vs = value_overlap_score(dwh_samples, o.sample_value)

            total = (0.55 * ns) + (0.20 * ds) + (0.25 * vs)

            if total >= min_total_score:
                local.execute(
                    """
                    insert or replace into mapping_candidates
                    (dwh_schema, dwh_table, dwh_column,
                     oracle_owner, oracle_table, oracle_column,
                     name_score, datatype_score, value_overlap_score,
                     total_score, reason, scanned_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d.schema_name,
                        d.table_name,
                        d.column_name,
                        o.owner,
                        o.table_name,
                        o.column_name,
                        round(ns, 4),
                        round(ds, 4),
                        round(vs, 4),
                        round(total, 4),
                        reason,
                        scanned_at,
                    ),
                )
    local.commit()


def build_mapping_candidates_with_value_hits(
    local: sqlite3.Connection,
    min_total_score: float = 0.25,
) -> None:
    scanned_at = now_iso()

    dwh_cols = pd.read_sql("select * from dwh_columns", local)
    ora_cols = pd.read_sql(
        """
        select c.*, t.num_rows_est, t.counted_rows, t.scan_status
        from oracle_columns c
        join oracle_tables t
          on t.owner = c.owner
         and t.table_name = c.table_name
        where t.scan_status in ('has_data', 'unknown_rows')
        """,
        local,
    )
    value_hits = pd.read_sql(
        """
        select
            dwh_schema,
            dwh_table,
            dwh_column,
            oracle_owner,
            oracle_table,
            oracle_column,
            count(*) as matched_probe_values,
            sum(hit_count) as total_hit_count
        from oracle_value_hits
        group by
            dwh_schema, dwh_table, dwh_column,
            oracle_owner, oracle_table, oracle_column
        """,
        local,
    )
    hit_lookup = {
        (
            r.dwh_schema,
            r.dwh_table,
            r.dwh_column,
            r.oracle_owner,
            r.oracle_table,
            r.oracle_column,
        ): (int(r.matched_probe_values), int(r.total_hit_count or 0))
        for r in value_hits.itertuples(index=False)
    }

    print(f"Scorer {len(dwh_cols)} DWH-kolonner mot {len(ora_cols)} Oracle-kolonner ...")

    for d in dwh_cols.itertuples(index=False):
        try:
            dwh_samples = json.loads(d.sample_values_json or "[]")
        except Exception:
            dwh_samples = []

        for o in ora_cols.itertuples(index=False):
            ns, reason = name_score(d.column_name, o.table_name, o.column_name)
            hit_key = (
                d.schema_name,
                d.table_name,
                d.column_name,
                o.owner,
                o.table_name,
                o.column_name,
            )
            matched_probe_values, total_hit_count = hit_lookup.get(hit_key, (0, 0))
            hit_score = min(1.0, matched_probe_values / 3) if matched_probe_values else 0.0

            if ns < 0.05 and hit_score <= 0:
                continue

            ds = datatype_score(d.data_type, o.data_type)
            vs = max(value_overlap_score(dwh_samples, o.sample_value), hit_score)
            total = (0.45 * ns) + (0.15 * ds) + (0.40 * vs)

            if matched_probe_values:
                reason = (
                    f"{reason} value_hits={matched_probe_values} "
                    f"limited_hit_count={total_hit_count}"
                )

            if total >= min_total_score:
                local.execute(
                    """
                    insert or replace into mapping_candidates
                    (dwh_schema, dwh_table, dwh_column,
                     oracle_owner, oracle_table, oracle_column,
                     name_score, datatype_score, value_overlap_score,
                     total_score, reason, scanned_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d.schema_name,
                        d.table_name,
                        d.column_name,
                        o.owner,
                        o.table_name,
                        o.column_name,
                        round(ns, 4),
                        round(ds, 4),
                        round(vs, 4),
                        round(total, 4),
                        reason,
                        scanned_at,
                    ),
                )
    local.commit()


# =============================================================================
# RAPPORTER
# =============================================================================

def write_reports(local: sqlite3.Connection) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    reports = {
        "01_oracle_tables.csv": """
            select *
            from oracle_tables
            order by
                case scan_status when 'has_data' then 0 when 'unknown_rows' then 1 else 2 end,
                coalesce(counted_rows, num_rows_est, 0) desc,
                owner,
                table_name
        """,
        "02_oracle_columns.csv": """
            select *
            from oracle_columns
            order by owner, table_name, column_id
        """,
        "03_dwh_columns.csv": """
            select *
            from dwh_columns
            order by schema_name, table_name, ordinal_position
        """,
        "04_dwh_value_fingerprints.csv": """
            select *
            from dwh_value_fingerprints
            order by dwh_table, dwh_column, complexity_score desc, occurrence_count asc
        """,
        "05_oracle_value_hits.csv": """
            select *
            from oracle_value_hits
            order by dwh_table, dwh_column, dwh_value_text, hit_count desc
        """,
        "06_mapping_candidates.csv": """
            select *
            from mapping_candidates
            order by dwh_table, dwh_column, total_score desc
        """,
        "07_best_mapping_per_dwh_column.csv": """
            select *
            from (
                select
                    *,
                    row_number() over (
                        partition by dwh_schema, dwh_table, dwh_column
                        order by total_score desc
                    ) as rn
                from mapping_candidates
            )
            where rn <= 10
            order by dwh_table, dwh_column, rn
        """,
        "08_tables_with_many_candidate_hits.csv": """
            select
                oracle_owner,
                oracle_table,
                count(*) as candidate_hits,
                count(distinct dwh_column) as distinct_dwh_columns,
                round(avg(total_score), 4) as avg_score,
                round(max(total_score), 4) as max_score
            from mapping_candidates
            group by oracle_owner, oracle_table
            order by distinct_dwh_columns desc, avg_score desc
        """,
    }

    for filename, sql in reports.items():
        df = pd.read_sql(sql, local)
        path = REPORT_DIR / filename
        df.to_csv(path, index=False, sep=";", encoding="utf-8-sig")
        print(f"Skrev {path} ({len(df)} rader)")

    # Skriv samples per tabell som JSONL
    samples = pd.read_sql(
        """
        select owner, table_name, sample_no, row_json
        from oracle_samples
        order by owner, table_name, sample_no
        """,
        local,
    )

    for (owner, table_name), group in samples.groupby(["owner", "table_name"]):
        path = SAMPLE_DIR / f"{safe_name(owner)}.{safe_name(table_name)}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in group.itertuples(index=False):
                f.write(row.row_json + "\n")


# =============================================================================
# MAIN
# =============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--schemas", nargs="+", default=DEFAULT_SCHEMAS)
    p.add_argument("--min-rows", type=int, default=1)
    p.add_argument("--sample-rows", type=int, default=10)
    p.add_argument("--force-count", action="store_true")
    p.add_argument("--skip-oracle-profile", action="store_true")
    p.add_argument("--skip-dwh-profile", action="store_true")
    p.add_argument(
        "--dwh-prefix",
        default=os.getenv("DWH_SQLSERVER_PREFIX", ""),
        help="Prefix for SQL Server config/secrets. Kan settes i setup.txt.",
    )
    p.add_argument("--dwh-schema", default=os.getenv("DWH_SCHEMA", "mart_m"))
    p.add_argument(
        "--dwh-tables",
        nargs="+",
        default=None,
        help="Mart-objekter som skal profileres. Hvis utelatt oppdages dim_/fact_ automatisk.",
    )
    p.add_argument(
        "--dwh-object-prefixes",
        nargs="+",
        default=["dim_", "fact_"],
        help="Prefikser for automatisk oppdagelse av mart-objekter.",
    )
    p.add_argument("--fingerprint-values-per-column", type=int, default=8)
    p.add_argument("--skip-value-search", action="store_true")
    p.add_argument("--oracle-value-search-limit-columns", type=int, default=2000)
    p.add_argument("--probes-per-dwh-column", type=int, default=5)
    p.add_argument(
        "--max-hits-per-probe",
        type=int,
        default=100,
        help="Teller treff opp til denne grensen per DWH-verdi/IFS-kolonne.",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv(dotenv_path=THIS_FILE.parent / ".env")
    load_setup_file(THIS_FILE.parent / "setup.txt")
    args = parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    local = init_local_db(DB_PATH)

    if not args.skip_oracle_profile:
        print("Kobler til Oracle/IFS via OracleBaseCls ...")
        with connect_oracle(owner_default=args.schemas[0] if args.schemas else "IFSAPP") as ora_db:
            print(f"Oracle driver: {ora_db.driver_name}")
            profile_oracle(
                db=ora_db,
                local=local,
                schemas=args.schemas,
                min_rows=args.min_rows,
                sample_rows=args.sample_rows,
                force_count=args.force_count,
            )

    if not args.skip_dwh_profile:
        print(f"Kobler til DWH SQL Server (prefix={args.dwh_prefix}) ...")
        with connect_dwh(prefix=args.dwh_prefix) as dwh:
            dwh_tables = args.dwh_tables
            if dwh_tables is None:
                dwh_tables = discover_dwh_tables(
                    dwh=dwh.conn,
                    dwh_schema=args.dwh_schema,
                    object_prefixes=args.dwh_object_prefixes,
                )
                print(
                    f"Fant {len(dwh_tables)} DWH-objekter i {args.dwh_schema} "
                    f"med prefiks {args.dwh_object_prefixes}."
                )

            load_dwh_columns(
                dwh=dwh.conn,
                local=local,
                dwh_schema=args.dwh_schema,
                dwh_tables=dwh_tables,
                fingerprint_values_per_column=args.fingerprint_values_per_column,
            )

    if not args.skip_value_search:
        print("Kobler til Oracle/IFS for verdibasert søk ...")
        with connect_oracle(owner_default=args.schemas[0] if args.schemas else "IFSAPP") as ora_db:
            search_oracle_value_hits(
                db=ora_db,
                local=local,
                max_oracle_columns=args.oracle_value_search_limit_columns,
                probes_per_dwh_column=args.probes_per_dwh_column,
                max_hits_per_probe=args.max_hits_per_probe,
            )

    build_mapping_candidates_with_value_hits(local)
    write_reports(local)

    print("")
    print("Ferdig.")
    print(f"Lokal database: {DB_PATH}")
    print(f"Rapporter:       {REPORT_DIR}")
    print(f"Samples:         {SAMPLE_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
