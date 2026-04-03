"""
Box AI Document Enricher — uses Box AI extract_structured.
Files never leave Box custody; BAA covered by Enterprise Advanced.

Usage:
  .venv/bin/python3 python/enrich.py \
    --manifest-file /tmp/test/slug_manifest.csv \
    --token <box_access_token> \
    [--model google__gemini_2_5_pro] \
    [--workers 5]
"""

import argparse
import csv
import threading
import concurrent.futures
import time
import requests

BOX_EXTRACT_URL = "https://api.box.com/2.0/ai/extract_structured"
ENRICHABLE_EXTENSIONS = {".pdf"}

FIELDS = [
    {
        "key": "document_date",
        "type": "string",
        "description": (
            "The date or date range of the records in this document. "
            "For a single document, return the document's own date in YYYY-MM-DD format — "
            "look in letterhead, header, footer, or signature block; not a received or filed stamp. "
            "For a compilation of records spanning multiple dates, return a range in the format "
            "'YYYY-MM-DD – YYYY-MM-DD'. Return an empty string if no clear date is found."
        ),
        "prompt": (
            "What is the date or date range of this document? "
            "If it is a single document, return its creation or signature date as YYYY-MM-DD. "
            "If it is a compilation of records covering multiple dates, return the range as "
            "'YYYY-MM-DD – YYYY-MM-DD'. Return empty string if no clear date exists."
        ),
    },
    {
        "key": "description",
        "type": "string",
        "description": (
            "A short label identifying what type of document this is. "
            "Maximum 20 words. Do not start with 'This document is' or 'This is'. "
            "Write a noun phrase, not a full sentence."
        ),
        "prompt": (
            "Identify what type of document this is in 20 words or fewer. "
            "Do not start with 'This document is' or 'This is'. "
            "Write a noun phrase only — for example: 'Forensic mental health evaluation report for [Patient Name].'"
        ),
    },
]


def call_box_ai(token: str, file_id: str, model: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "items": [{"type": "file", "id": file_id}],
        "fields": FIELDS,
        "ai_agent": {
            "type": "ai_agent_extract_structured",
            "long_text": {"model": model},
        },
    }
    response = requests.post(BOX_EXTRACT_URL, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    answer = response.json().get("answer", {})
    return {
        "ai_date": answer.get("document_date") or "",
        "ai_description": answer.get("description") or "",
    }


def enrich_row(token: str, model: str, row: dict) -> dict:
    last_err = None
    for attempt in range(1, 4):
        try:
            return call_box_ai(token, row["File ID"], model)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            last_err = f"HTTP {status}"
            if e.response is not None and e.response.status_code == 429:
                wait = 10 * attempt
                print(f"  Rate limited — waiting {wait}s before retry {attempt}/3 for {row['Name']}", flush=True)
                time.sleep(wait)
            else:
                break  # non-retriable HTTP error
        except requests.exceptions.Timeout:
            last_err = "timeout"
            wait = 5 * attempt
            print(f"  Timeout — waiting {wait}s before retry {attempt}/3 for {row['Name']}", flush=True)
            time.sleep(wait)
        except Exception as e:
            last_err = str(e)
            break
    print(f"  Warning: enrichment failed for {row['Name']}: {last_err}", flush=True)
    return {"ai_date": "", "ai_description": "", "_failed": True}


def main():
    parser = argparse.ArgumentParser(description="Box AI Document Enricher")
    parser.add_argument("--manifest-file", required=True, help="Path to *_manifest.csv (will be overwritten)")
    parser.add_argument("--token", required=True, help="Box access token")
    parser.add_argument("--model", default="google__gemini_2_5_pro", help="Box AI model ID")
    parser.add_argument("--workers", type=int, default=5, help="Parallel API workers")
    args = parser.parse_args()

    with open(args.manifest_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pdf_rows = [r for r in rows if r.get("Extension", "").lower() in ENRICHABLE_EXTENSIONS]
    total = len(pdf_rows)
    print(f"Box AI enrichment: {total} files to process (model: {args.model})", flush=True)

    row_by_id = {r["File ID"]: r for r in rows}
    counter = [0]
    lock = threading.Lock()
    failures = []

    def process(row):
        result = enrich_row(args.token, args.model, row)
        with lock:
            counter[0] += 1
            n = counter[0]
        status = "FAILED" if result.get("_failed") else (result['ai_date'] or 'no date')
        print(f"  [{n}/{total}] {row['Name']} → {status}", flush=True)
        return row["File ID"], result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process, r): r for r in pdf_rows}
        for future in concurrent.futures.as_completed(futures):
            file_id, result = future.result()
            row_by_id[file_id]["AI Date"] = result["ai_date"]
            row_by_id[file_id]["AI Description"] = result["ai_description"]
            if result.get("_failed"):
                failures.append(row_by_id[file_id]["Name"])

    for row in rows:
        row.setdefault("AI Date", "")
        row.setdefault("AI Description", "")

    fieldnames = list(rows[0].keys())
    with open(args.manifest_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    succeeded = total - len(failures)
    if failures:
        print(f"Enrichment complete: {succeeded}/{total} succeeded, {len(failures)} failed:", flush=True)
        for name in failures:
            print(f"  FAILED: {name}", flush=True)
    else:
        print(f"Enrichment complete: {total}/{total} succeeded → {args.manifest_file}", flush=True)


if __name__ == "__main__":
    main()