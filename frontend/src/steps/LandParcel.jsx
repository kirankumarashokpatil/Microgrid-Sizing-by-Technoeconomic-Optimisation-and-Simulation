// Land parcel per technology (solar/wind compete for land). Draws on a REAL map
// (see MapParcel) centred on the entered lat/lon; the drawn area is sent to the
// backend, which returns the available generation (max MW + yield, core/site.py).
import { useEffect, useRef, useState } from "react";
import { parcelCapacity } from "../lib/api.js";
import { fmt } from "../lib/svg.js";
import MapParcel from "./MapParcel.jsx";

const KIND = {
  solar: { label: "Solar PV", icon: "☀️", color: "#b8741f", unit: "MWp", pick: (c) => c.max_solar_mw, yield: (c) => c.solar_gwh },
  wind:  { label: "Wind", icon: "🌬️", color: "#15616d", unit: "MW", pick: (c) => c.max_wind_mw, yield: (c) => c.wind_gwh },
};

export default function LandParcel({ kind, onChange }) {
  const K = KIND[kind] || KIND.solar;
  const [areaHa, setAreaHa] = useState(184);
  const [lat, setLat] = useState(51.96);
  const [lon, setLon] = useState(1.35);
  const [cap, setCap] = useState(null);
  const debounce = useRef(null);

  // Fetch backend capacity when the drawn area or location changes (debounced).
  useEffect(() => {
    clearTimeout(debounce.current);
    debounce.current = setTimeout(() => {
      parcelCapacity({ area_ha: areaHa, lat, lon })
        .then((c) => { setCap(c); onChange({ areaHa: c.area_ha, maxMw: K.pick(c), lat, lon }); })
        .catch(() => {});
    }, 200);
    return () => clearTimeout(debounce.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [areaHa, lat, lon]);

  const maxMw = cap ? K.pick(cap) : null;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, color: K.color, fontWeight: 700 }}>
        <span style={{ fontSize: 17 }}>{K.icon}</span> {K.label} parcel
        <span style={{ marginLeft: "auto", fontSize: 12.5, color: "var(--slate)" }}>Area <b style={{ color: K.color }}>{areaHa.toFixed(1)} ha</b></span>
      </div>

      <MapParcel lat={lat} lon={lon} color={K.color} onArea={setAreaHa} />

      <div className="row" style={{ marginTop: 12 }}>
        <div style={{ maxWidth: 150 }}><label className="fld">Latitude (°)</label>
          <input type="number" step="0.01" value={lat} onChange={(e) => setLat(+e.target.value)} /></div>
        <div style={{ maxWidth: 150 }}><label className="fld">Longitude (°)</label>
          <input type="number" step="0.01" value={lon} onChange={(e) => setLon(+e.target.value)} /></div>
      </div>

      <div className="feaskpis" style={{ marginTop: 8 }}>
        <div className="kp"><div className="n" style={{ color: K.color }}>{maxMw != null ? `${maxMw} ${K.unit}` : "—"}</div>
          <div className="l">Max {K.label} (available gen)</div></div>
        {kind === "solar" && <div className="kp"><div className="n">{cap ? `${cap.solar_cf_pct}%` : "—"}</div><div className="l">Capacity factor</div></div>}
        <div className="kp"><div className="n">{cap ? fmt.gwh(K.yield(cap) * 1000) : "—"}</div><div className="l">Yield / yr</div></div>
        <div className="kp"><div className="n">{areaHa.toFixed(0)} ha</div><div className="l">Buildable area</div></div>
      </div>
      <div className="subtle" style={{ marginTop: 2 }}>
        Drag the corner markers on the map to shape the land; capacity + yield are computed by the backend (core/site.py).
      </div>
    </div>
  );
}
