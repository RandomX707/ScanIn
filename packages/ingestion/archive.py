"""Safe ZIP expansion planning: every entry gets an explicit decision, nothing is silently dropped.

Pure and side-effect free (no database, no storage). The API layer feeds each accepted entry through the same
scan -> store -> sandboxed-inspect path as a normal upload. Limits are enforced on the bytes actually read, never on
the sizes an archive declares.
"""
from __future__ import annotations

import io
import posixpath
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import BinaryIO

MAX_ENTRIES = 300                       # entries in the central directory (directories included)
MAX_ENTRY_BYTES = 50 * 1024 * 1024      # same ceiling as a single upload
MAX_TOTAL_BYTES = 250 * 1024 * 1024     # all expanded bytes of one archive
MAX_RATIO = 100                         # per-entry expansion ratio once an entry exceeds RATIO_FLOOR
RATIO_FLOOR = 1 * 1024 * 1024
MAX_PATH_DEPTH = 12
SUPPORTED = {".pdf", ".docx", ".xlsx", ".csv", ".json", ".txt", ".md", ".markdown", ".html", ".htm", ".xml", ".pptx",
             ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
NESTED = {".zip", ".jar", ".war", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".cab", ".iso", ".dmg"}
SYSTEM_JUNK = ("__macosx/", ".ds_store", "thumbs.db", "desktop.ini")
ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

# Stable, user-facing reason codes (the API turns these into plain sentences).
REASONS = {
    "unsafe_path": "The path points outside the archive folder.",
    "invalid_name": "The file name contains characters that can't be used.",
    "symlink": "Symbolic links are never extracted.",
    "special_file": "This entry isn't a regular file.",
    "encrypted_entry": "This file is password-protected, so it can't be imported.",
    "unsupported_compression": "This file uses a compression method that isn't allowed.",
    "nested_archive": "Archives inside archives aren't expanded.",
    "unsupported_type": "This file type can't be imported.",
    "system_metadata": "Operating-system metadata, not a document.",
    "duplicate_entry": "The archive lists this path more than once.",
    "entry_too_large": "This file is too large.",
    "ratio_exceeded": "This file expands far more than a normal document, so it was not extracted.",
    "total_too_large": "The archive would expand beyond the allowed total size.",
    "empty_file": "This file is empty.",
    "read_failed": "This file couldn't be read from the archive.",
}


class ArchiveRejected(Exception):
    """The whole archive is refused (nothing is expanded)."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(code)


@dataclass
class PlannedEntry:
    path: str                    # archive-relative path, forward slashes, exactly as stored (Unicode preserved)
    name: str                    # leaf file name
    folder: str                  # archive-relative parent folder ("" for root)
    size: int                    # declared uncompressed size
    compressed: int
    status: str                  # "accept" | "skipped" | "blocked"
    reason: str | None = None
    info: zipfile.ZipInfo | None = field(default=None, repr=False)

    def public(self) -> dict[str, object]:
        return {"path": self.path, "name": self.name, "folder": self.folder, "size": self.size,
                "status": self.status, "reason": self.reason, "reason_text": REASONS.get(self.reason or "")}


def _normalise(raw: str) -> tuple[str | None, str | None]:
    """Return (safe relative path, None) or (None, reason). Windows separators are treated as separators."""
    if "\x00" in raw or any(ord(c) < 32 for c in raw):
        return None, "invalid_name"
    path = raw.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or path.startswith("//"):
        return None, "unsafe_path"
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or len(parts) > MAX_PATH_DEPTH:
        return None, "unsafe_path"
    if len(parts[-1]) > 255 or not parts[-1].strip():
        return None, "invalid_name"
    return "/".join(parts), None


def plan(source: BinaryIO | bytes) -> list[PlannedEntry]:
    """Classify every entry of an archive. Raises ArchiveRejected for archive-level problems."""
    buf = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        zf = zipfile.ZipFile(buf)
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        raise ArchiveRejected("archive_unreadable", "This archive couldn't be read.") from None
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveRejected("too_many_entries", f"This archive has more than {MAX_ENTRIES} entries.")
    declared = sum(i.file_size for i in infos)
    if declared > MAX_TOTAL_BYTES * 4:          # a wildly inflated header; real totals are enforced while reading
        raise ArchiveRejected("total_too_large", REASONS["total_too_large"])
    seen: set[str] = set()
    out: list[PlannedEntry] = []
    for info in infos:
        if info.is_dir():
            continue
        path, bad = _normalise(info.filename)
        shown = info.filename if bad else path
        name = posixpath.basename(shown or info.filename) or info.filename
        folder = posixpath.dirname(shown) if not bad else ""

        def entry(status: str, reason: str | None = None) -> PlannedEntry:
            return PlannedEntry(shown or info.filename, name, folder, info.file_size, info.compress_size, status, reason, info)

        if bad:
            out.append(entry("blocked", bad))
            continue
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            out.append(entry("blocked", "symlink"))
            continue
        if mode and not (stat.S_ISREG(mode) or mode & 0o170000 == 0):
            out.append(entry("blocked", "special_file"))
            continue
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen:
            out.append(entry("blocked", "duplicate_entry"))
            continue
        seen.add(key)
        low = path.lower()
        if any(low.startswith(j) or f"/{j}" in f"/{low}" for j in SYSTEM_JUNK):
            out.append(entry("skipped", "system_metadata"))
            continue
        if info.flag_bits & 0x1:
            out.append(entry("blocked", "encrypted_entry"))
            continue
        if info.compress_type not in ALLOWED_COMPRESSION:
            out.append(entry("blocked", "unsupported_compression"))
            continue
        ext = posixpath.splitext(low)[1]
        if ext in NESTED:
            out.append(entry("blocked", "nested_archive"))
            continue
        if ext not in SUPPORTED:
            out.append(entry("skipped", "unsupported_type"))
            continue
        if info.file_size == 0:
            out.append(entry("skipped", "empty_file"))
            continue
        if info.file_size > MAX_ENTRY_BYTES:
            out.append(entry("blocked", "entry_too_large"))
            continue
        if info.file_size > RATIO_FLOOR and info.file_size / max(info.compress_size, 1) > MAX_RATIO:
            out.append(entry("blocked", "ratio_exceeded"))
            continue
        out.append(entry("accept"))
    return out


class Budget:
    """Running total of expanded bytes for one archive."""

    def __init__(self, total: int = MAX_TOTAL_BYTES):
        self.remaining = total


def read_entry(zf: zipfile.ZipFile, entry: PlannedEntry, budget: Budget) -> bytes:
    """Decompress one accepted entry, enforcing limits on the bytes actually produced."""
    assert entry.info is not None
    limit = min(MAX_ENTRY_BYTES, budget.remaining)
    chunks, got = [], 0
    try:
        with zf.open(entry.info) as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                got += len(block)
                if got > limit:
                    raise ArchiveRejected("entry_too_large" if got > MAX_ENTRY_BYTES else "total_too_large", "limit")
                if got > RATIO_FLOOR and got / max(entry.compressed, 1) > MAX_RATIO:
                    raise ArchiveRejected("ratio_exceeded", "limit")
                chunks.append(block)
    except ArchiveRejected:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, ValueError, NotImplementedError):
        raise ArchiveRejected("read_failed", "limit") from None
    budget.remaining -= got
    return b"".join(chunks)


def open_archive(source: BinaryIO | bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source)
