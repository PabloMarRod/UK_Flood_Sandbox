"""Check that this computer can reach every data service UK Flood Sandbox uses.

    python diagnose.py            (from the app folder, ideally with the app's environment:
                                   .venv\\Scripts\\python diagnose.py  on Windows,  .venv/bin/python diagnose.py  elsewhere)

Prints one line per service. Copy the output if you need help.
"""
import json, platform, sys, time
import net
import requests

TESTS = [
    ("OpenStreetMap Overpass (overpass-api.de)", "POST", "https://overpass-api.de/api/interpreter",
     {"data": "[out:json];node(54.97,-1.61,54.971,-1.609);out 1;"}),
    ("OpenStreetMap Overpass (kumi.systems)", "POST", "https://overpass.kumi.systems/api/interpreter",
     {"data": "[out:json];node(54.97,-1.61,54.971,-1.609);out 1;"}),
    ("OpenStreetMap main API", "GET", "https://api.openstreetmap.org/api/0.6/map?bbox=-1.6100,54.9700,-1.6090,54.9705", None),
    ("Environment Agency LIDAR (terrain, England)", "GET",
     "https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs?service=WCS&version=2.0.1&request=GetCapabilities", None),
    ("Environment Agency flood monitoring (tides)", "GET",
     "https://environment.data.gov.uk/flood-monitoring/id/measures/E70939-level-tidal_level-Mean-15_min-mAOD/readings?_limit=1", None),
    ("Environment Agency hydrology (river flows)", "GET",
     "https://environment.data.gov.uk/hydrology/id/stations.json?_limit=1", None),
    ("Environment Agency flood defences", "GET",
     "https://environment.data.gov.uk/spatialdata/spatial-flood-defences-including-standardised-attributes/ogc/features/v1/collections?f=json", None),
    ("Welsh Government / NRW (DataMapWales)", "GET", "https://datamap.gov.wales/geoserver/ows?service=WFS&request=GetCapabilities", None),
    ("Scottish LiDAR archive", "GET", "https://srsp-open-data.s3.eu-west-2.amazonaws.com/?list-type=2&max-keys=1", None),
    ("SEPA river flows (Scotland)", "GET", "https://timeseries.sepa.org.uk/KiWIS/KiWIS?service=kisters&type=queryServices&datasource=0&request=getStationList&format=json&station_name=Craigforth", None),
    ("Northern Ireland terrain (OSNI)", "HEAD", "https://docs.spatialni.gov.uk/OpenData/OSNI_OpenData_10m_DTM/OSNI_10M_DTM_Sheets_1-50.zip", None),
    ("Map tiles (Esri basemap)", "GET", "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/10/330/505", None),
]

print(f"UK Flood Sandbox diagnostics  |  Python {platform.python_version()}  |  {platform.system()} {platform.release()}")
print(f"Operating-system certificates in use (truststore): {'yes' if net.TRUSTSTORE else 'NO (pip install truststore)'}")
print("-" * 100)
ssl_problems = 0
for name, method, url, data in TESTS:
    t = time.time()
    try:
        r = requests.request(method, url, data=data, timeout=(15, 60))
        ok = r.status_code in (200, 206)
        msg = f"HTTP {r.status_code}" + ("" if ok else f"  {r.text[:100].strip()}")
    except Exception as ex:
        ok = False; msg = f"{type(ex).__name__}: {str(ex)[:150]}"
        if "SSL" in msg or "CERTIFICATE" in msg.upper(): ssl_problems += 1
    print(f"{'OK  ' if ok else 'FAIL'}  {name:46s} {time.time() - t:5.1f}s  {msg}")
print("-" * 100)
if ssl_problems:
    print("Some services failed with certificate (SSL) errors: your network probably inspects secure connections.")
    print("Fix: install 'truststore' in the app's environment (the start script does this) so Python uses the")
    print("operating system's certificates, or ask IT for the network's root certificate.")
else:
    print("If only the Overpass lines fail, those servers are busy or down; the app falls back to the main API.")
