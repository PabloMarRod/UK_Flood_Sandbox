"""IFC buildings: reading, placing, checks, the mesh override and the building report."""
import json, shutil
from pathlib import Path
import pytest
import areas, core, ifcbuilding as ib, sewer
from ifc_samples import make_house, SITE


@pytest.fixture(scope="module")
def A():
    return areas.load("jesmond-vale")


@pytest.fixture(scope="module")
def G(A):
    return sewer.Ground(A)


def test_georeferenced_model_lands_in_place(A, G, tmp_path):
    f = make_house(tmp_path / "h.ifc", **SITE)
    r = ib.check(f, A, G)
    assert r["status"] == "ok", r["issues"]
    b = r["building"]
    assert abs(b["area"] - 216) < 3 and abs(b["ffl"] - (SITE["ground"] + 0.15)) < 0.01
    assert [d["name"] for d in b["doors"] if d["kind"] == "external"] == ["Front door", "Back door"]
    assert {d["name"]: d["spaces"] for d in b["doors"] if d["kind"] == "internal"} == {"Living to hall": ["G01", "G03"], "Hall to kitchen": ["G02", "G03"]}
    assert all(abs(d["sill"] - b["ffl"]) < 0.01 for d in b["doors"]) and b["interior"]["mode"] == "spaces"
    assert {w["name"]: w["spaces"] for w in b["windows"]} == {"Living room window": ["G01"], "Kitchen window": ["G02"]}
    assert {s["use"] for s in b["spaces"]} == {"Living room", "Kitchen", "Hall", "Bedroom"} and len(b["replaces"]) == 1
    assert abs(b["rot"] - SITE["rot"]) < 0.5


def test_millimetre_model_is_converted(A, G, tmp_path):
    r = ib.check(make_house(tmp_path / "mm.ifc", unit="MILLIMETERS", **SITE), A, G)
    assert r["status"] == "ok" and "W_UNITS" in {i["code"] for i in r["issues"]}
    assert abs(r["building"]["area"] - 216) < 3 and abs(r["building"]["ffl"] - (SITE["ground"] + 0.15)) < 0.01


def test_millimetre_model_with_scale_left_at_1(A, G, tmp_path, monkeypatch):
    import ifcopenshell
    f = make_house(tmp_path / "mm.ifc", unit="MILLIMETERS", **SITE)
    m = ifcopenshell.open(str(f)); m.by_type("IfcMapConversion")[0].Scale = 1.0; m.write(str(f))
    r = ib.check(f, A, G)
    assert r["status"] == "ok" and "W_GEOREF_SCALE" in {i["code"] for i in r["issues"]} and abs(r["building"]["area"] - 216) < 3


def test_local_model_needs_placement_then_a_level_repair(A, G, tmp_path):
    f = make_house(tmp_path / "loc.ifc", georef=False)
    assert ib.check(f, A, G)["status"] == "needs_placement"
    pl = dict(E=SITE["E"], N=SITE["N"], rot=SITE["rot"])
    r = ib.check(f, A, G, placement=pl)
    assert r["status"] == "needs_fixes" and "F_LEVELS" in {i["code"] for i in r["issues"]}
    r = ib.check(f, A, G, placement=pl, accepted=["F_LEVELS"])
    assert r["status"] == "ok" and abs(r["building"]["ffl"] - (r["building"]["ground_min"] + 0.15)) < 0.01
    r = ib.check(f, A, G, placement=pl, ffl_user=21.0)                  # or the user types the level
    assert r["status"] == "ok" and r["building"]["ffl"] == 21.0 and all(d["sill"] == 21.0 for d in r["building"]["doors"])


@pytest.mark.parametrize("case,code", [("outside", "E_OUTSIDE"), ("empty", "E_NO_GEOMETRY"), ("notifc", "E_READ")])
def test_blocking_problems(A, G, tmp_path, case, code):
    if case == "outside": f = make_house(tmp_path / "o.ifc", **dict(SITE, E=SITE["E"] + 20000))
    elif case == "empty": f = make_house(tmp_path / "e.ifc", empty=True)
    else: f = tmp_path / "x.ifc"; f.write_text("not an ifc file")
    r = ib.check(f, A, G)
    assert r["status"] == "blocked" and code in {i["code"] for i in r["issues"]}


def _area_copy(A, tmp_path):
    d = tmp_path / "area"; shutil.copytree(A["dir"], d, ignore=shutil.ignore_patterns("ifc*", "sewer*", "data"))
    (d / "data").mkdir(); shutil.copy(Path(A["dir"]) / "data" / "osm.json", d / "data" / "osm.json")
    shutil.copy(Path(A["dir"]) / "data" / "dtm.tif", d / "data" / "dtm.tif")
    return dict(A, dir=str(d))


def test_saved_building_replaces_the_mapped_one_in_the_mesh(A, G, tmp_path):
    A2 = _area_copy(A, tmp_path)
    f = make_house(tmp_path / "h.ifc", **SITE)
    bid = ib.save(A2, f, ib.check(f, A2, G), "Test house")
    areas.build_mesh(A2)
    meta = json.loads((Path(A2["dir"]) / "cache" / "bld_meta.json").read_text())
    assert [b["ifc"] for b in meta if b.get("ifc")] == [bid]
    assert len(meta) == len(json.loads((Path(A["dir"]) / "cache" / "bld_meta.json").read_text()))   # one in, one out
    assert ib.geometry(A2, bid) and ib.load_all(A2)[0]["name"] == "Test house"
    ib.remove(A2, bid); areas.build_mesh(A2)
    assert not any(b.get("ifc") for b in json.loads((Path(A2["dir"]) / "cache" / "bld_meta.json").read_text()))


@pytest.mark.engine
def test_building_report_on_a_run(A, G, tmp_path, monkeypatch):
    A2 = _area_copy(A, tmp_path)
    f = make_house(tmp_path / "h.ifc", **SITE)
    bid = ib.save(A2, f, ib.check(f, A2, G), "Test house"); areas.build_mesh(A2)
    q, _ = core.validate(A2, dict(depth=60, duration=30, sim=0.75))
    run = tmp_path / "run"; run.mkdir(); monkeypatch.chdir(run)
    forcing = core.write_inp(A2, q, run / "model.inp", "model.h5")
    assert json.loads((run / "ifc_buildings.json").read_text()) == [bid]
    import openswmm.engine as eng
    eng.run_with_callback(str(run / "model.inp"), str(run / "model.rpt"), str(run / "model.out"), lambda frac: None)
    core.postprocess(A2, str(run / "model.h5"), q, forcing, 1.0)
    r = ib.report(A2, run, bid)
    assert "error" not in r, r
    assert len(r["doors"]) == 2 and len(r["times_min"]) == len(r["doors"][0]["series"])
    assert all(d["margin"] is None or abs(d["margin"]) < 5 for d in r["doors"])          # levels on a sane scale
    assert {s["name"] for s in r["spaces"]} == {"G01", "G02", "G03", "F01", "F02"} and r["inside"]["mode"] == "spaces"
    assert all(s["max_depth"] < 0.01 for s in r["spaces"] if s["storey"] == "First Floor")
    other = tmp_path / "old"; other.mkdir(); (other / "ifc_buildings.json").write_text("[]")
    assert "error" in ib.report(A2, other, bid)                                    # runs made before the building


@pytest.mark.engine
def test_open_doors_let_water_in_and_barriers_keep_it_out(A, G, tmp_path, monkeypatch):
    A2 = _area_copy(A, tmp_path)
    f = make_house(tmp_path / "h.ifc", **SITE)
    bid = ib.save(A2, f, ib.check(f, A2, G), "Test house"); areas.build_mesh(A2)
    import openswmm.engine as eng
    depth, walls = {}, {}
    for case, extra in (("open", dict(bld_doors="open")), ("barrier", dict(bld_doors="open", bld_barrier=0.6))):
        q, _ = core.validate(A2, dict(depth=80, duration=60, profile="centre", soil="wet", sim=1.25, **extra))
        run = tmp_path / case; run.mkdir(); monkeypatch.chdir(run)
        forcing = core.write_inp(A2, q, run / "model.inp", "model.h5")
        eng.run_with_callback(str(run / "model.inp"), str(run / "model.rpt"), str(run / "model.out"), lambda frac: None)
        rpt = (run / "model.rpt").read_text()
        assert "maximum depth increased for Node IO" not in rpt               # openings must not lift the outside nodes
        assert max(abs(float(l.split()[-1])) for l in rpt.split("\n") if "Continuity Error (%)" in l) < 0.5
        core.postprocess(A2, str(run / "model.h5"), q, forcing, 1.0)
        r = ib.report(A2, run, bid)
        assert any(d["margin"] is not None and d["margin"] < 0 for d in r["doors"])   # water does rise above a threshold
        depth[case] = max(s["max_depth"] for s in r["spaces"]); walls[case] = r["walls"]
    assert depth["open"] > 0.05 and depth["barrier"] < 0.01
    # with barriers the inside stays dry, so the difference across the walls is the full depth outside above the floor
    assert walls["barrier"]["max_diff"] >= walls["open"]["max_diff"] - 0.01 and walls["barrier"]["max_diff"] < 0.8


def test_door_leakage_follows_the_measured_rates_and_windows_stay_sealed(A, G, tmp_path):
    A2 = _area_copy(A, tmp_path)
    f = make_house(tmp_path / "h.ifc", **SITE)
    ib.save(A2, f, ib.check(f, A2, G), "Test house"); areas.build_mesh(A2)
    def outlets(**kw):
        q, warn = core.validate(A2, dict(depth=30, duration=30, sim=0.5, **kw))
        core.write_inp(A2, q, tmp_path / "m.inp", "m.h5")
        t = (tmp_path / "m.inp").read_text(); sec = t.split("[OUTLETS]")[1].split("[")[0]
        return [l.split() for l in sec.split("\n") if l.startswith("I")], warn
    o, _ = outlets()
    leak = sorted(float(l[5]) for l in o if l[0].startswith("IL"))
    assert len(leak) == 4 and not any(l[0].startswith("IW") for l in o)              # 2 doors, 2 windows; none break
    assert all(l[6] == "1.0" for l in o if l[0].startswith("IL"))                    # linear in depth
    # doors 0.9 m wide: 6,700 L/h per m width per m depth; windows 1.5 m wide at a tenth of that
    assert abs(leak[-1] - 6700 * 0.9 / 3.6e6) < 1e-6 and abs(leak[0] - 0.1 * 6700 * 1.5 / 3.6e6) < 1e-7
    o, _ = outlets(bld_door_type="outward")
    assert max(float(l[5]) for l in o if l[0].startswith("IL")) - 6700 * 0.26 * 0.9 / 3.6e6 < 1e-6
    o, warn = outlets(bld_windows="break")                                           # no height given
    assert not any(l[0].startswith("IW") for l in o) and any("treated as watertight" in w for w in warn)
    o, _ = outlets(bld_windows="break", bld_window_break=0.5)
    assert sum(l[0].startswith("IW") for l in o) == 2                                # both windows can now break
