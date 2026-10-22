"""The CF NetCDF time series is written completely, including the hazard class and the global attributes."""
import numpy as np
import netCDF4
import core


def test_write_cf_complete(tmp_path):
    NX, NY, NT = 4, 3, 2
    n = NX * NY
    A = dict(H=5, X0=425000.0, Y1=566000.0, NX=NX, NY=NY, name="Synthetic")
    q = dict(depth=30, duration=60, profile="centre", sim=1.0, measures=[])
    depth = np.full((NT, n), 0.2)
    f = {"Mesh2_face_depth": depth, "Mesh2_face_head": depth + 10.0,
         "Mesh2_face_vx": np.full((NT, n), 0.3), "Mesh2_face_vy": np.full((NT, n), 0.4),
         "Mesh2_face_z": np.full(n, 10.0)}
    grid = lambda v: np.asarray(v, float).reshape(NY, NX)

    MD = np.full((NY, NX), 0.5); MD[0, 0] = np.nan          # one cell outside the model
    MV = np.full((NY, NX), 0.5)
    HZC = np.full((NY, NX), 2, np.uint8)                     # uint8, as postprocess produces it

    path = tmp_path / "time_series.nc"
    core._write_cf(path, A, q, f, grid, [0, 5], MD, MV, HZC)

    with netCDF4.Dataset(str(path)) as nc:
        assert nc.Conventions == "CF-1.8"
        assert "scenario_json" in nc.ncattrs()
        assert nc["depth"].shape == (NT, NY, NX)
        hz = nc["hazard_class"][:]
        assert hz.mask[0, 0]                                 # no-data cell is masked (fill value -1)
        assert int(hz[1, 1]) == 2
