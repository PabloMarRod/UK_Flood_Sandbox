"""GIS export in another coordinate system: code checks and grid reprojection."""
import numpy as np
import pytest
import rasterio

import export

# a 300 m square in Newcastle on a 3 m grid
A = dict(H=3, X0=426000.0, Y1=565300.0, poly_bng=[(426000, 565000), (426300, 565000), (426300, 565300), (426000, 565300)])


@pytest.mark.parametrize("code,want", [(27700, 27700), ("25830", 25830), ("EPSG:4326", 4326), (7405, 27700)])
def test_target_crs_accepts_map_systems_for_this_place(code, want):
    assert export.target_crs(code, A)[0] == want


@pytest.mark.parametrize("code,msg", [("abc", "isn't an EPSG code"), (999999, "isn't a known"),
                                      (5701, "isn't a 2D map"), (2157, "isn't meant for this location")])
def test_target_crs_rejects(code, msg):
    with pytest.raises(ValueError, match=msg):
        export.target_crs(code, A)


@pytest.mark.parametrize("epsg", [27700, 25830, 4326])
def test_grid_keeps_peak_and_classes(tmp_path, epsg):
    rng = np.random.default_rng(1)
    depth = rng.uniform(0, 0.5, (100, 100)).astype("float32"); depth[37, 61] = 1.87; depth[:5, :5] = np.nan
    haz = rng.integers(0, 4, (100, 100)).astype("uint8"); haz[50, 50] = 4
    export._write_grid(tmp_path / "d.tif", depth, "float32", np.nan, A, epsg)
    export._write_grid(tmp_path / "h.tif", haz, "uint8", 255, A, epsg)
    with rasterio.open(tmp_path / "d.tif") as r:
        assert r.crs.to_epsg() == epsg and abs(float(np.nanmax(r.read(1))) - 1.87) < 1e-6
    with rasterio.open(tmp_path / "h.tif") as r:
        assert set(np.unique(r.read(1)).tolist()) <= {0, 1, 2, 3, 4, 255} and 4 in r.read(1)
