"""
Firestore → nested JSON exporter.

Dumps every document in the referral-extraction collection into one nested JSON
file. The flat `patient_*`, `patient_address_*`, `referring_doctor_*`, `gt_*`
and `pred_*` fields are re-grouped into nested objects for readability.

Usage
─────
    python firebase_export/extractor.py            # nested output (default)
    python firebase_export/extractor.py --flat     # keep fields flat, as stored
    python firebase_export/extractor.py out.json   # custom output path

Config comes from firebase_export/.env (see that file). This tool is fully
self-contained — it does NOT import the app's firestore_client, so it has its
own credentials/project and won't be affected by the app's .env.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
load_dotenv(HERE / ".env")

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "meddflow-dev-8397c")
COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "referral-extraction")
DATABASE   = os.environ.get("FIRESTORE_DATABASE", COLLECTION)
SA_PATH    = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "google.json")
OUT_PATH   = os.environ.get("EXPORT_OUTPUT_PATH", "firestore_export.json")


def _resolve_sa_path(raw: str) -> Path:
    """Resolve the service-account path relative to this folder or the project root."""
    p = Path(raw)
    if p.is_absolute():
        return p
    for cand in (HERE / raw, PROJECT_ROOT / raw, Path(raw)):
        if cand.exists():
            return cand
    return p


def get_client():
    from google.cloud import firestore
    from google.oauth2 import service_account

    scopes = ["https://www.googleapis.com/auth/datastore",
              "https://www.googleapis.com/auth/cloud-platform"]

    sa_json = os.environ.get("FIREBASE_SA_JSON") or os.environ.get("GOOGLE_SA_JSON")
    if sa_json:
        cred = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=scopes)
        print("auth: service-account JSON from env")
    else:
        sa = _resolve_sa_path(SA_PATH)
        if sa.exists():
            cred = service_account.Credentials.from_service_account_file(str(sa), scopes=scopes)
            print(f"auth: service-account file {sa}")
        else:
            import google.auth
            cred, _ = google.auth.default(scopes=scopes)
            print("auth: application default credentials")

    return firestore.Client(project=PROJECT_ID, credentials=cred, database=DATABASE)


# Flat-field prefixes → nested path. Most-specific prefix must come first.
# NB: address parts go under patient.address_components (NOT patient.address) so
# they don't collide with the scalar patient_address string field.
GROUP_PREFIXES = [
    ("patient_address_", ("patient", "address_components")),
    ("patient_",         ("patient",)),
    ("referring_doctor_", ("referring_doctor",)),
    ("gt_",              ("ground_truth",)),
    ("pred_",            ("predictions",)),
]


def nest(flat: dict) -> dict:
    """Re-group flat prefixed fields into nested objects; pass others through."""
    out: dict = {}
    for key, value in flat.items():
        for prefix, path in GROUP_PREFIXES:
            if key.startswith(prefix):
                node = out
                for part in path:
                    node = node.setdefault(part, {})
                node[key[len(prefix):]] = value
                break
        else:
            out[key] = value
    return out


def main() -> None:
    args = [a for a in sys.argv[1:]]
    nested = "--flat" not in args
    custom_out = next((a for a in args if not a.startswith("--")), None)

    client = get_client()
    print(f"exporting {PROJECT_ID}/{DATABASE}/{COLLECTION} ...")

    documents: dict = {}
    for doc in client.collection(COLLECTION).stream():
        data = doc.to_dict() or {}
        documents[doc.id] = nest(data) if nested else data

    payload = {
        "project": PROJECT_ID,
        "database": DATABASE,
        "collection": COLLECTION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "nested": nested,
        "count": len(documents),
        "documents": documents,
    }

    out = Path(custom_out or OUT_PATH)
    if not out.is_absolute():
        out = HERE / out
    out.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    print(f"✅ exported {len(documents)} documents → {out}")


if __name__ == "__main__":
    main()
