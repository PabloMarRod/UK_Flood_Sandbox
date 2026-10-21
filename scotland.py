"""Scotland: Scottish Remote Sensing Portal LiDAR (public S3 archive, Open Government Licence) and SEPA river data.

LiDAR tiles are Cloud Optimised GeoTIFFs, read by window over HTTP (no full downloads). An index of tile names
per programme (data/scotland/lidar_index.json) is built once with build_index() and shipped with the app.
Priority: National LiDAR Programme (50 cm, newest), then phases 6 to 1.
"""
import json, os, re, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).parent
S3 = "https://srsp-open-data.s3.eu-west-2.amazonaws.com"
PROGRAMMES = ["national-lidar-programme", "phase-6", "phase-5", "phase-4", "phase-3", "phase-2", "phase-1",
              "outer-hebrides", "orkney-islands-council-23", "coastal"]
INDEX = ROOT / "data" / "scotland" / "lidar_index.json"
SEPA = "https://timeseries.sepa.org.uk/KiWIS/KiWIS"
LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


def _square_origin(sq):
    """British National Grid origin (m) of a two-letter 100 km square such as 'NT'."""
    first = {"S": (0, 0), "T": (5, 0), "N": (0, 5), "O": (5, 5), "H": (0, 10), "J": (5, 10)}[sq[0]]
    i = LETTERS.index(sq[1]); row, col = divmod(i, 5)
    return (first[0] + col) * 100000, (first[1] + 4 - row) * 100000


def name_box(fname):
    """Approximate tile box from its name: NT27 (10 km), NT27SE (5 km), NT2574 (1 km)."""
    m = re.match(r"([A-Z]{2})(\d+)(NE|NW|SE|SW)?_", fname.upper())
    if not m: return None
    sq, dig, quad = m.groups()
    try:
        e0, n0 = _square_origin(sq)
    except (KeyError, ValueError):
        return None
    h = len(dig) // 2; step = 10 ** (5 - h)
    e, n = e0 + int(dig[:h]) * step, n0 + int(dig[h:]) * step; size = step
    if quad:
        size = step // 2; e += size if quad[1] == "E" else 0; n += size if quad[0] == "N" else 0
    return [e, n, e + size, n + size]


def build_index(status=print):
    import requests
    idx = {}
    for prog in PROGRAMMES:
        keys, token = [], None
        while True:
            params = {"list-type": "2", "prefix": f"lidar/{prog}/dtm/27700/gridded/", "max-keys": "1000"}
            if token: params["continuation-token"] = token
            r = requests.get(S3 + "/", params=params, timeout=120)
            keys += [k for k in re.findall(r"<Key>([^<]+\.tif)</Key>", r.text)]
            m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", r.text)
            if not m: break
            token = m.group(1)
        idx[prog] = [[k, name_box(Path(k).name)] for k in keys if name_box(Path(k).name)]
        status(f"{prog}: {len(idx[prog])} tiles")
    INDEX.parent.mkdir(parents=True, exist_ok=True); INDEX.write_text(json.dumps(idx))
    return idx


def fill_dtm(out, x0, y1, status=lambda m: None):
    """Fill NaN cells of a 1 m mosaic (top-left x0, y1) from Scottish LiDAR. Returns the programmes used."""
    if not INDEX.exists() or not np.isnan(out).any():
        return []
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.enums import Resampling
    idx = json.loads(INDEX.read_text())
    h, w = out.shape; x1, y0 = x0 + w, y1 - h
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif", GDAL_HTTP_MAX_RETRY="3")
    if os.environ.get("STORMLAB_UNSAFE_SSL"): env["GDAL_HTTP_UNSAFESSL"] = "YES"     # only for test sandboxes
    used = []
    with rasterio.Env(**env):
        for prog in PROGRAMMES:
            if not np.isnan(out).any(): break
            for key, b in idx.get(prog, []):
                if b[2] <= x0 or b[0] >= x1 or b[3] <= y0 or b[1] >= y1: continue
                status(f"Reading Scottish LiDAR ({prog})")
                try:
                    with rasterio.open(f"/vsicurl/{S3}/{key}") as src:
                        sb = src.bounds
                        ix0, ix1 = max(x0, sb.left), min(x1, sb.right); iy0, iy1 = max(y0, sb.bottom), min(y1, sb.top)
                        if ix1 - ix0 < 1 or iy1 - iy0 < 1: continue
                        c0, r0 = int(round(ix0 - x0)), int(round(y1 - iy1)); cw, rh = int(round(ix1 - ix0)), int(round(iy1 - iy0))
                        win = from_bounds(ix0, iy0, ix1, iy1, src.transform)
                        a = src.read(1, window=win, out_shape=(rh, cw), resampling=Resampling.average).astype(np.float32)
                        nd = src.nodata
                except Exception:
                    continue
                a[(a <= -100) | (a == nd)] = np.nan
                tgt = out[r0:r0 + rh, c0:c0 + cw]; m = np.isnan(tgt) & np.isfinite(a[:tgt.shape[0], :tgt.shape[1]])
                tgt[m] = a[:tgt.shape[0], :tgt.shape[1]][m]
                if m.any() and prog not in used: used.append(prog)
    return used


# ------------------------------------------------------------------ SEPA river flows (KiWIS, open)
def sepa_flow(ts_id, t0, t1):
    """15-minute flows from SEPA's time series service for a timeseries id. Returns {"YYYY-MM-DDTHH:MM": m3/s}."""
    import requests
    r = requests.get(SEPA, params={"service": "kisters", "type": "queryServices", "datasource": "0", "request": "getTimeseriesValues",
                                   "ts_id": str(ts_id), "from": t0.strftime("%Y-%m-%dT%H:%M:%S"), "to": t1.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "format": "json"}, timeout=120)
    d = r.json()
    rows = d[0]["data"] if isinstance(d, list) and d else []
    return {str(t)[:16]: float(v) for t, v in rows if v is not None}


def sepa_find_flow_ts(station_name):
    """Look up a SEPA station's 15-minute flow timeseries id by station name (for configuring estuaries)."""
    import requests
    r = requests.get(SEPA, params={"service": "kisters", "type": "queryServices", "datasource": "0", "request": "getTimeseriesList",
                                   "station_name": station_name, "stationparameter_name": "Q", "format": "json",
                                   "returnfields": "station_name,station_no,ts_id,ts_name,parametertype_name"}, timeout=120)
    return r.json()
