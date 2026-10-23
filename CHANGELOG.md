# Changelog

All notable changes are recorded here. The project follows semantic versioning once it reaches 1.0.

## Unreleased

- Drainage networks: import sewers or drains from a SWMM file, GeoPackage, shapefiles or an ArcGIS file
  geodatabase, with quality checks that stop the import or offer repairs the user must accept, coupled to the 2D
  surface through each manhole. Covers can be solid (gully in, cover lifts out), grated (both ways) or sealed,
  from the data or a scenario default, and each manhole stores water over its chamber area. SWMM files may also
  hold weirs, orifices, outlets, pumps and storage units, and outfalls keep their flap gates.
- Fixed: the NetCDF time series was left incomplete with NumPy 2 (hazard class and global attributes missing).

## 0.1.0 (first public release)

First release: drawn areas anywhere in the UK; rain, river, burst main, culvert and tidal scenarios; 26 estuaries
(four calibrated against tide gauges); user-placed defences, sustainable drainage measures, storage tanks, pumps
and timed gates; 3D view with buildings and trees; hazard, buildings, roads, key sites and indicative damage;
comparison with the EA surface-water map; PDF report, MP4 video, GeoTIFF, GeoPackage and CF NetCDF outputs;
size and run-time guidance from a first-start speed test.
