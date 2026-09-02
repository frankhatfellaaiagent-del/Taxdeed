# GIS "dirt" analysis — feasibility spike

**Status:** written finding, nothing shipped. Decision requested before any build.
**Date:** 2026-09-02 · **Context:** MADD Assets meeting (Aug 28, 2026).

## The idea (Jennifer's)

Instead of an AI title search — which we just removed as unreliable — grade the
**physical ground** of a parcel as a simple **yes/no**, to auto-separate bad
lots from good before anyone drives out:

> *"If it could analyze the value of the actual piece of ground as a yes or no…
> it's wet, you don't want it… this is landlocked… it would eliminate bad lots
> from good lots."*

Signals to detect: **wet / wetlands, landlocked (no legal road access), bad
topography (steep slope, a "big hole"), no utilities.** The appeal, in
Jennifer's words: *"they all have GIS"* — unlike online property searches,
GIS/mapping data is public and consistent, which is exactly what an automated
check needs.

## What we already have to build on

Every geometry check keys off the parcel's own footprint, which the pipeline
already captures from ReportAll:

| Field | What it is | Coverage in today's feed (3,235 records) |
|---|---|---|
| `parcel_geometry` | WKT boundary polygon (the true lot outline) | **20%** (658) — enriched parcels only |
| `lat` / `lng` | Parcel centroid | **64%** (2,082) |
| `property_use` | Appraiser land-use string | 21% (709) |

**Finding #1 — coverage is the first gap.** A land check is only as good as its
geometry. Boundaries (needed for wetland/flood *overlap*, road *frontage*) sit
at 20%; centroids (enough for a point-in-zone check) at 64%. Before this is
useful board-wide we'd run the existing ReportAll geometry backfill
(`python -m scraper enrich --backfill-geometry`) to lift boundary coverage on
upcoming, not-redeemed sales. Centroid-only parcels can still get a coarser
point check.

## What's answerable from free, public GIS (no paid data, no key)

Each of these is a public ArcGIS REST service you query by the parcel geometry
(or centroid). All return structured polygons/values — no scraping, no LLM
needed for the *facts*:

| Signal | Source (public REST) | Confidence | Notes |
|---|---|---|---|
| **Wet / wetlands** | USFWS **National Wetlands Inventory** MapServer | **High** | Intersect parcel ↔ wetland polygons → % of lot wet + wetland type. The single strongest "you don't want it" signal, and we already link NWI on each card. |
| **Flood** | FEMA **National Flood Hazard Layer** (NFHL) | **High** | Zone A/AE/VE vs X by geometry. Not "bad" per se but a real value/insurability flag. |
| **Topography / slope / "hole"** | USGS **3DEP** elevation (National Map) | **Medium** | Sample elevation across the lot → slope range and low-spot depth. Detects steep or a borrow-pit "hole". Heavier compute. |
| **Landlocked (road access)** | Parcel boundary ↔ road centerlines (OSM/Overpass or state DOT) | **Medium** | Does any edge of the lot touch a public road? No public *legal-easement* layer exists, so this is "physical frontage", a strong proxy but not a title opinion. |
| **Utilities (water/sewer/power)** | — | **Low (public)** | No consistent nationwide public layer. Proxy only: distance to nearest structure/road. This is the weakest one from free data. |

**Finding #2 — the geometric facts don't need an LLM.** Wetland/flood/slope/
frontage are deterministic spatial queries. A rule engine that hits these REST
services and emits `{wet: 0.4, flood: "AE", slope_max: 3%, road_frontage: true,
verdict: "review", reasons: [...]}` is **more reliable, cheaper, and more
explainable** than asking a model to eyeball a map. Reserve any LLM for turning
those facts into one plain sentence — not for deriving them.

**Finding #3 — utilities is where free data runs out.** Water/sewer/power
availability has no clean public source. This is the main thing a paid overlay
buys.

## Where Land ID / a paid overlay fits

Frank noted MADD already spoke to **Land ID** (formerly MapRight) and they're
keying to parcel ID. A paid overlay (Land ID, or Regrid/LandGlide-class data)
would add, per parcel: consolidated wetland/flood/soil/topo overlays and, in
some products, **utility service-area** layers and **soils** (agricultural
value) — the two things free GIS can't reliably give. Trade-off: per-parcel or
subscription cost, and an API/account dependency. **Recommendation: don't gate
v1 on it.** Build the free-GIS checks first (they cover the top signals — wet,
flood, slope, frontage), then add a paid overlay only for utilities/soils if
MADD finds those decisive.

## Reliability across 67 counties

The reason to prefer this over the title-search AI: these services are
**statewide/national single layers** (one NWI service covers all of Florida,
one NFHL, one 3DEP), not 67 different county websites. So unlike the clerk
document problem, coverage is uniform — the failure mode is a missing parcel
*geometry* on our side (Finding #1), not an unreadable county page.

## Recommendation

**Build a rule-based land check v1 from free public GIS, geometry-first, no paid
data, no LLM for the facts.** Concretely:

1. **Backfill geometry** on upcoming sales (existing job) so the check has lots
   to run against.
2. **A server-side land-check** (reuse the dormant `analyze-property` Edge
   Function shell, or a Python enrichment pass) that, per parcel, queries NWI +
   NFHL + 3DEP + road frontage by geometry and writes a structured verdict
   (`good / review / avoid` + reasons + the numbers) — cached like the other
   enrichment, so it's computed once and shared.
3. **Surface it on the card and as a filter** ("hide wet/landlocked lots"),
   which is the actual workflow win Jennifer described.
4. **LLM only for the one-line summary**, if at all.
5. **Land ID / paid overlay = fast-follow** for utilities + soils once v1 proves
   the pattern.

**Do not build yet** — this document is the decision point. If Frank says "go",
it becomes its own plan (the geometry backfill + the land-check service are each
a meaningful chunk, and the road-frontage/slope pieces need a short accuracy
test on ~30 known-good/known-bad MADD parcels before we trust the verdict).

## Explicitly out of scope for the spike

- No live calls to paid providers.
- No user-facing change shipped.
- No title/legal-access opinion — "physical frontage", not "legal easement".
