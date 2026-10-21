"""Northern Ireland: OSNI Open Data 10 m DTM (Open Government Licence), by 1:50 000 sheet.

Six zips of XYZ text (Irish Grid, EPSG:29903) cover Northern Ireland. An index of sheet extents
(data/ni/dtm_index.json) is shipped with the app, so preparing an area downloads only the zip(s) it needs
(about 170 MB each, cached in ~/.cache/stormlab_ni). Heights are converted onto the app's British National
Grid 1 m mosaic by bilinear interpolation of the 10 m grid. Northern Ireland heights refer to the Belfast
datum, not Newlyn (ODN); see the README.
"""
import io, json, re, zipfile
from pathlib import Path
import numpy as np

ROOT = Path(__file__).parent
CACHE = Path.home() / ".cache" / "stormlab_ni"
INDEX = ROOT / "data" / "ni" / "dtm_index.json"
BASE = "https://docs.spatialni.gov.uk/OpenData/OSNI_OpenData_10m_DTM/OSNI_10M_DTM_Sheets_{}.zip"
RANGES = ["1-50", "51-100", "101-150", "151-200", "201-250", "251-293"]
IG = "EPSG:29903"


def _zip(rng, status=lambda m: None):
    import requests
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"sheets_{rng}.zip"
    if not p.exists():
        status(f"Downloading Northern Ireland terrain (sheets {rng}, about 170 MB, once)")
        with requests.get(BASE.format(rng), stream=True, timeout=1800) as r:
            r.raise_for_status(); tmp = p.with_suffix(".part")
            with open(tmp, "wb") as f:
                for ch in r.iter_content(1 << 20): f.write(ch)
            tmp.replace(p)
    return p


def _read_sheet(zf, name):
    a = np.loadtxt(io.TextIOWrapper(zf.open(name)), dtype=np.float64)
    return a[:, 0], a[:, 1], a[:, 2]


def build_index(status=print, ranges=RANGES):
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    for rng in ranges:
        zf = zipfile.ZipFile(_zip(rng, status))
        for n in zf.namelist():
            if not n.lower().endswith(".txt"): continue
            first = last = None
            with zf.open(n) as f:                 # sheets are full rectangles sorted by row: first and last points
                for line in f:
                    if line.strip():
                        if first is None: first = line
                        last = line
            (x0, y0), (x1, y1) = [tuple(map(float, l.split()[:2])) for l in (first, last)]
            idx[n] = dict(zip=rng, bbox=[min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
        status(f"sheets {rng}: indexed")
    INDEX.parent.mkdir(parents=True, exist_ok=True); INDEX.write_text(json.dumps(idx))
    return idx


def fill_dtm(out, x0, y1, status=lambda m: None):
    """Fill NaN cells of a 1 m BNG mosaic from the OSNI 10 m DTM. Returns True if anything was filled."""
    if not INDEX.exists() or not np.isnan(out).any():
        return False
    from pyproj import Transformer
    from scipy.interpolate import RegularGridInterpolator
    h, w = out.shape
    to_ig = Transformer.from_crs(27700, IG, always_xy=True)
    cx = [x0, x0 + w, x0, x0 + w]; cy = [y1, y1, y1 - h, y1 - h]
    ix, iy = to_ig.transform(cx, cy)
    bx0, bx1, by0, by1 = min(ix) - 20, max(ix) + 20, min(iy) - 20, max(iy) + 20
    idx = json.loads(INDEX.read_text())
    pad = 10000          # coastal sheets have ragged rows, so indexed extents can be too small
    sheets = [(n, v) for n, v in idx.items() if not (v["bbox"][2] + pad < bx0 or v["bbox"][0] - pad > bx1
                                                    or v["bbox"][3] + pad < by0 or v["bbox"][1] - pad > by1)]
    if not sheets:
        return False
    xs = np.arange(np.floor(bx0 / 10) * 10 + 5, bx1 + 10, 10.0); ys = np.arange(np.floor(by0 / 10) * 10 + 5, by1 + 10, 10.0)
    grid = np.full((len(ys), len(xs)), np.nan)
    for n, v in sheets:
        status(f"Reading Northern Ireland terrain ({n})")
        x, y, z = _read_sheet(zipfile.ZipFile(_zip(v["zip"], status)), n)
        m = (x >= xs[0]) & (x <= xs[-1]) & (y >= ys[0]) & (y <= ys[-1])
        ci = np.round((x[m] - xs[0]) / 10).astype(int); ri = np.round((y[m] - ys[0]) / 10).astype(int)
        ok = (ci >= 0) & (ci < len(xs)) & (ri >= 0) & (ri < len(ys))
        grid[ri[ok], ci[ok]] = z[m][ok]
    grid[grid < -100] = np.nan
    f = RegularGridInterpolator((ys, xs), grid, bounds_error=False, fill_value=np.nan)
    jj, ii = np.nonzero(np.isnan(out))
    px, py = to_ig.transform(x0 + ii + 0.5, y1 - jj - 0.5)
    out[jj, ii] = f(np.c_[py, px]).astype(np.float32)
    return bool(np.isfinite(out[jj, ii]).any())


# ------------------------------------------------------------------ DfI River Basin LiDAR (1 m, ~14% of NI)
LIDAR_INDEX = ROOT / "data" / "ni" / "lidar_index.json"


def _asc_header(raw):
    h = {}
    for line in raw.split(b"\n")[:7]:
        p = line.split()
        if len(p) == 2:
            try: h[p[0].decode().lower()] = float(p[1])
            except ValueError: pass
    return h


def build_lidar_index(urls, status=print, workers=8):
    """Index every DTM tile in the DfI River Basin LiDAR archives (remote zip listing + first bytes of each tile)."""
    import requests, zlib, re
    from concurrent.futures import ThreadPoolExecutor
    import remotezip_lite as RZ
    s = requests.Session()
    tiles = []
    def head_of(u, e):
        name, off, csize, usize, method = e
        h = RZ._get(u, off, off + 29, s); nlen, elen = struct.unpack("<HH", h[26:30])
        raw = RZ._get(u, off + 30 + nlen + elen, off + 30 + nlen + elen + min(csize, 4096) - 1, s)
        raw = raw if method == 0 else zlib.decompressobj(-15).decompress(raw)
        return _asc_header(raw)
    import struct
    for k, u in enumerate(urls, 1):
        uu = u.replace(" ", "%20")
        try:
            L = RZ.listing(uu, s)
        except Exception as ex:
            status(f"{k}/{len(urls)} {u}: listing failed ({ex})"); continue
        dtm = [e for e in L if e[0].lower().endswith(".asc") and "dtm" in e[0].lower()]
        year = (re.findall(r"_(\d{4})\.zip$", u) or ["0"])[0]
        with ThreadPoolExecutor(workers) as ex:
            heads = list(ex.map(lambda e: (e, head_of(uu, e)), dtm))
        for e, h in heads:
            if {"xllcorner", "yllcorner", "ncols", "nrows", "cellsize"} <= set(h):
                x0, y0, cs = h["xllcorner"], h["yllcorner"], h["cellsize"]
                tiles.append(dict(url=uu, entry=list(e), year=int(year), bbox=[x0, y0, x0 + h["ncols"] * cs, y0 + h["nrows"] * cs]))
        status(f"{k}/{len(urls)} {u.split('/')[-1]}: {len(dtm)} DTM tiles")
    LIDAR_INDEX.parent.mkdir(parents=True, exist_ok=True); LIDAR_INDEX.write_text(json.dumps(tiles))
    return tiles


def fill_dtm_lidar(out, x0, y1, status=lambda m: None):
    """Fill NaN cells of a 1 m BNG mosaic from DfI River Basin LiDAR tiles (Irish Grid), newest first."""
    if not LIDAR_INDEX.exists() or not np.isnan(out).any():
        return False
    import remotezip_lite as RZ
    from pyproj import Transformer
    from scipy.interpolate import RegularGridInterpolator
    h, w = out.shape
    to_ig = Transformer.from_crs(27700, IG, always_xy=True)
    ix, iy = to_ig.transform([x0, x0 + w, x0, x0 + w], [y1, y1, y1 - h, y1 - h])
    bx0, bx1, by0, by1 = min(ix), max(ix), min(iy), max(iy)
    tiles = [t for t in json.loads(LIDAR_INDEX.read_text())
             if not (t["bbox"][2] < bx0 or t["bbox"][0] > bx1 or t["bbox"][3] < by0 or t["bbox"][1] > by1)]
    filled = False
    for t in sorted(tiles, key=lambda t: -t["year"]):
        if not np.isnan(out).any(): break
        status(f"Reading Northern Ireland LiDAR ({t['year']})")
        try:
            raw = RZ.read_member(t["url"], tuple(t["entry"]))
        except Exception:
            continue
        hd = _asc_header(raw[:2000]); nd = hd.get("nodata_value", -9999)
        lines = raw.split(b"\n"); body = b"\n".join(lines[6:]) if len(_asc_header(b"\n".join(lines[:6]))) >= 5 else raw
        a = np.array(body.split(), dtype=np.float64)
        ncols, nrows, cs = int(hd["ncols"]), int(hd["nrows"]), hd["cellsize"]
        if a.size < ncols * nrows: continue
        a = a[:ncols * nrows].reshape(nrows, ncols); a[(a <= -100) | (np.abs(a - nd) < 1e-3 * abs(nd))] = np.nan
        xs = hd["xllcorner"] + (np.arange(ncols) + 0.5) * cs; ys = hd["yllcorner"] + (np.arange(nrows)[::-1] + 0.5) * cs
        f = RegularGridInterpolator((ys[::-1], xs), a[::-1], bounds_error=False, fill_value=np.nan)
        jj, ii = np.nonzero(np.isnan(out))
        px, py = to_ig.transform(x0 + ii + 0.5, y1 - jj - 0.5)
        v = f(np.c_[py, px]); ok = np.isfinite(v)
        if ok.any(): out[jj[ok], ii[ok]] = v[ok].astype(np.float32); filled = True
    return filled
