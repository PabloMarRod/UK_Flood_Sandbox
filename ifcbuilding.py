"""IFC buildings for an area: read an IFC file into what the flood model needs (footprint, finished floor level,
doors and their thresholds, spaces by storey, height), place it in British National Grid, and check it.

Issues follow the drainage import: "error" stops the import (fix in the authoring tool), "fix" is a repair the user
must accept, "warn" is imported as it is and noted.
"""
import math
from pathlib import Path

import numpy as np

FFL_ABOVE_GROUND = 0.15     # m, used only when the file's levels can't be placed and the user accepts the repair
MAX_FOOTPRINT = 20000.0     # m2


def issue(code, sev, title, ids=(), detail="", fix=None):
    ids = [str(i) for i in ids]
    return dict(code=code, sev=sev, title=title, n=len(ids), ids=ids[:12], detail=detail, fix=fix)


def _open(path):
    import ifcopenshell
    return ifcopenshell.open(str(path))


def georef(m, unit_scale=1.0):
    """Map conversion (IFC4) to EPSG:27700-style coordinates, or None. Returns dict(E, N, H, a, b, scale, crs).
    The geometry comes out in metres, so the conversion's Scale (file units -> map units) is applied relative to
    metres. A millimetre model that left Scale at 1 (a common export slip) is read as if Scale converted it."""
    for mc in m.by_type("IfcMapConversion") if m.schema != "IFC2X3" else []:
        a, b = mc.XAxisAbscissa or 1.0, mc.XAxisOrdinate or 0.0
        r = math.hypot(a, b) or 1.0
        crs = getattr(mc.TargetCRS, "Name", None) if mc.TargetCRS else None
        sc = float(mc.Scale or 1.0); eff = sc / unit_scale; slip = False
        if abs(eff - 1.0) > 0.01 and abs(sc - 1.0) < 1e-9:
            eff, slip = 1.0, True
        return dict(E=float(mc.Eastings), N=float(mc.Northings), H=float(mc.OrthogonalHeight or 0.0),
                    a=a / r, b=b / r, scale=eff, crs=crs, source="IfcMapConversion", scale_slip=slip)
    for s in m.by_type("IfcSite"):
        if s.RefLatitude and s.RefLongitude:
            dms = lambda v: (abs(v[0]) + v[1] / 60 + v[2] / 3600 + (v[3] if len(v) > 3 else 0) / 3.6e9) * (-1 if v[0] < 0 else 1)
            from pyproj import Transformer
            E, N = Transformer.from_crs(4326, 27700, always_xy=True).transform(dms(s.RefLongitude), dms(s.RefLatitude))
            return dict(E=E, N=N, H=float(s.RefElevation or 0.0), a=1.0, b=0.0, scale=1.0, crs="from IfcSite latitude/longitude",
                        source="IfcSite", approximate=True)
    return None


def read(path):
    """Everything in local engineering coordinates (metres): footprint polygon, elements, storeys, doors, spaces."""
    import ifcopenshell.geom, ifcopenshell.util.unit, ifcopenshell.util.element
    from shapely.geometry import Polygon, MultiPolygon
    from shapely.ops import unary_union
    m = _open(path)
    st = ifcopenshell.geom.settings(); st.set("use-world-coords", True)
    scale = ifcopenshell.util.unit.calculate_unit_scale(m)                # file length unit -> metres
    def shape(el):
        try:
            sh = ifcopenshell.geom.create_shape(st, el)
        except Exception:
            return None
        v = np.array(sh.geometry.verts, float).reshape(-1, 3)
        f = np.array(sh.geometry.faces, int).reshape(-1, 3)
        return (v, f) if len(v) else None
    def plan(v, f):
        tris = [Polygon(v[t, :2]) for t in f]
        tris = [t for t in tris if t.is_valid and t.area > 1e-6]
        return unary_union(tris) if tris else None
    out = dict(schema=m.schema, unit_scale=scale, storeys=[], doors=[], spaces=[], elements=0, mesh=[])
    parts, zmin, zmax = [], 1e9, -1e9
    for cls in ("IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcColumn", "IfcCurtainWall", "IfcRoof", "IfcBuildingElementProxy"):
        for el in m.by_type(cls):
            g = shape(el)
            if not g: continue
            v, f = g; out["elements"] += 1
            zmin, zmax = min(zmin, v[:, 2].min()), max(zmax, v[:, 2].max())
            p = plan(v, f)
            if p is not None and not p.is_empty: parts.append(p.buffer(0.02))
            out["mesh"].append(dict(cls=el.is_a(), v=np.round(v, 3).tolist(), f=f.tolist()))
    for s in sorted(m.by_type("IfcBuildingStorey"), key=lambda s: s.Elevation or 0):
        out["storeys"].append(dict(name=s.Name, elevation=float(s.Elevation or 0.0) * scale))
    for d in m.by_type("IfcDoor"):
        g = shape(d)
        if not g: continue
        v, _ = g; c = v.mean(axis=0)
        cont = ifcopenshell.util.element.get_container(d)
        out["doors"].append(dict(name=d.Name or d.GlobalId, x=float(c[0]), y=float(c[1]), sill=float(v[:, 2].min()),
                                 width=float((d.OverallWidth or 0) * scale) or None, height=float((d.OverallHeight or 0) * scale) or None,
                                 storey=cont.Name if cont else None))
        out["mesh"].append(dict(cls="IfcDoor", v=np.round(v, 3).tolist(), f=g[1].tolist()))
    for sp in m.by_type("IfcSpace"):
        g = shape(sp)
        if not g: continue
        v, f = g; p = plan(v, f)
        storey = ifcopenshell.util.element.get_aggregate(sp) or ifcopenshell.util.element.get_container(sp)
        out["spaces"].append(dict(name=sp.Name, use=sp.LongName or sp.ObjectType or "", storey=getattr(storey, "Name", None),
                                  floor=float(v[:, 2].min()), area=round(float(p.area), 2) if p is not None else 0.0))
    fp = unary_union(parts) if parts else None
    if fp is not None and not fp.is_empty:
        fp = fp.buffer(0.3).buffer(-0.3)                                  # close hairline gaps between walls
        if isinstance(fp, MultiPolygon): fp = max(fp.geoms, key=lambda g_: g_.area)
        fp = Polygon(fp.exterior)                                         # a solid block for the flood model
    out["footprint"] = fp
    out["zmin"], out["zmax"] = (float(zmin), float(zmax)) if zmin < 1e9 else (None, None)
    out["georef"] = georef(m, scale)
    return out


def to_bng(g, x, y):
    return g["E"] + g["scale"] * (g["a"] * x - g["b"] * y), g["N"] + g["scale"] * (g["b"] * x + g["a"] * y)


def check(path, A, ground, placement=None, accepted=(), ffl_user=None):
    """Read, place and check. placement: dict(E, N, rot) chosen on the map, used when the file isn't georeferenced
    (or to override it). Returns dict(status, issues, building) with the building in BNG if nothing blocks it."""
    from shapely.affinity import affine_transform
    acc = set(accepted or []); iss = []
    def add(i):
        i["applied"] = i["sev"] == "fix" and i["code"] in acc; iss.append(i); return i["sev"] == "fix"
    try:
        r = read(path)
    except Exception as e:
        add(issue("E_READ", "error", "The file can't be read as IFC", [], str(e)[:200]))
        return dict(status="blocked", issues=iss, building=None)
    fp = r["footprint"]
    if fp is None or fp.is_empty or r["elements"] == 0:
        add(issue("E_NO_GEOMETRY", "error", "No walls or slabs with geometry", [],
                  "The flood model needs the building's outline. Export walls and slabs with body geometry."))
        return dict(status="blocked", issues=iss, building=None)
    if abs(r["unit_scale"] - 1.0) > 1e-9:
        add(issue("W_UNITS", "warn", "Lengths converted to metres", [f"1 file unit = {r['unit_scale']:g} m"]))
    if fp.area > MAX_FOOTPRINT:
        add(issue("E_TOO_BIG", "error", "The footprint is implausibly large", [f"{fp.area:,.0f} m²"],
                  "Check the file's units, or that it holds one building."))
        return dict(status="blocked", issues=iss, building=None)
    g = r["georef"]
    if placement:
        a = math.cos(math.radians(placement["rot"])); b = math.sin(math.radians(placement["rot"]))
        g = dict(E=placement["E"], N=placement["N"], H=0.0, a=a, b=b, scale=1.0, crs="placed on the map", source="map")
    if g is None:
        add(issue("E_PLACE", "error", "The file isn't georeferenced: place it on the map", [],
                  "Drag and rotate the outline into position, then check again."))
        return dict(status="needs_placement", issues=iss, building=None, local=_local(r))
    if g.get("crs") and "27700" not in str(g["crs"]) and g["source"] == "IfcMapConversion":
        add(issue("E_CRS", "error", "The model is georeferenced to another coordinate system", [g["crs"]],
                  "Georeference it to British National Grid (EPSG:27700) in the authoring tool, or place it on the map instead."))
        return dict(status="blocked", issues=iss, building=None)
    if g.get("scale_slip"):
        add(issue("W_GEOREF_SCALE", "warn", "Georeferencing scale left at 1 in a model not in metres", [],
                  "Read as if the scale converted the model's units to metres. Check the position on the map."))
    if g.get("approximate"):
        add(issue("W_SITE_LATLON", "warn", "Placed from the site's latitude and longitude", [],
                  "The outline isn't rotated; check its position and orientation on the map."))
    M = [g["scale"] * g["a"], -g["scale"] * g["b"], g["scale"] * g["b"], g["scale"] * g["a"], g["E"], g["N"]]
    fpb = affine_transform(fp, M)
    from shapely.geometry import Polygon as P_
    area_poly = P_(A["poly_bng"])
    if not area_poly.contains(fpb.centroid):
        add(issue("E_OUTSIDE", "error", "The building isn't inside this area", [f"E {fpb.centroid.x:.0f}, N {fpb.centroid.y:.0f}"],
                  "Check the georeferencing, or place it on the map."))
        return dict(status="blocked", issues=iss, building=None)
    # levels: the model's surface is LiDAR ground in m AOD
    gz = [ground.at(x, y, 3) for x, y in list(fpb.exterior.coords)]
    gz = [z for z in gz if z is not None]
    gmin = min(gz) if gz else None
    dz = g["H"] if g["source"] != "map" else 0.0
    doors = [dict(d, sill=d["sill"] + dz) for d in r["doors"]]
    ground_doors = [d for d in doors if gmin is not None and d["sill"] < gmin + 3.5]
    ffl = min(d["sill"] for d in ground_doors) if ground_doors else None
    storey_levels = [s["elevation"] + dz for s in r["storeys"]]
    if ffl is None and storey_levels and gmin is not None:
        cand = [z for z in storey_levels if z >= gmin - 4]
        ffl = min(cand) if cand else None
    if not doors:
        add(issue("W_NO_DOORS", "warn", "No doors found", [], "The report can't check thresholds; the finished floor level is used."))
    off = None if (ffl is None or gmin is None) else ffl - gmin
    if ffl_user is not None:
        shift = ffl_user - (ffl if ffl is not None else ffl_user)
        doors = [dict(d, sill=d["sill"] + shift) for d in doors]; storey_levels = [z + shift for z in storey_levels]
        ffl = ffl_user; dz += shift
    elif off is None or off < -3.5 or off > 3.0:
        target = (gmin or 0) + FFL_ABOVE_GROUND
        if add(issue("F_LEVELS", "fix", "Levels don't sit on the ground here",
                     [f"finished floor {ffl:.2f} m, ground {gmin:.2f} m" if ffl is not None and gmin is not None else "no usable levels"],
                     detail="Levels may be relative to the floor rather than metres above Ordnance Datum.",
                     fix=f"Set the finished floor level to {target:.2f} m AOD ({FFL_ABOVE_GROUND:g} m above the LiDAR ground); "
                         "you can type another value")):
            shift = target - (ffl if ffl is not None else 0.0)
            doors = [dict(d, sill=d["sill"] + shift) for d in doors]; storey_levels = [z + shift for z in storey_levels]
            ffl = target; dz += shift
    if not r["spaces"]:
        add(issue("W_NO_SPACES", "warn", "No spaces found", [], "Damage can't be split by room use; the whole footprint is used."))
    # buildings it replaces
    osm = _osm_overlaps(A, fpb)
    if osm:
        add(issue("W_REPLACES", "warn", "Mapped buildings this model replaces", [f"{len(osm)} footprint(s)"],
                  "OpenStreetMap buildings overlapping it by more than a quarter of their area are taken out of the model."))
    errors = [i for i in iss if i["sev"] == "error"]
    pending = [i for i in iss if i["sev"] == "fix" and not i["applied"]]
    status = "blocked" if errors else "needs_fixes" if pending else "ok"
    xy = lambda x, y: to_bng(g, x, y)
    bld = dict(footprint=[list(map(lambda v: round(v, 3), c)) for c in fpb.exterior.coords], area=round(fpb.area, 1),
               ffl=round(ffl, 3) if ffl is not None else None, ground_min=round(gmin, 3) if gmin is not None else None,
               height=round(r["zmax"] - r["zmin"], 2), top=round(r["zmax"] + dz, 3),
               doors=[dict(name=d["name"], x=round(xy(d["x"], d["y"])[0], 2), y=round(xy(d["x"], d["y"])[1], 2), sill=round(d["sill"], 3),
                           width=d["width"], height=d["height"], storey=d["storey"]) for d in doors],
               spaces=[dict(s, floor=round(s["floor"] + dz, 3)) for s in r["spaces"]], storeys=[round(z, 3) for z in storey_levels],
               placement=dict(E=g["E"], N=g["N"], a=g["a"], b=g["b"], scale=g["scale"], dz=dz, source=g["source"]),
               replaces=osm, schema=r["schema"])
    bld["rot"] = round(math.degrees(math.atan2(g["b"], g["a"])) % 360, 1)
    return dict(status=status, issues=iss, building=bld if status == "ok" else None, preview=bld, local=_local(r))


def _local(r):
    fp = r["footprint"]; c = fp.centroid
    return dict(footprint=[[x - c.x, y - c.y] for x, y in fp.exterior.coords], area=round(fp.area, 1))


_centres = {}
def read_local_centre(path):
    """Centroid of the footprint in the file's own coordinates (cached by file and size)."""
    k = (str(path), Path(path).stat().st_size, Path(path).stat().st_mtime)
    if k not in _centres:
        c = read(path)["footprint"].centroid; _centres[k] = (c.x, c.y)
    return _centres[k]


def _osm_overlaps(A, fpb):
    import json
    from shapely import wkt
    p = Path(A["dir"]) / "cache" / "bld_meta.json"
    if not p.exists():
        return []
    out = []
    for k, b in enumerate(json.loads(p.read_text())):
        g = wkt.loads(b["wkt"])
        if g.intersects(fpb) and g.intersection(fpb).area > 0.25 * g.area: out.append(k)
    return out


# ------------------------------------------------------------------------------------------------ stored buildings
def _dir(A):
    return Path(A["dir"]) / "ifc"


def save(A, src, result, name, placement=None, ffl_user=None):
    """Store a checked building: the IFC file, its data in BNG and its geometry for the 3D view. Returns its id."""
    import json, re, shutil, time
    if result["status"] != "ok":
        raise ValueError("the building has unresolved issues")
    b = result["building"]
    bid = re.sub(r"[^a-z0-9]+", "-", (name or "building").lower()).strip("-")[:30] + time.strftime("-%y%m%d%H%M%S")
    d = _dir(A) / bid; d.mkdir(parents=True)
    shutil.copy(src, d / "model.ifc")
    p = b["placement"]
    g = dict(E=p["E"], N=p["N"], a=p["a"], b=p["b"], scale=p["scale"])
    geom = []
    for part in read(src)["mesh"]:                                   # local -> BNG, levels shifted like the doors
        v = np.array(part["v"], float)
        x, y = to_bng(g, v[:, 0], v[:, 1])
        geom.append(dict(cls=part["cls"], v=np.round(np.c_[x, y, v[:, 2] + p["dz"]], 3).tolist(), f=part["f"]))
    (d / "geom.json").write_text(json.dumps(geom))
    (d / "building.json").write_text(json.dumps(dict(b, id=bid, name=name or "IFC building", file=Path(src).name,
                                                    placement_input=placement, ffl_user=ffl_user)))
    return bid


def load_all(A):
    import json
    d = _dir(A)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*/building.json")):
        try: out.append(json.loads(f.read_text()))
        except Exception: pass
    return out


def geometry(A, bid):
    import json
    p = _dir(A) / bid / "geom.json"
    return json.loads(p.read_text()) if p.exists() else []


def remove(A, bid):
    import shutil
    shutil.rmtree(_dir(A) / bid, ignore_errors=True)


def override(A, polys, tags):
    """The area's building polygons (BNG) with IFC buildings in place of the mapped ones they overlap by more than a
    quarter of their area. Used by the mesh and the 3D scene, so both see the same buildings."""
    from shapely.geometry import Polygon
    ifc = [(Polygon(b["footprint"]), b) for b in load_all(A)]
    if not ifc:
        return polys, tags
    keep_p, keep_t = [], []
    for p_, t_ in zip(polys, tags):
        if any(p_.intersects(f) and p_.intersection(f).area > 0.25 * p_.area for f, _ in ifc):
            continue
        keep_p.append(p_); keep_t.append(t_)
    for f, b in ifc:
        keep_p.append(f); keep_t.append({"building": "ifc", "ifc": b["id"], "name": b.get("name", "")})
    return keep_p, keep_t


# ------------------------------------------------------------------------------------------------ building report
COMMERCIAL = ("office", "shop", "retail", "store", "commercial", "café", "cafe", "restaurant", "bar", "reception", "meeting")
INDUSTRIAL = ("industrial", "plant", "warehouse", "workshop", "factory", "storage", "garage")


def _use_type(use):
    u = (use or "").lower()
    if any(w in u for w in INDUSTRIAL): return "industrial"
    if any(w in u for w in COMMERCIAL): return "commercial"
    return "residential"


def report(A, run_dir, bid):
    """Water against one IFC building in a finished run: at each door against its threshold, around the building
    against the finished floor level, and the ground-floor spaces that would take water if it got in.
    Spaces are an upper bound: stage 1 doesn't model water passing through doors."""
    import json, netCDF4
    from shapely.geometry import Point, Polygon
    import core
    run_dir = Path(run_dir)
    ids = json.loads((run_dir / "ifc_buildings.json").read_text()) if (run_dir / "ifc_buildings.json").exists() else []
    if bid not in ids:
        return dict(error="This run was made before the building was added. Run the scenario again.")
    b = json.loads((_dir(A) / bid / "building.json").read_text())
    nc = netCDF4.Dataset(str(run_dir / "time_series.nc"))
    t = np.array(nc["time"][:], float); t = t - t[0] + (t[1] - t[0] if len(t) > 1 else 0)
    x = np.array(nc["x"][:], float); y = np.array(nc["y"][:], float)
    WL = np.ma.filled(nc["water_level"][:].astype(float), np.nan); DP = np.ma.filled(nc["depth"][:].astype(float), np.nan)
    GL = np.ma.filled(nc["ground_level"][:].astype(float), np.nan)
    nc.close()
    fp = Polygon(b["footprint"])
    XX, YY = np.meshgrid(x, y)
    def cells_near(geom, r):
        near = geom.buffer(r)
        x0, y0, x1, y1 = near.bounds
        jj, ii = np.where((XX >= x0) & (XX <= x1) & (YY >= y0) & (YY <= y1))
        return [(j, i) for j, i in zip(jj, ii) if near.contains(Point(XX[j, i], YY[j, i])) and not fp.contains(Point(XX[j, i], YY[j, i]))
                and np.isfinite(DP[:, j, i]).any()]
    def low(cells, level):
        """Cells where water can stand against the building at this level: ground at or below it (on a slope, uphill
        cells only carry a film of rain on ground already above the floor)."""
        return [(j, i) for j, i in cells if np.isfinite(GL[j, i]) and GL[j, i] <= level + 0.05]
    def level_series(cells):
        if not cells: return np.full(len(t), np.nan)
        J, I = zip(*cells)
        wl = WL[:, J, I]; d = DP[:, J, I]
        wl = np.where(np.nan_to_num(d) > 0.02, wl, np.nan)                # only where there is water
        return np.nanmax(np.where(np.isnan(wl), -1e9, wl), axis=1).astype(float)
    def first(mask):
        k = np.argmax(mask) if mask.any() else None
        return float(t[k]) if k is not None else None
    dt = float(t[1] - t[0]) if len(t) > 1 else 5.0
    ring = cells_near(fp, 7.5)
    ffl0 = b.get("ffl")
    wall = low(ring, ffl0) if ffl0 is not None else ring
    lv = level_series(wall); lv[lv < -1e8] = np.nan
    dep = np.nanmax(np.where(np.isfinite(DP[:, [c[0] for c in ring], [c[1] for c in ring]]), DP[:, [c[0] for c in ring], [c[1] for c in ring]], 0), axis=1) if ring else np.zeros(len(t))
    peak = float(np.nanmax(lv)) if np.isfinite(lv).any() else None
    doors = []
    for d in b["doors"]:
        near = cells_near(Point(d["x"], d["y"]), 7.5); lo = low(near, d["sill"])
        raised = not lo and bool(near)                                    # outside ground above the threshold all round
        if raised: near = sorted(near, key=lambda c: (XX[c] - d["x"]) ** 2 + (YY[c] - d["y"]) ** 2)[:2]
        s_ = level_series(lo or near); s_[s_ < -1e8] = np.nan
        over = np.nan_to_num(s_, nan=-1e9) > d["sill"]
        pk = float(np.nanmax(s_)) if np.isfinite(s_).any() else None
        doors.append(dict(name=d["name"], sill=d["sill"], peak_level=round(pk, 3) if pk is not None else None,
                          margin=round(d["sill"] - pk, 3) if pk is not None else None,
                          first_over_min=first(over), minutes_over=round(float(over.sum() * dt)), ground_above_sill=raised,
                          series=[None if not np.isfinite(v) else round(float(v), 3) for v in s_]))
    ffl = ffl0
    over_ffl = np.nan_to_num(lv, nan=-1e9) > (ffl if ffl is not None else 1e9)
    entry = max([dd["peak_level"] for dd in doors if dd["peak_level"] is not None and dd["margin"] is not None and dd["margin"] < 0] or [None]) \
        if any(dd["margin"] is not None and dd["margin"] < 0 for dd in doors) else (peak if ffl is not None and peak is not None and peak > ffl else None)
    spaces = []
    for s in b.get("spaces", []):
        depth = (entry - s["floor"]) if entry is not None else 0.0
        typ = _use_type(s.get("use"))
        frac = float(np.interp(min(max(depth, 0), 6.0), core.JRC_DEPTH, core.JRC_EUROPE[typ])) if depth > 0 else 0.0
        spaces.append(dict(name=s["name"], use=s.get("use"), storey=s.get("storey"), area=s["area"], floor=s["floor"],
                           depth_if_entered=round(max(depth, 0), 3), type=typ, damage_eur=round(frac * core.JRC_UK_MAX[typ] * s["area"])))
    c = fp.centroid
    return dict(id=bid, name=b.get("name"), ffl=ffl, ground_min=b.get("ground_min"), times_min=[round(float(v)) for v in t],
                centre=[round(c.x, 2), round(c.y, 2)], local=[round(c.x - A["X0"], 1), round(A["Y1"] - c.y, 1)],
                around=dict(peak_level=round(peak, 3) if peak is not None else None, peak_depth=round(float(np.max(dep)), 3),
                            first_wet_min=first(dep > 0.05), level=[None if not np.isfinite(v) else round(float(v), 3) for v in lv]),
                ffl_exceeded=bool(over_ffl.any()), ffl_first_min=first(over_ffl), ffl_minutes=round(float(over_ffl.sum() * dt)),
                doors=doors, spaces=spaces, damage_eur=sum(s_["damage_eur"] for s_ in spaces),
                note="Spaces assume water that rises above a threshold gets in; water passing through doors isn't modelled yet.")
