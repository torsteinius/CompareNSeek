# Compare and Seek

`CompareAndSeek.py` bygger et lokalt grunnlag for å finne sammenhenger mellom mart-objekter i datavarehuset og IFS/Oracle-tabeller.

## Hva scriptet gjør

- Leser fakta- og dimensjonsobjekter fra mart, som standard objekter med prefiks `dim_` og `fact_`.
- Henter kolonner, datatyper og representative verdier fra DWH.
- Lager verdifingeravtrykk per DWH-kolonne, inkludert hyppige verdier og sjeldne/komplekse verdier.
- Profilerer IFS/Oracle-tabeller og kolonner.
- Søker etter DWH-verdier i kompatible IFS/Oracle-kolonner.
- Lager CSV-rapporter med kandidater og konkrete verdifunn.

## Lokal konfigurasjon

Private verdier legges i `setup.txt`. Alle `.txt`-filer ignoreres av git.

Eksempel på format:

```ini
DWH_SQLSERVER_PREFIX=
DWH_SCHEMA=mart_m

MSSQL_SERVER=
MSSQL_DATABASE=
MSSQL_DEFAULT_SCHEMA=
MSSQL_TRUSTED_CONNECTION=yes
MSSQL_ENCRYPT=yes
MSSQL_TRUST_SERVER_CERTIFICATE=yes
```

Hvis du bruker prefiks i `DivClasses.SQLServerBase`, legg prefikset i `DWH_SQLSERVER_PREFIX` og bruk prefiks på SQL Server-verdiene:

```ini
DWH_SQLSERVER_PREFIX=MYDWH_
DWH_SCHEMA=mart_m

MYDWH_MSSQL_SERVER=
MYDWH_MSSQL_DATABASE=
MYDWH_MSSQL_DEFAULT_SCHEMA=
MYDWH_MSSQL_TRUSTED_CONNECTION=yes
```

## Kjøring

```powershell
python CompareAndSeek.py
```

Vanlige valg:

```powershell
python CompareAndSeek.py --dwh-schema mart_m
python CompareAndSeek.py --dwh-tables dim_avtale fact_areal
python CompareAndSeek.py --skip-value-search
```

## Output

Rapporter skrives til `ifs_profile_output/reports/`.

Viktige rapporter:

- `04_dwh_value_fingerprints.csv`
- `05_oracle_value_hits.csv`
- `06_mapping_candidates.csv`
- `07_best_mapping_per_dwh_column.csv`
