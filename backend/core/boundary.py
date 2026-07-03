"""
core/boundary.py — parse an uploaded land boundary file into parcel rings.

The web app lets a developer start from the land: either draw a parcel on the
map, or upload a boundary file exported from GIS software. This module turns the
two common exchange formats — GeoJSON and KMZ/KML — into the same simple shape
the rest of the app already speaks: a list of rings, each a list of {lat, lng}
points, plus the polygon's area in hectares and its centroid.

No third-party GIS dependency is needed: GeoJSON is plain JSON, KML is XML, and a
KMZ is just a zip that contains a KML. Area uses the same equirectangular
shoelace the front-end map uses, so the hectare figure matches what the user
sees while drawing.
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from xml.etree import ElementTree as ET

# ── Geometry ──────────────────────────────────────────────────────────────────

def polygon_area_ha(pts: list[tuple[float, float]]) -> float:
    """Area of a (lat, lon) ring in hectares — equirectangular projection about
    the centroid, then the shoelace formula. Mirrors the front-end map maths."""
    if len(pts) < 3:
        return 0.0
    R = 6378137.0  # WGS84 mean radius (m)
    lat0 = math.radians(sum(p[0] for p in pts) / len(pts))
    xy = [(math.radians(lon) * math.cos(lat0) * R, math.radians(lat) * R) for lat, lon in pts]
    a = 0.0
    n = len(xy)
    for i in range(n):
        j = (i + 1) % n
        a += xy[i][0] * xy[j][1] - xy[j][0] * xy[i][1]
    return abs(a / 2.0) / 10000.0  # m² -> ha


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _ring_dict(pts: list[tuple[float, float]], name: str) -> dict:
    """Package a (lat, lon) ring into the JSON shape the front end consumes."""
    lat, lon = _centroid(pts)
    return {
        "name": name,
        "latlngs": [{"lat": la, "lng": lo} for la, lo in pts],
        "area_ha": round(polygon_area_ha(pts), 2),
        "centroid": {"lat": lat, "lng": lon},
    }


def _dedupe_closing_point(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """GeoJSON/KML rings repeat the first vertex at the end; the map uses open
    rings, so drop the trailing duplicate."""
    if len(pts) >= 2 and pts[0] == pts[-1]:
        return pts[:-1]
    return pts


# ── GeoJSON ───────────────────────────────────────────────────────────────────

def _geometry_rings(geom: dict) -> list[list[tuple[float, float]]]:
    """Exterior rings of a GeoJSON geometry (coordinates are [lon, lat])."""
    if not isinstance(geom, dict):
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    rings: list[list[tuple[float, float]]] = []
    if gtype == "Polygon" and coords:
        rings.append([(pt[1], pt[0]) for pt in coords[0]])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                rings.append([(pt[1], pt[0]) for pt in poly[0]])
    elif gtype == "GeometryCollection":
        for g in geom.get("geometries", []):
            rings.extend(_geometry_rings(g))
    return rings


def _rings_from_geojson(text: str) -> list[dict]:
    obj = json.loads(text)
    features = []
    if obj.get("type") == "FeatureCollection":
        features = obj.get("features", [])
    elif obj.get("type") == "Feature":
        features = [obj]
    else:  # bare geometry
        features = [{"geometry": obj, "properties": {}}]

    out: list[dict] = []
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        base_name = props.get("name") or props.get("Name") or f"Imported Parcel {chr(65 + i)}"
        for j, ring in enumerate(_geometry_rings(feat.get("geometry") or {})):
            ring = _dedupe_closing_point(ring)
            if len(ring) >= 3:
                name = base_name if j == 0 else f"{base_name} ({j + 1})"
                out.append(_ring_dict(ring, name))
    return out


# ── KML / KMZ ─────────────────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_coord_text(text: str) -> list[tuple[float, float]]:
    """A KML <coordinates> blob: whitespace-separated 'lon,lat[,alt]' tuples."""
    pts: list[tuple[float, float]] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) >= 2:
            lon, lat = float(parts[0]), float(parts[1])
            pts.append((lat, lon))
    return pts


def _rings_from_kml(text: str) -> list[dict]:
    root = ET.fromstring(text)
    out: list[dict] = []
    idx = 0
    # Each <Placemark> is one named feature; a Placemark may hold several Polygons.
    for placemark in root.iter():
        if _localname(placemark.tag) != "Placemark":
            continue
        name = None
        for child in placemark:
            if _localname(child.tag) == "name" and child.text:
                name = child.text.strip()
                break
        base_name = name or f"Imported Parcel {chr(65 + idx)}"
        p = 0
        for poly in placemark.iter():
            if _localname(poly.tag) != "Polygon":
                continue
            coords_el = next(
                (e for outer in poly.iter() if _localname(outer.tag) == "outerBoundaryIs"
                 for e in outer.iter() if _localname(e.tag) == "coordinates"),
                None,
            )
            if coords_el is None or not coords_el.text:
                continue
            ring = _dedupe_closing_point(_parse_coord_text(coords_el.text))
            if len(ring) >= 3:
                label = base_name if p == 0 else f"{base_name} ({p + 1})"
                out.append(_ring_dict(ring, label))
                p += 1
        idx += 1
    return out


def _kml_from_kmz(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not kml_names:
            raise ValueError("KMZ archive contains no .kml document")
        # doc.kml is the conventional root; otherwise take the first KML.
        target = next((n for n in kml_names if n.lower().endswith("doc.kml")), kml_names[0])
        return zf.read(target).decode("utf-8", errors="replace")


# ── Dispatch ──────────────────────────────────────────────────────────────────

def parse_boundary(filename: str, data: bytes) -> list[dict]:
    """Parse an uploaded boundary file into a list of ring dicts.

    Dispatches on file extension: .geojson/.json → GeoJSON, .kml → KML,
    .kmz → zipped KML. Raises ValueError with a readable message on bad input.
    """
    name = (filename or "").lower()
    if name.endswith((".geojson", ".json")):
        return _rings_from_geojson(data.decode("utf-8", errors="replace"))
    if name.endswith(".kml"):
        return _rings_from_kml(data.decode("utf-8", errors="replace"))
    if name.endswith(".kmz"):
        return _rings_from_kml(_kml_from_kmz(data))
    raise ValueError("Unsupported file type — upload a .geojson, .json, .kml or .kmz file.")
