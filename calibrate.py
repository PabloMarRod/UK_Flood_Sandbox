"""Compare an estuary model with its check gauges for a recorded period, and calibrate the bed profile
and roughness against them.   python calibrate.py <estuary> <start UTC, e.g. 2026-10-01T00:00> <hours> [--fit]"""
import json, sys, datetime as dt
from pathlib import Path
import numpy as np
import estuary as E


def observed(eid, start, hours):
    t0 = dt.datetime.fromisoformat(start); t1 = t0 + dt.timedelta(hours=hours)
    cfg = E.config(eid); out = {}
    for g in cfg["check_gauges"]:
        rec = E.recorded_levels(eid, g["measure"], t0, t1)
        keys = sorted(k for k in rec if t0.strftime("%Y-%m-%dT%H:%M") <= k <= t1.strftime("%Y-%m-%dT%H:%M")
                      and isinstance(rec[k], (int, float)))
        vals = np.array([rec[k] for k in keys], float) if keys else np.array([])
        if len(keys) > 20 and np.nanstd(vals) < 0.05:
            print(f"skipping {g['name']}: readings are flat (gauge fault)"); continue
        if len(keys) > 20:
            out[g["name"]] = (np.array([(dt.datetime.fromisoformat(k) - t0).total_seconds() / 60 for k in keys]),
                              np.array([rec[k] for k in keys], float))
    return out


def extremes(t, v, kind):
    """High or low waters: local extremes at least 4 h apart."""
    s = 1 if kind == "hw" else -1
    idx = [i for i in range(1, len(v) - 1) if s * v[i] >= s * v[i - 1] and s * v[i] >= s * v[i + 1]]
    keep = []
    for i in sorted(idx, key=lambda i: -s * v[i]):
        if all(abs(t[i] - t[j]) > 240 for j in keep): keep.append(i)
    return sorted(keep)


def compare(eid, res, obs, hw_only=()):
    geo = E.geometry(eid); cfg = geo["config"]; chs = np.array([s["ch"] for s in geo["stations"]])
    rows = []
    for g in cfg["check_gauges"]:
        if g["name"] not in obs: continue
        t, o = obs[g["name"]]
        k = g.get("node", int(np.abs(chs - g["ch"]).argmin()))
        mod = np.interp(t, res["minutes"], res["heads"][:, k])
        def pairs(io, im):            # each observed extreme with the nearest modelled one within 3 hours
            out = []
            for i in io:
                if not im: break
                j = min(im, key=lambda j: abs(t[j] - t[i]))
                if abs(t[j] - t[i]) <= 180: out.append((i, j))
            return out
        ph = pairs(extremes(t, o, "hw"), extremes(t, mod, "hw")); pl = pairs(extremes(t, o, "lw"), extremes(t, mod, "lw"))
        dhw = float(np.mean([mod[j] - o[i] for i, j in ph])) if ph else np.nan
        dlw = float(np.mean([mod[j] - o[i] for i, j in pl])) if pl else np.nan
        lag = float(np.mean([t[j] - t[i] for i, j in ph])) if ph else np.nan
        rmse = float(np.sqrt(np.mean((mod - o) ** 2)))
        rows.append(dict(gauge=g["name"], ch_km=round(g["ch"] / 1000, 1), rmse=round(rmse, 3), hw_err=round(dhw, 3),
                         lw_err=round(dlw, 3), hw_lag_min=round(lag, 1), lw_used=g["name"] not in hw_only,
                         skip=g["name"] in cfg.get("calibration_skip", [])))
    return rows


def score(rows):
    s = 0
    for r in rows:
        if r.get("skip"): continue
        s += np.nan_to_num(r["hw_err"], nan=1) ** 2 + (np.nan_to_num(r["lw_err"], nan=1) ** 2 if r["lw_used"] else 0) \
             + (np.nan_to_num(r["hw_lag_min"], nan=60) / 30) ** 2
    return s / max(len(rows), 1)


if __name__ == "__main__":
    eid, start, hours = sys.argv[1], sys.argv[2], float(sys.argv[3])
    cfg = E.config(eid)
    q = dict(sim=hours, tide_mode="recorded", tide_start=start, surge_peak=0, slr=0)
    sp = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--spinup=")), 1440))
    obs = observed(eid, start, hours)
    hw_only = tuple(cfg.get("calibration_hw_only", []))
    work = Path("/tmp/calib_" + eid); work.mkdir(exist_ok=True)
    res = E.run(eid, q, work, spinup_min=sp); rows = compare(eid, res, obs, hw_only)
    print("routing error", res["routing_error"], "score", round(score(rows), 4))
    for r in rows: print(r)
    if "--fit" in sys.argv:
        bp = [list(p) for p in cfg["bed_profile"]]; n_ = cfg["manning"]
        best = score(rows); log = []
        steps = [1.5, 0.75, 0.4]
        for step in steps:
            improved = True
            while improved:
                improved = False
                for k in list(range(len(bp))) + ["n"]:
                    for sgn in (+1, -1):
                        trial = [p[:] for p in bp]; tn = n_
                        if k == "n": tn = max(0.012, n_ * (1 + sgn * 0.15 * step))
                        else: trial[k][1] = round(trial[k][1] + sgn * step, 2)
                        r2 = compare(eid, E.run(eid, q, work, spinup_min=sp, override=dict(bed_profile=trial, manning=tn)), obs, hw_only)
                        sc = score(r2)
                        if sc < best - 1e-4:
                            best, bp, n_, rows, improved = sc, trial, tn, r2, True
                            log.append(dict(score=round(sc, 4), bed=bp, manning=round(n_, 4)))
                            print("improved", round(sc, 4), bp, round(n_, 4), flush=True)
        out = dict(period=[start, hours], score=round(best, 4), bed_profile=bp, manning=round(n_, 4), gauges=rows, log=log)
        Path(f"/tmp/calib_{eid}/fit.json").write_text(json.dumps(out, indent=1))
        print("FINAL", json.dumps(out)[:2000])
