// Interactive land-parcel drawer. Drag the vertices to reshape the buildable
// parcel; the area is sent to the backend, which returns the AVAILABLE GENERATION
// (max solar/wind MW + yields, computed in core/site.py). No engineering formula
// lives in the front end — it only supplies the drawn area + location.
import { useEffect, useRef, useState } from "react";
import { parcelCapacity } from "../lib/api.js";
import { fmt } from "../lib/svg.js";

// Calibrated so the default polygon (~66050 px²) reads as 184 ha, matching the site.
const PX2_TO_HA = 0.002785768;
const INIT_PTS = [
  { x: 120, y: 70 }, { x: 470, y: 55 }, { x: 520, y: 200 },
  { x: 300, y: 250 }, { x: 100, y: 190 },
];

function areaHaOf(pts) {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const j = (i + 1) % pts.length;
    a += pts[i].x * pts[j].y - pts[j].x * pts[i].y;
  }
  return Math.abs(a / 2) * PX2_TO_HA;
}

export default function LandParcel({ tech, onChange }) {
  const [pts, setPts] = useState(INIT_PTS);
  const [lat, setLat] = useState(51.96);
  const [cap, setCap] = useState(null);   // backend-computed capacity
  const svgRef = useRef();
  const drag = useRef(null);
  const debounce = useRef(null);

  const areaHa = areaHaOf(pts);

  // Ask the backend for the available generation whenever the parcel/area changes
  // (debounced so dragging doesn't flood it). The numbers shown are all backend.
  useEffect(() => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      parcelCapacity({ area_ha: areaHa, lat, lon: 1.35 })
        .then((c) => {
          setCap(c);
          onChange({ areaHa: c.area_ha, maxSolarMw: c.max_solar_mw, maxWindMw: c.max_wind_mw });
        })
        .catch(() => {});
    }, 180);
    return () => clearTimeout(debounce.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [areaHa, lat]);

  function toLocal(e) {
    const svg = svgRef.current;
    const p = svg.createSVGPoint();
    p.x = e.clientX; p.y = e.clientY;
    return p.matrixTransform(svg.getScreenCTM().inverse());
  }
  function onMove(e) {
    if (drag.current == null) return;
    const loc = toLocal(e);
    setPts((prev) => prev.map((p, i) => (i === drag.current ? { x: loc.x, y: loc.y } : p)));
  }
  useEffect(() => {
    const up = () => { drag.current = null; };
    window.addEventListener("mouseup", up);
    return () => window.removeEventListener("mouseup", up);
  }, []);

  const d = pts.map((p, i) => (i ? "L" : "M") + p.x + " " + p.y).join(" ") + " Z";
  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;

  return (
    <div>
      <div style={{ position: "relative", border: "1px solid var(--line2)", borderRadius: 10, overflow: "hidden" }}>
        <svg ref={svgRef} viewBox="0 0 700 300" width="100%" height="300"
             onMouseMove={onMove} style={{ display: "block", cursor: "crosshair" }}>
          <defs>
            <linearGradient id="land" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="#cfe0cf" /><stop offset="1" stopColor="#b9d2bf" />
            </linearGradient>
            <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
              <path d="M40 0H0V40" fill="none" stroke="#a9c2af" strokeWidth="1" />
            </pattern>
          </defs>
          <rect width="700" height="300" fill="url(#land)" />
          <rect width="700" height="300" fill="url(#grid)" />
          <path d={d} fill="rgba(31,138,138,.22)" stroke="#15616d" strokeWidth="2.5" strokeDasharray="6 4" />
          <text x={cx} y={cy} fill="#15616d" fontSize="14" fontWeight="700" textAnchor="middle">Buildable parcel</text>
          {pts.map((p, i) => (
            <circle key={i} cx={p.x} cy={p.y} r="7" fill="#15616d" style={{ cursor: "grab" }}
                    onMouseDown={(e) => { drag.current = i; e.preventDefault(); }} />
          ))}
        </svg>
        <div style={{ position: "absolute", bottom: 12, right: 12, background: "rgba(255,255,255,.95)",
          padding: "8px 14px", borderRadius: 8, fontSize: 12.5, boxShadow: "0 1px 5px rgba(0,0,0,.15)" }}>
          Parcel <b style={{ color: "var(--teal-dark)" }}>{areaHa.toFixed(1)} ha</b>
        </div>
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <div style={{ maxWidth: 160 }}><label className="fld">Latitude (°)</label>
          <input type="number" step="0.01" value={lat} onChange={(e) => setLat(+e.target.value)} /></div>
      </div>

      <div className="feaskpis" style={{ marginTop: 12 }}>
        <div className="kp"><div className="n" style={{ color: "var(--orange)" }}>{fmt.mw(cap?.max_solar_mw)}p</div>
          <div className="l">Max solar (backend)</div></div>
        <div className="kp"><div className="n" style={{ color: "var(--teal)" }}>{fmt.mw(cap?.max_wind_mw)}</div>
          <div className="l">Max wind {tech?.wind ? "" : "(tech off)"}</div></div>
        <div className="kp"><div className="n">{cap ? `${cap.solar_cf_pct}%` : "—"}</div>
          <div className="l">Solar capacity factor</div></div>
        <div className="kp"><div className="n">{cap ? fmt.gwh(cap.solar_gwh * 1000) : "—"}</div>
          <div className="l">Solar yield / yr</div></div>
      </div>
      <div className="subtle" style={{ marginTop: 2 }}>
        Drag to reshape; capacity + yield come from the backend (core/site.py). Max solar becomes the PV nameplate the engine sizes against.
      </div>
    </div>
  );
}
