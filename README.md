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

```bash
python extractor.py            # nested output (default)
python extractor.py --flat     # keep fields flat, exactly as stored
python extractor.py out.json   # custom output path
```

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
