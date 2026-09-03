"""Land check — the automated "dirt" research MADD asked for.

For a parcel we already have a boundary (or at least a centroid) for, ask the
public GIS layers three questions that decide whether raw land is even worth a
drive:

  * Wetlands  — does USFWS National Wetlands Inventory map wetland ON the parcel?
  * Flood     — is it in a FEMA Special Flood Hazard Area (zone A*/V*)?
  * Access    — is there a public road touching the parcel, or is it landlocked?

Everything here is a deterministic query against a free, public, statewide GIS
service keyed off the parcel geometry — no per-county scraping, no API key, no
LLM. One NWI service covers all of Florida, one FEMA service, OSM for roads, so
coverage is uniform; the only thing that stops a check is a parcel we have no
geometry for. Results are advisory: we report the facts and a soft verdict
(clear / review / avoid), never a hard "this is unbuildable".

Runs server-side in the enrichment pipeline (open egress), never in the browser.
Every network call is guarded; a source that doesn't answer yields "unknown"
for that dimension, never an exception.
"""

from __future__ import annotations

import json
import logging
import math
import re

import requests

log = logging.getLogger(__name__)

TIMEOUT = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# USFWS National Wetlands Inventory — layer 0 is the wetlands polygons.
NWI_URL = "https://fwsprimary.wim.usgs.gov/server/rest/services/Wetlands/MapServer/0/query"
# FEMA National Flood Hazard Layer — layer 28 is the flood hazard zones.
FEMA_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
# OpenStreetMap roads, via Overpass.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# FEMA zones that are a Special Flood Hazard Area (the 1%-annual "100-year").
SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}
# Road classes that give real legal/physical access (not footpaths/tracks).
ACCESS_HIGHWAYS = {"motorway", "trunk", "primary", "secondary", "tertiary",
                   "unclassified", "residential", "living_street", "service"}
NEAR_M = 35.0        # a road this close to the boundary = frontage
FAR_M = 150.0        # no road within this = likely landlocked


# ---------------------------------------------------------------- geometry ----
def _rings_from_wkt(wkt: str) -> list[list[list[float]]]:
    """WKT POLYGON/MULTIPOLYGON -> list of [lng,lat] rings. [] if unparseable."""
    if not wkt:
        return []
    rings = []
    # Rings are separated by "))" / "),(" in WKT; the robust approach is to read
    # each innermost parenthesis group and pull its coordinate pairs.
    for group in re.findall(r"\(([^()]*)\)", wkt):
        pts = re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", group)
        if len(pts) >= 3:
            rings.append([[float(x), float(y)] for x, y in pts])
    if not rings:
        pts = re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", wkt)
        if len(pts) >= 3:
            rings = [[[float(x), float(y)] for x, y in pts]]
    return rings


def _centroid(rings: list) -> tuple[float, float] | None:
    pts = [p for ring in rings for p in ring]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _envelope_ring(lng: float, lat: float, pad: float = 0.0009) -> list:
    """A small box (~100m) around a point, for a centroid-only fallback."""
    return [[lng - pad, lat - pad], [lng + pad, lat - pad],
            [lng + pad, lat + pad], [lng - pad, lat + pad], [lng - pad, lat - pad]]


def _geom_param(rings: list) -> str:
    return json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})


def _haversine_m(a: tuple, b: tuple) -> float:
    lng1, lat1 = a
    lng2, lat2 = b
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


# --------------------------------------------------------------- ArcGIS -------
def _arcgis_intersect(url: str, rings: list, out_fields: str, session) -> list[dict] | None:
    """Features from an ArcGIS layer intersecting the polygon. None on failure."""
    try:
        resp = session.post(url, timeout=TIMEOUT, data={
            "geometry": _geom_param(rings),
            "geometryType": "esriGeometryPolygon",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.debug("ArcGIS error from %s: %s", url, data["error"])
            return None
        return [f.get("attributes", {}) for f in data.get("features", [])]
    except (requests.RequestException, ValueError) as exc:
        log.debug("ArcGIS query failed %s: %s", url, exc)
        return None


def check_wetlands(rings: list, session) -> dict:
    feats = _arcgis_intersect(NWI_URL, rings, "WETLAND_TYPE,ACRES", session)
    if feats is None:
        return {"status": "unknown"}
    if not feats:
        return {"status": "none"}
    types = sorted({(f.get("WETLAND_TYPE") or "").strip() for f in feats if f.get("WETLAND_TYPE")})
    # "Freshwater Pond"/"Riverine" etc. all count; drop nothing but blanks.
    return {"status": "wetland", "types": types or ["mapped wetland"], "features": len(feats)}


def check_flood(rings: list, session) -> dict:
    feats = _arcgis_intersect(FEMA_URL, rings, "FLD_ZONE,ZONE_SUBTY", session)
    if feats is None:
        return {"status": "unknown"}
    zones = sorted({(f.get("FLD_ZONE") or "").strip() for f in feats if f.get("FLD_ZONE")})
    if not zones:
        return {"status": "none"}
    sfha = [z for z in zones if z in SFHA_ZONES]
    return {"status": "sfha" if sfha else "mapped", "zones": zones,
            "sfha_zones": sfha}


# ----------------------------------------------------------------- roads ------
def check_access(rings: list, session) -> dict:
    """Nearest public road to the parcel boundary, via OSM Overpass."""
    cen = _centroid(rings)
    if not cen:
        return {"status": "unknown"}
    lng, lat = cen
    # Roads within FAR_M of the centroid; we then measure to the boundary.
    q = (f"[out:json][timeout:20];way(around:{int(FAR_M)+200},{lat},{lng})"
         f"[highway];out geom;")
    try:
        resp = session.post(OVERPASS_URL, data={"data": q}, timeout=TIMEOUT)
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except (requests.RequestException, ValueError) as exc:
        log.debug("Overpass query failed: %s", exc)
        return {"status": "unknown"}
    boundary = [tuple(p) for ring in rings for p in ring]
    nearest = None
    for el in elements:
        if el.get("tags", {}).get("highway") not in ACCESS_HIGHWAYS:
            continue
        for nd in el.get("geometry", []) or []:
            rp = (nd.get("lon"), nd.get("lat"))
            if rp[0] is None:
                continue
            for bp in boundary:
                d = _haversine_m(rp, bp)
                if nearest is None or d < nearest:
                    nearest = d
    if nearest is None:
        return {"status": "landlocked", "nearest_road_m": None}
    if nearest <= NEAR_M:
        return {"status": "frontage", "nearest_road_m": round(nearest)}
    if nearest >= FAR_M:
        return {"status": "landlocked", "nearest_road_m": round(nearest)}
    return {"status": "verify", "nearest_road_m": round(nearest)}


# ---------------------------------------------------------------- verdict -----
def derive(wet: dict, flood: dict, access: dict) -> dict:
    reasons, avoid, review = [], False, False
    if wet.get("status") == "wetland":
        review = True
        reasons.append("Wetland mapped on the parcel (" + ", ".join(wet.get("types", [])) + ")")
    if flood.get("status") == "sfha":
        review = True
        reasons.append("In a FEMA flood zone (" + ", ".join(flood.get("sfha_zones", [])) + ")")
    if access.get("status") == "landlocked":
        avoid = True
        m = access.get("nearest_road_m")
        reasons.append("No public road on the parcel" + (f" — nearest is ~{m} m away" if m else "") + " — likely landlocked")
    elif access.get("status") == "verify":
        review = True
        reasons.append(f"Nearest public road ~{access.get('nearest_road_m')} m from the parcel — confirm legal access")
    verdict = "avoid" if avoid else "review" if review else "clear"
    if verdict == "clear" and all(d.get("status") == "unknown" for d in (wet, flood, access)):
        verdict = "unknown"
    if not reasons and verdict == "clear":
        reasons.append("No wetland, flood zone or access problem found in the public maps")
    return {"verdict": verdict, "reasons": reasons}


def assess(geometry_wkt: str | None, lat=None, lng=None, session=None) -> dict:
    """Full land check for one parcel. Never raises."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", UA)
    rings = _rings_from_wkt(geometry_wkt or "")
    if not rings and lat is not None and lng is not None:
        rings = [_envelope_ring(float(lng), float(lat))]
    if not rings:
        return {"verdict": "unknown", "reasons": ["No parcel boundary or coordinates to check"],
                "wetland": {"status": "unknown"}, "flood": {"status": "unknown"},
                "access": {"status": "unknown"}}
    wet = check_wetlands(rings, sess)
    flood = check_flood(rings, sess)
    access = check_access(rings, sess)
    out = derive(wet, flood, access)
    out.update({"wetland": wet, "flood": flood, "access": access,
                "from": "boundary" if _rings_from_wkt(geometry_wkt or "") else "centroid"})
    return out
