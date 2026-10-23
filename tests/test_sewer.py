"""Drainage network import: every check that stops an import, every in-house fix, and a coupled run."""
import copy, json, shutil, subprocess, sys
from pathlib import Path
import numpy as np
import pytest
import shapely
from shapely.geometry import Point, LineString
import areas, core, sewer

ROOT = Path(__file__).resolve().parents[1]
NET = json.loads((ROOT / "tests" / "data" / "jesmond_sewer.json").read_text())   # 20 manholes, 19 pipes, 1 outfall


def write_inp(path, nodes, pipes, extra=""):
    o = ["[OPTIONS]", "FLOW_UNITS CMS", "LINK_OFFSETS ELEVATION", "", "[JUNCTIONS]"]
    o += [f"{n['id']} {n['invert']} {round(n['cover'] - n['invert'], 3)} 0 0 0" for n in nodes if n["type"] == "manhole"]
    o += ["[OUTFALLS]"] + [f"{n['id']} {n['invert']} FREE" for n in nodes if n["type"] == "outfall"]
    o += ["[CONDUITS]"] + [f"{p['id']} {p['us']} {p['ds']} {p['length']} 0.013 {p['us_inv']} {p['ds_inv']} 0 0" for p in pipes]
    o += ["[XSECTIONS]"] + [f"{p['id']} CIRCULAR {p['size']} 0 0 0 1" for p in pipes]
    o += ["[COORDINATES]"] + [f"{n['id']} {n['x']:.2f} {n['y']:.2f}" for n in nodes]
    Path(path).write_text("\n".join(o) + "\n" + extra)
    return path


def write_gpkg(path, nodes, pipes, crs="EPSG:27700", mm=True, fromto=True, covers=None, chamber_mm=None):
    import pyogrio
    nan = lambda v: np.nan if v is None else v
    g = np.array([shapely.to_wkb(Point(n["x"], n["y"])) for n in nodes], dtype=object)
    f = [np.array([n["id"] for n in nodes]), np.array([nan(n.get("cover")) for n in nodes], dtype=float),
         np.array([nan(n.get("invert")) for n in nodes], dtype=float),
         np.array(["Outfall" if n["type"] == "outfall" else "Manhole" for n in nodes])]
    names = ["node_id", "ground_level", "chamber_floor", "node_type"]
    if covers is not None: f.append(np.array([covers[k % len(covers)] for k in range(len(nodes))])); names.append("cover_type")
    if chamber_mm is not None: f.append(np.array([float(chamber_mm)] * len(nodes))); names.append("chamber_size")
    pyogrio.raw.write(str(path), g, f, fields=names, layer="manholes", driver="GPKG", crs=crs, geometry_type="Point")
    g = np.array([shapely.to_wkb(LineString([(p["x0"], p["y0"]), (p["x1"], p["y1"])])) for p in pipes], dtype=object)
    f = [np.array([p["id"] for p in pipes])] + ([np.array([p["us"] for p in pipes]), np.array([p["ds"] for p in pipes])] if fromto else []) + [
         np.array([nan(p["size"]) * (1000 if mm else 1) for p in pipes], dtype=float),
         np.array([nan(p["us_inv"]) for p in pipes], dtype=float), np.array([nan(p["ds_inv"]) for p in pipes], dtype=float)]
    pyogrio.raw.write(str(path), g, f, fields=["link_id"] + (["us_node_id", "ds_node_id"] if fromto else []) +
                      ["conduit_width", "us_invert", "ds_invert"], layer="pipes", driver="GPKG", crs=crs,
                      geometry_type="LineString", append=True)
    return path


@pytest.fixture(scope="module")
def A():
    return areas.load("jesmond-vale")


def run_check(A, f, tmp, accept=(), all_fixes=False):
    ins = sewer.inspect(f, tmp / "x")
    r = sewer.check(f, ins.get("mapping"), A, list(accept), tmp / "x")
    if all_fixes:
        r = sewer.check(f, ins.get("mapping"), A, [i["code"] for i in r["issues"] if i["sev"] == "fix"], tmp / "x")
    return r


def codes(r, sev=None):
    return {i["code"] for i in r["issues"] if sev is None or i["sev"] == sev}


def test_clean_swmm_imports(A, tmp_path):
    r = run_check(A, write_inp(tmp_path / "n.inp", NET["nodes"], NET["pipes"]), tmp_path)
    assert r["status"] == "needs_fixes" and codes(r, "fix") == {"F_OUT_MULTI"}   # several pipes into one outfall
    r = run_check(A, tmp_path / "n.inp", tmp_path, all_fixes=True)
    assert r["status"] == "ok" and r["summary"]["pipes"] == 20 and not codes(r, "error")


def test_messy_gis_is_repaired_only_with_consent(A, tmp_path):
    nodes, pipes = copy.deepcopy(NET["nodes"]), copy.deepcopy(NET["pipes"])
    for n in nodes[:3]: n["cover"] = None                                   # F_COVER
    for n in nodes[3:6]: n["invert"] = None                                 # F_INVERT
    pipes[0]["size"] = None                                                 # F_SIZE
    pipes[2]["x0"] += 3.0                                                   # F_SNAP (no from/to fields, 3 m off)
    pipes.append(dict(pipes[1]))                                            # F_DUP_LINK
    nodes.append(dict(id="LONE", x=nodes[0]["x"] + 12, y=nodes[0]["y"], cover=nodes[0]["cover"] or 30, invert=28, type="manhole"))
    x0 = A["X0"] - 80; mh = nodes[7]                                        # F_CLIP: a pipe leaving the area
    nodes.append(dict(id="OUTSIDE", x=x0, y=mh["y"], cover=None, invert=mh["invert"] - 2, type="manhole"))
    pipes.append(dict(id="LEAVE", us=mh["id"], ds="OUTSIDE", x0=mh["x"], y0=mh["y"], x1=x0, y1=mh["y"], size=0.3,
                      us_inv=mh["invert"], ds_inv=mh["invert"] - 2, length=None))
    f = write_gpkg(tmp_path / "n.gpkg", nodes, pipes, crs=None, fromto=False)
    r = run_check(A, f, tmp_path)
    expected = {"F_CRS", "F_MM", "F_SNAP", "F_SIZE", "F_COVER", "F_INVERT", "F_DUP_LINK", "F_ISOLATED", "F_CLIP", "F_ROUGH"}
    assert r["status"] == "needs_fixes" and expected <= codes(r, "fix"), codes(r)
    assert not codes(r, "error")
    partial = run_check(A, f, tmp_path, accept=sorted(codes(r, "fix") - {"F_SIZE"}))   # one fix declined: no import
    assert partial["status"] == "needs_fixes"
    with pytest.raises(ValueError):
        sewer.save(dict(A, dir=str(tmp_path)), partial, "n.gpkg")
    r = run_check(A, f, tmp_path, all_fixes=True)
    assert r["status"] == "ok"
    net = r["network"]
    assert "LONE" not in net["nodes"] and net["nodes"]["OUTSIDE"]["kind"] == "outfall"
    assert all(0.1 < l["g1"] < 1 for l in net["links"].values())             # millimetres converted, gap filled


def _mutate(kind):
    nodes, pipes = copy.deepcopy(NET["nodes"]), copy.deepcopy(NET["pipes"]); extra = ""
    if kind == "E_UNSUPPORTED":
        extra = "[DIVIDERS]\nD1 30 a CUTOFF 0.5\n"
    elif kind == "E_DATUM":
        for n in nodes:
            n["cover"] = (n["cover"] or 0) + 40; n["invert"] += 40
        for p in pipes: p["us_inv"] += 40; p["ds_inv"] += 40
    elif kind == "E_UNKNOWN_NODE":
        pipes[0]["ds"] = "NOWHERE"
    elif kind == "E_DUP_NODE":
        nodes.append(dict(nodes[0]))
    elif kind == "E_OUTSIDE":
        for n in nodes: n["x"] += 50000
    elif kind == "E_ABOVE":
        pipes[4]["us_inv"] += 8
    return nodes, pipes, extra


@pytest.mark.parametrize("kind", ["E_UNSUPPORTED", "E_DATUM", "E_UNKNOWN_NODE", "E_DUP_NODE", "E_OUTSIDE", "E_ABOVE"])
def test_blocking_errors_stop_the_import(A, tmp_path, kind):
    nodes, pipes, extra = _mutate(kind)
    r = run_check(A, write_inp(tmp_path / "n.inp", nodes, pipes, extra), tmp_path, all_fixes=True)
    assert r["status"] == "blocked" and kind in codes(r, "error"), codes(r)
    assert r["network"] is None


def test_too_many_missing_sizes_blocks(A, tmp_path):
    pipes = copy.deepcopy(NET["pipes"])
    for p in pipes[:8]: p["size"] = None
    r = run_check(A, write_gpkg(tmp_path / "n.gpkg", NET["nodes"], pipes), tmp_path, all_fixes=True)
    assert r["status"] == "blocked" and "E_SIZE" in codes(r, "error")


def test_swmm_coordinates_in_wgs84_are_offered_conversion(A, tmp_path):
    from pyproj import Transformer
    tf = Transformer.from_crs(27700, 4326, always_xy=True)
    nodes = copy.deepcopy(NET["nodes"])
    for n in nodes: n["x"], n["y"] = tf.transform(n["x"], n["y"])
    lines = write_inp(tmp_path / "n.inp", nodes, NET["pipes"]).read_text().split("\n")
    k = lines.index("[COORDINATES]")                                        # keep enough decimals for degrees
    lines[k + 1:] = [f"{n['id']} {n['x']:.8f} {n['y']:.8f}" for n in nodes]
    (tmp_path / "n.inp").write_text("\n".join(lines) + "\n")
    r = run_check(A, tmp_path / "n.inp", tmp_path)
    assert "F_CRS" in codes(r, "fix")
    assert run_check(A, tmp_path / "n.inp", tmp_path, all_fixes=True)["status"] == "ok"


@pytest.mark.engine
def test_coupled_run(A, tmp_path, monkeypatch):
    area = tmp_path / "area"; shutil.copytree(A["dir"], area, ignore=shutil.ignore_patterns("sewer*"))
    A2 = dict(A, dir=str(area))
    r = run_check(A2, write_inp(tmp_path / "n.inp", NET["nodes"], NET["pipes"]), tmp_path, all_fixes=True)
    sewer.save(A2, r, "n.inp")
    q, _ = core.validate(A2, dict(depth=40, duration=30, profile="centre", sim=0.5, inflow="base", base_q=1, sewer=True))
    assert q["sewer"]
    run = tmp_path / "run"; run.mkdir()
    (run / "params.json").write_text(json.dumps(q))
    inp = run / "model.inp"
    core.write_inp(A2, q, inp, "model.h5")
    text = inp.read_text()
    assert text.count("\nS_") > 40 and "sw" in text.split("[2D_TRIANGLE_NODE_MAP]")[1]
    assert "[STORAGE]" in text and "\nOG_" in text and "\nOU_" in text          # solid covers: gully in, cover out
    import openswmm.engine as eng
    monkeypatch.chdir(run)                                                  # the engine writes its 2D output here
    eng.run_with_callback(str(inp), str(run / "model.rpt"), str(run / "model.out"), lambda frac: None)
    rpt = (run / "model.rpt").read_text()
    errs = [float(l.split()[-1]) for l in rpt.split("\n") if "Continuity Error (%)" in l]
    assert errs and max(abs(e) for e in errs) < 0.5
    st = sewer.stats(run / "model.rpt", run / "sewer_nodes.json")
    assert st["v_sewer_in"] > 0 and st["v_sewer_out"] > 0
    assert st["v_sewer_out"] + st["v_sewer_spill"] <= 1.05 * st["v_sewer_in"] + 5   # nothing leaves that didn't enter (some may still be in the pipes)


def test_cover_types_and_chambers_are_read_and_checked(A, tmp_path):
    f = write_gpkg(tmp_path / "n.gpkg", NET["nodes"], NET["pipes"], covers=["Solid", "Grating", "Bolted down", "D400"],
                   chamber_mm=1200)
    r = run_check(A, f, tmp_path)
    assert "F_COVER_TYPE" in codes(r, "fix") and "W_CHAMBER_MM" in codes(r, "warn")
    r = run_check(A, f, tmp_path, all_fixes=True)
    assert r["status"] == "ok"
    kinds = [n.get("cover_type") for n in r["network"]["nodes"].values() if n["kind"] != "outfall"]
    assert {"solid", "grated", "sealed", None} <= set(kinds)                # None: unrecognised, uses the default
    assert all(abs(n["chamber_area"] - 1.131) < 0.01 for n in r["network"]["nodes"].values() if n["kind"] != "outfall"
               and not n.get("added"))


def test_swmm_surcharge_depth_means_sealed(A, tmp_path):
    lines = write_inp(tmp_path / "n.inp", NET["nodes"], NET["pipes"]).read_text().split("\n")
    k = lines.index("[JUNCTIONS]") + 1
    p = lines[k].split(); p[4] = "5"; lines[k] = " ".join(p)                  # surcharge depth on the first manhole
    (tmp_path / "n.inp").write_text("\n".join(lines))
    r = run_check(A, tmp_path / "n.inp", tmp_path, all_fixes=True)
    assert "W_SEALED" in codes(r, "warn") and r["network"]["nodes"][p[0]]["cover_type"] == "sealed"


@pytest.mark.parametrize("cover", ["solid", "grated", "sealed"])
def test_each_cover_meets_the_surface_as_described(A, tmp_path, cover):
    area = tmp_path / "area"; shutil.copytree(A["dir"], area, ignore=shutil.ignore_patterns("sewer*", "data"))
    A2 = dict(A, dir=str(area))
    sewer.save(A2, run_check(A2, write_inp(tmp_path / "n.inp", NET["nodes"], NET["pipes"]), tmp_path, all_fixes=True), "n.inp")
    q, _ = core.validate(A2, dict(depth=40, duration=30, sim=0.5, sewer=True, sewer_cover=cover))
    core.write_inp(A2, q, tmp_path / "model.inp", "model.h5")
    t = (tmp_path / "model.inp").read_text(); nm = t.split("[2D_TRIANGLE_NODE_MAP]")[1]
    links = [l for l in nm.split("\n") if l.startswith("sw")]
    manholes = 20                                                             # 19 + the junction added before the outfall
    if cover == "solid":
        assert sum(" G_" in l for l in links) == manholes and sum(" U_" in l for l in links) == manholes
        orf = t.split("[ORIFICES]")[1].split("[")[0]
        assert orf.count("\nOG_") == manholes and orf.count("\nOU_") == manholes and all(l.endswith("YES") for l in orf.split("\n") if l.startswith("O"))
    elif cover == "grated":
        assert sum(" S_" in l for l in links) == manholes + 1 and "\nOG_" not in t   # + the outfall
    else:
        assert sum(" S_" in l for l in links) == 1 and "\nOG_" not in t              # only the outfall


def structures_inp(path):
    """The test network plus a CSO weir to an overflow outfall, an orifice, a storage tank emptied by a pump, and a
    flap gate on the main outfall."""
    nodes, pipes = copy.deepcopy(NET["nodes"]), copy.deepcopy(NET["pipes"])
    by = {n["id"]: n for n in nodes}
    out = next(n for n in nodes if n["type"] == "outfall")
    cso = next(n for n in nodes if n["type"] == "manhole" and n["cover"] - n["invert"] > 1.2 and
               ((n["x"] - out["x"]) ** 2 + (n["y"] - out["y"]) ** 2) ** .5 < 120)
    a, b = pipes[3]["us"], pipes[3]["ds"]
    tk = by[pipes[6]["us"]]
    lines = write_inp(path, nodes, pipes).read_text().split("\n")
    i = lines.index("[OUTFALLS]") + 1
    lines[i] = lines[i] + " YES"                                           # flap gate on the main outfall
    lines.insert(i + 1, f"OVF1 {out['invert'] + 0.2} FREE NO")
    lines.insert(lines.index("[COORDINATES]") + 1, f"OVF1 {out['x'] + 6:.2f} {out['y'] + 6:.2f}")
    lines.insert(lines.index("[COORDINATES]") + 1, f"TK1 {tk['x'] + 4:.2f} {tk['y'] + 4:.2f}")
    extra = f"""
[STORAGE]
TK1 {tk['invert'] - 1.5:.3f} 2.5 0 FUNCTIONAL 0 0 30 0 0
[WEIRS]
CSO1 {cso['id']} OVF1 TRANSVERSE {cso['invert'] + 0.35:.3f} 1.84 NO 0 0
[ORIFICES]
OR1 {a} {b} SIDE {by[a]['invert'] + 0.1:.3f} 0.65 NO
[PUMPS]
PMP1 TK1 {tk['id']} PC1 ON 1.0 0.3
[CURVES]
PC1 PUMP2 0.5 0.01
PC1 1.0 0.02
PC1 2.5 0.02
"""
    conduit = f"PTK {tk['id']} TK1 6 0.013 {tk['invert']:.3f} {tk['invert'] - 1.5:.3f} 0 0"
    lines.insert(lines.index("[CONDUITS]") + 1, conduit)
    lines.insert(lines.index("[XSECTIONS]") + 1, "PTK CIRCULAR 0.225 0 0 0 1")
    lines.insert(lines.index("[XSECTIONS]") + 1, "CSO1 RECT_OPEN 0.3 1.0 0 0")
    lines.insert(lines.index("[XSECTIONS]") + 1, "OR1 CIRCULAR 0.15 0 0 0")
    Path(path).write_text("\n".join(lines) + extra)
    return path, cso["id"]


def test_structures_are_imported_and_written(A, tmp_path):
    f, cso = structures_inp(tmp_path / "s.inp")
    r = run_check(A, f, tmp_path, all_fixes=True)
    assert r["status"] == "ok", [(i["code"], i["title"]) for i in r["issues"] if i["sev"] == "error"]
    assert "W_STRUCTURES" in codes(r, "warn") and r["summary"]["structures"] == 3
    net = r["network"]
    assert net["links"]["CSO1"]["type"] == "weir" and net["nodes"]["TK1"]["kind"] == "storage"
    assert any(n.get("gated") for n in net["nodes"].values())
    area = tmp_path / "area"; shutil.copytree(A["dir"], area, ignore=shutil.ignore_patterns("sewer*", "data"))
    A2 = dict(A, dir=str(area)); sewer.save(A2, r, "s.inp")
    q, _ = core.validate(A2, dict(depth=40, duration=30, sim=0.5, sewer=True))
    core.write_inp(A2, q, tmp_path / "model.inp", "model.h5")
    t = (tmp_path / "model.inp").read_text()
    for sec in ("[WEIRS]", "[PUMPS]", "[ORIFICES]", "[STORAGE]"):
        assert sec in t
    assert "S_CSO1 S_" + cso + " S_OVF1 TRANSVERSE" in t and "SC_PC1 PUMP2" in t and "S_PMP1 S_TK1" in t
    assert " FREE YES" in t


def test_missing_curve_blocks(A, tmp_path):
    f, _ = structures_inp(tmp_path / "s.inp")
    f.write_text(f.read_text().replace("PC1 PUMP2", "PCX PUMP2").replace("\nPC1 ", "\nPCX "))
    r = run_check(A, f, tmp_path, all_fixes=True)
    assert r["status"] == "blocked" and "E_CURVE" in codes(r, "error")


@pytest.mark.engine
def test_structures_run(A, tmp_path, monkeypatch):
    f, cso = structures_inp(tmp_path / "s.inp")
    area = tmp_path / "area"; shutil.copytree(A["dir"], area, ignore=shutil.ignore_patterns("sewer*", "data"))
    A2 = dict(A, dir=str(area)); sewer.save(A2, run_check(A2, f, tmp_path, all_fixes=True), "s.inp")
    q, _ = core.validate(A2, dict(depth=100, duration=30, sim=0.75, sewer=True, sewer_gully_lps=150))
    run = tmp_path / "run"; run.mkdir(); monkeypatch.chdir(run)
    core.write_inp(A2, q, run / "model.inp", "model.h5")
    import openswmm.engine as eng
    eng.run_with_callback(str(run / "model.inp"), str(run / "model.rpt"), str(run / "model.out"), lambda frac: None)
    rpt = (run / "model.rpt").read_text()
    assert "ERROR" not in rpt.split("Element Count")[0]
    errs = [float(l.split()[-1]) for l in rpt.split("\n") if "Continuity Error (%)" in l]
    assert errs and max(abs(e) for e in errs) < 0.5
    flows = {l.split()[0]: float(l.split()[2]) for l in rpt.split("Link Flow Summary")[1].split("\n")
             if l.strip().startswith(("S_CSO1", "S_PMP1", "S_OR1"))}
    assert set(flows) == {"S_CSO1", "S_PMP1", "S_OR1"}
