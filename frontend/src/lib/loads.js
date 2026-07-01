// Shared load-type registry — the single definition of consumer types, used by
// Step 1 (multi-load picker) and the Step 2 FlowDesigner (consumer nodes). Keeping
// it here means the loads chosen in Step 1 flow straight into Step 2 and beyond.

export const LOAD_TYPES = {
  data_centre: { label: "Data Centre",        icon: "🖥️", color: "#3f7cac", peak: 24, base: 14 },
  port:        { label: "Port / Cold Ironing", icon: "⚓",  color: "#15616d", peak: 20, base: 6 },
  ev_charging: { label: "EV Charging Hub",     icon: "🔌", color: "#7a6f9b", peak: 12, base: 2 },
  industrial:  { label: "Industrial",          icon: "🏭", color: "#c2603a", peak: 30, base: 18 },
  hvac:        { label: "HVAC / Chiller",      icon: "❄️", color: "#5f8fbe", peak: 8,  base: 4 },
  generic:     { label: "Generic Load",        icon: "⚡", color: "#6b7780", peak: 10, base: 5 },
};

let _loadSeq = 0;
export const newLoadId = () => `load-${++_loadSeq}-${Date.now() % 100000}`;

// Build a fresh load entry of a given type.
export function makeLoad(load_type) {
  const m = LOAD_TYPES[load_type] || LOAD_TYPES.generic;
  return { id: newLoadId(), load_type, name: m.label, peak_mw: m.peak, baseline_mw: m.base };
}
