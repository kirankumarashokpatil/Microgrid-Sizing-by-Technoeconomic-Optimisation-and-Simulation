// Which inputs each scenario's form shows — mirrors the Streamlit FIELDS map.
// Anything not listed falls back to DEFAULT_FIELDS. Add a row when a new scenario
// becomes runnable.
export const FIELDS = {
  S00_FIXED_DESIGN_EVAL: ["pv_mw", "bess_mw", "bess_mwh", "grid_ceiling_mw"],
  S11_BTM_SSR_TARGET_BESS: ["pv_mw", "target_ssr_pct"],
  S12_BTM_GC_TARGET_BESS: ["pv_mw", "target_gc_mw"],
  S21_BTM_SSR_CURVE: ["pv_mw"],
  S22_BTM_GC_CURVE: ["pv_mw"],
  S31_SUB_GC_TARGET_BESS: ["target_gc_mw"],
  S32_SUB_GC_CURVE: [],
  S33_SUB_FIXED_BESS_EVAL: ["bess_mw", "bess_mwh", "grid_ceiling_mw"],
  S34_SUB_FIRMNESS_TARGET_BESS: ["target_firmness_pct", "grid_ceiling_mw"],
  S41_OPERATIONAL_VERIFY: ["pv_mw", "bess_mw", "bess_mwh", "grid_ceiling_mw"],
  S42_DELIVERABLE_BESS_SIZE: ["pv_mw", "deliverable_target", "target_ssr_pct", "target_gc_mw"],
  S51_BTM_GCMIN_BESSOPT: ["pv_mw"],
  S52_BTM_GCMIN_FIXED_DESIGN: ["pv_mw", "bess_mw", "bess_mwh"],
  S62_BTM_PVBESS_SSR_SURFACE: ["pv_sweep_mw", "ssr_targets_pct"],
  S71_BTM_SSR_PLUS_GC_BESS: ["pv_mw", "target_ssr_pct", "target_gc_mw"],
  S91_OFFGRID_FIRMNESS_PVBESS: ["target_firmness_pct", "pv_sweep_mw"],
  S93_STANDALONE_EXPORTLIMIT_BESS: ["pv_mw", "ssr_targets_pct"],
};

export const DEFAULT_FIELDS = [
  "pv_mw", "target_ssr_pct", "target_gc_mw", "bess_mw",
  "bess_mwh", "grid_ceiling_mw", "target_firmness_pct",
];

// Shared Plotly layout so every chart matches the dark theme.
export const PLOT_THEME = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#e6edf3", family: "-apple-system, Segoe UI, sans-serif", size: 12 },
  margin: { l: 60, r: 24, t: 36, b: 48 },
  xaxis: { gridcolor: "#2d3742", zerolinecolor: "#2d3742" },
  yaxis: { gridcolor: "#2d3742", zerolinecolor: "#2d3742" },
  legend: { orientation: "h", y: -0.2 },
  colorway: ["#f5a623", "#3fb6ff", "#3fb950", "#f85149", "#a371f7"],
};
export const PLOT_CONFIG = { displayModeBar: false, responsive: true };
