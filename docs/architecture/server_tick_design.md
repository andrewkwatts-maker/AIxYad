# Server Tick + Client Prediction Design

**Task:** ARCH-1
**Status:** Design
**Sprint:** 2529

## 1. Overview

This document specifies the authoritative server tick architecture and matching
client-side prediction / reconciliation model used by the game engine. The goal
is responsive local input on the client while preserving a single authoritative
source of truth on the server, with deterministic recovery from divergence.

Design pillars:

- **Server is authoritative.** The server's simulation state is canonical. The
  client never tells the server "I am at X"; it only tells the server "I pressed
  these inputs at tick T".
- **Fixed server tick, uncapped client render.** The simulation runs at a fixed
  step. Rendering is decoupled and may run far above tick rate.
- **Optimistic client prediction.** Inputs are applied locally on the same tick
  they are sent so the player sees immediate response.
- **Reconciliation by replay + smoothing.** When the server's snapshot disagrees
  with the predicted state, the client replays unacknowledged inputs from the
  corrected baseline and visually lerps over a short window so the player does
  not see a hard pop.

## 2. Tick Loop (Authoritative Server)

### 2.1 Rate

- **Server tick rate:** 20 Hz fixed (50 ms per tick, `dt = 0.050`).
- **Client render rate:** uncapped (driven by display / vsync / engine loop).
- **Network send rate:** server broadcasts a snapshot every tick (20 Hz).
- **Client input send rate:** every tick boundary the client crosses while
  collecting input (effectively 20 Hz per player), batched if the client
  produces multiple input frames between sends.

### 2.2 Tick identity

- `tickId : uint64` — monotonic, starts at 0 at match start, increments by 1
  every server step. Wraparound is not a concern at 20 Hz (≈ 29 billion years).
- All packets (input, snapshot, ack) carry a `tickId`.

### 2.3 Server loop pseudocode

```
nextTickTime = now()
while running:
    sleep_until(nextTickTime)
    nextTickTime += 50ms

    # 1. Drain input queue: apply every input whose tickId <= currentTick.
    #    Late inputs (tickId < currentTick) are applied to currentTick anyway
    #    and the client will reconcile.
    for input in inputQueue.drain():
        apply_input(input.playerId, input)
        lastAckedTick[input.playerId] = max(lastAckedTick[input.playerId], input.tickId)

    # 2. Step deterministic simulation by exactly dt = 50ms.
    simulate(dt = 0.050)

    # 3. Build snapshot for currentTick.
    snap = build_snapshot(currentTick)

    # 4. For each connected client, send delta-compressed snapshot
    #    against that client's last acknowledged baseline.
    for client in clients:
        delta = delta_encode(snap, baseline=client.lastAckedSnapshot)
        send(client, SnapshotPacket(currentTick, lastAckedTick[client.id], delta))

    currentTick += 1
```

The simulation step is a pure function of (previous state, inputs collected
this tick). No wall-clock reads, no `Random()` without a seeded RNG. This is a
hard requirement so the client can replay it bit-for-bit during reconciliation.

## 3. Input Packet Format

Sent client → server. One packet per local tick, but the client may bundle the
last N unacknowledged inputs in each packet for redundancy under packet loss.

```
InputPacket {
    tickId      : uint64    // tick at which client believes this input applies
    playerId    : uint32    // server-assigned player id
    inputFlags  : uint16    // bitfield, see below
    targetPos   : Vec2f     // world-space target (cursor / move target)
    // optional fields, present only if inputFlags requires them:
    abilityId   : uint8     // which ability slot, if ABILITY flag set
    sequence    : uint32    // client local sequence, used for de-dup
}
```

### 3.1 inputFlags bitfield

```
bit 0  MOVE         // movement intent toward targetPos
bit 1  ATTACK       // basic attack at targetPos
bit 2  ABILITY      // cast abilityId at targetPos
bit 3  STOP         // cancel current order
bit 4  HOLD         // hold position
bit 5  INTERACT     // interact with entity at targetPos
bits 6-15 reserved
```

Multiple flags can be set in the same packet (e.g. MOVE + ATTACK = attack-move).

### 3.2 Wire size

Typical packet: 8 (tickId) + 4 (playerId) + 2 (flags) + 8 (targetPos) + 1
(abilityId) + 4 (sequence) = **27 bytes** before framing. With redundancy of
the last 3 inputs in each packet, ≈ 81 bytes/packet, ≈ 1.6 kB/s upstream per
player. Well within budget for any modern connection.

### 3.3 Server-side validation

- Reject packets where `tickId > currentTick + N` (clock-ahead clients,
  N = 4 ticks = 200 ms tolerance).
- Inputs with `tickId < currentTick - K` are still applied at currentTick but
  flagged as late (K = 6 ticks = 300 ms grace before drop).
- `targetPos` is clamped to playable bounds before use.
- `abilityId` is checked against the player's known kit; invalid → drop.

## 4. Client-Side Prediction

### 4.1 Goal

Local player sees their own input take effect on the same render frame it
happened, not 1× RTT later.

### 4.2 Mechanism

- Client maintains a **local simulation** running the same deterministic
  step function as the server.
- Client keeps a **ring buffer of unacknowledged inputs**, keyed by `tickId`,
  bounded at 64 entries (≈ 3.2 s of history at 20 Hz).
- On every render frame:
  1. Sample input devices, accumulate intent.
  2. When a tick boundary is crossed (every 50 ms of game time), build an
     `InputPacket`, push to the unacknowledged-input ring, send to server.
  3. Apply that input to the local simulation.
  4. Step local simulation by `dt = 50 ms` per crossed boundary.
- Between tick boundaries, **render interpolates** entity positions:
  - Local player: rendered at predicted position (no smoothing — instant).
  - Remote entities: rendered at `lerp(snapshot[tickId-1], snapshot[tickId], alpha)`
    where `alpha` is the fractional time since last received snapshot. This
    introduces a fixed ~50 ms display lag for remote entities, which is the
    standard Quake / Overwatch approach and is invisible at 20 Hz tick.

### 4.3 Pseudocode

```
on_render_frame(realDt):
    accumulator += realDt
    while accumulator >= TICK_DT:        // 50 ms
        accumulator -= TICK_DT
        input = sample_input(currentClientTick)
        unackedInputs.push(input)
        send(input)
        local_simulate_one_tick(input)
        currentClientTick += 1
    render(localState, alpha = accumulator / TICK_DT)
```

## 5. Server Reconciliation (Rollback)

### 5.1 When a snapshot arrives

```
on_snapshot(snap):
    serverTick      = snap.tickId
    serverState     = decode(snap, baseline=lastAckedSnapshot)
    lastAckedSnapshot = serverState
    lastAckedTick   = max(lastAckedTick, snap.lastAckedInputTick)

    # Drop inputs the server has now confirmed.
    unackedInputs.drop_through(lastAckedTick)

    # Compare predicted state at serverTick with server state.
    predicted = predictedHistory[serverTick]      // ring of past predictions
    if not predicted: return                      // we lost the prediction, accept server

    drift = distance(predicted.localPlayer.pos, serverState.localPlayer.pos)

    if drift <= 0.5 meters:
        # Within tolerance: trust prediction, do nothing visible.
        return

    # Divergence: rollback + replay.
    localState = serverState                      // hard reset baseline
    for input in unackedInputs.in_order():
        local_simulate_one_tick(input)            // re-run from corrected base

    # Visual smoothing: do NOT snap the rendered position.
    # Remember the position the player WAS seeing, then lerp toward
    # the new predicted position over 200 ms.
    visualOffset      = oldRenderedPos - localState.localPlayer.pos
    visualOffsetTime  = 0
    VISUAL_OFFSET_DURATION = 0.200    // seconds
```

### 5.2 Visual smoothing during render

```
render(state, alpha):
    pos = state.localPlayer.pos
    if visualOffsetTime < VISUAL_OFFSET_DURATION:
        t = visualOffsetTime / VISUAL_OFFSET_DURATION
        pos += visualOffset * (1.0 - t)        // decays to zero
        visualOffsetTime += realDt
    draw(pos)
```

Net effect on a mismatch >0.5 m:

- Simulation state is corrected **immediately and exactly** (no compromise on
  hit detection, collisions, etc.).
- Camera/sprite **lerps** from the wrong predicted position to the right one
  over 200 ms — a smooth slide, not a teleport.

### 5.3 Why 0.5 m and 200 ms

- **0.5 m threshold:** below this, network jitter and floating-point variance
  produce false positives. Above this, the player can see the discrepancy in a
  single frame, so we must correct.
- **200 ms smoothing:** longer than one tick (50 ms) so it visibly smooths,
  shorter than human reaction (~250 ms) so the corrected state is fully
  authoritative before the next meaningful input.

## 6. Snapshot (State) Format

Sent server → client every tick.

### 6.1 Full snapshot (used for keyframes / new joiners / when delta base is lost)

```
SnapshotPacket {
    tickId              : uint64
    lastAckedInputTick  : uint64    // per-receiver
    isDelta             : bool      // false for full snapshot
    baselineTickId      : uint64    // 0 if full
    entityCount         : uint16
    entities            : Entity[entityCount]
}

Entity {
    entityId    : uint32
    typeId      : uint16          // archetype, drives field layout
    position    : Vec2f           // 8 bytes
    velocity    : Vec2f           // 8 bytes (optional, see flags)
    facing      : float16         // radians
    hp          : uint16          // current
    hpMax       : uint16
    cooldowns   : uint8[K]        // K ability slots, ticks remaining
    statusFlags : uint16          // stunned, rooted, invuln, etc.
    fieldMask   : uint16          // which optional fields are present
}
```

### 6.2 Delta snapshot (default, every tick)

A delta snapshot is encoded against the receiver's last *acknowledged* full or
delta snapshot. Three pieces:

```
DeltaSnapshot {
    tickId              : uint64
    baselineTickId      : uint64
    addedEntities       : Entity[]      // full entries
    removedEntityIds    : uint32[]
    changedEntities     : EntityDelta[]
}

EntityDelta {
    entityId    : uint32
    fieldMask   : uint16          // which fields changed since baseline
    // only fields whose bit is set in fieldMask follow, in fixed order
    // e.g. position uses 8 bytes only if FIELD_POS bit is set
}
```

Bandwidth notes:

- Idle entities (no movement, no HP/cooldown change) contribute **0 bytes** —
  they are not listed in `changedEntities`.
- Position is the dominant field for moving entities; quantize to fixed-point
  16 bits per axis (1 cm resolution within ±327 m) to halve its cost when the
  map fits.
- Per-tick payload at 50 active entities, ~10 moving: ≈ 200–400 bytes typical,
  ≈ 8 kB/s/client. Comfortable on any broadband link.

### 6.3 Acknowledgement

Each input packet's `tickId` doubles as an implicit ack of "I've seen
snapshots up to this tick". The server uses the most recent `tickId` from a
client as the baseline for the next delta encoding for that client. This
piggybacks acks onto inputs and avoids dedicated ack packets.

If a client falls too far behind (baseline >2 s old) the server forces a full
snapshot to resync rather than encoding an enormous delta.

## 7. Open Questions / Out of Scope

- **Lag compensation for hitscan attacks** (server-side time-rewind for
  hit validation) — to be specified in ARCH-2.
- **Cheat detection** beyond the obvious clamps in §3.3 — out of scope.
- **Variable tick rate / slow-motion** — not needed for v1, fixed 20 Hz.
- **Spectators** — receive snapshots only, no input loop, no prediction.

## 8. Acceptance Criteria Mapping

| Criterion | Section |
|---|---|
| Server tick rate: 20 Hz fixed (50 ms tick), client renders uncapped | §2.1 |
| Input packet: tickId (uint64), playerId, inputFlags (move/attack/ability), targetPos | §3 |
| Client prediction: apply inputs locally at render rate, interpolate to server state | §4 |
| Rollback: when server state diverges >0.5 m, lerp to corrected position over 200 ms | §5 |
| State snapshot format: all entity positions, HP, cooldowns per tick (delta-compressed) | §6 |
| Document is written to docs/architecture/server_tick_design.md and non-empty | this file |
