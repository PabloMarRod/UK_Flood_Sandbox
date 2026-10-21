"""Fetch open inputs for build.py: EA LIDAR DTM 1 m (WCS), OSM features (Overpass), EA Hydrology 15-min series."""
import json, requests
from pyproj import Transformer
from osm import overpass
E0, E1, N0, N1 = 425800, 426900, 565100, 566400
wcs = "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs"
cov = "13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m"
r = requests.get(wcs, params=[("service","WCS"),("version","2.0.1"),("request","GetCoverage"),("CoverageId",cov),
    ("format","image/tiff"),("subset",f"E({E0},{E1})"),("subset",f"N({N0},{N1})")], timeout=300)
open("areas/jesmond-vale/data/dtm.tif","wb").write(r.content)
t = Transformer.from_crs(27700, 4326, always_xy=True)
lo0, la0 = t.transform(E0, N0); lo1, la1 = t.transform(E1, N1); bb = f"{la0:.5f},{lo0:.5f},{la1:.5f},{lo1:.5f}"
q = f"""[out:json][timeout:120];(way["building"]({bb});way["highway"]({bb});way["landuse"]({bb});way["leisure"]({bb});
way["natural"]({bb});way["waterway"]({bb});way["amenity"="parking"]({bb}););out geom;"""
json.dump(overpass(q), open("areas/jesmond-vale/data/osm.json","w"))
H = "https://environment.data.gov.uk/hydrology/id/measures/"
ids = {"flow":"438e6059-f150-4dea-b17a-b21cae02ded7-flow-i-900-m3s-qualified",      # Crag Hall
       "rain":"d64d746d-ccaa-4380-8bb9-6ae619b2b97e-rainfall-t-900-mm-qualified"}   # Newbiggin Hall
obs = {}
for k, m in ids.items():
    it = requests.get(H+m+"/readings.json", params={"mineq-date":"2012-06-28","maxeq-date":"2012-06-29","_limit":500}, timeout=120).json()["items"]
    obs[k] = {x["dateTime"][:16]: x["value"] for x in it}
json.dump(obs, open("data/obs_2012-06-28.json","w"))
