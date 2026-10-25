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
- IFC buildings: add a building from an IFC model (panel link, or click a mapped building). Georeferenced models
  land in place; others are dragged and rotated on the map. Checks stop unusable files and offer repairs (levels,
  units). The footprint replaces the mapped buildings it overlaps, the model shows in 3D, and each run gets a
  Building tab: water at each door against its threshold and against the finished floor level, and the spaces at
  risk with indicative damage. Water inside is modelled room by room (or per storey): it gets in through outside
  doors (leaking when closed at rates measured in University of Hull tests of UK doors, over the full width when
  open or once they give way at a set height, held back by door barriers) and windows (leaking above the sill at a
  tenth of the door rate, and breaking only at a height the user sets, after Kelman 2002), spreads through
  internal doors and drains out again. The report flags when the water outside rises more than 0.8 m above the
  water inside, where cavity walls may start to fail (Kelman 2002).
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
