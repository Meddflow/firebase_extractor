# firebase_extractor

Export a Firestore collection into one nested JSON file.

Dumps every document in the collection, re-grouping flat `patient_*`,
`patient_address_*`, `referring_doctor_*`, `gt_*` and `pred_*` fields into
nested objects for readability.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your project + service account
```

Provide credentials either as a key file (`FIREBASE_SERVICE_ACCOUNT_PATH`) or
inline JSON (`FIREBASE_SA_JSON`). Application Default Credentials are used as a
fallback if neither is set.

## Usage

### JSON (default)

```bash
python extractor.py            # nested output (default)
python extractor.py --flat     # keep fields flat, exactly as stored
python extractor.py out.json   # custom output path
```

### CSV

```bash
python extractor.py --csv                 # full export → referrals_full_<ts>.csv
python extractor.py --csv --incremental   # only records newer than last run → referrals_incr_<ts>.csv
python extractor.py --clean               # delete local referrals_*.csv older than 60 min
python extractor.py --watch               # loop: 1st cycle full, then every 2 min only-new + clean
```

## Flag reference

| Flag | What it does |
|------|--------------|
| *(none)* | JSON export — whole collection → one nested JSON file (default). |
| `--flat` | JSON export, fields kept flat (`patient_name`) instead of nested. |
| `out.json` | Custom output path for JSON mode (a bare, non-`--` argument). |
| `--json` | Force JSON even if `EXPORT_FORMAT=csv` is set in `.env`. |
| `--csv` | CSV mode — one row per record → `referrals_full_<ts>.csv`. |
| `--incremental` | With CSV: write only records newer than the saved watermark → `referrals_incr_<ts>.csv`. |
| `--full` | Force a full CSV even if `EXPORT_INCREMENTAL=true` is set. |
| `--clean` | Delete **local** `referrals_*.csv` older than the threshold. Never touches Firestore. Runs after an export in CSV mode, or standalone in JSON mode. |
| `--watch` | Loop: first cycle full, then every interval writes only-new + cleans. Ctrl-C to stop. |
| `--interval N` | `--watch` loop delay in **seconds** (default 120). |
| `--max-age-min N` | `--clean` age threshold in **minutes** (default 60). |
| `--collection NAME` | Export a different collection for this one run (overrides `.env`). |

**Incremental** uses `completed_at_utc` as a cursor. The high-water mark and a
stable CSV column order are saved per-collection in `.csv_state.json`, so each
run only emits records that finished after the previous run. Delete
`.csv_state.json` to force the next run back to a full export.

**Clean** only deletes the **local** CSV files — the records always remain in
Firestore. Typical setup: run `--watch` (or a 2-minute cron of
`--csv --incremental --clean`) so a rolling last-hour of CSV batches sits here.

## Environment toggles

Set defaults in `.env` so a bare `python extractor.py` behaves how you want —
no flags needed. **A CLI flag always overrides the matching toggle.**

| Env var | Values (default) | Effect |
|---------|------------------|--------|
| `EXPORT_FORMAT` | `json` \| `csv` (`json`) | Default output format. Set `csv` to make CSV the default. |
| `EXPORT_INCREMENTAL` | `true` \| `false` (`false`) | In CSV mode, export only-new by default. |
| `EXPORT_CLEAN_AFTER` | `true` \| `false` (`false`) | Run cleanup after each export. |
| `EXPORT_WATCH` | `true` \| `false` (`false`) | Run the watch loop by default. |
| `INCREMENTAL_CURSOR_FIELD` | field name (`completed_at_utc`) | Which field marks a record as "new". |
| `CSV_FILE_PREFIX` | string (`referrals`) | Prefix of generated CSV filenames. |
| `WATCH_INTERVAL_SECONDS` | int (`120`) | `--watch` loop delay. |
| `CSV_MAX_AGE_MINUTES` | int (`60`) | `--clean` age threshold. |

Example — make CSV incremental with cleanup the hands-off default:

```ini
EXPORT_FORMAT=csv
EXPORT_INCREMENTAL=true
EXPORT_CLEAN_AFTER=true
```

Then `python extractor.py` writes only-new records and tidies old CSVs, while
`python extractor.py --json` still gives you a one-off JSON dump.

## Output

```json
{
  "project": "...", "database": "...", "collection": "...",
  "exported_at_utc": "...", "nested": true, "count": 0,
  "documents": {
    "<doc_id>": {
      "status": "success",
      "patient": { "name": "...", "address": "...",
                   "address_components": { "street": "...", "suburb": "...",
                                           "state": "...", "postcode": "..." } },
      "referring_doctor": { "...": "..." },
      "ground_truth": { "region": "...", "funding": "..." },
      "predictions": { "region": "...", "funding": "..." }
    }
  }
}
```

## Security

**Never commit** the service-account key or the exported JSON — the export
contains patient PII. Both are covered by `.gitignore` (`*.json`, `.env`).
