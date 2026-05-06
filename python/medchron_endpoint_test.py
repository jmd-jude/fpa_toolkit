"""
MedChron Endpoint Comparison Test

Compares Box AI text_gen vs extract_structured on the same PDF page ranges.
For each range: downloads pages, uploads chunk to Box, calls both endpoints,
writes labeled results to a .txt comparison file.

Usage:
  .venv/bin/python3 python/medchron_endpoint_test.py \
    --token <token> \
    --file-id <box_file_id> \
    --pages 1-3 2-4 5-7 15-17 \
    [--output-dir ~/Desktop/medchron_test]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests
from pypdf import PdfReader, PdfWriter

BOX_API = "https://api.box.com/2.0"
BOX_UPLOAD = "https://upload.box.com/api/2.0"
BOX_AI_TEXT_GEN = f"{BOX_API}/ai/text_gen"
BOX_AI_EXTRACT = f"{BOX_API}/ai/extract_structured"
BOX_AI_MODEL = os.environ.get("BOX_AI_MODEL", "google__gemini_2_5_pro")
SCRATCH_FOLDER_NAME = "__medchron_endpoint_test__"

TEXT_GEN_PROMPT = """\
You are a medical records analyst. The attached PDF contains pages {start}-{end} of a medical record.

Extract every clinically significant event from these pages.

Return ONLY a JSON array. Each element must have:
- "event_date": string YYYY-MM-DD
- "event_date_is_range": boolean
- "event_date_end": string YYYY-MM-DD or null
- "provider": string or null
- "facility": string or null
- "medical_information": string, faithful summary of clinical content

Return [] if these pages contain no clinically significant content. This includes:
- Blank pages, cover sheets, tables of contents
- Affidavits, custodian of records declarations, authorization forms
- Any page consisting primarily of legal or administrative language
- ICD code tables, diagnosis revision logs, structured intake forms

Return ONLY the JSON array. No text before or after.\
"""

EXTRACT_FIELDS = [
    {
        "key": "is_clinical_page",
        "type": "string",
        "description": (
            "Classify this content: 'clinical' if it contains clinical encounter documentation "
            "(SOAP notes, diagnoses, procedures, lab results, imaging, medications, hospital "
            "admission or discharge notes); 'administrative' if it contains primarily "
            "administrative content (intake forms, ICD code tables, authorization forms, "
            "affidavits, legal documents, custodian of records declarations); 'image' if the "
            "page appears to be a scanned image with no readable text."
        ),
        "prompt": (
            "Is this page primarily clinical documentation, administrative content, "
            "or a scanned image with no readable text? Reply with one word: clinical, "
            "administrative, or image."
        ),
    },
    {
        "key": "event_date",
        "type": "date",
        "description": (
            "The date on which the clinical event described here occurred. "
            "Leave empty if no clinical event date is present."
        ),
    },
    {
        "key": "provider",
        "type": "string",
        "description": (
            "The attending or treating provider name and credentials, "
            "e.g. 'Peter Remedios, M.D.'. Empty if not present."
        ),
    },
    {
        "key": "facility",
        "type": "string",
        "description": (
            "The clinic or hospital name where the event occurred. Empty if not present."
        ),
    },
    {
        "key": "medical_information",
        "type": "string",
        "description": (
            "Faithful summary of the clinical content on these pages. "
            "Preserve labeled sections (Subjective, Objective, Assessment, Plan) when present. "
            "Empty if no clinical content is present on these pages."
        ),
    },
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return s


def get_current_user(session: requests.Session) -> dict:
    r = session.get(f"{BOX_API}/users/me", timeout=30)
    r.raise_for_status()
    return r.json()


def download_file(session: requests.Session, file_id: str, dest: Path) -> None:
    r = session.get(f"{BOX_API}/files/{file_id}/content", timeout=120, stream=True)
    r.raise_for_status()
    with dest.open("wb") as fh:
        for chunk in r.iter_content(chunk_size=65536):
            fh.write(chunk)


def upload_file(session: requests.Session, folder_id: str, path: Path) -> str:
    upload_headers = {"Authorization": session.headers["Authorization"]}
    with path.open("rb") as fh:
        r = requests.post(
            f"{BOX_UPLOAD}/files/content",
            headers=upload_headers,
            data={"attributes": json.dumps({"name": path.name, "parent": {"id": folder_id}})},
            files={"file": (path.name, fh, "application/pdf")},
            timeout=120,
        )
    r.raise_for_status()
    return r.json()["entries"][0]["id"]


def delete_file(session: requests.Session, file_id: str) -> None:
    try:
        session.delete(f"{BOX_API}/files/{file_id}", timeout=30)
    except Exception:
        pass


def create_scratch_folder(session: requests.Session) -> str:
    payload = {"name": SCRATCH_FOLDER_NAME, "parent": {"id": "0"}}
    r = session.post(f"{BOX_API}/folders", json=payload, timeout=30)
    if r.status_code == 409:
        try:
            return r.json()["context_info"]["conflicts"][0]["id"]
        except (KeyError, IndexError):
            pass
        items = session.get(
            f"{BOX_API}/folders/0/items",
            params={"fields": "id,name,type", "limit": 1000},
            timeout=30,
        )
        items.raise_for_status()
        for item in items.json().get("entries", []):
            if item["type"] == "folder" and item["name"] == SCRATCH_FOLDER_NAME:
                return item["id"]
    r.raise_for_status()
    return r.json()["id"]


def delete_folder(session: requests.Session, folder_id: str) -> None:
    try:
        session.delete(
            f"{BOX_API}/folders/{folder_id}",
            params={"recursive": "true"},
            timeout=60,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PDF chunk extraction
# ---------------------------------------------------------------------------

def extract_pages(reader: PdfReader, start: int, end: int, output_path: Path) -> int:
    total = len(reader.pages)
    start = max(1, start)
    end = min(total, end)
    writer = PdfWriter()
    for i in range(start - 1, end):
        writer.add_page(reader.pages[i])
    with output_path.open("wb") as fh:
        writer.write(fh)
    return end - start + 1


# ---------------------------------------------------------------------------
# Box AI calls
# ---------------------------------------------------------------------------

def call_text_gen(session: requests.Session, file_id: str, start: int, end: int) -> str:
    prompt = TEXT_GEN_PROMPT.format(start=start, end=end)
    payload = {
        "items": [{"type": "file", "id": file_id}],
        "prompt": prompt,
        "ai_agent": {
            "type": "ai_agent_text_gen",
            "basic_gen": {"model": BOX_AI_MODEL},
        },
    }
    r = session.post(BOX_AI_TEXT_GEN, json=payload, timeout=180)
    r.raise_for_status()
    return r.json().get("answer", "").strip()


def call_extract_structured(session: requests.Session, file_id: str) -> dict:
    payload = {
        "items": [{"type": "file", "id": file_id}],
        "fields": EXTRACT_FIELDS,
        "ai_agent": {
            "type": "ai_agent_extract_structured",
            "long_text": {"model": BOX_AI_MODEL},
        },
    }
    r = session.post(BOX_AI_EXTRACT, json=payload, timeout=180)
    r.raise_for_status()
    return r.json().get("answer", {})


# ---------------------------------------------------------------------------
# Per-range test
# ---------------------------------------------------------------------------

def run_range(
    session: requests.Session,
    reader: PdfReader,
    scratch_folder_id: str,
    file_id: str,
    start: int,
    end: int,
    tmpdir: Path,
    out: list[str],
) -> None:
    label = f"pages {start}-{end}"
    separator = "=" * 80

    print(f"\n  [{label}] Extracting PDF chunk...", flush=True)
    chunk_path = tmpdir / f"chunk_p{start:04d}-{end:04d}.pdf"
    actual_pages = extract_pages(reader, start, end, chunk_path)

    # Extract pypdf text before uploading so we can log what the text layer contains
    total = len(reader.pages)
    pypdf_sections: list[str] = []
    for pg in range(start, min(end, total) + 1):
        text = (reader.pages[pg - 1].extract_text() or "").strip()
        char_count = len(text)
        pypdf_sections.append(f"  -- page {pg} ({char_count} chars) --")
        pypdf_sections.append(text if text else "  [no text extracted]")

    print(f"  [{label}] Uploading to Box ({actual_pages} page(s))...", flush=True)
    chunk_file_id = upload_file(session, scratch_folder_id, chunk_path)

    out.append(separator)
    out.append(f"PAGE RANGE: pages {start}-{end}  (file pages {start} to {end}, chunk file id: {chunk_file_id})")
    out.append(separator)

    out.append("")
    out.append("[PYPDF TEXT — local text layer extraction, before any Box AI call]")
    out.append("-" * 40)
    out.extend(pypdf_sections)

    # --- text_gen ---
    print(f"  [{label}] Calling text_gen...", flush=True)
    try:
        tg_raw = call_text_gen(session, chunk_file_id, start, end)
        # Try to pretty-print if it's JSON
        try:
            parsed = json.loads(tg_raw)
            tg_formatted = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            tg_formatted = tg_raw
        tg_status = "OK"
    except Exception as e:
        tg_formatted = f"ERROR: {e}"
        tg_status = "ERROR"

    out.append("")
    out.append(f"[TEXT_GEN]  model={BOX_AI_MODEL}  status={tg_status}")
    out.append("-" * 40)
    out.append(tg_formatted)

    # --- extract_structured ---
    print(f"  [{label}] Calling extract_structured...", flush=True)
    try:
        es_raw = call_extract_structured(session, chunk_file_id)
        es_formatted = json.dumps(es_raw, indent=2)
        es_status = "OK"
    except Exception as e:
        es_formatted = f"ERROR: {e}"
        es_status = "ERROR"

    out.append("")
    out.append(f"[EXTRACT_STRUCTURED]  model={BOX_AI_MODEL}  status={es_status}")
    out.append("-" * 40)
    out.append(es_formatted)
    out.append("")

    print(f"  [{label}] Cleaning up chunk...", flush=True)
    delete_file(session, chunk_file_id)
    chunk_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_range(s: str) -> tuple[int, int]:
    parts = s.strip().split("-")
    if len(parts) == 1:
        p = int(parts[0])
        return p, p
    return int(parts[0]), int(parts[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="MedChron endpoint comparison test")
    parser.add_argument("--token", required=True)
    parser.add_argument("--file-id", required=True, help="Box file ID of the source PDF")
    parser.add_argument(
        "--pages", nargs="+", required=True,
        help="Page ranges to test, e.g. 1-3 2-4 5-7 15-17",
    )
    parser.add_argument("--output-dir", default=".", help="Where to write the comparison .txt")
    args = parser.parse_args()

    ranges = [parse_range(p) for p in args.pages]
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    session = make_session(args.token)

    print("Authenticating...", flush=True)
    user = get_current_user(session)
    print(f"  User: {user.get('name')} ({user.get('login')})", flush=True)

    print(f"\nDownloading source file {args.file_id}...", flush=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="medchron_test_"))
    source_pdf = tmpdir / f"source_{args.file_id}.pdf"
    download_file(session, args.file_id, source_pdf)

    reader = PdfReader(str(source_pdf))
    total_pages = len(reader.pages)
    print(f"  {total_pages} pages total", flush=True)

    print("\nCreating Box scratch folder...", flush=True)
    scratch_folder_id = create_scratch_folder(session)
    print(f"  Folder ID: {scratch_folder_id}", flush=True)

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"endpoint_comparison_{ts}.txt"

    header_lines = [
        "MEDCHRON ENDPOINT COMPARISON TEST",
        f"Run:      {datetime.utcnow().isoformat()}Z",
        f"File ID:  {args.file_id}",
        f"Model:    {BOX_AI_MODEL}",
        f"Ranges:   {', '.join(args.pages)}",
        f"Total pages in source: {total_pages}",
        "",
        "Endpoints compared:",
        "  TEXT_GEN         POST /2.0/ai/text_gen",
        "  EXTRACT_STRUCTURED  POST /2.0/ai/extract_structured",
        "",
    ]

    out_lines: list[str] = header_lines[:]

    try:
        for start, end in ranges:
            run_range(session, reader, scratch_folder_id, args.file_id, start, end, tmpdir, out_lines)
            time.sleep(1)  # avoid rate limits between ranges
    finally:
        print("\nCleaning up scratch folder...", flush=True)
        delete_folder(session, scratch_folder_id)
        source_pdf.unlink(missing_ok=True)
        tmpdir.rmdir() if tmpdir.exists() else None

    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"\nResults written to: {out_path}", flush=True)


if __name__ == "__main__":
    main()
