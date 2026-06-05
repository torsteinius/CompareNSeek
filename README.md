# Compare and Seek

`CompareAndSeek.py` hjelper oss å finne hvilke kolonner i IFS/Oracle som sannsynligvis tilsvarer kolonner vi allerede har i datavarehuset.

Scriptet er ment å være generelt. Det skal ikke ha en ferdigprogrammert liste over forretningsfelt. DWH-connectionen er utgangspunktet: scriptet spør datavarehuset hvilke tabeller og kolonner som finnes, og bruker disse som søkegrunnlag mot IFS/Oracle.

Kort sagt:

1. Scriptet leser kolonnene i DWH/mart.
2. Det leser en valgfri `suspects.txt` med IFS-tabeller vi tror kan være relevante.
3. Det profilerer og søker først i suspects-tabellene.
4. Deretter går det videre til andre IFS/Oracle-tabeller med nok rader.
5. Det sammenligner navn, datatyper og faktiske verdier.
6. Det skriver CSV-rapporter som viser beste IFS-kandidat per DWH-kolonne.
7. `streamlit_app.py` kan brukes til å importere rapportene, vurdere kandidater, markere hull og eksportere bekreftet mapping.

Målet er ikke å automatisk bevise fasiten, men å lage en god arbeidsliste for migrering og kildekartlegging. Streamlit-appen er kontrollpanelet der arbeidslisten kan bearbeides over tid.

## Rask start

Installer avhengigheter:

```powershell
pip install -r requirements.txt
```

Kjør profileringsscriptet:

```powershell
python CompareAndSeek.py
```

Start kontrollpanelet:

```powershell
streamlit run streamlit_app.py
```

Streamlit-appen oppretter automatisk en lokal SQLite-database i `data/compare_seek.sqlite` første gang den startes.

## Streamlit-kontrollpanel

`streamlit_app.py` er et lokalt arbeidsverktøy for å holde styr på migreringsmappingen etter at CompareNSeek har laget kandidater.

Appen har sider for:

- **Dashboard**: viser totalt antall DWH-felter, bekreftede mappinger, kandidater, fagavklaringer, hull og ukjente felter.
- **Import**: importerer target fields, source fields og field matches fra CSV.
- **Table Explorer**: viser status per felt i valgt DWH-tabell.
- **Field Detail**: viser én DWH-kolonne, kandidatene fra ny kilde og handlinger for bekreftelse, avvisning og kommentarer.
- **Gap List**: viser felter som fortsatt mangler kandidat eller trenger vurdering.
- **Source Explorer**: lar deg søke i scannede IFS/Oracle-kolonner og legge inn manuelle kandidater.
- **Export**: eksporterer bekreftet eller delvis ferdig mapping til CSV.

Statusene i appen er bevisst arbeidsorienterte:

- `unknown`: feltet er importert, men ikke analysert
- `not_analyzed`: feltet er kjent, men matching er ikke kjørt
- `candidates_found`: feltet har minst én kandidat
- `needs_review`: feltet må vurderes av fagperson eller teknisk ansvarlig
- `confirmed`: kandidat er bekreftet som riktig mapping
- `no_candidates`: ingen god kandidat er funnet
- `not_in_new_source`: feltet finnes ikke i ny kilde
- `deprecated`: feltet skal ikke videreføres

Nye importer skal ikke overskrive manuelle beslutninger. Hvis en kandidat allerede er `confirmed` eller `rejected`, beholdes den vurderingen når nye CSV-er importeres.

### Anbefalt importflyt

1. Importer target fields fra `03_dwh_columns.csv`.
2. Importer source fields fra `02_oracle_columns.csv`.
3. Importer kandidater fra `07_best_mapping_per_dwh_column.csv` for flere kandidater per DWH-felt.
4. Importer eventuelt `09_dwh_column_mapping_summary.csv` hvis du vil starte med beste kandidat per felt.
5. Bruk `Field Detail` til å bekrefte, avvise, kommentere eller markere hull.
6. Eksporter ferdig mapping fra `Export`.

Importen tåler også CompareAndSeek-navn som `dwh_table`, `dwh_column`, `oracle_owner`, `oracle_table`, `oracle_column`, `datatype_score`, `value_overlap_score` og `reason`.

## Hva scriptet gjør

Scriptet bygger først et lokalt SQLite-bibliotek i `ifs_profile_output/ifs_source_profile.sqlite`. Det gjør at dyre databaseoppslag kan gjenbrukes mellom rapportene.

Flyten er:

1. **Finn DWH-objekter**

   Som standard leter scriptet i DWH-skjemaet `mart_m` etter objekter som starter med `dim_` og `fact_`.

   Eksempel: `dim_avtale`, `dim_lokasjon`, `fact_areal`.

   Dette er den delen som bestemmer hvilke DWH-kolonner vi prøver å finne kilder for. Det ligger ikke en hardkodet liste med DWH-felt i scriptet.

2. **Les DWH-kolonner**

   For hver DWH-tabell hentes kolonnenavn, datatype og representative verdier.

   Representative verdier deles i to typer:

   - hyppige verdier, som ofte viser koder/statusfelt
   - sjeldne eller komplekse verdier, som ofte er bedre til å finne eksakt kildekolonne

3. **Les suspects-listen**

   Hvis `suspects.txt` finnes, leses den som en prioritert liste over IFS/Oracle-tabeller som skal undersøkes først.

   Dette er en bevisst "jukselapp": den sier ikke at tabellene er fasit, men den hjelper scriptet å lete i de mest sannsynlige tabellene før det bruker tid på resten av IFS.

4. **Profiler IFS/Oracle**

   Scriptet leser Oracle-tabeller og kolonner fra valgte skjemaer, som standard `IFSAPP`.

   Det lagrer blant annet:

   - tabellnavn
   - kolonnenavn
   - datatype
   - estimert eller talt radantall
   - eksempelverdier fra tabeller med data

   Tabellene behandles i denne rekkefølgen:

   1. tabeller fra `suspects.txt`
   2. andre tabeller som har minst `--min-rows` rader
   3. tabeller uten sikkert radantall, hvis de ellers er relevante

5. **Søk etter faktiske DWH-verdier i IFS**

   For hver DWH-kolonne tar scriptet noen utvalgte verdier og søker etter dem i kompatible Oracle-kolonner.

   Verdiene velges som fingerprints. Et godt fingerprint er en verdi som skiller seg ut nok til at den er lett å lete etter, men ikke så generell at den finnes overalt.

   Typiske gode fingerprints:

   - tekstverdier med litt lengde eller flere ord
   - tallverdier som ikke er `0` eller `1`
   - datoer som ser reelle og spesifikke ut
   - sjeldne verdier i DWH-kolonnen

   Typiske svake fingerprints:

   - korte koder som finnes i mange tabeller
   - generiske statusverdier
   - blanke/null-lignende verdier
   - vanlige tall som `0`, `1` eller korte løpenumre uten kontekst

   Søket er progressivt:

   1. Scriptet starter med den mest særpregede fingerprint-verdien per DWH-kolonne.
   2. Den første verdien søkes bredt i kompatible IFS/Oracle-kolonner.
   3. Hvis den gir treff, søkes neste fingerprint bare i kolonnene som allerede traff.
   4. Slik fortsetter det opp til `--probes-per-dwh-column`, som typisk er `5`.
   5. Hvis et bekreftelsestrinn ikke gir treff, stopper søket for den DWH-kolonnen.

   Dette gjør at vi ikke bruker fem verdier blindt mot hele IFS. Første fingerprint finner kandidater; neste fingerprints forsøker å bekrefte dem.

   Datatype styrer hvor verdier kan søkes:

   - tekstverdier søkes i tekstkolonner
   - tallverdier søkes i tallkolonner
   - datoverdier søkes i datokolonner

   Verdifunn er ofte sterkere enn navnelikhet. Hvis en DWH-verdi finnes i en IFS-kolonne, øker scoren for den kandidaten.

6. **Score mulige mappinger**

   Hver mulig kombinasjon av DWH-kolonne og IFS-kolonne får en totalscore basert på:

   - **navn**: overlapper kolonnenavn eller tabellnavn, inkludert norske/engelske synonymer
   - **datatype**: passer DWH-datatypen med Oracle-datatypen
   - **verdier**: finnes DWH-verdier igjen i Oracle-kolonnen
   - **suspects-prioritet**: om IFS-tabellen står i `suspects.txt`

   Scriptet bruker også DWH-tabellen som kontekst. For eksempel vil `dim_avtale.Avtalenr` få ekstra støtte hvis IFS-tabellen eller IFS-kolonnen også ser ut til å handle om avtaler, kontrakter eller lease.

   Suspects-prioritet er bare et lite signal. Den skal hjelpe riktige kandidater opp, men ikke gjøre en svak match til fasit alene.

7. **Skriv rapporter**

   Rapportene skrives til `ifs_profile_output/reports/`.

   Den viktigste rapporten for manuell gjennomgang er `09_dwh_column_mapping_summary.csv`.

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

## suspects.txt

`suspects.txt` er en valgfri liste over IFS/Oracle-tabeller som bør undersøkes først.

Fila legges ved siden av `CompareAndSeek.py`.

Format:

```text
# Kommentarer er lov
CONTRACT_TAB
IFSAPP.SUPPLIER_INFO_TAB
IFSAPP.IDENTITY_INVOICE_INFO
```

Regler:

- en tabell per linje
- `TABLE_NAME` gjelder for alle valgte Oracle-skjemaer
- `OWNER.TABLE_NAME` gjelder bare for det skjemaet
- tomme linjer ignoreres
- tekst etter `#` ignoreres

Når `suspects.txt` finnes, skjer dette:

- suspects-tabellene profileres først
- verdier søkes først i kompatible kolonner fra suspects-tabeller
- kandidater fra suspects-tabeller får et lite scorepåslag
- rapporten `00_source_table_hints.csv` viser hvilke hints som ble lest

Dette er nyttig når vi vet at enkelte IFS-tabeller sannsynligvis inneholder verdiene vi leter etter, men fortsatt vil la scriptet lete videre i resten av IFS etterpå.

## Kjøring

Standardkjøring:

```powershell
python CompareAndSeek.py
```

Vanlige kjøringer:

```powershell
python CompareAndSeek.py --dwh-schema mart_m
python CompareAndSeek.py --dwh-tables dim_avtale fact_areal
python CompareAndSeek.py --suspects-file suspects.txt
python CompareAndSeek.py --initial-probes-per-dwh-column 1 --probes-per-dwh-column 5
python CompareAndSeek.py --min-total-score 0.35
python CompareAndSeek.py --skip-value-search
```

Forklaring:

- `--dwh-schema mart_m` velger hvilket DWH-skjema som skal leses.
- `--dwh-tables dim_avtale fact_areal` begrenser analysen til konkrete DWH-tabeller.
- `--suspects-file suspects.txt` velger hvilken IFS-tabelliste som skal prioriteres.
- `--min-rows 10` gjør at vanlige Oracle-tabeller må ha minst 10 rader for å bli samplet og brukt bredt.
- `--initial-probes-per-dwh-column 1` søker første fingerprint bredt mot IFS.
- `--probes-per-dwh-column 5` bruker inntil fem fingerprints totalt, men etter første treff søkes de neste bare mot treffkolonnene.
- `--min-total-score 0.35` tar bare med kandidater med minst denne scoren.
- `--skip-value-search` hopper over verdibasert søk i Oracle. Dette går raskere, men gir svakere kandidater.
- `--skip-oracle-profile` bruker eksisterende Oracle-profil i lokal SQLite i stedet for å profilere Oracle på nytt.
- `--skip-dwh-profile` bruker eksisterende DWH-profil i lokal SQLite.

Ved første kjøring bør man normalt kjøre uten `--skip-*`, slik at både DWH og IFS/Oracle blir profilert.

## Output

Rapporter skrives til `ifs_profile_output/reports/`.

Disse rapportene kan åpnes direkte i regneark, men de kan også importeres i Streamlit-appen for strukturert gjennomgang og godkjenning.

### Anbefalt rekkefølge

Start med disse:

1. `09_dwh_column_mapping_summary.csv`

   En rad per DWH-kolonne, med beste IFS-kandidat hvis scriptet fant en.

   Viktige felt:

   - `dwh_table`, `dwh_column`: kolonnen vi prøver å finne kilde for
   - `oracle_table`, `oracle_column`: beste IFS-kandidat
   - `total_score`: samlet score
   - `mapping_status`: enkel vurdering av kandidaten
   - `reason`: hvilke navne-/tabelltreff som bidro

2. `07_best_mapping_per_dwh_column.csv`

   Viser inntil 10 kandidater per DWH-kolonne. Bruk denne når beste kandidat i summary-rapporten ikke virker riktig.

3. `05_oracle_value_hits.csv`

   Viser konkrete DWH-verdier som faktisk ble funnet i Oracle-kolonner. Dette er nyttig når du vil se hvorfor en kandidat fikk høy verdi-score.

   Feltet `match_kind` viser også om treffet kom fra bredt førstesøk (`*_initial`) eller fra bekreftelsessøk etter at en kandidat allerede var funnet (`*_confirming`).

4. `10_dwh_columns_without_mapping.csv`

   Viser DWH-kolonner der scriptet ikke fant noen kandidat over terskelen.

### Alle rapporter

- `00_source_table_hints.csv`

  Tabeller som ble lest fra `suspects.txt`, med prioritet.

- `04_dwh_value_fingerprints.csv`

  Verdier som ble hentet fra DWH-kolonner og brukt som søkegrunnlag.

- `05_oracle_value_hits.csv`

  Konkrete DWH-verdier som ble funnet igjen i Oracle.

- `06_mapping_candidates.csv`

  Alle kandidater over terskelen, sortert per DWH-kolonne.

- `07_best_mapping_per_dwh_column.csv`

  Toppkandidater per DWH-kolonne.

- `08_tables_with_many_candidate_hits.csv`

  Oracle-tabeller som får mange kandidattreff. Nyttig for å finne sentrale kildetabeller.

- `09_dwh_column_mapping_summary.csv` - beste IFS-kandidat per DWH-kolonne
- `10_dwh_columns_without_mapping.csv` - DWH-kolonner uten kandidat over terskelen

## Hvordan tolke score

`total_score` er en indikasjon, ikke en fasit.

Tommelfingerregel:

- `strong`: sannsynlig kandidat, bør valideres mot forretningslogikk
- `possible`: interessant kandidat, bør sammenlignes med flere verdier eller tabellkontekst
- `weak`: svakt signal, ofte bare navnelikhet eller datatype
- `no_candidate`: ingen kandidat over terskelen

Lav score betyr ikke nødvendigvis at kolonnen ikke finnes i IFS. Det kan også bety at:

- riktig IFS-tabell ikke ligger i `suspects.txt`, og ikke ble prioritert tidlig
- kolonnen ligger i et Oracle-skjema som ikke ble profilert
- verdiene er transformert i DWH
- DWH-kolonnen er beregnet
- DWH-navnet er langt unna IFS-navnet
- verdibasert søk ble hoppet over

## Anbefalt arbeidsmåte

1. Kjør først på et lite utvalg DWH-tabeller:

   ```powershell
   python CompareAndSeek.py --dwh-tables dim_avtale
   ```

2. Legg inn sannsynlige IFS-tabeller i `suspects.txt` hvis du kjenner noen.

3. Kjør samme DWH-tabell igjen med suspects-listen:

   ```powershell
   python CompareAndSeek.py --dwh-tables dim_avtale --suspects-file suspects.txt
   ```

4. Start Streamlit-appen og importer `03_dwh_columns.csv`, `02_oracle_columns.csv` og `07_best_mapping_per_dwh_column.csv`.

5. Bruk `Dashboard` og `Table Explorer` for å se hvilke felter som har kandidater, hull eller trenger vurdering.

6. Bruk `Field Detail` til å bekrefte eller avvise kandidater. Bruk `05_oracle_value_hits.csv` ved behov for å sjekke konkrete verdifunn.

7. Juster eventuelt `--min-total-score`:

   ```powershell
   python CompareAndSeek.py --dwh-tables dim_avtale --min-total-score 0.35
   ```

8. Importer nye kandidatrapporter i Streamlit etter nye kjøringer. Bekreftede og avviste vurderinger beholdes.

9. Når en tabell ser fornuftig ut, kjør bredere mot flere `dim_` og `fact_`-objekter.
