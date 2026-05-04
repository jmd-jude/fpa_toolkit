"""
MedChron Phase 2 Spike — Medical Chronology PDF Generator

Walks a Box folder, extracts clinically significant events from every PDF
via Box AI text_gen, and renders a chronology PDF.

Usage:
  .venv/bin/python3 python/medchron.py \
    --token <oauth_access_token> \
    --folder-id <fpa_test_folder_id> \
    --output-dir ~/Desktop/medchron_test \
    [--patient-name "Jane Doe"] \
    [--patient-dob "01/15/1972"] \
    [--chunk-pages 10]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from pypdf import PdfReader

sys.path.insert(0, os.path.dirname(__file__))
from medchron_schemas import (
    ChronologyEntry,
    PageCoverage,
    PatientHeader,
    RunManifest,
    UnprocessedFile,
)
from medchron_render import render_medchron_pdf
from medchron_splitter import write_window_pdf

IMAGE_CHAR_THRESHOLD = 50

BOX_API = "https://api.box.com/2.0"
BOX_UPLOAD = "https://upload.box.com/api/2.0"
BOX_AI_TEXT_GEN = f"{BOX_API}/ai/text_gen"
BOX_AI_MODEL = os.environ.get("BOX_AI_MODEL", "google__gemini_2_5_pro")
SCRATCH_FOLDER_NAME = "__medchron_chunks__"

_ANCHOR_PROMPT = """\
You are a medical records analyst extracting a medical chronology.

You are looking at a {window_span}-page window extracted from "{source_file_name}".
Page {anchor_pos} of this window is the ANCHOR PAGE — it corresponds to page {anchor_page} of the source record.
{context_desc}

Extract every clinically significant event that BEGINS on the anchor page (page {anchor_pos} of this window). \
Use the surrounding context pages to understand continuity and fill in details (such as provider name or date \
that may appear only on the preceding page), but do not extract events that started before the anchor page.

Return ONLY a JSON array (no explanation, no markdown fences). Each element must have:
- "event_date": string YYYY-MM-DD, date the event occurred (required)
- "event_date_is_range": boolean, true if the entry spans multiple dates (e.g. a hospital stay)
- "event_date_end": string YYYY-MM-DD or null, end date when event_date_is_range is true
- "event_time": string or null, time of day as shown e.g. "10:32"
- "provider": string or null, provider name and credentials e.g. "Peter Remedios, M.D."
- "facility": string or null, clinic or hospital name
- "medical_information": string, faithful structured summary of clinical content; \
preserve labeled sections (Subjective, Objective, Assessment, Plan) when present
- "is_clinically_significant": boolean

Clinically significant events include: encounters, diagnoses, procedures, labs with abnormal values, \
imaging findings, surgeries, medication changes, hospital admissions/discharges, physical exam findings, \
ED visits, and consultations.

Return [] if the anchor page contains no clinically significant content that begins here. This includes:
- Blank pages, tables of contents, fax headers, cover sheets
- Affidavits, custodian of records declarations, release of medical information forms, authorization forms
- Any page consisting primarily of legal or administrative language rather than clinical documentation
- Continuation pages where the clinical event started on a preceding page

Return ONLY the JSON array. No text before or after it."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def get_current_user(session: requests.Session) -> str:
    r = session.get(f"{BOX_API}/users/me", timeout=30)
    r.raise_for_status()
    return r.json().get("id", "unknown")


def get_folder_name(session: requests.Session, folder_id: str) -> str:
    r = session.get(f"{BOX_API}/folders/{folder_id}", params={"fields": "name"}, timeout=30)
    r.raise_for_status()
    return r.json().get("name", folder_id)


# ---------------------------------------------------------------------------
# Folder walk
# ---------------------------------------------------------------------------

def list_pdf_files(session: requests.Session, folder_id: str) -> list[tuple[str, str]]:
    pdfs: list[tuple[str, str]] = []
    _walk(session, folder_id, pdfs)
    return pdfs


def _walk(session: requests.Session, folder_id: str, pdfs: list) -> None:
    marker = None
    while True:
        params: dict = {"limit": 1000, "fields": "id,name,type"}
        if marker:
            params["marker"] = marker
        r = session.get(f"{BOX_API}/folders/{folder_id}/items", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("entries", []):
            if item["type"] == "file" and item["name"].lower().endswith(".pdf"):
                pdfs.append((item["id"], item["name"]))
                print(f"  Found: {item['name']}", flush=True)
            elif item["type"] == "folder" and item["name"] != SCRATCH_FOLDER_NAME:
                _walk(session, item["id"], pdfs)
        marker = data.get("next_marker")
        if not marker:
            break


# ---------------------------------------------------------------------------
# Scratch folder
# ---------------------------------------------------------------------------

def create_scratch_folder(session: requests.Session, parent_id: str) -> str:
    payload = {"name": SCRATCH_FOLDER_NAME, "parent": {"id": parent_id}}
    r = session.post(f"{BOX_API}/folders", json=payload, timeout=30)
    if r.status_code == 409:
        try:
            return r.json()["context_info"]["conflicts"][0]["id"]
        except (KeyError, IndexError):
            pass
        # fallback: scan parent items
        items = session.get(
            f"{BOX_API}/folders/{parent_id}/items",
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
    session.delete(f"{BOX_API}/folders/{folder_id}", params={"recursive": "true"}, timeout=60)


# ---------------------------------------------------------------------------
# File download / upload / delete
# ---------------------------------------------------------------------------

def download_file(session: requests.Session, file_id: str, dest: Path) -> None:
    r = session.get(f"{BOX_API}/files/{file_id}/content", timeout=120, stream=True)
    r.raise_for_status()
    with dest.open("wb") as fh:
        for chunk in r.iter_content(chunk_size=65536):
            fh.write(chunk)


def upload_chunk(session: requests.Session, folder_id: str, path: Path) -> str:
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


def delete_box_file(session: requests.Session, file_id: str) -> None:
    try:
        session.delete(f"{BOX_API}/files/{file_id}", timeout=30)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Box AI extraction
# ---------------------------------------------------------------------------

def extract_anchor(
    session: requests.Session,
    window_file_id: str,
    source_file_id: str,
    source_file_name: str,
    anchor_page: int,
    anchor_pos: int,
    window_span: int,
) -> list[ChronologyEntry]:
    if window_span == 1:
        context_desc = "There are no context pages."
    elif anchor_pos == 1:
        context_desc = "Page 2 is context only (the following source page). There is no preceding page."
    elif anchor_pos == window_span:
        context_desc = "Page 1 is context only (the preceding source page). There is no following page."
    else:
        context_desc = "Pages 1 and 3 are context only (the preceding and following source pages)."

    base_prompt = _ANCHOR_PROMPT.format(
        window_span=window_span,
        source_file_name=source_file_name,
        anchor_pos=anchor_pos,
        anchor_page=anchor_page,
        context_desc=context_desc,
    )
    prompt = base_prompt

    last_err: str = "unknown"
    for attempt in range(1, 4):
        payload = {
            "items": [{"type": "file", "id": window_file_id}],
            "prompt": prompt,
            "ai_agent": {
                "type": "ai_agent_text_gen",
                "basic_gen": {"model": BOX_AI_MODEL},
            },
        }
        try:
            r = session.post(BOX_AI_TEXT_GEN, json=payload, timeout=180)
            r.raise_for_status()
            raw = r.json().get("answer", "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw.rsplit("```", 1)[0].strip()
            events = json.loads(raw)
            entries: list[ChronologyEntry] = []
            for ev in events:
                if not ev.get("is_clinically_significant", True):
                    continue
                entries.append(ChronologyEntry(
                    event_date=ev["event_date"],
                    event_date_is_range=ev.get("event_date_is_range", False),
                    event_date_end=ev.get("event_date_end"),
                    event_time=ev.get("event_time"),
                    provider=ev.get("provider"),
                    facility=ev.get("facility"),
                    medical_information=ev["medical_information"],
                    first_page=anchor_page,
                    last_page=anchor_page,
                    source_file_id=source_file_id,
                    source_file_name=source_file_name,
                    is_clinically_significant=True,
                    ai_confidence=float(ev.get("ai_confidence", 1.0)),
                ))
            return entries

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            last_err = f"HTTP {status}"
            if e.response is not None and e.response.status_code == 429:
                wait = 15 * attempt
                print(f" [rate limit, waiting {wait}s]", flush=True, end="")
                time.sleep(wait)
            else:
                break

        except (json.JSONDecodeError, ValueError) as e:
            last_err = f"parse error: {e}"
            if attempt < 3:
                prompt = base_prompt + "\n\nReturn ONLY a valid JSON array. No text before or after."
                time.sleep(2)
            else:
                break

        except requests.exceptions.Timeout:
            last_err = "timeout"
            time.sleep(5 * attempt)

        except Exception as e:
            last_err = str(e)
            break

    raise RuntimeError(last_err)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(
    session: requests.Session,
    file_id: str,
    file_name: str,
    scratch_folder_id: str,
    tmpdir: Path,
) -> tuple[list[ChronologyEntry], PageCoverage, list[dict]]:
    local_pdf = tmpdir / f"{file_id}_{file_name}"
    print(f"  Downloading...", flush=True)
    download_file(session, file_id, local_pdf)

    reader = PdfReader(str(local_pdf))
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise RuntimeError("PDF has zero pages")

    cov = PageCoverage(file_id=file_id, file_name=file_name, total_pages=total_pages)
    entries: list[ChronologyEntry] = []
    skipped_chunks: list[dict] = []
    window_dir = tmpdir / f"windows_{file_id}"
    window_dir.mkdir(parents=True, exist_ok=True)

    print(f"  {total_pages} page(s) — extracting page by page:", flush=True)

    for anchor in range(1, total_pages + 1):
        print(f"    p{anchor}...", end=" ", flush=True)

        anchor_chars = len((reader.pages[anchor - 1].extract_text() or ""))
        if anchor_chars < IMAGE_CHAR_THRESHOLD:
            print("SKIPPED (image-only)", flush=True)
            cov.mark_skipped(anchor, anchor)
            skipped_chunks.append({
                "source_file_name": file_name,
                "source_file_id": file_id,
                "source_file_url": f"https://app.box.com/file/{file_id}",
                "pages": str(anchor),
                "reason": f"image-only: {anchor_chars} chars extracted",
                "char_counts": str([anchor_chars]),
            })
            continue

        window_pages: list[int] = []
        if anchor > 1:
            window_pages.append(anchor - 1)
        window_pages.append(anchor)
        anchor_pos = len(window_pages)
        if anchor < total_pages:
            window_pages.append(anchor + 1)

        window_path = window_dir / f"window_p{anchor:05d}.pdf"
        write_window_pdf(reader, window_pages, window_path)

        window_file_id = None
        try:
            window_file_id = upload_chunk(session, scratch_folder_id, window_path)
            page_entries = extract_anchor(
                session, window_file_id,
                file_id, file_name,
                anchor_page=anchor,
                anchor_pos=anchor_pos,
                window_span=len(window_pages),
            )
            entries.extend(page_entries)
            cov.mark_processed(anchor, anchor)
            print(f"{len(page_entries)} event(s)", flush=True)
        except Exception as e:
            print(f"FAILED ({e})", flush=True)
            cov.mark_failed(anchor, anchor, str(e))
        finally:
            if window_file_id:
                delete_box_file(session, window_file_id)
            window_path.unlink(missing_ok=True)

    return entries, cov, skipped_chunks


# ---------------------------------------------------------------------------
# Merge / dedupe / sort
# ---------------------------------------------------------------------------

def dedupe_and_sort(entries: list[ChronologyEntry]) -> list[ChronologyEntry]:
    seen: set[tuple] = set()
    unique: list[ChronologyEntry] = []
    for e in entries:
        key = (
            e.source_file_id,
            e.event_date.isoformat(),
            e.first_page,
            hashlib.md5(e.medical_information.encode()).hexdigest()[:8],
        )
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return sorted(unique, key=lambda e: (e.event_date, e.first_page))


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "source_file_name", "source_file_id", "source_file_url", "pages",
    "event_date", "event_time", "provider", "facility",
    "medical_information", "ai_confidence",
]


def write_csv(entries: list[ChronologyEntry], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for e in entries:
            writer.writerow({
                "source_file_name": e.source_file_name,
                "source_file_id": e.source_file_id,
                "source_file_url": f"https://app.box.com/file/{e.source_file_id}",
                "pages": e.page_label,
                "event_date": e.event_date.strftime("%m/%d/%Y"),
                "event_time": e.event_time or "",
                "provider": e.provider or "",
                "facility": e.facility or "",
                "medical_information": e.medical_information,
                "ai_confidence": e.ai_confidence,
            })


_UNPROCESSED_FIELDS = [
    "source_file_name", "source_file_id", "source_file_url", "pages", "reason", "char_counts",
]


def write_unprocessed_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_UNPROCESSED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="MedChron — medical chronology PDF generator")
    parser.add_argument("--token", required=True, help="Box OAuth access token")
    parser.add_argument("--folder-id", required=True, help="Box folder ID containing medical PDFs")
    parser.add_argument("--output-dir", required=True, help="Local directory for output PDF")
    parser.add_argument("--patient-name", default="", help="Patient full name or initials")
    parser.add_argument("--patient-dob", default="", help="Patient DOB MM/DD/YYYY or YYYY-MM-DD")
    parser.add_argument("--context-pages", type=int, default=1, help="Context pages on each side of anchor (default 1, not yet tunable)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    session = make_session(args.token)

    print("Authenticating...", flush=True)
    try:
        user_id = get_current_user(session)
        print(f"  User ID: {user_id}", flush=True)
    except Exception as e:
        print(f"Authentication failed: {e}", flush=True)
        sys.exit(1)

    folder_name = get_folder_name(session, args.folder_id)
    patient_name = args.patient_name or folder_name
    patient = PatientHeader(
        patient_name=patient_name,
        dob=args.patient_dob if args.patient_dob else None,
    )

    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.utcnow()

    print(f"\nFolder: {folder_name} ({args.folder_id})", flush=True)
    print("Walking for PDFs...", flush=True)
    pdf_files = list_pdf_files(session, args.folder_id)
    if not pdf_files:
        print("No PDFs found.", flush=True)
        sys.exit(1)
    print(f"Found {len(pdf_files)} PDF(s)\n", flush=True)

    tmpdir = Path(tempfile.mkdtemp(prefix="medchron_"))
    scratch_folder_id: str | None = None
    all_entries: list[ChronologyEntry] = []
    coverage_list: list[PageCoverage] = []
    unprocessed: list[UnprocessedFile] = []
    all_skipped_chunks: list[dict] = []

    try:
        print("Creating Box scratch folder...", flush=True)
        scratch_folder_id = create_scratch_folder(session, args.folder_id)
        print(f"  ID: {scratch_folder_id}\n", flush=True)

        for file_id, file_name in pdf_files:
            print(f"[{file_name}]", flush=True)
            try:
                file_entries, cov, file_skipped = process_file(
                    session, file_id, file_name,
                    scratch_folder_id, tmpdir,
                )
                all_entries.extend(file_entries)
                coverage_list.append(cov)
                all_skipped_chunks.extend(file_skipped)
                pct = cov.coverage_ratio() * 100
                skipped_pg_count = len(cov.skipped_pages)
                if skipped_pg_count:
                    print(f"  Done: {len(file_entries)} event(s), {pct:.1f}% coverage ({skipped_pg_count} pages skipped — image-only)\n", flush=True)
                else:
                    print(f"  Done: {len(file_entries)} event(s), {pct:.1f}% coverage\n", flush=True)
                missing = cov.missing_pages()
                if missing:
                    label = str(missing[:10]) + ("..." if len(missing) > 10 else "")
                    unprocessed.append(UnprocessedFile(
                        file_id=file_id, file_name=file_name,
                        reason=f"pages not extracted: {label}",
                    ))
            except Exception as e:
                print(f"  SKIPPED — {e}\n", flush=True)
                unprocessed.append(UnprocessedFile(
                    file_id=file_id, file_name=file_name, reason=str(e),
                ))

    finally:
        if scratch_folder_id:
            print("Deleting scratch folder...", flush=True)
            try:
                delete_folder(session, scratch_folder_id)
                print("  Done.\n", flush=True)
            except Exception as e:
                print(f"  Warning: could not delete scratch folder: {e}\n", flush=True)
        shutil.rmtree(tmpdir, ignore_errors=True)

    all_entries = dedupe_and_sort(all_entries)
    finished_at = datetime.utcnow()

    manifest = RunManifest(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        source_folder_id=args.folder_id,
        as_user_id=user_id,
        patient=patient,
        coverage=coverage_list,
        unprocessed=unprocessed,
        total_entries=len(all_entries),
    )

    safe_name = patient_name.replace(" ", "_").replace("/", "_")[:40]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    stem = f"MedChron_{safe_name}_{ts}"
    out_path = output_dir / f"{stem}.pdf"
    csv_path = output_dir / f"{stem}.csv"

    print(f"Writing CSV → {csv_path}", flush=True)
    write_csv(all_entries, csv_path)

    unprocessed_csv_path: Path | None = None
    if all_skipped_chunks:
        unprocessed_csv_path = output_dir / f"{stem}_unprocessed.csv"
        write_unprocessed_csv(all_skipped_chunks, unprocessed_csv_path)
        print(f"Unprocessed manifest → {unprocessed_csv_path}", flush=True)

    print(f"Rendering PDF → {out_path}", flush=True)
    render_medchron_pdf(out_path, patient, all_entries, manifest)

    print("Uploading outputs to Box...", flush=True)
    box_pdf_url = box_csv_url = ""
    try:
        pdf_file_id = upload_chunk(session, args.folder_id, out_path)
        box_pdf_url = f"https://app.box.com/file/{pdf_file_id}"
        print(f"  PDF → {box_pdf_url}", flush=True)
    except Exception as e:
        print(f"  Warning: PDF upload failed: {e}", flush=True)
    try:
        csv_file_id = upload_chunk(session, args.folder_id, csv_path)
        box_csv_url = f"https://app.box.com/file/{csv_file_id}"
        print(f"  CSV → {box_csv_url}", flush=True)
    except Exception as e:
        print(f"  Warning: CSV upload failed: {e}", flush=True)

    overall_pct = manifest.overall_coverage_ratio() * 100
    total_skipped_pages = sum(len(c.skipped_pages) for c in coverage_list)
    summary = {
        "run_id": run_id,
        "output_pdf": str(out_path),
        "csv_path": str(csv_path),
        "box_pdf_url": box_pdf_url,
        "box_csv_url": box_csv_url,
        "total_entries": len(all_entries),
        "coverage_pct": round(overall_pct, 2),
        "files_processed": len(pdf_files),
        "skipped_pages_count": total_skipped_pages,
        "unprocessed_csv_path": str(unprocessed_csv_path) if unprocessed_csv_path else None,
        "unprocessed_count": len(unprocessed),
        "unprocessed": [
            {"file_id": u.file_id, "file_name": u.file_name, "reason": u.reason}
            for u in unprocessed
        ],
    }
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
