"""
Firestore → JSON / CSV exporter.

Dumps documents from the referral-extraction collection. Two output formats:

  • JSON (default)  — one nested JSON file, flat `patient_*`, `patient_address_*`,
                      `referring_doctor_*`, `gt_*`, `pred_*` re-grouped for readability.
  • CSV            — one row per document. Supports incremental "only-new" batches
                      and time-based cleanup of the local CSV files.

Usage
─────
    # JSON (unchanged)
    python firebase_export/extractor.py              # nested JSON (default)
    python firebase_export/extractor.py --flat       # flat JSON, as stored
    python firebase_export/extractor.py out.json     # custom JSON path

    # CSV
    python firebase_export/extractor.py --csv                 # full export → referrals_full_<ts>.csv
    python firebase_export/extractor.py --csv --incremental   # only records newer than last run → referrals_incr_<ts>.csv
    python firebase_export/extractor.py --clean               # delete local referrals_*.csv older than 60 min
    python firebase_export/extractor.py --watch               # loop: 1st cycle full, then every 2 min only-new + clean

    # tuning flags (any mode)
    --interval 120        watch loop seconds (default 120)
    --max-age-min 60      clean threshold in minutes (default 60)
    --collection NAME     override FIRESTORE_COLLECTION for this run

Incremental cursor = `completed_at_utc` (set when a document finishes). The high-water
mark + CSV column order are persisted in firebase_export/.csv_state.json. Cleanup only
deletes the LOCAL csv files — the records always remain in Firestore.

Config comes from firebase_export/.env (see that file). This tool is fully
self-contained — it does NOT import the app's firestore_client, so it has its
own credentials/project and won't be affected by the app's .env.
"""

import os
import sys
import csv
import json
import time
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

# ── CSV / incremental config ──
CURSOR_FIELD    = os.environ.get("INCREMENTAL_CURSOR_FIELD", "completed_at_utc")
CSV_PREFIX      = os.environ.get("CSV_FILE_PREFIX", "referrals")
STATE_FILE      = ".csv_state.json"
DEFAULT_INTERVAL    = int(os.environ.get("WATCH_INTERVAL_SECONDS", "120"))
DEFAULT_MAX_AGE_MIN = int(os.environ.get("CSV_MAX_AGE_MINUTES", "60"))


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


# ── JSON export (original behaviour) ──────────────────────────────────────────

def export_json(client, nested: bool, custom_out: str | None) -> None:
    print(f"exporting {PROJECT_ID}/{DATABASE}/{COLLECTION} ...")
    documents: dict = {}
    for doc in client.collection(COLLECTION).stream():
        data = doc.to_dict() or {}
        documents[doc.id] = nest(data) if nested else data

    payload = {
        "project": PROJECT_ID, "database": DATABASE, "collection": COLLECTION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "nested": nested, "count": len(documents), "documents": documents,
    }
    out = Path(custom_out or OUT_PATH)
    if not out.is_absolute():
        out = HERE / out
    out.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    print(f"✅ exported {len(documents)} documents → {out}")


# ── CSV state (watermark + stable column order) ───────────────────────────────

def _load_state() -> dict:
    """Whole state file: {"<collection>": {"watermark":..., "columns":[...]}, ...}."""
    p = HERE / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _col_state(all_state: dict) -> dict:
    """Per-collection state so different collections keep independent cursors."""
    return all_state.get(COLLECTION) or {"watermark": None, "columns": []}


def _save_state(all_state: dict, col_state: dict) -> None:
    all_state[COLLECTION] = col_state
    (HERE / STATE_FILE).write_text(json.dumps(all_state, indent=2))


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _fetch_rows(client, incremental: bool):
    """Return (rows, new_watermark, state). rows = list of flat dicts incl. doc_id."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    all_state = _load_state()
    cs = _col_state(all_state)
    wm = cs.get("watermark")
    col = client.collection(COLLECTION)

    if incremental and wm:
        stream = (col.where(filter=FieldFilter(CURSOR_FIELD, ">", wm))
                     .order_by(CURSOR_FIELD).stream())
    else:
        stream = col.stream()

    rows, max_wm = [], wm
    for doc in stream:
        data = doc.to_dict() or {}
        rows.append({"doc_id": doc.id, **data})
        cur = data.get(CURSOR_FIELD)
        if cur and (max_wm is None or str(cur) > str(max_wm)):
            max_wm = str(cur)
    return rows, max_wm, all_state, cs


def export_csv(client, incremental: bool = False):
    """Write one CSV. Incremental writes only records newer than the saved watermark."""
    rows, new_wm, all_state, cs = _fetch_rows(client, incremental)

    if incremental and not rows:
        print("no new records since last export — nothing written")
        return None, 0

    # Stable, growing column order: reuse saved order, append any new keys.
    cols = list(cs.get("columns") or [])
    if "doc_id" not in cols:
        cols.insert(0, "doc_id")
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    kind = "incr" if incremental else "full"
    out = HERE / f"{CSV_PREFIX}_{kind}_{ts}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_value(r.get(k)) for k in cols})

    cs["columns"] = cols
    if new_wm:
        cs["watermark"] = new_wm
    _save_state(all_state, cs)
    print(f"✅ wrote {len(rows)} record(s) → {out.name}  (watermark: {cs.get('watermark')})")
    return out, len(rows)


def clean_old(max_age_min: int = DEFAULT_MAX_AGE_MIN) -> int:
    """Delete local referrals_*.csv older than max_age_min. Firestore is untouched."""
    cutoff = time.time() - max_age_min * 60
    deleted = 0
    for p in HERE.glob(f"{CSV_PREFIX}_*.csv"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            deleted += 1
            print(f"  deleted {p.name}")
    print(f"🧹 cleaned {deleted} local CSV(s) older than {max_age_min} min (records remain in Firestore)")
    return deleted


def watch(client, interval: int, max_age_min: int) -> None:
    """Loop: first cycle exports everything, then every `interval`s writes only-new + cleans."""
    print(f"watch mode — incremental CSV every {interval}s, clean CSVs > {max_age_min}min. Ctrl-C to stop.")
    try:
        while True:
            export_csv(client, incremental=True)   # 1st run (no watermark) = full, then only-new
            clean_old(max_age_min)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _int_flag(args, name, default):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                pass
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Flags that consume the NEXT token as their value (so it isn't mistaken for the
# JSON output path in default mode).
_VALUE_FLAGS = {"--collection", "--interval", "--max-age-min"}


def main() -> None:
    global COLLECTION
    args = sys.argv[1:]

    if "--collection" in args:
        i = args.index("--collection")
        if i + 1 < len(args):
            COLLECTION = args[i + 1]

    interval    = _int_flag(args, "--interval", DEFAULT_INTERVAL)
    max_age_min = _int_flag(args, "--max-age-min", DEFAULT_MAX_AGE_MIN)

    # ── Resolve behaviour: an explicit CLI flag always wins; else fall back to
    #    the .env toggle; else the built-in default. ──
    if "--csv" in args:
        fmt = "csv"
    elif "--json" in args:
        fmt = "json"
    else:
        fmt = os.environ.get("EXPORT_FORMAT", "json").strip().lower()

    watch_on = ("--watch" in args) or _env_bool("EXPORT_WATCH")

    if "--incremental" in args:
        incremental = True
    elif "--full" in args:
        incremental = False
    else:
        incremental = _env_bool("EXPORT_INCREMENTAL")

    clean_after = ("--clean" in args) or _env_bool("EXPORT_CLEAN_AFTER")

    # ── Dispatch ──
    if watch_on:                                   # watch loop (csv incremental + clean)
        watch(get_client(), interval, max_age_min)
        return

    # `--clean` with no export intent (JSON default) = clean-only, no Firestore call.
    if "--clean" in args and fmt != "csv":
        clean_old(max_age_min)
        return

    if fmt == "csv":
        client = get_client()
        export_csv(client, incremental=incremental)
        if clean_after:
            clean_old(max_age_min)
        return

    # default: JSON export (original behaviour)
    nested = "--flat" not in args
    consumed = set()
    for vf in _VALUE_FLAGS:
        if vf in args:
            i = args.index(vf)
            if i + 1 < len(args):
                consumed.add(i + 1)
    custom_out = next(
        (a for idx, a in enumerate(args) if not a.startswith("--") and idx not in consumed),
        None,
    )
    export_json(get_client(), nested, custom_out)


if __name__ == "__main__":
    main()
