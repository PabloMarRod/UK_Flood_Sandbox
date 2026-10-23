"""3D scene for an area: the model's terrain grid, a ground texture from land cover, buildings with heights,
defences and river points. Built once per area (cache/scene.json).

Building heights: surface model minus ground (median over the footprint) where a surface model exists
(England: EA LIDAR last-return DSM; Wales: Welsh LiDAR DSM; Scotland: Scottish LiDAR DSM), otherwise
OpenStreetMap height or levels, otherwise 7 m.
"""
import base64, io, json, math, time
from pathlib import Path
import numpy as np

EA_DSM_WCS = "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-surface-model-last-return-dsm-1m/wcs"
EA_DSM_COV = "9ba4d5ac-d596-445a-9056-dae3ddec0178__Lidar_Composite_Elevation_LZ_DSM_1m"

# ground colours (RGB): realistic, muted
COLOURS = dict(garden=(150, 168, 112), grass=(124, 163, 88), wood=(78, 112, 58), farmland=(186, 180, 122),
               road=(112, 114, 116), paved=(176, 172, 162), water=(88, 132, 164), building=(150, 146, 140),
               rail=(138, 128, 118), bare=(196, 182, 152))
GREENS = {"grass", "park", "pitch", "garden", "recreation_ground", "playground", "dog_park", "meadow", "village_green",
          "golf_course", "cemetery", "allotments", "nature_reserve", "common"}


def _b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def _png(rgb):
    from PIL import Image
    b = io.BytesIO(); Image.fromarray(rgb, "RGB").save(b, "JPEG" if rgb.shape[0] * rgb.shape[1] > 1_500_000 else "PNG",
                                                       **({"quality": 88} if rgb.shape[0] * rgb.shape[1] > 1_500_000 else {"optimize": True}))
    kind = "jpeg" if rgb.shape[0] * rgb.shape[1] > 1_500_000 else "png"
    return f"data:image/{kind};base64," + base64.b64encode(b.getvalue()).decode()


def _surface_model(A, dtm_shape, x0, y1, status):
    """1 m surface model aligned with dtm.tif, from whichever source covers the area (NaN elsewhere)."""
    import rasterio, requests
    d = Path(A["dir"]) / "data"
    h, w = dtm_shape
    dsm = np.full((h, w), np.nan, np.float32)
    p = d / "dsm.tif"                                      # Wales (downloaded with the terrain)
    if p.exists():
        a = rasterio.open(p).read(1).astype(np.float32)
        if a.shape == dsm.shape: dsm = np.where(np.isfinite(a), a, dsm)
    if np.isnan(dsm).mean() > 0.5:                          # England: EA last-return DSM
        status("Downloading the surface model (building heights)")
        x1, y0 = x0 + w, y1 - h
        for attempt in range(3):
            try:
                r = requests.get(EA_DSM_WCS, params=[("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
                                 ("CoverageId", EA_DSM_COV), ("format", "image/tiff"), ("subset", f"E({x0},{x1})"),
                                 ("subset", f"N({y0},{y1})")], timeout=300)
                if r.ok and r.content[:2] in (b"II", b"MM"):
                    with rasterio.MemoryFile(r.content) as mf, mf.open() as src:
                        a = src.read(1).astype(np.float32); a[(a < -100) | (a == 0)] = np.nan
                        hh, ww = min(h, a.shape[0]), min(w, a.shape[1])
                        sub = dsm[:hh, :ww]; m = np.isnan(sub); sub[m] = a[:hh, :ww][m]
                    break
            except Exception:
                time.sleep(3)
    if np.isnan(dsm).mean() > 0.5:                          # Scotland: SRSP DSM tiles (same index, dsm folders)
        try:
            import scotland
            if scotland.INDEX.exists():
                idx = json.loads(scotland.INDEX.read_text())
                dsm_idx = {k: [[key.replace("/dtm/", "/dsm/").replace("_DTM_", "_DSM_"), b] for key, b in v] for k, v in idx.items()}
                tmp = Path(A["dir"]) / "cache" / "_dsm_index.json"; tmp.write_text(json.dumps(dsm_idx))
                old = scotland.INDEX; scotland.INDEX = tmp
                try: scotland.fill_dtm(dsm, x0, y1, status)
                finally: scotland.INDEX = old; tmp.unlink(missing_ok=True)
        except Exception:
            pass
    return dsm


def build_scene(A, status=lambda m: None):
    import rasterio
    from rasterio import features
    from rasterio.transform import from_origin
    from shapely.geometry import Polygon, LineString, shape, mapping
    from shapely.ops import unary_union, polygonize
    from pyproj import Transformer
    import areas
    d = Path(A["dir"]); H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    X1, Y0 = X0 + NX * H, Y1 - NY * H
    outline = Polygon(A["poly_bng"])
    src = rasterio.open(d / "data" / "dtm.tif"); dtm = src.read(1).astype(np.float32); dtm[dtm < -100] = np.nan
    top, left = src.bounds.top, src.bounds.left

    # ---- terrain grid: the model's own corner heights (what the water was computed on)
    lines = (d / "cache" / "mesh_block.txt").read_text().split("\n")
    v0, t0 = lines.index("[2D_VERTICES]") + 1, lines.index("[2D_TRIANGLES]")
    heights = np.full((NY + 1, NX + 1), np.nan, np.float32)
    for l in lines[v0:t0]:
        p = l.split()
        if len(p) >= 3:
            i, j = int(round((float(p[0]) - X0) / H)), int(round((Y1 - float(p[1])) / H))
            if 0 <= i <= NX and 0 <= j <= NY: heights[j, i] = float(p[2])
    m = np.load(d / "cache" / "mesh.npz"); keep = m["keep"] if "keep" in m else ~m["is_bld"]
    inside = m["inside"] if "inside" in m else np.ones((NY, NX), bool)
    # fill heights under buildings and other holes so the ground under the 3D blocks is continuous
    hole = ~np.isfinite(heights)
    if hole.any():
        jj, ii = np.nonzero(hole)
        r_ = np.clip((top - (Y1 - jj * H)).astype(int), 0, dtm.shape[0] - 1); c_ = np.clip(((X0 + ii * H) - left).astype(int), 0, dtm.shape[1] - 1)
        heights[jj, ii] = dtm[r_, c_]
        heights = np.where(np.isfinite(heights), heights, np.nanmedian(heights))

    # ---- land cover texture (r m per pixel, max ~2048 px)
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    osm = json.loads((d / "data" / "osm.json").read_text())["elements"]
    buckets = {k: [] for k in COLOURS}
    bld = []
    roadw = areas.ROADW
    for e in osm:
        tg = e.get("tags", {})
        if e["type"] == "relation":
            segs = [LineString(list(zip(*tf([p["lon"] for p in mm["geometry"]], [p["lat"] for p in mm["geometry"]]))))
                    for mm in e.get("members", []) if mm.get("role") == "outer" and len(mm.get("geometry", [])) > 1]
            polys = [p_.buffer(0) for p_ in polygonize(unary_union(segs))] if segs else []
            if tg.get("natural") == "water" or tg.get("waterway") == "riverbank": buckets["water"] += polys
            elif "building" in tg: bld += [(p_, tg) for p_ in polys]
            continue
        g = e.get("geometry")
        if not g or len(g) < 2: continue
        xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g]); pts = list(zip(xs, ys))
        closed = len(pts) > 3 and pts[0] == pts[-1]
        if "building" in tg and closed: bld.append((Polygon(pts).buffer(0), tg))
        elif tg.get("highway") in roadw: buckets["road"].append(LineString(pts).buffer(roadw[tg["highway"]] / 2))
        elif tg.get("railway") in ("rail", "light_rail", "tram"): buckets["rail"].append(LineString(pts).buffer(2.5))
        elif closed:
            poly = Polygon(pts).buffer(0)
            if tg.get("natural") == "water" or tg.get("waterway") == "riverbank": buckets["water"].append(poly)
            elif tg.get("natural") in ("wood", "scrub") or tg.get("landuse") == "forest": buckets["wood"].append(poly)
            elif tg.get("landuse") in GREENS or tg.get("leisure") in GREENS: buckets["grass"].append(poly)
            elif tg.get("landuse") in ("farmland", "meadow", "orchard"): buckets["farmland"].append(poly)
            elif tg.get("amenity") == "parking" or tg.get("landuse") in ("industrial", "retail", "commercial", "railway") \
                    or tg.get("area:highway") or tg.get("highway") == "pedestrian": buckets["paved"].append(poly)
            elif tg.get("natural") in ("sand", "beach", "bare_rock", "mud"): buckets["bare"].append(poly)
        elif tg.get("waterway") in ("river", "stream", "canal") and not tg.get("tunnel"):
            buckets["water"].append(LineString(pts).buffer(3 if tg["waterway"] == "stream" else 8))
    r = max(1, int(math.ceil(max(X1 - X0, Y1 - Y0) / 2048)))
    tw, th = int((X1 - X0) / r), int((Y1 - Y0) / r)
    tt = from_origin(X0, Y1, r, r)
    cls = np.zeros((th, tw), np.uint8)                     # 0 = garden / mixed (default)
    order = ["farmland", "grass", "wood", "bare", "paved", "water", "rail", "road", "building"]
    names = ["garden"] + order
    buckets["building"] = [b for b, _ in bld]
    try:                                                    # estuary water from the model's mesh
        est = m["is_tyne"] if "is_tyne" in m else None
        if est is not None and est.any():
            big = np.kron(est, np.ones((max(1, H // r), max(1, H // r)), bool))[:th, :tw]
            cls[:big.shape[0], :big.shape[1]][big] = names.index("water")
    except Exception:
        pass
    for k in order:
        if buckets[k]:
            msk = features.rasterize([(g_, 1) for g_ in buckets[k] if not g_.is_empty], out_shape=(th, tw), transform=tt).astype(bool)
            cls[msk] = names.index(k)
    pal = np.array([COLOURS[n] for n in names], np.float32)
    rgb = pal[cls]
    rng = np.random.default_rng(1)                          # subtle texture so surfaces do not look flat
    rgb *= (1 + 0.06 * rng.standard_normal((th, tw, 1))).astype(np.float32)
    # terrain shading baked in gently (the 3D lighting does the rest)
    rr0, cc0 = int(top - Y1), int(X0 - left)
    sub = dtm[rr0:rr0 + th * r:r, cc0:cc0 + tw * r:r]
    if sub.shape == (th, tw):
        gy, gx = np.gradient(np.nan_to_num(sub, nan=np.nanmedian(sub)), float(r))
        slope = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
        hs = np.clip(np.sin(math.radians(50)) * np.cos(slope) + np.cos(math.radians(50)) * np.sin(slope) * np.cos(math.radians(315) - asp), 0, 1)
        rgb *= (0.8 + 0.25 * hs)[..., None]
    ins = features.rasterize([(outline, 1)], out_shape=(th, tw), transform=tt).astype(bool)
    rgb[~ins] = (230, 233, 233)
    ground = _png(np.clip(rgb, 0, 255).astype(np.uint8))

    # ---- buildings with heights
    dsm = _surface_model(A, dtm.shape, int(round(left)), int(round(top)), status)
    status("Measuring building heights")
    out_b, measured = [], 0
    for poly, tg in bld:
        if poly.is_empty or not poly.intersects(outline): continue
        poly = poly.intersection(outline)
        for part in getattr(poly, "geoms", [poly]):
            if part.geom_type != "Polygon" or part.area < 6: continue
            bx0, by0, bx1, by1 = part.bounds
            c0, c1 = max(int(bx0 - left), 0), min(int(math.ceil(bx1 - left)), dtm.shape[1])
            r0, r1 = max(int(top - by1), 0), min(int(math.ceil(top - by0)), dtm.shape[0])
            if c1 <= c0 or r1 <= r0: continue
            msk = features.rasterize([(part, 1)], out_shape=(r1 - r0, c1 - c0), transform=from_origin(left + c0, top - r0, 1, 1)).astype(bool)
            gz = dtm[r0:r1, c0:c1][msk]; sz = dsm[r0:r1, c0:c1][msk]
            base = float(np.nanpercentile(gz, 10)) if np.isfinite(gz).any() else float(np.nanmedian(heights))
            hgt = None
            ok = np.isfinite(gz) & np.isfinite(sz)
            if ok.sum() >= 4:
                hgt = float(np.median(sz[ok] - gz[ok]))
                if hgt < 2.0: hgt = None
                else: measured += 1
            if hgt is None:
                try: hgt = float(str(tg.get("height", "")).replace("m", "").strip())
                except ValueError:
                    try: hgt = float(tg.get("building:levels")) * 3.0 + 1.0
                    except (TypeError, ValueError): hgt = 7.0
            hgt = float(np.clip(hgt, 2.5, 250))
            ring = [[round(x - X0, 1), round(Y1 - y, 1)] for x, y in part.simplify(0.3).exterior.coords[:-1]]
            if len(ring) >= 3: out_b.append(dict(p=ring, b=round(base, 2), h=round(hgt, 1)))

    # ---- trees: canopy from the surface model (outside buildings and water), or mapped woodland as a fallback
    status("Finding trees")
    trees = []
    bmask = features.rasterize([(p_.buffer(1.5), 1) for p_, _ in bld if not p_.is_empty], out_shape=dtm.shape,
                               transform=from_origin(left, top, 1, 1)).astype(bool) if bld else np.zeros(dtm.shape, bool)
    wmask = features.rasterize([(g_, 1) for g_ in buckets["water"] if not g_.is_empty], out_shape=dtm.shape,
                               transform=from_origin(left, top, 1, 1)).astype(bool) if buckets["water"] else np.zeros(dtm.shape, bool)
    canopy = np.where(np.isfinite(dsm) & np.isfinite(dtm) & ~bmask & ~wmask, dsm - dtm, 0)
    canopy[(canopy < 2.5) | (canopy > 45)] = 0
    rng_t = np.random.default_rng(3)
    if (canopy > 0).mean() > 0.002:
        from scipy import ndimage
        peak = ndimage.maximum_filter(canopy, size=5)
        step = 6                                            # one tree every ~6 m of canopy, jittered
        for r0_ in range(0, canopy.shape[0] - step, step):
            for c0_ in range(0, canopy.shape[1] - step, step):
                blk = canopy[r0_:r0_ + step, c0_:c0_ + step]
                if (blk > 0).mean() < 0.35: continue
                k = int(np.argmax(blk)); rr, cc = r0_ + k // step, c0_ + k % step
                x, y = left + cc + 0.5, top - rr - 0.5
                if not outline.contains(Polygon([(x - .5, y - .5), (x + .5, y - .5), (x + .5, y + .5), (x - .5, y + .5)])): continue
                hgt = float(peak[rr, cc])
                trees.append([round(x - X0, 1), round(Y1 - y, 1), round(float(dtm[rr, cc]), 2), round(hgt, 1)])
        tree_source = "surface model"
    else:                                                   # no surface model: mapped woodland at a typical density
        for g_ in buckets["wood"]:
            g_ = g_.intersection(outline)
            if g_.is_empty: continue
            n_ = int(g_.area / 40)
            bx0, by0, bx1, by1 = g_.bounds
            from shapely.geometry import Point
            pts_ = [(rng_t.uniform(bx0, bx1), rng_t.uniform(by0, by1)) for _ in range(n_ * 2)]
            for x, y in [p_ for p_ in pts_ if g_.contains(Point(p_))][:n_]:
                rr, cc = int(top - y), int(x - left)
                if 0 <= rr < dtm.shape[0] and 0 <= cc < dtm.shape[1] and np.isfinite(dtm[rr, cc]):
                    trees.append([round(x - X0, 1), round(Y1 - y, 1), round(float(dtm[rr, cc]), 2), round(float(rng_t.uniform(9, 16)), 1)])
        tree_source = "mapped woodland (no surface model here)"
    if len(trees) > 40000:                                  # keep the scene light
        trees = [trees[i] for i in sorted(rng_t.choice(len(trees), 40000, replace=False))]

    # ---- defences and river points
    defs = []
    try:
        from shapely.geometry import Polygon
        clip = Polygon(A["poly_bng"]).buffer(H)                 # draw only the part on the area (a wall may run far beyond it)
        dl, _ = areas._defence_lines(A)
        for x in dl:
            g_ = x["geom"].intersection(clip)
            if g_.is_empty: continue
            for part in getattr(g_, "geoms", [g_]):
                if part.geom_type != "LineString": continue
                pts = [[round(px - X0, 1), round(Y1 - py, 1)] for px, py in part.coords]
                if len(pts) >= 2: defs.append(dict(l=pts, z=round(x["crest"], 2), k=x["kind"]))
    except Exception:
        pass
    meta = areas.mesh_meta(A)
    loc = lambda x, y: [round(x - X0, 1), round(Y1 - y, 1)]
    inflows = [dict(name=f["name"], at=loc(f["x"], f["y"])) for f in meta.get("inflows", [])]
    if "ent" in meta: inflows.append(dict(name="River inflow", at=loc(*meta["ent"])))
    exits = [dict(name=f["name"], at=loc(f["x"], f["y"])) for f in meta.get("exits", [])]
    if "ext" in meta: exits.append(dict(name="Culvert inlet", at=loc(*meta["ext"])))
    scene = dict(H=H, NX=NX, NY=NY, X0=X0, Y1=Y1, heights=_b64(heights.astype(np.float32)),
                 keep=_b64(keep.astype(np.uint8)), inside=_b64(inside.astype(np.uint8)),
                 ground=ground, ground_res=r, ground_size=[tw, th], buildings=out_b, defences=defs,
                 inflows=inflows, exits=exits, measured_buildings=measured, total_buildings=len(out_b),
                 trees=trees, tree_source=tree_source, version=3,
                 zmin=float(np.nanmin(heights)), zmax=float(np.nanmax(heights)),
                 outline=[loc(x, y) for x, y in A["poly_bng"]])
    (d / "cache" / "scene.json").write_text(json.dumps(scene))
    return scene
