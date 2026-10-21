"""Fast checks of scenario validation, mesh edits for defences, green roofs and damage curves."""
import numpy as np
import pytest
import areas, core


@pytest.fixture(scope="module")
def preset():
    return areas.load("jesmond-vale")


def base(**kw):
    p = dict(depth=30, duration=60, profile="centre", sim=1.0, inflow="base", base_q=2)
    p.update(kw)
    return p


def test_valid_scenario(preset):
    q, warn = core.validate(preset, base())
    assert q["depth"] == 30 and q["est_s"] > 0


@pytest.mark.parametrize("bad", [dict(depth=-1), dict(depth=250), dict(sim=0.1), dict(sim=9)])
def test_out_of_range(preset, bad):
    with pytest.raises(ValueError):
        core.validate(preset, base(**bad))


def test_measures_are_cleaned(preset):
    x, y = preset["poly_bng"][0]
    ms = core.check_measures(preset, [
        dict(type="wall", pts=[[x + 50, y + 50], [x + 150, y + 50]], height=99),      # height clipped to 6 m
        dict(type="unknown", pts=[[x, y], [x + 10, y]]),                              # dropped
        dict(type="basin", pts=[[x, y], [x + 10, y]]),                                # too few points: dropped
        dict(type="tank", pts=[[x + 60, y + 60]], volume=1000)])
    assert [m["type"] for m in ms] == ["wall", "tank"]
    assert ms[0]["height"] == 6.0


def test_wall_and_basin_change_ground(preset):
    H = preset["H"]; x0, y0 = preset["X0"], preset["Y1"] - 200
    xs, ys = np.meshgrid(np.arange(0, 100, H), np.arange(0, 100, H))
    vxy = np.c_[xs.ravel() + x0, ys.ravel() + y0]; vz = np.full(len(vxy), 10.0)
    wall = dict(type="wall", pts=[[x0, y0 + 50], [x0 + 100, y0 + 50]], height=1.5)
    basin = dict(type="basin", pts=[[x0 + 10, y0 + 10], [x0 + 40, y0 + 10], [x0 + 40, y0 + 30], [x0 + 10, y0 + 30]], depth=2.0, bund=0)
    z = core.apply_measures(preset, vxy, vz.copy(), [wall, basin])
    assert np.isclose(z.max(), 11.5) and np.isclose(z.min(), 8.0)
    assert (z == 10.0).sum() > len(z) / 2                          # most of the ground untouched


def test_green_roof_holds_small_storms():
    q = dict(measures=[dict(type="greenroof", retention=15)])
    small = core._green_roof_runoff([10.0] * 12, q)                # 10 mm over an hour: all held
    big = core._green_roof_runoff([30.0] * 12, q)                  # 30 mm: 15 mm held, the rest released slowly
    dt = core.STEP_MIN / 60
    assert small.sum() * dt < 0.01
    released = big.sum() * dt                                      # within the hour after the rain stops;
    assert 12.0 < released <= 15.0                                 # the remainder is still on the roof
    assert big[:4].max() < big.max()                               # delayed, not passed straight through


def test_jrc_curves():
    assert core.JRC_EUROPE["residential"][core.JRC_DEPTH.index(1)] == 0.40
    assert core.JRC_UK_MAX["residential"] == pytest.approx(758.12)
    for curve in core.JRC_EUROPE.values():
        assert curve[0] == 0 and curve[-1] == 1 and all(a <= b for a, b in zip(curve, curve[1:]))
