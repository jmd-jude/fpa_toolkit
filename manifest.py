"""
Box Folder Manifest Generator

Recursively walks a Box folder and exports two CSVs:
  - box_manifest.csv       — one row per file
  - box_folder_summary.csv — one row per folder with counts and totals

Page counts are inferred from Bates-range filenames where possible
(e.g. EPD000001-EPD000011.pdf → 11 pages). Files without a
recognizable Bates range show 'N/A' in Page Count.

Setup:
  1. Go to https://app.box.com/developers/console
  2. Create Custom App -> User Authentication (OAuth 2.0)
  3. Configuration tab -> Generate Developer Token (valid 60 min)
  4. Paste token and folder ID from the Box URL below

Run:
  1. run python manifest.py
  2. run python report.py
"""

import csv
import re
import time
import collections
import os
import io
import fitz  # pymupdf
from boxsdk import OAuth2, Client

# --- CONFIGURE THESE ---
DEV_TOKEN = 'rDCBcClmnxgjWe4nkHCICT1AZLn7e977'
MASTER_FOLDER_ID = '365775634866'
# -----------------------

REQUEST_DELAY = 0.03  # seconds between API calls — increase if rate limited

# Extensions that can never have pages — skip Bates inference for these
NON_PAGE_EXTENSIONS = {
    '.wav', '.mp3', '.mp4', '.avi', '.mov', '.mkv',
    '.flac', '.aac', '.ogg', '.wma', '.m4a', '.m4v',
    '.wmv', '.mpg', '.mpeg', '.webm', '.m4b',
    '.zip', '.gz', '.tar', '.rar', '.7z', 'html', '.json','.wgva','.xml'
}


def infer_page_count_from_bates(filename):
    """
    Infer page count from a Bates range encoded in the filename.

    Two strategies, tried in order:

    Strategy 1 — Classic padded Bates at end of name:
      EPD000001-EPD000011.pdf      →  11 pages
      JD1GREER-000769-000772.PDF   →   4 pages
      Both sides must have >=4 zero-padded digits trailing.

    Strategy 2 — Embedded range anywhere in the name:
      LOC 844-2445 C. Richardson Vol. 1_Redacted.pdf   →  1602 pages
      LOC 8586-14881 C. Richardson Vol. 4_Redacted.pdf →  6296 pages
      Scans for any NUMBER-NUMBER pair; filters out date components.

    Does NOT match:
      LOC_112167.pdf               →  single stamp, no range
      2024-03-26 Plf's Answers.pdf →  date prefix, filtered out
      JD6BGREER-000001.pdf         →  single number, no range

    Returns int or None.
    """
    stem = os.path.splitext(filename)[0]

    # --- Strategy 1: padded Bates at end — PREFIX000001-PREFIX000011 ---
    parts = stem.rsplit('-', 1)
    if len(parts) == 2:
        left, right = parts
        lm = re.search(r'(\d+)$', left)
        rm = re.search(r'(\d+)$', right)
        if lm and rm:
            l_str, r_str = lm.group(1), rm.group(1)
            if len(l_str) >= 4 and len(r_str) >= 4:
                start, end = int(l_str), int(r_str)
                if 0 <= end - start <= 9999:
                    return end - start + 1

    # --- Strategy 2: embedded range — "LOC 844-2445 Description" ---
    # Find all NUMBER-NUMBER pairs in the stem, with their end positions
    for m in re.finditer(r'(?<!\d)(\d+)\s*-\s*(\d+)(?!\d)', stem):
        start, end = int(m.group(1)), int(m.group(2))

        # Skip year-month patterns (2020-03, 2024-12, etc.)
        if 1900 <= start <= 2099 and 1 <= end <= 31:
            continue
        # Skip month-day patterns (03-26, 11-24, etc.) — both small, diff ≤ 30
        if 1 <= start <= 31 and 1 <= end <= 31 and end - start <= 30:
            continue
        # Skip case number patterns — e.g. OCA-CJ-23-6934_00010
        # If the match is immediately followed by _digits, it's a case number + file counter
        after = stem[m.end():]
        if re.match(r'_\d', after):
            continue
        # Require a plausible page count
        if end >= start and 1 <= end - start + 1 <= 50000:
            return end - start + 1

    return None


def get_page_count_from_pdf(client, file_id):
    """
    Download PDF bytes from Box and return page count via pymupdf.
    Returns int or None if the file can't be parsed.
    """
    try:
        pdf_bytes = client.file(file_id).content()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return None


def walk_box_folder(client, folder_id, path="", on_file=None):
    """
    Recursively walk a Box folder. Returns flat list of file dicts.
    on_file: optional callback(path_str) called for each file found.
             When None, prints to stdout (terminal mode).
    """
    manifest = []

    try:
        folder = client.folder(folder_id=folder_id).get()
        folder_path = f"{path}/{folder.name}" if path else folder.name

        fields = ['id', 'name', 'type', 'size', 'created_at', 'modified_at']
        for item in folder.get_items(limit=1000, fields=fields):
            current_path = f"{folder_path}/{item.name}"

            if item.type == 'file':
                if on_file:
                    on_file(current_path)
                else:
                    print(f"  {current_path}")
                ext = os.path.splitext(item.name)[1].lower() or 'no extension'

                if ext == '.pdf':
                    page_count = get_page_count_from_pdf(client, item.id)
                    if page_count is not None:
                        page_source = 'pdf_parsed'
                    else:
                        page_count = infer_page_count_from_bates(item.name)
                        page_source = 'bates_inferred' if page_count is not None else 'N/A'
                        if page_count is None:
                            page_count = 'N/A'
                else:
                    if ext in NON_PAGE_EXTENSIONS:
                        page_count = 'N/A'
                        page_source = 'N/A'
                    else:
                        page_count = infer_page_count_from_bates(item.name)
                        page_source = 'bates_inferred' if page_count is not None else 'N/A'
                        if page_count is None:
                            page_count = 'N/A'

                raw_size = getattr(item, 'size', None)
                size_kb = round(raw_size / 1024, 1) if raw_size else 'N/A'

                created = getattr(item, 'created_at', None)
                modified = getattr(item, 'modified_at', None)

                manifest.append({
                    'Name': item.name,
                    'Path': current_path,
                    'Folder': folder_path,
                    'Folder ID': folder_id,
                    'Folder URL': f'https://app.box.com/folder/{folder_id}',
                    'Extension': ext,
                    'Page Count': page_count,
                    'Page Count Source': page_source,
                    'Size (KB)': size_kb,
                    'Created': created[:10] if created else 'N/A',
                    'Modified': modified[:10] if modified else 'N/A',
                    'File ID': item.id,
                    'File URL': f'https://app.box.com/file/{item.id}',
                })
                time.sleep(REQUEST_DELAY)

            elif item.type == 'folder':
                manifest.extend(walk_box_folder(client, item.id, folder_path))

    except Exception as e:
        print(f"  Error accessing folder {folder_id}: {e}")

    return manifest


def build_and_write_summary(manifest, summary_file):
    """Build folder-level summary and write to CSV."""
    folders = collections.defaultdict(lambda: {
        'File Count': 0,
        'Known Page Total': 0,
        'NA Count': 0,
        'Total Size KB': 0,
        'File Types': collections.Counter(),
    })

    for row in manifest:
        f = row['Folder']
        folders[f]['File Count'] += 1
        folders[f]['File Types'][row['Extension']] += 1

        if row['Page Count'] == 'N/A':
            folders[f]['NA Count'] += 1
        else:
            folders[f]['Known Page Total'] += int(row['Page Count'])

        if row['Size (KB)'] != 'N/A':
            folders[f]['Total Size KB'] += float(row['Size (KB)'])

    summary = []
    for folder_path, data in sorted(folders.items()):
        depth = folder_path.count('/')
        types_str = ', '.join(
            f"{ext}({n})" for ext, n in data['File Types'].most_common()
        )
        total_kb = data['Total Size KB']
        size_display = (
            f"{round(total_kb / 1024, 1)} MB" if total_kb >= 1024
            else f"{round(total_kb, 1)} KB"
        )
        summary.append({
            'Folder': folder_path,
            'Depth': depth,
            'File Count': data['File Count'],
            'Known Page Total': data['Known Page Total'] if data['Known Page Total'] > 0 else 'N/A',
            'Files Missing Page Count': data['NA Count'],
            'Total Size': size_display,
            'File Types': types_str,
        })

    with open(summary_file, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['Folder', 'Depth', 'File Count', 'Known Page Total',
                      'Files Missing Page Count', 'Total Size', 'File Types']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)

    print(f"  Summary   → {summary_file}")


def print_stats(manifest, output_file):
    na_count = sum(1 for r in manifest if r['Page Count'] == 'N/A')
    known = sum(int(r['Page Count']) for r in manifest if r['Page Count'] != 'N/A')
    bates = sum(1 for r in manifest if r['Page Count Source'] == 'bates_inferred')
    ext_counts = collections.Counter(r['Extension'] for r in manifest)
    folders = len(set(r['Folder'] for r in manifest))

    print(f"\n{'='*50}")
    print(f"  Files found:          {len(manifest)}")
    print(f"  Folders found:        {folders}")
    print(f"  Page count inferred:  {bates} files (bates)")
    print(f"  Known page total:     {known}")
    print(f"  Files missing count:  {na_count} ({round(na_count / len(manifest) * 100)}%)")
    print(f"  File types:           {dict(ext_counts.most_common(5))}")
    print(f"{'='*50}")
    print(f"  Manifest  → {output_file}")


def annotate_duplicates(manifest):
    """
    Adds a 'Duplicate' column to each row — 'Yes' if another row shares
    the same Name + Size (KB), 'No' otherwise.
    Also returns a filtered list of just the duplicate rows.
    """
    key_counts = collections.Counter(
        (r['Name'], r['Size (KB)']) for r in manifest
    )
    for row in manifest:
        row['Duplicate'] = 'Yes' if key_counts[(row['Name'], row['Size (KB)'])] > 1 else 'No'
    duplicates = [r for r in manifest if r['Duplicate'] == 'Yes']
    return manifest, duplicates


def main():
    auth = OAuth2(client_id=None, client_secret=None, access_token=DEV_TOKEN)
    client = Client(auth)

    try:
        me = client.user().get()
        print(f"Authenticated as: {me.name} ({me.login})\n")
    except Exception as e:
        print(f"Authentication failed: {e}")
        print("Make sure your DEV_TOKEN is valid (expires after 60 minutes).")
        return

    print(f"Crawling folder ID: {MASTER_FOLDER_ID}\n")
    manifest = walk_box_folder(client, MASTER_FOLDER_ID)

    if not manifest:
        print("\nNo files found. Check your folder ID and permissions.")
        return

    # Derive output filenames from the case root folder name
    case_root = manifest[0]['Path'].split('/')[0]
    slug = re.sub(r'[^\w]+', '_', case_root).strip('_').lower()
    output_file  = f'{slug}_manifest.csv'
    summary_file = f'{slug}_summary.csv'
    dupes_file   = f'{slug}_duplicates.csv'

    manifest, duplicates = annotate_duplicates(manifest)

    fieldnames = ['Name', 'Path', 'Folder', 'Folder ID', 'Folder URL', 'Extension',
                  'Page Count', 'Page Count Source',
                  'Size (KB)', 'Created', 'Modified', 'File ID', 'File URL', 'Duplicate']

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(manifest)

    dup_rows_sorted = sorted(duplicates, key=lambda r: (r['Name'], r['Size (KB)']))
    with open(dupes_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dup_rows_sorted)
    print(f"  Duplicates → {dupes_file}  ({len(dup_rows_sorted)} instances, "
          f"{len(set((r['Name'], r['Size (KB)']) for r in duplicates))} unique files)")

    build_and_write_summary(manifest, summary_file)
    print_stats(manifest, output_file)


if __name__ == "__main__":
    main()
