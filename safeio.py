"""Status files shared between the server and job processes. On Windows a file cannot be replaced while
another process has it open, so writes and reads retry briefly instead of failing."""
import json, os, time
from pathlib import Path


def write_json(path, obj, tries=40):
    path = Path(path); tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    for k in range(tries):
        try:
            os.replace(tmp, path); return True
        except PermissionError:
            time.sleep(0.05)
    try:
        tmp.unlink()
    except OSError:
        pass
    return False                      # skip this update; the next one will get through


def read_json(path, default=None, tries=40):
    path = Path(path)
    for k in range(tries):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError, UnicodeDecodeError):
            time.sleep(0.05)
    return default
