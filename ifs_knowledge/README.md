# IFS knowledge base

Dette området er en liten, manuell kunnskapsbase for IFS-databasen.

Målet er å samle metadata, observasjoner og hypoteser mens vi prober IFS innenfor brannmuren. Vi skal helst unngå å hente mye faktisk datainnhold. Når eksempelrader trengs, holder vi oss til maks 20 rader per spørring.

## Filer

- `findings.md`: hva vi vet så langt, og hva vi tror.
- `candidate_objects.md`: objekter som ser relevante eller irrelevante ut.
- `suspects.txt`: prioritert liste som kan brukes av `CompareAndSeek.py`.
- `probe_queries.sql`: forsiktige metadata- og verifikasjonsspørringer.

## Prinsipper

- Prioriter metadata: objektnavn, kolonner, constraints, kommentarer, radstatistikk.
- Bruk faktisk innhold bare for små verifikasjoner, maks 20 rader.
- Skill mellom bekreftet funn og hypotese.
- Ikke anta at forretningsbegreper finnes i IFS-navn. Løsningen kan være en eiendomsvariant bygget oppå generiske IFS-strukturer.
- Når en tabell/view viser seg tom, behold funnet. Tomme objekter er nyttig negativ kunnskap.
