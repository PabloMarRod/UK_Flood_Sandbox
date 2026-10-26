"""Buildings from scans: survey point clouds (LAS/LAZ, E57, or text XYZ/PTS/CSV) and meshes from phones or
photogrammetry (OBJ, PLY, GLB/glTF, sampled into points). The scan gives the building's real
outline, height and the ground around it; doors and windows are marked on the scan afterwards. The result is
stored like an IFC building (see ifcbuilding.py), so the mesh, the water inside and the report work the same way.

Issues: "error" stops the import, "fix" is a repair the user must accept, "warn" is noted.
"""
import json, math, re, shutil, time
from pathlib import Path

import numpy as np

import ifcbuilding as ib

GROUND, BUILDING = 2, 6                      # ASPRS LAS classification codes
VOXEL = 0.10                                 # m, thinning for analysis
DISPLAY_MAX = 150_000                        # points kept for the 3D view
FFL_ABOVE_GROUND = 0.15                      # m, provisional floor level until doors are marked
MAX_DATUM_SHIFT = 0.5                        # m, scan ground vs LiDAR ground beyond which a repair is offered
MESH_DENSITY = 400.0                         # points per m2 sampled on a mesh (about 5 cm apart)
MESH_MAX_POINTS = 3_000_000
POINT_FORMATS = (".las", ".laz", ".e57", ".xyz", ".pts", ".csv", ".txt")
MESH_FORMATS = (".obj", ".ply", ".glb", ".gltf")


def _up_axis(xyz):
    """For a local mesh: which axis is up? The one with the most points on a flat bottom (the ground or the floor)."""
    best = None
    for k in (1, 2):
        v = xyz[:, k]; lo = np.percentile(v, 2)
        frac = float(np.mean(np.abs(v - lo) < 0.2))
        if best is None or frac > best[0]: best = (frac, k)
    return best[1]


def _read_mesh(path, ext):
    import trimesh
    loaded = trimesh.load(str(path), process=False)
    notes = []
    if isinstance(loaded, trimesh.PointCloud):                       # a PLY of points rather than faces
        xyz = np.asarray(loaded.vertices, float)
        c = getattr(loaded, "colors", None)
        rgb = np.asarray(c, float)[:, :3] / 255.0 if c is not None and len(c) == len(xyz) else None
    else:
        geoms = loaded.dump(concatenate=False) if isinstance(loaded, trimesh.Scene) else [loaded]
        geoms = [g for g in geoms if isinstance(g, trimesh.Trimesh) and len(g.faces)]
        if not geoms:
            raise ValueError("the file holds no mesh faces or points")
        total = sum(g.area for g in geoms)
        n_all = int(min(MESH_MAX_POINTS, max(20000, total * MESH_DENSITY)))
        pts, cols = [], []
        for g in geoms:
            n = max(50, int(n_all * g.area / total))
            try:
                smp, _, col = trimesh.sample.sample_surface(g, n, sample_color=True)
                cols.append(np.asarray(col, float)[:, :3] / 255.0 if col is not None else None)
            except Exception:
                smp, _ = trimesh.sample.sample_surface(g, n); cols.append(None)
            pts.append(np.asarray(smp, float))
        xyz = np.vstack(pts)
        rgb = np.vstack(cols) if all(c is not None for c in cols) else None
        notes.append(f"sampled {len(xyz):,} points from {sum(len(g.faces) for g in geoms):,} faces")
    georef = np.abs(xyz[:, :2]).max() > 1e5                          # coordinates the size of grid references
    if ext in (".glb", ".gltf"):                                     # glTF is Y-up by definition
        xyz = np.c_[xyz[:, 0], -xyz[:, 2], xyz[:, 1]]; notes.append("Y-up (glTF)")
    elif not georef and _up_axis(xyz) == 1:
        xyz = np.c_[xyz[:, 0], -xyz[:, 2], xyz[:, 1]]; notes.append("Y-up (detected)")
    else:
        notes.append("Z-up")
    return dict(xyz=xyz, cls=None, rgb=rgb, epsg=None, fmt=ext[1:].upper(), notes=notes, mesh=True)


def _read_text(path, ext):
    """XYZ, PTS (a count on the first line; x y z intensity r g b) or CSV, with or without a header."""
    import io, re as _re
    lines = Path(path).read_text(errors="ignore").splitlines()
    lines = [l for l in lines if l.strip() and not l.lstrip().startswith(("#", "//"))]
    if not lines:
        raise ValueError("the file is empty")
    if ext == ".pts" and len(lines[0].split()) == 1:
        lines = lines[1:]
    sep = "," if "," in lines[0] else None
    first = [t.strip().lower() for t in (lines[0].split(",") if sep else lines[0].split())]
    cols = {"x": 0, "y": 1, "z": 2}; rgbc = None
    try:
        [float(t) for t in first]
    except ValueError:                                               # a header: find the columns by name
        lines = lines[1:]
        name = {t: k for k, t in enumerate(first)}
        cols = {a: name.get(a, name.get(a.upper(), k)) for k, a in enumerate("xyz")}
        if all(c in name for c in ("r", "g", "b")): rgbc = [name["r"], name["g"], name["b"]]
        elif all(c in name for c in ("red", "green", "blue")): rgbc = [name["red"], name["green"], name["blue"]]
    data = np.loadtxt(io.StringIO("\n".join(lines)), delimiter=sep, ndmin=2)
    xyz = data[:, [cols["x"], cols["y"], cols["z"]]]
    if rgbc is None and data.shape[1] >= 6:                          # the last three columns, if they look like colours
        last = data[:, -3:]
        if last.min() >= 0 and last.max() <= 255: rgbc = list(range(data.shape[1] - 3, data.shape[1]))
    rgb = data[:, rgbc] / (255.0 if data[:, rgbc].max() > 1 else 1.0) if rgbc else None
    return dict(xyz=xyz, cls=None, rgb=rgb, epsg=None, fmt=ext[1:].upper(), notes=[])


def issue(code, sev, title, ids=(), detail="", fix=None):
    return ib.issue(code, sev, title, ids, detail, fix)


def read(path):
    """Points (N x 3, metres), classification (or None), colours (or None) and the coordinate system (EPSG or None)."""
    path = Path(path); ext = path.suffix.lower()
    if ext in (".las", ".laz"):
        import laspy
        las = laspy.read(str(path))
        xyz = np.c_[np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)]
        cls = np.asarray(las.classification).astype(np.uint8) if "classification" in las.point_format.dimension_names else None
        if cls is not None and not np.any(cls > 1): cls = None            # 0/1 only: never classified
        rgb = None
        if {"red", "green", "blue"} <= set(las.point_format.dimension_names):
            rgb = np.c_[las.red, las.green, las.blue].astype(float)
            rgb = (rgb / (65535.0 if rgb.max() > 255 else 255.0)) if rgb.max() > 0 else None
        epsg = None
        try:
            crs = las.header.parse_crs()
            epsg = crs.to_epsg() if crs is not None else None
            if crs is not None and epsg is None: epsg = -1
        except Exception:
            epsg = None
        return dict(xyz=xyz, cls=cls, rgb=rgb, epsg=epsg, fmt=ext[1:].upper())
    if ext == ".e57":
        import pye57
        e = pye57.E57(str(path)); parts, rgbs = [], []
        for k in range(e.scan_count):
            d = e.read_scan(k, ignore_missing_fields=True, colors=True, transform=True)
            parts.append(np.c_[d["cartesianX"], d["cartesianY"], d["cartesianZ"]])
            if "colorRed" in d: rgbs.append(np.c_[d["colorRed"], d["colorGreen"], d["colorBlue"]] / 255.0)
        return dict(xyz=np.vstack(parts), cls=None, rgb=np.vstack(rgbs) if len(rgbs) == len(parts) and rgbs else None,
                    epsg=None, fmt="E57")
    if ext in MESH_FORMATS:
        return _read_mesh(path, ext)
    if ext in (".xyz", ".pts", ".csv", ".txt"):
        return _read_text(path, ext)
    raise ValueError("Use a point cloud (LAS, LAZ, E57, XYZ, PTS, CSV) or a mesh (OBJ, PLY, GLB, glTF).")


def thin(xyz, *arrays, voxel=VOXEL):
    """One point per voxel (the first), keeping the matching rows of the other arrays."""
    key = np.floor((xyz - xyz.min(axis=0)) / voxel).astype(np.int64)
    _, idx = np.unique(key, axis=0, return_index=True); idx.sort()
    return (xyz[idx],) + tuple(a[idx] if a is not None else None for a in arrays)


def _place(xyz, A, epsg, placement):
    """To British National Grid. Returns (xyz, how, issues)."""
    from shapely.geometry import Polygon, Point
    from pyproj import Transformer
    iss = []
    area = Polygon(A["poly_bng"]); c = xyz[:, :2].mean(axis=0)
    if placement:                                                   # a local scan dropped on the map
        a = math.radians(placement["rot"]); ca, sa = math.cos(a), math.sin(a)
        d = xyz[:, :2] - c
        out = xyz.copy(); out[:, 0] = placement["E"] + ca * d[:, 0] - sa * d[:, 1]; out[:, 1] = placement["N"] + sa * d[:, 0] + ca * d[:, 1]
        return out, "map", iss
    if epsg and epsg > 0 and epsg != 27700:
        tf = Transformer.from_crs(epsg, 27700, always_xy=True)
        x, y = tf.transform(xyz[:, 0], xyz[:, 1]); out = np.c_[x, y, xyz[:, 2]]
        iss.append(issue("W_REPROJECTED", "warn", "Converted to British National Grid", [f"from EPSG:{epsg}"],
                         "Heights are kept as they are; the level check below compares them with the LiDAR."))
        return out, f"EPSG:{epsg}", iss
    if epsg == 27700:
        return xyz, "EPSG:27700", iss
    if area.buffer(500).contains(Point(*c)):                         # no coordinate system recorded, but it fits
        iss.append(issue("W_CRS_ASSUMED", "warn", "No coordinate system recorded: read as British National Grid", [],
                         "The coordinates fall on this area. Check the position on the map."))
        return xyz, "assumed EPSG:27700", iss
    return None, None, iss


def _footprint(xy, cell=0.25):
    """Outline of the building points: occupancy on a fine grid, closed, holes filled, largest part."""
    from scipy import ndimage
    from rasterio import features
    from rasterio.transform import from_origin
    from shapely.geometry import shape
    x0, y0 = xy.min(axis=0) - 1; x1, y1 = xy.max(axis=0) + 1
    nx, ny = int((x1 - x0) / cell) + 1, int((y1 - y0) / cell) + 1
    m = np.zeros((ny, nx), bool)
    m[((y1 - xy[:, 1]) / cell).astype(int).clip(0, ny - 1), ((xy[:, 0] - x0) / cell).astype(int).clip(0, nx - 1)] = True
    m = ndimage.binary_closing(m, iterations=3); m = ndimage.binary_fill_holes(m)
    lab, n = ndimage.label(m)
    if not n:
        return None
    big = 1 + int(np.argmax(ndimage.sum(m, lab, range(1, n + 1))))
    polys = [shape(g) for g, v in features.shapes((lab == big).astype(np.uint8), transform=from_origin(x0, y1, cell, cell)) if v == 1]
    return max(polys, key=lambda p: p.area).buffer(0).simplify(cell, preserve_topology=True) if polys else None


def check(path, A, ground, placement=None, accepted=(), ffl_user=None):
    """Read, place, separate ground and building, compare levels with the LiDAR. Returns dict(status, issues,
    building, preview, local) in the same shape as ifcbuilding.check."""
    from shapely.geometry import Polygon, Point
    from shapely import wkt, contains_xy, prepare
    def within(g, pts):                                              # vectorised point-in-polygon
        prepare(g); return contains_xy(g, pts[:, 0], pts[:, 1])
    acc = set(accepted or []); iss = []
    def add(i):
        i["applied"] = i["sev"] == "fix" and i["code"] in acc; iss.append(i); return i["sev"] == "fix"
    def done(status, **kw):
        return dict(status=status, issues=iss, building=None, **kw)
    try:
        r = read(path)
    except Exception as e:
        add(issue("E_READ", "error", "The file can't be read as a point cloud", [], str(e)[:200])); return done("blocked")
    if r.get("mesh"):
        add(issue("W_MESH", "warn", "Mesh read as points", r.get("notes", [])))
    if len(r["xyz"]) < 1000:
        add(issue("E_EMPTY", "error", "Too few points", [f"{len(r['xyz'])} points"], "Export the full scan of the building."))
        return done("blocked")
    ext_ = float(np.ptp(r["xyz"][:, :2], axis=0).max())
    if np.abs(r["xyz"][:, :2]).max() < 1e5 and ext_ > 200:          # a local scan this large is probably not in metres
        f_ = 1000.0 if ext_ > 2000 else 100.0
        if add(issue("F_UNITS", "fix", "The scan looks like it isn't in metres", [f"{ext_:,.0f} units across"],
                     fix=f"Read it as {'millimetres' if f_ == 1000 else 'centimetres'} (divide by {f_:g})")):
            r["xyz"] = r["xyz"] / f_
    xyz, how, more = _place(r["xyz"], A, r["epsg"], placement); iss += more
    if xyz is None:
        c = r["xyz"][:, :2].mean(axis=0)
        add(issue("E_PLACE", "error", "The scan isn't in British National Grid: place it on the map", [],
                  "Drag and rotate the outline into position, then check again."))
        loc = r["xyz"][:: max(1, len(r["xyz"]) // 20000)]
        fp = _footprint(loc[:, :2] - c) if len(loc) else None
        return done("needs_placement", local=dict(footprint=[list(p) for p in fp.exterior.coords], area=round(fp.area, 1)) if fp is not None else None)
    xyz, cls, rgb = thin(xyz, r["cls"], r["rgb"])
    area = Polygon(A["poly_bng"])
    if not area.contains(Point(*xyz[:, :2].mean(axis=0))):
        add(issue("E_OUTSIDE", "error", "The scan isn't inside this area", [f"E {xyz[:, 0].mean():.0f}, N {xyz[:, 1].mean():.0f}"],
                  "Check its coordinate system, or place it on the map."))
        return done("blocked")
    # ground and building points
    lz = ground.at_many(xyz[:, 0], xyz[:, 1])                        # LiDAR ground under every point
    if cls is not None and (cls == GROUND).any() and (cls == BUILDING).any():
        is_g, is_b = cls == GROUND, cls == BUILDING
        add(issue("W_CLASSIFIED", "warn", "Ground and building points taken from the scan's classification",
                  [f"{int(is_g.sum()):,} ground, {int(is_b.sum()):,} building"]))
    else:
        # ground: points near the LiDAR ground; building: points well above it, inside the mapped building they sit on.
        # A scan without real heights (a phone scan placed on the map) is lined up provisionally for this only; the
        # level repair below still asks before shifting it.
        ok_ = np.isfinite(lz)
        d0 = float(np.median(lz[ok_]) - np.percentile(xyz[ok_, 2], 2)) if ok_.any() else 0.0
        zc = xyz[:, 2] + (d0 if abs(d0) > MAX_DATUM_SHIFT else 0.0)
        is_g = np.abs(zc - lz) < 0.3
        high = zc > lz + 2.0
        bm = json.loads((Path(A["dir"]) / "cache" / "bld_meta.json").read_text())
        hits = [wkt.loads(b["wkt"]) for b in bm]                       # mapped and already added buildings alike
        hp = xyz[high][:: max(1, int(high.sum()) // 3000)]
        cen = Point(*hp[:, :2].mean(axis=0)) if len(hp) else None
        cand = [g for g in hits if cen is not None and g.distance(cen) < 60]
        best = max(cand, key=lambda g: int(within(g.buffer(1.0), hp).sum())) if cand else None
        if (best is None or not within(best.buffer(1.0), hp).any()) and len(hp):
            # no building on the map here (a new build, or a gap in the map): the raised points nearest the scan's centre
            from scipy import ndimage
            cell = 0.5; hx = xyz[high]; x0, y0 = hx[:, :2].min(axis=0) - 1
            nx, ny = int(np.ptp(hx[:, 0]) / cell) + 3, int(np.ptp(hx[:, 1]) / cell) + 3
            m = np.zeros((ny, nx), bool); m[((hx[:, 1] - y0) / cell).astype(int), ((hx[:, 0] - x0) / cell).astype(int)] = True
            lab, n = ndimage.label(ndimage.binary_closing(m, iterations=2))
            if n:
                sizes = ndimage.sum(m, lab, range(1, n + 1)); c = xyz[:, :2].mean(axis=0)
                cy, cx = int((c[1] - y0) / cell), int((c[0] - x0) / cell)
                pick = lab[cy, cx] if 0 <= cy < ny and 0 <= cx < nx and lab[cy, cx] else 1 + int(np.argmax(sizes))
                cells = np.argwhere(lab == pick)
                if len(cells) * cell * cell >= 20:
                    from shapely.geometry import MultiPoint
                    best = MultiPoint([(x0 + (i + .5) * cell, y0 + (j + .5) * cell) for j, i in cells]).buffer(cell * 0.75).buffer(0)
                    add(issue("W_NO_MAPPED", "warn", "No building on the map here: found from height alone", [],
                              "Trees or walls touching the building may be included. A classified scan avoids this."))
        if best is None:
            add(issue("E_NO_BUILDING", "error", "No building found in the scan", [],
                      "No raised points or mapped building where the scan lies. Classify building points (code 6), or check its position."))
            return done("blocked")
        reg = best.buffer(2.0)
        is_b = high & within(reg, xyz)
        # walls reach down to the ground: keep points above ground inside the region too
        is_b |= (zc > lz + 0.05) & within(best.buffer(0.3), xyz)
        add(issue("W_UNCLASSIFIED", "warn", "Unclassified scan: building points found from height and the mapped outline",
                  [f"{int(is_b.sum()):,} building, {int(is_g.sum()):,} ground"],
                  "Trees or walls touching the building may be included. A classified scan (ground 2, building 6) avoids this."))
    if is_b.sum() < 200:
        add(issue("E_NO_BUILDING", "error", "Too few building points", [f"{int(is_b.sum())}"])); return done("blocked")
    fp = _footprint(xyz[is_b, :2])
    if fp is None or fp.area < 10 or fp.area > ib.MAX_FOOTPRINT:
        add(issue("E_FOOTPRINT", "error", "No usable building outline", [f"{0 if fp is None else fp.area:.0f} m²"])); return done("blocked")
    # a scan that covers only part of the building it stands on (one side of a house from a phone, say)
    bm = json.loads((Path(A["dir"]) / "cache" / "bld_meta.json").read_text())
    mapped = [g for g in (wkt.loads(b["wkt"]) for b in bm) if g.intersects(fp)]
    big = max(mapped, key=lambda g: g.intersection(fp).area) if mapped else None
    if big is not None and big.area > 1.5 * fp.area and big.intersection(fp).area > 0.5 * fp.area:
        if add(issue("F_PARTIAL", "fix", "The scan covers only part of the mapped building", [f"scan {fp.area:.0f} m², mapped {big.area:.0f} m²"],
                     fix="Use the mapped outline for the building, with the scan's heights and ground")):
            fp = big
    # levels: scan ground around the building against the LiDAR
    ring = fp.buffer(4.0).difference(fp.buffer(0.3))
    gpts = xyz[is_g]; near = gpts[within(ring, gpts)] if len(gpts) else gpts
    if len(near) < 20:
        add(issue("E_NO_GROUND", "error", "The scan has too little ground around the building", [f"{len(near)} points within 4 m"],
                  "The levels can't be checked. Include the ground around the building in the scan."))
        return done("blocked")
    lzn = ground.at_many(near[:, 0], near[:, 1]); ok = np.isfinite(lzn)
    diff = float(np.median(near[ok, 2] - lzn[ok])) if ok.any() else 0.0
    shift = 0.0
    if abs(diff) > MAX_DATUM_SHIFT:
        local = how == "map"
        if add(issue("F_LEVELS", "fix", "The scan has no real heights" if local else "Scan heights don't match the LiDAR ground",
                     [f"median difference {diff:+.2f} m"],
                     detail=("A scan placed on the map (a phone scan, for example) is levelled on the LiDAR, so its levels are "
                             "only as accurate as the LiDAR.") if local else "Heights may not be metres above Ordnance Datum.",
                     fix=f"Shift the scan by {-diff:+.2f} m so its ground matches the LiDAR")):
            shift = -diff
    else:
        add(issue("W_LEVELS_OK", "warn", "Scan ground matches the LiDAR", [f"median difference {diff:+.2f} m"]))
    xyz = xyz + [0, 0, shift]; near = near + [0, 0, shift]
    gmin = float(np.percentile(near[:, 2], 5))
    top = float(np.percentile(xyz[is_b, 2], 99.5))
    ffl = float(ffl_user) if ffl_user is not None else gmin + FFL_ABOVE_GROUND
    if ffl_user is None:
        add(issue("W_FFL_PROVISIONAL", "warn", "Floor level provisional until doors are marked",
                  [f"{ffl:.2f} m AOD ({FFL_ABOVE_GROUND:g} m above the lowest ground)"],
                  "After adding the building, mark its doors and windows on the scan: their thresholds set the levels."))
    prev = ib.added_overlaps(A, fp)
    if prev:
        add(issue("F_REPLACE_ADDED", "fix", "It overlaps a building you added", [n for _, n in prev],
                  fix="Replace that building with this one"))
    osm = ib._osm_overlaps(A, fp)
    if osm:
        add(issue("W_REPLACES", "warn", "Mapped buildings this scan replaces", [f"{len(osm)} footprint(s)"]))
    errors = [i for i in iss if i["sev"] == "error"]; pending = [i for i in iss if i["sev"] == "fix" and not i["applied"]]
    status = "blocked" if errors else "needs_fixes" if pending else "ok"
    # points for the 3D view: the building and anything above ground near it, coloured by class or by the scan
    keep = is_b | (~is_g & within(fp.buffer(6), xyz))
    disp = np.where(keep)[0]
    if len(disp) > DISPLAY_MAX: disp = np.sort(np.random.default_rng(0).choice(disp, DISPLAY_MAX, replace=False))
    col = rgb[disp] if rgb is not None else np.where(is_b[disp, None], [[0.82, 0.78, 0.72]], [[0.36, 0.55, 0.30]])
    bld = dict(footprint=[[round(x, 3), round(y, 3)] for x, y in fp.exterior.coords], area=round(fp.area, 1),
               ffl=round(ffl, 3), ground_min=round(gmin, 3), height=round(top - gmin, 2), top=round(top, 3),
               doors=[], windows=[], spaces=[], storeys=[round(ffl, 3)],
               interior=dict(mode="storeys", reason="a scan has no rooms"),
               placement=dict(source="scan", how=how, shift=round(shift, 3)), replaces=osm, replaces_added=[i for i, _ in prev],
               schema=r["fmt"], source="scan",
               scan=dict(points=int(len(r["xyz"])), kept=int(len(xyz)), classified=r["cls"] is not None, datum_diff=round(diff, 3)),
               rot=0.0)
    c = fp.centroid
    out = dict(status=status, issues=iss, building=bld if status == "ok" else None, preview=bld,
               local=dict(footprint=[[x - c.x, y - c.y] for x, y in fp.exterior.coords], area=round(fp.area, 1)))
    out["_points"] = dict(xyz=np.round(xyz[disp], 3), rgb=np.round(col, 3))
    return out


def save(A, src, result, name, placement=None, ffl_user=None):
    """Store like an IFC building: building.json, and the display points as geom.json."""
    if result["status"] != "ok":
        raise ValueError("the scan has unresolved issues")
    b = result["building"]
    bid = re.sub(r"[^a-z0-9]+", "-", (name or "scan").lower()).strip("-")[:30] + time.strftime("-%y%m%d%H%M%S")
    d = ib._dir(A) / bid; d.mkdir(parents=True)
    p = result["_points"]
    (d / "geom.json").write_text(json.dumps([dict(cls="scan", pts=p["xyz"].ravel().tolist(), rgb=p["rgb"].ravel().tolist())]))
    (d / "building.json").write_text(json.dumps(dict(b, id=bid, name=name or "Scanned building", file=Path(src).name,
                                                    placement_input=placement, ffl_user=ffl_user)))
    return bid


def set_openings(A, bid, openings):
    """Doors and windows marked on the scan: [{kind: door|window, x, y, sill, width, height}] in BNG / m AOD.
    The lowest door threshold becomes the finished floor level."""
    p = ib._dir(A) / bid / "building.json"
    b = json.loads(p.read_text())
    doors = [dict(name=o.get("name") or f"Door {k + 1}", x=float(o["x"]), y=float(o["y"]), sill=float(o["sill"]),
                  width=float(o.get("width") or 0.9), height=float(o.get("height") or 2.1), storey=None, kind="external", spaces=[])
             for k, o in enumerate(x for x in openings if x.get("kind") == "door")]
    wins = [dict(name=o.get("name") or f"Window {k + 1}", x=float(o["x"]), y=float(o["y"]), sill=float(o["sill"]),
                 top=float(o["sill"]) + float(o.get("height") or 1.2), width=float(o.get("width") or 1.0), kind="external", spaces=[])
            for k, o in enumerate(x for x in openings if x.get("kind") == "window")]
    b["doors"], b["windows"] = doors, wins
    if doors and b.get("ffl_user") is None:
        b["ffl"] = round(min(d["sill"] for d in doors), 3); b["storeys"] = [b["ffl"]]
    p.write_text(json.dumps(b)); return b
