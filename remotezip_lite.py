"""Read the file list of a remote zip, and single members, with HTTP range requests (no full download)."""
import struct, zlib
import requests


def _get(url, start, end, s=None):
    r = (s or requests).get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    r.raise_for_status()
    return r.content


def listing(url, s=None):
    """[(name, local_header_offset, compressed_size, uncompressed_size, method)] for a remote zip."""
    size = int((s or requests).head(url, timeout=60).headers["Content-Length"])
    tail = _get(url, max(0, size - 65558), size - 1, s)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0: raise ValueError("no end of central directory")
    n, cd_size, cd_off = struct.unpack("<HII", tail[i + 10:i + 20])
    if cd_off == 0xFFFFFFFF or n == 0xFFFF:          # ZIP64
        j = tail.rfind(b"PK\x06\x07")
        z64_off = struct.unpack("<Q", tail[j + 8:j + 16])[0]
        z64 = _get(url, z64_off, z64_off + 55, s)
        n, cd_size, cd_off = struct.unpack("<QQQ", z64[32:56])
    cd = _get(url, cd_off, cd_off + cd_size - 1, s)
    out, p = [], 0
    while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", cd[p + 10:p + 12])
        csize, usize = struct.unpack("<II", cd[p + 20:p + 28])
        nlen, elen, clen = struct.unpack("<HHH", cd[p + 28:p + 34])
        off, = struct.unpack("<I", cd[p + 42:p + 46])
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = cd[p + 46 + nlen:p + 46 + nlen + elen]
        if 0xFFFFFFFF in (csize, usize, off):          # ZIP64 extra field
            q = 0
            while q < len(extra):
                hid, hlen = struct.unpack("<HH", extra[q:q + 4]); data = extra[q + 4:q + 4 + hlen]
                if hid == 1:
                    vals = list(struct.unpack("<" + "Q" * (hlen // 8), data)); k = 0
                    if usize == 0xFFFFFFFF: usize = vals[k]; k += 1
                    if csize == 0xFFFFFFFF: csize = vals[k]; k += 1
                    if off == 0xFFFFFFFF: off = vals[k]
                q += 4 + hlen
        out.append((name, off, csize, usize, method))
        p += 46 + nlen + elen + clen
    return out


def read_member(url, entry, s=None):
    name, off, csize, usize, method = entry
    head = _get(url, off, off + 29, s)
    nlen, elen = struct.unpack("<HH", head[26:30])
    data = _get(url, off + 30 + nlen + elen, off + 30 + nlen + elen + csize - 1, s)
    return data if method == 0 else zlib.decompress(data, -15)
