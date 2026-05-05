# Geo-Coordinate System Specification

**Status:** Approved (ARCH-2)
**Sprint:** 2530
**Owner:** Architecture
**Last Updated:** 2026-04-29

## 1. Overview

Automatica overlays a real-world geographic surface onto a flat RTS game grid.
Players' GPS coordinates (WGS-84 lat/lon from device GNSS) must translate to
deterministic, integer-friendly game-world coordinates while preserving
real-world distances at human (1-meter) scale.

This document specifies:

- The projection pipeline used to convert GPS → game coords.
- The grid scale, world radius, and chunk layout.
- Geohash precision tiers used for territory and spatial indexing.
- Reference formulas (with worked example) so any service or client can
  reimplement the math identically.

## 2. Goals & Non-Goals

### Goals

- **G1.** 1 game unit == 1 meter at the player's local scale.
- **G2.** Distances and bearings within ~50 km of the world origin must agree
  with great-circle reality to within ≤ 0.5 % error.
- **G3.** Deterministic, lossless round-trip: `gps → game → gps` within
  ≤ 1 cm tolerance for any coordinate inside the world radius.
- **G4.** Stable mapping to geohash precision tiers for territory/chunk
  systems and Redis-style spatial indices.

### Non-Goals

- Global (planetary-scale) RTS play. Worlds beyond 50 km radius are out of
  scope; a separate "world stitching" design will handle multi-world play.
- Altitude / 3D terrain. The game plane is 2D; elevation is metadata only.
- Geodesic accuracy beyond ~0.5 %. Players never measure mm-precision
  distances at km scale, so a tangent-plane approximation is acceptable.

## 3. Coordinate Spaces

| Space               | Symbol      | Units                  | Notes                                    |
|---------------------|-------------|------------------------|------------------------------------------|
| WGS-84 geographic   | (φ, λ)      | degrees                | Raw GNSS input; φ = latitude, λ = longitude |
| Web Mercator        | (Xm, Ym)    | meters                 | EPSG:3857; intermediate, web-tile aligned |
| Local tangent plane | (x, y)      | meters                 | Origin = world GPS anchor; flat RTS grid |
| Game grid           | (gx, gy)    | game units (1 u = 1 m) | Same as tangent plane, integer-aligned   |
| Chunk index         | (cx, cy)    | chunks (256 m each)    | `cx = floor(gx / 256)`                   |
| Geohash             | string      | base-32 chars          | Precision 6 → chunk, precision 7 → tile  |

## 4. Projection Pipeline

```
WGS-84 (lat, lon)
   │  step A: project to Web Mercator (EPSG:3857)
   ▼
Web Mercator (Xm, Ym)         ← canonical interchange format with map tiles
   │  step B: subtract origin & apply latitude correction
   ▼
Local tangent plane (x, y)    ← flat, equal-scale RTS coordinates
   │  step C: scale (1:1) + integer rounding
   ▼
Game grid (gx, gy)
```

### 4.1 Step A — Web Mercator (EPSG:3857)

```
R       = 6_378_137.0                       # WGS-84 semi-major axis (meters)
Xm(λ)   = R · radians(λ)
Ym(φ)   = R · ln( tan( π/4 + radians(φ)/2 ) )
```

Web Mercator is conformal but **not** equal-area: distances stretch with
latitude. Step B corrects for this so that 1 game unit == 1 meter at the
world origin.

### 4.2 Step B — Local Tangent Plane

Let `(φ0, λ0)` be the configured **world origin** (the GPS anchor point of
the playable region). Compute the Mercator origin once at world load:

```
Xm0 = R · radians(λ0)
Ym0 = R · ln( tan( π/4 + radians(φ0)/2 ) )
k0  = cos(radians(φ0))      # latitude scale factor at origin
```

Then for any incoming GPS sample `(φ, λ)`:

```
x = (Xm(λ) - Xm0) · k0
y = (Ym(φ) - Ym0) · k0
```

Multiplying by `k0` cancels Mercator's secant-of-latitude stretch at the
origin. Within ±50 km of `(φ0, λ0)` this preserves real-world distances to
≤ 0.5 % — well below player-perceptible error.

### 4.3 Step C — Game Grid

```
gx = round(x)
gy = round(y)
```

Because 1 unit = 1 meter, no further scaling is needed. The grid is
right-handed: +x = east, +y = north.

### 4.4 Inverse: Game → GPS

```
Xm  = Xm0 + gx / k0
Ym  = Ym0 + gy / k0
λ   = degrees(Xm / R)
φ   = degrees( 2 · atan( exp(Ym / R) ) - π/2 )
```

## 5. Alternative Formula — Direct Haversine Inversion

For services that want to skip the Mercator intermediate, the
equirectangular / local-tangent shortcut is mathematically equivalent to
Step B above:

```
x = R · cos(radians(φ0)) · radians(λ - λ0)
y = R · radians(φ - φ0)
```

The forward haversine distance `d` between two GPS points is the
ground-truth check used by tests:

```
a = sin²(Δφ/2) + cos(φ1)·cos(φ2)·sin²(Δλ/2)
d = 2·R·asin(√a)
```

Implementations MUST cross-check `hypot(gx2 - gx1, gy2 - gy1) ≈ d` to
within 0.5 % for points within the world radius.

## 6. Scale & World Bounds

| Parameter           | Value         | Rationale                                      |
|---------------------|---------------|------------------------------------------------|
| Game unit           | 1 u = 1 m     | Matches human/RTS scale; trivial UI math       |
| World origin        | configurable  | Per-instance GPS anchor (φ0, λ0)               |
| Max world radius    | 50 km         | 100 km diameter map; tangent-plane error ≤ 0.5 % |
| Max world span      | 100 km × 100 km | Bounding box around origin                   |
| Coord storage       | int32 meters  | ±2.1 × 10⁹ m headroom — far beyond world radius |

Coordinates outside the 50 km radius are clamped at ingress and flagged
`OUT_OF_WORLD`; clients may not act on them.

## 7. Chunking

The world is partitioned into fixed **256 m × 256 m chunks**. Chunk size
was chosen to match geohash precision-6 cell width at mid-latitudes
(≈ 1.2 km × 0.6 km nominally → we subdivide further; see §8) so chunk
boundaries align cleanly with geohash boundaries.

```
CHUNK_SIZE_M = 256
cx = floor(gx / CHUNK_SIZE_M)
cy = floor(gy / CHUNK_SIZE_M)
chunk_id = (cx, cy)            # signed pair, int32 each
```

Chunks are the unit of:
- Server-side spatial sharding & save-file granularity.
- Client streaming / LOD: load 3×3 chunks centered on player.
- Authority hand-off between regional services.

## 8. Geohash Precision Tiers

Standard geohash (base-32) cell sizes at the equator:

| Precision | Cell width × height (approx) | Use                                         |
|-----------|------------------------------|---------------------------------------------|
| 5         | 4.9 km × 4.9 km              | Region / shard routing                      |
| 6         | 1.2 km × 0.6 km              | **Chunk-cluster index** (groups ~24 chunks) |
| 7         | 153 m × 153 m (≈ 150 m²)     | **Smallest territory unit** (tile)          |
| 8         | 38 m × 19 m                  | Micro-objectives, capture points            |
| 9         | 4.8 m × 4.8 m                | Building footprint snap                     |

**Authoritative mapping for ARCH-2:**

- **Precision 7 ≈ 150 m × 150 m** is the smallest territory unit
  ("tile"). Territory ownership, capture timers, and resource yield are
  resolved per-tile.
- **Precision 6** anchors chunk boundaries: every 256 m chunk's
  south-west corner aligns to a precision-6 cell edge after the world
  origin offset.
- **Precision 5** identifies the regional shard a chunk belongs to and
  is used for cross-region service routing.

A geohash for any game coord is computed by inverse-projecting back to
GPS (§4.4) and feeding the lat/lon into a standard geohash encoder at
the desired precision.

## 9. Worked Example

World origin: Trafalgar Square, London — `(φ0, λ0) = (51.5080°, -0.1281°)`.

Player GPS: `(51.5090°, -0.1270°)` (≈ 110 m NNE of origin).

```
R   = 6_378_137
k0  = cos(radians(51.5080)) = 0.6225

Xm0 = R · radians(-0.1281)  = -14_260.1
Ym0 = R · ln(tan(π/4 + radians(51.5080)/2)) = 6_711_502.4

Xm  = R · radians(-0.1270)  = -14_137.8
Ym  = R · ln(tan(π/4 + radians(51.5090)/2)) = 6_711_681.7

x   = (Xm - Xm0) · k0 = ( 122.3) · 0.6225 ≈   76.1 m   (east)
y   = (Ym - Ym0) · k0 = ( 179.3) · 0.6225 ≈  111.6 m   (north)

gx, gy = (76, 112)
```

Haversine ground truth between the two GPS points: ≈ 135.1 m.
`hypot(76, 112)` = 135.4 m → 0.22 % error. ✅

Chunk: `(cx, cy) = (0, 0)` — same chunk as origin.
Geohash @ precision 7 of `(51.5090, -0.1270)`: `gcpvj0u` (smallest
territory unit containing the player).

## 10. Reference Implementation Skeleton (Python)

```python
import math
from dataclasses import dataclass

R = 6_378_137.0
CHUNK_SIZE_M = 256

@dataclass(frozen=True)
class WorldOrigin:
    lat: float
    lon: float

    @property
    def xm0(self) -> float:
        return R * math.radians(self.lon)

    @property
    def ym0(self) -> float:
        return R * math.log(math.tan(math.pi / 4 + math.radians(self.lat) / 2))

    @property
    def k0(self) -> float:
        return math.cos(math.radians(self.lat))


def gps_to_game(origin: WorldOrigin, lat: float, lon: float) -> tuple[int, int]:
    xm = R * math.radians(lon)
    ym = R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    x = (xm - origin.xm0) * origin.k0
    y = (ym - origin.ym0) * origin.k0
    return round(x), round(y)


def game_to_gps(origin: WorldOrigin, gx: int, gy: int) -> tuple[float, float]:
    xm = origin.xm0 + gx / origin.k0
    ym = origin.ym0 + gy / origin.k0
    lon = math.degrees(xm / R)
    lat = math.degrees(2 * math.atan(math.exp(ym / R)) - math.pi / 2)
    return lat, lon


def chunk_of(gx: int, gy: int) -> tuple[int, int]:
    return gx // CHUNK_SIZE_M, gy // CHUNK_SIZE_M
```

## 11. Validation & Test Hooks

Required automated checks (to live under `tests/test_geo_coords.py`):

1. **Round-trip:** `game_to_gps(gps_to_game(p)) ≈ p` for 1 000 random
   GPS points within 50 km of origin → max drift ≤ 1 cm.
2. **Distance fidelity:** for random pairs within world radius, compare
   Euclidean game-distance to haversine ground truth → ≤ 0.5 % error.
3. **Chunk alignment:** every chunk boundary maps to a multiple of
   `CHUNK_SIZE_M` in game space and to a precision-6 geohash edge in
   GPS space (after origin offset).
4. **Out-of-world clamp:** points beyond 50 km radius produce
   `OUT_OF_WORLD` and are not silently truncated.

## 12. Open Questions / Future Work

- **World stitching:** how do two adjacent 100 km worlds hand off
  players at the boundary? Deferred to ARCH-9.
- **Altitude:** terrain height is currently ignored. A z-channel
  (meters above WGS-84 ellipsoid) may be added without changing §4.
- **Polar play:** Web Mercator distorts severely above ±85° latitude.
  A polar stereographic variant of §4 will be specified if/when polar
  worlds ship.

## 13. References

- EPSG:3857 — Web Mercator definition.
- Snyder, J. P. *Map Projections — A Working Manual* (USGS PP-1395).
- Niemeyer, G. — Geohash specification (2008).
- WGS-84 datum, NGA TR8350.2.
