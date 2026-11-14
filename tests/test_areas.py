import pytest

import areas


@pytest.mark.parametrize("v, want", [(1, 1), (5, 5), (10, 10), ("7", 7), (3.0, 3)])
def test_cell_size_accepts_whole_metres_1_to_10(v, want):
    assert areas.cell_size(v) == want


@pytest.mark.parametrize("v", [0, 11, 2.5, "abc", None, -5])
def test_cell_size_rejects_others(v):
    with pytest.raises(ValueError):
        areas.cell_size(v)


def test_new_area_rejects_bad_cell_size():
    poly = [(54.99, -1.60), (54.99, -1.59), (54.98, -1.59), (54.98, -1.60)]
    with pytest.raises(ValueError, match="cell size"):
        areas.new_area("t", poly, 12)
