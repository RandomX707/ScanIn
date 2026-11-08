from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from packages.core.adapters.csv_adapter.adapter import CsvAdapter
from packages.core.adapters.json_adapter.adapter import JsonAdapter
from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import CapabilityProfile


@dataclass(frozen=True)
class AdapterSelection:
    profile: CapabilityProfile
    media_type: str
    findings: tuple[str, ...]
    score: float


class AdapterRegistry:
    """Content-first format selection with conservative unsupported results."""

    _extensions = {
        "xlsx": {".xlsx"}, "csv": {".csv"}, "json": {".json"},
        "pdf": {".pdf"}, "image": {".png", ".jpg", ".jpeg", ".gif"},
        "docx": {".docx"}, "pptx": {".pptx"},
    }

    def __init__(self, *, csv_adapter: CsvAdapter | None = None, json_adapter: JsonAdapter | None = None,
                 xlsx_adapter: XlsxAdapter | None = None):
        self.csv = csv_adapter or CsvAdapter()
        self.json = json_adapter or JsonAdapter()
        self.xlsx = xlsx_adapter or XlsxAdapter()

    def inspect(self, source: bytes, *, filename: str = "", max_expansion_ratio: int = 100) -> AdapterSelection:
        extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
        if source.startswith(b"%PDF-"):
            return self._unsupported("pdf", "application/pdf", "read_only", "adapter_not_implemented", extension)
        if source.startswith(b"\x89PNG\r\n\x1a\n"):
            return self._unsupported("image", "image/png", "read_only", "adapter_not_implemented", extension)
        if source.startswith(b"\xff\xd8\xff"):
            return self._unsupported("image", "image/jpeg", "read_only", "adapter_not_implemented", extension)
        if source.startswith((b"GIF87a", b"GIF89a")):
            return self._unsupported("image", "image/gif", "read_only", "adapter_not_implemented", extension)
        if source.startswith(b"PK\x03\x04"):
            return self._inspect_zip(source, extension, max_expansion_ratio=max_expansion_ratio)

        prefix = source.lstrip(b"\xef\xbb\xbf \t\r\n")
        if prefix.startswith((b"{", b"[")):
            profile = self.json.inspect(source)
            return self._selected(profile, "application/json", extension, score=1.0)

        if self._looks_textual(source):
            profile = self.csv.inspect(source)
            has_delimiter = self._has_delimiter(source)
            if profile.level in {"editable", "read_only"} and (has_delimiter or extension == ".csv"):
                score = 0.92 if extension == ".csv" else 0.8
                return self._selected(profile, "text/csv", extension, score=score)
            text_profile = CapabilityProfile("read_only", ("adapter_not_implemented",), (), (), "text", "0.1.0")
            return self._selected(text_profile, "text/plain", extension, score=0.55 if extension in {".txt", ".log"} else 0.3)

        return AdapterSelection(CapabilityProfile("limited", ("unknown_file_type",), (), (), "limited", "0.1.0"),
                                "application/octet-stream", (), 0.0)

    def _inspect_zip(self, source: bytes, extension: str, *, max_expansion_ratio: int) -> AdapterSelection:
        try:
            with zipfile.ZipFile(io.BytesIO(source), "r") as package:
                infos = package.infolist()
                self._validate_zip_metadata(infos, max_expansion_ratio=max_expansion_ratio)
                names = {item.filename for item in infos}
                content_types = package.read("[Content_Types].xml") if "[Content_Types].xml" in names else b""
        except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, ValueError) as error:
            reason = str(error) if isinstance(error, ValueError) else "invalid_or_unsafe_archive"
            profile = CapabilityProfile("rejected", (reason,), (), (), "registry", "0.1.0")
            return AdapterSelection(profile, "application/zip", (), 1.0)

        if "word/document.xml" in names:
            return self._unsupported("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                     "read_only", "adapter_not_implemented", extension)
        if "ppt/presentation.xml" in names:
            return self._unsupported("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                                     "read_only", "adapter_not_implemented", extension)
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            profile = self.xlsx.inspect(source)
            macro = b"macroEnabled" in content_types or "xl/vbaProject.bin" in names
            media = ("application/vnd.ms-excel.sheet.macroEnabled.12" if macro else
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            return self._selected(profile, media, extension, score=1.0)
        profile = CapabilityProfile("limited", ("unsupported_archive",), (), (), "limited", "0.1.0")
        return self._selected(profile, "application/zip", extension, score=0.5)

    def _selected(self, profile: CapabilityProfile, media: str, extension: str, *, score: float) -> AdapterSelection:
        adapter_key = profile.adapter.lower()
        expected = self._extensions.get(adapter_key, set())
        findings = ("extension_mismatch",) if extension and expected and extension not in expected else ()
        return AdapterSelection(profile, media, findings, score)

    def _unsupported(self, key: str, media: str, level: str, reason: str, extension: str) -> AdapterSelection:
        profile = CapabilityProfile(level, (reason,), (), (), key, "0.1.0")
        return self._selected(profile, media, extension, score=1.0)

    @staticmethod
    def _validate_zip_metadata(infos: list[zipfile.ZipInfo], *, max_expansion_ratio: int) -> None:
        if len(infos) > 5000:
            raise ValueError("zip_member_count_limit")
        total = 0
        names: set[str] = set()
        for info in infos:
            if info.filename in names:
                raise ValueError("duplicate_archive_member")
            names.add(info.filename)
            normalized = posixpath.normpath(info.filename)
            if (info.filename.startswith(("/", "\\")) or "\\" in info.filename
                    or normalized == ".." or normalized.startswith("../")):
                raise ValueError("unsafe_archive_member")
            if info.flag_bits & 1:
                raise ValueError("encrypted_archive")
            if info.filename.lower().endswith((".zip", ".jar")):
                raise ValueError("nested_archive_unsupported")
            total += info.file_size
            if total > 250 * 1024 * 1024:
                raise ValueError("zip_expanded_size_limit")
            if info.file_size and info.file_size / max(info.compress_size, 1) > max_expansion_ratio:
                raise ValueError("zip_expansion_limit")

    @staticmethod
    def _looks_textual(data: bytes) -> bool:
        sample = data[:8192]
        if not sample or b"\x00" in sample:
            return False
        return sum(byte < 9 or 13 < byte < 32 for byte in sample) < max(1, len(sample) // 20)

    @staticmethod
    def _has_delimiter(data: bytes) -> bool:
        sample = data[:8192]
        first_line = sample.splitlines()[0] if sample.splitlines() else sample
        return any(delimiter in first_line for delimiter in (b",", b";", b"\t", b"|"))
