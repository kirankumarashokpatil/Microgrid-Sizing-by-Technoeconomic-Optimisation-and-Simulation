// SiteDesigner.jsx — Unified GIS Site Planner & Land Parcel Designer
// Replaces separate Solar/Wind maps with a hero interactive GIS canvas.
// Supports:
//   • Full-screen interactive map (OSM Street + Esri Satellite imagery)
//   • Nominatim location search bar (fly to any town/city/site)
//   • Custom interactive polygon drawing (click vertices to shape land)
//   • Multi-parcel management (Figma-style layer list + right inspector)
//   • Live backend capacity & yield computation per parcel (core/site.py)
//   • Backward-compatible export to cfg.parcels.solar & cfg.parcels.wind

import { useEffect, useRef, useState, useCallback } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { parcelCapacity, parseBoundary, capacityFromMw } from "../lib/api.js";
import { LOAD_TYPES } from "../lib/loads.js";
import { fmt } from "../lib/svg.js";

const TECH_META = {
  land:       { label: "Available Land", icon: "🟫", color: "#9aa2ad", unit: "ha",  density: "Unassigned — subdivide" },
  solar:      { label: "Solar PV",   icon: "☀️", color: "#e0922f", unit: "MWp", density: "~0.52 MW/ha" },
  wind:       { label: "Wind Farm",  icon: "🌬️", color: "#1f8a8a", unit: "MW",  density: "~0.26 MW/ha" },
  battery:    { label: "BESS Yard",  icon: "🔋", color: "#2f8f5b", unit: "MW",  density: "Compact footprint" },
  load:       { label: "Load / Consumer", icon: "🏢", color: "#3f7cac", unit: "MW", density: "Demand footprint — reserves land" },
  substation: { label: "Substation", icon: "⚡", color: "#7a6f9b", unit: "kV",  density: "Grid interconnect" },
  reserve:    { label: "Reserve",    icon: "🟩", color: "#64748b", unit: "ha",  density: "Buffer / Future" },
};

// A parcel's visual identity. A "load" parcel borrows the icon/label/colour of
// its specific consumer type (Data Centre, Port, …) from the load registry.
function metaFor(p) {
  if (p?.tech === "load") {
    const lt = LOAD_TYPES[p.loadType] || LOAD_TYPES.generic;
    return { label: lt.label, icon: lt.icon, color: lt.color, unit: "MW", density: "Demand footprint — reserves land" };
  }
  return TECH_META[p?.tech] || TECH_META.reserve;
}

// Which tech types are generation (contribute nameplate) vs land-consuming only.
const GENERATION = new Set(["solar", "wind", "battery"]);

// Geodesic polygon area (equirectangular projection about the centroid -> shoelace).
function polygonAreaHa(latlngs) {
  if (!latlngs || latlngs.length < 3) return 0;
  const R = 6378137;
  const lat0 = (latlngs.reduce((s, p) => s + p.lat, 0) / latlngs.length) * Math.PI / 180;
  const pts = latlngs.map((p) => ({
    x: (p.lng * Math.PI / 180) * Math.cos(lat0) * R,
    y: (p.lat * Math.PI / 180) * R,
  }));
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const j = (i + 1) % pts.length;
    a += pts[i].x * pts[j].y - pts[j].x * pts[i].y;
  }
  return Math.abs(a / 2) / 10000; // m² -> ha
}

// Scale a ring about its centroid so its area becomes targetHa (area ∝ scale²).
// Lets the inspector "size by area / MW" reshape the polygon to match a number.
function rescaleRingToArea(latlngs, targetHa) {
  const cur = polygonAreaHa(latlngs);
  if (cur <= 1e-9 || targetHa <= 0) return latlngs;
  const f = Math.sqrt(targetHa / cur);
  const cLat = latlngs.reduce((s, p) => s + p.lat, 0) / latlngs.length;
  const cLng = latlngs.reduce((s, p) => s + p.lng, 0) / latlngs.length;
  return latlngs.map((p) => ({ lat: cLat + (p.lat - cLat) * f, lng: cLng + (p.lng - cLng) * f }));
}

// Generate default ring around lat/lon
function defaultRing(lat, lon, scale = 1) {
  const dLat = 0.0055 * scale;
  const dLng = (0.009 / Math.max(0.3, Math.cos(lat * Math.PI / 180))) * scale;
  return [
    { lat: lat + dLat, lng: lon - dLng },
    { lat: lat + dLat, lng: lon + dLng },
    { lat: lat - dLat * 0.6, lng: lon + dLng * 1.1 },
    { lat: lat - dLat, lng: lon },
    { lat: lat - dLat * 0.6, lng: lon - dLng * 1.1 },
  ];
}

let _pid = 100;
const nextPid = () => `pcl-${++_pid}`;

// Size a generation parcel by AREA or by GENERATION (MW) — Asif's radio + input.
// Applying reshapes the polygon to match; the reciprocal is computed by the backend.
function GenSizer({ parcel, onArea, onMw }) {
  const [mode, setMode] = useState("area");   // 'area' | 'mw'
  const [val, setVal] = useState("");
  useEffect(() => {
    setVal(String(mode === "area" ? Math.round(parcel.areaHa) : Math.round(parcel.maxMw)));
  }, [parcel.id, parcel.areaHa, parcel.maxMw, mode]);

  const apply = () => {
    const n = parseFloat(val);
    if (!(n > 0)) return;
    if (mode === "area") onArea(n); else onMw(n);
  };

  return (
    <div style={{ marginBottom: 14 }}>
      <label className="fld" style={{ marginBottom: 4 }}>Size by</label>
      <div style={{ display: "flex", background: "#e2e8f0", padding: 3, borderRadius: 8, gap: 2, marginBottom: 8 }}>
        {[["area", "Area (ha)"], ["mw", "Generation (MW)"]].map(([k, label]) => (
          <button key={k} type="button" onClick={() => setMode(k)}
            style={{ flex: 1, padding: "5px 8px", border: "none", borderRadius: 6, fontSize: 12, fontWeight: 600,
              cursor: "pointer", background: mode === k ? "#fff" : "transparent",
              color: mode === k ? "#0f172a" : "#64748b", boxShadow: mode === k ? "0 1px 3px rgba(0,0,0,.1)" : "none" }}>
            {label}
          </button>
        ))}
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <input type="number" value={val} min="0" onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && apply()}
          style={{ flex: 1, padding: "7px 10px", border: "1px solid #cbd5e1", borderRadius: 8, fontWeight: 600, fontSize: 13 }} />
        <span style={{ alignSelf: "center", fontSize: 12.5, color: "#64748b", width: 26 }}>{mode === "area" ? "ha" : "MW"}</span>
        <button type="button" onClick={apply}
          style={{ padding: "7px 12px", background: "#15616d", color: "#fff", border: "none", borderRadius: 8,
            fontWeight: 600, fontSize: 12.5, cursor: "pointer" }}>
          Set
        </button>
      </div>
      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
        {mode === "area"
          ? "Reshapes the parcel to this area; capacity follows."
          : "Reshapes the parcel to fit this nameplate at build density."}
      </div>
    </div>
  );
}

export default function SiteDesigner({ cfg, patch }) {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const layersRef = useRef({ street: null, satellite: null });
  const parcelsGroupRef = useRef(null);
  const drawGroupRef = useRef(null);
  const fileInputRef = useRef(null);

  // UI state
  const [activeLayer, setActiveLayer] = useState("street"); // 'street' | 'satellite'
  const [tool, setTool] = useState("select"); // 'select' | 'draw'
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [importing, setImporting] = useState(false);
  const [drawPoints, setDrawPoints] = useState([]);
  const [drawCursor, setDrawCursor] = useState(null);

  // Initial load or default parcels
  const [parcels, setParcels] = useState(() => {
    if (cfg.parcels?.list && cfg.parcels.list.length > 0) {
      return cfg.parcels.list;
    }
    // Default initial parcels: 1 Solar + 1 Wind side by side
    const baseLat = cfg.parcels?.solar?.lat || 51.96;
    const baseLon = cfg.parcels?.solar?.lon || 1.35;
    return [
      {
        id: "pcl-1",
        name: "Solar Parcel A",
        tech: "solar",
        areaHa: 184,
        maxMw: 95,
        yieldGwh: 140,
        cfPct: 17,
        latlngs: defaultRing(baseLat, baseLon - 0.012, 0.9),
      },
      {
        id: "pcl-2",
        name: "Wind Parcel B",
        tech: "wind",
        areaHa: 210,
        maxMw: 54,
        yieldGwh: 185,
        cfPct: 39,
        latlngs: defaultRing(baseLat, baseLon + 0.014, 1.05),
      },
    ];
  });

  const [selectedId, setSelectedId] = useState(() => parcels[0]?.id || null);
  const selectedParcel = parcels.find((p) => p.id === selectedId) || null;

  // Notify parent of changes with backward compatibility
  const syncToParent = useCallback((nextParcels) => {
    const solarList = nextParcels.filter((p) => p.tech === "solar");
    const windList = nextParcels.filter((p) => p.tech === "wind");

    const totalSolarHa = solarList.reduce((s, p) => s + (p.areaHa || 0), 0);
    const totalSolarMw = solarList.reduce((s, p) => s + (p.maxMw || 0), 0);
    const totalWindHa = windList.reduce((s, p) => s + (p.areaHa || 0), 0);
    const totalWindMw = windList.reduce((s, p) => s + (p.maxMw || 0), 0);

    const first = nextParcels[0] || {};
    const centerLat = first.latlngs?.[0]?.lat || 51.96;
    const centerLon = first.latlngs?.[0]?.lng || 1.35;

    patch({
      parcels: {
        list: nextParcels,
        solar: { areaHa: totalSolarHa, maxMw: totalSolarMw || 95, lat: centerLat, lon: centerLon },
        wind: { areaHa: totalWindHa, maxMw: totalWindMw || 54, lat: centerLat, lon: centerLon },
      },
    });
  }, [patch]);

  // Backend capacity calculations
  const computeBackendStats = useCallback((latlngs, tech) => {
    if (!latlngs || latlngs.length < 3) return Promise.resolve(null);
    const area = polygonAreaHa(latlngs);
    const lat = latlngs[0].lat;
    const lon = latlngs[0].lng;

    if (tech === "battery") return Promise.resolve({ areaHa: area, maxMw: Math.round(area * 15), yieldGwh: 0, cfPct: 0 });
    // Land, load footprints, substation and reserve host no generation — area only.
    if (tech === "land" || tech === "load" || tech === "substation" || tech === "reserve")
      return Promise.resolve({ areaHa: area, maxMw: 0, yieldGwh: 0, cfPct: 0 });

    return parcelCapacity({ area_ha: area, lat, lon })
      .then((c) => {
        if (tech === "solar") {
          return { areaHa: c.area_ha, maxMw: c.max_solar_mw, yieldGwh: c.solar_gwh, cfPct: c.solar_cf_pct };
        }
        return { areaHa: c.area_ha, maxMw: c.max_wind_mw, yieldGwh: c.wind_gwh, cfPct: 38 };
      })
      .catch(() => ({
        areaHa: area,
        maxMw: Math.round(area * (tech === "solar" ? 0.52 : 0.26)),
        yieldGwh: Math.round(area * (tech === "solar" ? 0.76 : 0.88)),
        cfPct: tech === "solar" ? 17 : 38,
      }));
  }, []);

  // Update a parcel property
  const updateParcel = useCallback((id, updates) => {
    setParcels((prev) => {
      const next = prev.map((p) => (p.id === id ? { ...p, ...updates } : p));
      syncToParent(next);
      return next;
    });
  }, [syncToParent]);

  // Recalculate capacity when technology or shape changes
  const handleShapeOrTechChange = useCallback(async (id, newLatLngs, newTech) => {
    const target = parcels.find((p) => p.id === id);
    if (!target) return;
    const ll = newLatLngs || target.latlngs;
    const tch = newTech || target.tech;
    // A newly-assigned load parcel needs a concrete consumer type to display.
    const loadDefault = tch === "load" && !target.loadType ? { loadType: "data_centre" } : {};
    const stats = await computeBackendStats(ll, tch);
    if (stats) {
      updateParcel(id, { latlngs: ll, tech: tch, ...loadDefault, ...stats });
    } else {
      updateParcel(id, { latlngs: ll, tech: tch, ...loadDefault, areaHa: polygonAreaHa(ll) });
    }
  }, [parcels, computeBackendStats, updateParcel]);

  // Reciprocal sizing (Phase 2): set a generation parcel by AREA or by MW. Both
  // reshape the polygon about its centroid so the map and the numbers stay in sync.
  const sizeByArea = useCallback(async (id, targetHa) => {
    const p = parcels.find((x) => x.id === id);
    if (!p) return;
    await handleShapeOrTechChange(id, rescaleRingToArea(p.latlngs, targetHa), p.tech);
  }, [parcels, handleShapeOrTechChange]);

  const sizeByMw = useCallback(async (id, targetMw) => {
    const p = parcels.find((x) => x.id === id);
    if (!p) return;
    const lat = p.latlngs[0]?.lat ?? 51.96, lon = p.latlngs[0]?.lng ?? 1.35;
    try {
      const c = await capacityFromMw({ mw: targetMw, tech: p.tech, lat, lon });
      updateParcel(id, {
        latlngs: rescaleRingToArea(p.latlngs, c.area_ha), areaHa: c.area_ha,
        maxMw: Math.round(targetMw),
        yieldGwh: p.tech === "wind" ? c.wind_gwh : c.solar_gwh,
        cfPct: p.tech === "wind" ? 38 : c.solar_cf_pct,
      });
    } catch {
      const ha = targetMw / (p.tech === "wind" ? 0.26 : 0.9);   // density fallback
      updateParcel(id, { latlngs: rescaleRingToArea(p.latlngs, ha), areaHa: ha, maxMw: Math.round(targetMw) });
    }
  }, [parcels, updateParcel]);

  // Initialize Leaflet Map
  useEffect(() => {
    if (!mapContainerRef.current) return;
    const center = parcels[0]?.latlngs?.[0] ? [parcels[0].latlngs[0].lat, parcels[0].latlngs[0].lng] : [51.96, 1.35];
    const map = L.map(mapContainerRef.current, {
      center,
      zoom: 13,
      zoomControl: false,
      attributionControl: false,
    });
    L.control.zoom({ position: "bottomright" }).addTo(map);

    const street = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 });
    const satellite = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19 });

    street.addTo(map);
    layersRef.current = { street, satellite };
    mapRef.current = map;

    const parcelsGroup = L.layerGroup().addTo(map);
    parcelsGroupRef.current = parcelsGroup;
    const drawGroup = L.layerGroup().addTo(map);
    drawGroupRef.current = drawGroup;

    // Click handler for drawing
    map.on("click", (e) => {
      setTool((currentTool) => {
        if (currentTool === "draw") {
          setDrawPoints((pts) => [...pts, { lat: e.latlng.lat, lng: e.latlng.lng }]);
        }
        return currentTool;
      });
    });

    map.on("mousemove", (e) => {
      setDrawCursor({ lat: e.latlng.lat, lng: e.latlng.lng });
    });

    // Fit initial bounds
    setTimeout(() => {
      map.invalidateSize();
    }, 150);

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Layer switching
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (activeLayer === "satellite") {
      map.removeLayer(layersRef.current.street);
      map.addLayer(layersRef.current.satellite);
    } else {
      map.removeLayer(layersRef.current.satellite);
      map.addLayer(layersRef.current.street);
    }
  }, [activeLayer]);

  // Render parcels on map
  useEffect(() => {
    const group = parcelsGroupRef.current;
    const map = mapRef.current;
    if (!group || !map) return;
    group.clearLayers();

    parcels.forEach((p) => {
      const meta = metaFor(p);
      const isSel = p.id === selectedId;

      const poly = L.polygon(
        p.latlngs.map((pt) => [pt.lat, pt.lng]),
        {
          color: meta.color,
          weight: isSel ? 3.5 : 2,
          dashArray: isSel ? null : "6 4",
          fillColor: meta.color,
          fillOpacity: isSel ? 0.35 : 0.18,
        }
      ).addTo(group);

      poly.on("click", (e) => {
        L.DomEvent.stopPropagation(e);
        setSelectedId(p.id);
        setTool("select");
      });

      // Render draggable vertex markers if selected
      if (isSel && tool === "select") {
        p.latlngs.forEach((pt, idx) => {
          const icon = L.divIcon({
            className: "",
            iconSize: [16, 16],
            iconAnchor: [8, 8],
            html: `<div style="width:14px;height:14px;border-radius:50%;background:${meta.color};border:2.5px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,0.5);cursor:grab;"></div>`,
          });
          const m = L.marker([pt.lat, pt.lng], { draggable: true, icon }).addTo(group);
          m.on("drag", () => {
            const curPts = p.latlngs.map((o, i) => (i === idx ? { lat: m.getLatLng().lat, lng: m.getLatLng().lng } : o));
            poly.setLatLngs(curPts.map((x) => [x.lat, x.lng]));
          });
          m.on("dragend", () => {
            const newPts = p.latlngs.map((o, i) => (i === idx ? { lat: m.getLatLng().lat, lng: m.getLatLng().lng } : o));
            handleShapeOrTechChange(p.id, newPts, p.tech);
          });
        });
      }

      // Tooltip label centered inside polygon
      if (p.latlngs.length >= 3) {
        const cLat = p.latlngs.reduce((s, pt) => s + pt.lat, 0) / p.latlngs.length;
        const cLng = p.latlngs.reduce((s, pt) => s + pt.lng, 0) / p.latlngs.length;
        const badge = L.divIcon({
          className: "",
          iconSize: [140, 26],
          iconAnchor: [70, 13],
          html: `<div style="background:rgba(255,255,255,0.92);border:1px solid ${meta.color};color:#1e293b;font-weight:700;font-size:11.5px;padding:3px 8px;border-radius:14px;box-shadow:0 2px 6px rgba(0,0,0,0.15);text-align:center;white-space:nowrap;pointer-events:none;display:inline-block;">${meta.icon} ${p.name} (${Math.round(p.areaHa)} ha)</div>`,
        });
        L.marker([cLat, cLng], { icon: badge, interactive: false }).addTo(group);
      }
    });
  }, [parcels, selectedId, tool, handleShapeOrTechChange]);

  // Render active drawing rubber-band
  useEffect(() => {
    const group = drawGroupRef.current;
    if (!group) return;
    group.clearLayers();

    if (tool === "draw" && drawPoints.length > 0) {
      const pts = drawPoints.map((p) => [p.lat, p.lng]);
      if (drawCursor) pts.push([drawCursor.lat, drawCursor.lng]);

      L.polyline(pts, { color: "#15616d", weight: 2.5, dashArray: "5 5" }).addTo(group);

      drawPoints.forEach((pt) => {
        const icon = L.divIcon({
          className: "",
          iconSize: [12, 12],
          iconAnchor: [6, 6],
          html: `<div style="width:10px;height:10px;border-radius:50%;background:#15616d;border:2px solid #fff;"></div>`,
        });
        L.marker([pt.lat, pt.lng], { icon }).addTo(group);
      });
    }
  }, [tool, drawPoints, drawCursor]);

  // Finish polygon drawing
  const finishDrawing = async () => {
    if (drawPoints.length < 3) {
      alert("Please click at least 3 points on the map to shape a parcel.");
      return;
    }
    const id = nextPid();
    const techCount = parcels.filter((p) => p.tech === "solar").length + 1;
    const name = `Solar Parcel ${String.fromCharCode(64 + techCount)}`;
    const area = polygonAreaHa(drawPoints);

    const newParcel = {
      id,
      name,
      tech: "solar",
      areaHa: area,
      maxMw: Math.round(area * 0.52),
      yieldGwh: Math.round(area * 0.76),
      cfPct: 17,
      latlngs: [...drawPoints],
    };

    setParcels((prev) => {
      const next = [...prev, newParcel];
      syncToParent(next);
      return next;
    });
    setSelectedId(id);
    setDrawPoints([]);
    setTool("select");

    // Fetch accurate capacity asynchronously
    const stats = await computeBackendStats(newParcel.latlngs, "solar");
    if (stats) updateParcel(id, stats);
  };

  // Import a land boundary file (GeoJSON / KMZ / KML) → one parcel per ring.
  // The backend (core/boundary.py) parses the geometry and returns rings with
  // area + centroid; we then fetch generation capacity per parcel like a draw.
  const handleImport = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-uploading the same file
    if (!file) return;
    setImporting(true);
    try {
      const { rings } = await parseBoundary(file);
      if (!rings || rings.length === 0) {
        alert("No polygon boundary was found in that file.");
        return;
      }
      // Skip rings we already hold (re-importing the same file) — match on name
      // + area to the nearest hectare.
      const seen = new Set(parcels.map((p) => `${p.name}|${Math.round(p.areaHa)}`));
      const fresh = rings.filter((r) => !seen.has(`${r.name}|${Math.round(r.area_ha)}`));
      if (fresh.length === 0) {
        alert("Those parcels are already on the map.");
        return;
      }
      // Imported boundaries are the TOTAL available site — unassigned land the
      // developer then subdivides into generation and load footprints.
      const imported = fresh.map((r, i) => ({
        id: nextPid(),
        name: r.name || `Imported Parcel ${String.fromCharCode(65 + i)}`,
        tech: "land",
        areaHa: r.area_ha,
        maxMw: 0,
        yieldGwh: 0,
        cfPct: 0,
        latlngs: r.latlngs,
      }));

      let next;
      setParcels((prev) => {
        next = [...prev, ...imported];
        syncToParent(next);
        return next;
      });
      setSelectedId(imported[0].id);
      setTool("select");

      // Fit the map to everything we just imported.
      const allPts = imported.flatMap((p) => p.latlngs.map((pt) => [pt.lat, pt.lng]));
      if (mapRef.current && allPts.length) {
        mapRef.current.fitBounds(allPts, { padding: [40, 40], maxZoom: 15 });
      }
    } catch (err) {
      alert("Boundary import failed: " + (err.message || err));
    } finally {
      setImporting(false);
    }
  };

  // Location search using free Nominatim API
  const handleSearch = async (e) => {
    e.preventDefault();
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(searchQuery)}`);
      const data = await res.json();
      if (data && data.length > 0) {
        const lat = parseFloat(data[0].lat);
        const lon = parseFloat(data[0].lon);
        if (mapRef.current) {
          mapRef.current.setView([lat, lon], 14, { animate: true });
        }
      } else {
        alert("Location not found. Try another city or site name.");
      }
    } catch {
      alert("Geocoding search failed. Please check network.");
    } finally {
      setSearching(false);
    }
  };

  // KPI Summary totals
  const totalArea = parcels.reduce((s, p) => s + (p.areaHa || 0), 0);
  const totalSolarMw = parcels.filter((p) => p.tech === "solar").reduce((s, p) => s + (p.maxMw || 0), 0);
  const totalWindMw = parcels.filter((p) => p.tech === "wind").reduce((s, p) => s + (p.maxMw || 0), 0);
  const totalYieldGwh = parcels.reduce((s, p) => s + (p.yieldGwh || 0), 0);
  // Land accounting: load / substation / reserve footprints are reserved away
  // from the land available to generation.
  const loadArea = parcels.filter((p) => p.tech === "load").reduce((s, p) => s + (p.areaHa || 0), 0);
  const reservedArea = parcels
    .filter((p) => p.tech === "load" || p.tech === "substation" || p.tech === "reserve")
    .reduce((s, p) => s + (p.areaHa || 0), 0);
  const generationLand = Math.max(0, totalArea - reservedArea);

  return (
    <div style={{ marginTop: 20 }}>
      {/* Top Section Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>GIS Site & Land Parcel Designer</h3>
          <div className="hint" style={{ margin: "3px 0 0" }}>
            Start from the land: <b>import a GeoJSON / KMZ boundary</b> or <b>draw it on the map</b>, assign renewable
            technologies, and instantly compute generation capacity.
          </div>
        </div>
      </div>

      {/* Hero Unified Workspace */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          border: "1px solid #e2e8ea",
          borderRadius: 14,
          background: "#fff",
          boxShadow: "0 4px 16px rgba(0,0,0,0.06)",
          overflow: "hidden",
        }}
      >
        {/* Top Floating Control Bar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "10px 14px",
            borderBottom: "1px solid #e2e8ea",
            background: "#f8fafc",
            flexWrap: "wrap",
          }}
        >
          {/* Search form */}
          <form onSubmit={handleSearch} style={{ display: "flex", alignItems: "center", gap: 6, flex: 1, minWidth: 240 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                background: "#fff",
                border: "1px solid #cbd5e1",
                borderRadius: 8,
                padding: "4px 10px",
                flex: 1,
              }}
            >
              <span style={{ marginRight: 6 }}>🔍</span>
              <input
                type="text"
                placeholder="Search location (e.g. Felixstowe, Texas, London)..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                style={{ border: "none", outline: "none", width: "100%", fontSize: 13, background: "transparent" }}
              />
            </div>
            <button
              type="submit"
              disabled={searching}
              style={{
                padding: "6px 14px",
                background: "#15616d",
                color: "#fff",
                border: "none",
                borderRadius: 8,
                fontWeight: 600,
                fontSize: 12.5,
                cursor: "pointer",
              }}
            >
              {searching ? "..." : "Fly to"}
            </button>
          </form>

          {/* Import boundary (GeoJSON / KMZ / KML) */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".geojson,.json,.kml,.kmz"
            hidden
            onChange={handleImport}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            title="Upload a land boundary exported from GIS (GeoJSON, KMZ or KML)"
            style={{
              padding: "6px 12px",
              background: "#fff",
              color: "#0f172a",
              border: "1px solid #cbd5e1",
              borderRadius: 8,
              fontWeight: 600,
              fontSize: 12.5,
              cursor: importing ? "default" : "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {importing ? "Importing…" : "⬆ Import boundary"}
          </button>

          {/* Tools switch */}
          <div style={{ display: "flex", background: "#e2e8f0", padding: 3, borderRadius: 8, gap: 2 }}>
            <button
              type="button"
              onClick={() => {
                setTool("select");
                setDrawPoints([]);
              }}
              style={{
                padding: "5px 12px",
                border: "none",
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                background: tool === "select" ? "#fff" : "transparent",
                color: tool === "select" ? "#0f172a" : "#64748b",
                boxShadow: tool === "select" ? "0 1px 3px rgba(0,0,0,0.1)" : "none",
              }}
            >
              ⬆ Select / Edit
            </button>
            <button
              type="button"
              onClick={() => setTool("draw")}
              style={{
                padding: "5px 12px",
                border: "none",
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                background: tool === "draw" ? "#15616d" : "transparent",
                color: tool === "draw" ? "#fff" : "#64748b",
                boxShadow: tool === "draw" ? "0 1px 3px rgba(0,0,0,0.15)" : "none",
              }}
            >
              ✏️ Draw New Parcel
            </button>
          </div>

          {/* Layer switcher */}
          <div style={{ display: "flex", background: "#e2e8f0", padding: 3, borderRadius: 8, gap: 2 }}>
            <button
              type="button"
              onClick={() => setActiveLayer("street")}
              style={{
                padding: "5px 10px",
                border: "none",
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                background: activeLayer === "street" ? "#fff" : "transparent",
                color: activeLayer === "street" ? "#0f172a" : "#64748b",
              }}
            >
              🗺️ Map
            </button>
            <button
              type="button"
              onClick={() => setActiveLayer("satellite")}
              style={{
                padding: "5px 10px",
                border: "none",
                borderRadius: 6,
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                background: activeLayer === "satellite" ? "#fff" : "transparent",
                color: activeLayer === "satellite" ? "#0f172a" : "#64748b",
              }}
            >
              🛰️ Satellite
            </button>
          </div>
        </div>

        {/* Drawing instructions alert */}
        {tool === "draw" && (
          <div
            style={{
              padding: "8px 16px",
              background: "#f0fdf4",
              borderBottom: "1px solid #bbf7d0",
              color: "#166534",
              fontSize: 12.5,
              fontWeight: 600,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span>
              📍 Click points on the map canvas below to outline your parcel vertices ({drawPoints.length} placed).
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                onClick={finishDrawing}
                style={{
                  padding: "4px 12px",
                  background: "#16a34a",
                  color: "#fff",
                  border: "none",
                  borderRadius: 6,
                  fontWeight: 700,
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                ✓ Complete Parcel
              </button>
              <button
                type="button"
                onClick={() => {
                  setTool("select");
                  setDrawPoints([]);
                }}
                style={{
                  padding: "4px 10px",
                  background: "transparent",
                  color: "#64748b",
                  border: "1px solid #cbd5e1",
                  borderRadius: 6,
                  fontWeight: 600,
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Middle: Left Sidebar + Map Canvas + Right Inspector */}
        <div style={{ display: "flex", height: 480 }}>
          {/* Left Layers List */}
          <div
            style={{
              width: 220,
              borderRight: "1px solid #e2e8ea",
              background: "#f8fafc",
              overflowY: "auto",
              display: "flex",
              flexDirection: "column",
            }}
          >
            <div
              style={{
                padding: "10px 12px",
                fontSize: 10.5,
                fontWeight: 700,
                color: "#94a3b8",
                textTransform: "uppercase",
                letterSpacing: 0.5,
                borderBottom: "1px solid #f1f5f9",
              }}
            >
              Site Parcels ({parcels.length})
            </div>
            <div style={{ flex: 1, padding: "8px" }}>
              {parcels.map((p) => {
                const meta = metaFor(p);
                const isSel = p.id === selectedId;
                return (
                  <div
                    key={p.id}
                    onClick={() => {
                      setSelectedId(p.id);
                      setTool("select");
                    }}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "8px 10px",
                      borderRadius: 8,
                      background: isSel ? "#e6f3f3" : "transparent",
                      border: `1px solid ${isSel ? "#15616d" : "transparent"}`,
                      cursor: "pointer",
                      marginBottom: 4,
                      transition: "all .12s",
                    }}
                  >
                    <span style={{ fontSize: 16 }}>{meta.icon}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: 12.5,
                          fontWeight: 700,
                          color: isSel ? "#15616d" : "#1e293b",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {p.name}
                      </div>
                      <div style={{ fontSize: 11, color: "#64748b" }}>
                        {Math.round(p.areaHa)} ha
                        {GENERATION.has(p.tech) && ` · ${Math.round(p.maxMw)} ${meta.unit}`}
                        {p.tech === "load" && ` · ${meta.label}`}
                        {p.tech === "land" && " · unassigned"}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            <div style={{ padding: 10, borderTop: "1px solid #e2e8ea" }}>
              <button
                type="button"
                onClick={() => setTool("draw")}
                style={{
                  width: "100%",
                  padding: "8px",
                  background: "#fff",
                  border: "1px solid #cbd5e1",
                  borderRadius: 8,
                  color: "#0f172a",
                  fontWeight: 600,
                  fontSize: 12.5,
                  cursor: "pointer",
                }}
              >
                + Add New Parcel
              </button>
            </div>
          </div>

          {/* Hero Map Canvas */}
          <div
            ref={mapContainerRef}
            style={{
              flex: 1,
              position: "relative",
              cursor: tool === "draw" ? "crosshair" : "grab",
            }}
          />

          {/* Right Inspector */}
          <div
            style={{
              width: 260,
              borderLeft: "1px solid #e2e8ea",
              background: "#fff",
              overflowY: "auto",
              padding: "16px",
              display: "flex",
              flexDirection: "column",
            }}
          >
            {selectedParcel ? (
              <>
                <div style={{ fontSize: 10.5, fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", marginBottom: 12 }}>
                  Selected Parcel
                </div>

                {/* Name edit */}
                <label className="fld" style={{ marginBottom: 4 }}>Parcel Name</label>
                <input
                  type="text"
                  value={selectedParcel.name}
                  onChange={(e) => updateParcel(selectedParcel.id, { name: e.target.value })}
                  style={{
                    width: "100%",
                    padding: "7px 10px",
                    border: "1px solid #cbd5e1",
                    borderRadius: 8,
                    fontWeight: 700,
                    fontSize: 13.5,
                    marginBottom: 14,
                  }}
                />

                {/* Technology dropdown */}
                <label className="fld" style={{ marginBottom: 4 }}>Assigned Technology</label>
                <select
                  value={selectedParcel.tech}
                  onChange={(e) => handleShapeOrTechChange(selectedParcel.id, null, e.target.value)}
                  style={{
                    width: "100%",
                    padding: "8px 10px",
                    border: "1px solid #cbd5e1",
                    borderRadius: 8,
                    fontWeight: 600,
                    fontSize: 13,
                    marginBottom: 16,
                  }}
                >
                  {Object.entries(TECH_META).map(([key, meta]) => (
                    <option key={key} value={key}>
                      {meta.icon} {meta.label}
                    </option>
                  ))}
                </select>

                {/* Load-type picker — only for a load / consumer footprint */}
                {selectedParcel.tech === "load" && (
                  <>
                    <label className="fld" style={{ marginBottom: 4 }}>Load Type</label>
                    <select
                      value={selectedParcel.loadType || "data_centre"}
                      onChange={(e) => updateParcel(selectedParcel.id, { loadType: e.target.value })}
                      style={{
                        width: "100%",
                        padding: "8px 10px",
                        border: "1px solid #cbd5e1",
                        borderRadius: 8,
                        fontWeight: 600,
                        fontSize: 13,
                        marginBottom: 16,
                      }}
                    >
                      {Object.entries(LOAD_TYPES).map(([key, m]) => (
                        <option key={key} value={key}>
                          {m.icon} {m.label}
                        </option>
                      ))}
                    </select>
                  </>
                )}

                {/* Reciprocal sizing — generation parcels only (Phase 2) */}
                {GENERATION.has(selectedParcel.tech) && (
                  <GenSizer
                    parcel={selectedParcel}
                    onArea={(ha) => sizeByArea(selectedParcel.id, ha)}
                    onMw={(mw) => sizeByMw(selectedParcel.id, mw)}
                  />
                )}

                {/* KPI Metrics card inside inspector */}
                <div
                  style={{
                    background: "#f8fafc",
                    border: "1px solid #e2e8ea",
                    borderRadius: 10,
                    padding: "12px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 12, color: "#64748b" }}>Land Area</span>
                    <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>{selectedParcel.areaHa.toFixed(1)} ha</span>
                  </div>
                  {GENERATION.has(selectedParcel.tech) && (
                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 12, color: "#64748b" }}>Max Capacity</span>
                      <span style={{ fontSize: 13, fontWeight: 700, color: metaFor(selectedParcel).color }}>
                        {Math.round(selectedParcel.maxMw)} {metaFor(selectedParcel).unit}
                      </span>
                    </div>
                  )}
                  {selectedParcel.tech === "load" && (
                    <div style={{ display: "flex", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 12, color: "#64748b" }}>Reserved from generation</span>
                      <span style={{ fontSize: 13, fontWeight: 700, color: metaFor(selectedParcel).color }}>
                        {selectedParcel.areaHa.toFixed(1)} ha
                      </span>
                    </div>
                  )}
                  {(selectedParcel.tech === "solar" || selectedParcel.tech === "wind") && (
                    <>
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ fontSize: 12, color: "#64748b" }}>Annual Yield</span>
                        <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>{selectedParcel.yieldGwh} GWh/yr</span>
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ fontSize: 12, color: "#64748b" }}>Capacity Factor</span>
                        <span style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>{selectedParcel.cfPct}%</span>
                      </div>
                    </>
                  )}
                </div>

                <div style={{ marginTop: 12, fontSize: 11, color: "#94a3b8", lineHeight: 1.6 }}>
                  💡 Drag the circular markers on the map boundary to adjust the parcel geometry.
                </div>

                <button
                  type="button"
                  onClick={() => {
                    if (confirm(`Delete ${selectedParcel.name}?`)) {
                      const rem = parcels.filter((p) => p.id !== selectedParcel.id);
                      setParcels(rem);
                      syncToParent(rem);
                      setSelectedId(rem[0]?.id || null);
                    }
                  }}
                  style={{
                    marginTop: "auto",
                    width: "100%",
                    padding: "8px",
                    background: "#fff5f5",
                    border: "1px solid #fee2e2",
                    color: "#dc2626",
                    borderRadius: 8,
                    fontWeight: 600,
                    fontSize: 12.5,
                    cursor: "pointer",
                  }}
                >
                  🗑️ Delete Parcel
                </button>
              </>
            ) : (
              <div style={{ textAlign: "center", color: "#94a3b8", marginTop: 40 }}>
                <div style={{ fontSize: 28, marginBottom: 8 }}>🗺️</div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>No Parcel Selected</div>
                <div style={{ fontSize: 12, marginTop: 4 }}>Click any parcel on the map or left list to inspect details.</div>
              </div>
            )}
          </div>
        </div>

        {/* Bottom Project Summary KPI Strip */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
            gap: 8,
            borderTop: "1px solid #e2e8ea",
            background: "#f8fafc",
            padding: "12px 20px",
          }}
        >
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Total Site Area</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#0f172a", marginTop: 2 }}>{Math.round(totalArea)} ha</div>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Load Footprint</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#3f7cac", marginTop: 2 }}>{Math.round(loadArea)} ha</div>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Land for Generation</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#0f172a", marginTop: 2 }}>{Math.round(generationLand)} ha</div>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Solar PV Nameplate</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#e0922f", marginTop: 2 }}>{Math.round(totalSolarMw)} MWp</div>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Wind Farm Nameplate</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#1f8a8a", marginTop: 2 }}>{Math.round(totalWindMw)} MW</div>
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase" }}>Combined Generation</div>
            <div style={{ fontSize: 18, fontWeight: 800, color: "#166534", marginTop: 2 }}>{fmt.gwh(totalYieldGwh * 1000)} / yr</div>
          </div>
        </div>
      </div>
    </div>
  );
}
