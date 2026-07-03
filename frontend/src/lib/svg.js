// Tiny hand-rolled SVG chart helpers — the same approach as the mock UI, but as
// pure functions returning path strings. Kept dependency-free (no plotly) so the
// NatPower screens render fast and match the mock's visual language exactly.

// Build an SVG polyline path "M x y L x y …" across a numeric series.
export function linePath(data, w, h, max, min = 0) {
  const n = data.length;
  if (n < 2 || max === min) return "";
  return data
    .map((v, i) => {
      const x = (i / (n - 1)) * w;
      const y = h - ((v - min) / (max - min)) * h;
      return `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

// Closed area under a series (line + baseline back to start).
export function areaPath(data, w, h, max, min = 0) {
  const line = linePath(data, w, h, max, min);
  if (!line) return "";
  return `${line} L${w} ${h} L0 ${h} Z`;
}

// Downsample a long series to ~targetN points by stride sampling (keeps shape,
// keeps render cheap for an 8760/35040-length year).
export function downsample(data, targetN = 240) {
  if (!data || data.length <= targetN) return data || [];
  const stride = Math.ceil(data.length / targetN);
  const out = [];
  for (let i = 0; i < data.length; i += stride) out.push(data[i]);
  return out;
}

// A window of the series (e.g. one sample week) by index range.
export function slice(data, start, count) {
  if (!data) return [];
  return data.slice(start, start + count);
}

export const fmt = {
  mw: (v) => (v == null ? "—" : `${(+v).toFixed(0)} MW`),
  mwh: (v) => (v == null ? "—" : `${(+v).toFixed(0)} MWh`),
  gwh: (v) => (v == null ? "—" : `${(+v / 1000).toFixed(1)} GWh`),
  pct: (v) => (v == null ? "—" : `${(+v).toFixed(0)}%`),
  pct1: (v) => (v == null ? "—" : `${(+v).toFixed(1)}%`),
  eurM: (v) => (v == null ? "—" : `€${(+v).toFixed(0)} M`),
  eurM1: (v) => (v == null ? "—" : `€${(+v).toFixed(1)} M`),
  num: (v, d = 1) => (v == null ? "—" : (+v).toFixed(d)),
};
