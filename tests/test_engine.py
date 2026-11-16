"""A complete short run on the preset: the engine runs, the water balance closes, outputs are written."""
import json, subprocess, sys
from pathlib import Path
import pytest
import areas, core

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.engine
def test_short_preset_run(tmp_path):
    A = areas.load("jesmond-vale")
    q, _ = core.validate(A, dict(depth=30, duration=30, profile="centre", sim=0.5, inflow="base", base_q=2))
    run = tmp_path / "run"; run.mkdir()
    (run / "params.json").write_text(json.dumps(q))
    subprocess.run([sys.executable, str(ROOT / "worker.py"), str(run)], cwd=run, timeout=600, check=False)
    status = json.loads((run / "status.json").read_text())
    assert status["state"] == "done", status.get("error")
    s = json.loads((run / "result.json").read_text())["stats"]
    assert abs(s["mb_error_pct"]) < 0.05
    assert s["max_depth"] > 0
    assert (run / "time_series.nc").exists()
    import netCDF4
    with netCDF4.Dataset(str(run / "time_series.nc")) as nc:
        assert nc.Conventions == "CF-1.8"                  # set last, so present only if the file was written fully
        assert "hazard_class" in nc.variables


@pytest.mark.engine
def test_open_gate_passes_water(tmp_path, monkeypatch):
    """Water reaches a gate's openings and passes through them, with the 1D network's water balance intact
    (a gate's openings used to be taller than their nodes, so the engine never coupled them)."""
    import re, numpy as np, json, core, areas
    from pathlib import Path
    A = areas.load("jesmond-vale"); H, X0, Y1 = A["H"], A["X0"], A["Y1"]
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz"); cj = np.argwhere(m["is_chan"] & m["keep"])
    j, i = cj[np.argmin(((cj - np.median(cj, axis=0)) ** 2).sum(1))]; x, y = X0 + (i + .5) * H, Y1 - (j + .5) * H
    ms = [dict(type="embankment", pts=[[x - 60, y], [x + 60, y]], height=3.0, width=6),
          dict(type="gate", pts=[[x - 12, y], [x + 12, y]], height=3.0, mode="level", close_level=50.0)]
    q, _ = core.validate(A, dict(depth=0, duration=60, profile="centre", sim=1.0, inflow="base", base_q=4, measures=ms, use_measures=True))
    inp = tmp_path / "m.inp"; core.write_inp(A, q, inp, "m.h5")
    import openswmm.engine as eng
    monkeypatch.chdir(tmp_path); eng.run_with_callback(str(inp), str(tmp_path / "m.rpt"), str(tmp_path / "m.out"), lambda f: None)
    rpt = (tmp_path / "m.rpt").read_text()
    flows = [float(l.split()[2]) for l in rpt.splitlines() if re.match(r"\s+GO\d+_\d+\s+ORIFICE\s+[\d.]", l)]
    errs = [abs(float(l.split()[-1])) for l in rpt.splitlines() if "Continuity Error (%)" in l]
    assert sum(flows) > 1.0                         # the river gets through the open gate
    assert max(flows) < 4 * 3                        # no runaway oscillation (the river carries 4 m3/s)
    assert max(errs) < 1.0
    ms[1]["close_level"] = 0.2                      # the same gate set to close once water is 0.2 m deep upstream
    q, _ = core.validate(A, dict(q, measures=ms)); core.write_inp(A, q, inp, "m.h5")
    eng.run_with_callback(str(inp), str(tmp_path / "m.rpt"), str(tmp_path / "m.out"), lambda f: None)
    shut = [float(l.split()[2]) for l in (tmp_path / "m.rpt").read_text().splitlines() if re.match(r"\s+GO\d+_\d+\s+ORIFICE\s+[\d.]", l)]
    assert sum(shut) < 0.5 * sum(flows)              # it closes early, so far less gets through


@pytest.mark.engine
def test_tank_and_pump_take_water(tmp_path, monkeypatch):
    """A tank fills from the surface and a pump moves its rate, with the 1D water balance intact."""
    import re, numpy as np, core, areas
    from pathlib import Path
    A = areas.load("jesmond-vale"); H, X0, Y1 = A["H"], A["X0"], A["Y1"]
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz"); cj = np.argwhere(m["is_chan"] & m["keep"])
    j, i = cj[np.argmin(((cj - np.median(cj, axis=0)) ** 2).sum(1))]; x, y = X0 + (i + .5) * H, Y1 - (j + .5) * H
    ms = [dict(type="tank", pts=[[x, y]], volume=2000, inflow=0.2), dict(type="pump", pts=[[x + 40, y + 40], [x + 240, y + 240]], rate=0.5, start=0.1)]
    q, _ = core.validate(A, dict(depth=0, duration=60, profile="centre", sim=1.0, inflow="base", base_q=4, measures=ms, use_measures=True))
    inp = tmp_path / "m.inp"; core.write_inp(A, q, inp, "m.h5")
    import openswmm.engine as eng
    monkeypatch.chdir(tmp_path); eng.run_with_callback(str(inp), str(tmp_path / "m.rpt"), str(tmp_path / "m.out"), lambda f: None)
    rpt = (tmp_path / "m.rpt").read_text()
    peak = {l.split()[0]: float(l.split()[2]) for l in rpt.splitlines() if re.match(r"\s+(OT|P)\d+\s+(ORIFICE|PUMP)\s+[\d.]", l)}
    assert peak.get("OT1", 0) > 0.1                  # water enters the tank
    assert abs(peak.get("P2", 0) - 0.5) < 0.05       # the pump runs at its rate
    assert max(abs(float(l.split()[-1])) for l in rpt.splitlines() if "Continuity Error (%)" in l) < 1.0
