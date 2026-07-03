// Real map parcel — Leaflet + OpenStreetMap (no API key). Drag the corner markers
// to shape the buildable land on the actual map; the map recenters on the entered
// lat/lon. Area is a true geodesic area (m² → ha) reported up via onArea().
import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

// Geodesic polygon area (equirectangular projection about the centroid → shoelace).
// Accurate to well under 1% for site-scale parcels.
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
  return Math.abs(a / 2) / 10000;   // m² → ha
}

// Default ~180 ha polygon around a centre point.
function defaultRing(lat, lon) {
  const dLat = 0.0061, dLng = 0.0099 / Math.max(0.3, Math.cos(lat * Math.PI / 180)) * 0.616;
  return [
    [lat + dLat, lon - dLng], [lat + dLat, lon + dLng], [lat - dLat * 0.55, lon + dLng * 1.1],
    [lat - dLat, lon], [lat - dLat * 0.55, lon - dLng * 1.1],
  ].map((p) => L.latLng(p[0], p[1]));
}

export default function MapParcel({ lat, lon, color = "#15616d", onArea }) {
  const elRef = useRef(null);
  const mapRef = useRef(null);
  const polyRef = useRef(null);
  const markersRef = useRef([]);
  const onAreaRef = useRef(onArea);
  onAreaRef.current = onArea;

  // init once
  useEffect(() => {
    const map = L.map(elRef.current, { center: [lat, lon], zoom: 14, scrollWheelZoom: true, zoomControl: true });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors", maxZoom: 19,
    }).addTo(map);
    mapRef.current = map;

    const ring = defaultRing(lat, lon);
    const poly = L.polygon(ring, { color, weight: 2.5, dashArray: "6 4", fillOpacity: 0.25 }).addTo(map);
    polyRef.current = poly;

    const sync = () => {
      const ll = markersRef.current.map((m) => m.getLatLng());
      poly.setLatLngs(ll);
      onAreaRef.current(polygonAreaHa(ll));
    };
    markersRef.current = ring.map((p) => {
      const icon = L.divIcon({
        className: "", iconSize: [16, 16], iconAnchor: [8, 8],
        html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.4);cursor:grab"></div>`,
      });
      const m = L.marker(p, { draggable: true, icon }).addTo(map);
      m.on("drag", sync);
      return m;
    });
    map.fitBounds(poly.getBounds(), { padding: [30, 30] });
    onAreaRef.current(polygonAreaHa(ring));
    // Leaflet needs a size recalculation once the container has laid out.
    const t = setTimeout(() => map.invalidateSize(), 150);

    return () => { clearTimeout(t); map.remove(); mapRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // recenter + move the parcel when the location changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !markersRef.current.length) return;
    const cur = markersRef.current.map((m) => m.getLatLng());
    const cLat = cur.reduce((s, p) => s + p.lat, 0) / cur.length;
    const cLon = cur.reduce((s, p) => s + p.lng, 0) / cur.length;
    const dLat = lat - cLat, dLon = lon - cLon;
    if (Math.abs(dLat) < 1e-9 && Math.abs(dLon) < 1e-9) return;
    markersRef.current.forEach((m) => {
      const p = m.getLatLng(); m.setLatLng(L.latLng(p.lat + dLat, p.lng + dLon));
    });
    polyRef.current.setLatLngs(markersRef.current.map((m) => m.getLatLng()));
    map.setView([lat, lon]);
    onAreaRef.current(polygonAreaHa(markersRef.current.map((m) => m.getLatLng())));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lat, lon]);

  return <div ref={elRef} style={{ height: 300, borderRadius: 10, overflow: "hidden", border: "1px solid var(--line2)" }} />;
}
