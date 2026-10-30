"""Owner-facing descriptions, units and field types; no numerical defaults."""
PARAMETERS={
 'forecast_method':('Forecast method','Observed-sales calendar average uses every verified completed sales day. Adaptive forecasting uses the separate upward/downward controls. Neither method imputes missing sales.','forecast'),
 'max_source_age_seconds':('Maximum source age','Global evidence freshness in seconds. Unset requires an explicit age for each history check; zero accepts only evidence at the cutoff. No default is installed.','int'),
 'lookback_days':('Historical span','Completed days considered in total. Older history excludes the recent window.','int'),
 'recent_days':('Recent demand window','Completed days used for recent demand, separate from older history.','int'),
 'minimum_available_days':('Minimum confirmed available days','Each compared window needs this many confirmed available days. The approved starting requirement is 14; days need not be consecutive.','int'),
 'recent_influence':('Recent demand influence','Baseline share given to recent demand, from 0 to 1. Trend and evidence quality adjust it.','fraction'),
 'increasing_sensitivity':('Upward trend sensitivity','How strongly an increase shifts influence toward recent demand, from 0 to 1.','fraction'),
 'decreasing_sensitivity':('Downward trend sensitivity','How strongly a decrease shifts influence toward recent demand, independently of upward sensitivity.','fraction'),
 'increasing_limit':('Upward trend limit','Maximum share of the observed upward demand gap accepted, from 0 to 1.','fraction'),
 'decreasing_limit':('Downward trend limit','Maximum share of the observed downward demand gap accepted, from 0 to 1.','fraction'),
 'reliability_depth_multiple':('Evidence depth','Available days divided by this multiple of the minimum history determine evidence depth. This is not a confidence probability.','fraction'),
 'stockout_kind':('Stockout Adjustment meaning','Confirmed-only uses available-day sales. An optional quality gate may reject low available share or excessive unknown evidence. None invents lost sales.','stockout'),
 'stockout_threshold':('Stockout quality threshold','Explicit fraction from 0 to 1 for the selected quality gate. Leave unset for confirmed-only.','fraction'),
 'negative_inventory':('Negative stock protection','Abstain when negative stock makes the purchasing position unreliable. Keep the observed count unchanged.','negative'),
 'reorder_weeks':('Automatic reorder coverage','Weeks of demand at or below which replenishment is suggested. This does not change manual unit rules.','fraction'),
 'target_weeks':('Automatic target coverage','Weeks of demand to target after replenishment. Must exceed reorder coverage.','fraction')}
