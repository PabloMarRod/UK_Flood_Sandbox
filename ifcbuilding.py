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
    out["windows"] = []
    for w in m.by_type("IfcWindow"):
        g = shape(w)
        if not g: continue
        v, _ = g; c = v.mean(axis=0)
        out["windows"].append(dict(name=w.Name or w.GlobalId, x=float(c[0]), y=float(c[1]), sill=float(v[:, 2].min()),
                                   top=float(v[:, 2].max()), width=float((w.OverallWidth or 0) * scale) or None))
        out["mesh"].append(dict(cls="IfcWindow", v=np.round(v, 3).tolist(), f=g[1].tolist()))
    for sp in m.by_type("IfcSpace"):
        g = shape(sp)
        if not g: continue
        v, f = g; p = plan(v, f)
        storey = ifcopenshell.util.element.get_aggregate(sp) or ifcopenshell.util.element.get_container(sp)
        if p is not None and p.geom_type == "MultiPolygon": p = max(p.geoms, key=lambda g_: g_.area)
        out["spaces"].append(dict(name=sp.Name, use=sp.LongName or sp.ObjectType or "", storey=getattr(storey, "Name", None),
                                  floor=float(v[:, 2].min()), height=round(float(v[:, 2].max() - v[:, 2].min()), 2),
                                  area=round(float(p.area), 2) if p is not None else 0.0,
                                  poly=[list(c) for c in p.exterior.coords] if p is not None and p.geom_type == "Polygon" else None))
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
    interior = _interior(r, fp, ffl - dz if ffl is not None else None)
    below = []                                                       # openings whose sill is under the ground outside
    for o, kind in [(d, interior["doors"][k]) for k, d in enumerate(doors)] + \
                   [(dict(w, sill=w["sill"] + dz), interior["windows"][k]) for k, w in enumerate(r.get("windows", []))]:
        if kind["kind"] != "external": continue
        gx, gy = to_bng(g, o["x"], o["y"]); zo = ground.at(gx, gy, 2)
        if zo is not None and zo > o["sill"] + 0.1: below.append(f"{o['name']} ({zo - o['sill']:.2f} m)")
    if below:
        add(issue("W_BELOW_GROUND", "warn", "Openings below the ground outside", below,
                  "The LiDAR ground outside is above these sills, often because the model sits on a slope it wasn't designed "
                  "for. Windows below ground are treated as sealed; doors stay open to water running down to them."))
    if interior["mode"] == "storeys":
        add(issue("W_INTERIOR_STOREY", "warn", "Water inside is modelled per storey", [interior["reason"]],
                  "Each storey is one volume. Rooms are modelled separately when every outside door and window lies on a "
                  "space and internal doors join the spaces."))
    # buildings it replaces
    prev = added_overlaps(A, fpb)
    if prev:
        add(issue("F_REPLACE_ADDED", "fix", "It overlaps a building you added", [n for _, n in prev],
                  fix="Replace that building with this one"))
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
                           width=d["width"], height=d["height"], storey=d["storey"], kind=o["kind"], spaces=o["spaces"])
                      for d, o in zip(doors, interior["doors"])],
               windows=[dict(name=w["name"], x=round(xy(w["x"], w["y"])[0], 2), y=round(xy(w["x"], w["y"])[1], 2),
                             sill=round(w["sill"] + dz, 3), top=round(w["top"] + dz, 3), width=w["width"], kind=o["kind"], spaces=o["spaces"])
                        for w, o in zip(r["windows"], interior["windows"])],
               spaces=[dict({k: v for k, v in s.items() if k != "poly"}, floor=round(s["floor"] + dz, 3),
                            poly=[list(map(lambda v: round(v, 3), xy(x, y))) for x, y in s["poly"]] if s.get("poly") else None)
                       for s in r["spaces"]], storeys=[round(z, 3) for z in storey_levels],
               interior=dict(mode=interior["mode"], reason=interior.get("reason")),
               placement=dict(E=g["E"], N=g["N"], a=g["a"], b=g["b"], scale=g["scale"], dz=dz, source=g["source"]),
               replaces=osm, replaces_added=[i for i, _ in prev], schema=r["schema"])
    bld["rot"] = round(math.degrees(math.atan2(g["b"], g["a"])) % 360, 1)
    return dict(status=status, issues=iss, building=bld if status == "ok" else None, preview=bld, local=_local(r))


def _interior(r, fp, ffl_local):
    """Which outside or inside each opening joins, and whether rooms can be modelled separately."""
    from shapely.geometry import Point, Polygon
    polys = [(s["name"], Polygon(s["poly"]).buffer(0.6)) if s.get("poly") else (s["name"], None) for s in r["spaces"]]
    ring = fp.exterior
    def classify(o):
        pt = Point(o["x"], o["y"]); ext = ring.distance(pt) < 0.8
        sp = [n for n, pg in polys if pg is not None and pg.contains(pt)]
        # an opening joins the spaces on the same level as its sill
        lvl = {s["name"]: s["floor"] for s in r["spaces"]}
        sp = [n for n in sp if abs(lvl[n] - o["sill"]) < 1.5 or (o.get("top") and lvl[n] <= o["sill"] <= lvl[n] + 3)]
        return dict(kind="external" if ext else "internal", spaces=sp[:1] if ext else sp[:2])
    doors = [classify(d) for d in r["doors"]]; wins = [classify(dict(w, top=w["top"])) for w in r.get("windows", [])]
    if not r["spaces"] or any(s.get("poly") is None for s in r["spaces"]):
        return dict(mode="storeys", reason="spaces without outlines", doors=doors, windows=wins)
    unmapped = [o for o in doors + wins if o["kind"] == "external" and not o["spaces"]]
    if unmapped:
        return dict(mode="storeys", reason=f"{len(unmapped)} outside opening(s) not on a space", doors=doors, windows=wins)
    return dict(mode="spaces", doors=doors, windows=wins)


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


def added_overlaps(A, fpb):
    """Buildings already added (IFC or scan) that this footprint overlaps substantially: [(id, name)]."""
    from shapely.geometry import Polygon
    out = []
    for b in load_all(A):
        g = Polygon(b["footprint"])
        if g.intersects(fpb) and g.intersection(fpb).area > 0.25 * min(g.area, fpb.area): out.append((b["id"], b.get("name") or b["id"]))
    return out


def _osm_overlaps(A, fpb):
    import json
    from shapely import wkt
    p = Path(A["dir"]) / "cache" / "bld_meta.json"
    if not p.exists():
        return []
    out = []
    for k, b in enumerate(json.loads(p.read_text())):
        if b.get("ifc"): continue                                    # added buildings are reported separately
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
    Rooms come from the modelled water inside when the run has it; otherwise (older runs) they are an upper bound."""
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
    for d in [d for d in b["doors"] if d.get("kind", "external") == "external"]:   # internal doors don't face the outside
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
    inside = _inside(run_dir, bid)                                    # water modelled inside the building
    note = "This run didn't model water inside, so spaces assume water that rises above a threshold gets in."
    if inside:
        spaces = []
        for name, v in inside["spaces"].items():
            typ = _use_type(v.get("use")); d = v["max_depth"]
            frac = float(np.interp(min(d, 6.0), core.JRC_DEPTH, core.JRC_EUROPE[typ])) if d > 0.02 else 0.0
            spaces.append(dict(name=name, use=v.get("use"), storey=v.get("storey"), area=v["area"], floor=v["floor"],
                               max_depth=round(d, 3), first_wet_min=v["first_wet_min"], minutes_wet=v["minutes_wet"],
                               series=v["series"], type=typ, damage_eur=round(frac * core.JRC_UK_MAX[typ] * v["area"])))
        note = ("Water inside is modelled through the doors and windows, with the opening settings of this scenario "
                f"({inside['mode']}). Leakage and failure settings are assumptions.")
    # difference between the water against the walls and the water inside (the floor if dry), against Kelman's thresholds
    walls = None
    if ffl is not None and np.isfinite(lv).any():
        inside_lvl = np.full(len(t), ffl)
        if inside:
            ti = np.array(inside["times_min"], float)
            for v in inside["spaces"].values():
                if v.get("storey") is not None and v["floor"] > ffl + 0.5: continue        # ground floor only
                ser = np.array(v["series"], float)
                if len(ser) == len(ti) and len(ti):
                    inside_lvl = np.maximum(inside_lvl, v["floor"] + np.interp(t, ti, ser))
        diff = np.where(np.isfinite(lv), lv - inside_lvl, -np.inf)
        md = float(np.max(diff))
        walls = dict(max_diff=round(max(md, 0.0), 3), onset=WALL_ONSET, most=WALL_MOST,
                     first_onset_min=first(diff > WALL_ONSET), first_most_min=first(diff > WALL_MOST))
    c = fp.centroid
    return dict(id=bid, name=b.get("name"), ffl=ffl, ground_min=b.get("ground_min"), times_min=[round(float(v)) for v in t],
                walls=walls,
                inside=dict(mode=inside["mode"], times_min=inside["times_min"], skipped=inside["skipped"]) if inside else None,
                centre=[round(c.x, 2), round(c.y, 2)], local=[round(c.x - A["X0"], 1), round(A["Y1"] - c.y, 1)],
                around=dict(peak_level=round(peak, 3) if peak is not None else None, peak_depth=round(float(np.max(dep)), 3),
                            first_wet_min=first(dep > 0.05), level=[None if not np.isfinite(v) else round(float(v), 3) for v in lv]),
                ffl_exceeded=bool(over_ffl.any()), ffl_first_min=first(over_ffl), ffl_minutes=round(float(over_ffl.sum() * dt)),
                doors=doors, spaces=spaces, damage_eur=sum(s_["damage_eur"] for s_ in spaces), note=note)


def _inside(run_dir, bid):
    """Depth in each modelled room over time, from the engine's output (None if the run didn't model the inside)."""
    import json
    p = Path(run_dir) / "ifc_nodes.json"; o_ = Path(run_dir) / "model.out"
    if not p.exists() or not o_.exists():
        return None
    info = json.loads(p.read_text()).get(bid)
    if not info or not info.get("spaces"):
        return None
    from openswmm.output import Output, NodeAttribute
    with Output(str(o_)) as o:
        times = list(o.times)
        step = (times[1] - times[0]).total_seconds() / 60 if len(times) > 1 else 5.0
        mins = [round((tt - times[0]).total_seconds() / 60 + step, 1) for tt in times]
        stride = max(1, len(mins) // 72)                                 # at most ~72 points for the chart
        out = {}
        for name, v in info["spaces"].items():
            d = np.array(list(o.get_node_timeseries(v["node"], NodeAttribute.INVERT_DEPTH).values()), float)
            wet = d > 0.02
            out[name] = dict(v, max_depth=float(d.max()) if len(d) else 0.0, first_wet_min=mins[int(np.argmax(wet))] if wet.any() else None,
                             minutes_wet=round(float(wet.sum() * step)), series=[round(float(x), 3) for x in d[::stride]])
    return dict(mode=info["mode"], spaces=out, times_min=mins[::stride], skipped=info.get("skipped", 0))


# ------------------------------------------------------------------------------------------------ water inside
# Leakage through closed domestic external doors rises linearly with water depth above the threshold: just over
# 1,000 litres per hour per metre of width at 0.2 m and about 4,000 at 0.6 m (University of Hull tests of six UK
# doors for Flood Re, McLelland et al. 2026, doi:10.5281/zenodo.23220942). As a rate per metre of width per metre
# of depth that is about 6,700 L/h; outward-opening doors leaked about 74% less on average (only two were tested).
DOOR_LEAK = {"typical": 6700.0, "outward": 6700.0 * 0.26}     # L/h per m width per m depth
# Windows: Kelman (2002, PhD, Cambridge) estimates water rising inside about ten times slower through a weather-
# stripped window than through a door (orders of magnitude only; he later noted his air-to-water conversion needs
# revising), and finds window glass too small and too high to break in floods. So windows leak above the sill at a
# tenth of the door rate and don't break unless the user sets a height.
WINDOW_LEAK_FACTOR = 0.1
# Kelman (2002): with still water, cavity walls of modern English homes start failing when the water outside is about
# 0.8 m above the ground floor (more than inside), and most have failed by 2.0 m.
WALL_ONSET, WALL_MOST = 0.8, 2.0
DOOR_HEIGHT = 2.1         # m, when the model has none


def model_lines(A, q, lines, cache, sidecar=None):
    """SWMM lines for the inside of each IFC building: a storage node per ground-floor space (or per storey), and
    each outside door and window as a link from a small node coupled to the cell outside. Closed doors and windows
    leak in proportion to the water above their threshold (measured rates, see DOOR_LEAK); windows are watertight
    unless set to break at a height; open doors (or doors and windows that give way at a set height) pass
    water over their full width. Internal doors join rooms. Everything is two-way, so water drains out again."""
    import json
    from shapely.geometry import Polygon, Point
    blds = [b for b in load_all(A) if b.get("interior")]
    out = dict(junctions=[], storage=[], orifices=[], weirs=[], outlets=[], xsections=[], controls=[], coords=[], nodemap=[])
    if not blds:
        return out
    H, X0, Y1 = A["H"], A["X0"], A["Y1"]
    t0 = lines.index("[2D_TRIANGLES]") + 1; v0 = lines.index("[2D_VERTICES]") + 1
    tsq = np.load(cache / "mesh.npz")["tsq"]
    first = {}
    for k, (j, i) in enumerate(tsq.tolist()): first.setdefault((j, i), k)
    vz = np.array([float(l.split()[2]) for l in lines[v0:t0 - 2] if len(l.split()) >= 3])
    tri = {}
    def tags(k):
        if k not in tri: tri[k] = lines[t0 + k].split()
        return tri[k]
    OPEN = ("mixed", "green", "paved", "perm", "raing", "swale", "wood")
    taken = set()
    def cell_near(x, y, r=2):
        """The lowest open cell within r cells: where water outside the opening collects (on a slope the nearest
        cell can sit a few centimetres above it)."""
        j0, i0 = int((Y1 - y) // H), int((x - X0) // H)
        best = None
        for j in range(j0 - r, j0 + r + 1):
            for i in range(i0 - r, i0 + r + 1):
                kk = first.get((j, i))
                if kk is None or (j, i) in taken or tags(kk)[5] not in OPEN: continue
                z = float(np.mean([vz[int(v)] for v in tags(kk)[:3]])); d = (j - j0) ** 2 + (i - i0) ** 2
                key = (round(z, 2), d)
                if best is None or key < best[0]: best = (key, j, i)
        return None if best is None else (best[1], best[2])
    doors_mode = q.get("bld_doors", "closed")
    rate = DOOR_LEAK.get(q.get("bld_door_type", "typical")) or float(q.get("bld_leak_06", 4000)) / 0.6   # L/h/m/m
    fail = float(q.get("bld_door_fail", 0) or 0); barrier = float(q.get("bld_barrier", 0) or 0)
    wmode = q.get("bld_windows", "watertight"); wbreak = float(q.get("bld_window_break") or 0)
    if wmode == "break" and wbreak <= 0: wmode = "watertight"                  # no breaking height set
    side, rules, nr = {}, [], 0
    for n, b in enumerate(blds):
        info = dict(mode=b["interior"]["mode"], spaces={}, openings=[], skipped=0)
        fpb = Polygon(b["footprint"])
        # interior volumes
        vol = {}
        if b["interior"]["mode"] == "spaces":
            for j, s in enumerate(b["spaces"]):
                if not s.get("poly") or s["area"] <= 0: continue
                node = f"IB{n}_{j}"; c = Polygon(s["poly"]).centroid
                out["storage"].append(f"{node} {s['floor']:.3f} {max(s.get('height') or 2.7, 0.5):.3f} 0 FUNCTIONAL 0 0 {s['area']:.3f} 0 0")
                out["coords"].append(f"{node} {c.x:.2f} {c.y:.2f}"); vol[s["name"]] = node
                info["spaces"][s["name"]] = dict(node=node, floor=s["floor"], area=s["area"], use=s.get("use"), storey=s.get("storey"))
            where = lambda o: vol.get(o["spaces"][0]) if o.get("spaces") else None
        else:
            levels = sorted(b.get("storeys") or [b["ffl"]])
            c = fpb.centroid
            for j, z in enumerate(levels):
                h = (levels[j + 1] - z) if j + 1 < len(levels) else 2.7
                node = f"IB{n}_s{j}"
                out["storage"].append(f"{node} {z:.3f} {max(h, 0.5):.3f} 0 FUNCTIONAL 0 0 {0.85 * fpb.area:.3f} 0 0")
                out["coords"].append(f"{node} {c.x:.2f} {c.y:.2f}"); vol[j] = node
                info["spaces"][f"Storey at {z:.2f} m"] = dict(node=node, floor=z, area=round(0.85 * fpb.area, 1), use="", storey=None)
            def where(o):
                ks = [j for j, z in enumerate(levels) if z <= o["sill"] + 0.05]
                return vol[ks[-1]] if ks else vol[0]
        # openings to the outside
        ops = [dict(o, is_door=True) for o in b.get("doors", []) if o.get("kind") == "external"] + \
              [dict(o, is_door=False) for o in b.get("windows", []) if o.get("kind") == "external"]
        for k, o in enumerate(ops):
            inside = where(o)
            c = cell_near(o["x"], o["y"])
            if c is not None and not o["is_door"]:                         # a window below the ground outside is a wall
                kk0 = first[c]; z0 = float(np.mean([vz[int(v)] for v in tags(kk0)[:3]]))
                if z0 > o["sill"] + 0.1:
                    info["skipped"] += 1; continue
            if inside is None or c is None:
                info["skipped"] += 1; continue
            taken.add(c); jj, ii = c; kk = first[(jj, ii)]
            z = float(np.mean([vz[int(v)] for v in tags(kk)[:3]]))
            on = f"IO{n}_{k}"; w = o.get("width") or (0.9 if o["is_door"] else 1.0)
            hgt = (o.get("height") or DOOR_HEIGHT) if o["is_door"] else max(0.3, (o.get("top") or o["sill"] + 1.2) - o["sill"])
            crest = o["sill"] + (barrier if o["is_door"] else 0.0)
            tg = f"bo{n}_{k}"
            for t in (kk, kk + 1):
                if t < len(tsq) and tuple(tsq[t]) == (jj, ii): tags(t)[5] = tg
            # the engine takes water from the surface into a node above its top, so the outside node's top is the
            # higher of the ground and the crest: water gets in only once it stands above the threshold
            top = max(z, crest) + 0.005
            out["junctions"].append(f"{on} {top - 0.3:.3f} 0.3 0 10.0 0")
            out["nodemap"].append(f"{tg} {on} 0.65 {min(1.0, max(0.05, 0.5 * w)):.3f}")
            out["coords"].append(f"{on} {o['x']:.2f} {o['y']:.2f}")
            # openings as outlets with a flow-head rule (no opening height to reach above the node), one each way
            c_full = 1.6 * w                                               # weir: Q = 1.6 * width * h^1.5
            # closed doors and windows leak in proportion to the water above the crest
            c_leak = rate * w / 3.6e6 * (1.0 if o["is_door"] else WINDOW_LEAK_FACTOR)   # m3/s per m above the crest
            lk, lr = f"IL{n}_{k}", f"IR{n}_{k}"
            out["outlets"].append(f"{lk} {on} {inside} {crest:.3f} FUNCTIONAL/HEAD {c_leak:.7f} 1.0 NO")
            out["outlets"].append(f"{lr} {inside} {on} {crest:.3f} FUNCTIONAL/HEAD {c_leak:.7f} 1.0 NO")
            full = (o["is_door"] and (doors_mode == "open" or fail > 0)) or (not o["is_door"] and wmode == "break")
            wl = None
            if full:
                wl, wr = f"IW{n}_{k}", f"IV{n}_{k}"
                out["outlets"].append(f"{wl} {on} {inside} {crest:.3f} FUNCTIONAL/HEAD {c_full:.4f} 1.5 NO")
                out["outlets"].append(f"{wr} {inside} {on} {crest:.3f} FUNCTIONAL/HEAD {c_full:.4f} 1.5 NO")
                h_fail = fail if o["is_door"] and doors_mode != "open" else (wbreak if not o["is_door"] else None)   # windows: watertight until they break
                if h_fail:                                                    # closed until the water outside reaches it,
                    lvl = crest + h_fail                                      # then open (and open once water is inside)
                    nr += 1
                    rules.append(f"RULE IFC{nr}a\nIF NODE {on} HEAD < {lvl:.3f}\nAND NODE {inside} DEPTH < 0.01\nTHEN OUTLET {wl} SETTING = 0\nAND OUTLET {wr} SETTING = 0")
                    rules.append(f"RULE IFC{nr}b\nIF NODE {on} HEAD >= {lvl:.3f}\nTHEN OUTLET {wl} SETTING = 1\nAND OUTLET {wr} SETTING = 1")
            info["openings"].append(dict(name=o["name"], door=o["is_door"], outside=on, leak=lk, full=wl, crest=round(crest, 3)))
        # internal doors (assumed open)
        if b["interior"]["mode"] == "spaces":
            for k, o in enumerate(d for d in b.get("doors", []) if d.get("kind") == "internal" and len(d.get("spaces", [])) == 2):
                a_, b_ = vol.get(o["spaces"][0]), vol.get(o["spaces"][1])
                if not a_ or not b_: continue
                wl = f"ID{n}_{k}"
                out["weirs"].append(f"{wl} {a_} {b_} TRANSVERSE {o['sill']:.3f} 1.6 NO 0 0")
                out["xsections"].append(f"{wl} RECT_OPEN {(o.get('height') or DOOR_HEIGHT):.3f} {(o.get('width') or 0.9):.3f} 0 0")
        side[b["id"]] = info
    for kk, p in tri.items(): lines[t0 + kk] = " ".join(p)
    out["controls"] = rules
    if sidecar:
        Path(sidecar).write_text(json.dumps(side))
    return out
