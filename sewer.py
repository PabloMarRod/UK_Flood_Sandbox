"""Drainage network import for an area: a SWMM input file (.inp, as exported by InfoWorks ICM, SewerGEMS or PCSWMM)
or GIS layers (GeoPackage, zipped shapefiles or a zipped ArcGIS file geodatabase) with manhole points and pipe lines.

The import runs in three steps:
  inspect(path)                -> what the file holds (layers, fields) and a suggested field mapping
  check(path, mapping, A, fixes) -> the network as read, every quality issue found, and the network after the fixes
                                  the user accepted
  save(...)                    -> written to the area only when nothing blocks it

Issue severities:
  error  the import stops; the data must be corrected in its source (GIS or the modelling software)
  fix    the app can repair it; applied only if the user accepts that fix, otherwise the import stops
  warn   imported as it is; noted so the user knows
"""
import json, math, re, shutil, zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

SHAPES = {"CIRCULAR": 1, "RECT_CLOSED": 2, "EGG": 1, "HORSESHOE": 1, "RECT_OPEN": 2, "TRAPEZOIDAL": 4,
          "TRIANGULAR": 2, "ELLIPTICAL": 2, "ARCH": 2, "FORCE_MAIN": 2, "BASKETHANDLE": 1}
UNSUPPORTED_INP = ("DIVIDERS",)
STRUCTURES = ("WEIRS", "ORIFICES", "OUTLETS", "PUMPS")
IGNORED_INP = ("SUBCATCHMENTS", "SUBAREAS", "INFILTRATION", "DWF", "INFLOWS", "RDII", "LOSSES", "LID_USAGE",
               "RAINGAGES", "POLLUTANTS", "LANDUSES", "BUILDUP", "WASHOFF", "TREATMENT", "GROUNDWATER", "CONTROLS")
DEFAULT_N = 0.013          # Manning's n for concrete or clay pipes in fair condition
DEFAULT_DEPTH = 1.5        # m, manhole depth assumed when no level at all is known
MAX_UPLOAD = 300_000_000  # bytes
SNAP_TOL = 1.0             # m, pipe end to manhole
SNAP_FAR = 5.0             # m, offered as a fix
MAX_MISSING_SIZE = 0.25    # share of pipes without a size above which the data is too poor to infill
MAX_MISSING_LEVEL = 0.5    # share of nodes without any level above which the data is too poor to infill

# field names seen in InfoWorks exports, water company GIS and Esri templates (lower case, no spaces)
SYN = {
    "node_id": ["node_id", "nodeid", "manholeref", "mh_ref", "mhref", "manhole_id", "manholeid", "assetid", "asset_id",
                "unitid", "reference", "ref", "id", "name", "facilityid", "node"],
    "cover": ["ground_level", "groundlevel", "cover_level", "coverlevel", "cl", "cover", "gl", "flood_level",
              "rim_elev", "rimelev", "rim", "surface_level"],
    "invert": ["chamber_floor", "chamberfloor", "invert_level", "invertlevel", "invert", "il", "inv", "base_level",
               "sump_level", "elevation", "elev"],
    "depth": ["depth", "chamber_depth", "mh_depth", "max_depth", "maxdepth"],
    "cover_type": ["cover_type", "covertype", "cover_kind", "lid_type", "lid", "flood_type", "floodtype", "cover_class",
                   "access_cover", "cover_desc"],
    "chamber_area": ["chamber_area", "chamberarea", "chamber_plan_area", "plan_area", "mh_area"],
    "chamber_size": ["chamber_size", "chambersize", "chamber_dia", "chamber_diameter", "chamber_width", "mh_size",
                     "mh_dia", "manhole_size", "shaft_size"],
    "node_type": ["node_type", "nodetype", "type", "category", "asset_type", "assettype", "feature_type"],
    "link_id": ["link_id", "linkid", "pipe_id", "pipeid", "pipe_ref", "assetid", "asset_id", "id", "name",
                "facilityid", "link_suffix"],
    "us_node": ["us_node_id", "usnodeid", "us_node", "from_node", "fromnode", "upstream", "us_mh", "start_node",
                "inlet_node", "from", "upmh"],
    "ds_node": ["ds_node_id", "dsnodeid", "ds_node", "to_node", "tonode", "downstream", "ds_mh", "end_node",
                "outlet_node", "to", "dsmh"],
    "size": ["conduit_width", "diameter", "dia", "pipe_size", "pipesize", "size", "width", "geom1", "bore",
             "nominal_diameter", "diam"],
    "height": ["conduit_height", "height", "geom2", "pipe_height"],
    "shape": ["shape", "pipe_shape", "shape_1", "profile", "section", "conduit_shape"],
    "us_invert": ["us_invert", "usinvert", "us_il", "usil", "up_invert", "invert_up", "start_invert", "us_level",
                  "inlet_offset", "inoffset"],
    "ds_invert": ["ds_invert", "dsinvert", "ds_il", "dsil", "down_invert", "invert_down", "end_invert", "ds_level",
                  "outlet_offset", "outoffset"],
    "length": ["conduit_length", "length", "pipe_length", "len", "shape_length"],
    "rough": ["bottom_roughness_n", "roughness", "manning", "manning_n", "n", "rough", "top_roughness_n",
              "bottom_roughness_cw"],
}
NODE_FIELDS = ("node_id", "cover", "invert", "depth", "node_type", "cover_type", "chamber_area", "chamber_size")
LINK_FIELDS = ("link_id", "us_node", "ds_node", "size", "height", "shape", "us_invert", "ds_invert", "length", "rough")
SHAPE_WORDS = {"CIRC": "CIRCULAR", "ROUND": "CIRCULAR", "CIRCULAR": "CIRCULAR", "C": "CIRCULAR", "RECT": "RECT_CLOSED",
               "RECTANGULAR": "RECT_CLOSED", "BOX": "RECT_CLOSED", "R": "RECT_CLOSED", "EGG": "EGG", "E": "EGG",
               "OVAL": "EGG", "HORSESHOE": "HORSESHOE", "ARCH": "ARCH", "ELLIPTICAL": "ELLIPTICAL"}
OUTFALL_WORDS = ("outfall", "outlet", "discharge", "out", "headwall", "cso")


def issue(code, sev, title, ids=(), detail="", fix=None):
    ids = [str(i) for i in ids]
    return dict(code=code, sev=sev, title=title, n=len(ids), ids=ids[:12], detail=detail, fix=fix)


# ------------------------------------------------------------------------------------------------ reading files
def unpack(path, work):
    """A zip holds shapefiles or a .gdb folder: extract it and return what GDAL should open."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        return path
    work = Path(work); shutil.rmtree(work, ignore_errors=True); work.mkdir(parents=True)
    with zipfile.ZipFile(path) as z:
        for m in z.infolist():                                  # no absolute paths or .. in members
            if m.filename.startswith("/") or ".." in Path(m.filename).parts:
                raise ValueError("the zip contains unsafe paths")
        z.extractall(work)
    gdbs = [p for p in work.rglob("*.gdb") if p.is_dir()]
    if gdbs:
        return gdbs[0]
    shps = sorted(work.rglob("*.shp"))
    if shps:
        return shps[0].parent
    gpkg = sorted(work.rglob("*.gpkg"))
    if gpkg:
        return gpkg[0]
    inp = sorted(work.rglob("*.inp"))
    if inp:
        return inp[0]
    raise ValueError("the zip holds no shapefiles, file geodatabase, GeoPackage or .inp file")


def kind_of(src):
    src = Path(src)
    return "inp" if src.suffix.lower() == ".inp" else "gis"


def read_layers(src):
    """Every vector layer: name, geometry kind, CRS (EPSG code or None), fields and rows (shapely geometries)."""
    import pyogrio
    from shapely import wkb
    src = Path(src)
    if src.is_dir() and src.suffix.lower() != ".gdb":            # a folder of shapefiles
        sources = [(p, None) for p in sorted(src.glob("*.shp"))]
    else:
        sources = [(src, l[0]) for l in pyogrio.list_layers(str(src))]
    out = []
    for path, layer in sources:
        meta, _, geom, fields = pyogrio.raw.read(str(path), layer=layer)
        name = layer or path.stem
        gt = (meta.get("geometry_type") or "").upper()
        kind = "point" if "POINT" in gt else "line" if "LINE" in gt else "other"
        epsg = None
        if meta.get("crs"):
            try:
                from pyproj import CRS
                epsg = CRS.from_user_input(meta["crs"]).to_epsg()
            except Exception:
                epsg = -1                                        # a CRS we can't identify
        cols = list(meta["fields"])
        rows = []
        for k in range(len(geom)):
            g = wkb.loads(geom[k]) if geom[k] is not None else None
            rows.append(dict(geom=g, **{c: _py(fields[n][k]) for n, c in enumerate(cols)}))
        out.append(dict(name=name, kind=kind, epsg=epsg, fields=cols, rows=rows))
    return out


def _py(v):
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, bytes):
        return v.decode(errors="ignore")
    return v


def read_inp(src):
    """Sections of a SWMM input file as lists of token lists (comments dropped)."""
    sec, cur = defaultdict(list), None
    for raw in Path(src).read_text(errors="ignore").split("\n"):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].upper(); continue
        if cur:
            sec[cur].append(line.split())
    return sec


def guess(fields, role_list):
    low = {re.sub(r"[\s\-]", "_", f.lower()): f for f in fields}
    out = {}
    for role in role_list:
        for s in SYN[role]:
            if s in low and low[s] not in out.values():
                out[role] = low[s]; break
    return out


def inspect(path, work):
    """What the upload holds and a suggested mapping (layers and fields)."""
    src = unpack(path, work)
    if kind_of(src) == "inp":
        sec = read_inp(src)
        return dict(kind="inp", source=str(src),
                    counts={k: len(v) for k, v in sec.items() if k in ("JUNCTIONS", "OUTFALLS", "CONDUITS") + UNSUPPORTED_INP})
    layers = read_layers(src)
    pts = [l for l in layers if l["kind"] == "point"]; lns = [l for l in layers if l["kind"] == "line"]
    pick = lambda ls, words: next((l for l in ls if any(w in l["name"].lower() for w in words)), ls[0] if ls else None)
    nl = pick(pts, ("manhole", "node", "chamber", "mh")); ll = pick(lns, ("pipe", "sewer", "link", "conduit", "main"))
    mapping = dict(node_layer=nl and nl["name"], link_layer=ll and ll["name"],
                   node_fields=guess(nl["fields"], NODE_FIELDS) if nl else {},
                   link_fields=guess(ll["fields"], LINK_FIELDS) if ll else {})
    return dict(kind="gis", source=str(src), mapping=mapping,
                layers=[dict(name=l["name"], kind=l["kind"], epsg=l["epsg"], fields=l["fields"], count=len(l["rows"]))
                        for l in layers])


# ------------------------------------------------------------------------------------------------ to a network
def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def from_inp(src):
    """Network from a SWMM file. Returns (net, issues, coordinates are in the file's own system)."""
    sec = read_inp(src); iss = []
    offsets = "DEPTH"
    for p in sec.get("OPTIONS", []):
        if p[0].upper() == "LINK_OFFSETS" and len(p) > 1: offsets = p[1].upper()
    xy = {p[0]: (float(p[1]), float(p[2])) for p in sec.get("COORDINATES", []) if len(p) >= 3}
    nodes, links, dup_n, dup_l = {}, {}, [], []
    for p in sec.get("JUNCTIONS", []):
        inv = _num(p[1]); md = _num(p[2]) if len(p) > 2 else None
        if p[0] in nodes: dup_n.append(p[0])
        sur = _num(p[4]) if len(p) > 4 else 0
        nodes[p[0]] = dict(kind="junction", invert=inv, cover=(inv + md) if inv is not None and md else None, depth=md or None,
                           cover_raw="sealed" if sur and sur > 0 else None, from_surdepth=bool(sur and sur > 0))
    odd_out = []
    for p in sec.get("OUTFALLS", []):
        if p[0] in nodes: dup_n.append(p[0])
        typ = p[2].upper() if len(p) > 2 else "FREE"
        rest = p[3:]
        stage = None
        if typ == "FIXED" and rest:
            stage = _num(rest[0]); rest = rest[1:]
        elif typ in ("TIDAL", "TIMESERIES") and rest:
            odd_out.append(f"{p[0]} ({typ})"); rest = rest[1:]
        gated = bool(rest) and rest[0].upper() == "YES"
        nodes[p[0]] = dict(kind="outfall", invert=_num(p[1]), cover=None, depth=None, gated=gated,
                           stage=stage if typ == "FIXED" else None)
    if odd_out: iss.append(issue("W_OUTFALL_TYPE", "warn", "Tidal or time-series outfalls treated as free", odd_out,
                                 "Inside the area the river and tide levels come from the 2D model; the curve or series isn't used."))
    for p in sec.get("STORAGE", []):
        if len(p) < 5: continue
        if p[0] in nodes: dup_n.append(p[0])
        inv = _num(p[1]); md = _num(p[2])
        nodes[p[0]] = dict(kind="storage", invert=inv, cover=None, depth=md, storage=p[1:], cover_type="sealed")
        nodes[p[0]]["x"], nodes[p[0]]["y"] = xy.get(p[0], (None, None))
    for nid, n in nodes.items():
        n["x"], n["y"] = xy.get(nid, (None, None))
    xs = {p[0]: p for p in sec.get("XSECTIONS", [])}
    bad_shape = []
    for p in sec.get("CONDUITS", []):
        if len(p) < 5: continue
        lid = p[0]
        x = xs.get(lid)
        if lid in links:
            dup_l.append(lid); n_ = 2
            while f"{lid}_{n_}" in links: n_ += 1
            lid = f"{lid}_{n_}"
        shape = x[1].upper() if x and len(x) > 1 else None
        if shape and shape not in SHAPES: bad_shape.append(f"{lid} ({shape})")
        g1 = _num(x[2]) if x and len(x) > 2 else None; g2 = _num(x[3]) if x and len(x) > 3 else None
        io = _num(p[5]) if len(p) > 5 else 0.0; oo = _num(p[6]) if len(p) > 6 else 0.0
        a, b = nodes.get(p[1]), nodes.get(p[2])
        if offsets == "ELEVATION":
            usi, dsi = io, oo
        else:
            usi = (a["invert"] + (io or 0)) if a and a["invert"] is not None else None
            dsi = (b["invert"] + (oo or 0)) if b and b["invert"] is not None else None
        links[lid] = dict(type="conduit", us=p[1], ds=p[2], length=_num(p[3]), n=_num(p[4]), shape=shape or "CIRCULAR",
                          g1=g1, g2=g2 if shape in ("RECT_CLOSED", "RECT_OPEN", "ELLIPTICAL", "ARCH", "TRIANGULAR") else None,
                          us_inv=usi, ds_inv=dsi, geom_len=None)
    curves = defaultdict(list)
    for p in sec.get("CURVES", []):
        curves[p[0]].append(p[1:])
    missing_curve, no_xs, n_struct = [], [], defaultdict(int)
    def crest(off, us):
        """Offsets as elevations (the app writes LINK_OFFSETS ELEVATION)."""
        o = _num(off)
        if o is None: return None
        if offsets == "ELEVATION": return o
        a = nodes.get(us)
        return (a["invert"] + o) if a and a.get("invert") is not None else None
    for secname, typ in (("WEIRS", "weir"), ("ORIFICES", "orifice"), ("OUTLETS", "outlet"), ("PUMPS", "pump")):
        for p in sec.get(secname, []):
            if len(p) < 4: continue
            lid = p[0]
            if lid in links:
                dup_l.append(lid); n_ = 2
                while f"{lid}_{n_}" in links: n_ += 1
                lid = f"{lid}_{n_}"
            l = dict(type=typ, us=p[1], ds=p[2], params=p[3:], xs=None, curve=None)
            if typ in ("weir", "orifice"):
                l["crest"] = crest(p[4] if len(p) > 4 else None, p[1])
                x = xs.get(p[0])
                if not x: no_xs.append(lid)
                else: l["xs"] = x[1:]
            elif typ == "outlet":
                l["crest"] = crest(p[3], p[1])
                if len(p) > 5 and p[4].upper().startswith("TABULAR"): l["curve"] = p[5]
            else:
                if p[3] != "*": l["curve"] = p[3]
            if l["curve"] and l["curve"] not in curves: missing_curve.append(f"{lid}: {l['curve']}")
            links[lid] = l; n_struct[secname.lower()] += 1
    for n in nodes.values():
        if n["kind"] == "storage" and len(n["storage"]) > 4 and n["storage"][3].upper() == "TABULAR" \
                and n["storage"][4] not in curves:
            missing_curve.append(n["storage"][4])
    nolevel = [k for k, l in links.items() if l.get("type") in ("weir", "orifice", "outlet") and l.get("crest") is None]
    if nolevel: iss.append(issue("E_STRUCT_LEVEL", "error", "Structures whose level can't be worked out", nolevel,
                                 "Their offset is relative to a node without an invert, or isn't a number."))
    if missing_curve: iss.append(issue("E_CURVE", "error", "Curves referenced but not in the file", missing_curve,
                                       "Add the missing [CURVES] entries in the source model."))
    if no_xs: iss.append(issue("E_STRUCT_XS", "error", "Weirs or orifices without a cross-section", no_xs,
                               "Each needs an [XSECTIONS] entry giving its opening."))
    shapes = {"TABULAR", "FUNCTIONAL"}
    odd_store = [k for k, n in nodes.items() if n["kind"] == "storage" and n["storage"][3].upper() not in shapes]
    if odd_store: iss.append(issue("E_STORAGE_SHAPE", "error", "Storage shapes this app can't read", odd_store,
                                   "Use a TABULAR curve or a FUNCTIONAL shape for these storage units."))
    n_store = sum(1 for n in nodes.values() if n["kind"] == "storage")
    if n_struct or n_store:
        one = dict(weirs="weir", orifices="orifice", outlets="outlet", pumps="pump")
        what = [f"{v} {k if v != 1 else one[k]}" for k, v in n_struct.items()] + \
               ([f"{n_store} storage unit{'s' if n_store != 1 else ''}"] if n_store else [])
        iss.append(issue("W_STRUCTURES", "warn", "Structures imported as in the source model", what,
                         "Weirs, orifices, outlets and pumps keep their settings; pumps run on their curves and "
                         "start-up and shut-off depths (control rules aren't imported). Storage units connect to "
                         "the surface only through the network."))
    net_curves = {k: v for k, v in curves.items()}
    if dup_n: iss.append(issue("E_DUP_NODE", "error", "Node names used more than once", dup_n,
                               "Pipes can't be connected reliably. Rename them in the source model."))
    if dup_l: iss.append(issue("F_DUP_LINK", "fix", "Pipe names used more than once", dup_l,
                               fix="Add a suffix (_2, _3) to the repeated names"))
    if bad_shape: iss.append(issue("E_SHAPE", "error", "Pipe shapes this app does not model", bad_shape,
                                   "Custom and irregular sections aren't supported. Replace them with a standard shape."))
    un = [f"{s} ({len(sec[s])})" for s in UNSUPPORTED_INP if sec.get(s)]
    if un: iss.append(issue("E_UNSUPPORTED", "error", "Structures this app can't import yet", un,
                            "Remove or simplify them in the source model (for example, replace a CSO weir with its "
                            "continuation pipe), then export again."))
    sealed = [k for k, n in nodes.items() if n.get("from_surdepth")]
    if sealed: iss.append(issue("W_SEALED", "warn", "Manholes with a surcharge depth treated as sealed", sealed,
                                "SWMM's surcharge depth means the manhole can pressurise without spilling."))
    ig = [s for s in IGNORED_INP if sec.get(s)]
    if ig: iss.append(issue("W_IGNORED", "warn", "Sections ignored", ig,
                            "Rain falls on the 2D surface here, so subcatchments, dry-weather and other inflows aren't "
                            "used. Pipes start empty."))
    missing_xy = [k for k, n in nodes.items() if n["x"] is None]
    if missing_xy: iss.append(issue("E_NO_XY", "error", "Nodes without coordinates", missing_xy,
                                    "Every node needs a position in [COORDINATES]."))
    return dict(nodes=nodes, links=links, curves=net_curves), iss


def from_gis(src, mapping):
    """Network from point (manholes) and line (pipes) layers, with the user's field mapping."""
    layers = {l["name"]: l for l in read_layers(src)}
    nl, ll = layers.get(mapping.get("node_layer")), layers.get(mapping.get("link_layer"))
    nf, lf = mapping.get("node_fields") or {}, mapping.get("link_fields") or {}
    iss = []
    if not ll:
        return dict(nodes={}, links={}), [issue("E_FIELDS", "error", "No pipe layer chosen", [], "Choose the layer with the pipes.")]
    crs = {ll["epsg"]} | ({nl["epsg"]} if nl else set())
    nodes, links, dup_n, dup_l = {}, {}, [], []
    if nl:
        if not nf.get("node_id"):
            iss.append(issue("E_FIELDS", "error", "Manhole layer: no ID field chosen", [], "Choose the field that names each manhole."))
        for k, r in enumerate(nl["rows"]):
            g = r["geom"]
            if g is None or g.is_empty: continue
            if g.geom_type == "MultiPoint": g = g.geoms[0]
            nid = str(r.get(nf.get("node_id")) if nf.get("node_id") else f"N{k + 1}")
            if nid in nodes: dup_n.append(nid)
            typ = str(r.get(nf["node_type"]) or "").lower() if nf.get("node_type") else ""
            nodes[nid] = dict(x=g.x, y=g.y, kind="outfall" if any(w in typ for w in OUTFALL_WORDS) else "junction",
                              cover=_num(r.get(nf.get("cover"))) if nf.get("cover") else None,
                              invert=_num(r.get(nf.get("invert"))) if nf.get("invert") else None,
                              depth=_num(r.get(nf.get("depth"))) if nf.get("depth") else None,
                              cover_raw=str(r.get(nf["cover_type"])).strip() if nf.get("cover_type") and r.get(nf["cover_type"]) not in (None, "") else None,
                              chamber_area=_num(r.get(nf.get("chamber_area"))) if nf.get("chamber_area") else None,
                              chamber_size=_num(r.get(nf.get("chamber_size"))) if nf.get("chamber_size") else None)
    if not lf.get("size"):
        iss.append(issue("E_FIELDS", "error", "Pipe layer: no size field chosen", [],
                         "Choose the field with the pipe diameter or width."))
    for k, r in enumerate(ll["rows"]):
        g = r["geom"]
        if g is None or g.is_empty: continue
        if g.geom_type == "MultiLineString":
            g = max(g.geoms, key=lambda s: s.length)
        lid = str(r.get(lf["link_id"])) if lf.get("link_id") and r.get(lf["link_id"]) is not None else f"P{k + 1}"
        if lid in links:
            dup_l.append(lid); n_ = 2
            while f"{lid}_{n_}" in links: n_ += 1
            lid = f"{lid}_{n_}"
        shp = str(r.get(lf["shape"]) or "").upper().strip() if lf.get("shape") else ""
        links[lid] = dict(us=str(r.get(lf["us_node"])) if lf.get("us_node") and r.get(lf["us_node"]) is not None else None,
                          ds=str(r.get(lf["ds_node"])) if lf.get("ds_node") and r.get(lf["ds_node"]) is not None else None,
                          shape=SHAPE_WORDS.get(shp, SHAPE_WORDS.get(shp[:4], "CIRCULAR")) if shp else "CIRCULAR",
                          raw_shape=shp or None,
                          g1=_num(r.get(lf["size"])) if lf.get("size") else None,
                          g2=_num(r.get(lf["height"])) if lf.get("height") else None,
                          us_inv=_num(r.get(lf["us_invert"])) if lf.get("us_invert") else None,
                          ds_inv=_num(r.get(lf["ds_invert"])) if lf.get("ds_invert") else None,
                          length=_num(r.get(lf["length"])) if lf.get("length") else None,
                          n=_num(r.get(lf["rough"])) if lf.get("rough") else None,
                          geom_len=g.length, ends=[tuple(g.coords[0][:2]), tuple(g.coords[-1][:2])])
    if dup_n: iss.append(issue("E_DUP_NODE", "error", "Manhole IDs used more than once", dup_n,
                               "Pipes can't be connected reliably. Make the IDs unique in the source data."))
    if dup_l: iss.append(issue("F_DUP_LINK", "fix", "Pipe IDs used more than once", dup_l,
                               fix="Add a suffix (_2, _3) to the repeated IDs"))
    unknown_shape = sorted({l["raw_shape"] for l in links.values() if l["raw_shape"] and l["raw_shape"] not in SHAPE_WORDS
                            and l["raw_shape"][:4] not in SHAPE_WORDS})
    if unknown_shape:
        iss.append(issue("F_SHAPE", "fix", "Pipe shapes not recognised", unknown_shape,
                         fix="Treat them as circular pipes of the given size"))
    return dict(nodes=nodes, links=links, crs=sorted(c for c in crs if c is not None), any_crs_missing=None in crs), iss


# ------------------------------------------------------------------------------------------------ the area
class Ground:
    """Ground level of the model surface at a point, and whether that cell is open ground."""
    def __init__(self, A):
        H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
        self.H, self.X0, self.Y1, self.NX, self.NY = H, X0, Y1, NX, NY
        cache = Path(A["dir"]) / "cache"
        L = (cache / "mesh_block.txt").read_text().split("\n")
        v0 = L.index("[2D_VERTICES]") + 1; t0 = L.index("[2D_TRIANGLES]") + 1
        vz = np.array([float(l.split()[2]) for l in L[v0:t0 - 2] if len(l.split()) >= 3])
        tsq = np.load(cache / "mesh.npz")["tsq"]
        self.z = np.full((NY, NX), np.nan); self.tag = {}
        for k, (j, i) in enumerate(tsq.tolist()):
            if np.isnan(self.z[j, i]):
                p = L[t0 + k].split()
                self.z[j, i] = float(np.mean([vz[int(v)] for v in p[:3]])); self.tag[(j, i)] = p[5]
        from shapely.geometry import Polygon
        self.poly = Polygon(A["poly_bng"])

    def cell(self, x, y):
        j, i = int((self.Y1 - y) // self.H), int((x - self.X0) // self.H)
        return (j, i) if 0 <= j < self.NY and 0 <= i < self.NX else None

    def at(self, x, y, r=2):
        """Ground at the cell, or the nearest model cell within r cells (buildings are holes in the mesh)."""
        c = self.cell(x, y)
        if c is None:
            return None
        j0, i0 = c
        best = None
        for j in range(max(0, j0 - r), min(self.NY, j0 + r + 1)):
            for i in range(max(0, i0 - r), min(self.NX, i0 + r + 1)):
                if not np.isnan(self.z[j, i]):
                    d = (j - j0) ** 2 + (i - i0) ** 2
                    if best is None or d < best[0]: best = (d, self.z[j, i])
        return None if best is None else float(best[1])

    def inside(self, x, y):
        from shapely.geometry import Point
        return self.poly.contains(Point(x, y))


def place_crs(net, kind, ground, declared):
    """Find the coordinate system that puts the network on the area. Returns (epsg or None, share inside)."""
    from pyproj import Transformer
    pts = [(n["x"], n["y"]) for n in net["nodes"].values() if n.get("x") is not None]
    pts += [e for l in net["links"].values() for e in (l.get("ends") or [])]
    if not pts:
        return None, 0.0
    pts = pts[:: max(1, len(pts) // 400)]
    cands = ([declared] if declared else []) + [27700, 29903, 29902, 2157, 4326, 3857]
    best = (None, 0.0)
    for c in dict.fromkeys(cands):
        try:
            tf = Transformer.from_crs(c, 27700, always_xy=True)
            share = float(np.mean([ground.poly.buffer(200).contains(_P(*tf.transform(x, y))) for x, y in pts]))
        except Exception:
            continue
        if share > best[1] + 1e-9:
            best = (c, share)
        if c == declared and share > 0.5:
            return c, share
    return best


def _P(x, y):
    from shapely.geometry import Point
    return Point(x, y)


def reproject(net, epsg):
    if epsg in (None, 27700):
        return
    from pyproj import Transformer
    tf = Transformer.from_crs(epsg, 27700, always_xy=True)
    for n in net["nodes"].values():
        if n.get("x") is not None: n["x"], n["y"] = tf.transform(n["x"], n["y"])
    for l in net["links"].values():
        if l.get("ends"): l["ends"] = [tf.transform(*e) for e in l["ends"]]
        if l.get("ends"): l["geom_len"] = math.dist(*l["ends"]) if l.get("geom_len") is None else l["geom_len"]


# ------------------------------------------------------------------------------------------------ checks and fixes
def _components(net):
    adj = defaultdict(set)
    for l in net["links"].values():
        if l["us"] in net["nodes"] and l["ds"] in net["nodes"]:
            adj[l["us"]].add(l["ds"]); adj[l["ds"]].add(l["us"])
    seen, comps = set(), []
    for n in net["nodes"]:
        if n in seen or n not in adj: continue
        stack, comp = [n], []
        seen.add(n)
        while stack:
            a = stack.pop(); comp.append(a)
            for b in adj[a]:
                if b not in seen: seen.add(b); stack.append(b)
        comps.append(comp)
    return comps


def _c(links):
    """The conduits among the links (weirs, orifices, outlets and pumps have no pipe size or inverts)."""
    return {k: l for k, l in links.items() if l.get("type", "conduit") == "conduit"}


def analyse(net, kind, ground, accepted, base_issues):
    """Run every check. Fixes the user accepted are applied as they are met, so later checks see the repaired
    network. Returns the issues (each marked applied or not) and the network."""
    acc = set(accepted or []); iss = list(base_issues); nodes, links = net["nodes"], net["links"]
    def add(i):
        # every fix is applied provisionally, so all the issues show in one pass; the import only goes ahead if the
        # user accepted all of them (see check)
        i["applied"] = i["sev"] == "fix" and i["code"] in acc
        iss.append(i); return i["sev"] == "fix"
    for i in iss:
        i["applied"] = i["sev"] == "fix" and i["code"] in acc
    if not links:
        add(issue("E_EMPTY", "error", "No pipes found", [], "The file holds no pipes, or the wrong layer was chosen."))
        return iss, net

    # coordinates
    declared = (net.get("crs") or [None])[0] if kind == "gis" else 27700
    epsg, share = place_crs(net, kind, ground, declared if declared and declared > 0 else None)
    if share < 0.05:
        add(issue("E_OUTSIDE", "error", "The network doesn't fall on this area", [],
                  "No coordinate system tried (British National Grid, Irish Grid, ITM, WGS84) places it here. "
                  "Check the coordinates and export in British National Grid (EPSG:27700)."))
        return iss, net
    if kind == "gis" and net.get("any_crs_missing") and epsg:
        if not add(issue("F_CRS", "fix", "A layer has no coordinate system", [f"EPSG:{epsg}"],
                         fix=f"Read it as EPSG:{epsg}, which places the network on this area")):
            return iss, net
    elif epsg != (declared if declared and declared > 0 else 27700):
        if not add(issue("F_CRS", "fix", "Coordinates are in another system", [f"EPSG:{epsg}"],
                         fix=f"Convert from EPSG:{epsg} to British National Grid")):
            return iss, net
    reproject(net, epsg)

    # pipe ends to manholes (GIS)
    if kind == "gis":
        from scipy.spatial import cKDTree
        ids = list(nodes); tree = cKDTree([(nodes[k]["x"], nodes[k]["y"]) for k in ids]) if ids else None
        far, unknown = [], []
        for lid, l in links.items():
            for end, key in ((0, "us"), (1, "ds")):
                if l[key] is not None and l[key] in nodes:
                    continue
                if l[key] is not None and l[key] not in nodes:
                    unknown.append(f"{lid}: {l[key]}")
                d, k = tree.query(l["ends"][end]) if tree is not None else (1e9, None)
                l[key + "_snap"] = (d, ids[k] if k is not None else None)
        if unknown:
            add(issue("E_UNKNOWN_NODE", "error", "Pipes name manholes that aren't in the manhole layer", unknown,
                      "Add the missing manholes or correct the pipe's upstream/downstream IDs."))
            return iss, net
        far = [lid for lid, l in links.items() for key in ("us", "ds")
               if l[key] is None and l[key + "_snap"][0] > SNAP_TOL]
        for lid, l in links.items():                             # within tolerance: connect
            for key in ("us", "ds"):
                if l[key] is None and l[key + "_snap"][0] <= SNAP_TOL: l[key] = l[key + "_snap"][1]
        if far:
            if add(issue("F_SNAP", "fix", f"Pipe ends more than {SNAP_TOL:g} m from a manhole", sorted(set(far)),
                         fix=f"Connect ends within {SNAP_FAR:g} m to the nearest manhole, and add a junction at ends "
                             "further away")):
                for lid, l in links.items():
                    for end, key in ((0, "us"), (1, "ds")):
                        if l[key] is None:
                            d, nid = l[key + "_snap"]
                            if d <= SNAP_FAR: l[key] = nid
                            else:
                                nid = f"{lid}_{key}"; x, y = l["ends"][end]
                                nodes[nid] = dict(x=x, y=y, kind="junction", cover=None, invert=None, depth=None, added=True)
                                l[key] = nid
            else:
                return iss, net
    else:
        unknown = [f"{lid}: {n}" for lid, l in links.items() for n in (l["us"], l["ds"]) if n not in nodes]
        if unknown:
            add(issue("E_UNKNOWN_NODE", "error", "Pipes connect to nodes that don't exist", unknown,
                      "Correct the pipe ends in the source model."))
            return iss, net

    # the area: keep what lies on it
    inside = {k: ground.inside(n["x"], n["y"]) for k, n in nodes.items()}
    out_l = [lid for lid, l in links.items() if not (inside[l["us"]] and inside[l["ds"]])]
    if not any(inside.values()):
        add(issue("E_OUTSIDE", "error", "No manhole lies inside this area", [], "Check that the file covers the area."))
        return iss, net
    if out_l:
        lost = [lid for lid in out_l if not inside[links[lid]["us"]] and inside[links[lid]["ds"]]]
        if add(issue("F_CLIP", "fix", "Parts of the network extend beyond this area", out_l,
                     detail=(f"{len(lost)} pipe(s) bring flow in from outside; that inflow would be lost." if lost else ""),
                     fix="Keep the pipes inside. Pipes leaving the area end at a free outfall where they leave; "
                         "pipes entering from outside are dropped")):
            for lid in out_l:
                l = links[lid]
                if inside[l["us"]] and not inside[l["ds"]]:
                    nodes[l["ds"]]["kind"] = "outfall"; nodes[l["ds"]]["outside"] = True
                elif not inside[l["us"]]:
                    del links[lid]
            used = {n for l in links.values() for n in (l["us"], l["ds"])}
            for k in [k for k in nodes if k not in used and not inside[k]]: del nodes[k]
        else:
            return iss, net

    # sizes
    sizes = [l["g1"] for l in _c(links).values() if l["g1"]]
    if sizes and np.median(sizes) > 20:
        if add(issue("F_MM", "fix", "Pipe sizes look like millimetres", [f"median {np.median(sizes):g}"],
                     fix="Divide sizes by 1,000 to get metres")):
            for l in _c(links).values():
                for g in ("g1", "g2"):
                    if l[g]: l[g] /= 1000
        else:
            return iss, net
    miss = [lid for lid, l in _c(links).items() if not l["g1"] or l["g1"] <= 0]
    if miss:
        if len(miss) > MAX_MISSING_SIZE * len(links):
            add(issue("E_SIZE", "error", "Too many pipes without a size", miss,
                      f"{len(miss)} of {len(links)} pipes. Fill in sizes in the source data (more than "
                      f"{MAX_MISSING_SIZE:.0%} missing is too many to estimate)."))
            return iss, net
        if add(issue("F_SIZE", "fix", "Pipes without a size", miss,
                     fix="Use the size of the next pipe downstream (or upstream), or 225 mm if neither has one")):
            by_us = defaultdict(list); by_ds = defaultdict(list)
            for lid, l in _c(links).items(): by_us[l["us"]].append(lid); by_ds[l["ds"]].append(lid)
            for lid in miss:
                l = links[lid]
                near = [links[k]["g1"] for k in by_us[l["ds"]] + by_ds[l["us"]] if links[k]["g1"]]
                l["g1"] = near[0] if near else 0.225; l["size_filled"] = True
        else:
            return iss, net
    big = [f"{lid} ({l['g1']:g} m)" for lid, l in _c(links).items() if l["g1"] and l["g1"] > 4]
    if big:
        add(issue("E_SIZE_BIG", "error", "Implausibly large pipes", big, "Check the size units in the source data."))
        return iss, net
    for l in _c(links).values():
        if l["shape"] in ("RECT_CLOSED", "RECT_OPEN", "ELLIPTICAL", "ARCH") and not l.get("g2"):
            l["g2"] = l["g1"]

    # levels
    gz = {k: ground.at(n["x"], n["y"]) for k, n in nodes.items()}
    for k, n in nodes.items():
        n["ground"] = gz[k]
    covers = [(n["cover"] - gz[k]) for k, n in nodes.items() if n.get("cover") is not None and gz[k] is not None]
    if covers and abs(np.median(covers)) > 2:
        add(issue("E_DATUM", "error", "Levels don't match the ground", [f"median difference {np.median(covers):+.1f} m"],
                  "Cover levels differ from the LiDAR ground by more than 2 m on average. Levels must be in metres "
                  "above Ordnance Datum (Malin Head for NI data in Irish Grid)."))
        return iss, net
    no_level = [k for k, n in nodes.items() if n.get("invert") is None and n.get("depth") is None
                and not any(l[f"{e}_inv"] is not None for l in _c(links).values() for e in ("us", "ds") if l[e] == k)]
    if len(no_level) > MAX_MISSING_LEVEL * len(nodes):
        add(issue("E_LEVEL", "error", "Too many manholes without any level", no_level,
                  f"{len(no_level)} of {len(nodes)} have no invert, depth or pipe invert. Fill in levels in the source data."))
        return iss, net
    miss_cover = [k for k, n in nodes.items() if n.get("cover") is None and not n.get("outside") and n["kind"] == "junction"]
    if miss_cover:
        if add(issue("F_COVER", "fix", "Manholes without a cover level", miss_cover,
                     fix="Use the LiDAR ground level at each manhole")):
            for k in miss_cover:
                nodes[k]["cover"] = gz[k]; nodes[k]["cover_filled"] = True
        else:
            return iss, net
    miss_inv = [k for k, n in nodes.items() if n.get("invert") is None]
    if miss_inv:
        if add(issue("F_INVERT", "fix", "Manholes without an invert level", miss_inv,
                     fix=f"Take the lowest connected pipe invert; else cover level minus depth; else interpolate along "
                         f"the pipes from manholes with levels; else {DEFAULT_DEPTH:g} m below cover")):
            _fill_inverts(net, miss_inv)
        else:
            return iss, net
    miss_pinv = [lid for lid, l in _c(links).items() if l["us_inv"] is None or l["ds_inv"] is None]
    if miss_pinv:
        if add(issue("F_PIPE_INVERT", "fix", "Pipes without invert levels", miss_pinv,
                     fix="Use the invert of the manhole at that end")):
            for lid in miss_pinv:
                l = links[lid]
                if l["us_inv"] is None: l["us_inv"] = nodes[l["us"]]["invert"]
                if l["ds_inv"] is None: l["ds_inv"] = nodes[l["ds"]]["invert"]
        else:
            return iss, net
    above = [lid for lid, l in _c(links).items()
             for e, key in ((l["us"], "us_inv"), (l["ds"], "ds_inv"))
             if nodes[e].get("cover") is not None and l[key] + l["g1"] > nodes[e]["cover"] + 0.05 and nodes[e]["kind"] != "outfall"]
    if above:
        add(issue("E_ABOVE", "error", "Pipes above the ground", sorted(set(above)),
                  "The pipe's top is above the manhole cover. Check the levels (or units) in the source data."))
        return iss, net
    below = sorted({e for l in _c(links).values() for e, key in ((l["us"], "us_inv"), (l["ds"], "ds_inv"))
                    if l[key] < nodes[e]["invert"] - 0.005})
    if below:
        if add(issue("F_BELOW", "fix", "Pipes below the manhole floor", below,
                     fix="Lower each manhole's invert to its lowest pipe")):
            for e in below:
                nodes[e]["invert"] = min(l[k] for l in _c(links).values() for n_, k in ((l["us"], "us_inv"), (l["ds"], "ds_inv")) if n_ == e)
        else:
            return iss, net

    # roughness
    rs = [l["n"] for l in _c(links).values() if l["n"] is not None]
    odd = [lid for lid, l in _c(links).items() if l["n"] is None or not (0.008 <= l["n"] <= 0.05)]
    if odd:
        cw = rs and np.median(rs) > 0.05
        if add(issue("F_ROUGH", "fix", "Roughness missing or not a Manning's n", odd,
                     detail="The values look like Colebrook-White roughness (mm), which SWMM doesn't use." if cw else "",
                     fix=f"Use Manning's n = {DEFAULT_N} (concrete or clay pipe)")):
            for lid in odd: links[lid]["n"] = DEFAULT_N
        else:
            return iss, net

    # connectivity
    used = {n for l in links.values() for n in (l["us"], l["ds"])}
    lone = [k for k in nodes if k not in used]
    if lone:
        if add(issue("F_ISOLATED", "fix", "Manholes with no pipe", lone, fix="Leave them out")):
            for k in lone: del nodes[k]
        else:
            return iss, net
    leaving = sorted({l["us"] for l in links.values() if nodes[l["us"]]["kind"] == "outfall"})
    if leaving:
        if add(issue("F_OUT_LEAVING", "fix", "Outfalls with pipes leaving them", leaving,
                     fix="Treat them as manholes (a part left without an outfall is handled by the next check)")):
            for k in leaving: nodes[k]["kind"] = "junction"
        else:
            return iss, net
    into = defaultdict(list)
    for lid, l in links.items():
        if nodes[l["ds"]]["kind"] == "outfall": into[l["ds"]].append(lid)
    multi = sorted(k for k, v in into.items() if len(v) > 1)
    if multi:
        if add(issue("F_OUT_MULTI", "fix", "Outfalls with more than one pipe", multi,
                     detail="SWMM allows one pipe per outfall.",
                     fix="Join the pipes at a new junction 1 m upstream, with a pipe the size of the largest to the outfall")):
            for k in multi:
                o = nodes[k]; jid = f"{k}_J"
                big = max(into[k], key=lambda lid: links[lid].get("g1") or 0)
                lx, ly = nodes[links[big]["us"]]["x"], nodes[links[big]["us"]]["y"]
                d = math.hypot(lx - o["x"], ly - o["y"]) or 1
                nodes[jid] = dict(x=o["x"] + (lx - o["x"]) / d, y=o["y"] + (ly - o["y"]) / d, kind="junction",
                                  invert=o["invert"], cover=o.get("cover") if o.get("cover") is not None else o.get("ground"),
                                  ground=o.get("ground"), added=True, outside=o.get("outside"))
                for lid in into[k]: links[lid]["ds"] = jid
                links[f"{k}_L"] = dict(type="conduit", us=jid, ds=k, shape=links[big].get("shape", "CIRCULAR"),
                                       g1=links[big].get("g1") or 1.0, g2=links[big].get("g2"),
                                       us_inv=o["invert"], ds_inv=o["invert"], length=2.0, geom_len=1.0, n=DEFAULT_N, added=True)
        else:
            return iss, net
    noout = [c for c in _components(net) if not any(nodes[k]["kind"] == "outfall" for k in c)]
    if noout:
        deg, ends_in = defaultdict(int), set()
        for l in links.values(): deg[l["us"]] += 1; deg[l["ds"]] += 1; ends_in.add(l["ds"])
        sinks = [[k for k in c if deg[k] == 1 and k in ends_in] for c in noout]
        if any(not s_ for s_ in sinks):
            add(issue("E_NO_OUTFALL", "error", "Parts of the network have no outfall and no end manhole to use as one",
                      [c[0] for c, s_ in zip(noout, sinks) if not s_], "Add the outfall in the source data."))
            return iss, net
        low = [min(s_, key=lambda k: nodes[k]["invert"]) for s_ in sinks]
        if add(issue("F_OUTFALL", "fix", "Parts of the network have no outfall", low,
                     detail=f"{len(noout)} connected part(s) with nowhere to discharge.",
                     fix="Make the lowest end manhole (one pipe) of each part a free outfall")):
            for k in low: nodes[k]["kind"] = "outfall"; nodes[k]["outfall_added"] = True
        else:
            return iss, net

    # cover types and chambers
    raw = {k: n.get("cover_raw") for k, n in nodes.items() if n["kind"] != "outfall"}
    unknown = sorted({v for v in raw.values() if v and cover_kind(v) == "?"})
    if unknown:
        if add(issue("F_COVER_TYPE", "fix", "Cover types not recognised", unknown,
                     fix="Treat those manholes as the scenario's default cover (solid unless you change it)")):
            pass
        else:
            return iss, net
    for k, v in raw.items():
        c = cover_kind(v) if v else None
        nodes[k]["cover_type"] = c if c in ("solid", "grated", "sealed") else None
    sizes = [n["chamber_size"] for n in nodes.values() if n.get("chamber_size")]
    mm = bool(sizes) and np.median(sizes) > 20
    for n in nodes.values():
        if n.get("chamber_size") and not n.get("chamber_area"):
            d = n["chamber_size"] / (1000 if mm else 1); n["chamber_area"] = math.pi * d * d / 4
    if mm:
        add(issue("W_CHAMBER_MM", "warn", "Chamber sizes read as millimetres", [f"median {np.median(sizes):g}"],
                  "Converted to metres; areas assume circular chambers."))
    odd = [f"{k} ({n['chamber_area']:g})" for k, n in nodes.items() if n.get("chamber_area") and not (0.2 <= n["chamber_area"] <= 30)]
    if odd:
        if add(issue("F_CHAMBER", "fix", "Chamber areas outside 0.2 to 30 m²", odd,
                     fix="Use the scenario's default chamber area for them")):
            for k, n in nodes.items():
                if n.get("chamber_area") and not (0.2 <= n["chamber_area"] <= 30): n["chamber_area"] = None
        else:
            return iss, net

    # warnings
    diff = [f"{k} ({n['cover'] - n['ground']:+.1f} m)" for k, n in nodes.items()
            if n.get("cover") is not None and n.get("ground") is not None and not n.get("cover_filled")
            and abs(n["cover"] - n["ground"]) > 0.5]
    if diff: add(issue("W_COVER", "warn", "Cover levels differ from the LiDAR ground by more than 0.5 m", diff,
                       "The model surface uses the LiDAR ground; each manhole reaches up to it."))
    adv = [lid for lid, l in _c(links).items() if l["ds_inv"] > l["us_inv"] + 0.01]
    if adv: add(issue("W_ADVERSE", "warn", "Pipes falling uphill", adv, "Imported as they are; check if this is intended."))
    short = [lid for lid, l in _c(links).items() if (l.get("length") or l.get("geom_len") or 0) < 2]
    if short: add(issue("W_SHORT", "warn", "Pipes shorter than 2 m", short, "Kept at 2 m in the model for stability."))
    mism = [lid for lid, l in _c(links).items() if l.get("length") and l.get("geom_len") and
            abs(l["length"] - l["geom_len"]) > 0.2 * max(l["geom_len"], 5)]
    if mism: add(issue("W_LENGTH", "warn", "Stated length differs from the drawn length by more than 20%", mism,
                       "The stated length is used."))
    deep = [k for k, n in nodes.items() if n.get("cover") is not None and n["cover"] - n["invert"] > 10]
    if deep: add(issue("W_DEEP", "warn", "Manholes deeper than 10 m", deep))
    return iss, net


def _fill_inverts(net, miss):
    nodes, links = net["nodes"], net["links"]
    for k in miss:
        n = nodes[k]
        pinv = [l[f"{e}_inv"] for l in _c(links).values() for e in ("us", "ds") if l[e] == k and l[f"{e}_inv"] is not None]
        if pinv:
            n["invert"] = min(pinv); n["invert_filled"] = "pipe"
        elif n.get("depth") and n.get("cover") is not None:
            n["invert"] = n["cover"] - n["depth"]; n["invert_filled"] = "depth"
    left = [k for k in miss if nodes[k].get("invert") is None]
    for _ in range(50):                                          # interpolate along pipes by graph distance
        if not left: break
        adj = defaultdict(list)
        for l in links.values():
            L = l.get("length") or l.get("geom_len") or 20
            adj[l["us"]].append((l["ds"], L)); adj[l["ds"]].append((l["us"], L))
        done = []
        for k in left:
            known = [(nodes[o]["invert"], L) for o, L in adj[k] if nodes[o].get("invert") is not None]
            depths = [nodes[o]["cover"] - nodes[o]["invert"] for o, _ in adj[k]
                      if nodes[o].get("invert") is not None and nodes[o].get("cover") is not None]
            if known and nodes[k].get("cover") is not None and depths:
                nodes[k]["invert"] = nodes[k]["cover"] - float(np.mean(depths)); nodes[k]["invert_filled"] = "neighbours"
                done.append(k)
        left = [k for k in left if k not in done]
        if not done: break
    for k in left:
        n = nodes[k]
        top = n["cover"] if n.get("cover") is not None else n.get("ground")
        if top is None:
            continue                                            # no level anywhere: reported by the caller
        n["invert"] = top - DEFAULT_DEPTH
        n["invert_filled"] = "default"


def check(path, mapping, A, accepted, work):
    """Read, check and repair. Returns dict(issues, status, summary, network)."""
    src = unpack(path, work)
    kind = kind_of(src)
    net, base = (from_inp(src) if kind == "inp" else from_gis(src, mapping or {}))
    if any(i["sev"] == "error" for i in base):
        iss = [dict(i, applied=False) for i in base]
    else:
        iss, net = analyse(net, kind, Ground(A), accepted, base)
    errors = [i for i in iss if i["sev"] == "error"]
    pending = [i for i in iss if i["sev"] == "fix" and not i["applied"]]
    accepted = set(accepted or [])
    for i in iss:
        if i["sev"] == "fix": i["applied"] = i["code"] in accepted
    status = "blocked" if errors else "needs_fixes" if pending else "ok"
    summ = dict(nodes=len(net["nodes"]), pipes=len(_c(net["links"])), structures=len(net["links"]) - len(_c(net["links"])),
                outfalls=sum(1 for n in net["nodes"].values() if n["kind"] == "outfall"),
                length_km=round(sum((l.get("length") or l.get("geom_len") or 0) for l in _c(net["links"]).values()) / 1000, 2))
    return dict(kind=kind, status=status, issues=iss, summary=summ, network=net if status == "ok" else None)


def save(A, result, source_name):
    """Write the checked network to the area (only an 'ok' result)."""
    if result["status"] != "ok":
        raise ValueError("the network has unresolved issues")
    net = result["network"]
    d = Path(A["dir"]) / "sewer"; d.mkdir(exist_ok=True)
    clean = dict(nodes={k: {f: v for f, v in n.items() if f in ("x", "y", "kind", "invert", "cover", "ground", "outside",
                                                                  "cover_filled", "invert_filled", "outfall_added", "added", "cover_type",
                                                                  "chamber_area", "gated", "stage", "storage")}
                        for k, n in net["nodes"].items()},
                 links={k: {f: v for f, v in l.items() if f in ("us", "ds", "shape", "g1", "g2", "us_inv", "ds_inv", "length",
                                                                  "geom_len", "n", "size_filled", "ends", "type", "params", "xs",
                                                                  "curve", "crest")}
                        for k, l in net["links"].items()},
                 curves=net.get("curves") or {}, source=source_name, summary=result["summary"],
                 issues=[{k: v for k, v in i.items() if k != "ids"} for i in result["issues"]])
    (d / "network.json").write_text(json.dumps(clean))
    return clean


def load(A):
    p = Path(A["dir"]) / "sewer" / "network.json"
    return json.loads(p.read_text()) if p.exists() else None


def remove(A):
    shutil.rmtree(Path(A["dir"]) / "sewer", ignore_errors=True)
    shutil.rmtree(Path(A["dir"]) / "sewer_upload", ignore_errors=True)


def geojson(A):
    """Pipes and manholes in WGS84 for the map."""
    net = load(A)
    if not net:
        return None
    from pyproj import Transformer
    tf = Transformer.from_crs(27700, 4326, always_xy=True).transform
    feats = []
    for k, l in net["links"].items():
        a, b = net["nodes"][l["us"]], net["nodes"][l["ds"]]
        feats.append(dict(type="Feature", properties=dict(id=k, kind=l.get("type", "conduit"), size=l.get("g1"),
                                                          filled=bool(l.get("size_filled"))),
                          geometry=dict(type="LineString", coordinates=[tf(a["x"], a["y"]), tf(b["x"], b["y"])])))
    for k, n in net["nodes"].items():
        feats.append(dict(type="Feature", properties=dict(id=k, kind=n["kind"], filled=bool(n.get("invert_filled")),
                                                          cover=n.get("cover_type"), chamber=n.get("chamber_area")),
                          geometry=dict(type="Point", coordinates=tf(n["x"], n["y"]))))
    return dict(type="FeatureCollection", features=feats, summary=net["summary"], source=net["source"])


# ------------------------------------------------------------------------------------------------ into the model
def swmm_id(s, used):
    b = "S_" + re.sub(r"[^A-Za-z0-9_.\-]", "_", str(s))[:28]
    k, n = b, 1
    while k in used:
        n += 1; k = f"{b}_{n}"
    used.add(k); return k


COVER_WORDS = {"sealed": ("seal", "bolt", "lock", "pressur"), "grated": ("grat", "grid", "vent", "open", "gully", "slot"),
               "solid": ("solid", "standard", "normal", "plain", "closed", "lost", "stored", "manhole", "mh")}
SUR_H = 10.0               # m a manhole may rise above ground before SWMM reports flooding (the cover orifice limits it)


def cover_kind(v):
    t = str(v or "").strip().lower()
    if not t:
        return None
    for k, words in COVER_WORDS.items():
        if any(w in t for w in words): return k
    return "?"


def model_lines(A, q, lines, cache, net, sidecar=None):
    """SWMM lines for the network, coupled to the 2D surface. Edits triangle tags in place.

    Each manhole is a storage node with its chamber's plan area. How it meets the surface depends on its cover:
      solid   water enters through a one-way gully inlet (triangle A of its cell -> gully node -> flap orifice) and
              leaves only when the manhole's head passes the ground, through a one-way orifice the size of the cover
              opening (-> cover node -> triangle B)
      grated  one two-way link through the grate's open area
      sealed  no link: the manhole pressurises (a junction with a large surcharge depth)
    """
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
    def free_cell(x, y, r, allowed):
        j0, i0 = int((Y1 - y) // H), int((x - X0) // H)
        for rr in range(0, r + 1):
            best = None
            for j in range(j0 - rr, j0 + rr + 1):
                for i in range(i0 - rr, i0 + rr + 1):
                    k = first.get((j, i))
                    if k is None or (j, i) in taken or tags(k)[5] not in allowed: continue
                    d = (j - j0) ** 2 + (i - i0) ** 2
                    if best is None or d < best[0]: best = (d, j, i)
            if best: return best[1], best[2]
        return None
    def tris(j, i):
        k = first[(j, i)]
        return [t for t in (k, k + 1) if t < len(tsq) and tuple(tsq[t]) == (j, i)]
    def zc(j, i):
        return float(np.mean([vz[int(v)] for v in tags(first[(j, i)])[:3]]))
    n_tag = [0]
    def tag_of(node):
        n_tag[0] += 1; return f"sw{n_tag[0]}"
    cover_def = q.get("sewer_cover", "solid"); cover_a = float(q.get("sewer_cover_area", 0.3))
    grate_a = float(q.get("sewer_grate_area", 0.1)); gully = float(q.get("sewer_gully_lps", 20)) / 1000
    chamber_def = float(q.get("sewer_chamber", 1.13))
    gully_a = gully / (0.65 * math.sqrt(2 * 9.81 * 0.3)) if gully > 0 else 0     # orifice area passing the capacity at 0.3 m head
    cover_d = math.sqrt(4 * cover_a / math.pi)
    out = dict(junctions=[], outfalls=[], storage=[], conduits=[], orifices=[], weirs=[], outlets=[], pumps=[], curves=[],
               xsections=[], coords=[], nodemap=[])
    cname = lambda c: "SC_" + re.sub(r"[^A-Za-z0-9_.\-]", "_", str(c))[:28]
    need_curves = set()
    used, sid = set(), {}
    for k in net["nodes"]: sid[k] = swmm_id(k, used)
    count = dict(solid=0, grated=0, sealed=0, unlinked=0)
    side = {}
    for k, n in net["nodes"].items():
        node = sid[k]
        out["coords"].append(f"{node} {n['x']:.2f} {n['y']:.2f}")
        if n["kind"] == "storage":                                # as in the source model, linked only through pipes
            t = list(n["storage"])
            if t[3].upper() == "TABULAR": need_curves.add(t[4]); t[4] = cname(t[4])
            out["storage"].append(" ".join([node] + t))
            side[node] = dict(kind="storage", z=n["invert"] + (n.get("depth") or 0)); continue
        if n["kind"] == "outfall":
            gate = "YES" if n.get("gated") else "NO"
            out["outfalls"].append(f"{node} {n['invert']:.3f} FIXED {n['stage']:.3f} {gate}" if n.get("stage") is not None
                                   else f"{node} {n['invert']:.3f} FREE {gate}")
            c = None if n.get("outside") else free_cell(n["x"], n["y"], 4, OPEN + ("chan",))
            if c:
                tg = tag_of(node)
                for t in tris(*c): tags(t)[5] = tg
                taken.add(c); out["nodemap"].append(f"{tg} {node} 0.65 4.000")
            continue
        kind = n.get("cover_type") or cover_def
        c = None if kind == "sealed" else free_cell(n["x"], n["y"], 2, OPEN)
        if c is None and kind != "sealed":
            count["unlinked"] += 1; kind = "sealed"             # nowhere to meet the surface: behaves as sealed
        if kind == "sealed":
            top = n.get("ground") if n.get("ground") is not None else (n.get("cover") if n.get("cover") is not None else n["invert"] + DEFAULT_DEPTH)
            out["junctions"].append(f"{node} {n['invert']:.3f} {max(0.3, top - n['invert']):.3f} 0 {SUR_H * 10:.1f} 0")
            count["sealed"] += 1; side[node] = dict(kind="sealed", z=top); continue
        j, i = c; z = zc(j, i); taken.add(c); ts = tris(j, i)
        area = n.get("chamber_area") or chamber_def
        depth = max(0.3, z - n["invert"])
        # the engine measures exchange from a node's top: a grated manhole's top is the ground; a solid one may rise
        # above it to push water out through its cover orifice
        out["storage"].append(f"{node} {n['invert']:.3f} {depth + (0 if kind == 'grated' else SUR_H):.3f} 0 FUNCTIONAL 0 0 {area:.3f} 0 0")
        if kind == "grated":
            tg = tag_of(node)
            for t in ts: tags(t)[5] = tg
            out["nodemap"].append(f"{tg} {node} 0.65 {grate_a:.3f}")
            count["grated"] += 1; side[node] = dict(kind="grated", z=z); continue
        # solid: gully in on one triangle, cover out on the other (or a neighbouring cell)
        g, u = f"G{node[1:]}"[:31], f"U{node[1:]}"[:31]
        ta, tb = ts[0], (ts[1] if len(ts) > 1 else None)
        if tb is None:
            c2 = free_cell(n["x"], n["y"], 1, OPEN)
            if c2: taken.add(c2); tb = tris(*c2)[0]
        if gully > 0:
            tg = tag_of(g); tags(ta)[5] = tg
            out["junctions"].append(f"{g} {z - 0.3:.3f} 0.3 0 {SUR_H:.1f} 0")      # top at ground; may pressurise under ponded water
            out["nodemap"].append(f"{tg} {g} 0.65 {max(0.01, 1.5 * gully_a):.4f}")   # ~ the inlet, as the engine advises
            out["orifices"].append(f"OG{node[1:]} {g} {node} SIDE {z - 0.3:.3f} 0.65 YES"[:200])
            out["xsections"].append(f"OG{node[1:]} RECT_CLOSED 0.2 {gully_a / 0.2:.4f} 0 0")   # low and wide: fits under the gully node's top
            out["coords"].append(f"{g} {n['x'] + 0.5:.2f} {n['y']:.2f}")
        if tb is not None:
            tg = tag_of(u); tags(tb)[5] = tg
            out["outfalls"].append(f"{u} {z:.3f} FREE")              # releases to the surface, like the roof downpipes
            out["nodemap"].append(f"{tg} {u} 0.65 {max(cover_a, 0.05):.3f}")
            out["orifices"].append(f"OU{node[1:]} {node} {u} SIDE {z:.3f} 0.65 YES")
            out["xsections"].append(f"OU{node[1:]} CIRCULAR {cover_d:.4f} 0 0 0")
            out["coords"].append(f"{u} {n['x'] - 0.5:.2f} {n['y']:.2f}")
        count["solid"] += 1; side[node] = dict(kind="solid", z=z, gully=g if gully > 0 else None, cover=u if tb is not None else None)
    used_l = set()
    for k, l in net["links"].items():
        lid = swmm_id(k, used_l); typ = l.get("type", "conduit"); us, ds = sid[l["us"]], sid[l["ds"]]
        if typ != "conduit":
            pr = list(l["params"])
            if typ in ("weir", "orifice"):
                out[typ + "s"].append(" ".join([lid, us, ds, pr[0], f"{l['crest']:.3f}"] + pr[2:]))
                out["xsections"].append(" ".join([lid] + list(l["xs"])))
            elif typ == "outlet":
                rest = pr[1:]
                if l.get("curve"): need_curves.add(l["curve"]); rest = [rest[0], cname(l["curve"])] + rest[2:]
                out["outlets"].append(" ".join([lid, us, ds, f"{l['crest']:.3f}"] + rest))
            else:
                if l.get("curve"): need_curves.add(l["curve"])
                out["pumps"].append(" ".join([lid, us, ds, cname(l["curve"]) if l.get("curve") else "*"] + pr[1:]))
            continue
        L = max(2.0, l.get("length") or l.get("geom_len") or 2.0)
        out["conduits"].append(f"{lid} {sid[l['us']]} {sid[l['ds']]} {L:.2f} {l['n']:.4f} {l['us_inv']:.3f} {l['ds_inv']:.3f} 0 0")
        out["xsections"].append(f"{lid} {l['shape']} {l['g1']:.3f} {l.get('g2') or 0:.3f} 0 0 1")
    for c in sorted(need_curves):
        for row in (net.get("curves") or {}).get(c, []):
            out["curves"].append(" ".join([cname(c)] + list(row)))
    for kk, p in tri.items(): lines[t0 + kk] = " ".join(p)
    out["stats"] = count
    if sidecar:
        Path(sidecar).write_text(json.dumps(dict(nodes=side, outfalls=[sid[k] for k, n in net["nodes"].items() if n["kind"] == "outfall"])))
    return out


def stats(rpt, sidecar=None):
    """Sewer results from the engine's report and the run's node list (sewer_nodes.json):
    v_sewer_in     water taken in from the surface (through gullies and grates, net of what they gave back)
    v_sewer_spill  water pushed back out to the surface (through lifted covers and grates)
    v_sewer_out    water carried to the network's outfalls
    n_surcharged   manholes whose water rose to ground level; n_spilling: of those, how many spilled"""
    try:
        L = Path(rpt).read_text(errors="ignore").split("\n")
        side = json.loads(Path(sidecar).read_text()) if sidecar and Path(sidecar).exists() else {}
    except Exception:
        return {}
    nodes, outs = side.get("nodes", {}), set(side.get("outfalls", []))
    gul = {v["gully"]: k for k, v in nodes.items() if v.get("gully")}
    cov = {v["cover"]: k for k, v in nodes.items() if v.get("cover")}
    sec, lat, flood, hgl, outv = None, {}, {}, {}, {}
    names = ("Node Depth Summary", "Node Inflow Summary", "Node Flooding Summary", "Outfall Loading Summary",
             "Node Surcharge Summary", "Storage Volume Summary", "Link Flow Summary")
    for l in L:
        t = l.strip()
        if t in names: sec = t; continue
        p = t.split()
        if len(p) < 5 or not p[0][:2] in ("S_", "G_", "U_"): continue
        try:
            if sec == "Node Depth Summary": hgl[p[0]] = float(p[4])
            elif sec == "Node Inflow Summary": lat[p[0]] = float(p[6]) * 1000
            elif sec == "Node Flooding Summary": flood[p[0]] = float(p[5]) * 1000
            elif sec == "Outfall Loading Summary": outv[p[0]] = float(p[4]) * 1000
        except (ValueError, IndexError):
            pass
    # for coupled nodes the report's lateral inflow is the net exchange with the surface (negative when it gave water
    # back), so overflow is already in it; per node it is the net over the run
    v_in = sum(lat.get(g, 0) for g in gul) + sum(max(lat.get(k, 0), 0) for k, v in nodes.items() if v["kind"] == "grated")
    spill = {k: outv.get(u, 0) for u, k in cov.items()}
    for k, v in nodes.items():
        if v["kind"] == "grated": spill[k] = max(-lat.get(k, 0), 0)
    sur = [k for k, v in nodes.items() if hgl.get(k, -1e9) >= v["z"] - 0.01]
    return dict(v_sewer_in=round(max(v_in, 0)), v_sewer_spill=round(sum(spill.values())),
                # an outfall on the river also passes river water that entered it from the surface: not network water
                v_sewer_out=round(sum(max(outv.get(o, 0) - lat.get(o, 0), 0) for o in outs)), n_surcharged=len(sur),
                n_spilling=sum(1 for v in spill.values() if v > 0.5))
