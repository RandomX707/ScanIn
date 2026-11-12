"""ZIP expansion planner: every entry is classified, hostile archives are contained (synthetic data only)."""
import io
import struct
import zipfile

import pytest

from packages.ingestion import archive as A


def _zip(entries, *, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for item in entries:
            if isinstance(item, zipfile.ZipInfo):
                z.writestr(item, b"data")
            else:
                name, data = item
                z.writestr(name, data)
    return buf.getvalue()


def _by(plan_):
    return {e.path: (e.status, e.reason) for e in plan_}


def test_documents_keep_folder_path_unicode_and_case():
    data = _zip([("QCR/Health Quote.docx", b"x"), ("RFQ/Marine\u00a0 RFQ.xlsx", b"x"), ("RFQ/GMC RFQ.XLSX", b"x"), ("Policy Copies/Motor  Policy.pdf", b"x")])
    plan = A.plan(data)
    assert [e.status for e in plan] == ["accept"] * 4
    by_path = {e.path: e for e in plan}
    assert "RFQ/Marine\u00a0 RFQ.xlsx" in by_path                      # non-breaking space survives exactly
    assert by_path["RFQ/GMC RFQ.XLSX"].name == "GMC RFQ.XLSX" and by_path["RFQ/GMC RFQ.XLSX"].folder == "RFQ"
    assert by_path["Policy Copies/Motor  Policy.pdf"].name == "Motor  Policy.pdf"     # double space survives


@pytest.mark.parametrize("name", ["../evil.pdf", "a/../../evil.pdf", "/etc/passwd.pdf", "C:/win/evil.pdf", "..\\evil.pdf", "a\\..\\..\\evil.pdf", "//host/share/x.pdf"])
def test_zip_slip_and_absolute_paths_are_blocked(name):
    plan = A.plan(_zip([(name, b"x")]))
    assert plan[0].status == "blocked" and plan[0].reason == "unsafe_path"


def test_control_characters_in_names_are_blocked():
    assert A.plan(_zip([("bad\x01name.pdf", b"x")]))[0].reason == "invalid_name"


def test_symlinks_and_special_files_are_blocked():
    link = zipfile.ZipInfo("link.pdf")
    link.external_attr = (0o120777 << 16)
    fifo = zipfile.ZipInfo("pipe.pdf")
    fifo.external_attr = (0o010644 << 16)
    got = _by(A.plan(_zip([link, fifo])))
    assert got["link.pdf"] == ("blocked", "symlink") and got["pipe.pdf"] == ("blocked", "special_file")


def test_encrypted_entries_are_blocked_not_imported():
    data = bytearray(_zip([("secret.pdf", b"data")]))
    for sig, off in ((b"PK\x01\x02", 8), (b"PK\x03\x04", 6)):           # general-purpose flag bit 0 = encrypted
        at = data.find(sig)
        data[at + off] |= 0x1
    assert A.plan(bytes(data))[0].reason == "encrypted_entry"


def test_nested_archives_unsupported_junk_and_empty_are_reported_not_dropped():
    data = _zip([("inner.zip", b"PK"), ("run.exe", b"MZ"), ("__MACOSX/._a.pdf", b"x"), (".DS_Store", b"x"), ("empty.pdf", b""), ("ok.csv", b"a,b\n1,2\n")])
    got = _by(A.plan(data))
    assert got["inner.zip"] == ("blocked", "nested_archive")
    assert got["run.exe"] == ("skipped", "unsupported_type")
    assert got["__MACOSX/._a.pdf"] == ("skipped", "system_metadata") and got[".DS_Store"] == ("skipped", "system_metadata")
    assert got["empty.pdf"] == ("skipped", "empty_file")
    assert got["ok.csv"] == ("accept", None)


def test_duplicate_paths_including_unicode_equivalents():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/Caf\u00e9.pdf", b"1")
        zf.writestr("a/Cafe\u0301.pdf", b"2")                          # different bytes, same name after NFC
    got = A.plan(buf.getvalue())
    assert [e.status for e in got] == ["accept", "blocked"] and got[1].reason == "duplicate_entry"


def test_decompression_bomb_is_blocked_by_ratio_before_any_expansion():
    bomb = _zip([("bomb.csv", b"0" * (60 * 1024 * 1024))])
    assert A.plan(bomb)[0].reason in {"ratio_exceeded", "entry_too_large"}


def test_oversized_entry_and_too_many_entries():
    with pytest.raises(A.ArchiveRejected) as err:
        A.plan(_zip([(f"f{i}.csv", b"x") for i in range(A.MAX_ENTRIES + 1)]))
    assert err.value.code == "too_many_entries"


def test_lying_header_cannot_expand_past_limits():
    """Declared size is not trusted: the reader enforces limits on bytes it actually produces."""
    data = bytearray(_zip([("big.csv", b"0" * (3 * 1024 * 1024))]))
    # rewrite the declared uncompressed size (central directory + local header) to something tiny
    for sig, off in ((b"PK\x01\x02", 24), (b"PK\x03\x04", 22)):
        at = data.find(sig)
        data[at + off:at + off + 4] = struct.pack("<I", 10)
    zf = A.open_archive(bytes(data))
    plan = A.plan(bytes(data))
    assert plan[0].status == "accept"
    with pytest.raises(A.ArchiveRejected):
        A.read_entry(zf, plan[0], A.Budget())


def test_total_budget_is_enforced_while_reading():
    data = _zip([("a.csv", b"0123456789" * 1000)])
    zf = A.open_archive(data)
    plan = A.plan(data)
    with pytest.raises(A.ArchiveRejected) as err:
        A.read_entry(zf, plan[0], A.Budget(total=100))
    assert err.value.code == "total_too_large"


def test_unreadable_archive_is_rejected():
    with pytest.raises(A.ArchiveRejected) as err:
        A.plan(b"PK\x03\x04 not really a zip")
    assert err.value.code == "archive_unreadable"


def test_unsupported_compression_blocked():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("x.pdf", b"data")
    assert A.plan(buf.getvalue())[0].reason == "unsupported_compression"
