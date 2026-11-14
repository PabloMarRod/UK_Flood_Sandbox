"""Getting results out: a one-page PDF report and a GIS package (GeoTIFFs + GeoPackage) for a run.
Both are saved in the run's folder."""
import base64, io, json, math, shutil, time, zipfile
from pathlib import Path
import numpy as np

import areas, core

HAZ_NAMES = ["none", "low", "moderate", "significant", "extreme"]


def target_crs(epsg, A):
    """The export coordinate system: (epsg, pyproj CRS). The model runs in British National Grid; other systems are
    for the GIS package only. Raises ValueError for unknown codes, systems that aren't 2D map coordinates, or ones
    whose area of use doesn't cover the area."""
    from pyproj import CRS, Transformer
    try:
        n = int(str(epsg).strip().upper().removeprefix("EPSG:"))
    except ValueError:
        raise ValueError(f"'{epsg}' isn't an EPSG code: use a number such as 27700")
    try:
        crs = CRS.from_epsg(n)
    except Exception:
        raise ValueError(f"EPSG:{n} isn't a known coordinate system")
    if crs.is_compound:
        crs = crs.sub_crs_list[0]
    if not (crs.is_projected or crs.is_geographic) or len(crs.axis_info) != 2:
        raise ValueError(f"EPSG:{n} ({crs.name}) isn't a 2D map coordinate system")
    au = crs.area_of_use
    if au:
        lon, lat = Transformer.from_crs(27700, 4326, always_xy=True).transform(*_centre(A))
        if not (au.west - 1e-6 <= lon <= au.east + 1e-6 and au.south - 1e-6 <= lat <= au.north + 1e-6):
            raise ValueError(f"EPSG:{n} ({crs.name}) isn't meant for this location: its area of use is {au.name}")
    return crs.to_epsg() or n, crs


def _centre(A):
    from shapely.geometry import Polygon
    c = Polygon(A["poly_bng"]).centroid
    return c.x, c.y


def _load(run):
    res = json.loads((run / "result.json").read_text())
    q = json.loads((run / "params.json").read_text()) if (run / "params.json").exists() else res.get("params", {})
    A = areas.load(res["area"])
    meta_f = Path(A["dir"]) / "cache" / "bld_meta.json"
    if not meta_f.exists() or '"wkt"' not in meta_f.read_text()[:4000]:
        areas.build_mesh(A)                         # older areas: store building outlines once
    return res, q, A


def _grids(res, A):
    c = res["cells"]; NX, NY = c["NX"], c["NY"]
    u8 = lambda k, s: np.frombuffer(base64.b64decode(c[k]), np.uint8).reshape(NY, NX).astype(np.float32) * s
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz"); keep = m["keep"]
    md, mv = u8("maxd_u8", 0.02), u8("maxv_u8", 0.05)
    hz = u8("hazard_u8", 1.0) if "hazard_u8" in c else np.zeros_like(md)
    return md, mv, hz, keep, m


def _buildings(A, md, keep):
    """Per building: footprint, type, deepest water beside it, estimated damage (JRC, as in the results)."""
    from scipy import ndimage
    from shapely import wkt as _wkt
    cache = Path(A["dir"]) / "cache"
    bid = np.load(cache / "bld_ids.npy"); meta = json.loads((cache / "bld_meta.json").read_text())
    around = ndimage.maximum_filter(np.where(keep, md, 0), size=3)
    ids = np.unique(bid[bid > 0]); depth = dict(zip(ids.tolist(), np.asarray(ndimage.maximum(around, labels=bid, index=ids)).tolist()))
    rows = []
    for k, b in enumerate(meta, 1):
        if "wkt" not in b: continue
        dv = float(depth.get(k, 0.0)); t = b.get("type", "residential")
        dmg = float(np.interp(min(dv, 6.0), core.JRC_DEPTH, core.JRC_EUROPE[t])) * core.JRC_UK_MAX[t] * b["area"] if dv > 0.02 and k in depth else 0.0
        rows.append(dict(geom=_wkt.loads(b["wkt"]), type=t, name=b.get("name") or "", area_m2=b["area"],
                         modelled=k in depth, depth_beside_m=round(dv, 2), damage_eur=round(dmg), affected=dv > 0.3))
    return rows


def _roads_sites(A, md, keep):
    from shapely.geometry import LineString, Point, Polygon
    from pyproj import Transformer
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    tf = Transformer.from_crs(4326, 27700, always_xy=True).transform
    osm = json.loads((Path(A["dir"]) / "data" / "osm.json").read_text())["elements"]
    mdk = np.where(keep, md, np.nan); roads, sites = [], []
    cell = lambda x, y: (int((Y1 - y) // H), int((x - X0) // H))
    for e in osm:
        tg, g = e.get("tags", {}), e.get("geometry")
        if e["type"] == "way" and g and tg.get("highway") in areas.ROADW and not tg.get("bridge") and not tg.get("tunnel"):
            xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g]); ln = LineString(list(zip(xs, ys)))
            ds = []
            for s in np.arange(1.25, ln.length, 2.5):
                p = ln.interpolate(s); j, i = cell(p.x, p.y)
                if 0 <= j < NY and 0 <= i < NX and np.isfinite(mdk[j, i]): ds.append(float(mdk[j, i]))
            if ds and max(ds) > 0.1:
                roads.append(dict(geom=ln, name=tg.get("name") or tg.get("ref") or "", highway=tg["highway"],
                                  max_depth_m=round(max(ds), 2), flooded_over_0_3_m=round(2.5 * sum(d > 0.3 for d in ds))))
        kind = tg.get("amenity") if tg.get("amenity") in core.SITE_KINDS else ("station" if tg.get("railway") == "station" else None)
        if kind:
            if e["type"] == "node": x, y = tf(e["lon"], e["lat"])
            elif g:
                xs, ys = tf([p["lon"] for p in g], [p["lat"] for p in g])
                c_ = Polygon(list(zip(xs, ys))).centroid if len(g) > 3 else Point(xs[0], ys[0]); x, y = c_.x, c_.y
            else: continue
            j, i = cell(x, y); r_ = int(math.ceil(15 / H))
            if not (0 <= j < NY and 0 <= i < NX): continue
            win = mdk[max(j - r_, 0):j + r_ + 1, max(i - r_, 0):i + r_ + 1]
            dv = float(np.nanmax(win)) if np.isfinite(win).any() else 0.0
            sites.append(dict(geom=Point(x, y), name=tg.get("name") or core.SITE_KINDS[kind], kind=core.SITE_KINDS[kind],
                              depth_m=round(dv, 2), affected=dv > 0.3))
    return roads, sites


def _write_layer(path, layer, rows, geom_type, epsg=27700):
    import shapely, pyogrio.raw
    if not rows: return
    keys = [k for k in rows[0] if k != "geom"]
    fields = [np.array([r[k] for r in rows], dtype=object if isinstance(rows[0][k], str) else None) for k in keys]
    geoms = [r["geom"] for r in rows]
    if epsg != 27700:
        from pyproj import Transformer
        tf = Transformer.from_crs(27700, epsg, always_xy=True)
        geoms = shapely.transform(geoms, lambda xy: np.column_stack(tf.transform(xy[:, 0], xy[:, 1])))
    pyogrio.raw.write(str(path), shapely.to_wkb(geoms), field_data=fields, fields=keys, layer=layer,
                      driver="GPKG", crs=f"EPSG:{epsg}", geometry_type=geom_type, append=Path(path).exists())


def _write_grid(path, arr, dtype, nodata, A, epsg):
    """A model grid as GeoTIFF. In another system each output cell takes the largest value of the model cells it
    covers, so depths, speeds and hazard classes stay modelled values and peaks are kept."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import calculate_default_transform, reproject, Resampling
    H, X0, Y1 = A["H"], A["X0"], A["Y1"]; ny, nx = arr.shape
    src_t = from_origin(X0, Y1, H, H); arr = arr.astype(dtype)
    if epsg == 27700:
        out, t, crs = arr, src_t, "EPSG:27700"
    else:
        crs = f"EPSG:{epsg}"
        t, w, h = calculate_default_transform("EPSG:27700", crs, nx, ny, X0, Y1 - ny * H, X0 + nx * H, Y1)
        out = np.full((h, w), nodata, dtype)
        reproject(arr, out, src_transform=src_t, src_crs="EPSG:27700", dst_transform=t, dst_crs=crs,
                  resampling=Resampling.max, src_nodata=nodata, dst_nodata=nodata)
    with rasterio.open(path, "w", driver="GTiff", height=out.shape[0], width=out.shape[1], count=1, dtype=dtype,
                       crs=crs, transform=t, nodata=nodata, compress="deflate") as dst:
        dst.write(out, 1)


def gis(run, epsg=27700):
    from shapely.geometry import Polygon, LineString
    res, q, A = _load(run)
    epsg, crs = target_crs(epsg, A)
    md, mv, hz, keep, m = _grids(res, A)
    H = A["H"]
    work = run / "gis_export"
    if work.exists(): shutil.rmtree(work)
    work.mkdir()
    for name, arr, dtype, nod in (("max_depth_m", np.where(keep, md, np.nan), "float32", np.nan),
                                  ("max_speed_m_s", np.where(keep, mv, np.nan), "float32", np.nan),
                                  ("hazard_class", np.where(keep, hz, 255), "uint8", 255)):
        _write_grid(work / f"{name}.tif", arr, dtype, nod, A, epsg)
    gp = work / "flood_results.gpkg"
    lay = lambda name, rows, kind: _write_layer(gp, name, rows, kind, epsg)
    lay("area_outline", [dict(geom=Polygon(A["poly_bng"]), area=A["name"], run=res.get("run", run.name))], "Polygon")
    lay("buildings", _buildings(A, md, keep), "Polygon")
    roads, sites = _roads_sites(A, md, keep)
    lay("roads_flooded", roads, "LineString")
    lay("key_sites", sites, "Point")
    ms = q.get("measures") or []
    attrs = lambda mm: dict(name=mm["name"], type=mm["type"], height_m=float(mm.get("height", 0) or 0), depth_m=float(mm.get("depth", 0) or 0),
                            width_m=float(mm.get("width", 0) or 0), cost=float(mm.get("cost") or 0))
    lay("defences_lines", [dict(geom=LineString(mm["pts"]), **attrs(mm)) for mm in ms if mm["type"] not in core.AREA_TYPES], "LineString")
    lay("defences_areas", [dict(geom=Polygon(mm["pts"]), **attrs(mm)) for mm in ms if mm["type"] in core.AREA_TYPES], "Polygon")
    coords = (f"British National Grid (EPSG:27700); grids at the model's {H} m cells." if epsg == 27700 else
              f"{crs.name} (EPSG:{epsg}), converted from the model's British National Grid. Grids are reprojected\n"
              f"from the model's {H} m cells: each output cell holds the largest value of the model cells it covers. time_series.nc stays in British National Grid.")
    (work / "README.txt").write_text(f"""UK Flood Sandbox: GIS export
Area: {A['name']}    Run: {res.get('run', run.name)}    Exported: {time.strftime('%Y-%m-%d %H:%M')}
Coordinates: {coords}

max_depth_m.tif     deepest water during the run, metres (blank outside the model and under buildings)
max_speed_m_s.tif   fastest water, m/s
hazard_class.tif    hazard to people (Defra/EA FD2320): 0 none, 1 low, 2 moderate, 3 significant, 4 extreme
time_series.nc      CF-1.8 NetCDF on the same grid: depth, water_level and speed at every saved frame (time in
                    minutes from the start), plus max_depth, max_speed, hazard_class and ground_level.
                    QGIS: drag in, pick a variable, use the Temporal Controller. ArcGIS Pro: Make Multidimensional
                    Raster Layer. Python: xarray.open_dataset("time_series.nc"). Missing value -9999.
flood_results.gpkg  area_outline; buildings (OpenStreetMap footprints: type, deepest water beside the building,
                    estimated damage in EUR at 2010 prices from the JRC European curves and UK maximum damage values,
                    affected = water over 0.3 m beside it; 'modelled' false for buildings smaller than half a cell);
                    roads_flooded (roads with water over 0.1 m: max depth, length over 0.3 m); key_sites (schools,
                    hospitals, care homes, stations ...: deepest water within 15 m); defences_lines / defences_areas.

Sources: terrain from national LiDAR; buildings, roads and sites (c) OpenStreetMap contributors (ODbL); damage
functions Huizinga, de Moel and Szewczyk (2017) doi:10.2760/16510. Indicative results from a simplified model.
""")
    if (run / "time_series.nc").exists():
        shutil.copy(run / "time_series.nc", work / "time_series.nc")
    out = run / ("gis_export.zip" if epsg == 27700 else f"gis_export_epsg{epsg}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(work.iterdir()): z.write(f, f"{run.name}_gis/{f.name}")
    for f in work.iterdir(): f.unlink()
    work.rmdir()
    return out


def _map_image(res, A, width=1100):
    """Maximum depth over terrain shading and the outline, without basemap tiles (as in the map video)."""
    from PIL import Image, ImageDraw
    st = json.loads((Path(A["dir"]) / "cache" / "static_layers.json").read_text())
    merc = lambda la: math.log(math.tan(math.pi / 4 + math.radians(la) / 2))
    pts = st["outline"]; la = [p[0] for p in pts]; lo = [p[1] for p in pts]
    x0, x1, y0, y1 = min(lo), max(lo), merc(min(la)), merc(max(la))
    sx = (width - 40) / (x1 - x0); hgt = int((y1 - y0) * 180 / math.pi * sx) + 40
    X = lambda g: 20 + (g - x0) * sx; Y = lambda l: 20 + (y1 - merc(l)) * 180 / math.pi * sx
    img = Image.new("RGBA", (width, hgt), (238, 241, 240, 255))
    def paste(data_url, b):
        im = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))).convert("RGBA")
        bx0, by0, bx1, by1 = X(b[0][1]), Y(b[1][0]), X(b[1][1]), Y(b[0][0])
        im = im.resize((max(1, int(bx1 - bx0)), max(1, int(by1 - by0))))
        img.alpha_composite(im, (int(bx0), int(by0)))
    paste(st["shade"], st["shadeBounds"]); paste(res["maxd"], res["bounds"])
    d = ImageDraw.Draw(img); d.line([(X(p[1]), Y(p[0])) for p in pts] + [(X(pts[0][1]), Y(pts[0][0]))], fill=(22, 48, 58, 255), width=2)
    b = io.BytesIO(); img.convert("RGB").save(b, "PNG"); b.seek(0)
    return b, width, hgt


def report(run, description="", snapshot=None, ref=None, factor=1.0):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    res, q, A = _load(run); s = res["stats"]
    b = None
    if ref:
        rf = run.parent / ref / "result.json"
        if rf.exists(): b = json.loads(rf.read_text())["stats"]
    out = run / "report.pdf"
    c = canvas.Canvas(str(out), pagesize=A4); W, Hh = A4
    ink, muted = colors.HexColor("#16303a"), colors.HexColor("#5d7179")
    c.setFillColor(ink); c.setFont("Helvetica-Bold", 16); c.drawString(15 * mm, Hh - 18 * mm, f"Flood scenario: {A['name']}")
    c.setFont("Helvetica", 9); c.setFillColor(muted)
    c.drawString(15 * mm, Hh - 24 * mm, f"UK Flood Sandbox  |  run {res.get('run', run.name)}  |  {time.strftime('%d %B %Y')}  |  {A.get('area_km2', '')} km2, {A['H']} m cells")
    c.setFillColor(ink); c.setFont("Helvetica", 10)
    text = c.beginText(15 * mm, Hh - 32 * mm); text.setFont("Helvetica", 10)
    words, line = (description or "").split(), ""
    for w_ in words:
        if c.stringWidth(line + " " + w_, "Helvetica", 10) > W - 30 * mm: text.textLine(line); line = w_
        else: line = (line + " " + w_).strip()
    if line: text.textLine(line)
    c.drawText(text)
    # images: map (left) and 3D (right)
    top = Hh - 45 * mm; img_h = 80 * mm; col_w = (W - 35 * mm) / 2
    mb, mw, mh = _map_image(res, A)
    sc = min(col_w / mw, img_h / mh); c.drawImage(ImageReader(mb), 15 * mm, top - mh * sc, mw * sc, mh * sc)
    c.setFont("Helvetica", 7.5); c.setFillColor(muted)
    c.drawString(15 * mm, top - mh * sc - 4 * mm, "Maximum water depth (light blue shallow to dark blue 1.8 m or more) over terrain shading")
    if snapshot:
        from PIL import Image
        sn = Image.open(io.BytesIO(base64.b64decode(snapshot.split(",", 1)[1]))).convert("RGBA")
        bb = sn.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()      # crop to the model
        if bb:
            pad = 20; bb = (max(bb[0] - pad, 0), max(bb[1] - pad, 0), min(bb[2] + pad, sn.width), min(bb[3] + pad, sn.height)); sn = sn.crop(bb)
        bg = Image.new("RGBA", sn.size, (238, 241, 240, 255)); bg.alpha_composite(sn)
        buf = io.BytesIO(); bg.convert("RGB").save(buf, "PNG"); buf.seek(0)
        im = ImageReader(buf); iw, ih = im.getSize()
        sc2 = min(col_w / iw, img_h / ih); c.drawImage(im, 20 * mm + col_w, top - ih * sc2, iw * sc2, ih * sc2)
        c.drawString(20 * mm + col_w, top - ih * sc2 - 4 * mm, "3D view, as shown in the app")
    # key results
    y = top - img_h - 14 * mm
    money = lambda v: "{:,.0f}".format(v * factor) + ("" if factor != 1 else " EUR")
    rows = [("", "This run", "Compared run" if b else "")]
    def add(label, k, fmt):
        if s.get(k) is None: return
        rows.append((label, fmt(s[k], s), fmt(b[k], b) if b and b.get(k) is not None else ""))
    add("Rain", "rain_mm", lambda v, _: f"{v} mm")
    add("Rain from roofs", "v_roof", lambda v, _: "{:,} m3".format(v))
    add("Land wet deeper than 0.3 m", "wet30_ha", lambda v, _: f"{v} ha")
    add("Deepest point", "max_depth_off", lambda v, _: f"{v} m")
    add("Hazard to people: significant or extreme", "hazard3_ha", lambda v, z: f"{round(v + z.get('hazard4_ha', 0), 2)} ha")
    add("Buildings with water over 0.3 m beside them", "buildings_affected", lambda v, z: f"{v} of {z.get('buildings_total')}")
    add("Roads impassable to cars (over 0.3 m)", "roads_over_0_3_km", lambda v, z: f"{v} of {z.get('roads_km')} km")
    add("Key sites with water over 0.3 m", "sites_affected", lambda v, z: f"{v} of {z.get('sites_total')}")
    add("Estimated damage to buildings", "damage_eur", lambda v, _: money(v))
    add("Your defences in the run", "measures", lambda v, _: str(v))
    if b and s.get("damage_eur") is not None and b.get("damage_eur") is not None:
        rows.append(("Damage avoided", money(b["damage_eur"] - s["damage_eur"]), ""))
        if s.get("measures_cost"):
            rows.append(("Damage avoided per unit of cost (this storm)", f"{(b['damage_eur'] - s['damage_eur']) * factor / s['measures_cost']:.2f}", ""))
    c.setFont("Helvetica-Bold", 11); c.setFillColor(ink); c.drawString(15 * mm, y, "Key results"); y -= 6 * mm
    for k_, (a_, b_, c_) in enumerate(rows):
        c.setFont("Helvetica-Bold" if k_ == 0 else "Helvetica", 9.5)
        c.drawString(15 * mm, y, a_); c.drawRightString(140 * mm, y, b_); c.drawRightString(W - 15 * mm, y, c_); y -= 5.2 * mm
    if s.get("sites_list"):
        y -= 2 * mm; c.setFont("Helvetica", 8.5)
        c.drawString(15 * mm, y, "Key sites affected: " + "; ".join(f"{x['name']} ({x['depth']} m)" for x in s["sites_list"][:6])[:170]); y -= 5 * mm
    if q.get("measures"):
        c.setFont("Helvetica", 8.5)
        c.drawString(15 * mm, y, "Defences: " + "; ".join(m_["name"] for m_ in q["measures"])[:170]); y -= 5 * mm
    # sources and limits
    c.setFont("Helvetica", 7); c.setFillColor(muted)
    notes = ["Model: OpenSWMM 6 two-dimensional surface model on national LiDAR; no sewers; roofs drain to the ground beside them. "
             "Indicative results for exploring scenarios, not for decisions about individual properties.",
             "Hazard: Defra/EA FD2320. Damage: Huizinga, de Moel and Szewczyk (2017) Global flood depth-damage functions, doi:10.2760/16510; "
             "European curves, UK maximum values, EUR at 2010 prices" + (f", multiplied by {factor}" if factor != 1 else "") + ".",
             "Map data (c) OpenStreetMap contributors. Terrain: Environment Agency, Welsh Government, Scottish Government or OSNI LiDAR (Open Government Licence)."]
    yy = 30 * mm
    for n_ in notes:
        words, line = n_.split(), ""
        for w_ in words + [None]:
            if w_ is None or c.stringWidth(line + " " + w_, "Helvetica", 7) > W - 30 * mm:
                c.drawString(15 * mm, yy, line); yy -= 3.3 * mm; line = w_ or ""
            else:
                line = (line + " " + w_).strip()
        yy -= 1.2 * mm
    c.showPage(); c.save()
    return out
