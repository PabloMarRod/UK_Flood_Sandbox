"""Build an estuary configuration and geometry from Ordnance Survey open data.

    python build_estuary.py <spec.json>

The spec gives: id, name, river_label, river_names, limit_name, start [lat, lng] (tidal limit), end [lat, lng]
(tide gauge), tide_measure, tide_gauge, flows, check_gauges ([name, measure, lat, lng]), os_tiles (100 km squares
for OS OpenMap Local, e.g. ["NZ"]), bed_profile (ASSUMED, mAOD) and optional width limits.

Downloads (cached in ~/.cache/stormlab_os): OS Open Rivers (GB, about 50 MB) and OS OpenMap Local per tile
(70 to 230 MB each). Centreline: OS Open Rivers links with the river's names plus all tidal-river links.
Water: OS OpenMap Local TidalWater and SurfaceWater_Area. Writes data/estuaries/<id>/.
"""
import io, json, sys, zipfile
from pathlib import Path
import requests
from pyproj import Transformer

ROOT = Path(__file__).parent
OS_CACHE = Path.home() / ".cache" / "stormlab_os"
OS_API = "https://api.os.uk/downloads/v1/products"


def _download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    with requests.get(url, stream=True, timeout=900) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20): f.write(chunk)
        tmp.replace(dest)
    return dest


def open_rivers():
    z = _download(f"{OS_API}/OpenRivers/downloads?area=GB&format=GeoPackage&redirect", OS_CACHE / "oprvrs_gb.zip")
    out = OS_CACHE / "oprvrs_gb.gpkg"
    if not out.exists():
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".gpkg"))
            out.write_bytes(zf.read(name))
    return out


def openmap_water(tile):
    try:
        return _openmap_water(tile)
    except requests.HTTPError:          # all-sea squares have no OpenMap Local tile
        return []


def _openmap_water(tile):
    z = _download(f"{OS_API}/OpenMapLocal/downloads?area={tile}&format=ESRI%C2%AE+Shapefile&redirect",
                  OS_CACHE / f"oml_{tile}.zip")
    d = OS_CACHE / f"oml_{tile}"
    if not d.exists():
        d.mkdir()
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if any(k in n for k in (f"{tile}_TidalWater.", f"{tile}_SurfaceWater_Area.")):
                    (d / Path(n).name).write_bytes(zf.read(n))
    return sorted(str(p) for p in d.glob("*.shp"))


def os_tiles_for(bbox):
    """OS 100 km grid squares (e.g. "SJ") covering a BNG bounding box."""
    letters = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
    def tile(e, n):
        first = {(0, 0): "S", (1, 0): "T", (0, 1): "N", (1, 1): "O", (0, 2): "H"}[(int(e // 500000), int(n // 500000))]
        e100, n100 = int(e // 100000) % 5, int(n // 100000) % 5
        return first + letters[(4 - n100) * 5 + e100]
    x0, y0, x1, y1 = bbox
    return sorted({tile(x, y) for x in (x0, (x0 + x1) / 2, x1) for y in (y0, (y0 + y1) / 2, y1)})


def osm_api_geometry(spec, d, bbox_ll, step=0.02):
    """River lines and water outlines from the main OpenStreetMap API in small tiles (when Overpass is down and
    OS data does not cover the area, e.g. Northern Ireland). Writes line.json / water.json (ways only)."""
    import time
    from osm import osm_api_ways
    la0, lo0, la1, lo1 = bbox_ll
    lines, water, seen = [], [], set()
    la = la0
    while la < la1:
        lo = lo0
        while lo < lo1:
            try:
                d_ = osm_api_ways(la, lo, min(la + step, la1), min(lo + step, lo1), keys=("waterway", "natural"))
            except RuntimeError:
                d_ = {"elements": []}
            for e in d_["elements"]:
                if e["id"] in seen: continue
                seen.add(e["id"]); t = e.get("tags", {})
                if t.get("waterway") in ("river", "tidal_channel"): lines.append(e)
                elif t.get("natural") == "water" or t.get("waterway") == "riverbank": water.append(e)
            lo += step; time.sleep(1)
        la += step
    (d / "line.json").write_text(json.dumps({"elements": lines})); (d / "water.json").write_text(json.dumps({"elements": water}))
    return len(lines), len(water)


def build(spec):
    import os_data, estuary
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    bng = lambda la, lo: [round(v, 1) for v in tf(lo, la)]
    s, e = bng(*spec["start"]), bng(*spec["end"])
    pts = [s, e] + [bng(*br["start"]) for br in spec.get("branches", [])]
    pad = spec.get("pad", 6000)
    bbox = (min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad, max(p[0] for p in pts) + pad, max(p[1] for p in pts) + pad)
    d = ROOT / "data" / "estuaries" / spec["id"]; d.mkdir(parents=True, exist_ok=True)
    if spec.get("geometry_source") == "osm_api":
        tf_back = Transformer.from_crs(27700, 4326, always_xy=True).transform
        lo0, la0 = tf_back(bbox[0], bbox[1]); lo1, la1 = tf_back(bbox[2], bbox[3])
        print("OpenStreetMap API (tiles) ...", flush=True)
        for f_ in ("line.geojson", "water.geojson"): (d / f_).unlink(missing_ok=True)
        print("  ways:", osm_api_geometry(spec, d, (la0, lo0, la1, lo1)), flush=True)
    else:
        print("OS Open Rivers ...", flush=True)
        lines = os_data.river_lines(open_rivers(), spec["river_names"], bbox)
        os_data.save_geojson(d / "line.geojson", lines)
        print(f"  {len(lines)} links; OS OpenMap Local water ...", flush=True)
        shps = [p for t in (spec.get("os_tiles") or os_tiles_for(bbox)) for p in openmap_water(t)]
        os_data.save_geojson(d / "water.geojson", os_data.water_polygons(shps, bbox))
    cfg = {k: spec[k] for k in ("id", "name", "river_label", "river_names", "limit_name", "tide_measure", "tide_gauge", "flows",
                                "bed_profile")}
    cfg["branches"] = [dict(name=br["name"], start=bng(*br["start"]), bed_profile=br["bed_profile"]) for br in spec.get("branches", [])]
    cfg.update(start=s, end=e, check_gauges=[dict(name=n, measure=m, x=bng(la, lo)[0], y=bng(la, lo)[1])
                                              for n, m, la, lo in spec.get("check_gauges", [])],
               min_width=spec.get("min_width", 30), default_width=spec.get("default_width", 80),
               max_width=spec.get("max_width", 5000), manning=spec.get("manning", 0.025), bank_height=spec.get("bank_height", 25.0),
               spacing=spec.get("spacing", 250), width_mode=spec.get("width_mode"), max_gap=spec.get("max_gap", 800), corridor_factor=spec.get("corridor_factor", 0.5), defaults=spec.get("defaults", {"river_base": 20, "river_peak": 300}),
               calibration=None, status=spec.get("status", "assumed"), tide_proxy_km=spec.get("tide_proxy_km"),
               _note=spec.get("note", "Bed levels are ASSUMPTIONS (mAOD), not survey data."))
    for fl in cfg["flows"]:
        if isinstance(fl.get("at"), list) and len(fl["at"]) == 2 and abs(fl["at"][0]) < 90:
            fl["at"] = bng(*fl["at"])
    (d / "config.json").write_text(json.dumps(cfg, indent=1))
    (d / "geometry.json").unlink(missing_ok=True)
    g = estuary.geometry(spec["id"])
    print(f"  {g['length'] / 1000:.1f} km, {len(g['stations'])} nodes; widths {[s_['width'] for s_ in g['stations'][::max(1, len(g['stations']) // 8)]]}")
    print("  gauges:", [(c["name"], round(c["ch"] / 1000, 1)) for c in g["config"]["check_gauges"]])
    return g


if __name__ == "__main__":
    build(json.loads(Path(sys.argv[1]).read_text()))
