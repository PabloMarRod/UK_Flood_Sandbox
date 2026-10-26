"""Synthetic survey point clouds for the tests: an 18 x 12 m two-storey house with a pitched roof on the test plot,
the ground around it (from the area's LiDAR, with noise) and a tree, in British National Grid and metres AOD."""
import math
import numpy as np
from ifc_samples import SITE


def house_points(ground_fn, E=SITE["E"], N=SITE["N"], rot=SITE["rot"], spacing=0.15, ffl_up=0.15, seed=1):
    rng = np.random.default_rng(seed)
    a = math.radians(rot); ca, sa = math.cos(a), math.sin(a)
    to_bng = lambda x, y: (E + ca * x - sa * y, N + sa * x + ca * y)
    L, W_, eaves, ridge = 18.0, 12.0, 6.3, 9.0
    g0 = ground_fn(*to_bng(9, 6)); ffl = g0 + ffl_up
    pts, cls = [], []
    # walls: four faces sampled on a grid, from the ground to the eaves
    for (x0, y0, x1, y1) in ((0, 0, L, 0), (L, 0, L, W_), (L, W_, 0, W_), (0, W_, 0, 0)):
        n = int(math.hypot(x1 - x0, y1 - y0) / spacing)
        for t in np.linspace(0, 1, n):
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            for z in np.arange(g0, ffl + eaves, spacing):
                pts.append((x, y, z)); cls.append(6)
    # pitched roof, ridge along the long side
    for x in np.arange(0, L, spacing * 1.5):
        for y in np.arange(0, W_, spacing * 1.5):
            z = ffl + eaves + (ridge - eaves) * (1 - abs(y - W_ / 2) / (W_ / 2))
            pts.append((x, y, z)); cls.append(6)
    # front door step: threshold at the finished floor level
    for x in np.arange(5.6, 6.5, 0.05):
        for y in np.arange(-0.6, 0.0, 0.05):
            pts.append((x, y, ffl)); cls.append(6)
    bld = np.array(pts) + rng.normal(0, 0.01, (len(pts), 3))
    # ground in a 50 x 40 m patch around the house, outside it
    gx, gy = np.meshgrid(np.arange(-16, L + 16, 0.4), np.arange(-14, W_ + 14, 0.4))
    gx, gy = gx.ravel(), gy.ravel(); keep = ~((gx > -0.2) & (gx < L + 0.2) & (gy > -0.7) & (gy < W_ + 0.2))
    gx, gy = gx[keep], gy[keep]
    gxy = np.array([to_bng(x, y) for x, y in zip(gx, gy)])
    gz = np.array([ground_fn(x, y) for x, y in gxy]) + rng.normal(0, 0.02, len(gx))
    # a tree beside the house
    th = rng.uniform(0, 2 * math.pi, 1500); tr = rng.uniform(0, 3, 1500); tz = rng.uniform(4, 10, 1500)
    tx, ty = L + 6 + tr * np.cos(th), W_ / 2 + tr * np.sin(th)
    txy = np.array([to_bng(x, y) for x, y in zip(tx, ty)]); tz = tz + ground_fn(*txy[0])
    bxy = np.array([to_bng(x, y) for x, y in bld[:, :2]])
    xyz = np.vstack([np.c_[bxy, bld[:, 2]], np.c_[gxy, gz], np.c_[txy, tz]])
    cl = np.r_[np.array(cls), np.full(len(gz), 2), np.full(len(tz), 5)].astype(np.uint8)
    return xyz, cl, dict(ffl=ffl, ground=g0, door=to_bng(6.05, -0.3))


def write_las(path, xyz, cls=None, crs=27700, laz=False):
    import laspy
    from pyproj import CRS
    h = laspy.LasHeader(point_format=6, version="1.4")
    h.offsets = xyz.min(axis=0); h.scales = [0.001, 0.001, 0.001]
    if crs: h.add_crs(CRS.from_epsg(crs))
    las = laspy.LasData(h); las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if cls is not None: las.classification = cls
    las.write(str(path)); return path


def write_e57(path, xyz):
    import pye57
    e = pye57.E57(str(path), mode="w")
    e.write_scan_raw({"cartesianX": xyz[:, 0], "cartesianY": xyz[:, 1], "cartesianZ": xyz[:, 2]})
    e.close(); return path


def house_mesh(ground_z=0.0, ffl_up=0.15, partial=False):
    """The test house as a mesh in local metres, Z-up, origin at its south-west corner, with a flat ground patch.
    partial: only the front wall and the ground in front of it (a phone scan of one side)."""
    import trimesh
    L, W_, eaves, ridge = 18.0, 12.0, 6.3, 9.0
    parts = []
    if partial:
        wall = trimesh.creation.box(extents=[L, 0.3, eaves]); wall.apply_translation([L / 2, 0.15, ground_z + eaves / 2]); parts.append(wall)
        g = trimesh.creation.box(extents=[L + 6, 8, 0.05]); g.apply_translation([L / 2, -4, ground_z - 0.025]); parts.append(g)
    else:
        body = trimesh.creation.box(extents=[L, W_, eaves + ffl_up]); body.apply_translation([L / 2, W_ / 2, ground_z + (eaves + ffl_up) / 2]); parts.append(body)
        z0 = ground_z + ffl_up + eaves
        v = np.array([[0, 0, z0], [L, 0, z0], [L, W_, z0], [0, W_, z0], [0, W_ / 2, ground_z + ffl_up + ridge], [L, W_ / 2, ground_z + ffl_up + ridge]])
        roof = trimesh.Trimesh(v, [[0, 1, 5], [0, 5, 4], [3, 4, 5], [3, 5, 2], [0, 4, 3], [1, 2, 5]]); parts.append(roof)
        step = trimesh.creation.box(extents=[0.9, 0.6, ffl_up]); step.apply_translation([6.05, -0.3, ground_z + ffl_up / 2]); parts.append(step)
        g = trimesh.creation.box(extents=[L + 32, W_ + 28, 0.05]); g.apply_translation([L / 2, W_ / 2, ground_z - 0.025])
        hole = trimesh.creation.box(extents=[L + 0.4, W_ + 1.2, 1]); hole.apply_translation([L / 2, W_ / 2 - 0.4, ground_z])
        try:
            g = g.difference(hole)
        except Exception:
            pass
        parts.append(g)
    m = trimesh.util.concatenate(parts)
    m.visual.vertex_colors = np.tile([200, 190, 175, 255], (len(m.vertices), 1))
    return m


def to_site(m, E=SITE["E"], N=SITE["N"], rot=SITE["rot"], z=0.0):
    a = math.radians(rot); T = np.eye(4); T[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]; T[:3, 3] = [E, N, z]
    m = m.copy(); m.apply_transform(T); return m


def y_up(m):
    m = m.copy(); T = np.eye(4); T[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]; m.apply_transform(T); return m


def write_text(path, xyz, rgb=None, kind="xyz"):
    cols = xyz if rgb is None else np.c_[xyz, (np.asarray(rgb) * 255).round()]
    with open(path, "w") as f:
        if kind == "pts":
            f.write(f"{len(xyz)}\n"); cols = np.c_[xyz, np.zeros(len(xyz)), (np.asarray(rgb) * 255).round()] if rgb is not None else xyz
        if kind == "csv":
            f.write("x,y,z" + (",red,green,blue" if rgb is not None else "") + "\n")
        np.savetxt(f, cols, fmt="%.3f", delimiter="," if kind == "csv" else " ")
    return path
