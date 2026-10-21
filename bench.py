"""Speed test (python bench.py): a 30-minute storm on the Jesmond Vale preset, run on one core, on all cores and on
the GPU when there is one; keeps the fastest setting and seeds the run-time and memory estimates."""
import json, os, subprocess, sys, time
from pathlib import Path
import safeio, sysinfo

ROOT = Path(__file__).parent
work = ROOT / "runs" / "_bench"; work.mkdir(parents=True, exist_ok=True)
status = work / "status.json"
put = lambda **kw: safeio.write_json(status, dict(kw, t=time.time()))
PROBE = r'''
import json, os, sys, time, resource
t = time.time()
import openswmm.engine as eng
eng.run(sys.argv[1], sys.argv[2], sys.argv[3])
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 if hasattr(resource, "getrusage") else None
print(json.dumps({"s": time.time() - t, "mb": rss}))
'''
PROBE_WIN = PROBE.replace("import json, os, sys, time, resource", "import json, os, sys, time").replace(
    'rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 if hasattr(resource, "getrusage") else None',
    'import psutil; rss = psutil.Process().memory_info().peak_wset / 2**20')


def main():
    import areas, core
    put(state="running", progress=0.05, step="Preparing the test")
    A = areas.load("jesmond-vale")
    q, _ = core.validate(A, dict(depth=30, duration=30, profile="centre", sim=0.5, inflow="base", base_q=2))
    inp = work / "bench.inp"; core.write_inp(A, q, inp, "bench.h5")
    text = inp.read_text()
    si = sysinfo.info(); cores = si["cores_physical"] or 1
    configs = [("1 core", {"OMP_NUM_THREADS": "1"}, None)]
    if cores > 1: configs.append((f"{cores} cores", {"OMP_NUM_THREADS": str(cores)}, None))
    if si.get("gpu"): configs.append(("graphics card (CUDA)", {"OMP_NUM_THREADS": str(cores)}, "CUDA"))
    probe = work / "probe.py"; probe.write_text(PROBE_WIN if os.name == "nt" else PROBE)
    results = []
    for n, (label, env, backend) in enumerate(configs):
        put(state="running", progress=0.1 + 0.85 * n / len(configs), step=f"Testing {label}")
        f = work / f"c{n}.inp"
        f.write_text(text.replace("[2D_OPTIONS]", f"[2D_OPTIONS]\nBACKEND {backend}") if backend else text)
        t = time.time()
        r = subprocess.run([sys.executable, str(probe), str(f), str(work / f"c{n}.rpt"), str(work / f"c{n}.out")],
                           env={**os.environ, **env}, capture_output=True, text=True, timeout=1800)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            results.append(dict(label=label, ok=False, note=(r.stderr or "failed")[-200:])); continue
        used_gpu = backend is None or "CPU marcher" not in (r.stderr or "")
        results.append(dict(label=label, ok=used_gpu, seconds=round(out["s"], 1), peak_mb=out["mb"], env=env, backend=backend,
                            note="" if used_gpu else "the engine fell back to the processor (no CUDA support in this install)"))
    ok = [r_ for r_ in results if r_.get("ok")]
    best = min(ok, key=lambda r_: r_["seconds"])
    cells = A["n_cells"]
    k = max(best["seconds"] - 3, 1) / (cells * q["sim"])
    base_mb = 80.0
    kb_cell = max(1.5, ((ok[0].get("peak_mb") or 260) - base_mb) * 1024 / cells * 1.3)    # cautious
    cal = json.loads(core.CALIB.read_text()) if core.CALIB.exists() else {}
    cal.update(k=k, last=k, threads=best["env"].get("OMP_NUM_THREADS", "1"), backend=best["backend"], mem_kb_per_cell=round(kb_cell, 2),
               bench=dict(when=time.strftime("%Y-%m-%d %H:%M"), results=results, best=best["label"], cells=cells, sim_h=q["sim"]))
    core.CALIB.parent.mkdir(exist_ok=True); core.CALIB.write_text(json.dumps(cal, indent=1))
    put(state="done", progress=1, best=best["label"], results=results)
    for p in work.glob("c*.*"): p.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        import traceback
        put(state="error", error=f"{type(ex).__name__}: {ex}", trace=traceback.format_exc()[-1500:])
