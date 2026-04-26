"""
Persist manifest and summary CSVs to the database after a document index job.
Non-fatal: exits 0 even on failure so the pipeline always continues.

Usage:
    .venv/bin/python3 python/db_persist.py \
        --job-id <uuid> \
        --manifest-file /tmp/.../slug_manifest.csv \
        --summary-file /tmp/.../slug_summary.csv
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras


def _load_env_local():
    env_path = Path(__file__).parent.parent / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() and key.strip().isidentifier():
            os.environ.setdefault(key.strip(), val.strip())


def coerce_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def coerce_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def coerce_date(val):
    if not val or val == "N/A":
        return None
    return val  # stored as text 'YYYY-MM-DD'; Postgres accepts it for DATE columns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--manifest-file", required=True)
    parser.add_argument("--summary-file", required=True)
    args = parser.parse_args()

    _load_env_local()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("db_persist: DATABASE_URL not set — skipping.", flush=True)
        return

    try:
        with open(args.manifest_file, newline="", encoding="utf-8") as f:
            manifest_rows = list(csv.DictReader(f))
        with open(args.summary_file, newline="", encoding="utf-8") as f:
            summary_rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"db_persist: could not read CSVs — {e}", flush=True)
        return

    try:
        conn = psycopg2.connect(db_url)
    except Exception as e:
        print(f"db_persist: DB connection failed — {e}", flush=True)
        return

    try:
        with conn:
            with conn.cursor() as cur:
                file_records = []
                for r in manifest_rows:
                    pc = r.get("Page Count", "")
                    file_records.append((
                        args.job_id,
                        r.get("Folder ID") or None,
                        r.get("File ID") or None,
                        r.get("Name") or None,
                        r.get("Path") or None,
                        r.get("Folder") or None,
                        r.get("Extension") or None,
                        coerce_int(pc) if pc != "N/A" else None,
                        r.get("Page Count Source") or None,
                        coerce_float(r.get("Size (KB)", "")),
                        coerce_date(r.get("Created")),
                        coerce_date(r.get("Modified")),
                        r.get("File URL") or None,
                        r.get("Folder URL") or None,
                        True if r.get("Duplicate") == "Yes" else False,
                        r.get("AI Date") or "",
                        r.get("AI Description") or "",
                    ))

                psycopg2.extras.execute_values(cur, """
                    INSERT INTO manifest_files
                        (job_id, folder_id, file_id, name, path, folder, extension,
                         page_count, page_count_source, size_kb, created, modified,
                         file_url, folder_url, duplicate, ai_date, ai_description)
                    VALUES %s
                """, file_records)

                summary_records = []
                for r in summary_rows:
                    kpt = r.get("Known Page Total", "")
                    summary_records.append((
                        args.job_id,
                        r.get("Folder") or None,
                        coerce_int(r.get("Depth")),
                        coerce_int(r.get("File Count")),
                        coerce_int(kpt) if kpt != "N/A" else None,
                        coerce_int(r.get("Files Missing Page Count")),
                        r.get("Total Size") or None,
                        r.get("File Types") or None,
                    ))

                psycopg2.extras.execute_values(cur, """
                    INSERT INTO manifest_summary
                        (job_id, folder, depth, file_count, known_page_total,
                         files_missing_page_count, total_size, file_types)
                    VALUES %s
                """, summary_records)

        print(f"db_persist: saved {len(file_records)} files, {len(summary_records)} folders.", flush=True)
    except Exception as e:
        print(f"db_persist: write failed — {e}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
