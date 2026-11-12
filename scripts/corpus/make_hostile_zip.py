#!/usr/bin/env python3
"""Build a synthetic hostile ZIP for import tests (no real data). usage: make_hostile_zip.py OUT.zip"""
import io, sys, zipfile

def build() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("good/ok-orders.csv", "id,item\n1,widget\n2,gadget\n")
        z.writestr("good/ok-notes.txt", "Synthetic notes for the archive test.\n")
        z.writestr("../evil.csv", "id\n1\n")                                  # zip-slip
        link = zipfile.ZipInfo("link.csv"); link.external_attr = 0o120777 << 16
        z.writestr(link, "/etc/passwd")                                        # symlink
        z.writestr("nested/inner.zip", b"PK\x03\x04nested")                    # archive in archive
        z.writestr("tools/run.exe", b"MZ" + b"0" * 64)                         # unsupported type
        z.writestr("__MACOSX/._ok-orders.csv", b"junk")                        # OS metadata
        z.writestr("secret.csv", "a,b\n1,2\n")
        z.writestr("bomb.csv", b"0" * (120 * 1024 * 1024))                     # declared 120 MB, ~120 KB compressed
    data = bytearray(buf.getvalue())
    for sig, off in ((b"PK\x01\x02", 8), (b"PK\x03\x04", 6)):                   # mark secret.csv encrypted (flag bit 0)
        at = data.find(sig, 0)
        while at != -1:
            name_len = int.from_bytes(data[at + (28 if sig == b"PK\x01\x02" else 26):at + (30 if sig == b"PK\x01\x02" else 28)], "little")
            head = 46 if sig == b"PK\x01\x02" else 30
            if bytes(data[at + head:at + head + name_len]) == b"secret.csv":
                data[at + off] |= 0x1
            at = data.find(sig, at + 4)
    return bytes(data)

if __name__ == "__main__":
    open(sys.argv[1], "wb").write(build())
