"""Ordnance Survey open data (Great Britain): OS Open Rivers centrelines and OS OpenMap Local water areas.
Used to build estuary geometry where OpenStreetMap is incomplete or unavailable."""
import json
from pathlib import Path
import numpy as np


def _read(path, layer=None, bbox=None, where=None):
    import pyogrio.raw, shapely
    meta, fids, geom, fields = pyogrio.raw.read(str(path), layer=layer, bbox=bbox, where=where)
    names = list(meta["fields"])
    return shapely.from_wkb(geom), {n: fields[i] for i, n in enumerate(names)}


def river_lines(gpkg, names, bbox, unnamed_tidal=True):
    """Open Rivers watercourse links with the given names (plus unnamed tidal-river links, which fill
    gaps in named estuary channels), oriented downstream."""
    from shapely.geometry import LineString
    esc = lambda n: n.replace("'", "''")
    q = " OR ".join(f"watercourse_name = '{esc(n)}' OR watercourse_name_alternative = '{esc(n)}'" for n in names)
    if unnamed_tidal:
        q = f"({q}) OR form = 'tidalRiver'"
    geoms, f = _read(gpkg, "watercourse_link", bbox, q)
    out = []
    for g, d in zip(geoms, f["flow_direction"]):
        g = LineString([c[:2] for c in g.coords])
        if str(d).lower().startswith("in opposite"): g = LineString(list(g.coords)[::-1])
        out.append(g)
    return out


def water_polygons(shp_paths, bbox):
    from shapely.ops import unary_union
    polys = []
    for p in shp_paths:
        geoms, _ = _read(p, bbox=bbox)
        polys += [g.buffer(0) for g in geoms if g is not None]
    return unary_union(polys)


def save_geojson(path, geoms):
    from shapely.geometry import mapping
    feats = [{"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in (geoms if isinstance(geoms, list) else [geoms])]
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "crs": "EPSG:27700", "features": feats}))
