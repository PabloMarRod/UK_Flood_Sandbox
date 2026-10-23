import math
"""UK Flood Sandbox scenario core (area-aware).

storm:    user parameters -> hyetograph, inflow series, .inp file for an area
results:  HDF5 -> PNG frames (Web Mercator), statistics, series
"""
import base64, io, json, math
from pathlib import Path
import numpy as np
import areas
import sewer

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STEP_MIN = 5                      # rainfall step (min)
GAUGE_START = "2012-06-28T15:00"  # rain onset on Thunder Thursday (UTC)
MAX_RUN_S = 600                   # refuse runs estimated above 10 minutes
CALIB = ROOT / "runs" / "calibration.json"
DEFAULT_K = 4.5e-4                # seconds per cell per simulated hour (single core, measured in development)

LIMITS = dict(depth=(0, 200), duration=(5, 360), sim=(0.5, 8.0), base_q=(0, 30), factor=(0.1, 3.0), uplift=(0, 100),
              wave_base=(0, 30), wave_peak=(0, 80), wave_start=(0, 480), wave_rise=(5, 480), wave_fall=(5, 960),
              burst_q=(0.01, 5), burst_start=(0, 480), burst_dur=(1, 480), blockage=(0, 95))
RIVER_LIMIT = 3000.0              # m3/s, per detected inflow in drawn areas (above the largest UK river floods)
TIDE_LIMITS = dict(tide_hw=(-3, 12), tide_lw=(-8, 5), tide_hw_time=(0, 1440), surge_peak=(0, 3), surge_time=(0, 1440),
                   surge_dur=(30, 2880), slr=(0, 2), river_base=(0, 5000), river_peak=(0, 8000))
PROFILES = ("uniform", "front", "centre", "late", "obs2012")
INFLOWS = ("none", "base", "gauge2012", "wave")
SOILS = {  # Horton (max, min mm/h, decay 1/h) for green and garden/mixed cells; assumed values
    "dry": ((40, 5, 4), (15, 2, 4)), "normal": ((20, 3, 4), (8, 1, 4)), "wet": ((5, 2, 2), (2, 0.5, 2)), "sealed": None}
# Ouseburn culvert (Tyne and Wear HER 5046): elliptical, 30 ft wide x 20 ft high
CULVERT_B, CULVERT_D = 9.14, 6.10


# ------------------------------------------------------------------ run-time budget
def k_per_cell():
    try:
        return float(json.loads(CALIB.read_text())["k"])
    except Exception:
        return DEFAULT_K


def effective_cells(A):
    """Deep, permanently wet river cells force short time steps; weight them in the budget."""
    meta = areas.mesh_meta(A)
    frac = meta.get("estuary", {}).get("n_cells", 0) / max(A["n_cells"], 1)
    return A["n_cells"] * (1 + 8 * frac)


def calibrate(cells, sim_h, runtime_s):
    """Update the seconds-per-cell-hour figure from a finished run (moving average)."""
    k_new = runtime_s / max(cells * sim_h, 1)
    k = 0.6 * k_per_cell() + 0.4 * k_new if CALIB.exists() else k_new
    cal = calibration(); cal.update(k=k, last=k_new)               # keep the speed-test settings
    CALIB.parent.mkdir(exist_ok=True); CALIB.write_text(json.dumps(cal, indent=1))


def calibration():
    try:
        return json.loads(CALIB.read_text())
    except Exception:
        return {}


def record_memory(cells, peak_mb):
    """Refine memory per cell from a finished run (moving average)."""
    if not cells or not peak_mb: return
    kb = max(1.0, (peak_mb - 80.0) * 1024 / cells)
    cal = calibration(); old = cal.get("mem_kb_per_cell")
    cal["mem_kb_per_cell"] = round(0.7 * old + 0.3 * kb * 1.3, 2) if old else round(kb * 1.3, 2)
    CALIB.parent.mkdir(exist_ok=True); CALIB.write_text(json.dumps(cal, indent=1))


def estimate_s(A, sim_h):
    return 5 + k_per_cell() * effective_cells(A) * sim_h


# ------------------------------------------------------------------ storm
def _cdf(profile, x):
    """Cumulative rainfall fraction at normalised time x (0..1)."""
    x = np.clip(x, 0, 1)
    if profile == "uniform":
        return x
    if profile == "obs2012":
        obs = json.load(open(DATA / "obs_2012-06-28.json"))["rain"]
        ks = sorted(k for k in obs if "2012-06-28T15:00" <= k < "2012-06-28T17:30")
        vals = np.array([obs[k] for k in ks]); cum = np.concatenate([[0], np.cumsum(vals)]) / vals.sum()
        return np.interp(x, np.linspace(0, 1, len(cum)), cum)
    m = dict(front=0.25, centre=0.5, late=0.75)[profile]
    return np.where(x < m, x ** 2 / m, 1 - (1 - x) ** 2 / (1 - m))


MEASURE_TYPES = {"wall": "Flood wall", "embankment": "Embankment", "barrier": "Demountable barrier", "basin": "Storage basin",
                 "permeable": "Permeable surface", "raingarden": "Rain garden", "swale": "Swale", "trees": "Tree planting",
                 "kerb": "Raised kerb", "deepen": "Channel deepening", "greenroof": "Green roofs",
                 "tank": "Storage tank", "pump": "Pump", "gate": "Flood gate"}
AREA_TYPES = {"basin", "permeable", "raingarden", "trees", "deepen", "greenroof"}
POINT_TYPES = {"tank", "pump"}                # a pump has two points: intake, then discharge (or one: out of the area)
# land-cover tags given to cells covered by surface measures: Horton (max, min mm/h, decay 1/h) at "normal"
# ground, Manning n. Indicative values for well-maintained features, scaled with the ground condition.
SUDS = {"perm": ((150, 40, 3), 0.015), "raing": ((200, 50, 3), 0.10), "swale": ((100, 20, 3), 0.06), "wood": ((120, 25, 3), 0.12)}
SOIL_SCALE = {"dry": 1.3, "normal": 1.0, "wet": 0.5}


def check_measures(A, ms):
    """User-placed defences: [{type, pts [[x, y] BNG], height | depth, width}]. Returns a clean list."""
    from shapely.geometry import Polygon, LineString
    out, poly = [], Polygon(A["poly_bng"]).buffer(A["H"])
    for m in (ms or [])[:60]:
        t = m.get("type")
        if t not in MEASURE_TYPES: continue
        try:
            pts = [[float(x), float(y)] for x, y in (m.get("pts") or [])][:500]
        except (TypeError, ValueError):
            continue
        if len(pts) < (3 if t in AREA_TYPES else (1 if t in POINT_TYPES else 2)): continue
        from shapely.geometry import Point as _Pt
        g = Polygon(pts) if t in AREA_TYPES else (_Pt(pts[0]) if t in POINT_TYPES else LineString(pts))
        if not g.is_valid or not g.intersects(poly): continue
        c = dict(type=t, name=str(m.get("name") or MEASURE_TYPES[t])[:60], pts=pts)
        try:
            if m.get("cost") not in (None, ""): c["cost"] = max(0.0, float(m["cost"]))
        except (TypeError, ValueError):
            pass
        lim = lambda k, d, lo, hi: float(min(max(float(m.get(k, d)), lo), hi))
        if t == "basin":
            c["depth"] = lim("depth", 1.5, 0.2, 5.0); c["bund"] = lim("bund", 0.0, 0.0, 3.0)
        elif t == "raingarden": c["depth"] = lim("depth", 0.2, 0.05, 0.5)
        elif t == "swale": c["depth"] = lim("depth", 0.3, 0.1, 1.0); c["width"] = lim("width", 4.0, 2.0, 15.0)
        elif t == "kerb": c["height"] = lim("height", 0.2, 0.05, 0.5)
        elif t == "deepen": c["depth"] = lim("depth", 0.5, 0.1, 3.0)
        elif t == "greenroof": c["retention"] = lim("retention", 15.0, 5.0, 40.0)
        elif t in ("permeable", "trees"): pass
        elif t == "tank":
            c["volume"] = lim("volume", 500.0, 10.0, 50000.0); c["inflow"] = lim("inflow", 0.2, 0.01, 5.0); c["depth"] = lim("depth", 3.0, 1.0, 8.0)
            c["pts"] = pts[:1]
        elif t == "pump":
            c["rate"] = lim("rate", 0.2, 0.01, 10.0); c["start"] = lim("start", 0.3, 0.05, 2.0); c["pts"] = pts[:2]
        elif t == "gate":
            c["height"] = lim("height", 1.2, 0.2, 6.0)
            c["mode"] = m.get("mode") if m.get("mode") in ("time", "level", "open", "closed") else "time"
            c["close_min"] = lim("close_min", 30.0, 0.0, 2880.0); c["close_level"] = lim("close_level", 0.3, 0.05, 5.0)
        else:
            c["height"] = float(min(max(float(m.get("height", 1.0)), 0.2), 6.0))
            if t == "embankment": c["width"] = float(min(max(float(m.get("width", 10.0)), 4.0), 40.0))
        out.append(c)
    return out


def apply_measures(A, vxy, vz, measures, is_tidal_vertex=None, chan_vertex=None):
    """Raise or lower mesh vertex heights for user defences (vz edited in place and returned).
    Walls and barriers: vertices within 0.75 cell of the line raised to ground + height. Embankments: a crest of
    the given width at ground + height with 1-in-3 side slopes. Basins: ground lowered by the depth inside the
    outline, with an optional bund just outside it. River (estuary) vertices are never changed."""
    import shapely
    from shapely.geometry import Polygon, LineString
    H = A["H"]
    pts = shapely.points(vxy[:, 0], vxy[:, 1])
    ground = vz.copy()
    for m in measures:
        if m["type"] in ("permeable", "trees", "greenroof", "tank", "pump"):
            continue                                   # surface, roof or 1D changes only, no change in ground level
        if m["type"] in ("raingarden", "deepen"):
            g = Polygon(m["pts"]); inside = shapely.contains(g, pts)
            if m["type"] == "deepen" and chan_vertex is not None:
                inside &= chan_vertex                  # only the river channel itself is deepened
            vz[inside] = np.minimum(vz[inside], ground[inside] - m["depth"])
            continue
        if m["type"] == "swale":
            g = LineString(m["pts"]); d = shapely.distance(g, pts)
            near = d <= max(m["width"] / 2, H * 0.75)
            vz[near] = np.minimum(vz[near], ground[near] - m["depth"] * np.clip(1 - d[near] / (m["width"] / 2 + H), 0.3, 1))
            continue
        if m["type"] == "basin":
            g = Polygon(m["pts"]); inside = shapely.contains(g, pts)
            vz[inside] = np.minimum(vz[inside], ground[inside] - m["depth"])
            if m.get("bund", 0) > 0:
                d = shapely.distance(g.exterior, pts); ring = (~inside) & (d <= H * 1.2)
                vz[ring] = np.maximum(vz[ring], ground[ring] + m["bund"])
            continue
        g = LineString(m["pts"]); d = shapely.distance(g, pts)
        if m["type"] == "embankment":
            half, slope = m["width"] / 2, 3.0
            near = d <= half + m["height"] * slope
            vz[near] = np.maximum(vz[near], ground[near] + m["height"] - np.maximum(d[near] - half, 0) / slope)
        else:                                          # wall, barrier, raised kerb, gate (its openings are 1D links)
            near = d <= H * 0.75
            vz[near] = np.maximum(vz[near], ground[near] + m["height"])
    if is_tidal_vertex is not None:
        vz[is_tidal_vertex] = ground[is_tidal_vertex]
    return vz


def validate(A, p):
    """Normalise and check user parameters for area A. Returns (params, warnings) or raises ValueError."""
    g = lambda k, d=None: p.get(k, d)
    F = A["features"]
    q = dict(area=A["id"], depth=float(g("depth", 30)), duration=int(g("duration", 60)), profile=g("profile", "centre"),
             uplift=float(g("uplift", 0)), inflow=g("inflow", "base" if F.get("river_inflow") else "none"),
             base_q=float(g("base_q", 0.5)), factor=float(g("factor", 1.0)), wave_base=float(g("wave_base", 0.5)),
             wave_peak=float(g("wave_peak", 15)), wave_start=float(g("wave_start", 0)), wave_rise=float(g("wave_rise", 45)),
             wave_fall=float(g("wave_fall", 120)), burst=bool(g("burst", False)), burst_lat=g("burst_lat"),
             burst_lng=g("burst_lng"), burst_q=float(g("burst_q", 0.3)), burst_start=float(g("burst_start", 0)),
             burst_dur=float(g("burst_dur", 60)), blockage=float(g("blockage", 0)), soil=g("soil", "normal"),
             sim=float(g("sim", 3.0)))
    names = dict(depth="Rain", duration="Storm duration", sim="Simulation length", base_q="Steady flow", factor="Scale factor",
                 uplift="Climate uplift", wave_base="Base flow", wave_peak="Peak flow", wave_start="Wave start",
                 wave_rise="Time to peak", wave_fall="Fall time", burst_q="Burst flow", burst_start="Burst start",
                 burst_dur="Burst duration", blockage="Blockage")
    for k, (lo, hi) in LIMITS.items():
        if not lo <= q[k] <= hi:
            raise ValueError(f"{names[k]} must be between {lo:g} and {hi:g}")
    if q["profile"] not in PROFILES: raise ValueError("unknown storm profile")
    if q["inflow"] not in INFLOWS: raise ValueError("unknown inflow option")
    if q["soil"] not in SOILS: raise ValueError("unknown ground condition")
    if not F.get("river_inflow"): q["inflow"] = "none"
    q["rivers"] = []
    known = {f["id"]: f for f in areas.mesh_meta(A).get("inflows", [])}
    for r in (p.get("rivers") or []):
        rid, mode = r.get("id"), r.get("mode", "none")
        if rid not in known or mode not in ("none", "base", "wave"):
            continue
        base, peak = float(r.get("base", 0)), float(r.get("peak", 0))
        nm = known[rid]["name"]
        if not (0 <= base <= RIVER_LIMIT and 0 <= peak <= RIVER_LIMIT):
            raise ValueError(f"{nm}: flows must be between 0 and {RIVER_LIMIT:g} m³/s")
        if mode == "wave" and peak < base:
            raise ValueError(f"{nm}: peak flow must be above the base flow")
        if mode != "none":
            q["rivers"].append(dict(id=rid, name=nm, mode=mode, base=base, peak=peak))
    if not F.get("culvert"): q["blockage"] = 0.0
    if q["inflow"] == "wave" and q["wave_peak"] < q["wave_base"]: raise ValueError("Peak flow must be above the base flow")
    q["duration"] = int(round(q["duration"] / STEP_MIN) * STEP_MIN)
    if q["depth"] > 0 and q["sim"] * 60 < q["duration"]:
        raise ValueError("Simulation must be at least as long as the storm")
    warn = []
    if q["burst"]:
        if q["burst_lat"] is None or q["burst_lng"] is None:
            raise ValueError("Pick the burst location on the map")
        q["burst_lat"], q["burst_lng"] = float(q["burst_lat"]), float(q["burst_lng"])
        q["burst_cell"] = burst_cell(A, q["burst_lat"], q["burst_lng"])
        if q["burst_start"] >= q["sim"] * 60: raise ValueError("The burst starts after the simulation ends")
        if q["burst_start"] + q["burst_dur"] > q["sim"] * 60: warn.append("The burst is still running when the simulation ends.")
    if q["depth"] == 0 and not q["burst"] and q["inflow"] in ("none", "base") and not any(r["mode"] == "wave" for r in q["rivers"]) \
            and not areas.mesh_meta(A).get("estuary"):
        warn.append("No rain, no burst and no flood wave: little will happen.")
    if q["depth"] > 0 and q["sim"] * 60 < q["duration"] + 60:
        warn.append("Less than an hour after the rain stops: water will still be draining at the end.")
    meta = areas.mesh_meta(A)
    if meta.get("estuary"):
        import estuary as _est
        ecfg = _est.config(meta["estuary"]["id"]); dflt = ecfg.get("defaults", {})
        for old_, new_ in (("tyne_mode", "river_mode"), ("tyne_base", "river_base"), ("tyne_peak", "river_peak")):
            if old_ in p and new_ not in p: p = dict(p, **{new_: p[old_]})
        g = lambda k, d=None: p.get(k, d)
        import datetime as dt
        q.update(tide_mode=g("tide_mode", "synthetic"), tide_start=g("tide_start"), river_mode=g("river_mode", "base"))
        for k in TIDE_LIMITS:
            q[k] = float(g(k, dict(tide_hw=2.3, tide_lw=-1.3, tide_hw_time=120, surge_peak=0, surge_time=120,
                                   surge_dur=360, slr=0, river_base=dflt.get("river_base", 40), river_peak=dflt.get("river_peak", 400))[k]))
            lo, hi = TIDE_LIMITS[k]
            if not lo <= q[k] <= hi:
                raise ValueError(f"{k.replace('_', ' ').capitalize()} must be between {lo:g} and {hi:g}")
        if q["tide_mode"] not in ("synthetic", "recorded"): raise ValueError("unknown tide option")
        if q["tide_mode"] == "synthetic" and q["tide_hw"] <= q["tide_lw"]: raise ValueError("High water must be above low water")
        if q["river_mode"] == "wave" and q["river_peak"] < q["river_base"]: raise ValueError(f"{ecfg['river_label']} peak flow must be above its base flow")
        if q["tide_mode"] == "recorded":
            try:
                t0 = dt.datetime.fromisoformat(str(q["tide_start"]).replace("Z", ""))
            except ValueError:
                raise ValueError("Choose a start date and time for the recorded tide")
            if t0 + dt.timedelta(hours=q["sim"]) > dt.datetime.utcnow():
                raise ValueError("The recorded period must be in the past")
            q["tide_start"] = t0.strftime("%Y-%m-%dT%H:%M")
        q["estuary"] = meta["estuary"]["id"]
    est = estimate_s(A, q["sim"])
    if est > MAX_RUN_S:                       # a warning, not a limit: the user decides whether to wait
        warn.append(f"Long run: estimated about {est / 60:.0f} minutes on this computer. To shorten it, simulate fewer hours, "
                    f"or draw the area again with 10 m cells (typically 4 to 8 times faster) or smaller.")
    q["est_s"] = round(est)
    q["measures"] = check_measures(A, p.get("measures")) if p.get("use_measures", True) else []
    try:
        q["damage_factor"] = min(max(float(p.get("damage_factor", 1.0)), 0.1), 10.0)
    except (TypeError, ValueError):
        q["damage_factor"] = 1.0
    q["sewer"] = bool(p.get("sewer")) and sewer.load(A) is not None    # an imported drainage network, if the user ticks it
    q["sewer_cover"] = p.get("sewer_cover") if p.get("sewer_cover") in ("solid", "grated", "sealed") else "solid"
    for k_, d_, lo, hi in (("sewer_cover_area", 0.3, 0.05, 2.0), ("sewer_grate_area", 0.1, 0.01, 2.0),
                           ("sewer_gully_lps", 20.0, 0.0, 500.0), ("sewer_chamber", 1.13, 0.2, 30.0)):
        try:
            q[k_] = min(max(float(p.get(k_, d_)), lo), hi)
        except (TypeError, ValueError):
            q[k_] = d_
    if p.get("sewer") and not q["sewer"]:
        warn.append("No drainage network has been imported for this area, so the run has none.")
    return q, warn


def burst_cell(A, lat, lng):
    """Nearest open (non-building, non-inflow) model square to a clicked point, as (row, col)."""
    from pyproj import Transformer
    H, X0, Y1 = A["H"], A["X0"], A["Y1"]
    x, y = Transformer.from_crs(4326, 27700, always_xy=True).transform(lng, lat)
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz"); open_ = m["keep"].copy() if "keep" in m else ~m["is_bld"]
    if A["features"].get("river_inflow"):
        open_[:2, :] = False                                 # keep clear of the river inflow cells
    for r_ in areas.mesh_meta(A).get("roofs", []):
        open_[r_["cell"][0], r_["cell"][1]] = False          # and of roof downpipe cells
    j0, i0 = int((Y1 - y) // H), int((x - X0) // H)
    jj, ii = np.nonzero(open_)
    k = np.argmin((jj - j0) ** 2 + (ii - i0) ** 2)
    if (jj[k] - j0) ** 2 + (ii[k] - i0) ** 2 > (50 / H) ** 2:
        raise ValueError("The burst must be inside the model area")
    return [int(jj[k]), int(ii[k])]


def hyetograph(q):
    """Rain intensity (mm/h) per 5-min step over the storm."""
    n = q["duration"] // STEP_MIN
    edges = _cdf(q["profile"], np.linspace(0, 1, n + 1))
    return np.diff(edges) * q["depth"] * (1 + q["uplift"] / 100) * 60 / STEP_MIN


def inflow_series(q):
    """River inflow (m3/s) at 5-min resolution over the simulation."""
    n = int(round(q["sim"] * 60 / STEP_MIN)) + 1
    t = np.arange(n) * STEP_MIN
    if q["inflow"] == "none":
        return t, np.zeros(n)
    if q["inflow"] == "base":
        return t, np.full(n, q["base_q"])
    if q["inflow"] == "wave":
        b, pk, ts, tr, tf = q["wave_base"], q["wave_peak"], q["wave_start"], q["wave_rise"], q["wave_fall"]
        r = np.clip((t - ts) / tr, 0, 1); f = np.clip((t - ts - tr) / tf, 0, 1)
        shape = np.where(t < ts + tr, 0.5 * (1 - np.cos(np.pi * r)), 0.5 * (1 + np.cos(np.pi * f)))
        return t, b + (pk - b) * shape
    flow = json.load(open(DATA / "obs_2012-06-28.json"))["flow"]
    ks = sorted(k for k in flow if k >= GAUGE_START)
    gq = np.array([flow[k] for k in ks]); gt = np.arange(len(gq)) * 15.0
    return t, np.interp(t, gt, gq) * q["factor"]


def wave_shape(q, t):
    ts, tr, tf = q["wave_start"], q["wave_rise"], q["wave_fall"]
    r = np.clip((t - ts) / tr, 0, 1); f = np.clip((t - ts - tr) / tf, 0, 1)
    return np.where(t < ts + tr, 0.5 * (1 - np.cos(np.pi * r)), 0.5 * (1 + np.cos(np.pi * f)))


def river_series(q, r):
    """Inflow (m3/s) for one detected watercourse in a drawn area, at 5-min resolution."""
    n = int(round(q["sim"] * 60 / STEP_MIN)) + 1
    t = np.arange(n) * STEP_MIN
    if r["mode"] == "base":
        return t, np.full(n, r["base"])
    return t, r["base"] + (r["peak"] - r["base"]) * wave_shape(q, t)


def total_inflow(q):
    """Total river inflow for charts: the preset's river, or the sum of drawn-area inflows."""
    t, qq = inflow_series(q)
    for r in q.get("rivers", []):
        qq = qq + river_series(q, r)[1]
    return t, qq


def burst_series(q):
    """Burst flow (m3/s): off, then on for the burst duration (1-min ramps)."""
    if not q["burst"]:
        return [], []
    a, b, v = q["burst_start"], q["burst_start"] + q["burst_dur"], q["burst_q"]
    pts = ([(0, 0)] if a > 0 else []) + [(a, 0 if a > 0 else v), (a + 1, v), (b, v), (b + 1, 0)]
    return [float(t) for t, _ in pts], [float(y) for _, y in pts]


def forcing_preview(q):
    tq, qq = total_inflow(q); bt, bq = burst_series(q)
    f = dict(rain=hyetograph(q).round(2).tolist(), rain_step=STEP_MIN, q_t=tq.tolist(), q=qq.round(3).tolist(),
             burst_t=bt, burst_q=bq)
    if q.get("estuary") and q["tide_mode"] == "synthetic":
        import estuary
        m, tide, flows, _ = estuary.boundary_series(q["estuary"], q, 0)
        f.update(tide_t=m.tolist(), tide=tide.round(3).tolist())
    return f


def culvert_rating(z_inv, blockage, L_out):
    """Inlet-control rating of the elliptical culvert: stage (m) -> discharge per metre of boundary edge.
    Unsubmerged: critical flow through the ellipse (minimum specific energy). Submerged: orifice.
    Blockage reduces the effective opening width in proportion."""
    B, D, g = CULVERT_B, CULVERT_D, 9.81
    yc = np.linspace(0.01, 0.95 * D, 120)
    th = np.arccos(1 - 2 * yc / D)
    Aseg = B * D / 4 * (th - np.sin(th) * np.cos(th))
    T = B * np.sin(th)
    E = yc + Aseg / (2 * T); Qc = np.sqrt(g * Aseg ** 3 / T)
    Afull = np.pi * B * D / 4
    hs = np.concatenate([[0], E[E < 1.2 * D], np.linspace(1.2 * D, 4 * D, 12)])
    def Q(h):
        if h <= 0: return 0.0
        qw = np.interp(h, E, Qc) if h <= E[-1] else Qc[-1]
        qo = 0.6 * Afull * np.sqrt(2 * g * max(h - D / 2, 0))
        if h <= D: return qw
        if h >= 1.2 * D: return qo
        w = (h - D) / (0.2 * D); return (1 - w) * qw + w * qo
    hs = np.unique(np.round(hs, 3))
    return [(z_inv + h, (1 - blockage / 100) * Q(h) / L_out) for h in hs]


def write_inp(A, q, path, h5name, est=None):
    meta = areas.mesh_meta(A); F = A["features"]
    H, X0, Y1, NY = A["H"], A["X0"], A["Y1"], A["NY"]
    Y0 = Y1 - NY * H
    cache = Path(A["dir"]) / "cache"
    hhmm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    day = lambda m: f"01/{1 + m // 1440:02d}/2030"
    rain, (tq, qq) = hyetograph(q), inflow_series(q)
    bt, bq = burst_series(q)
    lines = (cache / "mesh_block.txt").read_text().split("\n")
    if q.get("measures"):                                  # user-placed defences: edit this run's copy of the mesh
        v0_ = lines.index("[2D_VERTICES]") + 1; t0_ = lines.index("[2D_TRIANGLES]")
        vl = [l.split() for l in lines[v0_:t0_]]
        idx_ = [k for k, p_ in enumerate(vl) if len(p_) >= 3]
        vxy = np.array([[float(vl[k][0]), float(vl[k][1])] for k in idx_]); vzz = np.array([float(vl[k][2]) for k in idx_])
        mm_ = np.load(cache / "mesh.npz"); tidal_v = None
        if "is_tyne" in mm_ and mm_["is_tyne"].any():
            jj = np.clip(np.floor((A["Y1"] - vxy[:, 1]) / A["H"]).astype(int), 0, A["NY"] - 1)
            ii = np.clip(np.floor((vxy[:, 0] - A["X0"]) / A["H"]).astype(int), 0, A["NX"] - 1)
            tidal_v = mm_["is_tyne"][jj, ii]
        jj = np.clip(np.floor((A["Y1"] - vxy[:, 1]) / A["H"]).astype(int), 0, A["NY"] - 1)
        ii = np.clip(np.floor((vxy[:, 0] - A["X0"]) / A["H"]).astype(int), 0, A["NX"] - 1)
        chan_v = (mm_["is_chan"] | mm_["is_water"])[jj, ii] if "is_water" in mm_ else mm_["is_chan"][jj, ii]
        vzz = apply_measures(A, vxy, vzz, q["measures"], tidal_v, chan_v)
        for n_, k in enumerate(idx_):
            p_ = vl[k]; p_[2] = f"{vzz[n_]:.3f}"; lines[v0_ + k] = " ".join(p_)
    suds_used = _apply_surface_measures(A, q, lines, cache)
    st1d = _structures(A, q, lines, cache)
    sw = sewer.model_lines(A, q, lines, cache, sewer.load(A), Path(path).with_name("sewer_nodes.json")) if q.get("sewer") else None
    if sw:
        for k_ in ("junctions", "outfalls", "storage", "orifices", "pumps", "curves", "xsections", "coords", "nodemap"):
            st1d[k_] += sw[k_]
    nodemap = ["[2D_TRIANGLE_NODE_MAP]"] + (["inflow OIN 0.65 4.0"] if F.get("river_inflow") else [])
    nodes, coords, inflows, ts = [], [], [], []
    if q["burst"]:
        tsq = np.load(cache / "mesh.npz")["tsq"]
        j, i = q["burst_cell"]
        ks = np.nonzero((tsq[:, 0] == j) & (tsq[:, 1] == i))[0]
        t0 = lines.index("[2D_TRIANGLES]") + 1
        vstart = lines.index("[2D_VERTICES]") + 1
        zs = []
        for k in ks:
            parts = lines[t0 + k].split()
            zs += [float(lines[vstart + int(v)].split()[2]) for v in parts[:3]]
            parts[5] = "burst"; lines[t0 + k] = " ".join(parts)
        nodemap.append("burst OBURST 0.65 4.0")
        nodes.append(f"OBURST {np.mean(zs):.2f} FREE")
        coords.append(f"OBURST {X0 + (i + .5) * H:.1f} {Y1 - (j + .5) * H:.1f}")
        inflows.append('OBURST FLOW QBURST "" 1.0 1.0')
        ts += [f"QBURST {day(int(t))} {hhmm(int(t) % 1440)} {v:.3f}" for t, v in zip(bt, bq)]
    if F.get("river_inflow"):
        ex, ey = meta["ent"]
        nodes.insert(0, f"OIN {meta['ent_z']:.2f} FREE"); coords.insert(0, f"OIN {ex:.1f} {ey - 3:.1f}")
        inflows.insert(0, 'OIN FLOW QIN "" 1.0 1.0')
        ts = [f"QIN {day(int(m))} {hhmm(int(m) % 1440)} {v:.3f}" for m, v in zip(tq, qq)] + ts
    # roofs: one downpipe node per drain cell, fed by the rain series scaled to its roof area (mm/h x m2 -> m3/s)
    roof_ts = False
    green_share = _green_roof_shares(A, q, meta, cache)       # drain id -> green roof area (m2)
    groof = _green_roof_runoff(rain, q) if green_share else None
    for r_ in meta.get("roofs", []):
        if q["burst"] and list(q.get("burst_cell", [])) == r_["cell"]:
            continue                                         # that cell carries the burst main instead
        node = "O" + r_["id"].upper()
        nodes.append(f"{node} {r_['z']:.2f} FREE"); coords.append(f"{node} {r_['x']:.1f} {r_['y']:.1f}")
        f_ = 1.0 - q.get("green_roof_fraction", 0.0)
        ga = min(green_share.get(r_["id"], 0.0), r_["area"])
        if ga > 0:                                           # part green: its own series (m3/s)
            ordinary = (r_["area"] - ga) * f_ / 3.6e6
            inflows.append(f'{node} FLOW G{r_["id"].upper()} "" 1.0 1.0')
            ts += [f"G{r_['id'].upper()} {day(k * STEP_MIN)} {hhmm(k * STEP_MIN % 1440)} "
                   f"{(rain[k] if k < len(rain) else 0) * ordinary + groof[k] * ga / 3.6e6:.6f}" for k in range(len(groof))]
            ts.append(f"G{r_['id'].upper()} {day(len(groof) * STEP_MIN)} {hhmm(len(groof) * STEP_MIN % 1440)} 0")
        else:
            inflows.append(f'{node} FLOW ROOFRAIN "" 1.0 {r_["area"] * f_ / 3.6e6:.8f}')
        nodemap.append(f"{r_['id']} {node} 0.65 4.0"); roof_ts = True
    if roof_ts:
        ts += [f"ROOFRAIN {day(k * STEP_MIN)} {hhmm(k * STEP_MIN % 1440)} {r:.3f}" for k, r in enumerate(rain)]
        ts.append(f"ROOFRAIN {day(len(rain) * STEP_MIN)} {hhmm(len(rain) * STEP_MIN % 1440)} 0")
    known = {f["id"]: f for f in meta.get("inflows", [])}
    for n_, r in enumerate(q.get("rivers", []), 1):
        f = known[r["id"]]; node = f"OR{n_}"; series = f"QR{n_}"
        nodes.append(f"{node} {f['z']:.2f} FREE")
        coords.append(f"{node} {f['x']:.1f} {f['y']:.1f}")
        inflows.append(f'{node} FLOW {series} "" 1.0 1.0')
        nodemap.append(f"{r['id']} {node} 0.65 4.0")
        rt, rq = river_series(q, r)
        ts += [f"{series} {day(int(m))} {hhmm(int(m) % 1440)} {v:.3f}" for m, v in zip(rt, rq)]
    tyne = meta.get("estuary")
    tyne_ts, tyne_bc = [], []
    if tyne:
        if est is None:
            raise RuntimeError("the Tyne estuary model must run first")
        mins, heads = est["minutes"], est["heads"]
        for nd in tyne["nodes"]:
            tyne_ts += [f"TY{nd} {day(int(t))} {hhmm(int(t) % 1440)} {h:.3f}" for t, h in zip(mins, heads[:, nd]) if t <= q["sim"] * 60 + 5]
        tyne_bc = [f"{k} {e} TS_STAGE TY{nd} * tidal" for k, e, nd in tyne["edges"]]
        tz = areas.estuary_npz(A)
        t0l = lines.index("[2D_TRIANGLES]") + 1
        h0 = heads[0]
        for k, nd, zb in zip(tz["tris"], tz["nodes"], tz["bed"]):   # start with the river at the tide level
            parts = lines[t0l + int(k)].split(); parts[4] = f"{max(0.0, h0[nd] - zb):.3f}"; lines[t0l + int(k)] = " ".join(parts)
    end = int(round(q["sim"] * 60))
    rep = 5 if end <= 240 else 10
    o = [";; UNITS: SI (m)", ";; SOURCE_CRS: EPSG:27700", "[TITLE]", f"UK Flood Sandbox: {A['name']}", "",
         "[OPTIONS]", "FLOW_UNITS CMS", "INFILTRATION HORTON", "FLOW_ROUTING DYNWAVE", "LINK_OFFSETS ELEVATION",
         "START_DATE 01/01/2030", "START_TIME 00:00:00", "REPORT_START_DATE 01/01/2030", "REPORT_START_TIME 00:00:00",
         f"END_DATE {day(end)}", f"END_TIME {hhmm(end % 1440)}:00", f"REPORT_STEP 00:{rep:02d}:00",
         "WET_STEP 00:01:00", "DRY_STEP 00:05:00", "ROUTING_STEP 00:00:05", "THREADS 0", "",
         # SCF is not applied to 2D rain in openswmm 6.0.0a4, so intensities are written at full value
         "[RAINGAGES]", f"RG1 INTENSITY 0:{STEP_MIN:02d} 1.0 TIMESERIES RAIN", "", "[TIMESERIES]"]
    for k, r in enumerate(rain):
        o.append(f"RAIN {day(k * STEP_MIN)} {hhmm(k * STEP_MIN % 1440)} {r:.3f}")
    o.append(f"RAIN {day(len(rain) * STEP_MIN)} {hhmm(len(rain) * STEP_MIN % 1440)} 0")
    o += ts + tyne_ts
    bcs = ["[2D_BOUNDARY_CONDITIONS]"] + tyne_bc
    if F.get("culvert"):
        rating = culvert_rating(meta["ext_z"], q["blockage"], H * len(meta["bc"]))
        o += ["", "[CURVES]"] + [f"CULVERT {'RATING ' if k == 0 else ''}{z:.3f} {v:.5f}" for k, (z, v) in enumerate(rating)]
        bcs += [f"{k} {e} RATING_CURVE CULVERT * outlet" for k, e in meta["bc"]]
    bcs += [f"{k} {e} NORMAL_FLOW {s:.4f} * edge" for k, e, s in meta.get("outflow", [])]
    # a detached dummy pipe keeps 1D routing active, which the 2D module needs
    o += ["", "[JUNCTIONS]", "JX 0 1"] + st1d["junctions"] + ["[OUTFALLS]", "OX -1 FREE"] + nodes + st1d["outfalls"] + \
         (["[STORAGE]"] + st1d["storage"] if st1d["storage"] else []) + [
          "[CONDUITS]", "CX JX OX 10 0.013 0 -1"] + (sw["conduits"] if sw else []) + (["[ORIFICES]"] + st1d["orifices"] if st1d["orifices"] else []) + \
         (["[PUMPS]"] + st1d["pumps"] if st1d["pumps"] else []) + \
         (["[WEIRS]"] + sw["weirs"] if sw and sw["weirs"] else []) + (["[OUTLETS]"] + sw["outlets"] if sw and sw["outlets"] else []) + ["[XSECTIONS]", "CX CIRCULAR 0.3 0 0 0 1"] + st1d["xsections"]
    if st1d["curves"]: o += ["[CURVES]"] + st1d["curves"]
    if st1d["controls"]: o += ["[CONTROLS]"] + st1d["controls"]
    nodemap += st1d["nodemap"]; coords += st1d["coords"]
    if inflows: o += ["[INFLOWS]"] + inflows
    o += ["[COORDINATES]", f"JX {X0 - 500} {Y0}", f"OX {X0 - 510} {Y0}"] + coords + ["",
          "[2D_OPTIONS]", f"OUTPUT_FILE {h5name}", "RAINFALL_MODE SYSTEM"] + (["BACKEND CUDA"] if calibration().get("backend") == "CUDA" else []) + [
          f"INFILTRATION {'NO' if q['soil'] == 'sealed' else 'YES'}", "MAX_TIMESTEP 10.0", "CFL_NUMBER 0.7", "LTS_TIERS 4", ""]
    if q["soil"] != "sealed":
        (g1, g2, g3), (m1, m2, m3) = SOILS[q["soil"]]
        sc_ = SOIL_SCALE.get(q["soil"], 1.0)
        o += ["[2D_INFILTRATION_DEFAULTS]", f"green HORTON {g1} {g2} {g3} 7 0", f"mixed HORTON {m1} {m2} {m3} 7 0"] + \
             [f"{t} HORTON {SUDS[t][0][0] * sc_:.1f} {SUDS[t][0][1] * sc_:.1f} {SUDS[t][0][2]} 7 0" for t in sorted(suds_used)] + [""]
    Path(path).write_text("\n".join(o) + "\n" + "\n".join(lines) + "\n".join(nodemap) + "\n\n" + "\n".join(bcs) + "\n")
    end_m = q["sim"] * 60
    if not F.get("river_inflow"):
        qq = np.zeros_like(qq)
    for r in q.get("rivers", []):
        qq = qq + river_series(q, r)[1]
    v_river = float(np.sum((qq[1:] + qq[:-1]) / 2 * np.diff(tq) * 60)) if len(tq) > 1 else 0.0
    v_burst = q["burst_q"] * min(q["burst_dur"], max(end_m - q["burst_start"], 0)) * 60 if q["burst"] else 0.0
    roof_area = sum(r_["area"] for r_ in meta.get("roofs", []) if not (q["burst"] and list(q.get("burst_cell", [])) == r_["cell"]))
    v_roof = float(np.sum(rain) * STEP_MIN / 60 / 1000 * roof_area)       # mm/h steps -> m of rain x roof area
    _gs = _green_roof_shares(A, q, meta, cache)
    if _gs:
        _gr = _green_roof_runoff(rain, q); ga_tot = sum(_gs.values())
        v_roof -= float((np.sum(rain) - np.sum(_gr)) * STEP_MIN / 60 / 1000 * ga_tot)
    f = dict(rain=rain.round(2).tolist(), rain_step=STEP_MIN, q_t=tq.tolist(), q=qq.round(3).tolist(),
             burst_t=bt, burst_q=bq, report_step=rep, v_river_in=v_river, v_burst_in=v_burst, v_roof_in=v_roof,
             roof_area_m2=round(roof_area))
    if tyne:
        nd_up, nd_dn = tyne["nodes"][0], tyne["nodes"][-1]
        keep = est["minutes"] <= q["sim"] * 60 + 0.1
        f["estuary"] = dict(id=tyne["id"], name=tyne["name"], river=tyne.get("river"), minutes=est["minutes"][keep].tolist(), up=est["heads"][keep, nd_up].round(3).tolist(),
                         down=est["heads"][keep, nd_dn].round(3).tolist(), mouth=est["tide"][keep].round(3).tolist(),
                         limit_flow=next(iter(est["flows"].values()))[keep].round(1).tolist(), routing_error=est["routing_error"],
                         sources=est.get("sources", {}))
    return f


# ------------------------------------------------------------------ results
_DEPTH_STOPS = np.array([0.02, 0.1, 0.3, 0.6, 1.0, 1.8])
_DEPTH_COLS = np.array([[198, 232, 240], [140, 205, 228], [66, 160, 206], [24, 108, 170], [12, 68, 128], [8, 36, 78]])
_SPEED_STOPS = np.array([0.05, 0.5, 1.0, 2.0, 3.0])
_SPEED_COLS = np.array([[250, 232, 200], [240, 190, 110], [214, 130, 50], [160, 70, 30], [90, 30, 20]])
_png = areas._png


def _rgba_depth(a):
    a = np.nan_to_num(a, nan=0); out = np.zeros(a.shape + (4,), np.uint8)
    for k in range(3): out[..., k] = np.interp(a, _DEPTH_STOPS, _DEPTH_COLS[:, k])
    out[..., 3] = np.where(a < 0.02, 0, np.interp(a, [0.02, 0.06, 0.3], [0, 170, 225]))
    return out


def _rgba_speed(v, d):
    v, d = np.nan_to_num(v), np.nan_to_num(d); out = np.zeros(v.shape + (4,), np.uint8)
    for k in range(3): out[..., k] = np.interp(v, _SPEED_STOPS, _SPEED_COLS[:, k])
    out[..., 3] = np.where((d > 0.02) & (v > 0.05), 220, 0)
    return out


def _warper(A):
    from rasterio.warp import reproject, Resampling, calculate_default_transform, transform_bounds
    from rasterio.transform import from_origin
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    st = from_origin(X0, Y1, H, H)
    res = max(4.0, 0.8 * H, max(NX, NY) * H / 1400)
    dt, w, h = calculate_default_transform("EPSG:27700", "EPSG:3857", NX, NY, X0, Y1 - NY * H, X0 + NX * H, Y1, resolution=res)
    def warp(a):
        out = np.full((h, w), np.nan, np.float32)
        reproject(a.astype(np.float32), out, src_transform=st, src_crs="EPSG:27700", dst_transform=dt,
                  dst_crs="EPSG:3857", resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
        return out
    b = transform_bounds("EPSG:3857", "EPSG:4326", dt.c, dt.f + dt.e * h, dt.c + dt.a * w, dt.f)
    return warp, [[b[1], b[0]], [b[3], b[2]]]




def _structures(A, q, lines, cache):
    """Storage tanks, pumps and flood gates as 1D elements linked to the 2D surface. Returns dict of extra
    input-file lines and the triangle tags used for the links."""
    import shapely
    from shapely.geometry import LineString
    ms = [m for m in (q.get("measures") or []) if m["type"] in ("tank", "pump", "gate")]
    out = dict(junctions=[], outfalls=[], storage=[], orifices=[], pumps=[], xsections=[], curves=[], controls=[], coords=[], nodemap=[])
    if not ms:
        return out
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    t0 = lines.index("[2D_TRIANGLES]") + 1; v0 = lines.index("[2D_VERTICES]") + 1
    tsq = np.load(cache / "mesh.npz")["tsq"]
    first = {}
    for k, (j, i) in enumerate(tsq.tolist()): first.setdefault((j, i), k)
    vz = np.array([float(l.split()[2]) for l in lines[v0:t0 - 2] if len(l.split()) >= 3])
    tri = [lines[t0 + k].split() for k in range(len(tsq))]
    def cell_of(x, y):
        return int((Y1 - y) // H), int((x - X0) // H)
    def free_cell(x, y, avoid=(), keep_clear=None):
        j0, i0 = cell_of(x, y)
        for r in range(0, 6):
            best = None
            for j in range(j0 - r, j0 + r + 1):
                for i in range(i0 - r, i0 + r + 1):
                    k = first.get((j, i))
                    if k is None or (j, i) in avoid: continue
                    if keep_clear is not None and keep_clear(X0 + (i + .5) * H, Y1 - (j + .5) * H): continue
                    if tri[k][5] in ("mixed", "green", "paved", "chan", "perm", "raing", "swale", "wood"):
                        d = (j - j0) ** 2 + (i - i0) ** 2
                        if best is None or d < best[0]: best = (d, j, i)
            if best: return best[1], best[2]
        return None
    taken = set()
    def link(tag, j, i, node, area=4.0):
        k = first[(j, i)]
        for kk in (k, k + 1):
            if kk < len(tri) and tuple(tsq[kk]) == (j, i): tri[kk][5] = tag
        taken.add((j, i)); out["nodemap"].append(f"{tag} {node} 0.65 {area:.3f}")      # exchange area (m2)
        out["coords"].append(f"{node} {X0 + (i + .5) * H:.1f} {Y1 - (j + .5) * H:.1f}")
    def zcell(j, i):
        k = first[(j, i)]; return float(np.mean([vz[int(v)] for v in tri[k][:3]]))
    rules, n = [], 0
    for m in ms:
        n += 1
        if m["type"] == "tank":
            c = free_cell(*m["pts"][0], taken)
            if not c: continue
            j, i = c; z = zcell(j, i); inlet, tank = f"TI{n}", f"TS{n}"
            out["junctions"].append(f"{inlet} {z - 0.6:.3f} 0.6 0 0 0")
            area = m["volume"] / m["depth"]
            out["storage"].append(f"{tank} {z - 0.6 - m['depth']:.3f} {m['depth']:.2f} 0 FUNCTIONAL 0 0 {area:.1f} 0 0")
            dia = math.sqrt(4 * (m["inflow"] / (0.65 * math.sqrt(2 * 9.81 * 0.6))) / math.pi)      # passes the rate at 0.6 m head
            out["orifices"].append(f"OT{n} {inlet} {tank} SIDE {z - 0.6:.3f} 0.65 YES")
            out["xsections"].append(f"OT{n} CIRCULAR {dia:.3f} 0 0 0")
            out["coords"].append(f"{tank} {X0 + (i + .5) * H + 2:.1f} {Y1 - (j + .5) * H:.1f}")
            link(f"tank{n}", j, i, inlet, max(0.05, math.pi * dia ** 2 / 4 * 1.5))
        elif m["type"] == "pump":
            c = free_cell(*m["pts"][0], taken)
            if not c: continue
            j, i = c; z = zcell(j, i); well, dis = f"PW{n}", f"PD{n}"
            out["storage"].append(f"{well} {z - 3:.3f} 3.0 0 FUNCTIONAL 0 0 20 0 0")
            link(f"pin{n}", j, i, well, max(0.05, 2 * m["rate"] / (0.65 * math.sqrt(2 * 9.81 * 0.3))))
            if len(m["pts"]) > 1 and (c2 := free_cell(*m["pts"][1], taken)):
                j2, i2 = c2; out["outfalls"].append(f"{dis} {zcell(j2, i2):.3f} FREE"); link(f"pout{n}", j2, i2, dis)
            else:
                out["outfalls"].append(f"{dis} {z - 3:.3f} FREE"); out["coords"].append(f"{dis} {X0 + (i + .5) * H + 3:.1f} {Y1 - (j + .5) * H:.1f}")
            out["curves"] += [f"PC{n} PUMP2 0.0 {m['rate']:.4f}", f"PC{n} 3.0 {m['rate']:.4f}"]
            out["pumps"].append(f"P{n} {well} {dis} PC{n} ON {m['start']:.2f} 0.05")
        elif m["type"] == "gate":
            ln = LineString(m["pts"]); L = ln.length; pairs = []
            for s in np.arange(H / 2, L, H):
                p = ln.interpolate(s); a = ln.interpolate(min(s + 1, L)); b_ = ln.interpolate(max(s - 1, 0))
                dx, dy = a.x - b_.x, a.y - b_.y; nn = math.hypot(dx, dy) or 1; nx, ny = -dy / nn, dx / nn
                near_wall = lambda x, y, ln=ln: ln.distance(shapely.Point(x, y)) < H * 1.6     # cells raised by the wall itself
                side = lambda x, y, sx=p.x, sy=p.y, nx=nx, ny=ny: (x - sx) * nx + (y - sy) * ny
                ca = free_cell(p.x + nx * H * 2.2, p.y + ny * H * 2.2, taken, lambda x, y: near_wall(x, y) or side(x, y) <= 0)
                cb = free_cell(p.x - nx * H * 2.2, p.y - ny * H * 2.2, taken, lambda x, y: near_wall(x, y) or side(x, y) >= 0)
                if not ca or not cb or ca == cb: continue
                ga, gb, gid = f"GA{n}_{len(pairs)}", f"GB{n}_{len(pairs)}", f"GO{n}_{len(pairs)}"
                za, zb = zcell(*ca), zcell(*cb); base = min(za, zb) - 0.3
                out["junctions"] += [f"{ga} {base:.3f} {m['height'] + 1:.2f} 0 0 0", f"{gb} {base:.3f} {m['height'] + 1:.2f} 0 0 0"]
                opening = H * (m["height"] + 0.3)                       # the full opening, so an open gate passes water freely
                link(f"g{n}a{len(pairs)}", *ca, ga, opening); link(f"g{n}b{len(pairs)}", *cb, gb, opening)
                out["orifices"].append(f"{gid} {ga} {gb} SIDE {base:.3f} 0.65 NO")
                out["xsections"].append(f"{gid} RECT_CLOSED {m['height'] + 0.3:.2f} {H:.2f} 0 0")
                pairs.append((gid, ga))
            if not pairs: continue
            if m["mode"] == "closed":
                rules.append(f"RULE R{n}\nIF SIMULATION TIME >= 0\nTHEN " + "\nAND ".join(f"ORIFICE {g} SETTING = 0" for g, _ in pairs))
            elif m["mode"] == "time":
                hrs = m["close_min"] / 60
                rules.append(f"RULE R{n}\nIF SIMULATION TIME >= {hrs:.3f}\nTHEN " + "\nAND ".join(f"ORIFICE {g} SETTING = 0" for g, _ in pairs)
                             + "\nELSE " + "\nAND ".join(f"ORIFICE {g} SETTING = 1" for g, _ in pairs))
            elif m["mode"] == "level":
                g0, a0 = pairs[0]
                rules.append(f"RULE R{n}\nIF NODE {a0} DEPTH > {m['close_level'] + 0.3:.2f}\nTHEN " + "\nAND ".join(f"ORIFICE {g} SETTING = 0" for g, _ in pairs))
    out["controls"] = rules
    for k, parts in enumerate(tri): lines[t0 + k] = " ".join(parts)
    return out



def _structure_stats(rpt):
    """Tanks (largest volume held) and pumps (volume pumped) from the engine's report."""
    try:
        lines = Path(rpt).read_text(errors="ignore").split("\n")
    except Exception:
        return {}
    tank, pumped = 0.0, 0.0
    for l in lines:
        p = l.split()
        if len(p) >= 6 and p[0].startswith("TS"):
            try: tank += float(p[5]) * 1000
            except ValueError: pass
        if len(p) >= 7 and p[0].startswith("P") and p[0][1:].isdigit():
            try: pumped += float(p[6]) * 1000
            except ValueError: pass
    return dict(v_tanks=round(tank), v_pumped=round(pumped)) if (tank or pumped) else {}


def _measure_cells(A, m, shape):
    """Cells whose centres lie in an area measure, or within the width of a line measure."""
    from rasterio import features
    from rasterio.transform import from_origin
    from shapely.geometry import Polygon, LineString
    g = Polygon(m["pts"]) if m["type"] in AREA_TYPES else LineString(m["pts"]).buffer(max(m.get("width", A["H"]) / 2, A["H"] * 0.6))
    return features.rasterize([(g, 1)], out_shape=shape, transform=from_origin(A["X0"], A["Y1"], A["H"], A["H"])).astype(bool)


def _apply_surface_measures(A, q, lines, cache):
    """Permeable surfaces, rain gardens, swales and tree planting: retag the cells they cover (infiltration and
    roughness). Only ordinary ground is changed (not rivers, roof drains, inflows or the burst)."""
    tagfor = {"permeable": "perm", "raingarden": "raing", "swale": "swale", "trees": "wood"}
    ms = [m for m in (q.get("measures") or []) if m["type"] in tagfor]
    if not ms:
        return set()
    tsq = np.load(cache / "mesh.npz")["tsq"]
    t0 = lines.index("[2D_TRIANGLES]") + 1
    used = set()
    for m in ms:
        tag = tagfor[m["type"]]; cells = _measure_cells(A, m, (A["NY"], A["NX"]))
        for k in np.nonzero(cells[tsq[:, 0], tsq[:, 1]])[0]:
            parts = lines[t0 + k].split()
            if len(parts) >= 6 and parts[5] in ("mixed", "green", "paved"):
                parts[5] = tag; parts[3] = f"{SUDS[tag][1]:.3f}"; lines[t0 + k] = " ".join(parts); used.add(tag)
    return used


def _green_roof_shares(A, q, meta, cache):
    """Drain id -> roof area (m2) of buildings selected as green roofs (cells of their footprint in the outline)."""
    ms = [m for m in (q.get("measures") or []) if m["type"] == "greenroof"]
    p = cache / "roof_grp.npy"
    if not ms or not p.exists():
        return {}
    grp = np.load(p); H = A["H"]
    sel = np.zeros(grp.shape, bool)
    for m in ms: sel |= _measure_cells(A, m, grp.shape)
    green_gids = set(np.unique(grp[sel & (grp > 0)]).tolist())
    if not green_gids:
        return {}
    cnt = {int(g): int(n) for g, n in zip(*np.unique(grp[grp > 0], return_counts=True))}
    return {r_["id"]: float(sum(cnt.get(g, 0) for g in r_.get("gids", []) if g in green_gids) * H * H)
            for r_ in meta.get("roofs", []) if any(g in green_gids for g in r_.get("gids", []))}


def _green_roof_runoff(rain, q):
    """Runoff from a green roof (mm/h per step): the substrate retains the first `retention` mm, and what drains
    is delayed through a linear store (time constant 30 minutes). Indicative extensive green roof."""
    ms = [m for m in (q.get("measures") or []) if m["type"] == "greenroof"]
    ret = max(m.get("retention", 15.0) for m in ms) if ms else 15.0
    dt = STEP_MIN / 60.0; k = 0.5                                  # hours
    store, slow, out = 0.0, 0.0, []
    for r in list(rain) + [0.0] * 12:                              # an hour of tail after the rain
        store += r * dt
        spill = max(0.0, store - ret); store -= spill
        slow += spill; q_ = slow * (1 - np.exp(-dt / k)); slow -= q_
        out.append(q_ / dt)
    return np.array(out)



HAZ_RGBA = [(0, 0, 0, 0), (250, 220, 120, 190), (240, 150, 60, 210), (205, 70, 50, 225), (120, 20, 60, 235)]
HAZ_NAMES = ["", "Low (caution)", "Moderate (danger for some)", "Significant (danger for most)", "Extreme (danger for all)"]


def _rgba_hazard(cls):
    c = np.clip(np.nan_to_num(np.round(cls)), 0, 4).astype(int)
    return np.array(HAZ_RGBA, np.uint8)[c]


def _hazard_stats(HZC, excl, H):
    land = ~excl
    return {f"hazard{k}_ha": round(float(((HZC == k) & land).sum() * H * H / 1e4), 2) for k in range(1, 5)}


SITE_KINDS = {"school": "School", "kindergarten": "Nursery", "college": "College", "university": "University",
              "hospital": "Hospital", "clinic": "Clinic", "doctors": "GP surgery", "nursing_home": "Care home",
              "social_facility": "Care or social facility", "fire_station": "Fire station", "police": "Police station",
              "station": "Railway station", "townhall": "Town hall"}


from areas import ROADW  # noqa: E402  (road widths by OpenStreetMap highway type)


def _roads_and_sites(A, MD, excl):
    """Roads impassable to cars (max depth over 0.3 m) and key sites with water over 0.3 m at their entrance
    (within 15 m), from the area's OpenStreetMap data."""
    import json as _j
    from shapely.geometry import LineString, Point, Polygon
    from pyproj import Transformer
    d = Path(A["dir"]); H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    try:
        osm = _j.loads((d / "data" / "osm.json").read_text())["elements"]
    except Exception:
        return {}
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    md = np.where(excl, np.nan, MD)
    cell = lambda x, y: (int((Y1 - y) // H), int((x - X0) // H))
    roads, length = {}, {"over_0_3": 0.0, "over_0_1": 0.0, "total": 0.0}
    sites = []
    for e in osm:
        tg = e.get("tags", {})
        g = e.get("geometry")
        if e["type"] == "way" and g and tg.get("highway") in ROADW and not tg.get("bridge") and not tg.get("tunnel"):
            xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g]); ln = LineString(list(zip(xs, ys)))
            for s in np.arange(1.25, ln.length, 2.5):
                p = ln.interpolate(s); j, i = cell(p.x, p.y)
                if not (0 <= j < NY and 0 <= i < NX) or not np.isfinite(md[j, i]): continue
                length["total"] += 2.5; dv = md[j, i]
                if dv > 0.1: length["over_0_1"] += 2.5
                if dv > 0.3:
                    length["over_0_3"] += 2.5; nm = tg.get("name") or tg.get("ref") or "Unnamed road"
                    r = roads.setdefault(nm, [0.0, 0.0]); r[0] += 2.5; r[1] = max(r[1], float(dv))
        kind = tg.get("amenity") if tg.get("amenity") in SITE_KINDS else ("station" if tg.get("railway") == "station" else
               ("nursing_home" if tg.get("social_facility") == "nursing_home" else None))
        if kind:
            if e["type"] == "node": x, y = tf(e["lon"], e["lat"])
            elif g:
                xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g])
                c_ = Polygon(list(zip(xs, ys))).centroid if len(g) > 3 else Point(xs[0], ys[0]); x, y = c_.x, c_.y
            else: continue
            j, i = cell(x, y); r_ = int(np.ceil(15 / H))
            if not (0 <= j < NY and 0 <= i < NX): continue
            win = md[max(j - r_, 0):j + r_ + 1, max(i - r_, 0):i + r_ + 1]
            dv = float(np.nanmax(win)) if np.isfinite(win).any() else 0.0
            sites.append(dict(name=tg.get("name") or SITE_KINDS[kind], kind=SITE_KINDS[kind], depth=round(dv, 2)))
    hit = sorted([s for s in sites if s["depth"] > 0.3], key=lambda s: -s["depth"])
    top = sorted(roads.items(), key=lambda kv: -kv[1][0])[:8]
    return dict(roads_km=round(length["total"] / 1000, 2), roads_over_0_3_km=round(length["over_0_3"] / 1000, 2),
                roads_over_0_1_km=round(length["over_0_1"] / 1000, 2),
                roads_worst=[dict(name=k, m=round(v[0]), depth=round(v[1], 2)) for k, v in top],
                sites_total=len(sites), sites_affected=len(hit), sites_list=hit[:12])



# Direct flood damage to buildings: JRC global flood depth-damage functions (Huizinga, de Moel and Szewczyk 2017,
# EUR 28552 EN, doi:10.2760/16510), European damage curves and the United Kingdom's building-based maximum damage
# values (structure + content, euros per m2 of building, 2010 price level), read from the JRC database
# (copy_of_global_flood_depth-damage_functions__30102017.xlsx).
JRC_DEPTH = [0, 0.5, 1, 1.5, 2, 3, 4, 5, 6]
JRC_EUROPE = {"residential": [0, 0.25, 0.40, 0.50, 0.60, 0.75, 0.85, 0.95, 1.00],
              "commercial":  [0, 0.15, 0.30, 0.45, 0.55, 0.75, 0.90, 1.00, 1.00],
              "industrial":  [0, 0.15, 0.27, 0.40, 0.52, 0.70, 0.85, 1.00, 1.00]}
JRC_UK_MAX = {"residential": 758.12, "commercial": 1053.19, "industrial": 855.95}      # EUR/m2, 2010 prices


def _damage(A, MD, keep_mask, factor=1.0):
    """Estimated direct damage to buildings for this run: for each building, the deepest water on the open ground
    beside it, the JRC European curve for its type (from OpenStreetMap tags) and its footprint area."""
    from scipy import ndimage
    import json as _j
    cache = Path(A["dir"]) / "cache"
    if not (cache / "bld_ids.npy").exists() or not (cache / "bld_meta.json").exists():
        return {}
    bid = np.load(cache / "bld_ids.npy"); meta = _j.loads((cache / "bld_meta.json").read_text())
    if bid.shape != MD.shape or not bid.any():
        return {}
    md = np.nan_to_num(MD, nan=0.0)
    if keep_mask is not None: md = np.where(keep_mask, md, 0)
    around = ndimage.maximum_filter(md, size=3)
    ids = np.unique(bid[bid > 0]); depth = np.asarray(ndimage.maximum(around, labels=bid, index=ids))
    total, by_type, n = 0.0, {}, 0
    for k, dv in zip(ids, depth):
        if dv <= 0.02 or k - 1 >= len(meta): continue
        b = meta[k - 1]; t = b.get("type", "residential")
        frac = float(np.interp(min(dv, 6.0), JRC_DEPTH, JRC_EUROPE[t]))
        eur = frac * JRC_UK_MAX[t] * b["area"] * factor
        total += eur; by_type[t] = by_type.get(t, 0) + eur; n += 1
    return dict(damage_eur=round(total), damage_by_type={t: round(v) for t, v in by_type.items()}, buildings_damaged=n,
                damage_factor=factor)



def _write_cf(path, A, q, f, grid, minutes, MD, MV, HZC):
    """Time series of the run on the model's grid as CF-1.8 NetCDF: depth, water level and speed at every saved
    frame, plus maxima, hazard class and ground level. British National Grid; opens in QGIS, ArcGIS Pro, xarray."""
    import netCDF4, datetime as _dt
    from pyproj import CRS
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    nc = netCDF4.Dataset(str(path), "w", format="NETCDF4")
    try:
        nc.createDimension("time", None); nc.createDimension("y", NY); nc.createDimension("x", NX)
        x = nc.createVariable("x", "f8", ("x",)); x[:] = X0 + (np.arange(NX) + 0.5) * H
        x.standard_name, x.long_name, x.units, x.axis = "projection_x_coordinate", "easting", "m", "X"
        y = nc.createVariable("y", "f8", ("y",)); y[:] = Y1 - (np.arange(NY) + 0.5) * H
        y.standard_name, y.long_name, y.units, y.axis = "projection_y_coordinate", "northing", "m", "Y"
        crs = nc.createVariable("crs", "i4")
        for k, v in CRS.from_epsg(27700).to_cf().items(): setattr(crs, k, v)
        t = nc.createVariable("time", "f8", ("time",)); t[:] = np.asarray(minutes, float)
        if q.get("tide_mode") == "recorded" and q.get("tide_start"):
            t.units = f"minutes since {_dt.datetime.fromisoformat(q['tide_start']).strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            t.units = "minutes since 2000-01-01 00:00:00"
            t.comment = "Minutes from the start of the scenario; the date is nominal (designed storm)."
        t.standard_name, t.calendar, t.axis = "time", "standard", "T"
        def var(name, dims, dtype="f4", **att):
            v = nc.createVariable(name, dtype, dims, zlib=True, complevel=4, shuffle=True,
                                  fill_value=(np.float32(-9999.0) if dtype == "f4" else np.int8(-1)),
                                  least_significant_digit=(3 if dtype == "f4" else None))
            v.grid_mapping = "crs"
            for k, val in att.items(): setattr(v, k, val)
            return v
        datum = "Ordnance Datum Belfast" if A["Y1"] < 600000 and A["X0"] < 200000 else "Ordnance Datum Newlyn"
        dv = var("depth", ("time", "y", "x"), long_name="water depth", units="m")
        hv = var("water_level", ("time", "y", "x"), long_name="water surface level", units="m",
                 standard_name="water_surface_height_above_reference_datum", reference_datum=datum)
        sv = var("speed", ("time", "y", "x"), long_name="water speed (depth-averaged)", units="m s-1")
        depth, head = f["Mesh2_face_depth"], f["Mesh2_face_head"]
        vx, vy = (f["Mesh2_face_vx"], f["Mesh2_face_vy"]) if "Mesh2_face_vx" in f else (None, None)
        for k in range(depth.shape[0]):
            dk = np.asarray(depth[k]); g = grid(dk)
            dv[k] = np.where(np.isfinite(g), g, -9999.0)
            hk = grid(np.asarray(head[k])); hv[k] = np.where(np.isfinite(hk), hk, -9999.0)
            if vx is not None:
                sk = grid(np.where(dk > 0.001, np.hypot(np.asarray(vx[k]), np.asarray(vy[k])), 0.0)); sv[k] = np.where(np.isfinite(sk), sk, -9999.0)
        for name, arr, att in (("max_depth", MD, dict(long_name="maximum water depth", units="m")),
                               ("max_speed", MV, dict(long_name="maximum water speed", units="m s-1")),
                               ("ground_level", grid(np.asarray(f["Mesh2_face_z"][:])), dict(long_name="ground level of the model cell",
                                standard_name="surface_altitude", units="m", reference_datum=datum))):
            v = var(name, ("y", "x"), **att); v[:] = np.where(np.isfinite(arr), arr, -9999.0)
        hz = var("hazard_class", ("y", "x"), dtype="i1", long_name="hazard to people (Defra/EA FD2320)",
                 flag_values=np.array([0, 1, 2, 3, 4], np.int8), flag_meanings="none low moderate significant extreme")
        hz[:] = np.where(np.isfinite(MD), HZC.astype(np.int8), np.int8(-1))
        nc.Conventions = "CF-1.8"
        nc.title = f"UK Flood Sandbox: {A['name']}"
        nc.institution = "UK Flood Sandbox (local run)"
        nc.source = "OpenSWMM 6 two-dimensional surface model; triangle results averaged to the model's square cells"
        nc.history = f"{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} written by UK Flood Sandbox"
        nc.references = "Hazard: Defra/EA FD2320. Terrain: national LiDAR (Open Government Licence). Map data (c) OpenStreetMap contributors."
        nc.comment = (f"Scenario: {q.get('depth', 0)} mm of rain in {q.get('duration', 0)} min ({q.get('profile', '')}), "
                      f"{q.get('sim', 0)} h simulated; {len(q.get('measures') or [])} user defences. Indicative results.")
        nc.scenario_json = json.dumps({k: v for k, v in q.items() if k not in ("measures",)})[:20000]
    finally:
        nc.close()


def _buildings_affected(A, MD, keep_mask=None, threshold=0.3):
    """Buildings with water deeper than `threshold` m on the open ground right beside them."""
    from scipy import ndimage
    p = Path(A["dir"]) / "cache" / "bld_ids.npy"
    if not p.exists():
        return {}
    bid = np.load(p)
    if bid.shape != MD.shape or not bid.any():
        return {}
    md = np.nan_to_num(MD, nan=0.0)
    if keep_mask is not None: md = np.where(keep_mask, md, 0)
    around = ndimage.maximum_filter(md, size=3)
    ids = np.unique(bid[bid > 0])
    mx = ndimage.maximum(around, labels=bid, index=ids)
    return dict(buildings_total=int(len(ids)), buildings_affected=int((np.asarray(mx) > threshold).sum()))


def postprocess(A, h5path, q, forcing, runtime_s):
    import h5py
    from pyproj import Transformer
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz"); tsq, is_chan = m["tsq"], m["is_chan"]
    meta = areas.mesh_meta(A); F = A["features"]
    def grid(vals):
        g, c = np.zeros((NY, NX)), np.zeros((NY, NX))
        np.add.at(g, (tsq[:, 0], tsq[:, 1]), vals); np.add.at(c, (tsq[:, 0], tsq[:, 1]), 1)
        out = np.full((NY, NX), np.nan); k = c > 0; out[k] = g[k] / c[k]; return out
    f = h5py.File(h5path, "r")
    t = f["time"][:]; minutes = np.round((t - t[0]) * 1440 + forcing["report_step"]).astype(int)
    depth = f["Mesh2_face_depth"]; flux = f["Mesh2_edge_flux"]
    mdt = f["Mesh2_face_max_depth"][:]
    MD, MV = grid(mdt), grid(f["Mesh2_face_max_velocity"][:])
    # hazard to people (Defra/EA FD2320): HR = d (v + 0.5) + DF, with DF = 1 where d > 0.25 m (urban debris factor);
    # worked out at every saved frame from that frame's depth and speed, and the highest value kept
    HZ = np.zeros_like(MD)
    if "Mesh2_face_vx" in f:
        vx_, vy_ = f["Mesh2_face_vx"], f["Mesh2_face_vy"]
        for k in range(depth.shape[0]):
            dk = np.asarray(depth[k]); vk = np.hypot(np.asarray(vx_[k]), np.asarray(vy_[k]))
            hr = dk * (np.minimum(vk, 10.0) + 0.5) + np.where(dk > 0.25, 1.0, 0.0)
            HZ = np.fmax(HZ, grid(np.where(dk > 0.1, hr, 0.0)))     # rated where at least 0.1 m deep
    else:
        HZ = np.nan_to_num(MD) * (np.minimum(np.nan_to_num(MV), 10) + 0.5) + np.where(np.nan_to_num(MD) > 0.25, 1.0, 0.0)
    HZC = np.digitize(np.nan_to_num(HZ), [0.001, 0.75, 1.25, 2.0]).astype(np.uint8)   # 0 none, 1 low .. 4 extreme
    warp, bounds = _warper(A)
    frames, cells_u8 = [], []
    q8 = lambda a, step: base64.b64encode(np.clip(np.round(np.nan_to_num(a, nan=0) / step), 0, 255).astype(np.uint8).tobytes()).decode()
    for k in range(depth.shape[0]):
        gk = grid(depth[k])
        frames.append(_png(_rgba_depth(warp(gk))))
        cells_u8.append(q8(gk, 0.02))                      # depth per model cell, 2 cm steps (for the 3D view)
    outlet = [(c, e) for c, e in meta.get("bc", [])] + [(c, e) for c, e, s in meta.get("outflow", [])]
    if outlet:
        cs = np.array([c for c, e in outlet]); es = np.array([e for c, e in outlet])
        qout = np.array([float(flux[k][cs, es].sum()) for k in range(flux.shape[0])])
    else:
        qout = np.zeros(len(minutes))
    mb = {k: float(f[f"mass_balance_2d/{k}"][()]) for k in f["mass_balance_2d"]}
    # every inflow and outflow term of the 2D surface, including exchanges with 1D elements (tanks, pumps, gates)
    vin = mb.get("init_storage", 0.0) + sum(v for k, v in mb.items() if k.endswith("_in"))
    vout = mb.get("final_storage", 0.0) + sum(v for k, v in mb.items() if k.endswith("_out"))
    v1d_net = sum(v for k, v in mb.items() if k.endswith("_out") and ("drain" in k or "1d" in k.lower())) - \
              sum(v for k, v in mb.items() if k.endswith("_in") and ("spill" in k or "1d" in k.lower()))
    from scipy import ndimage
    is_tyne = m["is_tyne"] if "is_tyne" in m else np.zeros_like(is_chan)
    is_water = m["is_water"] if "is_water" in m else np.zeros_like(is_chan)
    # permanent water (the Tyne, mapped ponds, basins and docks) is not "flooding": keep it and its edge cells
    # out of the land statistics
    # a 15 m margin also covers unmapped foreshore and quay-wall cells along tidal rivers
    is_tyne = ndimage.binary_dilation(is_tyne | is_water, iterations=max(1, round(15 / H))) if (is_tyne.any() or is_water.any()) else is_tyne
    # user storage basins hold water on purpose: not counted as flooding either
    basins = [mm_ for mm_ in (q.get("measures") or []) if mm_["type"] == "basin"]
    if basins:
        from rasterio import features as _f
        from rasterio.transform import from_origin as _fo
        from shapely.geometry import Polygon as _P
        bm = _f.rasterize([(_P(b["pts"]), 1) for b in basins], out_shape=MD.shape, transform=_fo(A["X0"], A["Y1"], H, H)).astype(bool)
        is_tyne = is_tyne | bm
    MDl = np.where(is_tyne, np.nan, MD); MVl = np.where(is_tyne, np.nan, MV)
    if "inside" in m and A["features"].get("auto_outflow"):       # boundary cells: drawdown at open edges
        rim = m["inside"] & ~ndimage.binary_erosion(m["inside"], iterations=2)
        MVl = np.where(rim, np.nan, MVl)
    off = np.where(is_chan | is_tyne, np.nan, MD)
    if np.all(np.isnan(off)): off = np.nan_to_num(MD)
    jo, io_ = np.unravel_index(np.nanargmax(off), off.shape)
    area_ha = lambda a, th: round(float(np.nansum(a > th)) * H * H / 1e4, 2)
    kpk = int(np.argmax(qout))
    culv_depth = float(max(mdt[c] for c, e in meta["bc"])) if F.get("culvert") else None
    rain_mm = q["depth"] * (1 + q["uplift"] / 100)
    tot_in = forcing["v_river_in"] + forcing["v_burst_in"] + forcing.get("v_roof_in", 0)
    share = mb["outfall_in"] / tot_in if tot_in > 0 else 0
    spd = np.where(MDl > 0.05, MVl, np.nan)
    if F.get("culvert"): spd = spd[:NY - 3]          # skip the outlet drawdown rows
    stats = dict(
        rain_mm=round(rain_mm, 1), peak_mm_h=round(float(max(forcing["rain"], default=0)), 1), culvert_depth=
        round(culv_depth, 2) if culv_depth is not None else None,
        wet10_ha=area_ha(MDl, 0.1), wet30_ha=area_ha(MDl, 0.3), wet30_off_ha=area_ha(off, 0.3),
        max_depth=round(float(np.nanmax(MDl)), 2), max_depth_off=round(float(np.nanmax(off)), 2),
        max_speed=round(float(np.nanmax(np.where(spd > 10, np.nan, spd))) if (np.isfinite(spd) & (spd <= 10)).any() else 0.0, 2),
        **_buildings_affected(A, MD, m["keep"] if "keep" in m else None),
        **_hazard_stats(HZC, is_tyne, H),
        **_damage(A, MD, m["keep"] if "keep" in m else None, float(q.get("damage_factor", 1.0))),
        measures_cost=round(sum(float(mm_.get("cost") or 0) for mm_ in (q.get("measures") or []))),
        **_roads_and_sites(A, MD, is_tyne),
        measures=len(q.get("measures") or []),
        speed_artefact_cells=int(np.nansum(spd > 10)),   # unphysical speeds at steep drops (engine instability), excluded
        peak_out=round(float(qout.max()), 2), peak_out_min=int(minutes[kpk]),
        v_rain=round(mb["rainfall_in"]), v_river=round(forcing["v_river_in"] * share), v_burst=round(forcing["v_burst_in"] * share),
        v_roof=round(forcing.get("v_roof_in", 0) * share), roof_area_ha=round(forcing.get("roof_area_m2", 0) / 1e4, 2),
        v_out=round(mb["boundary_out"]), v_infil=round(mb["infil_out"]), v_stored=round(mb["final_storage"]),
        v_est_in=round(mb["boundary_in"]), v_init=round(mb["init_storage"]),
        mb_error_pct=round(100 * (vin - vout) / vin, 3) if vin else 0.0,
        v_1d_held=round(v1d_net), mb_terms={k: round(v) for k, v in mb.items()},
        **_structure_stats(Path(h5path).with_suffix(".rpt")),
        **(sewer.stats(Path(h5path).with_suffix(".rpt"), Path(h5path).with_name("sewer_nodes.json")) if q.get("sewer") else {}),
        runtime_s=round(runtime_s, 1), cells=meta["n_cells"],
        est_max=round(max(max(forcing["estuary"]["up"]), max(forcing["estuary"]["down"])), 2) if forcing.get("estuary") else None,
        est_min=round(min(min(forcing["estuary"]["up"]), min(forcing["estuary"]["down"])), 2) if forcing.get("estuary") else None,
        est_river=forcing["estuary"]["river"] if forcing.get("estuary") else None)
    tf = Transformer.from_crs(27700, 4326, always_xy=True)
    lon, lat = tf.transform(X0 + (io_ + .5) * H, Y1 - (jo + .5) * H)
    bl = None
    if q["burst"]:
        j, i = q["burst_cell"]; lon_b, lat_b = tf.transform(X0 + (i + .5) * H, Y1 - (j + .5) * H); bl = [lat_b, lon_b]
    cells = dict(depth_u8=cells_u8, maxd_u8=q8(MD, 0.02), maxv_u8=q8(MV, 0.05), hazard_u8=q8(HZC.astype(float), 1.0), NX=NX, NY=NY)
    try:                                                    # full time series for GIS and analysis (CF NetCDF)
        _write_cf(Path(h5path).with_name("time_series.nc"), A, q, f, grid, minutes, MD, MV, HZC)
    except Exception as ex:
        print("time series not written:", ex)
    return dict(area=A["id"], params=q, stats=stats, burst_at=bl, frames=frames, cells=cells, minutes=minutes.tolist(),
                qout=qout.round(2).tolist(), maxd=_png(_rgba_depth(warp(MD))), speed=_png(_rgba_speed(warp(MV), warp(MD))),
                hazard=_png(_rgba_hazard(warp(HZC.astype(float)))),
                bounds=bounds, deepest_off=[lat, lon], forcing=forcing)
