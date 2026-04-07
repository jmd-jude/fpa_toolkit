"""
Fetch and cache Box API documentation for use as LLM context.

Fetches specific Box developer docs (publicly accessible markdown endpoints)
and writes them into grouped files under LLMs/.

Output files:
  LLMs/box-core-auth.txt    — OAuth2, token refresh, downscoping, users/me
  LLMs/box-core-ai.txt      — AI extract structured, agent overrides, model cards, rate limits
  LLMs/box-core-files.txt   — Files, folders, uploads, downloads
  LLMs/box-core-picker.txt  — Content Picker UI element, CORS

Usage:
  .venv/bin/python3 scripts/fetch_box_docs.py
  .venv/bin/python3 scripts/fetch_box_docs.py --output-dir LLMs
  .venv/bin/python3 scripts/fetch_box_docs.py --dry-run
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Doc groups — each entry is (display_title, url)
# URLs are the .md endpoints served by developer.box.com
# ---------------------------------------------------------------------------

AUTH_DOCS = [
    ("OAuth 2.0 Auth overview",          "https://developer.box.com/guides/authentication/oauth2/index.md"),
    ("OAuth 2.0 without SDKs",           "https://developer.box.com/guides/authentication/oauth2/without-sdk.md"),
    ("Refresh a Token",                   "https://developer.box.com/guides/authentication/tokens/refresh.md"),
    ("Access Tokens",                     "https://developer.box.com/guides/authentication/tokens/access-tokens.md"),
    ("Downscope a Token",                 "https://developer.box.com/guides/authentication/tokens/downscope.md"),
    ("Request access token (reference)",  "https://developer.box.com/reference/post-oauth2-token.md"),
    ("Refresh access token (reference)",  "https://developer.box.com/reference/post-oauth2-token--refresh.md"),
    ("Get current user (reference)",      "https://developer.box.com/reference/get-users-me.md"),
    ("Users overview",                    "https://developer.box.com/guides/users/index.md"),
]

AI_DOCS = [
    ("Extract metadata (structured) — reference",  "https://developer.box.com/reference/post-ai-extract-structured.md"),
    ("Extract metadata structured — tutorial",      "https://developer.box.com/guides/box-ai/ai-tutorials/extract-metadata-structured.md"),
    ("AI agent model overrides — guide",            "https://developer.box.com/guides/box-ai/ai-agents/index.md"),
    ("Override AI model configuration",             "https://developer.box.com/guides/box-ai/ai-agents/ai-agent-overrides.md"),
    ("Box AI overview",                             "https://developer.box.com/guides/box-ai/index.md"),
    ("Google Gemini 2.5 Pro model card",            "https://developer.box.com/guides/box-ai/ai-models/google-gemini-2-5-pro-model-card.md"),
    ("Supported AI models",                         "https://developer.box.com/guides/box-ai/ai-models/index.md"),
    ("Rate Limits",                                 "https://developer.box.com/guides/api-calls/permissions-and-errors/rate-limits.md"),
]

FILES_DOCS = [
    ("Get file information (reference)",   "https://developer.box.com/reference/get-files-id.md"),
    ("Download file (reference)",          "https://developer.box.com/reference/get-files-id-content.md"),
    ("Upload file (reference)",            "https://developer.box.com/reference/post-files-content.md"),
    ("List items in folder (reference)",   "https://developer.box.com/reference/get-folders-id-items.md"),
    ("Files overview",                     "https://developer.box.com/guides/files/get.md"),
    ("Folders overview",                   "https://developer.box.com/guides/folders/single/index.md"),
    ("Direct uploads overview",            "https://developer.box.com/guides/uploads/direct/index.md"),
    ("Upload new file — guide",            "https://developer.box.com/guides/uploads/direct/file.md"),
    ("Download file — guide",              "https://developer.box.com/guides/downloads/file.md"),
]

PICKER_DOCS = [
    ("Content Picker",          "https://developer.box.com/guides/embed/ui-elements/picker.md"),
    ("UI Elements overview",    "https://developer.box.com/guides/embed/ui-elements/index.md"),
    ("UI Elements installation","https://developer.box.com/guides/embed/ui-elements/installation.md"),
    ("Cross-Origin Resource Sharing (CORS)", "https://developer.box.com/guides/security/cors.md"),
]

GROUPS = [
    ("box-core-auth.txt",   "Box API — Auth, Tokens & Users",         AUTH_DOCS),
    ("box-core-ai.txt",     "Box API — AI Extract Structured",        AI_DOCS),
    ("box-core-files.txt",  "Box API — Files, Folders & Uploads",     FILES_DOCS),
    ("box-core-picker.txt", "Box API — Content Picker UI Element",    PICKER_DOCS),
]

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": "fpa-toolkit-doc-fetcher/1.0"}
RETRY_DELAYS = [2, 5, 10]


def fetch_url(url: str, dry_run: bool = False) -> str:
    """Fetch a single URL and return its text content. Retries on 5xx/timeout."""
    if dry_run:
        return f"[dry-run: would fetch {url}]"

    for attempt, delay in enumerate([0] + RETRY_DELAYS, start=1):
        if delay:
            print(f"    Retry {attempt - 1}/3 after {delay}s...", flush=True)
            time.sleep(delay)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code < 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
            # 5xx — will retry
            print(f"    HTTP {resp.status_code} — will retry", flush=True)
        except requests.exceptions.Timeout:
            print("    Timeout — will retry", flush=True)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(str(e)) from e

    raise RuntimeError(f"Failed after {len(RETRY_DELAYS) + 1} attempts: {url}")


def build_output(group_title: str, docs: list, dry_run: bool) -> tuple[str, list[str]]:
    """
    Fetch all docs in a group and assemble the output string.
    Returns (content, list_of_failed_urls).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_list = "\n".join(f"  {url}" for _, url in docs)

    header = (
        f"# {group_title}\n"
        f"# Generated: {ts}\n"
        f"# Sources:\n{source_list}\n"
        f"# {'=' * 74}\n\n"
    )

    sections = []
    failures = []

    for title, url in docs:
        print(f"  Fetching: {title}", flush=True)
        try:
            content = fetch_url(url, dry_run=dry_run)
            sections.append(
                f"## {title}\n## Source: {url}\n\n{content.strip()}\n"
            )
        except RuntimeError as e:
            print(f"    FAILED: {e}", flush=True)
            sections.append(
                f"## {title}\n## Source: {url}\n\n[FETCH FAILED: {e}]\n"
            )
            failures.append(url)

    return header + "\n\n".join(sections), failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Box API docs for LLM context")
    parser.add_argument(
        "--output-dir",
        default="LLMs",
        help="Output directory (default: LLMs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be fetched without making HTTP requests",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_failures = []

    for filename, group_title, docs in GROUPS:
        out_path = output_dir / filename
        print(f"\n[{group_title}] → {out_path}", flush=True)

        content, failures = build_output(group_title, docs, dry_run=args.dry_run)
        total_failures.extend(failures)

        if not args.dry_run:
            out_path.write_text(content, encoding="utf-8")
            size_kb = round(out_path.stat().st_size / 1024, 1)
            print(f"  Written: {out_path} ({size_kb} KB)", flush=True)

    total_urls = sum(len(docs) for _, _, docs in GROUPS)
    succeeded = total_urls - len(total_failures)

    print(f"\nDone. {succeeded}/{total_urls} URLs fetched successfully.", flush=True)
    if total_failures:
        print("Failed URLs:", flush=True)
        for url in total_failures:
            print(f"  {url}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
