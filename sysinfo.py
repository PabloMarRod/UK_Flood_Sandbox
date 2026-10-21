"""What this computer has: processor, cores, memory and an NVIDIA graphics card if present. Read with ordinary
user rights (no administrator access); nothing leaves the computer."""
import os, platform, re, shutil, subprocess


def _mem():
    try:
        import psutil
        v = psutil.virtual_memory(); return v.total / 2**20, v.available / 2**20
    except Exception:
        pass
    try:                                                   # Linux
        m = dict(l.split(":", 1) for l in open("/proc/meminfo"))
        kb = lambda k: float(m[k].split()[0])
        return kb("MemTotal") / 1024, kb("MemAvailable") / 1024
    except Exception:
        pass
    if platform.system() == "Windows":
        import ctypes
        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                        ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
        s = MS(); s.l = ctypes.sizeof(MS); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return s.tot / 2**20, s.avail / 2**20
    try:                                                   # macOS
        tot = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**20
        return tot, tot * 0.5
    except Exception:
        return None, None


def _cpu_model():
    try:
        if platform.system() == "Windows":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        if platform.system() == "Darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        for l in open("/proc/cpuinfo"):
            if l.lower().startswith("model name"): return l.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _gpu():
    exe = shutil.which("nvidia-smi")
    if not exe: return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
        return out.split("\n")[0] if out else None
    except Exception:
        return None


def info():
    logical = os.cpu_count() or 1
    try:
        import psutil; physical = psutil.cpu_count(logical=False) or logical
    except Exception:
        physical = logical
    tot, avail = _mem()
    return dict(cpu=_cpu_model(), cores_logical=logical, cores_physical=physical,
                mem_total_mb=round(tot) if tot else None, mem_avail_mb=round(avail) if avail else None,
                gpu=_gpu(), os=f"{platform.system()} {platform.release()}", python=platform.python_version())
