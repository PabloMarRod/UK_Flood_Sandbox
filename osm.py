import requests, time, json, sys
MIRRORS=["https://overpass-api.de/api/interpreter","https://overpass.kumi.systems/api/interpreter",
         "https://maps.mail.ru/osm/tools/overpass/api/interpreter","https://overpass.private.coffee/api/interpreter",
         "https://overpass.osm.jp/api/interpreter"]
LAST_ERRORS = []


def overpass(q, tries=6):
    LAST_ERRORS.clear()
    for i in range(tries):
        for u in MIRRORS:
            try:
                r=requests.post(u,data={'data':q},timeout=(10,90))   # fail fast when a server is down
                if r.status_code==200 and r.text.lstrip().startswith('{'):
                    return r.json()
                LAST_ERRORS.append(f"{u.split('/')[2]}: HTTP {r.status_code}")
            except Exception as e:
                LAST_ERRORS.append(f"{u.split('/')[2]}: {type(e).__name__}: {str(e)[:120]}")
        time.sleep(5)
    raise RuntimeError('overpass failed')


class TooDense(Exception):
    pass


def osm_api_ways(s, w, n, e, keys=("building", "highway", "landuse", "leisure", "natural", "waterway", "amenity"), rel_cache=None):
    """Fallback when Overpass is unavailable: the main OpenStreetMap API map call (small areas only),
    converted to the Overpass 'out geom' layout used by the app (ways with tags and geometry)."""
    import xml.etree.ElementTree as ET
    r, why = None, ""
    for i in range(4):
        try:
            r = requests.get("https://api.openstreetmap.org/api/0.6/map", params={"bbox": f"{w},{s},{e},{n}"}, timeout=300)
            if r.status_code == 200: break
            if r.status_code == 400 and "too many nodes" in r.text.lower():
                raise TooDense()
            why = f"HTTP {r.status_code}: {r.text[:120].strip()}"
            if r.status_code in (429, 509): time.sleep(20)
        except TooDense:
            raise
        except Exception as ex:
            why = f"{type(ex).__name__}: {str(ex)[:160]}"
        time.sleep(5)
    if r is None or r.status_code != 200:
        over = "; ".join(LAST_ERRORS[:3])
        raise RuntimeError(f"OpenStreetMap is unavailable. Main API: {why or 'no response'}. Overpass: {over or 'not tried'}. "
                           "If both mention SSL or certificates, your network inspects secure connections: run "
                           "'python diagnose.py' and see the README.")
    root = ET.fromstring(r.content)
    nodes = {nd.get("id"): (float(nd.get("lat")), float(nd.get("lon"))) for nd in root.iter("node")}
    out = []
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.iter("tag")}
        if not any(k in tags for k in keys): continue
        if "amenity" in tags and tags["amenity"] != "parking" and not any(k in tags for k in keys if k != "amenity"): continue
        geom = [nodes[nd.get("ref")] for nd in way.iter("nd") if nd.get("ref") in nodes]
        if len(geom) >= 2:
            out.append({"type": "way", "id": int(way.get("id")), "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in geom]})
    # key sites mapped as single points (schools, hospitals, care homes, stations ...)
    SITE = {"school", "kindergarten", "college", "university", "hospital", "clinic", "doctors", "nursing_home",
            "social_facility", "fire_station", "police", "townhall"}
    for nd in root.iter("node"):
        tags = {t.get("k"): t.get("v") for t in nd.iter("tag")}
        if tags.get("amenity") in SITE or tags.get("railway") == "station":
            out.append({"type": "node", "id": int(nd.get("id")), "lat": float(nd.get("lat")), "lon": float(nd.get("lon")), "tags": tags})
    # multipolygon relations (e.g. rivers and docks mapped as water areas): outer member ways with geometry
    wgeom = {}
    for way in root.iter("way"):
        g = [nodes[nd.get("ref")] for nd in way.iter("nd") if nd.get("ref") in nodes]
        if len(g) >= 2: wgeom[way.get("id")] = g
    for rel in root.iter("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.iter("tag")}
        if tags.get("type") != "multipolygon" or not any(k in tags for k in keys): continue
        rnodes, rways = dict(nodes), dict(wgeom)
        if rel_cache is not None and rel.get("id") in rel_cache:
            continue                                 # already fetched in full from another tile
        if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            try:            # water areas often extend beyond the box: fetch the whole relation so its rings close
                rr = requests.get(f"https://api.openstreetmap.org/api/0.6/relation/{rel.get('id')}/full", timeout=120)
                if rr.status_code == 200:
                    full = ET.fromstring(rr.content)
                    rnodes.update({nd.get("id"): (float(nd.get("lat")), float(nd.get("lon"))) for nd in full.iter("node")})
                    for wy in full.iter("way"):
                        g = [rnodes[n.get("ref")] for n in wy.iter("nd") if n.get("ref") in rnodes]
                        if len(g) >= 2: rways[wy.get("id")] = g
                time.sleep(0.5)
            except Exception:
                pass
        mem = [{"type": "way", "role": m.get("role"), "geometry": [{"lat": a, "lon": b} for a, b in rways[m.get("ref")]]}
               for m in rel.iter("member") if m.get("type") == "way" and m.get("ref") in rways and m.get("role") == "outer"]
        if mem:
            out.append({"type": "relation", "id": int(rel.get("id")), "tags": tags, "members": mem})
            if rel_cache is not None: rel_cache.add(rel.get("id"))
    return {"elements": out}


def osm_api_tiled(s, w, n, e, keys=("building", "highway", "landuse", "leisure", "natural", "waterway", "amenity"),
                  status=lambda m: None, step=0.01, min_step=0.0015):
    """The main OpenStreetMap API in small tiles (it refuses areas with more than 50 000 points); dense tiles are
    split further. Elements on tile edges are merged once."""
    seen, out, rel_cache = set(), [], set()
    todo, done = [], 0
    la = s
    while la < n:
        lo = w
        while lo < e:
            todo.append((la, lo, min(la + step, n), min(lo + step, e))); lo += step
        la += step
    total = len(todo)
    while todo:
        a, b, c, d = todo.pop(0)
        try:
            res = osm_api_ways(a, b, c, d, keys, rel_cache)
        except TooDense:
            if c - a <= min_step:
                raise RuntimeError("OpenStreetMap data here is too dense to download through the main API")
            ma, mb = (a + c) / 2, (b + d) / 2
            todo[:0] = [(a, b, ma, mb), (a, mb, ma, d), (ma, b, c, mb), (ma, mb, c, d)]; total += 3
            continue
        for el in res["elements"]:
            k = (el["type"], el["id"])
            if k not in seen: seen.add(k); out.append(el)
        done += 1
        status(f"Downloading buildings and land cover (OpenStreetMap API, {done} of {total})")
        time.sleep(0.3)
    return {"elements": out}
