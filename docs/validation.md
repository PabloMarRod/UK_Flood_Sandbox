# Validation so far

What has been checked, and what has not. Figures come from development runs described in
[technical-notes.md](technical-notes.md).

## Checked
- **Water balance**: every run reports its mass balance error; development runs closed at 0.000%, including runs
  with storage tanks, pumps and gates.
- **Tidal estuaries**: the Thames, Humber, Severn and Exe models were calibrated against tide gauges and checked on
  a separate period (Thames: high water within 8 cm at four gauges; Humber within 17 cm at three; Severn and Exe
  at one gauge each, high water only).
- **Comparison with the EA national surface-water map** for one area of central Newcastle and one 50 mm storm: 61%
  of the modelled flooding lay within the EA extent (critical success index 28%), consistent with comparing one
  storm against a map of all likelihoods.
- **Physical behaviour of mitigation measures** in controlled runs (for example green roofs halving roof runoff
  for a 30 mm storm; a gate passing water when open and none when closed).

## Not yet checked
- The Environment Agency's 2D hydraulic model benchmark tests.
- Systematic comparison with the EA surface-water map across many areas and matched design storms.
- Recorded flood events with observed extents (other than the Jesmond Vale 2012 preset's gauged flows).
- Estuaries without gauges along them (most estuaries; see the status shown in the app).
- Multi-core and graphics-card (CUDA) performance paths.

Contributions of validation data and comparisons are especially welcome.
