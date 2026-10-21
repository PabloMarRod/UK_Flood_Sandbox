"""Comparison with the Environment Agency's national Risk of Flooding from Surface Water map (NaFRA2, England).

The map is fetched as an image aligned with the area's model grid (one pixel per cell) and read by colour into
likelihood bands: 3 High (3.3% or more chance each year), 2 Medium (1 to 3.3%), 1 Low (0.1 to 1%).
Layers: "rofsw" (any depth) and depth thresholds rofsw_0_2m_depth ... rofsw_1_2m_depth.
(c) Environment Agency copyright and/or database right 2025.
"""
import io, json
from pathlib import Path
import numpy as np

WMS = "https://environment.data.gov.uk/spatialdata/nafra2-risk-of-flooding-from-surface-water/wms"
LAYERS = {"rofsw": 0.1, "rofsw_0_2m_depth": 0.2, "rofsw_0_3m_depth": 0.3, "rofsw_0_6m_depth": 0.6,
          "rofsw_0_9m_depth": 0.9, "rofsw_1_2m_depth": 1.2}       # layer -> model depth threshold used to compare
BANDS = {3: (85, 91, 157), 2: (154, 159, 222), 1: (195, 224, 255)}


def classes(A, layer="rofsw"):
    import requests
    from PIL import Image
    if layer not in LAYERS: raise ValueError("unknown layer")
    f = Path(A["dir"]) / "cache" / f"{layer}.npy"
    if f.exists(): return np.load(f)
    H, X0, Y1, NX, NY = A["H"], A["X0"], A["Y1"], A["NX"], A["NY"]
    r = requests.get(WMS, params=dict(service="WMS", version="1.3.0", request="GetMap", layers=layer, styles="",
                                      crs="EPSG:27700", bbox=f"{X0},{Y1 - NY * H},{X0 + NX * H},{Y1}",
                                      width=NX, height=NY, format="image/png", transparent="true"), timeout=180)
    if not r.ok or not r.headers.get("content-type", "").startswith("image"):
        raise RuntimeError(f"the EA map service did not return an image ({r.status_code})")
    a = np.array(Image.open(io.BytesIO(r.content)).convert("RGBA")).astype(int)
    out = np.zeros(a.shape[:2], np.uint8); solid = a[..., 3] > 128
    best = np.full(a.shape[:2], 1e9)
    for k, rgb in BANDS.items():
        dd = ((a[..., :3] - np.array(rgb)) ** 2).sum(-1)
        m = solid & (dd < best); out[m] = k; best[m] = dd[m]
    out[solid & (best > 3000)] = 0                     # other colours (e.g. 'unavailable') are not counted
    np.save(f, out)
    return out


def compare(A, result, layer="rofsw", band=1):
    """Agreement between a run's maximum depth and the EA map: cells wet in the model (deeper than the layer's
    threshold) against cells in the EA map at the chosen likelihood or more likely. Rivers, ponds and their edges
    are left out, as in the run's statistics."""
    import base64
    from scipy import ndimage
    ea = classes(A, layer) >= band
    c = result["cells"]; md = np.frombuffer(base64.b64decode(c["maxd_u8"]), np.uint8).reshape(c["NY"], c["NX"]) * 0.02
    m = np.load(Path(A["dir"]) / "cache" / "mesh.npz")
    excl = (m["is_tyne"] | m["is_water"]) if "is_water" in m else m["is_tyne"]
    excl = ndimage.binary_dilation(excl, iterations=max(1, round(15 / A["H"]))) | ~m["keep"]
    model = (md > LAYERS[layer]) & ~excl; ea = ea & ~excl
    cell = A["H"] ** 2 / 1e4
    hit, miss, extra = (model & ea).sum(), (~model & ea).sum(), (model & ~ea).sum()
    csi = hit / (hit + miss + extra) if hit + miss + extra else None
    return dict(layer=layer, band=band, threshold=LAYERS[layer], ea_ha=round(float(ea.sum() * cell), 2),
                model_ha=round(float(model.sum() * cell), 2), both_ha=round(float(hit * cell), 2),
                csi=None if csi is None else round(float(csi), 3),
                model_in_ea=round(float(hit / model.sum()), 3) if model.sum() else None,
                ea_in_model=round(float(hit / ea.sum()), 3) if ea.sum() else None)
