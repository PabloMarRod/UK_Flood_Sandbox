"""UK Flood Sandbox: local server.

    python server.py            then open http://localhost:8765
"""
import net  # noqa: F401  (network setup: user agent, certificates)
import json, subprocess, sys, threading, time, traceback, uuid, webbrowser
import safeio
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 8765
ROOT = Path(__file__).parent
RUNS = ROOT / "runs"
JOB = {"kind": None, "id": None, "proc": None}   # one heavy job at a time: a run or an area preparation
LOCK = threading.Lock()


def start_bench():
    """Run the speed test as a background job (call with LOCK held)."""
    (RUNS / "_bench").mkdir(parents=True, exist_ok=True)
    safeio.write_json(RUNS / "_bench" / "status.json", {"state": "running", "progress": 0, "step": "Starting"})
    JOB.update(kind="bench", id="_bench", proc=subprocess.Popen([sys.executable, str(ROOT / "bench.py")], cwd=ROOT))


def auto_bench():
    """First start on this computer: measure it once, in the background, when nothing else is running."""
    time.sleep(8)
    if core.calibration().get("bench"): return
    with LOCK:
        if not busy(): start_bench()
SCENE_LOCK = threading.Lock()


def busy():
    return JOB["proc"] is not None and JOB["proc"].poll() is None


def latest_result(area_id):
    best = None
    for s in RUNS.glob("*/result.json"):
        try:
            p = json.loads((s.parent / "params.json").read_text())
        except Exception:
            continue
        if p.get("area") == area_id and (best is None or s.parent.name > best.parent.name):
            best = s
    return best


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _status(self, path, kind, rid):
        st = safeio.read_json(path)
        with LOCK:
            mine = JOB["id"] == rid and JOB["kind"] == kind
            alive = mine and busy()
        if st is None:
            if mine and not alive:      # the job process ended without reporting
                return {"state": "error", "error": "The job stopped unexpectedly. See the console window for details."}
            return {"state": "starting", "progress": 0}
        if st["state"] not in ("done", "error") and mine and not alive:
            st = {"state": "error", "error": "The job stopped unexpectedly. See the console window for details."}
        if st["state"] in ("done", "error"):
            with LOCK:
                if JOB["id"] == rid and JOB["kind"] == kind:
                    JOB.update(kind=None, id=None, proc=None)
        return st

    def do_GET(self):
        try:
            self._get()
        except Exception as e:
            traceback.print_exc()
            try: self._send(500, {"error": f"server error: {e}"})
            except Exception: pass

    def do_POST(self):
        try:
            self._post()
        except Exception as e:
            traceback.print_exc()
            try: self._send(500, {"error": f"server error: {e}"})
            except Exception: pass

    def _get(self):
        p = self.path.split("?")[0]
        if p == "/view3d.js":
            return self._send(200, (ROOT / "static" / "view3d.js").read_bytes(), "application/javascript; charset=utf-8")
        if p in ("/", "/index.html"):
            return self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/areas":
            return self._send(200, {"areas": areas.listing(), "k": core.k_per_cell(), "max_cells": areas.MAX_CELLS, "hard_cells": areas.ceiling_cells(),
                                    "max_run_s": core.MAX_RUN_S})
        if p.startswith("/api/area/"):
            try:
                A = areas.load(p.rsplit("/", 1)[1])
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            res = latest_result(A["id"])
            base = Path(A["dir"]) / "cache" / "baseline.json"
            result = json.loads(res.read_text()) if res else (json.loads(base.read_text()) if base.exists() else None)
            if result is not None:
                result.setdefault("run", res.parent.name if res else f"{A['id']}-baseline")
            return self._send(200, {"area": {k: v for k, v in A.items() if k != "dir"},
                                    "static": areas.static_layers(A), "result": result,
                                    "baseline": json.loads(base.read_text())["stats"] if base.exists() else None})
        if p == "/api/system":                    # this computer, the speed test and the resulting limits
            import sysinfo
            cal = core.calibration(); st = safeio.read_json(RUNS / "_bench" / "status.json") or {}
            return self._send(200, dict(info=sysinfo.info(), k=core.k_per_cell(), threads=cal.get("threads"), backend=cal.get("backend"),
                                        mem_kb_per_cell=cal.get("mem_kb_per_cell"), ceiling_cells=areas.ceiling_cells(),
                                        bench=cal.get("bench"), bench_status=st, running=bool(JOB.get("kind") == "bench" and busy())))
        if p == "/api/current":
            with LOCK:
                if busy():
                    d = (RUNS if JOB["kind"] == "run" else areas.AREAS) / JOB["id"]
                    return self._send(200, {"kind": JOB["kind"], "id": JOB["id"],
                                            "params": safeio.read_json(d / "params.json") if JOB["kind"] == "run" else None})
            return self._send(200, {"kind": None})
        if p == "/api/tidestats":
            import estuary
            from urllib.parse import urlparse, parse_qs
            eid = parse_qs(urlparse(self.path).query).get("est", ["tyne"])[0]
            return self._send(200, estuary.tide_recent_stats(eid))
        if p.startswith("/api/scene/"):
            import scene
            try:
                A = areas.load(p.rsplit("/", 1)[1])
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            f = Path(A["dir"]) / "cache" / "scene.json"
            if f.exists() and b'"version": 2' not in f.read_bytes()[-400:] and b'"trees"' not in f.read_bytes():
                f.unlink()                       # scenes from before trees were added are rebuilt once
            if not f.exists():
                with SCENE_LOCK:
                    if not f.exists(): scene.build_scene(A)
            return self._send(200, f.read_bytes())
        if p.startswith("/api/rofsw/"):            # EA national surface-water map for an area: classes per cell
            import rofsw, base64
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query); A = areas.load(p.rsplit("/", 1)[1])
            try:
                c = rofsw.classes(A, qs.get("layer", ["rofsw"])[0])
            except Exception as e:
                return self._send(502, {"error": str(e)})
            return self._send(200, dict(NX=A["NX"], NY=A["NY"], cells_u8=base64.b64encode(c.tobytes()).decode(),
                                        counts={k: int((c == k).sum()) for k in (1, 2, 3)}))
        if p.startswith("/api/rofsw_compare/"):    # /api/rofsw_compare/<run>?layer=...&band=1
            import rofsw
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query); rid = p.rsplit("/", 1)[1]
            f = RUNS / rid / "result.json"
            if not f.exists(): return self._send(404, {"error": "run not found"})
            res = json.loads(f.read_text()); A = areas.load(res["area"])
            try:
                return self._send(200, rofsw.compare(A, res, qs.get("layer", ["rofsw"])[0], int(qs.get("band", ["1"])[0])))
            except Exception as e:
                return self._send(502, {"error": str(e)})
        if p.startswith("/api/runs/"):          # this area's runs, newest first (for choosing a comparison)
            aid = p.rsplit("/", 1)[1]; out = []
            for f in sorted(RUNS.glob("*/result.json"), reverse=True):
                try:
                    q = json.loads((f.parent / "params.json").read_text())
                except Exception:
                    continue
                if q.get("area") != aid: continue
                out.append(dict(id=f.parent.name, measures=len(q.get("measures") or []),
                                names=[m.get("name") for m in (q.get("measures") or [])][:6], depth=q.get("depth"), duration=q.get("duration")))
                if len(out) >= 30: break
            return self._send(200, out)
        if p.startswith("/api/measures/"):
            try:
                A = areas.load(p.rsplit("/", 1)[1])
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            f = Path(A["dir"]) / "measures.json"
            return self._send(200, json.loads(f.read_text()) if f.exists() else [])
        if p.startswith("/api/areajob/"):
            aid = p.rsplit("/", 1)[1]
            return self._send(200, self._status(areas.AREAS / aid / "status.json", "area", aid))
        if p.startswith("/api/job/"):
            rid = p.rsplit("/", 1)[1]
            return self._send(200, self._status(RUNS / rid / "status.json", "run", rid))
        if p.startswith("/api/result/"):
            rid = p.rsplit("/", 1)[1]; f = RUNS / rid / "result.json"
            if not f.exists(): return self._send(404, {"error": "not ready"})
            res = json.loads(f.read_text()); res.setdefault("run", rid)
            return self._send(200, res)
        if p.startswith("/api/video/") and p.endswith("/file"):
            rid = p.split("/")[3]; name = self.path.split("name=")[-1] if "name=" in self.path else "simulation.mp4"
            f = RUNS / rid / Path(name).name
            if not f.exists(): return self._send(404, {"error": "no video"})
            data = f.read_bytes()
            ctype = {".mp4": "video/mp4", ".pdf": "application/pdf", ".zip": "application/zip"}.get(f.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{rid}-{f.name}"'); self.end_headers()
            self.wfile.write(data); return
        self._send(404, {"error": "not found"})

    def _post(self):
        p = self.path.split("?")[0]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if p in ("/api/check", "/api/run"):
            try:
                A = areas.load(body.get("area", ""))
                q, warn = core.validate(A, body)
            except ValueError as e:
                return self._send(200 if p == "/api/check" else 400, {"ok": False, "error": str(e)})
            if p == "/api/check":
                return self._send(200, {"ok": True, "params": q, "warnings": warn, "forcing": core.forcing_preview(q)})
            with LOCK:
                if busy():
                    return self._send(409, {"error": "Another job is running.", "busy": True})
                rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
                run = RUNS / rid; run.mkdir(parents=True)
                (run / "params.json").write_text(json.dumps(q))
                proc = subprocess.Popen([sys.executable, str(ROOT / "worker.py"), str(run.resolve())], cwd=run)
                JOB.update(kind="run", id=rid, proc=proc)
            return self._send(200, {"id": rid})
        if p == "/api/areas":
            try:
                A = areas.new_area(body.get("name", "My area"), body.get("poly", []), int(body.get("H", 5)))
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            with LOCK:
                if busy():
                    return self._send(409, {"error": "Another job is running.", "busy": True})
                areas.save(A)
                proc = subprocess.Popen([sys.executable, str(ROOT / "prepare.py"), A["id"]], cwd=ROOT)
                JOB.update(kind="area", id=A["id"], proc=proc)
            return self._send(200, {"id": A["id"]})
        if p.startswith("/api/measures/"):
            A = areas.load(p.rsplit("/", 1)[1])
            import core as _core
            ms = _core.check_measures(A, body if isinstance(body, list) else body.get("measures", []))
            (Path(A["dir"]) / "measures.json").write_text(json.dumps(ms))
            return self._send(200, ms)
        if p == "/api/bench":                      # start the speed test
            with LOCK:
                if busy(): return self._send(409, {"error": "Another job is running.", "busy": True})
                start_bench()
            return self._send(200, {"ok": True})
        if p.startswith("/api/export/"):           # /api/export/<run>/gis  or  /api/export/<run>/report
            import export
            parts = p.split("/"); rid, kind = parts[3], parts[4]
            run = RUNS / rid
            if "/" in rid or ".." in rid or not (run / "result.json").exists():
                return self._send(404, {"error": "run not found (results from before this version cannot be exported; rerun it)"})
            try:
                if kind == "gis": out = export.gis(run)
                else: out = export.report(run, body.get("description", ""), body.get("snapshot"), body.get("ref"), float(body.get("factor", 1) or 1))
            except Exception as ex:
                import traceback; traceback.print_exc()
                return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})
            return self._send(200, {"ok": True, "file": f"/api/video/{rid}/file?name={out.name}", "path": str(out), "size": out.stat().st_size})
        if p.startswith("/api/video/"):
            # /api/video/<run>/frame   {k, data: "data:image/jpeg;base64,..."}   or   /api/video/<run>/finish {fps, name}
            parts = p.split("/"); rid, act = parts[3], parts[4]
            if "/" in rid or ".." in rid: return self._send(400, {"error": "bad run id"})
            d = RUNS / rid / "video_frames"; d.mkdir(parents=True, exist_ok=True)
            if act == "frame":
                import base64
                (d / f"f{int(body['k']):04d}.jpg").write_bytes(base64.b64decode(body["data"].split(",", 1)[1]))
                return self._send(200, {"ok": True})
            if act == "finish":
                import imageio_ffmpeg, shutil
                name = Path(body.get("name") or "simulation.mp4").name
                out = RUNS / rid / name; fps = max(1, min(30, int(body.get("fps", 5))))
                cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(d / "f%04d.jpg"),
                       "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                shutil.rmtree(d, ignore_errors=True)
                if r.returncode != 0 or not out.exists():
                    return self._send(500, {"error": "video encoding failed: " + r.stderr[-300:]})
                return self._send(200, {"ok": True, "file": f"/api/video/{rid}/file?name={name}", "path": str(out), "size": out.stat().st_size})
            return self._send(404, {"error": "unknown video action"})
        if p == "/api/cancel":
            with LOCK:
                if busy():
                    JOB["proc"].kill()
                    d = (RUNS if JOB["kind"] == "run" else areas.AREAS) / JOB["id"]
                    safeio.write_json(d / "status.json", {"state": "error", "error": "Cancelled"})
                JOB.update(kind=None, id=None, proc=None)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/area/"):
            try:
                areas.delete(p.rsplit("/", 1)[1])
                return self._send(200, {"ok": True})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    if not ((3, 10) <= sys.version_info[:2] <= (3, 13)):
        sys.exit(f"Python {sys.version.split()[0]} is not supported by openswmm 6.0.0a4. Use Python 3.10 to 3.13 "
                 "(the start_windows.bat / start_mac_linux.sh scripts set this up for you).")
    try:
        import openswmm.engine  # noqa: F401
    except ImportError:
        sys.exit("openswmm is not installed. Run: python -m pip install -r requirements.txt")
    import areas, core
    gone = areas.prune()
    if gone:
        print(f"Removed {len(gone)} older drawn area(s); their simulation results stay in runs/.")
    print("Preparing map layers (first start only)...")
    for a in areas.listing():
        areas.static_layers(areas.load(a["id"]))
    RUNS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=auto_bench, daemon=True).start()
    print(f"UK Flood Sandbox running at http://localhost:{PORT}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
