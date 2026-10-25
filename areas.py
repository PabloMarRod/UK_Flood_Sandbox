"""Model areas: definition, data download (EA LIDAR, OSM), mesh building and map layers.

An area lives in areas/<id>/ with:
  area.json            definition (outline, cell size, grid, features)
  data/dtm.tif         EA LIDAR Composite DTM 1 m (with a margin around the outline)
  data/osm.json        OpenStreetMap buildings, roads, land cover, waterways
  cache/               mesh_block.txt, mesh.npz, mesh_meta.json, static_layers.json
"""
import io, json, math, re, shutil, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).parent
AREAS = ROOT / "areas"
MAX_CELLS = 250_000          # comfortable size: above this the app warns (longer preparation and runs)
HARD_CELLS = 3_000_000       # fallback memory ceiling when free memory cannot be read


def ceiling_cells():
    """Largest area (cells) this computer can hold: 60% of free memory at the measured memory per cell."""
    try:
        import sysinfo, core
        avail = sysinfo.info().get("mem_avail_mb")
        kb = core.calibration().get("mem_kb_per_cell", 3.5)
        if avail:
            return int(min(max((avail * 0.6 - 100) * 1024 / kb, 100_000), 20_000_000))
    except Exception:
        pass
    return HARD_CELLS
WCS = "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs"
COVERAGE = "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m"
ROADW = dict(motorway=14, trunk=11, primary=10, secondary=9, tertiary=8, residential=6, unclassified=6, service=4,
             pedestrian=4, living_street=5, footway=2, cycleway=2.5, path=1.5, steps=2, track=3)
GREENS = {"grass", "park", "pitch", "garden", "allotments", "cemetery", "recreation_ground", "playground",
          "dog_park", "meadow", "village_green", "golf_course", "farmland", "nature_reserve"}


# ------------------------------------------------------------------ definitions
def load(area_id):
    p = AREAS / area_id / "area.json"
    if not p.exists():
        raise ValueError("unknown area")
    A = json.loads(p.read_text()); A["dir"] = str(AREAS / area_id)
    return A


def save(A):
    d = Path(A["dir"]); d.mkdir(parents=True, exist_ok=True)
    (d / "area.json").write_text(json.dumps({k: v for k, v in A.items() if k != "dir"}, indent=1))


def listing():
    out = []
    for p in sorted(AREAS.glob("*/area.json")):
        A = json.loads(p.read_text())
        if not (p.parent / "cache" / "mesh_meta.json").exists():
            continue
        out.append({k: A.get(k) for k in ("id", "name", "kind", "H", "area_km2", "n_cells", "features", "created")})
    return sorted(out, key=lambda a: (a["kind"] != "preset", a.get("created") or 0))


def new_area(name, poly_ll, H):
    """Create an area definition from a lat/lng outline; raises ValueError if too big."""
    from pyproj import Transformer
    from shapely.geometry import Polygon
    if H not in (5, 10):
        raise ValueError("cell size must be 5 or 10 m")
    if len(poly_ll) < 3:
        raise ValueError("draw at least three points")
    tf = Transformer.from_crs(4326, 27700, always_xy=True)
    pts = [tf.transform(lng, lat) for lat, lng in poly_ll]
    poly = Polygon(pts).buffer(0)
    if poly.is_empty or poly.area < 4 * H * H * 100:
        raise ValueError("the area is too small")
    est = 2 * poly.area / H ** 2
    cap = ceiling_cells()
    if est > cap:
        raise ValueError(f"the area would need about {est / 1e6:.1f} million cells, beyond what this computer's free memory can handle "
                         f"(about {cap / 1e6:.1f} million); use 10 m cells or a smaller area, or close other programs")
    x0, y0, x1, y1 = poly.bounds
    X0, Y1 = math.floor(x0 / H) * H, math.ceil(y1 / H) * H
    NX, NY = int(math.ceil((x1 - X0) / H)), int(math.ceil((Y1 - y0) / H))
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "area"
    aid = f"{slug}-{time.strftime('%m%d%H%M%S')}"
    A = dict(id=aid, name=name.strip()[:60] or "My area", kind="drawn", H=H, X0=X0, Y1=Y1, NX=NX, NY=NY,
             poly_bng=[list(p) for p in poly.exterior.coords], poly_ll=poly_ll,
             area_km2=round(poly.area / 1e6, 3), features=dict(river_inflow=False, culvert=False, auto_outflow=True),
             created=time.time(), dir=str(AREAS / aid))
    return A


def prune(keep_id=None):
    """Keep at most one drawn area: delete every other drawn area (and unfinished folders).
    Presets and simulation results in runs/ are never touched."""
    removed = []
    drawn = []
    for p in AREAS.glob("*/area.json"):
        A = json.loads(p.read_text())
        if A.get("kind") == "preset":
            continue
        done = (p.parent / "cache" / "mesh_meta.json").exists()
        drawn.append((A.get("created") or 0, A["id"], done))
    if keep_id is None:                      # keep the newest finished drawn area
        finished = sorted(d for d in drawn if d[2])
        keep_id = finished[-1][1] if finished else None
    for _, aid, _ in drawn:
        if aid != keep_id:
            shutil.rmtree(AREAS / aid, ignore_errors=True); removed.append(aid)
    return removed


def delete(area_id):
    A = load(area_id)
    if A["kind"] == "preset":
        raise ValueError("preset areas cannot be deleted")
    shutil.rmtree(A["dir"])


# ------------------------------------------------------------------ data download
def _bbox(A, margin):
    H = A["H"]
    return (A["X0"] - margin, A["Y1"] - A["NY"] * H - margin, A["X0"] + A["NX"] * H + margin, A["Y1"] + margin)


def fetch_dtm(A, status=lambda m: None, chunk=2500):
    """Download the EA LIDAR DTM 1 m for the area (plus margin), in chunks, and mosaic it."""
    import requests, rasterio
    from rasterio.transform import from_origin
    x0, y0, x1, y1 = [int(round(v)) for v in _bbox(A, 3 * A["H"] + 10)]
    out = np.full((y1 - y0, x1 - x0), np.nan, np.float32)
    tiles = [(x, y) for y in range(y0, y1, chunk) for x in range(x0, x1, chunk)]
    for n, (x, y) in enumerate(tiles, 1):
        status(f"Downloading terrain ({n} of {len(tiles)})")
        xe, ye = min(x + chunk, x1), min(y + chunk, y1)
        params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"), ("CoverageId", COVERAGE),
                  ("format", "image/tiff"), ("subset", f"E({x},{xe})"), ("subset", f"N({y},{ye})")]
        for attempt in range(3):
            try:
                r = requests.get(WCS, params=params, timeout=300)
                if r.ok and r.content[:2] in (b"II", b"MM"):
                    break
            except requests.RequestException:
                pass
            time.sleep(3)
        else:
            continue          # no EA data here (e.g. Wales): leave the gap for the Welsh tiles
        try:
            mf = rasterio.MemoryFile(r.content); src = mf.open()
        except Exception:
            continue
        with mf, src:
            a = src.read(1).astype(np.float32); a[a < -100] = np.nan
            if (a == 0).mean() > 0.5: a[a == 0] = np.nan      # outside England the EA service returns zeros
            b = src.bounds
            r0, c0 = int(round(y1 - b.top)), int(round(b.left - x0))
            h, w = a.shape
            out[r0:r0 + h, c0:c0 + w] = a[:max(0, min(h, out.shape[0] - r0)), :max(0, min(w, out.shape[1] - c0))]
    d = Path(A["dir"]) / "data"; d.mkdir(parents=True, exist_ok=True)
    if np.isnan(out).mean() > 0.01:          # outside England: Welsh Government LiDAR (1 km tiles), DTM and DSM
        dsm = np.full_like(out, np.nan)
        n = _wales_fill(out, dsm, x0, y1, x1, y0, status)
        if n:
            with rasterio.open(d / "dsm.tif", "w", driver="GTiff", height=dsm.shape[0], width=dsm.shape[1], count=1,
                               dtype="float32", crs="EPSG:27700", transform=from_origin(x0, y1, 1, 1), nodata=np.nan,
                               compress="deflate") as dst:
                dst.write(dsm, 1)
            A["terrain_source"] = "Welsh Government LiDAR 2020-23" + (" and EA LIDAR" if np.isfinite(out).mean() < 0.999 else "")
    if np.isnan(out).mean() > 0.01:          # Northern Ireland: OSNI 10 m DTM (by 1:50 000 sheet)
        try:
            import ni
            if ni.fill_dtm_lidar(out, x0, y1, status): A["terrain_source"] = "DfI River Basin LiDAR 1 m (Northern Ireland)"
            if np.isnan(out).mean() > 0.01 and ni.fill_dtm(out, x0, y1, status):
                A["terrain_source"] = (A.get("terrain_source", "") + " and " if A.get("terrain_source") else "") + "OSNI 10 m DTM"
        except Exception:
            pass
    if np.isnan(out).mean() > 0.01:          # Scotland: Scottish Remote Sensing Portal LiDAR (windowed reads)
        try:
            import scotland
            used = scotland.fill_dtm(out, x0, y1, status)
            if used: A["terrain_source"] = "Scottish LiDAR (" + ", ".join(used) + ")"
        except Exception:
            pass
    with rasterio.open(d / "dtm.tif", "w", driver="GTiff", height=out.shape[0], width=out.shape[1], count=1,
                       dtype="float32", crs="EPSG:27700", transform=from_origin(x0, y1, 1, 1), nodata=np.nan,
                       compress="deflate") as dst:
        dst.write(out, 1)


WALES_TILES = "https://datamap.gov.wales/geoserver/ows"


def _wales_fill(out, dsm, x0, y1, x1, y0, status):
    """Fill gaps in the terrain mosaic with Welsh Government LiDAR 1 m tiles (DTM; DSM kept for wall crests)."""
    import requests, rasterio
    try:
        r = requests.get(WALES_TILES, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                         "typeNames": "geonode:welsh_government_lidar_tile_catalogue_2020_2023", "outputFormat": "application/json",
                         "bbox": f"{x0},{y0},{x1},{y1},EPSG:27700", "count": 400}, timeout=120)
        tiles = r.json().get("features", [])
    except Exception:
        return 0
    n = 0
    for k, t in enumerate(tiles, 1):
        p = t["properties"]
        status(f"Downloading Welsh LiDAR ({k} of {len(tiles)})")
        for link, target, gaps_only in ((p.get("dtm_link"), out, True), (p.get("dsm_link"), dsm, False)):
            if not link: continue
            try:
                rr = requests.get("https://" + link.split("://")[-1], timeout=180)
                if rr.status_code != 200: continue
                with rasterio.MemoryFile(rr.content) as mf, mf.open() as src:
                    a = src.read(1).astype(np.float32); a[(a <= -100) | (a == src.nodata)] = np.nan; b = src.bounds
            except Exception:
                continue
            c0, r0 = int(round(b.left - x0)), int(round(y1 - b.top))
            rs, cs = max(r0, 0), max(c0, 0)
            re_, ce = min(r0 + a.shape[0], target.shape[0]), min(c0 + a.shape[1], target.shape[1])
            if re_ <= rs or ce <= cs: continue
            sub = a[rs - r0:re_ - r0, cs - c0:ce - c0]; tgt = target[rs:re_, cs:ce]
            m = np.isnan(tgt) & np.isfinite(sub) if gaps_only else np.isfinite(sub)
            tgt[m] = sub[m]; n += 1
    return n


def fetch_osm(A, status=lambda m: None):
    from pyproj import Transformer
    from osm import overpass
    status("Downloading buildings and land cover")
    x0, y0, x1, y1 = _bbox(A, 50)
    t = Transformer.from_crs(27700, 4326, always_xy=True)
    lo0, la0 = t.transform(x0, y0); lo1, la1 = t.transform(x1, y1)
    bb = f"{la0:.5f},{lo0:.5f},{la1:.5f},{lo1:.5f}"
    q = f"""[out:json][timeout:180];(way["building"]({bb});way["highway"]({bb});way["landuse"]({bb});
way["leisure"]({bb});way["natural"]({bb});way["waterway"]({bb});way["amenity"="parking"]({bb});
relation["natural"="water"]({bb});relation["waterway"="riverbank"]({bb});
nwr["amenity"~"^(school|kindergarten|college|university|hospital|clinic|doctors|nursing_home|social_facility|fire_station|police|townhall)$"]({bb});
nwr["railway"="station"]({bb}););out geom;"""
    d = Path(A["dir"]) / "data"; d.mkdir(parents=True, exist_ok=True)
    try:
        data = overpass(q, tries=1)
    except RuntimeError:
        from osm import osm_api_tiled
        status("Overpass unavailable; downloading from the OpenStreetMap API instead")
        data = osm_api_tiled(la0, lo0, la1, lo1, status=status)
    data["sites_included"] = True                     # key sites (schools, hospitals ...) are part of this download
    (d / "osm.json").write_text(json.dumps(data))


DEFENCES = ("https://environment.data.gov.uk/spatialdata/spatial-flood-defences-including-standardised-attributes/"
            "ogc/features/v1/collections/Spatial_Flood_Defences_Including_Standardised_Attributes/items")


def fetch_defences(A, status=lambda m: None):
    """Environment Agency Spatial Flood Defences (England) inside the area, with crest levels."""
    import requests
    from pyproj import Transformer
    status("Downloading flood defences")
    x0, y0, x1, y1 = _bbox(A, 30)
    t = Transformer.from_crs(27700, 4326, always_xy=True)
    lo0, la0 = t.transform(x0, y0); lo1, la1 = t.transform(x1, y1)
    url, params, feats = DEFENCES, {"f": "json", "bbox": f"{lo0:.6f},{la0:.6f},{lo1:.6f},{la1:.6f}", "limit": 200}, []
    for page in range(60):
        d = None
        for attempt in range(4):               # the service occasionally answers 404; retry
            try:
                r = requests.get(url, params=params, timeout=120)
                if r.status_code == 200: d = r.json(); break
            except Exception:
                pass
            time.sleep(2)
        if d is None:
            feats = None; break                   # defences are optional: carry on without them
        feats += d.get("features", [])
        nxt = [l["href"] for l in d.get("links", []) if l.get("rel") == "next"]
        if not nxt or not d.get("features"): break
        url, params = nxt[0], None
    out = Path(A["dir"]) / "data" / "defences.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"features": feats, "available": feats is not None}))
    # Wales: NRW flood defence locations (no crest levels; estimated later from the LiDAR surface model)
    try:
        r = requests.get(WALES_TILES, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                         "typeNames": "geonode:nrw_flood_defence_locations", "outputFormat": "application/json",
                         "bbox": f"{x0},{y0},{x1},{y1},EPSG:27700", "count": 2000}, timeout=120)
        wf = r.json().get("features", []) if r.status_code == 200 else []
    except Exception:
        wf = []
    (out.parent / "defences_wales.json").write_text(json.dumps({"features": wf}))


def _defence_lines(A):
    """Defence lines (BNG) with a crest level; natural high ground is already in the terrain."""
    from shapely.geometry import shape
    from shapely.ops import transform as sh_tf
    from pyproj import Transformer
    p = Path(A["dir"]) / "data" / "defences.json"
    if not p.exists():
        return [], False
    d = json.loads(p.read_text())
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    out = []
    for f in d.get("features") or []:
        pr = f.get("properties", {}); g = f.get("geometry")
        if not g or "high ground" in str(pr.get("asset_sub_type", "")).lower():
            continue
        levels = [pr.get(k) for k in ("effective_cl", "actual_ucl", "actual_dcl")]
        levels = [v for v in levels if isinstance(v, (int, float)) and -10 < v < 50]
        if not levels:
            levels = [v for v in (pr.get("design_ucl"), pr.get("design_dcl")) if isinstance(v, (int, float)) and -10 < v < 50]
        if not levels:
            continue
        out.append(dict(geom=sh_tf(tf, shape(g)), crest=float(np.mean(levels)), kind=pr.get("asset_sub_type") or "Defence",
                        id=pr.get("asset_id")))
    pw = Path(A["dir"]) / "data" / "defences_wales.json"
    if pw.exists():
        wf = json.loads(pw.read_text()).get("features", [])
        dsm_p = Path(A["dir"]) / "data" / "dsm.tif"
        if wf and dsm_p.exists():
            import rasterio
            ds = rasterio.open(dsm_p); arr = ds.read(1); top_, left_ = ds.bounds.top, ds.bounds.left
            for f in wf:
                pr = f.get("properties", {}); g = f.get("geometry")
                if not g or "high ground" in str(pr.get("subtype", "")).lower(): continue
                geom = shape(g)          # already British National Grid
                vals = []
                for part in getattr(geom, "geoms", [geom]):
                    for dd in np.arange(0, part.length, 2.0):
                        pnt = part.interpolate(dd); r_, c_ = int(top_ - pnt.y), int(pnt.x - left_)
                        if 1 <= r_ < arr.shape[0] - 1 and 1 <= c_ < arr.shape[1] - 1:
                            blk = arr[r_ - 1:r_ + 2, c_ - 1:c_ + 2]; blk = blk[np.isfinite(blk)]
                            if blk.size: vals.append(blk.max())
                if len(vals) >= 3:
                    out.append(dict(geom=geom, crest=float(np.median(vals)), kind=f"{pr.get('subtype') or 'Defence'} (crest estimated from LiDAR)",
                                    id=pr.get("amxid")))
    return out, d.get("available", False)


# ------------------------------------------------------------------ mesh
def build_mesh(A):
    """Triangulated 5 or 10 m grid inside the outline; buildings are holes (walls)."""
    import rasterio
    from rasterio import features
    from rasterio.transform import from_origin
    from scipy import ndimage
    from shapely.geometry import Polygon, LineString
    from shapely.ops import unary_union
    from pyproj import Transformer

    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    F = A["features"]; d = Path(A["dir"])
    (d / "cache").mkdir(parents=True, exist_ok=True)
    src = rasterio.open(d / "data" / "dtm.tif")
    dtm = src.read(1).astype(float); dtm[~np.isfinite(dtm) | (dtm < -100)] = np.nan
    top, left = src.bounds.top, src.bounds.left
    # fill gaps in the terrain data (often foreshore and water) from the nearest valid ground, so they stay in the mesh
    gaps = ~np.isfinite(dtm); filled_frac = float(gaps.mean())
    if gaps.any() and not gaps.all():
        from scipy import ndimage as _nd
        idx_ = _nd.distance_transform_edt(gaps, return_distances=False, return_indices=True)
        dtm = dtm[tuple(idx_)]

    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    bld, roads, green, wood, parking, river, culverts = [], [], [], [], [], [], 0
    bld_tags = []
    river_info = []
    water_polys = []
    from shapely.ops import polygonize, linemerge
    for e in json.loads((d / "data" / "osm.json").read_text())["elements"]:
        g, tg = e.get("geometry"), e.get("tags", {})
        if e["type"] == "relation" and (tg.get("natural") == "water" or tg.get("waterway") == "riverbank"):
            segs = [LineString(list(zip(*tf([p["lon"] for p in m["geometry"]], [p["lat"] for p in m["geometry"]]))))
                    for m in e.get("members", []) if m.get("role") == "outer" and len(m.get("geometry", [])) > 1]
            water_polys += [p_.buffer(0) for p_ in polygonize(unary_union(segs))] if segs else []
            continue
        if not g or e["type"] != "way":
            continue
        xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g])
        pts = list(zip(xs, ys)); closed = len(pts) > 3 and pts[0] == pts[-1]
        if "building" in tg and closed: bld.append(Polygon(pts).buffer(0)); bld_tags.append(tg)
        elif tg.get("highway") in ROADW: roads.append(LineString(pts).buffer(ROADW[tg["highway"]] / 2))
        elif F.get("legacy_rivers") and tg.get("waterway") == "river":
            river.append(LineString(pts))      # preset: mapped river lines as originally built
        elif not F.get("legacy_rivers") and tg.get("waterway") in ("river", "stream", "canal"):
            if tg.get("tunnel") in ("culvert", "yes") or tg.get("culvert"): culverts += 1
            else:
                river.append(LineString(pts))
                river_info.append((LineString(pts), tg.get("name") or ("Unnamed " + tg["waterway"]), tg["waterway"]))
        elif closed and (tg.get("natural") == "water" or tg.get("waterway") == "riverbank"): water_polys.append(Polygon(pts).buffer(0))
        elif closed and tg.get("natural") in ("wood", "scrub"): wood.append(Polygon(pts).buffer(0))
        elif closed and (tg.get("landuse") in GREENS or tg.get("leisure") in GREENS): green.append(Polygon(pts).buffer(0))
        elif closed and tg.get("amenity") == "parking": parking.append(Polygon(pts).buffer(0))
    river_ln = unary_union(river) if river else None

    gt = from_origin(X0, Y1, H, H)
    def ras(gs, at=False, shape=(NY, NX), transform=gt):
        if not gs: return np.zeros(shape, bool)
        return features.rasterize([(x, 1) for x in gs], out_shape=shape, transform=transform, all_touched=at).astype(bool)
    inside = ras([Polygon(A["poly_bng"])])
    import ifcbuilding
    bld, bld_tags = ifcbuilding.override(A, bld, bld_tags)        # imported IFC buildings replace the mapped ones
    b1 = features.rasterize([(x, 1) for x in bld], out_shape=(NY * H, NX * H), transform=from_origin(X0, Y1, 1, 1)) if bld \
        else np.zeros((NY * H, NX * H), np.uint8)
    is_bld = b1.reshape(NY, H, NX, H).mean(axis=(1, 3)) >= 0.5
    is_chan = ras([river_ln.buffer(4)], True) if river_ln is not None else np.zeros((NY, NX), bool)
    n = np.full((NY, NX), 0.05); tag = np.full((NY, NX), "mixed", dtype=object)
    for mask, nn, tg in ((ras(green), 0.035, "green"), (ras(wood), 0.10, "green"),
                         (ras(roads + parking), 0.016, "paved"), (is_chan, 0.04, "chan")):
        n[mask] = nn; tag[mask] = tg

    # tidal Tyne: cells inside the river's water outline get an assumed bed and tidal boundaries
    tyne_poly, est_id = None, None
    if F.get("auto_outflow"):
        try:
            import estuary
            hit = estuary.find_for(Polygon(A["poly_bng"]))
            if hit: est_id, tyne_poly = hit
        except Exception:
            tyne_poly = None
    is_tyne = (ras([tyne_poly]) & inside) if tyne_poly is not None else np.zeros((NY, NX), bool)
    is_bld[is_tyne] = False
    n[is_tyne] = 0.03; tag[is_tyne] = "tidal"

    # vertex elevations: block mean, or block minimum near watercourses (keeps channels continuous)
    rm, rn = H // 2, H - 1
    mean = ndimage.uniform_filter(np.nan_to_num(dtm, nan=0.0), size=2 * rm + 1, mode="nearest")
    cnt = ndimage.uniform_filter(np.isfinite(dtm).astype(float), size=2 * rm + 1, mode="nearest")
    mean = np.where(cnt > 0.999, mean / np.maximum(cnt, 1e-9), np.nan)
    mn = ndimage.minimum_filter(np.where(np.isfinite(dtm), dtm, np.inf), size=2 * rn + 1, mode="nearest")
    mn[~np.isfinite(mn)] = np.nan
    jj, ii = np.mgrid[0:NY + 1, 0:NX + 1]
    rr = np.clip((top - (Y1 - jj * H)).astype(int), 0, dtm.shape[0] - 1)
    cc = np.clip(((X0 + ii * H) - left).astype(int), 0, dtm.shape[1] - 1)
    vz = mean[rr, cc]
    if river_ln is not None:
        near = ras([river_ln.buffer(H + 2)], shape=(NY + 1, NX + 1), transform=from_origin(X0 - H / 2, Y1 + H / 2, H, H))
        vz = np.where(near, mn[rr, cc], vz)
    if tyne_poly is not None:   # replace the LIDAR water surface with the assumed bed (estuary bed profile)
        import estuary
        wv = ras([tyne_poly], shape=(NY + 1, NX + 1), transform=from_origin(X0 - H / 2, Y1 + H / 2, H, H))
        jv, iv = np.nonzero(wv)
        if len(jv):
            vz[jv, iv] = estuary.bed_at_points(est_id, X0 + iv * H, Y1 - jv * H)
    defences, _ = _defence_lines(A) if F.get("defences", True) else ([], False)
    wet_v = ras([tyne_poly], shape=(NY + 1, NX + 1), transform=from_origin(X0 - H / 2, Y1 + H / 2, H, H)) if tyne_poly is not None else None
    n_def = 0
    for dfc in defences:          # walls and embankments: raise the corners of every cell the line crosses
        cells = ras([dfc["geom"].buffer(0.5)], True) & ~is_tyne
        if is_tyne.any():         # walls on the water's edge: use the land cells alongside so there are no gaps
            cells |= ras([dfc["geom"].buffer(H)], True) & ~is_tyne
        jj_, ii_ = np.nonzero(cells)
        if not len(jj_): continue
        n_def += 1
        for dj in (0, 1):
            for di in (0, 1):
                jv_, iv_ = jj_ + dj, ii_ + di
                if wet_v is not None:              # leave river-bed corners alone
                    ok = ~wet_v[jv_, iv_]; jv_, iv_ = jv_[ok], iv_[ok]
                vz[jv_, iv_] = np.maximum(vz[jv_, iv_], dfc["crest"])
        is_bld[cells] = False      # a defence crossing a building footprint must stay in the mesh
    vok = np.isfinite(vz)
    cell_ok = vok[:-1, :-1] & vok[:-1, 1:] & vok[1:, :-1] & vok[1:, 1:]
    keep = inside & ~is_bld & cell_ok

    vid = -np.ones((NY + 1, NX + 1), int); V, T, TN, TT, TSQ = [], [], [], [], []
    def v(j, i):
        if vid[j, i] < 0:
            vid[j, i] = len(V); V.append((X0 + i * H, Y1 - j * H, vz[j, i]))
        return vid[j, i]
    for j in range(NY):
        for i in range(NX):
            if not keep[j, i]: continue
            nw, ne, se, sw = v(j, i), v(j, i + 1), v(j + 1, i + 1), v(j + 1, i)
            for tri in ((sw, se, ne), (sw, ne, nw)):
                T.append(tri); TN.append(n[j, i]); TT.append(tag[j, i]); TSQ.append((j, i))
    TSQ = np.array(TSQ, int).reshape(-1, 2)
    if len(T) == 0:
        raise ValueError("no usable ground in this area (missing terrain data?)")
    if len(T) > MAX_CELLS:
        raise ValueError(f"the area has {len(T):,} cells; the limit is {MAX_CELLS:,}")
    # first triangle index of each kept square
    first = -np.ones((NY, NX), int); first[TSQ[::2, 0], TSQ[::2, 1]] = np.arange(0, len(T), 2)

    meta = dict(n_cells=len(T), area_m2=len(T) * H * H / 2, culverts=culverts, rivers=len(river), bc=[], outflow=[],
                defences=n_def, terrain_filled_pct=round(100 * filled_frac, 1))
    lines_extra = []
    if F.get("river_inflow"):   # preset: gauged inflow where the river crosses the north edge, culvert on the south edge
        north = LineString([(X0, Y1 - .01), (X0 + NX * H, Y1 - .01)])
        south = LineString([(X0, Y1 - NY * H + .01), (X0 + NX * H, Y1 - NY * H + .01)])
        pick = lambda g: g if g.geom_type == "Point" else list(g.geoms)[0]
        ent, ext = pick(river_ln.intersection(north)), pick(river_ln.intersection(south))
        for k, (j, i) in enumerate(TSQ):
            if j <= 1 and abs(X0 + (i + .5) * H - ent.x) <= 7.5: TT[k] = "inflow"
        meta["bc"] = [(int(k), 2) for k, (j, i) in enumerate(TSQ)
                      if j == NY - 1 and k % 2 == 0 and abs(X0 + (i + .5) * H - ext.x) <= 12.5]
        meta.update(ent=[ent.x, ent.y], ext=[ext.x, ext.y], ent_z=float(vz[0, int(round((ent.x - X0) / H))]),
                    ext_z=float(vz[NY, int(round((ext.x - X0) / H))]))
    if F.get("auto_outflow"):   # open the outline wherever the ground falls away outwards
        poly = Polygon(A["poly_bng"])
        ins_, exits, tidal = watercourse_crossings(river_info, poly)
        def outside_z(x, y):
            r, c = int(top - y), int(x - left)
            if not (0 <= r < dtm.shape[0] and 0 <= c < dtm.shape[1]): return np.nan
            return mean[r, c]
        sides = ((0, -1, 1, 1), (0, 1, 0, 0), (-1, 0, 1, 0), (1, 0, 0, 2))   # W, E, N, S: (dj, di, triangle, edge)
        rim = []                      # outline sides: (j, i, k, edge, zc, zo)
        for j, i in zip(*np.nonzero(keep)):
            k0 = first[j, i]; zc = vz[j:j + 2, i:i + 2].mean()
            for dj, di, tri, edge in sides:
                jn, inn = j + dj, i + di
                if 0 <= jn < NY and 0 <= inn < NX and inside[jn, inn]:
                    continue          # neighbour inside the outline: interior edge or building wall
                rim.append((j, i, int(k0 + tri), edge, zc, outside_z(X0 + (inn + .5) * H, Y1 - (jn + .5) * H)))
        cx = lambda j, i: (X0 + (i + .5) * H, Y1 - (j + .5) * H)
        near = lambda j, i, p, r: math.hypot(cx(j, i)[0] - p[0], cx(j, i)[1] - p[1]) <= r
        r_in, r_block, r_out = max(8, 1.6 * H), 2.5 * H + 10, 1.6 * H + 8
        rim_cells = sorted({(j, i) for j, i, *_ in rim})
        inflows = []
        for n_, (p, name, kind) in enumerate(ins_, 1):
            cells = [c for c in rim_cells if near(*c, p, r_in)]
            # spread a river across its channel where it crosses the edge, so large flows enter over the bed's full
            # width rather than through one or two cells: grow along the rim through channel cells
            grown = set(cells)
            if cells:
                zbed = min(float(np.nanmin(vz[j:j + 2, i:i + 2])) for j, i in cells)
                low = lambda c: float(np.nanmin(vz[c[0]:c[0] + 2, c[1]:c[1] + 2])) <= zbed + 1.5
                while True:
                    more = {c for c in rim_cells if c not in grown and near(*c, p, 60) and (TT[first[c]] == "chan" or low(c))
                            and any(abs(c[0] - g[0]) <= 1 and abs(c[1] - g[1]) <= 1 for g in grown)}
                    if not more: break
                    grown |= more
            cells = sorted(grown)
            if not cells:
                cand = sorted(rim_cells, key=lambda c: math.hypot(cx(*c)[0] - p[0], cx(*c)[1] - p[1]))[:1]
                cells = [c for c in cand if near(*c, p, 3 * H)]
            if not cells:
                continue
            tagname = f"in{n_}"
            zs = []
            for j, i in cells:
                for t in (first[j, i], first[j, i] + 1):
                    TT[t] = tagname
                zs.append(np.nanmin(vz[j:j + 2, i:i + 2]))
            jx, ix = cells[0]
            inflows.append(dict(id=tagname, name=name, kind=kind, x=p[0], y=p[1], z=float(min(zs)),
                                cell=[int(jx), int(ix)], n_cells=len(cells), cells=[[int(a), int(b)] for a, b in cells]))
        tyne_edges = []
        if is_tyne.any():
            import estuary
            rest, tid = [], []
            for item in rim:
                (tid if is_tyne[item[0], item[1]] else rest).append(item)
            if tid:
                pts = np.array([cx(j, i) for j, i, *_ in tid])
                nd, _ = estuary.nodes_near(est_id, pts[:, 0], pts[:, 1])
                tyne_edges = [(k, edge, int(n_)) for (j, i, k, edge, zc, zo), n_ in zip(tid, nd)]
            rim = rest
        out = []
        for j, i, k, edge, zc, zo in rim:
            if any(near(j, i, (f["x"], f["y"]), r_block) or
                   any(abs(j - a) <= 2 and abs(i - b) <= 2 for a, b in f.get("cells", ())) for f in inflows):
                continue          # never let injected river water straight back out
            forced = any(near(j, i, p, r_out) for p, _, _ in exits)
            if np.isfinite(zo) and (zo < zc or forced):
                out.append((k, edge, float(np.clip((zc - zo) / H, 0.005 if forced else 0.002, 0.3))))
            elif forced:
                out.append((k, edge, 0.005))
        meta["outflow"] = out
        meta["inflows"] = inflows
        if tyne_edges:
            import estuary
            tt = np.nonzero(np.array([t_ == "tidal" for t_ in TT]))[0]
            chs = np.array([x["ch"] for x in estuary.geometry(est_id)["stations"]])
            cxs = np.array([cx(*TSQ[k]) for k in tt])
            nodes, _ = estuary.nodes_near(est_id, cxs[:, 0], cxs[:, 1])
            zb = np.array([np.mean([V[v][2] for v in T[k]]) for k in tt])
            np.savez_compressed(d / "cache" / "estuary.npz", tris=tt, nodes=nodes, bed=zb)
            used = sorted({e[2] for e in tyne_edges})
            ecfg = estuary.config(est_id)
            meta["estuary"] = dict(id=est_id, name=ecfg["name"], river=ecfg["river_label"], edges=tyne_edges, nodes=used,
                                   n_cells=int(len(tt)), ch_range=[float(chs[used[0]]), float(chs[used[-1]])])
        meta["exits"] = [dict(name=nm, kind=kd, x=p[0], y=p[1]) for p, nm, kd in exits]
        meta["tidal"] = sorted(set(tidal)) if not tyne_edges else []

    (d / "cache").mkdir(exist_ok=True)
    # ---- roofs: rain on building cells (removed from the mesh) is drained by a virtual downpipe into the lowest
    # open cell beside each building, so the model receives the water that falls on roofs
    roofs = []
    if F.get("roofs", True) and is_bld.any():
        from scipy import ndimage as _nd
        removed = is_bld & inside
        if bld:
            ids = features.rasterize([(g_, k + 1) for k, g_ in enumerate(bld)], out_shape=(NY, NX), transform=gt)
        else:
            ids = np.zeros((NY, NX), int)
        lab, nlab = _nd.label(removed)                          # unlabelled removed cells: their connected block
        grp = np.where(ids > 0, ids, 0).astype(np.int64)
        grp[(grp == 0) & removed] = 1_000_000 + lab[(grp == 0) & removed]
        grp[~removed] = 0
        cell_bed = np.where(keep, (vz[:-1, :-1] + vz[:-1, 1:] + vz[1:, :-1] + vz[1:, 1:]) / 4, np.nan)
        # only open ground that belongs to the wider street network: not courtyards or light wells closed in by
        # buildings (in reality those drain to sewers, and here roof water would pile up in them)
        klab, nk = _nd.label(keep)
        ksz = np.bincount(klab.ravel()); ksz[0] = 0
        open_net = np.isin(klab, np.nonzero(ksz >= max(200, 0.02 * keep.sum()))[0])
        taken = set()
        def tag_free(j, i):
            return keep[j, i] and TT[first[j, i]] in ("mixed", "green", "paved", "chan") and (j, i) not in taken
        drains, drain_gids = {}, {}
        for gid in np.unique(grp[grp > 0]):
            cells = np.argwhere(grp == gid)
            j0, i0 = cells.min(0); j1, i1 = cells.max(0)
            found = None
            for r_ in (1, 2, 3, 5, 8, 12):                      # look for open cells round the building
                jj0, ii0, jj1, ii1 = max(j0 - r_, 0), max(i0 - r_, 0), min(j1 + r_, NY - 1), min(i1 + r_, NX - 1)
                win = np.zeros((NY, NX), bool); win[jj0:jj1 + 1, ii0:ii1 + 1] = True
                ring_ = _nd.binary_dilation(grp == gid, iterations=r_) & win & keep & open_net
                cand = np.argwhere(ring_)
                cand = [tuple(c) for c in cand if TT[first[c[0], c[1]]] in ("mixed", "green", "paved", "chan")]
                if cand:
                    found = min(cand, key=lambda c: cell_bed[c]); break
            if found is None: continue
            drains.setdefault(found, 0.0); drains[found] += float(len(cells) * H * H)
            drain_gids.setdefault(found, []).append(int(gid))
        for n_, ((j, i), area) in enumerate(sorted(drains.items()), 1):
            t_ = f"roof{n_}"
            for t in (first[j, i], first[j, i] + 1): TT[t] = t_
            zs = vz[j:j + 2, i:i + 2]
            roofs.append(dict(id=t_, cell=[int(j), int(i)], area=round(area, 1), z=float(np.nanmin(zs)),
                              x=X0 + (i + .5) * H, y=Y1 - (j + .5) * H, gids=drain_gids.get((j, i), [])))
        np.save(d / "cache" / "roof_grp.npy", grp)
    meta["roofs"] = roofs
    try:
        bid = features.rasterize([(g_, k + 1) for k, g_ in enumerate(bld)], out_shape=(NY, NX), transform=gt).astype(np.int32) if bld \
            else np.zeros((NY, NX), np.int32)
        bid[~(is_bld & inside)] = 0
        np.save(d / "cache" / "bld_ids.npy", bid)
        def _btype(tg):
            b = str(tg.get("building", "")); a = str(tg.get("amenity", "")); sh = tg.get("shop")
            if b in ("industrial", "warehouse", "factory", "manufacture") or tg.get("landuse") == "industrial": return "industrial"
            if b in ("commercial", "retail", "office", "supermarket", "hotel", "kiosk", "hospital", "school", "university",
                     "college", "public", "civic", "government", "church", "cathedral", "train_station", "transportation",
                     "sports_hall", "stadium", "museum", "library", "parking", "kindergarten") or sh or a: return "commercial"
            return "residential"
        tags_list = bld_tags if len(bld_tags) == len(bld) else [{}] * len(bld)
        (d / "cache" / "bld_meta.json").write_text(json.dumps([dict(area=round(g_.area, 1), type=_btype(t_), name=t_.get("name"),
                                                                     wkt=g_.simplify(0.2).wkt, **({"ifc": t_["ifc"]} if t_.get("ifc") else {})) for g_, t_ in zip(bld, tags_list)]))
    except Exception:
        pass
    meta["roof_area_m2"] = round(sum(r_["area"] for r_ in roofs), 1)
    meta["roof_cells_missing_drain"] = int((is_bld & inside).sum() - meta["roof_area_m2"] / (H * H))

    o = ["[2D_VERTICES]"] + [f"{x:.1f} {y:.1f} {z:.3f}" for x, y, z in V]
    o += ["", "[2D_TRIANGLES]"] + [f"{a} {b} {c} {nn:.3f} 0 {tg}" for (a, b, c), nn, tg in zip(T, TN, TT)]
    cache = d / "cache"; cache.mkdir(exist_ok=True)
    (cache / "mesh_block.txt").write_text("\n".join(o) + "\n\n")
    is_water = ras(water_polys) & inside
    np.savez_compressed(cache / "mesh.npz", tsq=TSQ, is_chan=is_chan, is_bld=is_bld, keep=keep, inside=inside, is_tyne=is_tyne,
                        is_water=is_water)
    (cache / "mesh_meta.json").write_text(json.dumps(meta))
    A["n_cells"] = len(T); A["area_km2"] = round(len(T) * H * H / 2 / 1e6 + is_bld[inside].sum() * H * H / 1e6, 3)
    save(A)
    return meta


def _estuary_rivers():
    try:
        import estuary
        return {n for e in estuary.ids() for n in estuary.config(e)["river_names"]}
    except Exception:
        return {"River Tyne", "Tyne"}


def watercourse_crossings(river_info, poly):
    """Where mapped watercourses cross the outline. OpenStreetMap draws waterways in the downstream
    direction, so a crossing from outside to inside is an inflow and inside to outside an exit."""
    from shapely.geometry import Point
    bnd = poly.exterior
    ins_, outs, tidal = [], [], []
    for ln, name, kind in river_info:
        if kind == "canal":
            continue
        if kind == "river" and name in _estuary_rivers():
            if ln.intersects(poly): tidal.append(name)
            continue
        inter = ln.intersection(bnd)
        pts = [inter] if inter.geom_type == "Point" else [g for g in getattr(inter, "geoms", []) if g.geom_type == "Point"]
        for p in pts:
            d = ln.project(p)
            after, before = ln.interpolate(min(d + 2, ln.length)), ln.interpolate(max(d - 2, 0))
            a_in, b_in = poly.contains(after), poly.contains(before)
            if a_in and not b_in: ins_.append(((p.x, p.y), name, kind))
            elif b_in and not a_in: outs.append(((p.x, p.y), name, kind))
    def dedupe(lst):
        keep = []
        for p, nm, kd in lst:
            if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 15 for q, _, _ in keep): keep.append((p, nm, kd))
        return keep
    return dedupe(ins_), dedupe(outs), tidal


def mesh_meta(A):
    meta = json.loads((Path(A["dir"]) / "cache" / "mesh_meta.json").read_text())
    if "tyne" in meta and "estuary" not in meta:          # areas prepared before the estuary registry
        meta["estuary"] = dict(meta["tyne"], id="tyne", name="Tidal Tyne", river="River Tyne")
    return meta


def estuary_npz(A):
    c = Path(A["dir"]) / "cache"
    return np.load(c / "estuary.npz") if (c / "estuary.npz").exists() else np.load(c / "tyne.npz")


# ------------------------------------------------------------------ map layers
def _png(rgba):
    import base64
    from PIL import Image
    b = io.BytesIO(); Image.fromarray(rgba, "RGBA").save(b, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def static_layers(A):
    p = Path(A["dir"]) / "cache" / "static_layers.json"
    if p.exists():
        return json.loads(p.read_text())
    import rasterio
    from rasterio.warp import reproject, Resampling, calculate_default_transform, transform_bounds
    from rasterio.transform import from_origin
    from pyproj import Transformer
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    X1, Y0 = X0 + NX * H, Y1 - NY * H
    d = rasterio.open(Path(A["dir"]) / "data" / "dtm.tif"); a = d.read(1).astype(float)
    r0, c0 = int(d.bounds.top - Y1), int(X0 - d.bounds.left)
    a = a[r0:r0 + int(Y1 - Y0), c0:c0 + int(X1 - X0)]
    f = max(2, int(math.ceil(max(a.shape) / 1600)))
    a = a[:a.shape[0] // f * f, :a.shape[1] // f * f].reshape(a.shape[0] // f, f, a.shape[1] // f, f).mean(axis=(1, 3))
    gy, gx = np.gradient(a, float(f)); sl = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
    hs = np.clip(np.sin(math.radians(45)) * np.cos(sl) + np.cos(math.radians(45)) * np.sin(sl) * np.cos(math.radians(315) - asp), 0, 1)
    # outside the outline: leave transparent
    from rasterio import features
    from shapely.geometry import Polygon
    ins = features.rasterize([(Polygon(A["poly_bng"]), 1)], out_shape=hs.shape, transform=from_origin(X0, Y1, f, f)).astype(bool)
    hs = np.where(ins, hs, np.nan)
    dt, w, h = calculate_default_transform("EPSG:27700", "EPSG:3857", hs.shape[1], hs.shape[0], X0, Y1 - hs.shape[0] * f,
                                           X0 + hs.shape[1] * f, Y1, resolution=float(f))
    out = np.full((h, w), np.nan, np.float32)
    reproject(hs.astype(np.float32), out, src_transform=from_origin(X0, Y1, f, f), src_crs="EPSG:27700",
              dst_transform=dt, dst_crs="EPSG:3857", resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
    rg = np.zeros((h, w, 4), np.uint8); rg[..., :3] = 40
    rg[..., 3] = np.clip((0.85 - np.nan_to_num(out, nan=0.85)) * 300, 0, 170).astype(np.uint8)
    b = transform_bounds("EPSG:3857", "EPSG:4326", dt.c, dt.f + dt.e * h, dt.c + dt.a * w, dt.f)
    tf = Transformer.from_crs(27700, 4326, always_xy=True)
    ll = lambda x, y: list(tf.transform(x, y))[::-1]
    meta = mesh_meta(A)
    res = dict(shade=_png(rg), shadeBounds=[[b[1], b[0]], [b[3], b[2]]],
               outline=[ll(x, y) for x, y in A["poly_bng"]], cells=meta["n_cells"], culverts=meta.get("culverts", 0),
               rivers=meta.get("rivers", 0), outflow_edges=len(meta.get("outflow", [])))
    if "ent" in meta:
        res.update(entry=ll(*meta["ent"]), exit=ll(*meta["ext"]))
    res["inflows"] = [dict(id=f["id"], name=f["name"], kind=f["kind"], at=ll(f["x"], f["y"])) for f in meta.get("inflows", [])]
    res["exits"] = [dict(name=f["name"], kind=f["kind"], at=ll(f["x"], f["y"])) for f in meta.get("exits", [])]
    res["tidal"] = meta.get("tidal", [])
    dl, avail = _defence_lines(A)
    res["defences"] = [dict(crest=round(x["crest"], 2), kind=x["kind"],
                            lines=[[ll(*c) for c in part.coords] for part in getattr(x["geom"], "geoms", [x["geom"]])]) for x in dl]
    res["defences_available"] = avail
    if meta.get("estuary"):
        e = meta["estuary"]
        try:
            import estuary as _est
            ec = _est.config(e["id"]); cal = bool(ec.get("calibration"))
            status = ec.get("status") or ("calibrated" if cal else "assumed")
        except Exception:
            cal, status = False, "assumed"
        res["estuary"] = dict(id=e["id"], name=e["name"], river=e.get("river", e["name"]), n_cells=e["n_cells"],
                              ch_range=e["ch_range"], calibrated=cal, status=status)
    if meta.get("outflow"):
        tsq = np.load(Path(A["dir"]) / "cache" / "mesh.npz")["tsq"]
        pts = {}
        for k, e, s in meta["outflow"]:
            j, i = tsq[k]; pts[(j, i)] = ll(X0 + (i + .5) * H, Y1 - (j + .5) * H)
        res["outflow_pts"] = [[round(a, 6), round(b_, 6)] for a, b_ in pts.values()][::max(1, len(pts) // 1500)]
    p.write_text(json.dumps(res))
    return res
