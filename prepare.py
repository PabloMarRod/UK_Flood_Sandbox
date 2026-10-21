"""Prepare a drawn area in its own process: python prepare.py <area_id>"""
import net  # noqa: F401  (network setup: user agent, certificates)
import json, sys, time, traceback
import safeio
from pathlib import Path
import areas

A = areas.load(sys.argv[1])
status = Path(A["dir"]) / "status.json"

def put(**kw):
    kw["t"] = time.time()
    safeio.write_json(status, kw)

try:
    t0 = time.time()
    areas.fetch_dtm(A, lambda m: put(state="running", step=m))
    areas.fetch_osm(A, lambda m: put(state="running", step=m))
    areas.fetch_defences(A, lambda m: put(state="running", step=m))
    put(state="running", step="Building the mesh")
    meta = areas.build_mesh(A)
    put(state="running", step="Preparing map layers")
    areas.static_layers(A)
    try:
        import scene
        scene.build_scene(A, lambda m: put(state="running", step=m))
    except Exception:
        pass                      # the 3D scene is built on first view instead
    removed = areas.prune(keep_id=A["id"])       # one drawn area at a time; results in runs/ are kept
    put(state="done", removed=removed, cells=meta["n_cells"], outflow_edges=len(meta["outflow"]), culverts=meta["culverts"],
        seconds=round(time.time() - t0, 1))
except Exception as e:
    put(state="error", error=str(e), trace=traceback.format_exc()[-2000:])
