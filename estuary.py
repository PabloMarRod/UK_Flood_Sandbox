"""Tidal estuaries: 1D SWMM models from the tidal limit to a tide gauge near the mouth.

Each estuary is a folder in data/estuaries/<id>/ with:
  config.json     river names, tidal limit and mouth points, tide gauge, check gauges, gauged inflows,
                  bed profile (an ASSUMPTION unless calibrated), roughness
  line.json       OpenStreetMap river centreline ways
  water.json      OpenStreetMap water outline
  geometry.json   built stations (chainage, x, y, width, bed), centreline and water outline (cached)

River flow enters at the tidal limit (lagged from the gauge), gauged tributaries at their confluences,
and the recorded or synthetic tide sets the level at the mouth node. Drawn 2D areas crossing the
estuary's water outline use the modelled levels as boundaries.
"""
import json, math, time, datetime as dt
from pathlib import Path
import numpy as np

ROOT = Path(__file__).parent
ESTS = ROOT / "data" / "estuaries"
CACHE = ROOT / "data" / "estuary_cache"
TIDE_PERIOD_MIN = 745.2              # M2 semidiurnal period
FM = "https://environment.data.gov.uk/flood-monitoring"
HYD = "https://environment.data.gov.uk/hydrology/id/measures/"
_GEO = {}


# ------------------------------------------------------------------ registry
def ids():
    return sorted(p.parent.name for p in ESTS.glob("*/config.json"))


def config(eid):
    return json.loads((ESTS / eid / "config.json").read_text())


def save_config(eid, cfg):
    (ESTS / eid / "config.json").write_text(json.dumps(cfg, indent=1))
    _GEO.pop(eid, None)
    (ESTS / eid / "geometry.json").unlink(missing_ok=True)


def find_for(poly, min_area=2000):
    """The estuary whose water outline overlaps a BNG polygon most (or None)."""
    best = None
    for eid in ids():
        try:
            w = water_shape(eid).intersection(poly)
        except Exception:
            continue
        if w.area > min_area and (best is None or w.area > best[1].area):
            best = (eid, w)
    return best


# ------------------------------------------------------------------ geometry
def _lines(eid, cfg):
    """River centreline pieces (BNG): OS Open Rivers GeoJSON if present, else OpenStreetMap ways."""
    gj = ESTS / eid / "line.geojson"
    if gj.exists():
        from shapely.geometry import shape
        return [shape(f["geometry"]) for f in json.loads(gj.read_text())["features"]]
    return _osm_lines(eid, cfg)


def _path_segment(lines, start, end, max_gap=800):
    """Main channel from the tidal limit to the mouth: shortest path through the network of centreline pieces
    (side channels around islands are skipped)."""
    import networkx as nx
    from shapely.geometry import LineString, Point
    G = nx.Graph(); key = lambda c: (round(c[0], 0), round(c[1], 0))
    for k, ln in enumerate(lines):
        a, b = key(ln.coords[0]), key(ln.coords[-1])
        if not G.has_edge(a, b) or G[a][b]["w"] > ln.length:
            G.add_edge(a, b, w=ln.length, k=k)
    nodes = list(G.nodes)
    near = lambda p: min(nodes, key=lambda n: (n[0] - p[0]) ** 2 + (n[1] - p[1]) ** 2)
    s_, e_ = near(start), near(end)
    ek, sk = key(end), key(start)
    if (e_[0] - end[0]) ** 2 + (e_[1] - end[1]) ** 2 > 500 ** 2:   # point beyond the mapped river: join it in
        G.add_node(ek); nodes.append(ek); e_ = ek
    if (s_[0] - start[0]) ** 2 + (s_[1] - start[1]) ** 2 > 500 ** 2:
        G.add_node(sk); nodes.append(sk); s_ = sk
    # bridge small gaps in the mapped network (weirs, locks, unnamed links): join dead ends to the
    # nearest node of another piece, up to max_gap metres, as straight segments
    for gap in sorted({100, 300, 800, max_gap}):     # wide estuaries: the mapped river may stop short of the mouth gauge
        if gap > max_gap or nx.has_path(G, s_, e_): break
        max_gap_now = gap
        comp = {n: i for i, c in enumerate(nx.connected_components(G)) for n in c}
        arr = np.array(nodes, float)
        for n in [n for n in nodes if G.degree(n) == 1]:
            d = np.hypot(arr[:, 0] - n[0], arr[:, 1] - n[1])
            d[[comp[m] == comp[n] for m in nodes]] = np.inf
            j = int(np.argmin(d))
            if d[j] <= max_gap_now:
                G.add_edge(n, nodes[j], w=float(d[j]) * 2, k=None)
    # still apart (open water between mapped reaches): grow from the start, joining the nearest other piece
    for it in range(60):
        if nx.has_path(G, s_, e_): break
        reach = nx.node_connected_component(G, s_)
        A_ = np.array(list(reach), float); others = [n for n in G.nodes if n not in reach]
        B_ = np.array(others, float)
        from scipy.spatial import cKDTree
        d, j = cKDTree(B_).query(A_)
        i = int(np.argmin(d))
        if d[i] > max_gap: break
        G.add_edge(tuple(A_[i]), others[int(j[i])], w=float(d[i]) * 2, k=None)
    path = nx.shortest_path(G, s_, e_, weight="w")
    coords = []
    for a, b in zip(path[:-1], path[1:]):
        k = G[a][b]["k"]
        c = list(lines[k].coords) if k is not None else [a, b]
        if key(c[0]) != a: c = c[::-1]
        coords += c if not coords else c[1:]
    return LineString(coords)


def _main_path(lines, start, end, max_gap=800, via=(), straight_after=None):
    """Main channel through optional waypoints (to keep it in the estuary where the mapped river network
    detours up side channels)."""
    from shapely.geometry import LineString
    pts = [tuple(start)] + [tuple(v) for v in via] + [tuple(end)]
    coords = []
    for k, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        if straight_after is not None and k >= straight_after:
            c = [a, b]          # open estuary with no mapped channel line: straight through the waypoints
        else:
            c = list(_path_segment(lines, a, b, max_gap).coords)
        coords += c if not coords else c[1:]
    return LineString(coords)


def _osm_lines(eid, cfg):
    from shapely.geometry import LineString
    from pyproj import Transformer
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    L = json.loads((ESTS / eid / "line.json").read_text())["elements"]
    names = set(cfg["river_names"])
    return [LineString(list(zip(*tf([q["lon"] for q in e["geometry"]], [q["lat"] for q in e["geometry"]]))))
            for e in L if e.get("tags", {}).get("name") in names and e["tags"].get("waterway") in ("river", "tidal_channel")
            and len(e.get("geometry", [])) > 1]


def _water(eid):
    gj = ESTS / eid / "water.geojson"
    if gj.exists():
        from shapely.geometry import shape
        from shapely.ops import unary_union
        return unary_union([shape(f["geometry"]) for f in json.loads(gj.read_text())["features"]])
    return _osm_water(eid)


def _osm_water(eid):
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union, polygonize
    from pyproj import Transformer
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    polys = []
    for e in json.loads((ESTS / eid / "water.json").read_text())["elements"]:
        if e["type"] == "way" and e.get("geometry"):
            pts = list(zip(*tf([q["lon"] for q in e["geometry"]], [q["lat"] for q in e["geometry"]])))
            if len(pts) > 3 and pts[0] == pts[-1]: polys.append(Polygon(pts).buffer(0))
        elif e["type"] == "relation":
            segs = [LineString(list(zip(*tf([q["lon"] for q in m["geometry"]], [q["lat"] for q in m["geometry"]]))))
                    for m in e.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
            polys += list(polygonize(unary_union(segs)))
    return unary_union(polys)


def _stations_along(cl, water, cfg, sp, profile, branch):
    """Stations every `sp` m along a centreline, with widths measured across the water outline."""
    from shapely.geometry import LineString
    n = max(int(cl.length // sp), 1)
    st = []
    for k in range(n + 1):
        c = min(k * sp, cl.length)
        p_ = cl.interpolate(c); a = cl.interpolate(max(c - 20, 0)); b = cl.interpolate(min(c + 20, cl.length))
        dx, dy = b.x - a.x, b.y - a.y; ln_ = math.hypot(dx, dy) or 1
        tr = LineString([(p_.x + 3000 * dy / ln_, p_.y - 3000 * dx / ln_), (p_.x - 3000 * dy / ln_, p_.y + 3000 * dx / ln_)]).intersection(water)
        w = None
        if cfg.get("width_mode") == "sum" and branch == 0 and c >= cfg.get("sum_from_ch", 0):
            # very wide estuaries: all water across the section (sandbanks, channels); not in meandering upper reaches
            half = cfg.get("max_width", 5000) / 2
            w = sum(g.length for g in getattr(tr, "geoms", [tr]) if not g.is_empty and g.distance(p_) <= half) or None
        else:
            for g in getattr(tr, "geoms", [tr]):
                if not g.is_empty and g.distance(p_) < 3: w = g.length
        st.append(dict(b=branch, ch=round(c, 1), x=round(p_.x, 1), y=round(p_.y, 1), width=w, dx=dx / ln_, dy=dy / ln_))
    ws = np.array([s["width"] if s["width"] else np.nan for s in st], float)
    good = np.isfinite(ws)
    ws = np.interp(np.arange(len(ws)), np.nonzero(good)[0], ws[good]) if good.any() else np.full(len(ws), cfg["default_width"])
    ws = np.clip(ws, cfg["min_width"], cfg.get("max_width", 5000))
    bp = np.array(profile, float)
    for s, w in zip(st, ws):
        s["width"] = round(float(w), 1)
        s["bed"] = round(float(np.interp(s["ch"], bp[:, 0], bp[:, 1])), 2)
    return st


def geometry(eid):
    """Stations from the tidal limit(s) to the mouth gauge, with widths and bed levels (cached).
    Branch 0 is the main channel (start to end); optional branches (config "branches") run from their own tidal
    limit to the point where they meet the main channel, and join it there."""
    if eid in _GEO:
        return _GEO[eid]
    p = ESTS / eid / "geometry.json"
    if p.exists():
        geo = json.loads(p.read_text())
        for s in geo["stations"]: s.setdefault("b", 0)
        geo.setdefault("branches", [dict(name=geo["config"]["name"], first=0, last=len(geo["stations"]) - 1, join=None)])
        _GEO[eid] = geo; return geo
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union, substring
    cfg = config(eid); sp = cfg.get("spacing", 250.0)
    lines = _lines(eid, cfg)
    mg = cfg.get("max_gap", 800)
    m = _main_path(lines, cfg["start"], cfg["end"], mg, cfg.get("via", []), cfg.get("straight_after"))
    s0, s1 = m.project(Point(*cfg["start"])), m.project(Point(*cfg["end"]))
    cl = substring(m, min(s0, s1), max(s0, s1))
    if s0 > s1: cl = LineString(list(cl.coords)[::-1])
    water_all = _water(eid)
    reaches = [(cl, cfg["bed_profile"], cfg["name"])]
    for br in cfg.get("branches", []):
        bm = _main_path(lines, br["start"], cfg["end"], mg)
        # cut the branch where it reaches the main channel
        coords = list(bm.coords); cut = len(coords)
        for k, c in enumerate(coords):
            if Point(c).distance(cl) < max(sp, 100): cut = k + 1; break
        bl = LineString(coords[:max(cut, 2)])
        reaches.append((bl, br["bed_profile"], br["name"]))
    water = unary_union([g for g in getattr(water_all, "geoms", [water_all]) if any(g.distance(r[0]) < 5 for r in reaches)])
    st, branches = [], []
    for bi, (line, prof, name) in enumerate(reaches):
        part = _stations_along(line, water, cfg, sp, prof, bi)
        first = len(st); st += part
        join = None
        if bi > 0:   # nearest main-channel station to the branch's last point
            lx, ly = part[-1]["x"], part[-1]["y"]
            join = int(np.argmin([math.hypot(s["x"] - lx, s["y"] - ly) if s["b"] == 0 else 1e18 for s in st[:first]]))
        branches.append(dict(name=name, first=first, last=len(st) - 1, join=join))
    # keep only water within a corridor around the channels (drops tributary mouths and docks); fill holes (piers)
    corr = []
    for bi, br in enumerate(branches):
        left, right = [], []
        for s_ in st[br["first"]:br["last"] + 1]:
            h = float(s_["width"]) * cfg.get("corridor_factor", 0.5) + 25
            left.append((s_["x"] - h * s_["dy"], s_["y"] + h * s_["dx"])); right.append((s_["x"] + h * s_["dy"], s_["y"] - h * s_["dx"]))
        if len(left) > 1: corr.append(Polygon(left + right[::-1]).buffer(0))
    water = water.intersection(unary_union(corr))
    water = unary_union([Polygon(g.exterior) for g in getattr(water, "geoms", [water]) if g.geom_type == "Polygon"])
    if water.is_empty or water.area < 0.2 * sum(s_["width"] * sp for s_ in st):
        # no usable mapped water outline (e.g. Northern Ireland without OS data): use the channel line and its widths
        from shapely.geometry import LineString as _LS
        parts = []
        for br in branches:
            seg = st[br["first"]:br["last"] + 1]
            for a_, b_ in zip(seg[:-1], seg[1:]):
                parts.append(_LS([(a_["x"], a_["y"]), (b_["x"], b_["y"])]).buffer((a_["width"] + b_["width"]) / 4, cap_style=2))
        water = unary_union(parts + ([water] if not water.is_empty else []))
    for s_ in st: s_.pop("dx", None); s_.pop("dy", None)
    for g in cfg.get("check_gauges", []):
        k = int(np.argmin([math.hypot(s["x"] - g["x"], s["y"] - g["y"]) for s in st])); g["node"] = k; g["ch"] = st[k]["ch"]
    geo = dict(stations=st, branches=branches, centreline=[list(c) for c in cl.coords],
               centrelines=[[list(c) for c in r[0].coords] for r in reaches], water=water.__geo_interface__,
               length=sum(r[0].length for r in reaches), config=cfg)
    p.write_text(json.dumps(geo)); _GEO[eid] = geo
    return geo


def water_shape(eid):
    from shapely.geometry import shape
    return shape(geometry(eid)["water"])


def nodes_near(eid, xs, ys):
    """Nearest 1D node for points (any branch); returns (node indices, distances)."""
    from scipy.spatial import cKDTree
    st = geometry(eid)["stations"]
    tree = cKDTree(np.array([[s["x"], s["y"]] for s in st]))
    d, i = tree.query(np.c_[np.atleast_1d(np.asarray(xs, float)), np.atleast_1d(np.asarray(ys, float))])
    return i, d


def bed_at_points(eid, xs, ys):
    """Bed level at points: distance-weighted between the two nearest stations on the same branch."""
    from scipy.spatial import cKDTree
    st = geometry(eid)["stations"]
    xy = np.array([[s["x"], s["y"]] for s in st]); bed = np.array([s["bed"] for s in st]); br = np.array([s["b"] for s in st])
    d, i = cKDTree(xy).query(np.c_[np.atleast_1d(np.asarray(xs, float)), np.atleast_1d(np.asarray(ys, float))], k=2)
    w0 = d[:, 1] / np.maximum(d[:, 0] + d[:, 1], 1e-9)
    same = br[i[:, 0]] == br[i[:, 1]]
    return np.where(same, w0 * bed[i[:, 0]] + (1 - w0) * bed[i[:, 1]], bed[i[:, 0]])


# ------------------------------------------------------------------ data
def _get(url, params=None, tries=3):
    import requests
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=300)
            if r.ok: return r
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(f"data service did not respond: {url}")


def _measures(cfg):
    return [cfg["tide_measure"]] + [g["measure"] for g in cfg.get("check_gauges", [])] + \
           [f["rt_stage"] for f in cfg.get("flows", []) if f.get("rt_stage")] + \
           [f["rt_flow"] for f in cfg.get("flows", []) if f.get("rt_flow")]


def archive_day(eid, day, status=lambda m: None):
    """15-min readings for one UTC day for this estuary's tide, check and stage gauges (cached).
    Recent days from the live API; older ones from the EA daily archive (about nine months)."""
    cfg = config(eid); CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{eid}_{day}.json"
    if f.exists():
        return json.loads(f.read_text())
    status(f"Downloading tide and river levels for {day}")
    meas = _measures(cfg); vals = {m: {} for m in meas}
    for m in meas:
        try:
            r = _get(f"{FM}/id/measures/{m}/readings", {"startdate": str(day), "enddate": str(day), "_limit": 2000}, tries=2)
            for it in r.json().get("items", []):
                v = it.get("value")
                if isinstance(v, list): v = next((x for x in v if isinstance(x, (int, float))), None)
                if isinstance(v, (int, float)): vals[m][it["dateTime"][:16]] = v
        except RuntimeError:
            pass
    if not vals[cfg["tide_measure"]]:
        import requests
        with requests.get(f"{FM}/archive/readings-{day}.csv", stream=True, timeout=600) as rr:
            if rr.status_code != 200:
                raise ValueError(f"No {cfg['tide_gauge']} tide record is available for {day}. "
                                 "Recorded tides cover roughly the last nine months; use a synthetic tide instead.")
            for line in rr.iter_lines():
                if isinstance(line, bytes): line = line.decode("utf-8", "ignore")
                if not line or "/measures/" not in line: continue
                d_, url, v = line.split(",")[:3]
                mm = url.rsplit("/", 1)[-1]
                if mm in vals:
                    try: vals[mm][d_[:16]] = float(v)
                    except ValueError: pass
    if not vals[cfg["tide_measure"]]:
        raise ValueError(f"No {cfg['tide_gauge']} tide record is available for {day}.")
    if day < dt.datetime.utcnow().date():
        f.write_text(json.dumps(vals))
    return vals


def recorded_levels(eid, measure, t0, t1, status=lambda m: None):
    out, day = {}, t0.date()
    while day <= t1.date():
        out.update(archive_day(eid, day, status).get(measure, {})); day += dt.timedelta(days=1)
    return out


def rating(sid, name, status=lambda m: None):
    """Empirical stage-to-flow relation from a year of the EA's own paired 15-min level and flow records."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"rating_{sid}.json"
    if f.exists() and time.time() - f.stat().st_mtime < 30 * 86400:
        return json.loads(f.read_text())
    status(f"Building a stage-flow relation for {name}")
    last = _get(HYD + sid + "-flow-i-900-m3s-qualified/readings.json", {"_sort": "-date", "_limit": 1}).json()["items"][0]["dateTime"][:10]
    end = dt.date.fromisoformat(last); start = end - dt.timedelta(days=365)
    rd = lambda m: {i["dateTime"][:16]: i.get("value") for i in _get(HYD + sid + m + "/readings.json",
                    {"mineq-date": str(start), "maxeq-date": str(end), "_limit": 40000}).json()["items"]}
    lv, fl = rd("-level-i-900-m-qualified"), rd("-flow-i-900-m3s-qualified")
    pairs = np.array([(lv[k], fl[k]) for k in lv if k in fl and lv[k] is not None and fl[k] is not None], float)
    if len(pairs) < 500:
        raise ValueError(f"not enough paired records to relate stage and flow at {name}")
    edges = np.arange(pairs[:, 0].min(), pairs[:, 0].max() + 0.05, 0.05)
    idx = np.digitize(pairs[:, 0], edges)
    h, q = [], []
    for k in np.unique(idx):
        sel = idx == k
        if sel.sum() >= 3: h.append(float(np.median(pairs[sel, 0]))); q.append(float(np.median(pairs[sel, 1])))
    res = dict(stage=h, flow=np.maximum.accumulate(q).tolist(), period=[str(start), str(end)], n=int(len(pairs)))
    f.write_text(json.dumps(res))
    return res


def _settings():
    p = ROOT / "settings.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


NRW_API = "https://api.naturalresources.wales/rivers-and-seas/v1/api/StationData/historical"
NRFA_API = "https://nrfaapps.ceh.ac.uk/nrfa/ws/time-series"


def nrw_levels(location, parameter, t0, t1):
    """15-minute river levels from Natural Resources Wales (key in settings.json: nrw_api_key)."""
    import requests
    key = _settings().get("nrw_api_key")
    if not key:
        return {}
    import re as _re
    out, day = {}, t0.date()
    while day <= t1.date():                       # up to a year per request; the service limits request rates
        end = min(day + dt.timedelta(days=366), t1.date())
        for attempt in range(6):
            try:
                r = requests.get(NRW_API, params={"location": location, "parameter": parameter, "from": str(day), "to": str(end)},
                                 headers={"Ocp-Apim-Subscription-Key": key}, timeout=300)
            except Exception:
                time.sleep(5); continue
            if r.status_code == 429:              # "Rate limit is exceeded. Try again in N seconds."
                m = _re.search(r"(\d+) seconds", r.text); time.sleep(int(m.group(1)) + 2 if m else 30); continue
            if r.ok:
                for i in r.json().get("parameterReadings") or []:
                    if isinstance(i.get("value"), (int, float)): out[str(i["time"])[:16]] = float(i["value"])
            break
        day = end + dt.timedelta(days=1)
    return out


def nrw_rating(fl, status=lambda m: None):
    """Level-to-flow relation for an NRW gauge: a year of NRW daily mean levels paired with the National River
    Flow Archive's daily mean flows at the same site (NRFA data lag by about a year). Daily means smooth the
    relation, so the highest flows are only approximate."""
    import requests
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"rating_nrw_{fl['nrw_location']}.json"
    if f.exists() and time.time() - f.stat().st_mtime < 30 * 86400:
        return json.loads(f.read_text())
    status(f"Building a level-flow relation for {fl['name']} (NRW levels, NRFA flows)")
    d = requests.get(NRFA_API, params={"format": "json-object", "data-type": "gdf", "station": fl["nrfa_station"]}, timeout=180).json()
    s = d.get("data-stream", []); flows = dict(zip(s[0::2], s[1::2]))
    if not flows:
        raise ValueError(f"the National River Flow Archive has no daily flows for station {fl['nrfa_station']}")
    last = dt.date.fromisoformat(max(flows)); first = last - dt.timedelta(days=365)
    lv = nrw_levels(fl["nrw_location"], fl["nrw_parameter"], dt.datetime.combine(first, dt.time()), dt.datetime.combine(last, dt.time()))
    daily = {}
    for k, v in lv.items(): daily.setdefault(k[:10], []).append(v)
    pairs = np.array([(np.mean(v), flows[dd]) for dd, v in daily.items() if dd in flows and isinstance(flows[dd], (int, float)) and len(v) > 48], float)
    if len(pairs) < 60:
        # NRW keeps about a year of levels and the NRFA flows lag by about a year, so the records may not overlap.
        # Fall back to matching their distributions: the level exceeded p% of days maps to the flow exceeded p%
        # of days (NRFA, last five years). Approximate; assumes the gauge's behaviour has not changed.
        lv = nrw_levels(fl["nrw_location"], fl["nrw_parameter"], dt.datetime.utcnow() - dt.timedelta(days=365), dt.datetime.utcnow())
        daily = {}
        for k, v in lv.items(): daily.setdefault(k[:10], []).append(v)
        dl = np.array([np.mean(v) for v in daily.values() if len(v) > 48])
        fq = np.array([v for k, v in flows.items() if k >= str(last - dt.timedelta(days=5 * 365)) and isinstance(v, (int, float))])
        if len(dl) < 120 or len(fq) < 365:
            raise ValueError(f"not enough level or flow records to relate them at {fl['name']}")
        p = np.linspace(0.5, 99.5, 100)
        h, q = np.percentile(dl, p), np.percentile(fq, p)
        h, idx_ = np.unique(np.round(h, 3), return_index=True); q = np.maximum.accumulate(q[idx_])
        res = dict(stage=h.tolist(), flow=q.tolist(), method="distribution matching (records do not overlap)",
                   period=[f"NRW levels {min(daily)} to {max(daily)}", f"NRFA flows {str(last - dt.timedelta(days=5 * 365))} to {last}"], n=int(len(dl)))
        f.write_text(json.dumps(res))
        return res
    edges = np.arange(pairs[:, 0].min(), pairs[:, 0].max() + 0.05, 0.05); idx = np.digitize(pairs[:, 0], edges)
    h, q = [], []
    for k in np.unique(idx):
        sel = idx == k
        h.append(float(np.median(pairs[sel, 0]))); q.append(float(np.median(pairs[sel, 1])))
    res = dict(stage=h, flow=np.maximum.accumulate(q).tolist(), period=[str(first), str(last)], n=int(len(pairs)),
               method="paired daily means")
    f.write_text(json.dumps(res))
    return res


def nrw_flow(fl, t0, t1, status=lambda m: None):
    """15-minute flows for a Welsh gauge: NRW levels converted with nrw_rating(). Returns (readings, source)."""
    if not _settings().get("nrw_api_key"):
        return {}, "no data (no NRW key in settings.json)"
    if not (fl.get("nrw_location") and fl.get("nrw_parameter") and fl.get("nrfa_station")):
        return {}, "no data (gauge not configured)"
    status(f"Downloading river levels: {fl['name']}")
    lv = nrw_levels(fl["nrw_location"], fl["nrw_parameter"], t0, t1)
    if not lv:
        return {}, "no data (NRW returned no levels)"
    try:
        rt = nrw_rating(fl, status)
    except Exception as ex:
        return {}, f"no data ({ex})"
    how = "matched level and flow distributions, approximate" if rt.get("method", "").startswith("distribution") else "paired daily records"
    return {k: float(np.interp(v, rt["stage"], rt["flow"])) for k, v in lv.items()}, f"flow from NRW levels via a level-flow relation with NRFA ({how})"


def recorded_flow(eid, fl, t0, t1, status=lambda m: None):
    """15-min flows: EA Hydrology records where published (they lag by several weeks); after that,
    real-time stage converted with the gauge's own stage-flow relation. Returns (readings, source)."""
    if fl.get("sepa_ts"):                       # Scotland: SEPA time series service (open)
        try:
            import scotland
            status(f"Downloading river flows: {fl['name']} (SEPA)")
            rec = scotland.sepa_flow(fl["sepa_ts"], t0, t1)
        except Exception:
            rec = {}
        return rec, ("gauged flow (SEPA)" if rec else "no data")
    if fl.get("nrw_location") or fl.get("nrw_station") or fl.get("hydrology") in (None, "none"):
        return nrw_flow(fl, t0, t1, status)
    status(f"Downloading river flows: {fl['name']}")
    r = _get(HYD + fl["hydrology"] + "-flow-i-900-m3s-qualified/readings.json",
             {"mineq-date": str(t0.date()), "maxeq-date": str(t1.date()), "_limit": 20000})
    rec = {it["dateTime"][:16]: it.get("value") for it in r.json().get("items", []) if it.get("value") is not None}
    if rec and max(rec) >= t1.strftime("%Y-%m-%dT%H:%M"):
        return rec, "gauged flow"
    if fl.get("rt_flow"):          # the gauge also publishes real-time flow
        live = recorded_levels(eid, fl["rt_flow"], t0, t1, status)
        fac = fl.get("rt_flow_factor", 1.0)        # e.g. 1/86.4 for gauges that publish Ml/d
        for k, v in live.items():
            if k not in rec and v is not None: rec[k] = float(v) * fac
        return rec, ("gauged flow (real-time)" if live else ("gauged flow" if rec else "no data"))
    if not fl.get("rt_stage"):
        return rec, ("gauged flow" if rec else "no data")
    try:
        rt = rating(fl["hydrology"], fl["name"], status)
    except (ValueError, RuntimeError, KeyError, IndexError):
        return rec, ("gauged flow (partial period)" if rec else "no data (no stage-flow relation available)")
    stage = recorded_levels(eid, fl["rt_stage"], t0, t1, status)
    for k, v in stage.items():
        if k not in rec and v is not None:
            rec[k] = float(np.interp(v, rt["stage"], rt["flow"]))
    return rec, ("flow from stage via the gauge's rating" if stage else "no data")


def tide_recent_stats(eid):
    """Typical high and low water at the mouth gauge over the last 30 days (for defaults and hints)."""
    cfg = config(eid); CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{eid}_recent_stats.json"
    if f.exists() and time.time() - f.stat().st_mtime < 86400:
        return json.loads(f.read_text())
    try:
        r = _get(f"{FM}/id/measures/{cfg['tide_measure']}/readings", {"_sorted": "", "_limit": 3000}, tries=2)
        v = np.array([x for x in (it.get("value") for it in r.json()["items"]) if isinstance(x, (int, float))], float)
        res = dict(hw_mean=round(float(np.percentile(v, 90)), 2), hw_max=round(float(v.max()), 2),
                   lw_mean=round(float(np.percentile(v, 10)), 2), lw_min=round(float(v.min()), 2), n=len(v))
    except Exception:
        res = dict(cfg.get("fallback_stats", dict(hw_mean=2.3, hw_max=2.8, lw_mean=-1.4, lw_min=-2.0)), n=0, offline=True)
    res.update(gauge=cfg["tide_gauge"], limit=cfg["limit_name"], river=cfg["river_label"], name=cfg["name"],
               proxy_km=cfg.get("tide_proxy_km"))
    f.write_text(json.dumps(res))
    return res


# ------------------------------------------------------------------ scenario series (minutes from scenario start)
def _series_from_record(rec, t_start, minutes):
    keys = sorted(rec); ts = np.array([(dt.datetime.fromisoformat(k) - t_start).total_seconds() / 60 for k in keys])
    vs = np.array([rec[k] for k in keys], float); ok = np.isfinite(vs)
    if ok.sum() < 2:
        raise ValueError("Too few records in the chosen period.")
    return np.interp(minutes, ts[ok], vs[ok])


def _p(q, key, default=None):
    """Scenario value with backward compatibility (river_* was tyne_* in earlier versions)."""
    if key in q: return q[key]
    old = key.replace("river_", "tyne_")
    return q.get(old, default)


def boundary_series(eid, q, spinup_min, status=lambda m: None):
    cfg = config(eid)
    end = q["sim"] * 60
    m = np.arange(-spinup_min, end + 5, 5.0)
    sources = {}
    if q["tide_mode"] == "recorded":
        t_start = dt.datetime.fromisoformat(q["tide_start"])
        t0, t1 = t_start - dt.timedelta(minutes=spinup_min + 30), t_start + dt.timedelta(minutes=end + 30)
        tide = _series_from_record(recorded_levels(eid, cfg["tide_measure"], t0, t1, status), t_start, m)
        flows = {}
        for fl in cfg.get("flows", []):
            lag = fl.get("lag_min", 0)
            rec, src = recorded_flow(eid, fl, t0 - dt.timedelta(minutes=lag), t1, status)
            if len(rec) > 1:
                flows[fl["key"]] = _series_from_record(rec, t_start, m - lag); sources[fl["name"]] = src
            else:                       # no gauge data (e.g. Welsh gauges need an NRW key): fall back to the default
                dflt = cfg.get("defaults", {}).get("river_base", 0) if fl is cfg["flows"][0] else 0
                flows[fl["key"]] = np.full_like(m, float(dflt))
                sources[fl["name"]] = f"no data; {dflt:g} m³/s assumed" if dflt else "no data"
    else:
        amp, mid = (q["tide_hw"] - q["tide_lw"]) / 2, (q["tide_hw"] + q["tide_lw"]) / 2
        tide = mid + amp * np.cos(2 * np.pi * (m - q["tide_hw_time"]) / TIDE_PERIOD_MIN)
        base, peak = _p(q, "river_base", 40), _p(q, "river_peak", 400)
        if _p(q, "river_mode", "base") == "wave":
            ts_, tr, tf_ = q["wave_start"], q["wave_rise"], q["wave_fall"]
            r = np.clip((m - ts_) / tr, 0, 1); f = np.clip((m - ts_ - tr) / tf_, 0, 1)
            shape = np.where(m < ts_ + tr, 0.5 * (1 - np.cos(np.pi * r)), 0.5 * (1 + np.cos(np.pi * f)))
            qt = base + (peak - base) * shape
        else:
            qt = np.full_like(m, base)
        flows = {cfg["flows"][0]["key"]: qt}
    if q.get("surge_peak", 0) > 0:
        r = np.clip(1 - np.abs(m - q["surge_time"]) / (q["surge_dur"] / 2), 0, 1)
        tide = tide + q["surge_peak"] * 0.5 * (1 - np.cos(np.pi * r))
    tide = tide + q.get("slr", 0)
    return m, tide, flows, sources


# ------------------------------------------------------------------ 1D model
def build_inp(eid, q, path, status=lambda m: None, spinup_min=1440, override=None):
    """override: optional dict (bed_profile, manning) used during calibration."""
    geo = geometry(eid); st = geo["stations"]; cfg = dict(geo["config"]); cfg.update(override or {})
    beds = np.array([s["bed"] for s in st])
    if override and "bed_profile" in override:      # calibration adjusts the main channel
        main = np.array([s.get("b", 0) == 0 for s in st])
        beds[main] = np.interp(np.array([s["ch"] for s in st])[main], *np.array(override["bed_profile"], float).T)
    m, tide, flows, sources = boundary_series(eid, q, spinup_min, status)
    hhmm = lambda x: f"{int(x) // 60 % 24:02d}:{int(x) % 60:02d}"
    day = lambda x: f"01/{1 + int(x) // 1440:02d}/2030"
    tot = int(spinup_min + q["sim"] * 60); sp = cfg.get("spacing", 250.0)
    o = [f";; {cfg['name']} 1D model (UK Flood Sandbox). Bed levels: see data/estuaries/{eid}/config.json",
         "[TITLE]", cfg["name"], "", "[OPTIONS]", "FLOW_UNITS CMS", "FLOW_ROUTING DYNWAVE", "LINK_OFFSETS ELEVATION",
         "START_DATE 01/01/2030", "START_TIME 00:00:00", "REPORT_START_DATE 01/01/2030", "REPORT_START_TIME 00:00:00",
         f"END_DATE {day(tot)}", f"END_TIME {hhmm(tot)}:00", "REPORT_STEP 00:05:00", "WET_STEP 00:05:00",
         "DRY_STEP 00:05:00", "ROUTING_STEP 00:00:05", "VARIABLE_STEP 0.75", "MIN_SURFAREA 100", "NORMAL_FLOW_LIMITED BOTH",
         "INERTIAL_DAMPING PARTIAL", "MIN_SLOPE 0", "THREADS 1", "", "[JUNCTIONS]"]
    brs = geo.get("branches") or [dict(first=0, last=len(st) - 1, join=None)]
    mouth = brs[0]["last"]
    links = []          # (upstream node, downstream node, length)
    for br in brs:
        for k in range(br["first"], br["last"]):
            links.append((k, k + 1, st[k + 1]["ch"] - st[k]["ch"]))
        if br["join"] is not None:
            j = br["join"]; links.append((br["last"], j, max(math.hypot(st[j]["x"] - st[br["last"]]["x"], st[j]["y"] - st[br["last"]]["y"]), 50.0)))
    for k, s in enumerate(st):
        if k != mouth:
            o.append(f"N{k} {beds[k]:.2f} {cfg['bank_height']:.1f} 0 0 {s['width'] * sp / 2:.0f}")
    o += ["", "[OUTFALLS]", f"N{mouth} {beds[mouth]:.2f} TIMESERIES TIDE", "", "[CONDUITS]"]
    for c, (a, b, L) in enumerate(links):
        o.append(f"C{c} N{a} N{b} {L:.1f} {cfg['manning']} {beds[a]:.2f} {beds[b]:.2f} 0")
    o += ["", "[XSECTIONS]"] + [f"C{c} RECT_OPEN {cfg['bank_height']:.1f} {(st[a]['width'] + st[b]['width']) / 2:.1f} 0 0 1"
                                 for c, (a, b, L) in enumerate(links)]
    o += ["", "[INFLOWS]"]; ts = ["", "[TIMESERIES]"]
    for fl in cfg.get("flows", []):
        if fl["key"] not in flows: continue
        at = fl.get("at")
        if at == "start": node = 0
        elif isinstance(at, str) and at.startswith("branch:"):
            node = next(br["first"] for br in brs if br.get("name") == at[7:])
        else: node = int(np.argmin([math.hypot(s["x"] - at[0], s["y"] - at[1]) for s in st]))
        o.append(f'N{node} FLOW Q_{fl["key"]} "" 1.0 1.0')
        ts += [f"Q_{fl['key']} {day(t + spinup_min)} {hhmm(t + spinup_min)} {max(v, 0):.3f}" for t, v in zip(m, flows[fl["key"]])]
    ts += [f"TIDE {day(t + spinup_min)} {hhmm(t + spinup_min)} {v:.3f}" for t, v in zip(m, tide)]
    o += ts + ["", "[COORDINATES]"] + [f"N{k} {s['x']:.1f} {s['y']:.1f}" for k, s in enumerate(st)]
    Path(path).write_text("\n".join(o) + "\n")
    return m, tide, flows, sources


def run(eid, q, workdir, status=lambda m: None, spinup_min=1440, override=None, name="estuary"):
    """Run the estuary model; returns minutes (from scenario start), head per node [time, node], tide, flows."""
    import openswmm.engine as eng
    wd = Path(workdir); wd.mkdir(parents=True, exist_ok=True)
    m, tide, flows, sources = build_inp(eid, q, wd / f"{name}.inp", status, spinup_min, override)
    status(f"Running the {config(eid)['name']} model")
    eng.run(str(wd / f"{name}.inp"), str(wd / f"{name}.rpt"), str(wd / f"{name}.out"))
    st = geometry(eid)["stations"]
    with eng.OutputReader(str(wd / f"{name}.out")) as r:
        heads = np.stack([np.asarray(r.node_series(f"N{k}", eng.OutNodeVar.HEAD)) for k in range(len(st))], axis=1)
    mins = np.arange(1, heads.shape[0] + 1) * 5.0 - spinup_min
    keep = mins >= -0.1
    err = None
    for line in (wd / f"{name}.rpt").read_text(errors="ignore").splitlines():
        if "Continuity Error" in line:
            try: err = float(line.split()[-1])
            except ValueError: pass
    return dict(minutes=mins[keep], heads=heads[keep], tide=np.interp(mins[keep], m, tide),
                flows={k: np.interp(mins[keep], m, v) for k, v in flows.items()}, routing_error=err, sources=sources,
                estuary=eid)
