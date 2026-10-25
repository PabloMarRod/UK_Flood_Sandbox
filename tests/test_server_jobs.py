"""The IFC endpoints refuse to rebuild the model only while a job is actually running."""
import subprocess, sys
import server


def test_a_finished_job_does_not_block(monkeypatch):
    done = subprocess.Popen([sys.executable, "-c", "pass"]); done.wait()
    monkeypatch.setitem(server.JOB, "kind", "run"); monkeypatch.setitem(server.JOB, "proc", done)
    assert server.api_current_kind() is None                     # the last run ended: adding a building is allowed
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setitem(server.JOB, "proc", alive)
        assert server.api_current_kind() == "run"                 # while it runs, it is refused
    finally:
        alive.kill(); alive.wait()
