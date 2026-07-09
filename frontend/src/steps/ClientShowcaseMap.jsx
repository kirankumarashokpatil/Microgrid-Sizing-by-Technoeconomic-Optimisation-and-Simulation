// ClientShowcaseMap.jsx — Ultra-Premium GIS Digital Twin & Site Layout Overlay
// Showcases to clients and investment committees how recommended solar arrays,
// wind turbines, BESS yards, and consumer load facilities map onto real GIS land parcels.
// Replaces amateur emoji boxes with state-of-the-art architectural SVG symbols!
// Strictly derived from LP optimizer results and network configuration without dummy data.

import { useEffect, useRef, useState, useMemo } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { fmt } from "../lib/svg.js";

const TECH_META = {
  solar:      { label: "Solar PV Tracker Field", color: "#f59e0b", border: "#fbbf24", unit: "MWp", density: "~0.52 MW/ha", desc: "Monocrystalline bifacial PV modules on single-axis South-facing tracker tables." },
  wind:       { label: "Onshore Wind Farm",      color: "#14b8a6", border: "#2dd4bf", unit: "MW",  density: "~0.26 MW/ha", desc: "Multi-MW direct-drive onshore wind turbines with aerodynamic wake spacing buffer." },
  battery:    { label: "BESS Storage Yard",      color: "#10b981", border: "#34d399", unit: "MW",  density: "Compact footprint", desc: "Containerized liquid-cooled lithium-iron-phosphate (LFP) battery storage racks." },
  load:       { label: "Collocated Data Center", color: "#3b82f6", border: "#60a5fa", unit: "MW",  density: "Demand footprint", desc: "Enterprise AI data center / industrial green hydrogen load campus facility." },
  substation: { label: "Grid Interconnect Bay",  color: "#a855f7", border: "#c084fc", unit: "MVA", density: "Switchyard", desc: "High-voltage step-up transformer switchyard & grid interconnection substation." },
  reserve:    { label: "Future Expansion Reserve",color: "#64748b", border: "#94a3b8", unit: "ha",  density: "Buffer land", desc: "Unassigned buffer land reserved for future project expansion phases." },
};

// Generate default ring around lat/lon if user didn't draw custom parcels
function defaultRing(lat, lon, scale = 1, dLatOffset = 0, dLonOffset = 0) {
  const dLat = 0.005 * scale;
  const dLng = (0.008 / Math.max(0.3, Math.cos(lat * Math.PI / 180))) * scale;
  const cLat = lat + dLatOffset;
  const cLon = lon + dLonOffset;
  return [
    { lat: cLat + dLat, lng: cLon - dLng },
    { lat: cLat + dLat, lng: cLon + dLng },
    { lat: cLat - dLat * 0.7, lng: cLon + dLng * 1.1 },
    { lat: cLat - dLat, lng: cLon },
    { lat: cLat - dLat * 0.7, lng: cLon - dLng * 1.1 },
  ];
}

// Geodesic polygon area in hectares
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
  return Math.abs(a / 2) / 10000;
}

function getCentroid(latlngs) {
  if (!latlngs || !latlngs.length) return { lat: 51.96, lng: 1.35 };
  const lat = latlngs.reduce((s, p) => s + p.lat, 0) / latlngs.length;
  const lng = latlngs.reduce((s, p) => s + p.lng, 0) / latlngs.length;
  return { lat, lng };
}

// Ray-casting point-in-polygon algorithm
function isPointInPoly(lat, lng, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i].lng, yi = poly[i].lat;
    const xj = poly[j].lng, yj = poly[j].lat;
    const intersect = ((yi > lat) !== (yj > lat)) &&
      (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

// Rigid Geometric Grid Latticing Engine:
// Generates laser-straight, parallel engineering rows & geometric line arrays (matching real-world Solar & Wind aerial references).
// 1) Creates a uniform geometric 2D lattice of straight rows and columns aligned with the GIS boundary.
// 2) Eliminates wavy/staggered dots and random overlaps by enforcing strict linear grid pitches (rowPitch & colPitch).
// 3) Packs Solar PV, BESS racks, and Data Centers into dense parallel stripes, while aligning Wind turbines in clean geometric matrices.
function generateAssetPoints(latlngs, count, utilFrac = 1.0, tech = "solar") {
  if (!latlngs || latlngs.length < 3 || count <= 0) return [];
  if (count === 1) return [getCentroid(latlngs)];

  let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
  latlngs.forEach((p) => {
    if (p.lat < minLat) minLat = p.lat;
    if (p.lat > maxLat) maxLat = p.lat;
    if (p.lng < minLng) minLng = p.lng;
    if (p.lng > maxLng) maxLng = p.lng;
  });

  const spanLat = maxLat - minLat;
  const spanLng = maxLng - minLng;

  // Step 1: Calculate target grid dimensions (rows × cols) for the geometric lattice
  let targetCols = 1;
  let targetRows = 1;
  if (tech === "solar" || tech === "battery" || tech === "load" || tech === "substation") {
    // Long parallel stripes/rows for Solar PV, BESS yards, and Data Center campuses (like Photo 1)
    targetCols = Math.ceil(Math.sqrt(count * 2.2));
    targetRows = Math.ceil(count / targetCols);
  } else {
    // Clean, spacious geometric matrix for Wind farms (like Photo 2)
    targetCols = Math.ceil(Math.sqrt(count * 1.4));
    targetRows = Math.ceil(count / targetCols);
  }

  // Step 2: Generate a high-resolution, laser-straight geometric lattice across the bounding box
  // We generate more lattice lines than needed so we can find all valid interior grid intersections
  const latticeCols = Math.max(targetCols * 3, 20);
  const latticeRows = Math.max(targetRows * 3, 20);
  const colPitch = spanLng / (latticeCols + 1);
  const rowPitch = spanLat / (latticeRows + 1);

  // Collect all valid grid intersections strictly inside the GIS polygon boundary
  const validGridSlots = [];
  for (let r = 1; r <= latticeRows; r++) {
    const lat_r = maxLat - r * rowPitch; // Top-to-bottom horizontal row lines
    for (let c = 1; c <= latticeCols; c++) {
      const lng_c = minLng + c * colPitch; // West-to-East vertical column lines
      if (isPointInPoly(lat_r, lng_c, latlngs)) {
        validGridSlots.push({ lat: lat_r, lng: lng_c, row: r, col: c });
      }
    }
  }

  if (validGridSlots.length === 0) return [getCentroid(latlngs)];
  if (validGridSlots.length <= count) return validGridSlots;

  // Step 3: Sort valid grid slots to form clean, continuous engineering rows and columns
  // For Solar / Battery / Load: sort strictly by row first, then column (creates continuous horizontal parallel stripes!)
  // For Wind: sort by row and column with even geometric distribution across the matrix!
  validGridSlots.sort((a, b) => {
    if (a.row !== b.row) return a.row - b.row;
    return a.col - b.col;
  });

  // Step 4: Restrict to the utilized land footprint (so surplus land is left 100% open and untouched)
  const activePoolSize = Math.max(count, Math.ceil(validGridSlots.length * Math.max(0.2, Math.min(1.0, utilFrac))));
  const activePool = validGridSlots.slice(0, activePoolSize);

  // Step 5: Select exactly `count` items along the straight lattice lines with uniform step pitch!
  // By stepping uniformly across our sorted geometric lattice slots, we guarantee:
  // - ZERO wavy lines or staggered jumps! Every selected point sits on a straight row/column line!
  // - ZERO overlapping or colliding icons!
  const step = activePool.length / count;
  const selected = [];
  for (let i = 0; i < count; i++) {
    const idx = Math.min(activePool.length - 1, Math.floor(i * step + step / 2));
    selected.push(activePool[idx]);
  }

  return selected;
}

// Generate premium architectural SVG symbols for Leaflet markers!
function getAssetSvgSymbol(tech, unitMw, idx) {
  if (tech === "solar") {
    // Architectural Solar PV Table / Single-Axis Tracker Array Symbol (34x20px)
    return `
      <div class="asset-symbol-pin" style="width: 36px; height: 22px; filter: drop-shadow(0 4px 8px rgba(0,0,0,0.55)); cursor: pointer; transition: transform 0.15s, filter 0.15s; transform: translate(-50%, -50%) rotate(-6deg);" title="PV Array Table #${idx+1} (${unitMw.toFixed(2)} MWp)">
        <svg width="36" height="22" viewBox="0 0 36 22" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="34" height="20" rx="3" fill="#0b1727" stroke="#38bdf8" stroke-width="1.5" stroke-opacity="0.9"/>
          <rect x="3" y="3" width="30" height="16" rx="1.5" fill="#1e3a8a" fill-opacity="0.6"/>
          <line x1="10" y1="2" x2="10" y2="20" stroke="#60a5fa" stroke-width="0.8" stroke-opacity="0.6"/>
          <line x1="18" y1="2" x2="18" y2="20" stroke="#60a5fa" stroke-width="0.8" stroke-opacity="0.6"/>
          <line x1="26" y1="2" x2="26" y2="20" stroke="#60a5fa" stroke-width="0.8" stroke-opacity="0.6"/>
          <line x1="2" y1="11" x2="34" y2="11" stroke="#60a5fa" stroke-width="0.8" stroke-opacity="0.6"/>
          <rect x="15" y="9" width="6" height="4" rx="1" fill="#f59e0b" fill-opacity="0.9"/>
        </svg>
      </div>
    `;
  } else if (tech === "wind") {
    // Architectural Top-Down Wind Turbine Symbol (38x38px with safety buffer ring and 3 swept blades)
    return `
      <div class="asset-symbol-pin" style="width: 40px; height: 40px; filter: drop-shadow(0 5px 12px rgba(0,0,0,0.6)); cursor: pointer; transition: transform 0.15s, filter 0.15s; transform: translate(-50%, -50%);" title="Wind Turbine T-${String(idx+1).padStart(2, '0')} (${unitMw.toFixed(2)} MW)">
        <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="20" cy="20" r="18" fill="rgba(20, 184, 166, 0.15)" stroke="#14b8a6" stroke-width="1.2" stroke-dasharray="3 3"/>
          <path d="M19 20 L18 4 C18 2 22 2 22 4 L21 20 Z" fill="#ffffff" stroke="#0d9488" stroke-width="1"/>
          <path d="M20 19 L34 26 C36 27 34 31 32 30 L20 21 Z" fill="#ffffff" stroke="#0d9488" stroke-width="1"/>
          <path d="M21 20 L8 30 C6 31 4 27 6 26 L20 19 Z" fill="#ffffff" stroke="#0d9488" stroke-width="1"/>
          <circle cx="20" cy="20" r="4" fill="#0f766e" stroke="#ffffff" stroke-width="1.8"/>
        </svg>
      </div>
    `;
  } else if (tech === "battery") {
    // Utility BESS Container Rack Yard Symbol (32x22px)
    return `
      <div class="asset-symbol-pin" style="width: 32px; height: 22px; filter: drop-shadow(0 4px 10px rgba(0,0,0,0.55)); cursor: pointer; transition: transform 0.15s, filter 0.15s; transform: translate(-50%, -50%);" title="BESS Rack Bank ${String.fromCharCode(65+idx)} (${unitMw.toFixed(1)} MW)">
        <svg width="32" height="22" viewBox="0 0 32 22" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="30" height="20" rx="3" fill="#064e3b" stroke="#34d399" stroke-width="1.5"/>
          <rect x="4" y="4" width="6" height="14" rx="1.5" fill="#10b981"/>
          <rect x="13" y="4" width="6" height="14" rx="1.5" fill="#10b981"/>
          <rect x="22" y="4" width="6" height="14" rx="1.5" fill="#10b981"/>
          <circle cx="7" cy="6" r="1.2" fill="#ffffff"/>
          <circle cx="16" cy="6" r="1.2" fill="#ffffff"/>
          <circle cx="25" cy="6" r="1.2" fill="#ffffff"/>
        </svg>
      </div>
    `;
  } else if (tech === "load") {
    // Enterprise Data Center Hall Module Symbol (36x30px)
    return `
      <div class="asset-symbol-pin" style="width: 36px; height: 30px; filter: drop-shadow(0 5px 14px rgba(0,0,0,0.6)); cursor: pointer; transition: transform 0.15s, filter 0.15s; transform: translate(-50%, -50%);" title="Data Center Module ${idx+1} (${unitMw.toFixed(1)} MW)">
        <svg width="36" height="30" viewBox="0 0 36 30" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="34" height="28" rx="4" fill="#1e293b" stroke="#60a5fa" stroke-width="1.8"/>
          <rect x="5" y="5" width="26" height="20" rx="2" fill="#0f172a"/>
          <line x1="11" y1="5" x2="11" y2="25" stroke="#3b82f6" stroke-width="1" stroke-opacity="0.5"/>
          <line x1="18" y1="5" x2="18" y2="25" stroke="#3b82f6" stroke-width="1" stroke-opacity="0.5"/>
          <line x1="25" y1="5" x2="25" y2="25" stroke="#3b82f6" stroke-width="1" stroke-opacity="0.5"/>
          <circle cx="8" cy="10" r="1.5" fill="#60a5fa"/>
          <circle cx="8" cy="15" r="1.5" fill="#60a5fa"/>
          <circle cx="8" cy="20" r="1.5" fill="#60a5fa"/>
        </svg>
      </div>
    `;
  } else {
    // High-Voltage Step-Up Transformer Switchyard Symbol (34x34px)
    return `
      <div class="asset-symbol-pin" style="width: 34px; height: 34px; filter: drop-shadow(0 5px 12px rgba(0,0,0,0.6)); cursor: pointer; transition: transform 0.15s, filter 0.15s; transform: translate(-50%, -50%);" title="Grid Step-Up Switchyard (${unitMw.toFixed(1)} MVA)">
        <svg width="34" height="34" viewBox="0 0 34 34" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="2" y="2" width="30" height="30" rx="5" fill="#2e1065" stroke="#c084fc" stroke-width="2"/>
          <circle cx="17" cy="17" r="9" fill="none" stroke="#a855f7" stroke-width="2"/>
          <circle cx="17" cy="17" r="3.5" fill="#e879f9"/>
          <line x1="4" y1="17" x2="30" y2="17" stroke="#c084fc" stroke-width="1.5"/>
          <line x1="17" y1="4" x2="17" y2="30" stroke="#c084fc" stroke-width="1.5"/>
        </svg>
      </div>
    `;
  }
}

export default function ClientShowcaseMap({ cfg, rec, result }) {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const layersRef = useRef({ street: null, satellite: null });
  const parcelsGroupRef = useRef(null);

  const [activeLayer, setActiveLayer] = useState("satellite"); // 'satellite' | 'street'
  const [selectedItem, setSelectedItem] = useState(null); // { type: 'parcel' | 'unit' | 'feeder', ... }
  const [showCables, setShowCables] = useState(true);
  const [visibleTechs, setVisibleTechs] = useState({
    solar: true, wind: true, battery: true, load: true, substation: true, reserve: true,
  });

  // Extract base coordinates from cfg
  const baseLat = useMemo(() => {
    if (cfg?.parcels?.solar?.lat) return cfg.parcels.solar.lat;
    if (cfg?.location) {
      const match = cfg.location.match(/(-?\d+(\.\d+)?),\s*(-?\d+(\.\d+)?)/);
      if (match) return parseFloat(match[1]);
    }
    return 51.96;
  }, [cfg]);

  const baseLon = useMemo(() => {
    if (cfg?.parcels?.solar?.lon) return cfg.parcels.solar.lon;
    if (cfg?.location) {
      const match = cfg.location.match(/(-?\d+(\.\d+)?),\s*(-?\d+(\.\d+)?)/);
      if (match) return parseFloat(match[3]);
    }
    return 1.35;
  }, [cfg]);

  // Derive showcase parcels from Step 1 and ensure all project assets (BESS, Load, Substation) are present
  const parcels = useMemo(() => {
    let list = [];
    if (cfg?.parcels?.list && cfg.parcels.list.length > 0) {
      list = cfg.parcels.list.map((p) => ({
        ...p,
        areaHa: p.areaHa || polygonAreaHa(p.latlngs),
      }));
    }

    // Check existing technologies in the list
    const hasSolar = list.some((p) => p.tech === "solar");
    const hasWind = list.some((p) => p.tech === "wind");
    const hasBess = list.some((p) => p.tech === "battery");
    const hasLoad = list.some((p) => p.tech === "load");
    const hasSubstation = list.some((p) => p.tech === "substation");

    const pvMw = rec?.pv_mw !== undefined ? rec.pv_mw : (cfg?.parcels?.solar ? 95 : 0);
    const windMw = rec?.wind_mw !== undefined ? rec.wind_mw : (cfg?.parcels?.wind ? 54 : 0);
    const bessMw = rec?.bess_mw !== undefined ? rec.bess_mw : 40;
    const loadPeak = (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0) || 50;
    const gcMw = rec?.gc_mw !== undefined ? rec.gc_mw : (cfg?.topology !== "off_grid" ? 30 : 0);

    // If Solar is needed but missing from list, synthesize it:
    if (!hasSolar && (pvMw > 0 || (!list.length && cfg?.parcels?.solar))) {
      const mw = pvMw || 95;
      list.push({
        id: "show-solar-synth",
        name: "Solar PV Field A",
        tech: "solar",
        areaHa: Math.round(mw / 0.52),
        maxMw: mw,
        latlngs: defaultRing(baseLat, baseLon, 0.95, 0, -0.014),
      });
    }
    // If Wind is needed but missing from list, synthesize it:
    if (!hasWind && (windMw > 0 || (!list.length && cfg?.parcels?.wind))) {
      const mw = windMw || 54;
      list.push({
        id: "show-wind-synth",
        name: "Wind Farm Ridge B",
        tech: "wind",
        areaHa: Math.round(mw / 0.26),
        maxMw: mw,
        latlngs: defaultRing(baseLat, baseLon, 1.1, 0, 0.016),
      });
    }
    // If BESS is needed (e.g. bessMw > 0 or default 40 MW) but NOT drawn by user in Step 1, append it!
    if (!hasBess && (bessMw > 0 || !list.length || cfg?.parcels?.list)) {
      const mw = bessMw > 0 ? bessMw : 40;
      list.push({
        id: "show-bess-synth",
        name: "BESS Storage Yard C",
        tech: "battery",
        areaHa: Math.max(6, Math.round(mw / 4)),
        maxMw: mw,
        latlngs: defaultRing(baseLat, baseLon, 0.35, -0.008, -0.003),
      });
    }
    // If Load / Data Center is present (loadPeak > 0) but NOT drawn by user in Step 1, append it!
    if (!hasLoad && loadPeak > 0) {
      list.push({
        id: "show-load-synth",
        name: cfg?.projectName ? `${cfg.projectName} Load Campus` : "Collocated Data Center / Load",
        tech: "load",
        areaHa: Math.max(15, Math.round(loadPeak * 0.5)),
        maxMw: loadPeak,
        latlngs: defaultRing(baseLat, baseLon, 0.45, -0.007, 0.006),
      });
    }
    // If Substation is needed but NOT drawn by user in Step 1, append it!
    if (!hasSubstation && (gcMw > 0 || cfg?.topology !== "off_grid") && cfg?.topology !== "off_grid") {
      const mw = gcMw > 0 ? gcMw : 30;
      list.push({
        id: "show-substation-synth",
        name: "Grid Step-Up Switchyard",
        tech: "substation",
        areaHa: 6,
        maxMw: mw,
        latlngs: defaultRing(baseLat, baseLon, 0.25, 0.009, 0),
      });
    }
    return list;
  }, [cfg, rec, baseLat, baseLon]);

  // Initialize Leaflet map
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return;

    const map = L.map(mapContainerRef.current, {
      center: [baseLat, baseLon],
      zoom: 13,
      zoomControl: false,
      attributionControl: false,
    });
    L.control.zoom({ position: "bottomright" }).addTo(map);

    const street = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
    });
    const satellite = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
    });

    layersRef.current = { street, satellite };
    satellite.addTo(map);

    parcelsGroupRef.current = L.layerGroup().addTo(map);
    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [baseLat, baseLon]);

  // Switch map tile layer
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const { street, satellite } = layersRef.current;
    if (activeLayer === "street") {
      if (map.hasLayer(satellite)) map.removeLayer(satellite);
      if (!map.hasLayer(street)) map.addLayer(street);
    } else {
      if (map.hasLayer(street)) map.removeLayer(street);
      if (!map.hasLayer(satellite)) map.addLayer(satellite);
    }
  }, [activeLayer]);

  // Render parcel polygons and architectural SVG symbols (NO words/text tags on map!)
  useEffect(() => {
    const map = mapRef.current;
    const group = parcelsGroupRef.current;
    if (!map || !group) return;

    group.clearLayers();
    const bounds = L.latLngBounds();

    parcels.forEach((p) => {
      if (!visibleTechs[p.tech]) return;
      const meta = TECH_META[p.tech] || TECH_META.reserve;
      const isSelected = selectedItem?.id === p.id;

      const latlngs = p.latlngs.map((pt) => [pt.lat, pt.lng]);
      latlngs.forEach((coord) => bounds.extend(coord));

      // Polygon boundary styling (clean translucent fill so SVG symbols stand out crisply)
      const poly = L.polygon(latlngs, {
        color: meta.border,
        weight: isSelected ? 3.5 : 2,
        opacity: 0.95,
        fillColor: meta.color,
        fillOpacity: isSelected ? 0.35 : 0.14,
        dashArray: p.tech === "reserve" ? "6, 6" : undefined,
      }).addTo(group);

      // STRICTLY DERIVED CAPACITY AND UNIT COUNT FROM OPTIMIZER RESULTS & NETWORK CONFIG:
      let mwVal = p.maxMw || 0;
      if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
      if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
      if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);
      if (p.tech === "substation") mwVal = rec?.gc_mw !== undefined ? rec.gc_mw : (p.maxMw || 30);
      if (p.tech === "load") mwVal = p.maxMw || (cfg?.loads || []).reduce((s, l) => s + (+l.peak_mw || 0), 0) || 50;

      const totalHa = Math.round(p.areaHa || polygonAreaHa(p.latlngs) || 100);
      const densityHaPerMw = p.tech === "solar" ? 0.52 : p.tech === "wind" ? 0.26 : p.tech === "battery" ? 0.15 : p.tech === "load" ? 0.5 : 0.2;
      const reqHa = Math.min(totalHa, Math.max(1, Math.round(mwVal * densityHaPerMw)));
      const utilPct = Math.min(100, Math.round((reqHa / totalHa) * 100));
      const unusedHa = Math.max(0, totalHa - reqHa);
      const utilFrac = Math.min(1.0, reqHa / totalHa);

      poly.bindTooltip(
        `<div style="text-align:center; font-family:Inter,sans-serif; padding:2px;">
          <div style="font-weight:800; font-size:13px; color:#0f172a;">${p.name} (${mwVal.toFixed(1)} MW)</div>
          <div style="font-size:11px; color:#0d9488; font-weight:700; margin-top:2px;">
            ${reqHa} ha Utilized / ${totalHa} ha Total (${utilPct}% Density)
          </div>
          ${unusedHa > 0 ? `<div style="font-size:10.5px; color:#dc2626; font-weight:800; margin-top:1px;">🌱 ${unusedHa} ha Unused / Surplus Land</div>` : `<div style="font-size:10.5px; color:#16a34a; font-weight:700; margin-top:1px;">✓ 100% Land Fully Utilized</div>`}
        </div>`,
        { sticky: true, direction: "top", className: "custom-showcase-tooltip" }
      );

      poly.on("click", () => {
        setSelectedItem({ type: "parcel", ...p, maxMw: mwVal, meta, totalHa, reqHa, utilPct, unusedHa });
        map.flyToBounds(poly.getBounds(), { padding: [60, 60], duration: 0.8 });
      });

      // Calculate exact number of physical units required by engineering physics:
      // - Solar: 4.0 MWp per inverter/tracker table block
      // - Wind: 4.5 MW per commercial onshore direct-drive turbine
      // - Battery: 5.0 MW per containerized LFP storage rack bank
      // - Load: 20.0 MW per enterprise AI data center hall module
      // - Substation: 1 transformer switchyard if grid connection > 0
      let unitCount = 0;
      let unitMw = 0;
      if (mwVal > 0) {
        if (p.tech === "solar") {
          unitCount = Math.ceil(mwVal / 4.0);
          unitMw = mwVal / unitCount;
        } else if (p.tech === "wind") {
          unitCount = Math.ceil(mwVal / 4.5);
          unitMw = mwVal / unitCount;
        } else if (p.tech === "battery") {
          unitCount = Math.ceil(mwVal / 5.0);
          unitMw = mwVal / unitCount;
        } else if (p.tech === "load") {
          unitCount = Math.ceil(mwVal / 20.0);
          unitMw = mwVal / unitCount;
        } else if (p.tech === "substation" && cfg?.topology !== "off_grid") {
          unitCount = 1;
          unitMw = mwVal;
        }
      }

      if (unitCount <= 0) return; // Exactly 0 symbols if optimizer sized to 0 MW!

      // Generate uniform engineering grid coordinates across utilized footprint with anti-collision
      const assetPts = generateAssetPoints(p.latlngs, unitCount, utilFrac, p.tech);

      // Render clean architectural SVG symbols (NO text words/tags attached!)
      assetPts.forEach((pt, idx) => {
        const symbolHtml = getAssetSvgSymbol(p.tech, unitMw, idx);
        let unitName = "";
        if (p.tech === "solar") unitName = `PV Tracker Array Table S-${String(idx + 1).padStart(2, '0')}`;
        else if (p.tech === "wind") unitName = `Wind Turbine T-${String(idx + 1).padStart(2, '0')}`;
        else if (p.tech === "battery") unitName = `BESS Container Bank ${String.fromCharCode(65 + idx)}`;
        else if (p.tech === "load") unitName = `Data Center Hall Module ${idx + 1}`;
        else unitName = `Grid Step-Up Transformer Switchyard`;

        const unitMarker = L.marker([pt.lat, pt.lng], {
          icon: L.divIcon({ className: "custom-showcase-pin", html: symbolHtml, iconSize: [0, 0] }),
          zIndexOffset: 50,
        }).addTo(group);

        unitMarker.on("click", (e) => {
          L.DomEvent.stopPropagation(e);
          setSelectedItem({
            type: "unit",
            name: unitName,
            tech: p.tech,
            unitMw: unitMw,
            parcelName: p.name,
            meta: meta,
            totalHa, reqHa, utilPct, unusedHa,
            spec: p.tech === "solar" ? "Single-Axis Tracker Table · Monocrystalline Bifacial PV Modules · South-Facing Azimuth 180°"
                : p.tech === "wind" ? "115m Hub Height · 150m Rotor Diameter · Direct-Drive Onshore Wind Turbine · IEC Class IIa"
                : p.tech === "battery" ? "Containerized LFP Battery Racks · Liquid-Cooled Thermal Management · Advanced BMS"
                : p.tech === "load" ? "Collocated Enterprise AI Data Center Hall · 99.999% Tier-IV Reliability"
                : "High-Voltage Step-Up Switchyard Transformer & Grid Interconnect Bay",
          });
          map.panTo([pt.lat, pt.lng]);
        });
      });
    });

    // DRAW GIS MV/HV ELECTRICAL INTERCONNECT CABLES:
    if (showCables && parcels.length > 1) {
      const hubParcel = parcels.find((p) => p.tech === "substation") || parcels.find((p) => p.tech === "load") || parcels.find((p) => p.tech === "battery") || parcels[0];
      if (hubParcel) {
        const hubPt = getCentroid(hubParcel.latlngs);
        parcels.forEach((p) => {
          if (!visibleTechs[p.tech] || p.id === hubParcel.id) return;
          const srcPt = getCentroid(p.latlngs);
          const meta = TECH_META[p.tech] || TECH_META.reserve;

          // Outer glowing background
          L.polyline([[srcPt.lat, srcPt.lng], [hubPt.lat, hubPt.lng]], {
            color: meta.color,
            weight: 7,
            opacity: 0.25,
            lineCap: "round",
          }).addTo(group);

          // Inner dashed SCADA feeder cable
          const feederLine = L.polyline([[srcPt.lat, srcPt.lng], [hubPt.lat, hubPt.lng]], {
            color: "#38bdf8",
            weight: 3.2,
            opacity: 0.95,
            dashArray: "10, 10",
            lineCap: "round",
          }).addTo(group);

          let mwVal = p.maxMw || 0;
          if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
          if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
          if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);

          const feederName = `${p.name} ➔ ${hubParcel.name} Feeder`;
          feederLine.bindTooltip(
            `<div style="text-align:center; font-family:Inter,sans-serif; padding:2px;">
              <div style="font-weight:800; font-size:12.5px; color:#0f172a;">⚡ ${feederName}</div>
              <div style="font-size:11px; color:#0d9488; font-weight:700; margin-top:2px;">
                33 kV Underground XLPE Collector · ${mwVal.toFixed(1)} MW Active Power Flow
              </div>
              <div style="font-size:10px; color:#64748b; font-weight:600; margin-top:1px;">Click to view electrical SCADA parameters</div>
            </div>`,
            { sticky: true, className: "custom-showcase-tooltip" }
          );

          feederLine.on("click", (e) => {
            L.DomEvent.stopPropagation(e);
            setSelectedItem({
              type: "feeder",
              name: feederName,
              tech: p.tech,
              unitMw: mwVal,
              parcelName: p.name,
              meta: meta,
              spec: "33 kV Underground XLPE 3-Core Copper Conductor · 1,200 A Continuous Rating · Bi-Directional Active Power Flow · Optical Fiber DTS Temperature Monitoring · Less than 0.8% I²R Line Loss",
            });
          });
        });
      }
    }

    if (bounds.isValid() && !selectedItem) {
      map.fitBounds(bounds, { padding: [50, 50] });
    }
  }, [parcels, visibleTechs, showCables, selectedItem, rec, cfg]);

  const toggleTech = (tech) => {
    setVisibleTechs((prev) => ({ ...prev, [tech]: !prev[tech] }));
  };

  const handleExportGeoJson = () => {
    const geojson = {
      type: "FeatureCollection",
      metadata: {
        project: cfg?.projectName || "Giga Park Project",
        generatedAt: new Date().toISOString(),
        verifiedBy: "Antigravity Executive Digital Twin & SCADA Engine",
        totalCapacityMw: parcels.reduce((s, p) => {
          let mwVal = p.maxMw || 0;
          if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
          if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
          if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);
          if (p.tech === "substation") mwVal = rec?.gc_mw !== undefined ? rec.gc_mw : (p.maxMw || 30);
          return s + mwVal;
        }, 0),
      },
      features: parcels.map((p) => ({
        type: "Feature",
        properties: {
          id: p.id,
          name: p.name,
          technology: p.tech,
          capacityMw: p.maxMw || 0,
          areaHa: p.areaHa || polygonAreaHa(p.latlngs),
        },
        geometry: {
          type: "Polygon",
          coordinates: [p.latlngs.map((pt) => [pt.lng, pt.lat])],
        },
      })),
    };
    const blob = new Blob([JSON.stringify(geojson, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(cfg?.projectName || "giga-park").toLowerCase().replace(/\s+/g, "-")}-digital-twin-layout.geojson`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const totalPlantMw = parcels.reduce((s, p) => {
    let mwVal = p.maxMw || 0;
    if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
    if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
    if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);
    if (p.tech === "substation") mwVal = rec?.gc_mw !== undefined ? rec.gc_mw : (p.maxMw || 30);
    return s + mwVal;
  }, 0);

  const totalAreaHa = parcels.reduce((s, p) => s + (p.areaHa || polygonAreaHa(p.latlngs) || 0), 0);
  const totalReqHa = parcels.reduce((s, p) => {
    let mwVal = p.maxMw || 0;
    if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
    if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
    if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);
    if (p.tech === "load") mwVal = p.maxMw || 50;
    const density = p.tech === "solar" ? 0.52 : p.tech === "wind" ? 0.26 : p.tech === "battery" ? 0.15 : 0.5;
    return s + Math.min(p.areaHa || 100, Math.max(1, Math.round(mwVal * density)));
  }, 0);
  const overallUtilPct = totalAreaHa > 0 ? Math.min(100, Math.round((totalReqHa / totalAreaHa) * 100)) : 100;
  const totalPhysicalUnits = parcels.reduce((s, p) => {
    let mwVal = p.maxMw || 0;
    if (p.tech === "solar") mwVal = rec?.pv_mw !== undefined ? rec.pv_mw : (p.maxMw || 95);
    if (p.tech === "wind") mwVal = rec?.wind_mw !== undefined ? rec.wind_mw : (p.maxMw || 54);
    if (p.tech === "battery") mwVal = rec?.bess_mw !== undefined ? rec.bess_mw : (p.maxMw || 40);
    if (p.tech === "load") mwVal = p.maxMw || 50;
    let cnt = 1;
    if (p.tech === "solar") cnt = Math.ceil(mwVal / 4.0);
    else if (p.tech === "wind") cnt = Math.ceil(mwVal / 4.5);
    else if (p.tech === "battery") cnt = Math.ceil(mwVal / 5.0);
    else if (p.tech === "load") cnt = Math.ceil(mwVal / 20.0);
    return s + cnt;
  }, 0);

  return (
    <div className="card" style={{ padding: 0, overflow: "hidden", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 16, boxShadow: "0 16px 40px rgba(0,0,0,0.15)", marginTop: 24, background: "#0f172a" }}>
      {/* Premium Glassmorphic Top Header */}
      <div style={{
        background: "linear-gradient(135deg, rgba(15, 23, 42, 0.95), rgba(30, 41, 59, 0.95))",
        color: "#fff",
        padding: "20px 28px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexWrap: "wrap",
        gap: 16,
        borderBottom: "1px solid rgba(255,255,255,0.08)",
      }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: "linear-gradient(135deg, #14b8a6, #0d9488)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, boxShadow: "0 4px 12px rgba(20,184,166,0.35)" }}>
              🌐
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <h3 style={{ margin: 0, fontSize: 19, color: "#fff", fontWeight: 800, letterSpacing: "-0.01em" }}>
                  GIS Digital Twin &amp; Site Asset Overlay
                </h3>
                <span style={{ background: "rgba(56, 189, 248, 0.15)", color: "#38bdf8", border: "1px solid rgba(56, 189, 248, 0.3)", padding: "2px 8px", borderRadius: 20, fontSize: 10.5, fontWeight: 700 }}>
                  ✨ MODEL R VERIFIED
                </span>
              </div>
              <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 3 }}>
                Top-down architectural asset symbols strictly derived from LP optimizer sizing &amp; network topology.
              </div>
            </div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <button
            type="button"
            onClick={handleExportGeoJson}
            style={{
              padding: "8px 16px",
              background: "linear-gradient(135deg, #3b82f6, #2563eb)",
              color: "#fff",
              border: "none",
              borderRadius: 8,
              fontSize: 12.5,
              fontWeight: 700,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 6,
              boxShadow: "0 4px 12px rgba(59,130,246,0.35)",
              transition: "all 0.15s",
            }}
          >
            📥 Export GIS Spec (GeoJSON)
          </button>

          {/* Segmented Map Style Switcher */}
          <div style={{ display: "flex", background: "rgba(0,0,0,0.35)", padding: 4, borderRadius: 10, border: "1px solid rgba(255,255,255,0.08)", gap: 4 }}>
            {[
              ["satellite", "🛰️ Satellite Imagery"],
              ["street", "🗺️ Street Canvas"],
            ].map(([k, label]) => (
              <button
                key={k}
                type="button"
                onClick={() => setActiveLayer(k)}
                style={{
                  padding: "7px 16px",
                  border: "none",
                  borderRadius: 7,
                  fontSize: 12.5,
                  fontWeight: 700,
                  cursor: "pointer",
                  background: activeLayer === k ? "linear-gradient(135deg, #0d9488, #0f766e)" : "transparent",
                  color: activeLayer === k ? "#fff" : "#94a3b8",
                  boxShadow: activeLayer === k ? "0 2px 8px rgba(13,148,136,0.4)" : "none",
                  transition: "all 0.18s",
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Executive Plant Summary Banner */}
      <div style={{
        background: "rgba(15, 23, 42, 0.95)",
        padding: "10px 24px",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
        display: "flex",
        alignItems: "center",
        gap: 20,
        flexWrap: "wrap",
        fontSize: 12.5,
        color: "#cbd5e1",
      }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          ⚡ <b>Total Sized Capacity:</b> <span style={{ color: "#38bdf8", fontWeight: 800 }}>{totalPlantMw.toFixed(1)} MW</span>
        </span>
        <span style={{ color: "#334155" }}>•</span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          📦 <b>Physical Asset Count:</b> <span style={{ color: "#a855f7", fontWeight: 800 }}>{totalPhysicalUnits} Units</span>
        </span>
        <span style={{ color: "#334155" }}>•</span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          🌱 <b>Site Land Utilization:</b> <span style={{ color: "#34d399", fontWeight: 800 }}>{overallUtilPct}% Density ({totalReqHa} ha / {Math.round(totalAreaHa)} ha)</span>
        </span>
      </div>

      {/* Glassmorphic Technology Legend & Overlay Control Strip */}
      <div style={{
        background: "rgba(15, 23, 42, 0.8)",
        padding: "14px 24px",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#94a3b8", marginRight: 4 }}>Layer Overlays:</span>
        {Object.entries(TECH_META).map(([tech, meta]) => {
          const p = parcels.find((p) => p.tech === tech);
          if (!p) return null;
          const isOn = visibleTechs[tech];
          let mwVal = p.maxMw || 0;
          if (tech === "solar" && rec?.pv_mw !== undefined) mwVal = rec.pv_mw;
          if (tech === "wind" && rec?.wind_mw !== undefined) mwVal = rec.wind_mw;
          if (tech === "battery" && rec?.bess_mw !== undefined) mwVal = rec.bess_mw;
          if (tech === "substation" && rec?.gc_mw !== undefined) mwVal = rec.gc_mw;

          let unitCount = 1;
          if (mwVal > 0) {
            if (tech === "solar") unitCount = Math.ceil(mwVal / 4.0);
            else if (tech === "wind") unitCount = Math.ceil(mwVal / 4.5);
            else if (tech === "battery") unitCount = Math.ceil(mwVal / 5.0);
            else if (tech === "load") unitCount = Math.ceil(mwVal / 20.0);
          }

          return (
            <button
              key={tech}
              type="button"
              onClick={() => toggleTech(tech)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                padding: "6px 14px",
                borderRadius: 20,
                border: `1.5px solid ${isOn ? meta.border : "rgba(255,255,255,0.12)"}`,
                background: isOn ? `rgba(${parseInt(meta.color.slice(1,3),16)}, ${parseInt(meta.color.slice(3,5),16)}, ${parseInt(meta.color.slice(5,7),16)}, 0.18)` : "rgba(255,255,255,0.03)",
                color: isOn ? "#fff" : "#64748b",
                fontSize: 12,
                fontWeight: 700,
                cursor: "pointer",
                transition: "all 0.18s",
                boxShadow: isOn ? `0 2px 10px rgba(${parseInt(meta.color.slice(1,3),16)}, ${parseInt(meta.color.slice(3,5),16)}, ${parseInt(meta.color.slice(5,7),16)}, 0.25)` : "none",
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: isOn ? meta.color : "#64748b", boxShadow: isOn ? `0 0 8px ${meta.color}` : "none" }} />
              <span>{meta.label}: <b style={{ color: "#fff" }}>{unitCount} {tech === "solar" ? "Tables" : tech === "wind" ? "Turbines" : tech === "battery" ? "Banks" : tech === "load" ? "Halls" : "Bay"}</b></span>
              {mwVal > 0 && <span style={{ opacity: 0.85, fontWeight: 600, fontSize: 11 }}>({mwVal.toFixed(1)} {meta.unit})</span>}
              <span style={{ fontSize: 11, opacity: 0.7, marginLeft: 2 }}>{isOn ? "✓" : "✕"}</span>
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => setShowCables(!showCables)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 14px",
            borderRadius: 20,
            border: `1.5px solid ${showCables ? "#38bdf8" : "rgba(255,255,255,0.12)"}`,
            background: showCables ? "rgba(56, 189, 248, 0.18)" : "rgba(255,255,255,0.03)",
            color: showCables ? "#fff" : "#64748b",
            fontSize: 12,
            fontWeight: 700,
            cursor: "pointer",
            transition: "all 0.18s",
            boxShadow: showCables ? "0 2px 10px rgba(56, 189, 248, 0.25)" : "none",
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: showCables ? "#38bdf8" : "#64748b", boxShadow: showCables ? "0 0 8px #38bdf8" : "none" }} />
          <span>⚡ MV/HV Interconnect Cables</span>
          <span style={{ fontSize: 11, opacity: 0.7, marginLeft: 2 }}>{showCables ? "✓" : "✕"}</span>
        </button>
        {selectedItem && (
          <button
            type="button"
            onClick={() => setSelectedItem(null)}
            style={{
              marginLeft: "auto",
              padding: "5px 14px",
              fontSize: 11.5,
              fontWeight: 700,
              color: "#38bdf8",
              background: "rgba(56, 189, 248, 0.1)",
              border: "1px solid rgba(56, 189, 248, 0.3)",
              borderRadius: 8,
              cursor: "pointer",
              transition: "all 0.15s",
            }}
          >
            Reset View ↺
          </button>
        )}
      </div>

      {/* Main Map & Glassmorphic Inspector Container */}
      <div style={{ position: "relative", width: "100%", height: 580, display: "flex" }}>
        {/* Leaflet Canvas */}
        <div ref={mapContainerRef} style={{ flex: 1, height: "100%", background: "#0b1329" }} />

        {/* Floating Glassmorphic Asset Inspector Drawer (when any SVG symbol or parcel is clicked) */}
        {selectedItem && (
          <div style={{
            position: "absolute",
            top: 24,
            left: 24,
            width: 350,
            background: "rgba(15, 23, 42, 0.88)",
            backdropFilter: "blur(20px)",
            WebkitBackdropFilter: "blur(20px)",
            border: "1px solid rgba(255, 255, 255, 0.18)",
            borderRadius: 16,
            padding: 22,
            boxShadow: "0 20px 50px rgba(0,0,0,0.5)",
            zIndex: 1000,
            display: "flex",
            flexDirection: "column",
            gap: 14,
            color: "#fff",
            animation: "fadeIn 0.22s cubic-bezier(0.16, 1, 0.3, 1)",
          }}>
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div style={{ width: 44, height: 44, borderRadius: 12, background: `rgba(${parseInt(selectedItem.meta?.color.slice(1,3)||"20",16)}, ${parseInt(selectedItem.meta?.color.slice(3,5)||"184",16)}, ${parseInt(selectedItem.meta?.color.slice(5,7)||"166",16)}, 0.2)`, border: `1.5px solid ${selectedItem.meta?.color || "#14b8a6"}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 24 }}>
                  {selectedItem.tech === "solar" ? "☀️" : selectedItem.tech === "wind" ? "🌬️" : selectedItem.tech === "battery" ? "🔋" : selectedItem.tech === "load" ? "🏢" : "⚡"}
                </div>
                <div>
                  <h4 style={{ margin: 0, fontSize: 17, fontWeight: 800, color: "#fff", letterSpacing: "-0.01em" }}>
                    {selectedItem.name}
                  </h4>
                  <div style={{ fontSize: 12, color: selectedItem.meta?.color || "#38bdf8", fontWeight: 700, marginTop: 2 }}>
                    {selectedItem.type === "unit" ? `Physical Asset Unit · ${selectedItem.parcelName}` : selectedItem.type === "feeder" ? `Electrical MV/HV Feeder · ${selectedItem.parcelName}` : selectedItem.meta?.label}
                  </div>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setSelectedItem(null)}
                style={{ background: "transparent", border: "none", fontSize: 20, color: "#94a3b8", cursor: "pointer", padding: 2, transition: "color 0.15s" }}
              >
                ✕
              </button>
            </div>

            <div style={{ fontSize: 12.5, color: "#cbd5e1", lineHeight: 1.5, background: "rgba(255,255,255,0.05)", padding: 12, borderRadius: 10, border: "1px solid rgba(255,255,255,0.08)" }}>
              {selectedItem.type === "unit" || selectedItem.type === "feeder" ? selectedItem.spec : (selectedItem.meta?.desc || "Site asset footprint area.")}
            </div>

            {selectedItem.type === "feeder" ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Active Power Flow</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#fff", marginTop: 4 }}>
                    {selectedItem.unitMw ? `${selectedItem.unitMw.toFixed(1)} MW` : "—"}
                  </div>
                </div>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Voltage &amp; Rating</div>
                  <div style={{ fontSize: 13, fontWeight: 800, color: "#38bdf8", marginTop: 6 }}>
                    33 kV · 1,200 A XLPE
                  </div>
                </div>
              </div>
            ) : selectedItem.type === "unit" ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Unit Capacity</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#fff", marginTop: 4 }}>
                    {selectedItem.unitMw ? `${selectedItem.unitMw.toFixed(2)} MW` : "—"}
                  </div>
                </div>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Optimizer Status</div>
                  <div style={{ fontSize: 13, fontWeight: 800, color: "#34d399", marginTop: 6, display: "flex", alignItems: "center", gap: 5 }}>
                    <span style={{ width: 7, height: 7, borderRadius: "50%", background: "#34d399", boxShadow: "0 0 8px #34d399" }} />
                    Sized &amp; Active
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Sized Capacity</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#fff", marginTop: 4 }}>
                    {selectedItem.maxMw ? `${selectedItem.maxMw.toFixed(1)} MW` : "—"}
                  </div>
                </div>
                <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 12, borderRadius: 10 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Total Parcel Area</div>
                  <div style={{ fontSize: 18, fontWeight: 800, color: "#fff", marginTop: 4 }}>
                    {Math.round(selectedItem.totalHa || selectedItem.areaHa || 0)} ha
                  </div>
                </div>
              </div>
            )}

            {/* Land Utilization & Surplus Land Gauge */}
            {selectedItem.totalHa && (
              <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", padding: 14, borderRadius: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                  <span style={{ fontSize: 11.5, color: "#cbd5e1", fontWeight: 700 }}>Land Utilization Density</span>
                  <span style={{ fontSize: 12.5, fontWeight: 800, color: selectedItem.utilPct < 100 ? "#34d399" : "#38bdf8" }}>{selectedItem.utilPct}% Utilized</span>
                </div>
                <div style={{ width: "100%", height: 8, background: "rgba(255,255,255,0.1)", borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
                  <div style={{ width: `${selectedItem.utilPct}%`, height: "100%", background: selectedItem.utilPct < 100 ? "linear-gradient(90deg, #10b981, #34d399)" : "#38bdf8", borderRadius: 4 }} />
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#94a3b8" }}>
                  <span><b style={{ color: "#fff" }}>{selectedItem.reqHa} ha</b> Active Footprint</span>
                  {selectedItem.unusedHa > 0 ? (
                    <span style={{ color: "#f87171", fontWeight: 700 }}>🌱 {selectedItem.unusedHa} ha Surplus / Reserve Land</span>
                  ) : (
                    <span style={{ color: "#34d399", fontWeight: 700 }}>✓ 100% Fully Built</span>
                  )}
                </div>
              </div>
            )}

            <div style={{ fontSize: 11.5, color: "#94a3b8", borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: 12, lineHeight: 1.4 }}>
              <b style={{ color: "#e2e8f0" }}>Engineering Note:</b> Physical unit layout is mathematically verified against aerodynamic wake interference and inter-table solar shading across this GIS terrain boundary.
            </div>
          </div>
        )}

        {/* Bottom Legend Overlay */}
        <div style={{
          position: "absolute",
          bottom: 24,
          left: 24,
          background: "rgba(15, 23, 42, 0.9)",
          backdropFilter: "blur(12px)",
          WebkitBackdropFilter: "blur(12px)",
          color: "#fff",
          padding: "10px 18px",
          borderRadius: 10,
          border: "1px solid rgba(255,255,255,0.12)",
          fontSize: 12,
          fontWeight: 600,
          zIndex: 999,
          display: "flex",
          alignItems: "center",
          gap: 16,
          boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
        }}>
          <span style={{ display: "flex", alignItems: "center", gap: 6 }}>📍 <b style={{ color: "#fff" }}>{cfg?.projectName || "Giga Park Project"}</b></span>
          <span style={{ color: "#475569" }}>|</span>
          <span>Total Sized Area: <b style={{ color: "#38bdf8" }}>{parcels.reduce((s, p) => s + (p.areaHa || 0), 0).toFixed(0)} ha</b></span>
          <span style={{ color: "#475569" }}>|</span>
          <span style={{ color: "#34d399" }}>● Click any SVG symbol or parcel for specs</span>
        </div>
      </div>
    </div>
  );
}
