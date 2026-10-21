"""Run one scenario in its own process: python worker.py <run_dir>"""
import os as _os, json as _json
from pathlib import Path as _P
try:                                            # processor threads chosen by the speed test (before the engine loads)
    _cal = _json.loads((_P(__file__).parent / "runs" / "calibration.json").read_text())
    if _cal.get("threads"): _os.environ.setdefault("OMP_NUM_THREADS", str(_cal["threads"]))
except Exception:
    pass
import net  # noqa: F401  (network setup: user agent, certificates)
import json, sys, time, traceback
import safeio
from pathlib import Path
import areas, core

run = Path(sys.argv[1])
status = run / "status.json"

def put(**kw):
    kw["t"] = time.time()               # heartbeat, lets the server spot a stalled job
    safeio.write_json(status, kw)

try:
    q = json.loads((run / "params.json").read_text())
    A = areas.load(q["area"])
    put(state="preparing", progress=0)
    try:
        has_sites = json.loads((Path(A["dir"]) / "data" / "osm.json").read_text()).get("sites_included")
    except Exception:
        has_sites = True
    if not has_sites:                                # areas downloaded before key sites were included
        put(state="preparing", progress=0, step="Updating map data (schools, hospitals, stations)")
        try:
            areas.fetch_osm(A, lambda m: put(state="preparing", progress=0, step=m)); areas.build_mesh(A)
        except Exception:
            pass
    if "roofs" not in areas.mesh_meta(A) or not (Path(A["dir"]) / "cache" / "roof_grp.npy").exists() \
            or not (Path(A["dir"]) / "cache" / "bld_meta.json").exists():
        put(state="preparing", progress=0, step="Updating the area's mesh (roof drainage)")
        areas.build_mesh(A)
    (run / "area.json").write_text(json.dumps({k: v for k, v in A.items() if k != "dir"}))
    (run / "static_layers.json").write_text(json.dumps(areas.static_layers(A)))
    est = None
    if q.get("estuary"):
        import estuary
        est = estuary.run(q["estuary"], q, run, status=lambda m: put(state="preparing", progress=0, step=m))
    forcing = core.write_inp(A, q, run / "model.inp", "model.h5", est=est)
    import openswmm.engine as eng
    t0, last = time.time(), [0.0]
    def cb(frac):
        now = time.time()
        if now - last[0] > 0.5:
            last[0] = now
            el = now - t0
            put(state="running", progress=round(frac, 4), elapsed_s=round(el, 1),
                eta_s=round(el / frac - el, 1) if frac > 0.02 else None)
    put(state="running", progress=0)
    eng.run_with_callback(str(run / "model.inp"), str(run / "model.rpt"), str(run / "model.out"), cb)
    runtime = time.time() - t0
    core.calibrate(core.effective_cells(A), q["sim"], runtime)
    try:                                           # peak memory of this run, to refine the memory ceiling
        try:
            import resource; peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            if sys.platform == "darwin": peak /= 1024
        except ImportError:
            import psutil; peak = psutil.Process().memory_info().peak_wset / 2**20
        core.record_memory(A["n_cells"], peak)
    except Exception:
        pass
    put(state="processing", progress=1)
    res = core.postprocess(A, run / "model.h5", q, forcing, runtime)
    res["run"] = run.name
    (run / "result.json").write_text(json.dumps(res))
    import os
    if not os.environ.get("STORMLAB_KEEP_H5"):
        (run / "model.h5").unlink(missing_ok=True)      # large; results are kept in result.json
    put(state="done", progress=1, runtime_s=round(runtime, 1))
except Exception as e:
    put(state="error", error=str(e), trace=traceback.format_exc()[-2000:])
