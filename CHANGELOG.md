# Changelog

All notable changes are recorded here. The project follows semantic versioning once it reaches 1.0.

## Unreleased

- Drainage networks: import sewers or drains from a SWMM file, GeoPackage, shapefiles or an ArcGIS file
  geodatabase, with quality checks that stop the import or offer repairs the user must accept, coupled to the 2D
  surface through each manhole. Covers can be solid (gully in, cover lifts out), grated (both ways) or sealed,
  from the data or a scenario default, and each manhole stores water over its chamber area. SWMM files may also
  hold weirs, orifices, outlets, pumps, storage units and flow dividers, and outfalls keep their flap gates. GIS
  files may add weirs, flow controls, pumps and storage tanks as layers; any missing hydraulic value stops the
  import. SWMM cross-sections keep all their dimensions; custom closed shapes are supported;
  open channels (transects) and US-unit files are refused with an explanation.
- Rivers entering a drawn area can carry up to 3,000 m³/s (was 300), and enter across their channel's width at
  the edge (found from the LiDAR bed level) rather than through one or two cells.
- Fixed: flood defences running beyond an area were drawn in full in the 3D view, as a long line off the edge.
- Fixed: the NetCDF time series was left incomplete with NumPy 2 (hazard class and global attributes missing).

## 0.1.0 (first public release)

First release: drawn areas anywhere in the UK; rain, river, burst main, culvert and tidal scenarios; 26 estuaries
(four calibrated against tide gauges); user-placed defences, sustainable drainage measures, storage tanks, pumps
and timed gates; 3D view with buildings and trees; hazard, buildings, roads, key sites and indicative damage;
comparison with the EA surface-water map; PDF report, MP4 video, GeoTIFF, GeoPackage and CF NetCDF outputs;
size and run-time guidance from a first-start speed test.
