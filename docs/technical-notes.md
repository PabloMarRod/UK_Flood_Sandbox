# UK Flood Sandbox

Flood scenarios for any place in England, Wales, Scotland and Northern Ireland: draw an area, design a scenario (rain, rivers, tides and surges, burst water mains, culvert blockage, ground conditions), run it with the OpenSWMM 6 2D engine on your own computer, and see the flooding on a map. Formerly "Tyneside storm lab"; the Jesmond Vale (Ouseburn) preset is where it started.


## Start

1. Install Python 3.13 (3.10 to 3.12 also work). Python 3.14 is not supported yet by openswmm 6.0.0a4;
   it can stay installed alongside.
2. Run `start_windows.bat` (Windows) or `./start_mac_linux.sh` (macOS/Linux). The first run finds a
   supported Python, creates a private environment in `.venv` and installs the packages. Later runs start straight away.
3. Your browser opens http://localhost:8765. The Jesmond Vale preset and its 2012 replay load first.
4. To model somewhere else, click **New area**, draw a rectangle or polygon, pick 5 m or 10 m cells and click
   **Prepare area**. The terrain and map data are downloaded (internet needed) and the mesh is built, which takes
   about half a minute per km². Then click **Design a scenario**.

Only one drawn area is kept at a time. When a new area is prepared successfully, the previous drawn area (its
terrain, map data and mesh) is deleted; if preparation fails or is cancelled, the previous area stays. The Jesmond
Vale preset is always kept. Simulation results are never deleted: each run folder in `runs/` holds the model input
(`model.inp`), the engine report (`model.rpt`), the parameters, the results (`result.json`) and a copy of the area
outline and map layers (`area.json`, `static_layers.json`), so a run stays complete after its area is gone.

The server only listens on your own machine (127.0.0.1). Keep its window open while you use the page.

## Files

- `server.py`: local web server and job runner (one storm at a time).
- `worker.py`: runs one storm in its own process and reports progress.
- `areas.py`: area outlines, terrain and OpenStreetMap download, mesh building, map layers.
- `estuary.py`: estuary registry, geometry, boundary data and the 1D estuary model.
- `calibrate.py`: compare an estuary model with tide gauges and calibrate it.
- `os_data.py`: Ordnance Survey open data readers (OS Open Rivers, OS OpenMap Local).
- `prepare.py`: prepares a newly drawn area in its own process.
- `core.py`: scenario checks, storm, inflow and burst series, model input, result maps and statistics.
- `static/index.html`: the web page.
- `areas/<id>/`: the preset and the current drawn area, each with its definition (`area.json`), downloaded data (`data/`) and cached
  mesh, map layers and baseline (`cache/`). `areas/jesmond-vale` is the preset.
- `data/`: EA gauge records for 28 June 2012 (used by the preset and the "2012 pattern" storm shape).
  `fetch_inputs.py` downloads the preset's inputs again from the original sources.
- `runs/`: one folder per simulation (see above); kept permanently.

## Compute budget

Run time grows with the number of cells times the simulated hours. While drawing, the app shows the area, the
approximate cell count and seconds per simulated hour (green, amber, red). Limits: 250,000 cells per area and
about 10 minutes per run. The speed figure starts from a development estimate and is recalibrated on your computer
after every run (`runs/calibration.json`).

## Tidal estuaries

Registered estuaries live in `data/estuaries/<id>/` (currently `tyne` and `thames`). When a drawn area overlaps an
estuary's water outline, a Tide tab appears and two models run:

1. A 1D SWMM model of the estuary from its tidal limit to its tide gauge (`estuary.py`; input and report saved as
   `estuary.inp` / `estuary.rpt` in the run folder). River flow enters at the tidal limit; gauged tributaries enter
   at their confluences; the tide sets the level at the mouth node. A 24-hour spin-up precedes each scenario.
2. The 2D area model, with the 1D water levels applied as time-varying water-level boundaries where the area's
   outline crosses the river. The river cells start at the tide level.

| Estuary | Reach | Tide boundary | River flow | Check gauges | Status |
|---|---|---|---|---|---|
| Tidal Thames | Teddington to Tilbury, 68.7 km | Tilbury | Kingston | Richmond, Westminster, Tower Pier, Charlton, Silvertown | calibrated |
| Humber Estuary | Ouse from Naburn Lock and Trent from Cromwell Lock to Immingham, 198 km (branched) | Immingham | Skelton, North Muskham, Tadcaster, Buttercrambe, Beal, Doncaster | South Ferriby, Brough (high water), Keadby; Hull, Goxhill, New Holland when reporting | calibrated |
| Severn Estuary | Gloucester to Avonmouth, 78 km | Avonmouth | Deerhurst | Oldbury (high water only) | partly calibrated |
| Exe Estuary | Trews Weir to Exmouth, 14.9 km | Exmouth | Thorverton | Lympstone (high water only) | partly calibrated |
| Tidal Tyne | Wylam to North Shields, 30.6 km | North Shields | Bywell (+ Derwent, Team, Ouse Burn) | none | assumed bed |
| Tidal Tees | Tees Barrage to Tees Dock, 11.5 km | Tees Dock | Low Moor | none | assumed bed |
| Tidal Great Ouse | Denver Sluice to King's Lynn, 20.7 km | King's Lynn | Offord (rough proxy; Denver Sluice not modelled) | none | assumed bed |
| Mersey Estuary | Howley Weir to Liverpool, 49.6 km | Liverpool | Westy | none | experimental |
| Usk Estuary (Wales) | Newbridge-on-Usk to Newport, 28.7 km | Newport | NRW (key needed; default flow otherwise) | none | assumed bed |
| Dee Estuary | Chester Weir to the estuary mouth, 32.3 km | Liverpool (16.7 km away) | NRW / default | none | assumed bed |
| Conwy Estuary | Llanrwst to Deganwy, 23.8 km | Llandudno (3.9 km away) | NRW / default | none | assumed bed |
| Clwyd Estuary | Rhuddlan to Rhyl, 5.0 km | Llandudno (21.8 km away) | NRW / default | none | assumed bed |
| Mawddach Estuary | Llanelltyd to Barmouth, 14.0 km | Barmouth | NRW / default | none | assumed bed |
| Dyfi Estuary | Derwenlas to Aberdyfi, 18.5 km | Barmouth (19.7 km away) | NRW / default | none | assumed bed |
| Teifi Estuary | Llechryd to Cardigan, 10.1 km | Fishguard (22.5 km away) | NRW / default | none | assumed bed |
| Tywi Estuary | Carmarthen to Ferryside, 15.7 km | Mumbles (35 km away; gauge currently offline) | NRW / default | none | assumed bed |
| Loughor Estuary | Pontarddulais to Burry Port, 31.3 km | Mumbles (16.5 km away; offline) | NRW / default | none | assumed bed |
| Neath Estuary | Neath to Baglan, 7.4 km | Mumbles (10.3 km away; offline) | NRW / default | none | assumed bed |
| Daugleddau | Haverfordwest to Milford Haven, 33.1 km | Milford Haven | NRW / default | none | assumed bed |
| Tidal Wye | Bigsweir to the Severn, 27.0 km | Avonmouth (13.6 km away) | not yet connected / default | none | assumed bed |

| Forth Estuary (Scotland) | Stirling (Craigforth) to Leith, 55.1 km | Leith | SEPA Craigforth | none | assumed bed |
| Clyde Estuary (Scotland) | Glasgow Tidal Weir to the Firth of Clyde, 35.6 km | Millport (24 km away) | SEPA Daldowie | none | assumed bed |
| Tay Estuary (Scotland) | Perth to Broughty Ferry, 45.6 km | Leith (55.9 km away: a weak stand-in) | SEPA Ballathie | none | assumed bed |
| Aberdeen Dee (Scotland) | Bridge of Dee to Aberdeen harbour, 3.8 km | Aberdeen | SEPA Park | none | assumed bed |

| Tidal Lagan (Northern Ireland) | Lagan Weir to Belfast harbour, 4.4 km | Bangor (14 km away) | none (default flow) | none | experimental |
| Foyle Estuary (Northern Ireland) | Strabane/Lifford to Culmore Point, 33.6 km | Portrush (42 km away) | none (default flow) | none | experimental |

Not modelled as estuaries because barrages impound them: the Taff and Ely (Cardiff Bay barrage) and the Tawe
(Swansea). Where a tide gauge is some distance from the mouth ("stand-in gauge"), the Tide tab says so; recorded
tides there have the right shape but may be early or late and slightly too high or low. Tidal limit positions for
the Welsh estuaries are approximate.

Status meanings (shown in the Tide tab): calibrated = bed and roughness fitted to gauges along the estuary and
checked on a separate period; partly calibrated = fitted to high water at one gauge; assumed bed = not checked
against any gauge up the estuary; experimental = the rectangular 1D channel represents the estuary's wide
intertidal flats only roughly (the Mersey model raises high water at Warrington about 1.6 m above Liverpool,
which is likely too much).

Tide options: synthetic (high and low water in mAOD, time of first high water, 12.42-hour period) or recorded from a
chosen start time (about the last nine months: EA live API plus daily archive). A storm surge and a sea level rise
offset can be added to either. With a synthetic tide the river flow at the tidal limit is steady or a flood wave;
with a recorded tide gauged flows are used: EA Hydrology 15-min flows where published (they lag several weeks),
then real-time flow where the gauge publishes it, otherwise real-time level converted with a stage-flow relation
built from a year of the same gauge's paired records. Each result lists the source used for every gauge.

### Each estuary's configuration (`config.json`)
River names, tidal limit (`start`) and mouth gauge (`end`) points in British National Grid, tide gauge measure,
check gauges, flows (`at`: "start" or a confluence point; `lag_min`; `hydrology` station id; optional `rt_flow` or
`rt_stage` real-time measures), bed profile `[[chainage m, bed mAOD], ...]`, Manning's n, widths limits, node
spacing, and the calibration record. Geometry comes from `line.geojson` / `water.geojson` (Ordnance Survey: OS Open
Rivers and OS OpenMap Local) when present, otherwise `line.json` / `water.json` (OpenStreetMap). The main channel is
found as the shortest path through the river network, so side channels round islands are skipped and small gaps
(weirs, locks) are bridged. After editing a config, delete `geometry.json` to rebuild.

### Calibration (`calibrate.py`)
`python calibrate.py <estuary> <start UTC> <hours>` compares the model with the check gauges for a recorded period
(errors at high and low water, timing, RMSE). Add `--fit` to adjust the bed profile points and Manning's n by a
coordinate search; the result is written to `/tmp/calib_<estuary>/fit.json` and can be saved into the config.
`calibration_skip` and `calibration_hw_only` exclude gauges affected by structures.

Tidal Thames result (calibrated on 1-2 October 2026, checked on 15-16 September 2026): high water within 8 cm at
Westminster, Tower Pier, Silvertown and Charlton, high-water timing within 7.5 minutes. Low water is less good
(+0.3 m at Tower Pier, +0.8 m at Silvertown, a gauge likely affected by a dock). Not modelled: Richmond half-tide
lock and weir (Richmond low water held up) and Thames Barrier closures, so extreme surges are not realistic for the
Thames without the barrier.

Important assumptions:
- Tidal Tyne: the bed profile is an assumption (no check gauges on the estuary); Bywell flows are delayed by an
  assumed 60 minutes to reach Wylam.
- Wet-area statistics exclude the estuary, other mapped water bodies and a 15 m margin along them (unmapped
  foreshore and quay walls); speeds exclude cells next to open edges.
- Areas with an estuary run more slowly per cell (deep water forces short time steps); the budget accounts for this.

### Adding an estuary with the builder
`python build_estuary.py spec.json` downloads OS Open Rivers and the OS OpenMap Local tile(s) (cached in
`~/.cache/stormlab_os`), extracts the centreline and tidal water, writes `data/estuaries/<id>/` and builds the
geometry. The spec holds the names, tidal limit and gauge positions (lat/lng), EA measures, flows, OS tiles and an
initial bed profile; `corridor_factor: 1.0` keeps the whole water outline for wide estuaries whose channel runs
near one bank. Then check and calibrate with `calibrate.py`.

Where the mapped river network detours (side channels, tributaries, docks), add waypoints (`via`, British
National Grid points in order from the tidal limit). With `straight_after: n`, the channel runs straight through the
waypoints after the n-th one, for open estuaries with no mapped channel line. With `width_mode: "sum"`, widths
count all water across the section; `sum_from_ch` limits that to the outer estuary so meanders upstream are not
counted several times. Check a new estuary with a recorded run: if the tide stops part-way up, look for stations
with very small widths or that lie away from the water (the Forth and Humber needed this).

### Adding an estuary by hand
1. Create `data/estuaries/<id>/config.json` (copy the Thames one) with the river names, tidal limit, mouth gauge
   (an EA tidal-level measure in mAOD), flows and an initial bed profile.
2. Add geometry: OS Open Rivers and OS OpenMap Local extracts via `os_data.py` (`river_lines`, `water_polygons`,
   `save_geojson`), or OpenStreetMap `line.json` / `water.json`.
3. Run `python calibrate.py <id> <start> 48` to check it, `--fit` to calibrate where check gauges exist.

## Roofs

Buildings are solid walls in the mesh (cells at least half covered by a footprint are removed), so rain on them
would otherwise never enter the model; in a city centre that is up to half the area (48% in central Newcastle).
Each building now drains its roof through a virtual downpipe into the lowest open cell beside it (searching up to
60 m), on ground connected to the wider street network rather than enclosed courtyards or light wells. The roof
area is exactly the area of the removed cells, so the model receives the rain it previously missed. Downpipe cells
get an inflow equal to rain intensity x roof area; the results note lists roof water separately. Imported drainage
networks don't take roof water yet, so this represents roof water reaching the surface (blocked or overwhelmed drains).

Effect in central Newcastle (0.5 km2, 30 mm in an hour): 7,330 m3 of roof water, land wet deeper than 0.3 m from
0.40 to 0.76 ha, peak outflow from 1.37 to 4.0 m3/s. The Jesmond Vale 2012 replay was regenerated with roofs
(wet over 0.3 m 3.33 ha instead of 3.10; peak culvert outflow 21.1 m3/s instead of 20.2). Areas prepared before
this change update their mesh automatically at their next run.

## Flood defences (England)

When an area is prepared, the Environment Agency's Spatial Flood Defences (including standardised attributes) are
downloaded for it (OGC API; `data/defences.json` in the area folder). Every wall, embankment, gate or bridge
abutment with a recorded crest level (effective, actual or design crest) is raised into the mesh: the corners of
the cells it crosses are lifted to the crest level, and where the defence runs along the water's edge the land
cells alongside are used so the wall has no gaps. Natural high ground is skipped (it is already in the terrain).
Gates are assumed closed. Defences show as purple lines on the map with their crest level.

Limits: walls become one to two cells wide (5 or 10 m) instead of their real width; only defences in the EA
dataset are included (defences missing from it, or without a crest level, are not modelled); temporary barriers
are not included. Wales has an equivalent NRW dataset, not yet connected. The Jesmond Vale preset keeps its
original mesh (no defences).

## Wales

- Terrain: where the Environment Agency has no data, the app downloads Welsh Government LiDAR 2020-23 (1 m DTM and
  DSM tiles, Open Government Licence) through the DataMapWales tile catalogue. The area info shows the terrain source.
- Flood defences: Natural Resources Wales flood defence locations (walls, embankments, gates, quays, demountables).
  They have no crest levels, so each crest is estimated as the median of the highest LiDAR surface-model value
  along the line (labelled "crest estimated from LiDAR"). At Newport these come out 1.2 to 1.4 m above the ground
  behind the walls. Natural high ground is skipped.
- Tide gauges: Newport, Mumbles, Milford Haven, Holyhead, Barmouth, Llandudno and Fishguard are in the EA service.
- River flows: Natural Resources Wales publishes 15-minute river levels (not flows) through its API, which needs a
  free key: sign up at api-portal.naturalresources.wales, subscribe to the River Levels product, and copy the key
  into `settings.json` (`{"nrw_api_key": "..."}`; keep it private). Levels are turned
  into flows with a level-flow relation built for each gauge from the National River Flow Archive (UKCEH, open,
  daily flows) at the same site: from paired daily records where NRW levels and NRFA flows overlap, otherwise (NRW
  keeps about a year of levels, NRFA lags by about a year) by matching their distributions, the level exceeded on
  p% of days mapped to the flow exceeded on p% of days over the last five years. That is approximate and is
  labelled so in the results. Each Welsh estuary is configured with its NRW level station, parameter and NRFA
  station (`flows[].nrw_location`, `nrw_parameter`, `nrfa_station`), e.g. Usk at Chainbridge (4039, 176, 56001).
  The NRW service limits request rates; the app waits when asked to. Without a key, recorded runs use the default
  base flow and say so.

## Scotland

- Terrain: Scottish Remote Sensing Portal LiDAR (public archive, Open Government Licence). Tiles are cloud-optimised
  GeoTIFFs read by window over HTTP, so only the needed part is downloaded. An index of 9,623 tiles
  (`data/scotland/lidar_index.json`, rebuilt with `python -c "import scotland; scotland.build_index()"`) lists the
  National LiDAR Programme (50 cm, preferred) and phases 1 to 6 and Orkney. Coverage is not complete: areas with no
  Scottish LiDAR fall back to filled gaps or cannot be prepared.
- River flows: SEPA's time series service (open, no key), 15-minute flows (`flows[].sepa_ts` in the estuary config;
  `scotland.sepa_find_flow_ts("Station name")` finds the id).
- Tide gauges: Leith, Aberdeen, Wick, Ullapool, Kinlochbervie, Tobermory, Stornoway and Millport (EA service, mAOD).
- Flood defences: there is no open national flood-defence dataset for Scotland, so only defences that show in the
  terrain are represented.
- Areas, OS Open Rivers and OS OpenMap Local work as in England and Wales.

## Northern Ireland

- Terrain, best first:
  1. DfI River Basin LiDAR (1 m; Belfast City 2006 at 0.5 m), about 14% of Northern Ireland including Belfast and
     many towns, published as 104 large area archives (Open Government Licence). The app does not download them:
     `data/ni/lidar_index.json` lists all 3,007 one-kilometre DTM tiles with their extents and positions inside
     the archives, and `remotezip_lite.py` fetches just the tiles an area needs with HTTP range requests.
  2. OSNI Open Data 10 m DTM everywhere else (six zips by 1:50 000 sheet, about 170 MB each, downloaded once
     when first needed; `data/ni/dtm_index.json`). With 10 m terrain use 10 m cells, and expect walls and
     embankments to be smoothed away.
  The area info shows which source was used.
- Datum: Northern Ireland heights refer to Ordnance Datum Belfast. The national tide gauge service quotes its
  Northern Ireland gauges (Bangor, Portrush) against OD Belfast too, so gauge levels and terrain share a datum.
- River lines and water outlines come from OpenStreetMap (OS open data does not cover Northern Ireland), including
  multi-part water areas such as the River Lagan, fetched complete so their outlines close.
- River flows: no real-time flow data is published for Northern Ireland, so recorded runs use the default flow.
- Flood defences: no open dataset found, so none are represented.
- The Foyle is on the border: there is no terrain for the Donegal side.

Belfast test (Lagan, highest Bangor tide of the last 30 days plus a 0.8 m surge, river at 2.93 m): land wet
deeper than 0.3 m fell from 25 ha with the 10 m terrain to 14.7 ha with LiDAR and to 5.3 ha once the river above
the weir and the docks were recognised as water. Without defence data the remaining figure may still be high.

## Robustness of the gauge data

Calibration skips gauges whose readings are flat for the whole period (gauge faults, e.g. New Holland during the
Humber calibration), and gauges that dry at low tide are used for high water only. Missing flow data never stops a
run: the default base flow is used and the result lists the source used for every gauge.

## Numerical artefacts at steep drops

Where the ground drops several metres within one cell (quay walls, slipways, cuttings), the openswmm 6.0.0a4 2D
solver can produce unphysical local speeds (tens of m/s) while the overall mass balance still closes. Speeds above
10 m/s are excluded from the "fastest water" statistic and counted in the results note; depths right at those
spots should be treated with caution. Smaller cells (5 m rather than 10 m) reduce the problem.

## 3D view

Once an area is open, the app shows it as a 3D model (a diorama of the area only); the map view is used for
finding and drawing areas and picking burst locations, and the 3D/Map switch (top right) changes between them.

- Ground: the model's own terrain grid (so water sits exactly on the ground it was computed on), textured with
  land cover from OpenStreetMap (roads, paved areas, parks, woodland, gardens, farmland, water) and gentle terrain
  shading, on an earth base.
- Buildings: OpenStreetMap footprints at heights measured as surface model minus ground (median over the
  footprint): EA LIDAR last-return DSM in England, Welsh LiDAR DSM in Wales, Scottish LiDAR DSM in Scotland;
  otherwise OpenStreetMap height or levels, otherwise 7 m. The area info shows how many were measured.
- Trees: canopy is found in the same surface model (more than 2.5 m above ground, outside buildings and water);
  one tree is placed about every 6 m of canopy at the measured height, with a crown sized to match (up to 40,000
  per area). Where there is no surface model, mapped woodland gets trees at a typical density. The Trees box in the
  view bar hides them.
- Also shown: flood defences as walls at their crest level, river inflow and outflow points, the burst main.
- No shadows, by design: they would hide the water in streets. The water is matte (no sun reflections), so it keeps
  its colour from every angle, and it always sits slightly above the ground so thin water never merges with it.
- Water: a lit, semi-transparent surface at ground plus depth for every wet model cell, coloured by depth (or by
  speed for the maximum-speed layer), following playback.
- Controls: drag to rotate, right-drag or shift-drag to pan, wheel to zoom, the rotate buttons for the four
  diagonal views, and "Ground" to exaggerate the relief (buildings keep their real heights).
- The scene is built once per area (`cache/scene.json`, about 2 MB per km²), when the area is prepared or on first
  view. Results now also keep water depths per model cell and frame for the 3D view.
- The 3D library (three.js) loads from the internet like the map tiles.

## Your own flood defences (mitigation)

In the 3D view, "Defences" opens a panel where you draw defences on the model: click points on the ground, then
double-click or press Enter (Backspace undoes a point, Esc cancels). Set the size before or while drawing.

| Type | Drawn as | Built into the run's ground as |
|---|---|---|
| Flood wall | a line; height above ground | mesh points within 0.75 cell of the line raised to ground + height |
| Demountable barrier | a line; height | the same as a wall (for testing deployed or not) |
| Embankment | a line; height and crest width | a crest of that width at ground + height, with 1-in-3 side slopes |
| Storage basin | an area; depth, optional bund | ground lowered by the depth inside, a bund just outside |
| Raised kerb | a line; height (0.05 to 0.5 m) | like a low wall, steering shallow water |
| Channel deepening | an area over a river; extra depth | river cells inside lowered (tidal rivers are never changed) |
| Permeable surface | an area | ground and paving soak up to 150 mm/h, falling to 40 (Horton), Manning n 0.015 |
| Rain garden | an area; ponding depth | ground lowered by the ponding depth; soaks 200 to 50 mm/h; n 0.10 |
| Swale | a line; width and depth | a shallow lowered channel; soaks 100 to 20 mm/h; n 0.06 |
| Tree planting | an area | rougher ground (n 0.12); soaks 120 to 25 mm/h |
| Green roofs | an area round the buildings | those roofs hold the first 15 mm (adjustable 5 to 40) and release the rest through a 30-minute store |
| Storage tank | one point (its inlet); volume, maximum inflow | an inlet junction linked to that cell drains into a sealed storage unit through an orifice sized for the inflow rate; when full, water backs up to the surface |
| Pump | intake point, then discharge point (or Enter for "out of the area"); rate, start depth | a 20 m2 wet well linked to the intake cell; the pump runs at the rate once the well is at the start depth, onto the discharge cell or out of the model |
| Flood gate | a line; height; closes at a time, at a water depth, stays open or stays closed | a wall in the ground with one opening per cell along it (an orifice between junctions linked to cells either side, opening = cell width x height); control rules close every opening at the set time or when water beside the gate reaches the set depth |

Surface measures only change ordinary ground (not rivers, roof downpipes, inflows or the burst main). Infiltration
values are indicative of well-maintained features at "normal" ground, scaled by 1.3 for dry and 0.5 for wet ground;
with "sealed" ground nothing soaks in. Test in central Newcastle (0.5 km2, 30 mm in an hour, measures over most of
the area): green roofs on every building cut roof water from 7,330 to 3,250 m3 and buildings with water over 0.3 m
beside them from 79 to 68 (of 319); permeable surfaces everywhere soaked up 9,721 m3 (452 without) and cut those
buildings to 48; mass balance exact in every run.

Defences are saved with the area (`areas/<area>/measures.json`) and included in the next run unless you untick
"Include my defences" in the scenario window. Each run builds them into its own copy of the mesh, so the area is
never changed and every run records the defences it used (`params.json`). River cells are never altered.

Results show "Buildings with water over 0.3 m beside them" (from OpenStreetMap footprints) and the number of your
defences. Under Results, "Compare with" picks any earlier run of the area (for example the same storm without
defences); the 3D view then offers a "Difference" layer: green where maximum depth fell, red where it rose (by up
to 0.5 m on the colour scale; changes under 3 cm are not shown). Water held in your storage basins is not counted
as flooding. Limits: walls are at least one cell wide in the model; water goes round open ends and through gaps,
so tie lines into higher ground or other defences.

## Hazard, roads, key sites and the EA national map

- Hazard to people: the Defra/EA method (FD2320): hazard rating = depth x (speed + 0.5) + debris factor (1 where
  deeper than 0.25 m), worked out at every saved frame from that frame's depth and speed (the engine's vx, vy) and
  the highest value kept, for cells at least 0.1 m deep. Classes: low under 0.75, moderate 0.75 to 1.25,
  significant 1.25 to 2.0, extreme above 2.0. "Hazard" layer in both views; areas per class in the results.
- Roads impassable to cars: OpenStreetMap roads (not bridges or tunnels), sampled every 2.5 m, with water deeper
  than 0.3 m at some time (and over 0.1 m); the most affected roads are listed.
- Key sites: schools, nurseries, colleges, universities, hospitals, clinics, GP surgeries, care homes, fire and
  police stations, town halls and railway stations from OpenStreetMap (points and areas), affected when water over
  0.3 m comes within 15 m. Areas prepared earlier refresh their map data once, at their next run.
- EA map: the Environment Agency's Risk of Flooding from Surface Water (NaFRA2, England; (c) Environment Agency
  copyright and/or database right 2025). "EA map" layer: the WMS on the 2D map, and per-cell likelihood bands
  (high 1 in 30, medium 1 in 100, low 1 in 1000) in 3D. In the results, "Compare with the EA national map" sets
  the EA depth layer and likelihood and reports the EA extent, this run's flooding (deeper than the layer's depth,
  0.1 m for "any depth"), their overlap, and the critical success index. Rivers, ponds and their edges are left
  out. The EA map is probabilistic (chance in any year), so one storm only partly matches it.

Test (central Newcastle around the RVI, 0.65 km2, 50 mm in an hour): 1.77 ha significant or extreme hazard, 1.47 of
29.6 km of road impassable, the Royal Victoria Infirmary with 0.46 m of water at its entrance, 79 of 189 buildings
with water over 0.3 m beside them; 61% of the modelled flooding lies within the EA extent (critical success index
28%).

Tanks, pumps and gates use the engine's 1D elements linked to the 2D surface. The mass balance includes the
water exchanged with them (the results report water held in tanks and pumped away). Tested on the Jesmond Vale
preset: a 3,000 m3 tank at the deepest pool filled to about 1,700 m3; a 0.3 m3/s pump moved 861 m3 in an hour; a
60 m gate on a flow path passed 1,025 m3 left open, nothing after closing at 20 minutes, and nothing kept closed;
mass balance exact in every run.

## Damage to buildings and cost-benefit

Estimated direct damage uses the European Commission Joint Research Centre's global flood depth-damage functions
(Huizinga, J., de Moel, H. and Szewczyk, W. (2017) Global flood depth-damage functions: Methodology and the
database with guidelines. EUR 28552 EN. doi:10.2760/16510), read from the JRC database spreadsheet:
the European damage curves (fraction of maximum damage at 0, 0.5, 1, 1.5, 2, 3, 4, 5 and 6 m) and the United
Kingdom's building-based maximum damage values, structure plus content, in euros per m2 at 2010 prices:
residential 758.12, commercial 1,053.19, industrial 855.95.

For each building (OpenStreetMap footprint, at least half a cell), the depth used is the deepest water on the open
ground beside it, the curve is chosen from its tags (homes and unspecified buildings residential; shops, offices,
hospitals, schools, universities and other public buildings commercial, as the JRC has no public category;
warehouses and factories industrial), and damage = fraction x maximum x footprint area. Results show the estimated
damage, and when comparing with another run, the damage avoided. "Multiply damage by" in the results converts
figures (for example an exchange rate and inflation to today's pounds); the factor is the user's choice.

Defence costs are not provided (no open source): enter a cost for each defence in the Defences panel, and the
results show the total and the damage avoided per unit of cost for that storm. This is one storm, not an annual
average, so it is not a full cost-benefit appraisal. All figures are indicative: depths beside buildings stand in
for depths inside them, and large multi-storey buildings are valued by footprint.

Test (central Newcastle around the RVI, 50 mm in an hour): about EUR 28.8 million damage to 173 buildings; green
roofs and permeable surfaces over the area (entered cost 4 million) cut it to about 10.7 million: 18.1 million
avoided, 4.5 per unit of cost for this storm.

## Report and GIS export

In the results window:
- "Report (PDF)": one A4 page with the scenario, the maximum depth map, the current 3D view (when in 3D), the key
  results side by side with the run chosen under "Compare with" (including damage avoided and damage avoided per
  unit of cost when defences have costs), affected key sites, the defences used, and sources and limitations.
  Damage uses the "Multiply damage by" factor. Saved as `runs/<run>/report.pdf`.
- "GIS data (zip)": `runs/<run>/gis_export.zip`, British National Grid (EPSG:27700), with a README:
  `max_depth_m.tif`, `max_speed_m_s.tif`, `hazard_class.tif` (0 none to 4 extreme) at the model's cell size, and
  `flood_results.gpkg` with layers area_outline, buildings (type, deepest water beside the building, estimated
  damage in EUR at 2010 prices, affected flag), roads_flooded (roads with water over 0.1 m: maximum depth, length
  over 0.3 m), key_sites (deepest water within 15 m), defences_lines and defences_areas (with entered costs).
  Also `time_series.nc`: the run as CF-1.8 NetCDF on the same grid (British National Grid, model cell size):
  depth, water_level (m above Ordnance Datum) and speed at every saved frame, with max_depth, max_speed,
  hazard_class and ground_level; time in minutes from the start (real dates for recorded tides). It is written at
  the end of every run (`runs/<run>/time_series.nc`, about 2 MB per hour simulated for a 1 km2 area at 5 m).
  QGIS: add it and use the Temporal Controller; ArcGIS Pro: Make Multidimensional Raster Layer; Python:
  xarray.open_dataset. Values are the averages of each cell's two triangles, which in this regular mesh lose
  almost nothing.
Runs made before this version cannot be exported (rerun them). Areas prepared earlier store their building
outlines once, on first export.

## Saving a video

"Save video (MP4)" (under the clock, and in the results window) records the playback as shown: the 3D view, or the
map view drawn without the basemap tiles (terrain shading, water, outline and markers). Each frame carries a caption
(area, scenario, time) and the colour legend; the maximum depth and speed layers become a short still clip. The
browser draws the frames, the local server encodes them with ffmpeg (bundled in the `imageio-ffmpeg` package) and
saves the MP4 in that run's folder, `runs/<run>/simulation_<view>_<layer>.mp4`, then the browser downloads it.
For the Jesmond Vale 2012 replay, the video goes in `runs/jesmond-vale-baseline/`.

## Area size and run time

There are no hard limits on area size or run time except a memory ceiling of 3 million cells (about 75 km2 at
10 m cells). Instead the app tells you what to expect:
- While drawing, the size line estimates the run time per simulated hour on this computer (calibrated from your
  own runs) and turns amber when an area is large (over 250k cells, or a 2-hour storm over 10 minutes).
- Preparing a large area opens a window with the expected preparation time, run time per simulated hour and for a
  2-hour storm, a note that tidal rivers make runs several times slower, a note on 3D playback above 500k cells,
  and the same figures with 10 m cells, with a one-click switch to them (a quarter of the cells and longer time
  steps: typically 4 to 8 times faster, with streets and walls less sharply resolved).
- Running a scenario estimated over 10 minutes asks for confirmation; the scenario window also shows the estimate
  and suggestions to shorten it.
This computer: the app reads the processor, number of cores, total and free memory and any NVIDIA graphics card
with ordinary user rights (Python's standard library and psutil; nothing is sent anywhere), shown under "About
the model". On first start it runs a one-minute speed test in the background (a 30-minute storm on the Jesmond
Vale preset) on one core, on all cores and on the graphics card if there is one (the engine's CUDA back end, used
only if this install supports it), keeps the fastest setting for every run, and seeds the run-time estimate.
"Measure speed again" repeats it. The memory ceiling is 60% of free memory at the measured memory per cell (about
2.5 KB per cell, refined after every run); it replaces the fixed 3 million cells and falls back to it if memory
cannot be read. Run-time estimates keep refining themselves from each finished run.
Observed on a single-core test machine: about 0.3 s per 1,000 cells per simulated hour in towns, 0.7 to 2 s with a
tidal river (deep water forces short time steps). Multi-core computers may be faster.

## Troubleshooting downloads

Run `python diagnose.py` from the app folder (on Windows: `.venv\Scripts\python diagnose.py`). It tests every
data service the app uses and prints one line per service, with the exact error for any that fail.

- Several services failing with SSL or certificate errors: the network inspects secure connections (common on
  university and corporate networks). The app uses the operating system's certificate store through the
  `truststore` package, which the start script installs; if errors remain, ask IT for the network's root
  certificate or try another network.
- Only the Overpass lines failing: those public servers are busy or down; the app falls back to the main
  OpenStreetMap API automatically.
- All requests identify the app with a clear user agent, as OpenStreetMap's usage policy asks.
- Error messages from area preparation include the underlying reason for each service tried.

## OpenStreetMap downloads

Buildings, roads, land cover and water come from OpenStreetMap through Overpass (five public servers are tried,
failing fast when one is down). If all are down, the app uses the main OpenStreetMap API instead, in tiles of
about 1 km (smaller where the map is dense, since that API refuses requests over 50,000 points), merging
features that cross tile edges. Multi-part water areas such as rivers are fetched complete. A 2.2 x 2.8 km block
of central London takes about half a minute this way.

## Terrain gaps

Gaps in the LIDAR (often over water and foreshore) are filled from the nearest valid ground so those cells stay in
the mesh; the filled share is recorded in each area's `mesh_meta.json` (`terrain_filled_pct`).

## Drainage networks

An area can hold one imported sewer or surface-water drainage network (`sewer.py`), opened from the side panel
("Import drainage"). Accepted files: a SWMM input file (`.inp`, for example exported from InfoWorks ICM, SewerGEMS
or PCSWMM), a GeoPackage, or a `.zip` holding shapefiles or an ArcGIS file geodatabase (`.gdb`). GIS data needs a
point layer of manholes and a line layer of pipes; fields are matched by name (InfoWorks and common water-company
names) and can be changed. Levels must be in metres above Ordnance Datum.

Every file is checked before anything is imported. Each issue is one of:

- **error**: the import stops and the data must be corrected at source. Examples: a SWMM file in US units;
  open channels given as IRREGULAR or STREET sections (the 2D surface already carries open channels, so importing
  them would carry the same water twice); dummy links; shape names SWMM doesn't have; storage units with a shape other than TABULAR or FUNCTIONAL; curves referenced but missing;
  weirs or orifices without a cross-section, or whose level can't be worked out; GIS structures missing a value
  the model needs (crest level or length, flow-control level or size, pump rate or switch levels, tank floor level,
  depth or area); pumps switching off at or above their switch-on level; weir crests more than 0.5 m above the
  upstream cover; repeated manhole IDs; pipes naming manholes that don't
  exist; no coordinate system placing the network on the area; levels more than 2 m from the LiDAR ground on
  average (wrong datum or units); pipe tops above the cover; more than 25% of pipe sizes or 50% of manhole levels
  missing.
- **fix**: the app can repair it, and does so only if the user ticks that repair. All repairs are shown in one pass;
  any left unticked stops the import. Repairs: convert coordinates (British National Grid, Irish Grid, ITM, WGS84);
  sizes from millimetres; join pipe ends to manholes within 5 m, or add a junction further away; fill missing sizes
  from the next pipe (225 mm if none); cover levels from LiDAR; inverts from connected pipes, depth, neighbours or
  1.5 m below cover; pipe inverts from their manholes; lower manholes above their pipes; Manning's n of 0.013 where
  missing or not a Manning value; rename repeated pipe IDs; drop manholes with no pipe; keep only the part on the
  area (pipes leaving it end at a free outfall; pipes entering it are dropped); join several pipes into an outfall at
  a new junction (SWMM allows one); treat outfalls with pipes leaving them as manholes; give each part without an
  outfall one at its lowest end manhole; cover types not recognised use the scenario default; chamber areas outside
  0.2 to 30 m2 use the default.
- **warn**: imported as it is and noted (weirs, orifices, outlets, pumps and storage units imported from a SWMM
  file; flow dividers imported as junctions; flap valves inside the network (not modelled); tidal or time-series
  outfalls treated as free; SWMM junctions with a surcharge depth read as sealed, chamber sizes read
  as millimetres, covers more than 0.5 m from LiDAR, pipes falling uphill, pipes under 2 m,
  stated and drawn lengths differing by more than 20%, manholes deeper than 10 m).

In a run, each manhole is a SWMM storage node from its invert, with a constant plan area: the chamber area from
the data (a "chamber area" field, or a "chamber diameter or width" read as circular, in mm or m), else the
scenario's default (1.13 m2, a 1.2 m circular chamber; an assumption). Pipes are conduits (Manning, link offsets as
elevations). How a manhole meets the surface depends on its cover type, read from a "cover type" field (words such
as solid, grating, vented, sealed, bolted; others are offered a repair that uses the default; a SWMM junction with a
surcharge depth is read as sealed):

- **solid** (the default): water enters only through a gully. One triangle of the manhole's cell is linked to a
  gully node, which drains into the manhole through a one-way (flap) orifice sized to pass the gully capacity at
  0.3 m head (default 20 l/s per manhole, an assumption). Water leaves only when the manhole's level passes the
  ground: a one-way orifice the size of the cover opening (default 0.3 m2) discharges to a coupled outfall on the
  cell's other triangle, as the roof downpipes do.
- **grated**: one two-way link through the grate's open area (default 0.1 m2, an assumption).
- **sealed**: no link; the manhole pressurises without spilling.

The engine measures exchange from a node's top, so gully nodes and grated manholes have their top at the ground
(gully nodes may pressurise under ponded water through a surcharge depth); solid manholes may rise up to 10 m above
the ground so the cover orifice can open. Gully orifices are low rectangles, so they never raise a gully node's top.
A manhole with no open cell within two cells behaves as sealed. Outfalls inside the area are linked to the nearest
open or channel cell, so their water returns to the surface; outfalls outside it take water out of the model. Pipes
start empty: foul and dry-weather flow, subcatchments and roof connections are not modelled.

Cross-sections from a SWMM file are written back as given, with all their dimensions and barrels, so any of SWMM's
standard shapes imports, as do CUSTOM closed shapes with their SHAPE curve (carried over and renamed; a missing
curve stops the import).

SWMM files may also hold weirs (CSO overflows, for example), orifices, outlets, pumps and storage units. They keep
their settings from the source model: crest heights are converted to elevations, pump and outlet curves and
TABULAR storage curves are carried over (renamed), and pumps run on their curves with their start-up and shut-off
depths (control rules aren't imported). Storage units connect to the surface only through the network. Outfalls
keep their flap gates and fixed stages; tidal and time-series outfalls become free, as inside the area the river
and tide come from the 2D model. Flow dividers become junctions: the model routes flow by dynamic wave, under which
SWMM treats a divider as a plain junction anyway.

GIS imports can add structures as further layers, mapped field by field like pipes (InfoWorks-style names are
recognised): weirs and flow controls (orifices, vortex controls) and pumps as lines between two nodes, joined by ID
or by snapping their ends like pipes, and storage tanks as points or polygons. Required values: a weir's crest level
and crest length; a flow control's invert level and either a diameter, a width and height, or a design flow and
design head (converted to the orifice that passes that flow at that head); a pump's rate and its switch-on and
switch-off levels; a tank's floor level, depth and plan area (or its volume, or the polygon's area). A missing
required value stops the import, because estimating it would change the results. Repairs are offered only for:
discharge coefficients (1.84 for weirs, SWMM's sharp-crested value in SI units, and 0.6 for flow controls); a
weir's opening height (crest to the upstream cover); pump rates in litres per second; and switch levels given as
depths above the wet well floor. Pumps deliver a constant rate between their switch levels. Flap valves can be a
yes/no field on outfalls, pipes, weirs and flow controls; on a pipe into an outfall they gate the outfall, and
elsewhere in a pipe network they are not modelled (SWMM has no flap on conduits) and are listed.

Check (the test network plus a CSO weir to an overflow outfall, an orifice, and a 30 m2 tank emptied by a pump;
120 mm in 30 minutes, 300 l/s gullies): the weir spilled 259 m3 to the overflow, the tank filled to 98% while the
pump ran at its curve's rate, and flow routing continuity was 0.15%.

Results add the water taken in (through gullies and grates, net of what they gave back, per manhole over the run), the water pushed back out
through manholes, the water carried to the outfalls, and the number of manholes full to ground level and spilling.
They come from the engine's report and the run's `sewer_nodes.json`.

Checks (synthetic network, 1 h simulated, 120 mm in 30 minutes): with solid covers and 300 l/s gullies, 1,166 m3
entered, 9 manholes filled to the ground, one lifted its cover and spilled 164 m3, and 923 m3 reached the outfall;
with grated covers, 1,215 m3 entered, 318 m3 came back out at two manholes and 858 m3 reached the outfall; sealed
covers exchanged nothing. Flow routing continuity stayed within 0.15%. Water in, out and spilled differ by the water
still in the pipes plus up to about 3%, as the report gives each manhole's exchange as a net figure. Gully nodes
under ponded water show local node continuity errors in the report as water moves in and out between steps; the
system totals close.

`tests/test_sewer.py` covers each blocking error, the repairs (including that a declined repair blocks saving),
cover types and chamber sizes on import, how each cover type is linked to the surface, and a coupled engine run.

## IFC buildings

A building can be added from an IFC model (`ifcbuilding.py`, read with IfcOpenShell): from "Add IFC building" in
the side panel, or by clicking a mapped building on the map and choosing "Replace with an IFC model". The model
gives what national data doesn't: the footprint from its walls and slabs, the finished floor level (the lowest
ground-floor door sill, else the lowest storey near the ground), each door's position and threshold level, and its
spaces with their use, storey and floor area.

Placement: an IfcMapConversion to British National Grid places it directly (its Scale is applied relative to
metres, since the geometry is read in metres; a model in millimetres that left Scale at 1 is read as if it
converted and noted). A site latitude and longitude gives an approximate, unrotated position. Otherwise the
outline is dragged and rotated on the map, with the mapped buildings around it drawn as a guide.

Checks, as for drainage networks: unreadable files, no walls or slabs with geometry, an implausibly large
footprint, georeferencing to another coordinate system, or a building outside the area stop the import. If the
levels don't sit on the ground (finished floor more than 3.5 m below or 3 m above the lowest LiDAR ground beside
it, typically because levels are relative to the floor), the repair sets the floor 0.15 m above the LiDAR ground,
or the user types the level; doors, storeys and spaces move with it. Missing doors or spaces are noted.

In the model the footprint is a solid block and replaces OpenStreetMap buildings overlapping it by more than a
quarter of their area, in both the mesh and the 3D scene; roof water and damage then treat it like any other
building. The 3D view draws the model's own triangles at true height (only the ground is exaggerated). Adding or
removing a building rebuilds the mesh (seconds) and the 3D scene, and is refused while a run is in progress.

Each run records which IFC buildings were in its mesh. Its "Building" tab reports, for each: the highest water
against the walls compared with the finished floor level (only from cells whose ground is at or below the floor,
since on a slope uphill cells only carry a film of rain on ground already above it); at each door, the water level
outside over time (cells within 7.5 m with ground at or below the threshold), the peak, the margin, when it first
rises above the threshold and for how long, with doors whose surrounding ground is above the threshold flagged;
and the ground-floor spaces below the peak level, with the depth they would take and indicative damage from the
JRC curve for their use (residential, commercial or industrial, from the space's name) and floor area.

Water inside. Each ground-floor and lower space becomes a storage node (its floor level, area and height), or each
storey becomes one volume (85% of the footprint) when spaces lack outlines or an outside door or window doesn't lie
on a space; the check report says which and why. Doors and windows are classed as outside (within 0.8 m of the
outline) or internal, and joined to the spaces around them by position and level. Each outside opening gets a small
node coupled to the lowest open cell within two cells of it, with its top at the higher of the ground and the
threshold or sill, so water enters only once it stands above it. The opening itself is a pair of SWMM outlets (one
each way, so water drains out again) with a flow-head rule, since an opening height would lift the outside node's
top and cut it off from the surface. A closed door leaks in proportion to the depth of water above its threshold,
following laboratory tests of six common UK external doors (composite, wooden, uPVC and fire-rated) for Flood Re
(McLelland et al. 2026, University of Hull, doi:10.5281/zenodo.23220942): leakage rose linearly with depth, from just
over 1,000 litres per hour per metre of door width at 0.2 m to about 4,000 at 0.6 m, so the model uses about 6,700
L/h per metre of width per metre of depth. Individual doors ranged from almost nothing to about 10,000 L/h per metre
at 0.6 m, and outward-opening doors leaked about 74% less on average (only two were tested); the scenario offers a
typical door, an outward-opening door, or a rate the user sets. An open door, or a closed door once it gives way,
passes water as a weir over its width. Giving way is a control rule (closed until the water outside reaches the set
height above the threshold, then open, and open once water is inside); it is off by default, as no domestic door
gave way up to 0.6 m in the Hull tests, and the only failure depths found are for non-domestic doors (hollow-core
doors at 0.71 to 0.99 m, outward-opening steel doors at about 0.9 m on average and an inward-opening steel door at
2.05 m, in tests reported in Results in Engineering, 2019). For modern English homes, Kelman's PhD (2002,
University of Cambridge; extended summary at ilankelman.org) found the non-glass parts of doors not to be the weak
point, but large double-glazed door panels likely to break once the pane is nearly submerged, about 1.5 m of water
above the ground floor in still water; the help text suggests that value for glazed doors. He found window glass too
small and too high to break in floods, so windows don't break unless the user sets a height, after which they pass
water like an open door. His estimates of leakage put a weather-stripped window at about a tenth of a door (as orders
of magnitude; he later noted that his conversion from air to water leakage needs revising), so closed windows leak
above the sill at a tenth of the door rate per metre of width. As a cross-check, the Hull door rate extended to 2.5 m
gives about 0.08 mm/s of rise inside a 55 m2 home through one door, the same order as Kelman's 0.1 mm/s. Door barriers raise the crest of outside doors and are treated as
watertight up to their top, which is optimistic: in the Hull tests temporary barriers ranged from full protection
to failure depending on fit and installation. Internal doors are assumed open and are weirs between rooms. Windows
whose sill is below the ground outside are treated as sealed (and flagged in the check, often a sign of a model
placed on a slope it wasn't designed for). Airbricks are not modelled: the only figure found is anecdotal (about 500
litres per hour each), with no measured data. The report then gives, for each room, the depth
over time (from the engine's output), when it gets wet, for how long, and damage from its deepest water. It also
gives the largest difference between the water against the walls and the water inside (the floor level while the
inside is dry) and flags it against Kelman's finding that, in still water, cavity walls of modern English homes start
to fail at about 0.8 m above the ground floor and most have failed by 2.0 m (Kelman 2002; Kelman and Spence 2003,
Masonry International 16(2), 51-61). Wall failure itself isn't modelled; the flag matters most with door barriers,
which keep water out and so raise the difference.

Check (synthetic two-storey house, 18 x 12 m, finished floor 0.15 m above ground, with two outside doors, two
internal doors and two windows, on a flat plot in Jesmond Vale; 80 mm in an hour on saturated ground): water rose
about 6 cm above the back door threshold. With doors closed, at the measured leakage rate, about 4 mm leaked in;
with doors open the three ground-floor rooms took 10 to 13 cm for 20
to 25 minutes (indicative damage about EUR 8,600); with 0.6 m door barriers they stayed dry. Flow routing continuity
stayed within 0.1%. `tests/test_ifc.py` covers georeferenced, millimetre and local models, placement, the level repair and a
typed level, the blocking checks, the mesh override and its removal, a report on an engine run, and water getting in
through open doors but not past barriers.

## Scanned buildings

A building can also come from a scan (`scanbuilding.py`), from the same "Add building" link: a point cloud as LAS or
LAZ (read with laspy), E57 (pye57) or text (XYZ; PTS with a count on the first line; CSV with or without a header,
columns found by name or position, colours taken from the last three columns when they look like 0-255 values), or a
mesh as OBJ, PLY or GLB/glTF (read with trimesh) from a phone scan or photogrammetry. A mesh is turned into points by
sampling its surface evenly (about 400 per m2, up to 3 million) with its texture or vertex colours; glTF is Y-up by
definition, and for OBJ and PLY the up axis is the one with a flat bottom (most points within 0.2 m of the lowest).
A local scan more than 200 units across is offered a units repair (centimetres, or millimetres beyond 2,000). A
scan placed on the map (such as a phone scan) has no real heights: for telling ground from building it is lined up
provisionally on the LiDAR, and the level repair then asks before shifting it, so its levels are only as accurate as
the LiDAR. A scan whose outline is much smaller than the mapped building it stands on (less than two thirds of it,
typically one side of a house) is offered the mapped outline, keeping the scan's heights and ground. It is stored like an IFC building, so the mesh override, water
inside, the building report, pins and "Go to" work the same way. Coordinates: a LAS coordinate system is used (and
converted to British National Grid if it is another); a file with none whose points fall on the area is read as
British National Grid and noted; otherwise the scan is placed on the map, turned about its own centre. The cloud is
thinned to one point per 10 cm cube. Ground and building points come from the ASPRS classification (2 ground, 6
building) when present; otherwise ground is within 0.3 m of the LiDAR, and the building is the points more than 2 m
above it (and walls above the ground) over the mapped building they sit on, which can take in trees or walls touching
it and is noted. The outline is the occupancy of the building points on a 0.25 m grid, closed and filled; the
height comes from the roof (99.5th percentile) and the ground level from the scanned ground within 4 m. The
scanned ground is compared with the LiDAR: within 0.5 m it is noted; beyond, a repair shifts the scan to match.
Errors: unreadable files, fewer than 1,000 points, a scan outside the area, no building found, no usable outline,
or too little ground around the building to check levels. Until doors are marked the floor level is provisional
(0.15 m above the lowest ground). Doors and windows are then marked in 3D by clicking the scan at each threshold or
sill (the level is read from the point clicked) and giving a width and height; the lowest door threshold becomes
the floor level. A scan has no rooms, so its inside is one volume per storey. Up to 150,000 points are kept for the
3D view, in the scan's colours if it has them.

`tests/test_scan.py` covers LAS, LAZ, unclassified LAS, E57, XYZ, PTS, CSV and PLY point clouds, a georeferenced OBJ,
a phone-style GLB placed and levelled on the LiDAR, millimetre units, a partial scan, a level offset and its repair, the blocking checks,
placing a local scan, the mesh override and marked openings, and water getting in through a marked door on an
engine run.

## Model notes

- Each area is a 5 or 10 m grid inside the drawn outline, each square split into two triangles; buildings
  (OpenStreetMap footprints covering at least half a square) are walls.
- Drawn areas: an outline edge lets water out (normal-flow boundary) wherever the ground just outside is lower
  than the cell inside; elsewhere it is a wall. Runoff from outside the outline is not included.
- Drawn areas, rivers and streams: OpenStreetMap watercourses (river, stream) that cross the outline are detected
  when the area is prepared. OpenStreetMap draws waterways in the downstream direction, so a crossing into the
  outline becomes an inflow point (the boundary cells there are coupled to an inflow node, and nearby edges are kept
  closed), and a crossing out of it is always an open edge. Each inflow can be off, steady, or a flood wave (base
  and peak per watercourse, shared timing). Canals and culverted sections are skipped; the River Tyne is tidal and is
  flagged, not modelled. Flow values are yours to set: there is no gauge data for drawn areas.
- Jesmond Vale preset: 900 x 1,190 m, 73,688 triangles, gauged river inflow and the Ouseburn culvert outlet.
- Rain is uniform over the domain in 5-minute steps. Climate uplift multiplies the whole storm, with presets from the
  Environment Agency peak rainfall allowances for the Tyne management catchment (version 21 March 2022).
- River inflow enters at the top of the box: none, steady, the 2012 Crag Hall record (scaled), or a synthetic flood wave
  (base flow, peak, start, time to peak, fall time; smooth cosine rise and fall).
- Burst water main: a point inflow at the 5 m cell you pick, with flow, start time and duration.
- Outflow leaves through the Ouseburn culvert, modelled as an inlet-control rating curve on the outlet edges:
  critical flow through a 9.1 x 6.1 m ellipse when unsubmerged, orifice flow when submerged. Culvert size from the
  Tyne and Wear HER record 5046; invert taken from the LIDAR channel level, not surveyed. Blockage reduces the
  effective opening width in proportion.
- Ground condition sets Horton infiltration on green and garden areas (dry, normal, saturated) or switches it off.
  All infiltration parameters are assumed.
- Not included unless imported: sewers. Not included: runoff from outside the outline, channel beds of streams below the surveyed water surface.
  Results are indicative and not validated.
- Engine quirks found in openswmm 6.0.0a4: the rain gauge scale factor is not applied to 2D rain
  (rain is written at full intensity), and a model needs at least one 1D element, so a detached
  dummy pipe is included.
- The engine prints a "CPU marcher" message on start; a CUDA GPU can be used with
  `OPENSWMM_2D_BACKEND=cuda` if your build supports it.
