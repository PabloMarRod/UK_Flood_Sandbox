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


@pytest.mark.parametrize("bad", [dict(depth=-1), dict(depth=450), dict(sim=0.1), dict(sim=73)])
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


def test_custom_rain_pattern(preset):
    rain = [0, 12, 60, 120, 30, 6]                                   # mm/h in each 5 minutes
    q, _ = core.validate(preset, base(profile="custom", rain=rain, depth=999, duration=999, uplift=20))
    assert q["duration"] == 30 and q["depth"] == 19.0                 # both follow the pattern, whatever was sent
    assert core.hyetograph(q).tolist() == pytest.approx([v * 1.2 for v in rain])


@pytest.mark.parametrize("rain,msg", [(None, "Draw or paste"), ([], "Draw or paste"), (["a"], "numbers"),
                                      ([10, -1], "between 0 and"), ([10, 301], "between 0 and"),
                                      ([1] * 865, "at most"), ([300] * 60, "Rain must be between")])
def test_custom_rain_pattern_checks(preset, rain, msg):
    with pytest.raises(ValueError, match=msg):
        core.validate(preset, base(profile="custom", rain=rain))


def test_2012_pattern_no_longer_offered(preset):
    with pytest.raises(ValueError, match="unknown storm profile"):
        core.validate(preset, base(profile="obs2012"))


def test_custom_rain_hourly_steps(preset):
    q, _ = core.validate(preset, base(profile="custom", rain=[2, 6, 4], rain_step=60, sim=4))
    assert q["duration"] == 180 and q["depth"] == 12.0
    h = core.hyetograph(q)
    assert len(h) == 36 and h[:12].tolist() == [2.0] * 12 and h[12:24].tolist() == [6.0] * 12


def test_custom_rain_step_checked(preset):
    with pytest.raises(ValueError, match="every 5, 15 or 60 minutes"):
        core.validate(preset, base(profile="custom", rain=[2, 6], rain_step=10))


def test_long_events_accepted(preset):
    q, _ = core.validate(preset, base(depth=120, duration=2880, sim=60, inflow="none"))
    assert q["sim"] == 60 and q["duration"] == 2880


def test_quiet_hours_cost_less():
    q = dict(sim=10.0, depth=30, duration=60, inflow="base", rivers=[])
    assert core.effective_hours(q) == pytest.approx(2 + 8 * core.QUIET_W)        # storm hour, hour after, 8 quiet
    q["burst"], q["burst_start"], q["burst_dur"] = True, 300, 60
    assert core.effective_hours(q) == pytest.approx(4 + 6 * core.QUIET_W)        # plus the burst and the hour after it


def test_runs_over_an_hour_refused(preset, monkeypatch):
    monkeypatch.setattr(core, "k_per_cell", lambda: 1.0)
    with pytest.raises(ValueError, match="over the 60 minute limit"):
        core.validate(preset, base())


def test_save_interval_scales_with_length(preset, tmp_path):
    for sim, rep in ((2, 5), (8, 10), (24, 15), (72, 30)):
        q, _ = core.validate(preset, base(sim=sim, inflow="none"))
        core.write_inp(preset, q, tmp_path / "m.inp", "m.h5")
        assert f"REPORT_STEP 00:{rep:02d}:00" in (tmp_path / "m.inp").read_text()


def test_recorded_river_flows():
    q = dict(sim=2.0, rivers=[])
    r = dict(mode="recorded", values=[1.0, 4.0, 2.0], step=15, base=1.0, peak=4.0)
    t, v = core.river_series(q, r)
    assert v[0] == 1.0 and v[3] == 4.0 and v[6] == 2.0 and v[-1] == 2.0          # every 15 min, held after the record ends
    assert v[1] == pytest.approx(2.0) and v[4] == pytest.approx(10 / 3)               # interpolated in between
    assert core.effective_hours(dict(q, rivers=[r], depth=0, duration=60)) == 2.0


def test_basin_has_a_level_floor_on_a_slope():
    """A basin on sloping ground holds its area times its depth: its floor is level, below the lowest edge."""
    H = 5.0
    xs, ys = np.meshgrid(np.arange(0, 200, H), np.arange(0, 100, H))
    vxy = np.c_[xs.ravel(), ys.ravel()]; ground = 50 + 0.02 * vxy[:, 0]          # 2% slope rising east
    vz = core.apply_measures({"H": H}, vxy, ground.copy(), [dict(type="basin", pts=[[50, 20], [150, 20], [150, 80], [50, 80]], depth=1.5, bund=0)])
    inside = (vxy[:, 0] > 50) & (vxy[:, 0] < 150) & (vxy[:, 1] > 20) & (vxy[:, 1] < 80)
    floor = vz[inside]
    assert floor.max() - floor.min() < 1e-9                                      # level
    edge_low = ground[(np.abs(vxy[:, 0] - 50) <= H * 1.2) & (vxy[:, 0] <= 50)].min()
    assert floor[0] == pytest.approx(edge_low - 1.5)                             # depth below the lowest edge
