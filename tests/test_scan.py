"""Buildings from survey point clouds: formats, classification, levels, checks, marked openings, and a run."""
import json, shutil
from pathlib import Path
import numpy as np
import pytest
import areas, core, ifcbuilding as ib, scanbuilding as sb, sewer
from scan_samples import house_points, write_las, write_e57


@pytest.fixture(scope="module")
def A():
    return areas.load("jesmond-vale")


@pytest.fixture(scope="module")
def G(A):
    return sewer.Ground(A)


@pytest.fixture(scope="module")
def cloud(G):
    return house_points(lambda x, y: G.at(x, y, 3) or 38.6)


def _codes(r, sev=None):
    return {i["code"] for i in r["issues"] if sev is None or i["sev"] == sev}


@pytest.mark.parametrize("fmt", ["las", "laz", "unclassified", "e57"])
def test_formats_give_the_house(A, G, cloud, tmp_path, fmt):
    xyz, cl, info = cloud
    f = {"las": lambda: write_las(tmp_path / "h.las", xyz, cl), "laz": lambda: write_las(tmp_path / "h.laz", xyz, cl),
         "unclassified": lambda: write_las(tmp_path / "u.las", xyz, None), "e57": lambda: write_e57(tmp_path / "h.e57", xyz)}[fmt]()
    r = sb.check(f, A, G)
    assert r["status"] == "ok", r["issues"]
    b = r["building"]
    assert 200 < b["area"] < 240 and abs(b["height"] - 9.2) < 0.4 and len(b["replaces"]) == 1      # 18 x 12 m, ridge 9 m
    assert abs(b["ground_min"] - info["ground"]) < 0.3 and "W_LEVELS_OK" in _codes(r)
    assert ("W_CLASSIFIED" in _codes(r)) == (fmt in ("las", "laz"))
    assert ("W_CRS_ASSUMED" in _codes(r)) == (fmt == "e57")                  # E57 records no coordinate system


def test_a_level_offset_is_offered_a_repair(A, G, cloud, tmp_path):
    xyz, cl, info = cloud
    f = write_las(tmp_path / "off.las", xyz + [0, 0, 5.0], cl)                  # heights 5 m too high
    r = sb.check(f, A, G)
    assert r["status"] == "needs_fixes" and "F_LEVELS" in _codes(r, "fix")
    r = sb.check(f, A, G, accepted=["F_LEVELS"])
    assert r["status"] == "ok" and abs(r["building"]["ground_min"] - info["ground"]) < 0.3


@pytest.mark.parametrize("case,code", [("outside", "E_OUTSIDE"), ("few", "E_EMPTY"), ("notacloud", "E_READ")])
def test_blocking_problems(A, G, cloud, tmp_path, case, code):
    xyz, cl, _ = cloud
    if case == "outside": f = write_las(tmp_path / "o.las", xyz + [20000, 0, 0], cl)
    elif case == "few": f = write_las(tmp_path / "f.las", xyz[:500], cl[:500])
    else: f = tmp_path / "x.las"; f.write_text("not a point cloud")
    r = sb.check(f, A, G)
    assert r["status"] == "blocked" and code in _codes(r, "error")


def test_local_scan_needs_placement(A, G, cloud, tmp_path):
    xyz, cl, _ = cloud
    loc = xyz - [xyz[:, 0].mean(), xyz[:, 1].mean(), 0]
    f = write_las(tmp_path / "l.las", loc, cl, crs=None)
    r = sb.check(f, A, G)
    assert r["status"] == "needs_placement" and r["local"]["area"] > 150
    r = sb.check(f, A, G, placement=dict(E=float(xyz[:, 0].mean()), N=float(xyz[:, 1].mean()), rot=0))
    assert r["status"] == "ok"


def _area_copy(A, tmp_path):
    d = tmp_path / "area"; shutil.copytree(A["dir"], d, ignore=shutil.ignore_patterns("ifc*", "sewer*", "data"))
    (d / "data").mkdir()
    for f in ("osm.json", "dtm.tif"): shutil.copy(Path(A["dir"]) / "data" / f, d / "data" / f)
    return dict(A, dir=str(d))


def test_saved_scan_replaces_the_mapped_building_and_takes_openings(A, G, cloud, tmp_path):
    xyz, cl, info = cloud
    A2 = _area_copy(A, tmp_path)
    f = write_las(tmp_path / "h.laz", xyz, cl)
    bid = sb.save(A2, f, sb.check(f, A2, G), "Scanned house"); areas.build_mesh(A2)
    meta = json.loads((Path(A2["dir"]) / "cache" / "bld_meta.json").read_text())
    assert [b["ifc"] for b in meta if b.get("ifc")] == [bid]
    assert ib.geometry(A2, bid)[0]["pts"]
    b = sb.set_openings(A2, bid, [dict(kind="door", x=info["door"][0], y=info["door"][1], sill=info["ffl"], width=0.9),
                                   dict(kind="window", x=info["door"][0] + 3, y=info["door"][1], sill=info["ffl"] + 0.9, width=1.2, height=1.2)])
    assert b["ffl"] == round(info["ffl"], 3) and len(b["doors"]) == 1 and len(b["windows"]) == 1


@pytest.mark.engine
def test_water_gets_into_a_scanned_house_through_a_marked_door(A, G, cloud, tmp_path, monkeypatch):
    xyz, cl, info = cloud
    A2 = _area_copy(A, tmp_path)
    f = write_las(tmp_path / "h.laz", xyz, cl)
    bid = sb.save(A2, f, sb.check(f, A2, G), "Scanned house"); areas.build_mesh(A2)
    sb.set_openings(A2, bid, [dict(kind="door", x=info["door"][0], y=info["door"][1], sill=info["ffl"] - 0.12, width=0.9)])
    q, _ = core.validate(A2, dict(depth=80, duration=60, profile="centre", soil="wet", sim=1.25, bld_doors="open"))
    run = tmp_path / "run"; run.mkdir(); monkeypatch.chdir(run)
    forcing = core.write_inp(A2, q, run / "model.inp", "model.h5")
    import openswmm.engine as eng
    eng.run_with_callback(str(run / "model.inp"), str(run / "model.rpt"), str(run / "model.out"), lambda frac: None)
    rpt = (run / "model.rpt").read_text()
    assert max(abs(float(l.split()[-1])) for l in rpt.split("\n") if "Continuity Error (%)" in l) < 0.5
    core.postprocess(A2, str(run / "model.h5"), q, forcing, 1.0)
    r = ib.report(A2, run, bid)
    assert r["inside"]["mode"] == "storeys" and len(r["doors"]) == 1 and r["walls"] is not None


from scan_samples import house_mesh, to_site, y_up, write_text, SITE


@pytest.fixture(scope="module")
def centre(cloud):
    xyz, cl, _ = cloud
    return dict(E=float(xyz[cl == 6][:, 0].mean()), N=float(xyz[cl == 6][:, 1].mean()), rot=SITE["rot"])


def _accept_all(f, A, G, **kw):
    r = sb.check(f, A, G, **kw)
    if r["status"] == "needs_fixes":
        r = sb.check(f, A, G, accepted=[i["code"] for i in r["issues"] if i["sev"] == "fix"], **kw)
    return r


@pytest.mark.parametrize("kind", ["xyz", "pts", "csv", "ply"])
def test_text_and_ply_point_clouds(A, G, cloud, tmp_path, kind):
    import trimesh
    xyz, cl, info = cloud
    rgb = np.tile([0.8, 0.75, 0.7], (len(xyz), 1))
    if kind == "ply": f = tmp_path / "h.ply"; trimesh.PointCloud(xyz).export(str(f))
    else: f = write_text(tmp_path / f"h.{kind}", xyz, None if kind == "xyz" else rgb, kind)
    r = sb.check(f, A, G)
    assert r["status"] == "ok" and 200 < r["building"]["area"] < 240 and abs(r["building"]["ground_min"] - info["ground"]) < 0.3


def test_georeferenced_obj(A, G, cloud, tmp_path):
    f = tmp_path / "geo.obj"; to_site(house_mesh(ground_z=cloud[2]["ground"])).export(str(f))
    r = sb.check(f, A, G)
    assert r["status"] == "ok" and "W_MESH" in _codes(r) and 200 < r["building"]["area"] < 240


def test_phone_scan_is_placed_then_levelled_on_the_lidar(A, G, cloud, centre, tmp_path):
    f = tmp_path / "phone.glb"; y_up(house_mesh()).export(str(f))                  # local, Y-up, no real heights
    assert sb.check(f, A, G)["status"] == "needs_placement"
    r = sb.check(f, A, G, placement=centre)
    assert r["status"] == "needs_fixes" and "F_LEVELS" in _codes(r, "fix")
    r = _accept_all(f, A, G, placement=centre)
    b = r["building"]
    assert r["status"] == "ok" and abs(b["ground_min"] - cloud[2]["ground"]) < 0.3 and abs(b["height"] - 9.2) < 0.4


def test_millimetre_mesh_and_partial_scan_are_offered_repairs(A, G, cloud, centre, tmp_path):
    m = house_mesh(); m.apply_scale(1000); f = tmp_path / "mm.obj"; m.export(str(f))
    r = sb.check(f, A, G, placement=centre)
    assert "F_UNITS" in _codes(r, "fix") and _accept_all(f, A, G, placement=centre)["status"] == "ok"
    f = tmp_path / "part.glb"; y_up(house_mesh(partial=True)).export(str(f))
    r = sb.check(f, A, G, placement=centre)
    assert "F_PARTIAL" in _codes(r, "fix")
    r = _accept_all(f, A, G, placement=centre)
    assert r["status"] == "ok" and r["building"]["area"] > 120                       # the mapped outline, not one wall
