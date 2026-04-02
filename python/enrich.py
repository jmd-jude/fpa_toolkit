"""
AI Document Enricher — extracts document date and description using Claude vision API.
Reads a manifest CSV, downloads each PDF from Box, renders the first 3 pages as images,
and asks Claude for a document date and brief description.
Overwrites the input manifest with two new columns: AI Date, AI Description.

Usage:
  .venv/bin/python3 python/enrich.py \
    --manifest-file /tmp/test/slug_manifest.csv \
    --token <box_access_token> \
    [--model claude-haiku-4-5-20251001] \
    [--workers 5]
"""

import argparse
import base64
import csv
import json
import os
import threading
import concurrent.futures

import fitz  # pymupdf
import anthropic
from boxsdk import OAuth2, Client

MAX_PAGES = 3
ENRICHABLE_EXTENSIONS = {'.pdf'}


def render_pages_as_images(pdf_bytes: bytes) -> list[str]:
    """Render first MAX_PAGES of a PDF as base64-encoded PNG strings."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page_num in range(min(MAX_PAGES, len(doc))):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        images.append(base64.standard_b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images


def call_claude(anthropic_client: anthropic.Anthropic, model: str, images: list[str]) -> dict:
    """Send page images to Claude and return {date, description}."""
    content = []
    for img_b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
        })
    content.append({
        "type": "text",
        "text": (
            "Review these document pages. Return a JSON object with exactly two fields:\n"
            "- \"date\": the document's own date in YYYY-MM-DD format (look in letterhead, "
            "header, footer, signature block, or body). Use the earliest date that appears "
            "to be the document's creation date, not a received/filed stamp. "
            "Return null if no clear date is found.\n"
            "- \"description\": one sentence (15 words or fewer) describing what this document is.\n"
            "Return only valid JSON, no markdown, no other text."
        ),
    })

    response = anthropic_client.messages.create(
        model=model,
        max_tokens=150,
        messages=[{"role": "user", "content": content}],
    )

    text = response.content[0].text.strip()
    # Strip markdown code fences if model wraps output
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    return {
        "ai_date": result.get("date") or "",
        "ai_description": result.get("description") or "",
    }


def enrich_row(
    box_client: Client,
    anthropic_client: anthropic.Anthropic,
    model: str,
    row: dict,
) -> dict:
    """Download a file from Box and enrich it. Returns {ai_date, ai_description}."""
    try:
        pdf_bytes = box_client.file(row["File ID"]).content()
        images = render_pages_as_images(pdf_bytes)
        if not images:
            return {"ai_date": "", "ai_description": ""}
        return call_claude(anthropic_client, model, images)
    except Exception as e:
        print(f"  Warning: enrichment failed for {row['Name']}: {e}", flush=True)
        return {"ai_date": "", "ai_description": ""}


def main():
    parser = argparse.ArgumentParser(description="AI Document Enricher")
    parser.add_argument("--manifest-file", required=True, help="Path to *_manifest.csv (will be overwritten)")
    parser.add_argument("--token", required=True, help="Box access token")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Anthropic model ID")
    parser.add_argument("--workers", type=int, default=5, help="Parallel download/API workers")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set", flush=True)
        raise SystemExit(1)

    box_auth = OAuth2(client_id=None, client_secret=None, access_token=args.token)
    box_client = Client(box_auth)
    anthropic_client = anthropic.Anthropic(api_key=api_key)

    with open(args.manifest_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pdf_rows = [r for r in rows if r.get("Extension", "").lower() in ENRICHABLE_EXTENSIONS]
    total = len(pdf_rows)
    print(f"AI enrichment: {total} PDF files to process", flush=True)

    row_by_id = {r["File ID"]: r for r in rows}
    counter = [0]
    lock = threading.Lock()

    def process(row):
        result = enrich_row(box_client, anthropic_client, args.model, row)
        with lock:
            counter[0] += 1
            n = counter[0]
        date_display = result["ai_date"] or "no date found"
        print(f"  [{n}/{total}] {row['Name']} → {date_display}", flush=True)
        return row["File ID"], result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process, r): r for r in pdf_rows}
        for future in concurrent.futures.as_completed(futures):
            file_id, result = future.result()
            row_by_id[file_id]["AI Date"] = result["ai_date"]
            row_by_id[file_id]["AI Description"] = result["ai_description"]

    # Ensure all rows have the new fields (non-PDF rows get empty strings)
    for row in rows:
        row.setdefault("AI Date", "")
        row.setdefault("AI Description", "")

    fieldnames = list(rows[0].keys())
    with open(args.manifest_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Enrichment complete → {args.manifest_file}", flush=True)


if __name__ == "__main__":
    main()
