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
