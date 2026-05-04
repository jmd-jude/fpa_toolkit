"""Local PDF chunking for Box AI.

Box AI's structured extraction endpoints have small per-request page limits,
so large multi-hundred/-thousand page medical records can't be extracted in
a single call. We split them into small page-range sub-PDFs locally (after
decrypting if we know the password) and upload each sub-PDF so Box AI only
ever sees a bite-sized file.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PdfChunk:
    """A single chunk of a source PDF."""

    path: Path
    start_page: int  # 1-indexed, inclusive, absolute in source
    end_page: int    # 1-indexed, inclusive, absolute in source


# Box AI extract has ~50 MB per file + per-request limits; stay well under.
_DEFAULT_MAX_CHUNK_BYTES = 40 * 1024 * 1024
_MIN_CHUNK_PAGES = 1


def _write_chunk(
    reader, writer_cls, start: int, end: int, out_path: Path
) -> int:
    writer = writer_cls()
    for p in range(start - 1, end):
        writer.add_page(reader.pages[p])
    with out_path.open("wb") as fh:
        writer.write(fh)
    return out_path.stat().st_size


def split_pdf(
    source_path: Path,
    *,
    chunk_pages: int = 10,
    password: str | None = None,
    output_dir: Path | None = None,
    base_name: str = "chunk",
    max_chunk_bytes: int = _DEFAULT_MAX_CHUNK_BYTES,
) -> list[PdfChunk]:
    """Split ``source_path`` into page-range PDFs sized for Box AI.

    We emit at most ``chunk_pages`` pages per chunk, and if a written chunk
    exceeds ``max_chunk_bytes`` (Box AI's per-file upload limit is ~50 MB for
    extract endpoints) we recursively halve it until each piece fits. If the
    source is encrypted, ``password`` is used to decrypt before writing out
    the chunk PDFs so each chunk is unencrypted and readable by Box AI.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source_path))
    if getattr(reader, "is_encrypted", False):
        if not password:
            raise ValueError(
                f"PDF '{source_path}' is encrypted and no password was provided"
            )
        if not reader.decrypt(password):
            raise ValueError(f"Password did not unlock '{source_path}'")

    total_pages = len(reader.pages)
    if total_pages == 0:
        return []

    out_dir = Path(output_dir) if output_dir else Path(
        tempfile.mkdtemp(prefix="medchron_chunks_")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    def emit(start: int, end: int, chunks: list[PdfChunk]) -> None:
        out_path = out_dir / f"{base_name}_p{start:05d}-{end:05d}.pdf"
        size = _write_chunk(reader, PdfWriter, start, end, out_path)
        span = end - start + 1
        if size > max_chunk_bytes and span > _MIN_CHUNK_PAGES:
            out_path.unlink(missing_ok=True)
            mid = start + span // 2 - 1
            emit(start, mid, chunks)
            emit(mid + 1, end, chunks)
            return
        chunks.append(PdfChunk(path=out_path, start_page=start, end_page=end))

    chunks: list[PdfChunk] = []
    start = 1
    while start <= total_pages:
        end = min(start + chunk_pages - 1, total_pages)
        emit(start, end, chunks)
        start = end + 1

    log.info(
        "Split %s (%d pages) into %d chunks (target %d pages/chunk, max %.0f MB/chunk)",
        source_path.name,
        total_pages,
        len(chunks),
        chunk_pages,
        max_chunk_bytes / 1024 / 1024,
    )
    return chunks


def write_window_pdf(reader, page_numbers: list[int], output_path: Path) -> None:
    """Write a subset of pages from an open PdfReader to a new PDF.

    page_numbers are 1-indexed absolute page numbers from the source document.
    """
    from pypdf import PdfWriter
    writer = PdfWriter()
    for pn in page_numbers:
        writer.add_page(reader.pages[pn - 1])
    with output_path.open("wb") as fh:
        writer.write(fh)
