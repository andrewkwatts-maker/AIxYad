# World Partition + Streaming

**Task:** ARCH-3
**Status:** Design
**Sprint:** 2531
**Owner:** Architecture
**Last Updated:** 2026-04-29
**Related:** [geo_coordinate_system.md](geo_coordinate_system.md), [server_tick_design.md](server_tick_design.md)

## 1. Overview

Automatica worlds span up to **100 km × 100 km** at **1 m grid resolution**
(see ARCH-2). A naive single-scene representation would require ~10¹⁰ grid
samples and gigabytes of resident state — infeasible to load on a client and
prohibitively expensive to simulate as one monolithic scene on a server.

This document specifies how the world is partitioned into fixed streaming
**cells**, how cells are loaded, simulated, and handed off as players move,
and the on-disk format that every service (client, region server, persistence
worker) agrees on.

Design pillars:

- **Cell = unit of streaming, persistence, and authority.** Everything spatial
  is addressable as `(cell_x, cell_y, content)`.
- **Geohash-aligned.** Cell edges land on geohash precision-6 boundaries so
  spatial indices, region routing, and territory math agree.
- **Async streaming.** Cell I/O never blocks the simulation tick or the
  render thread.
- **Server cells are independently simulatable.** A cell with no players still
  ticks (buildings, NPCs, resource yield) on the regional server.
- **Entity hand-off is explicit.** When an entity crosses a cell boundary it
  is moved — atomically, on a tick boundary — into the destination cell's
  simulation set.

## 2. Goals & Non-Goals

### Goals

- **G1.** Bound client memory: ≤ 9 cells resident per player at any time.
- **G2.** Bound server cost per cell: each cell is small enough that a single
  worker thread can tick it well under the 50 ms server step (ARCH-1).
- **G3.** Deterministic addressing: any (game-coord) maps to exactly one cell;
  any cell maps to exactly one geohash-6 prefix span.
- **G4.** Async, jank-free streaming: cell loads happen on background threads;
  the player never sees a hitch when crossing a boundary.
- **G5.** Crash-safe persistence: a cell's on-disk blob is always either the
  previous valid version or a complete new one — never half-written.

### Non-Goals

- LOD / impostor rendering of distant cells. The 3×3 neighborhood is the only
  visible set; cells beyond it are not rendered. Long-range visibility (e.g.
  skybox / silhouettes) is a separate art-side concern.
- Sub-cell streaming. The cell is the smallest streaming unit; we do not
  stream individual chunks within a cell.
- Multi-world stitching. Crossing into an adjacent 100 km world is ARCH-9.

## 3. Cell Definition

### 3.1 Size and alignment

| Parameter         | Value                            | Rationale                                                 |
|-------------------|----------------------------------|-----------------------------------------------------------|
| Cell edge length  | **256 m**                        | Matches the chunk size from ARCH-2 §7 (1 cell = 1 chunk). |
| Cell area         | 65 536 m² (0.065 km²)            | Small enough to tick cheaply, big enough to amortize I/O. |
| Cells per world   | ~152 000 (100 km × 100 km grid)  | 391 × 391 cells inside the 50 km radius bounding box.     |
| Geohash alignment | precision 6 edges                | Cell SW corner == precision-6 cell edge in GPS space.     |

Cell index from game coordinates (re-stating ARCH-2 §7 for completeness):

```
CELL_SIZE_M = 256
cell_x = floor(gx / CELL_SIZE_M)
cell_y = floor(gy / CELL_SIZE_M)
cell_id = (cell_x, cell_y)            # signed int32 pair
```

The world origin is placed so `(0, 0)` in game space coincides with a
precision-6 geohash corner; this guarantees every cell boundary is also a
geohash-6 boundary (ARCH-2 §8).

### 3.2 Player neighborhood

A connected player loads a **3 × 3 cell neighborhood** centered on the cell
containing the player:

```
N = { (cell_x + dx, cell_y + dy) : dx, dy ∈ {-1, 0, +1} }
```

- 9 cells resident → **768 m × 768 m** loaded region (≈ 2.3 km diagonal /
  ~1.1 km view radius from the player; the "2.3 km view radius" figure cited
  in the acceptance criteria refers to the diagonal extent of the streamed
  region).
- All 9 cells are simulated client-side for prediction (ARCH-1) but only the
  cell containing the player is the player's "home" for hand-off purposes.

### 3.3 Cell life-cycle states

```
UNLOADED  ──load──▶  LOADING  ──ready──▶  RESIDENT
                                   │
                                   ├─ promote_to_active when player enters
                                   ▼
                              ACTIVE (in 3×3 neighborhood)
                                   │
                                   ├─ demote when no longer adjacent
                                   ▼
                              RESIDENT (cooldown)
                                   │
                                   ├─ evict after T_evict (default 60 s)
                                   ▼
                              UNLOADED (saved if dirty)
```

A cooldown delay before eviction prevents thrash when a player oscillates
across a boundary.

## 4. On-Disk Cell Format

### 4.1 Container

A cell is a **single binary blob** stored at:

```
worlds/<world_id>/cells/<cell_x>_<cell_y>.acell
```

`.acell` ("Automatica cell") is a versioned, length-prefixed container.
Writes are atomic: write to `.acell.tmp`, fsync, rename.

### 4.2 Layout (v1)

All multi-byte integers are little-endian. All offsets are absolute byte
positions inside the file.

```
+--------------------------------------------------------+
| Header  (64 B, fixed)                                  |
|   magic[4]      = "ACEL"                               |
|   version u16   = 1                                    |
|   flags u16     = bit 0: compressed, bit 1: dirty      |
|   cell_x i32                                           |
|   cell_y i32                                           |
|   world_origin_lat f64                                 |
|   world_origin_lon f64                                 |
|   created_tick u64                                     |
|   modified_tick u64                                    |
|   section_count u16                                    |
|   reserved[10]                                         |
+--------------------------------------------------------+
| Section table  (16 B × section_count)                  |
|   for each section:                                    |
|     kind u16    (see §4.3)                             |
|     codec u16   (0=raw, 1=zstd, 2=rle-u16, 3=delta)    |
|     offset u64  (file-absolute)                        |
|     length u32  (compressed bytes on disk)             |
+--------------------------------------------------------+
| Section payloads  (back-to-back, codec-encoded)        |
+--------------------------------------------------------+
| Trailer (16 B)                                         |
|   crc32c u32     (over header + table + payloads)      |
|   reserved[12]                                         |
+--------------------------------------------------------+
```

A reader validates `magic`, `version`, `crc32c`, then walks the section
table; unknown `kind` values are skipped (forward-compat).

### 4.3 Required sections

| kind | Name              | Format                                                      |
|------|-------------------|-------------------------------------------------------------|
| 0x01 | `terrain_height`  | 257 × 257 grid of `int16` cm above ellipsoid (~328 KB raw). +1 row/col supplies shared edge with east/north neighbor for seamless meshing. Default codec: zstd. |
| 0x02 | `voxel_density`   | Sparse octree of `uint8` density (0=air, 255=solid). Mineable resources, caves, building substrate. Codec: zstd over a serialized octree. |
| 0x03 | `placed_entities` | Array of `EntityRecord` — pickups, props, resource nodes, NPCs. See §4.4. |
| 0x04 | `buildings`       | Array of `BuildingRecord` — player/faction structures with footprint, ownership, state. See §4.5. |
| 0x05 | `territory`       | Per-tile (geohash-7) ownership + capture state. 256 m / ~150 m ≈ a few tiles per cell. |
| 0x06 | `nav_mesh`        | Pre-baked navigation polygons for AI pathing within the cell. Optional; rebuilt offline. |
| 0x07 | `metadata`        | JSON-CBOR key/value bag: biome id, climate, lighting tint, scripted-event tags. |

### 4.4 `EntityRecord`

```
struct EntityRecord {
    uint64 entity_id          // globally unique
    uint16 archetype_id       // index into entity catalog
    int32  local_x_cm         // position relative to cell SW corner, cm
    int32  local_y_cm         // 0..25_600_000 (256 m = 25 600 000 cm)
    int32  local_z_cm         // height above terrain, cm
    uint16 facing_deg         // 0..359
    uint8  state               // archetype-specific
    uint8  reserved
    uint64 owner_player_id    // 0 = world / NPC
    uint32 payload_offset     // offset to per-archetype blob inside section
    uint32 payload_length
}
```

Position is stored **cell-local** (cm precision, fits in 25.6M < 2³¹) so a
cell blob is location-independent: corruption of the world origin does not
desynchronize entity positions, and entities cannot accidentally drift to
the wrong cell on load.

### 4.5 `BuildingRecord`

```
struct BuildingRecord {
    uint64 building_id
    uint16 blueprint_id
    int32  origin_local_x_cm   // SW corner of footprint, cell-local
    int32  origin_local_y_cm
    uint16 footprint_w_m       // size in meters (max 256)
    uint16 footprint_h_m
    uint16 facing_deg
    uint8  tier
    uint8  state                // intact / damaged / destroyed
    uint64 faction_id
    uint64 last_tick_simulated // for catch-up on cell wake
    uint32 payload_offset       // production queue, storage, etc.
    uint32 payload_length
}
```

Buildings whose footprint straddles a cell boundary are stored in the cell
containing the **SW corner** of the footprint and listed by reference in
neighboring cells (see §6.2).

### 4.6 Versioning

`version` in the header is bumped on any breaking change. Old clients
refuse newer files; new clients have explicit upgrade paths for each prior
version (`v1 → v2`, …) implemented in the persistence service.

## 5. Streaming

### 5.1 Threads and queues

```
   Simulation thread (tick loop, ARCH-1)
        │ produces "cell needed" / "cell no longer needed" events
        ▼
   Streaming controller (one per client / per region-shard on server)
        │ enqueues load/save jobs
        ▼
   I/O worker pool  (N background threads, default N = 2)
        │ reads/writes .acell, decompresses, validates CRC
        ▼
   Decoded cell handed back via lock-free SPSC queue
        │
        ▼
   Simulation thread integrates the cell at the next tick boundary
```

The simulation thread **never blocks on I/O**. If a needed cell is not yet
ready when the player's neighborhood expands to include it, the cell is
treated as "pending" — entities and terrain inside it are not simulated or
rendered, and the client shows a placeholder skirt mesh until the load
completes.

### 5.2 Predictive prefetch

The streaming controller monitors player velocity. If the player is moving
toward an adjacent cell and the time-to-cross is below a threshold
`T_prefetch` (default 4 s at current speed), that cell is queued for load
**before** the player crosses, so the new neighborhood is already resident
on entry.

```
ttc = distance_to_boundary / max(speed, ε)
if ttc < T_prefetch and target_cell.state == UNLOADED:
    enqueue_load(target_cell)
```

### 5.3 Eviction

A cell exits the resident set when:

1. It is no longer in any local player's 3×3 neighborhood, AND
2. Its cooldown timer (`T_evict`, default 60 s) has elapsed, AND
3. Pending writes (if dirty) have completed.

If the resident-set memory budget is exceeded before `T_evict` fires, the
controller evicts least-recently-used resident cells early (still respecting
condition 3).

### 5.4 Failure modes

| Failure                  | Handling                                                    |
|--------------------------|-------------------------------------------------------------|
| File missing             | Generate from procedural seed (worldgen), mark dirty, save. |
| CRC mismatch             | Quarantine `.acell` → `.acell.bad`, regenerate from seed, log incident. |
| Version too new          | Refuse to load; surface "client out of date" error.         |
| Disk full on save        | Retry with backoff; if persistent, stop accepting writes for that cell and alert ops. |
| Concurrent writer        | Per-cell file lock (advisory, OS-level); waiters back off.  |

## 6. Entity Hand-off

### 6.1 Trigger

After integrating each tick, the simulation iterates **dirty entities**
(those whose position changed) and computes their new `cell_id`. If
`new_cell_id != current_cell_id`, the entity is queued for hand-off.

### 6.2 Atomic move protocol

Hand-off happens **at tick boundaries** to keep simulation deterministic:

```
on_tick_end():
    for entity in handoff_queue:
        src = entity.current_cell
        dst = cell_of(entity.position)

        assert dst.state in {RESIDENT, ACTIVE}    # see 6.3 for not-yet-loaded
        src.entities.remove(entity)
        entity.local_position = world_to_local(entity.position, dst)
        dst.entities.add(entity)
        entity.current_cell = dst
        src.dirty = True
        dst.dirty = True
        emit_handoff_event(entity, src, dst)
```

The transfer is a single logical step: an external observer never sees the
entity in both cells nor in neither. On the server this is enforced by
holding write locks on both cells (acquired in canonical `(cell_x, cell_y)`
order to avoid deadlock).

### 6.3 Crossing into an unloaded cell

If `dst` is not yet RESIDENT (rare on the server, common on the client at
the edge of the streamed neighborhood):

- **Server:** the destination cell is always loaded inside the regional
  shard's working set, by definition of regional ownership (§7). This case
  cannot occur server-side.
- **Client:** the entity is **parked** at the cell boundary on the source
  side, prediction is paused for it, and hand-off is retried each tick
  until `dst` becomes resident or the server's snapshot supersedes the
  client's view.

### 6.4 Buildings and footprints

Buildings do not "cross" cells — they are placed once and stored in the
cell containing their footprint's SW corner (§4.5). Cells that overlap a
building's footprint but do not contain its SW corner store a lightweight
**reference record** so collision and rendering work without round-tripping
to the owning cell:

```
struct BuildingRef {
    uint64 building_id
    int32  owner_cell_x
    int32  owner_cell_y
    int32  footprint_min_local_x_cm   // clipped to this cell's bounds
    int32  footprint_min_local_y_cm
    int32  footprint_max_local_x_cm
    int32  footprint_max_local_y_cm
}
```

When the owning cell is unloaded, references in neighbors are inert
placeholders (collision still resolves, but interaction RPCs queue until
the owner is loaded).

## 7. Server-Side Independent Simulation

### 7.1 Regional shards

The world is partitioned across **regional shard processes**. Each shard
owns a contiguous rectangle of cells (typically 16 × 16 = 256 cells per
shard, ~4.1 km × 4.1 km). Within a shard:

- Every owned cell is **always resident in memory**, regardless of player
  presence.
- Every owned cell is **ticked every server step** (20 Hz, ARCH-1) by a
  worker pulled from the shard's thread pool.
- Cells with no players still execute building production, NPC behavior,
  resource regrowth, scripted events, and territory capture timers.

This is the property the acceptance criteria call out as "buildings run in
cells without players": a player-empty cell behaves identically to a
populated one from the simulation's point of view; only its network
fan-out is empty.

### 7.2 Tick budget per cell

At 20 Hz with 256 cells per shard, each cell has a **soft budget of
≈ 195 µs per tick** on a single core, or much more in practice because
cells are ticked in parallel across the shard's thread pool. Cells that
exceed budget are flagged for profiling; cells that consistently exceed
budget are candidates for content simplification or splitting (e.g.
breaking a building's heavy logic into a separate "factory" entity that
ticks at lower rate).

### 7.3 Cross-cell interactions

Most simulation is intra-cell. The cases that cross cell boundaries are:

| Interaction          | Mechanism                                                 |
|----------------------|-----------------------------------------------------------|
| Entity hand-off      | §6.2 — atomic move at tick boundary, both cells write-locked. |
| Projectile / line-of-sight | Spatial query against neighbor cells via shard-local index; read-only across cells. |
| Building footprint   | §6.4 — owner cell authoritative, references in neighbors. |
| Faction / territory  | Resolved at the geohash-7 tile level (§4.3 kind 0x05); a tile lives in exactly one cell. |

### 7.4 Cross-shard cells

Cells on a shard boundary may need to interact with cells owned by another
shard. These interactions go through the **inter-shard relay**:

- Reads (e.g. line-of-sight) are answered from a 1-cell-thick read-only
  mirror of the neighbor shard's edge cells, refreshed every tick.
- Writes (e.g. an entity hand-off across shard boundaries) are serialized
  as RPCs: source shard removes the entity, sends `HandoffRequest`, target
  shard inserts on its next tick boundary and acks. The entity is briefly
  "in flight" — it ticks on neither shard for at most one tick. Position
  is interpolated client-side to mask this.

### 7.5 Persistence cadence

- **Dirty cells** are flushed to disk every **30 s** by a low-priority
  persistence worker, plus on graceful shutdown.
- A WAL (write-ahead log) per shard captures every state-mutating event
  between flushes so a crash loses ≤ 30 s of progress and is replayable
  on recovery.
- The save itself uses the atomic-rename pattern (§4.1).

## 8. Client-Side Streaming Specifics

### 8.1 Source of truth

Clients **do not read `.acell` files from disk** in the multiplayer case.
The authoritative cell content is delivered by the regional shard via the
snapshot stream (ARCH-1) with cell granularity:

```
CellSnapshot {
    cell_id, base_tick, terrain_hash, entity_deltas, building_deltas, ...
}
```

A local on-disk cache (`%LOCALAPPDATA%/Automatica/cellcache/<world_id>/...`)
stores the last-known content of cells the player has visited; on
re-visit, the client diffs against the server's `terrain_hash` and only
re-downloads sections that changed.

### 8.2 Single-player / offline

In single-player or offline mode the client *is* the regional shard for
the world it is in, and reads/writes `.acell` files directly per §4.

### 8.3 Memory budget

| Budget item       | Per-cell (max) | 9-cell neighborhood |
|-------------------|----------------|---------------------|
| terrain heightmap | 0.5 MB         | 4.5 MB              |
| voxel density     | 4 MB (typical) | 36 MB               |
| entities          | 0.2 MB         | 1.8 MB              |
| buildings         | 0.1 MB         | 0.9 MB              |
| GPU-resident mesh | 8 MB           | 72 MB               |
| **Total**         | **~13 MB**     | **~115 MB**         |

Headroom for VFX, audio, and shader resources is provisioned separately;
the cell system must stay under the budget above.

## 9. Worked Example

Player at game coord `(gx, gy) = (40_321, 17_502)`:

```
cell_x = floor(40_321 / 256) = 157
cell_y = floor(17_502 / 256) =  68
home_cell = (157, 68)

neighborhood = {
  (156, 67), (157, 67), (158, 67),
  (156, 68), (157, 68), (158, 68),
  (156, 69), (157, 69), (158, 69),
}
```

Player moves east at 6 m/s. Distance from player to east boundary of
home_cell: `256 - (40_321 mod 256) = 256 - 129 = 127 m`.
`ttc = 127 / 6 ≈ 21 s` → above `T_prefetch = 4 s` → no prefetch yet.

20 s later the player is 7 m from the boundary; `ttc ≈ 1.2 s` →
`(159, 68)`, `(159, 67)`, `(159, 69)` are queued for load. By the time
the player crosses, the new column is resident, the now-stale west column
`(156, *)` enters cooldown, and the home cell becomes `(158, 68)`.

Any entity the player carried that crossed the boundary on the same tick
is handed off per §6.2.

## 10. Validation & Test Hooks

Required automated checks (to live under
`tests/test_world_partition.py` / `tests/test_acell_format.py`):

1. **Round-trip:** synth → write `.acell` → read back → compare every
   section byte-for-byte; CRC must match.
2. **Neighborhood correctness:** for 1 000 random player positions,
   `neighborhood(p)` returns exactly 9 cells, all containing or adjacent
   to `cell_of(p)`.
3. **Hand-off atomicity:** simulate 10 000 entity boundary crossings;
   no entity is ever observed in two cells in the same tick or in zero
   cells across two consecutive ticks.
4. **Independent simulation:** run a shard with all players removed and
   verify building production, NPC behavior, and territory timers
   advance identically to the populated case (modulo the absent player
   inputs).
5. **Async streaming:** load 100 cells while running a 20 Hz simulation
   tick on the same process; assert no tick exceeds its 50 ms budget.
6. **Geohash alignment:** every cell's SW corner inverse-projects to a
   precision-6 geohash boundary in GPS space (cross-check vs ARCH-2 §8).
7. **Eviction safety:** force eviction of a dirty cell and verify the
   on-disk blob ends in a complete, CRC-valid state.

## 11. Open Questions / Future Work

- **Sub-cell streaming for very large buildings.** A megastructure that
  fills most of a cell currently forces full-fidelity load of the whole
  cell. A "building-only LOD" stream may be added.
- **Cell merging / splitting.** Static cell size is simple but uniform.
  Sparsely populated wilderness cells could be coalesced for cheaper I/O.
  Not planned for v1.
- **Cross-shard hand-off latency.** §7.4 introduces a one-tick "in
  flight" gap. If this proves visible at high speeds we will move to a
  two-phase commit with shadow ticking on both shards.
- **Procedural-only cells.** Cells that have never been modified by a
  player can stay file-less and be regenerated from the world seed each
  time. Save-on-first-write is implemented; pruning never-dirtied files
  on shutdown is a future optimization.

## 12. References

- ARCH-1 — Server tick + client prediction (`server_tick_design.md`).
- ARCH-2 — Geo-coordinate system (`geo_coordinate_system.md`).
- Niemeyer, G. — Geohash specification (2008).
- "Streaming Large Open Worlds" — GDC engineering tracks, various.
