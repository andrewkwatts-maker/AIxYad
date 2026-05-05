# Offline Simulation Spec

**Task:** ARCH-4
**Status:** Design
**Sprint:** 2529

## 1. Overview

This document specifies how the world continues to evolve while a player is
disconnected. The core principle is that **the server's simulation never
pauses on a player's behalf**: production, construction, and combat continue
to apply to a player's holdings whether they are online, asleep, or offline
for days. When the player reconnects, their client is brought back to the
authoritative present without expensive recomputation.

Design pillars:

- **Server is always authoritative.** No "offline mode" runs on the client;
  the client only ever displays a snapshot of the live server state.
- **Real-time tick is the source of truth.** Production queues, construction
  timers, and combat events advance against wall-clock time, not session
  time.
- **Catch-up is reconstruction, not re-simulation.** When a player
  reconnects, the server replays only the per-player accounting (resources
  produced, build queue completions, attack outcomes) up to a hard horizon —
  it does not re-step the global ECS for each absent player.
- **Caps and horizons prevent abuse.** Offline accumulation is bounded so a
  player cannot leave for a year and return to an unbreakable economy.
- **No safety from absence.** Being offline grants no shield, no immunity,
  and no pause. A player who logs off has the same vulnerability surface as
  one who is online but idle.

This spec composes with the live server tick (ARCH-1, `server_tick_design.md`)
and the world partition model (`world_partition.md`).

## 2. Continuous Server Simulation

### 2.1 What "always on" means

The authoritative game server runs a fixed tick loop continuously. Player
sessions attach and detach from this loop; they do not start or stop it.

| Subsystem | Runs while player offline? | Notes |
|---|---|---|
| Production queues (resources) | **Yes** | Buildings produce against wall clock. |
| Production queues (units) | **Yes** | Barracks, factories complete units. |
| Construction queues | **Yes** | Building upgrades / new builds finish. |
| Research queues | **Yes** | Tech unlocks complete. |
| Incoming attacks | **Yes** | Enemy armies resolve combat at ETA. |
| Outgoing attacks | **Yes** | Player's own dispatched armies resolve. |
| Resource cap checks | **Yes** | Storage fills; overflow is discarded (see §4). |
| Player input | **No** | No new orders can be issued; queued orders only. |
| Trade / chat | **No** | Sessions only. |

### 2.2 Tick model for offline players

Two-tier model:

- **Hot tier (online players):** simulated at full server tick rate
  (20 Hz, see ARCH-1). Every entity in their visible region is stepped.
- **Cold tier (offline / unloaded players):** stepped on a coarser
  **economy tick** (default 1 Hz). The economy tick advances only the
  bookkeeping subsystems listed in §2.1: it does not step physics,
  pathing, or visual state. There is no reason to advance the position of
  a unit standing in a barracks queue at 20 Hz.

A player's holdings move from hot to cold tier:

- Immediately on session disconnect.
- After a configurable grace period (default 60 s) on idle disconnect
  (network drop) so a brief reconnect avoids a tier transition.

When the player reconnects, holdings are promoted back to hot tier as part of
the catch-up flow (§3).

### 2.3 Determinism and time source

- The economy tick reads `server_now()` — a monotonic UTC clock — and
  computes elapsed real time since the last update of each entity. It does
  **not** count "ticks since the player logged in".
- Each productive entity stores `lastUpdateTick : uint64` (in economy-tick
  units) and `lastUpdateRealTime : int64` (UTC ms). On every economy tick
  it computes `dt = clamp(server_now() - lastUpdateRealTime, 0, MAX_DT)`
  and applies production for that interval.
- `MAX_DT` is bounded per-pass to keep tick latency predictable (default
  60 s). If a process restart caused a longer gap, multiple economy ticks
  apply in sequence; this is rare and controlled.

### 2.4 What does *not* run while offline

Explicitly out of scope for the offline tick, to keep cost finite:

- Per-unit pathing for stationed garrisons.
- Animation, particle, audio state.
- Vision / fog-of-war updates from the offline player's perspective.
- Client-only interpolation buffers.

Anything in this list is purely a presentation concern and is reconstructed
when the client reconnects.

## 3. Catch-Up Simulation on Reconnect

### 3.1 Goal

When a player reconnects after `T` seconds offline, the server must hand
their client an up-to-date snapshot reflecting everything that should have
happened during `T`. The client must not see "yesterday's state" for any
amount of time visible to the user.

### 3.2 Horizon

```
CATCHUP_HORIZON = 72 hours          // hard cap on simulated elapsed time
```

If the player has been offline longer than 72 hours, the server simulates
**only the most recent 72 hours** of effects. Anything earlier is collapsed:
production beyond the horizon is treated as if it never happened (subject to
the resource cap in §4, which is tighter still). This bounds reconnect cost
and prevents pathological "logged off for a year" scenarios from spiking the
server.

Rationale for 72 h:
- Long enough that a weekend away feels rewarding.
- Short enough that a one-week absence does not hand a player a runaway
  economy advantage over active competitors.
- Aligns with the resource-cap window of 24 h (§4): even at the cap, a
  72 h window leaves room for queued unit completions and construction
  finishes that are not subject to the 24 h cap.

### 3.3 Reconstruction algorithm

Catch-up is **per-entity reconstruction**, not global re-simulation.

```
on_player_reconnect(playerId):
    nowUtc      = server_now()
    elapsed     = min(nowUtc - player.lastSeenUtc, CATCHUP_HORIZON)
    fromUtc     = nowUtc - elapsed

    # 1. Resource production: closed-form integration.
    for building in player.productionBuildings:
        produce_resources_closed_form(building, fromUtc, nowUtc)

    # 2. Unit production queues: pop completions whose finishTime <= nowUtc.
    for queue in player.unitQueues:
        while queue.head and queue.head.finishTime <= nowUtc:
            unit = queue.pop()
            spawn_unit_at(building.location, unit)
            push_event(playerId, UnitCompletedEvent(unit, queue.head.finishTime))

    # 3. Construction queues: same pattern.
    for build in player.constructionQueue:
        if build.finishTime <= nowUtc:
            apply_construction_complete(build)
            push_event(playerId, BuildingCompletedEvent(build, build.finishTime))

    # 4. Research queues: same pattern.
    for r in player.researchQueue:
        if r.finishTime <= nowUtc:
            apply_research_complete(r)
            push_event(playerId, ResearchCompletedEvent(r, r.finishTime))

    # 5. Combat: already resolved on the live tick at the actual ETA.
    #    Just collect the events the player missed:
    events += combat_event_log.since(playerId, fromUtc)

    # 6. Promote to hot tier; send full snapshot + event log.
    promote_to_hot_tier(playerId)
    send_snapshot(playerId, build_snapshot(playerId))
    send_event_log(playerId, events)
```

Total cost is `O(buildings + queue_entries + events)` per player, independent
of how long they were offline. The 72 h horizon caps the event-log size.

### 3.4 Closed-form resource production

Resource buildings produce at a rate `r` (units / second) up to a cap `C`
(see §4). For a single building offline from `t0` to `t1`:

```
gross    = r * (t1 - t0)
stored0  = building.storedAtT0
stored1  = min(stored0 + gross, C)
delta    = stored1 - stored0          // actually credited to the player
```

This is a closed-form integral; no per-tick stepping is needed for the
common case. If production rate changes mid-window (e.g. a building was
upgraded by an *online* sibling — irrelevant since this player is alone
during their offline window — or a buff expired), the window is split at
each rate-change event and the integral is summed piecewise. Rate-change
events come from the server's authoritative event log; there are typically
zero or one per offline window per building.

### 3.5 Combat during offline window

Combat is **never** deferred to reconnect. Incoming and outgoing attacks
resolve on the live tick at their scheduled ETA, even if no participant is
online. Results are written to the persistent combat event log keyed by
`playerId` and `utcTimestamp`. On reconnect (§3.3 step 5) the player simply
receives the log entries; the outcomes are already real.

This is what makes "no shield mechanic" workable as a design (see §6) —
attackers do not get penalized for hitting an offline player, and the
offline player does not get to "review and counter" pre-resolution.

## 4. Resource Cap

### 4.1 Rule

```
OFFLINE_PRODUCTION_CAP = 24 hours    // of nominal production per resource
```

For any resource type `R` and any building producing `R`, the maximum amount
that can accumulate **offline** is `24 h × productionRate(building, R)`. Any
production beyond this cap during the offline window is **discarded** (not
stored, not credited, not refundable).

The cap applies per-building, then per-resource-type the building feeds
into the player's stockpile. It is independent of the player's storage
buildings' capacity:

- **Storage capacity** (existing online mechanic) caps the *stockpile*.
- **Offline production cap** caps the *gain during one offline window*.

If both apply, the smaller wins.

### 4.2 Worked example

A lumber mill produces 100 wood/hour. Player logs off for 50 h.

- Naive gross: `100 * 50 = 5000 wood`.
- After offline cap: `100 * 24 = 2400 wood` credited from this mill.
- After storage cap (e.g. warehouse holds 1500 wood, currently 800 used):
  `min(2400, 1500 - 800) = 700 wood` actually stored. Remaining 1700 is
  discarded.

The cap is intentionally per-resource-source so a player with many mills
still benefits from breadth, but cannot stockpile from a single mill beyond
24 h regardless of horizon.

### 4.3 Why 24 h vs the 72 h catch-up horizon

The two windows are deliberately different:

- **24 h resource cap** — economy balance. Encourages daily logins without
  penalizing weekenders, while preventing infinite stockpile farming.
- **72 h catch-up horizon** — server cost bound. Caps the work the server
  must do per reconnect. Construction and unit-queue completions are not
  rate-limited by the 24 h cap, only by what was actually queued before
  logoff (a finite list).

A player offline for 60 h thus sees:

- Resources: capped at 24 h of production.
- Unit queue: every unit whose `finishTime <= now()` has completed (could
  be all of them).
- Construction queue: same.
- Combat events: full log up to 72 h.

### 4.4 Communicating the cap

The client UI surfaces the cap so players can plan logoff timing:

- Resource tooltip: "X / Y stored — offline gain capped at 24 h
  (≈ Z more before cap)".
- Player-settings page documents the rule explicitly.

The server is the single source of truth; the client never computes the
cap independently for purposes of state, only for display estimates.

## 5. Construction Continues Offline

### 5.1 Behavior

Construction and upgrade queues are **fire-and-forget timers**. When the
player issues a build order while online, the server records:

```
ConstructionJob {
    jobId       : uint64
    playerId    : uint32
    targetTile  : TileCoord
    blueprint   : BlueprintId
    startedUtc  : int64
    finishUtc   : int64
    cost        : ResourceBundle    // already deducted at queue time
}
```

The economy tick (§2.2) checks `finishUtc <= server_now()` for every active
job in the cold tier. When true:

1. The building is materialized in the world (placed, hp = full, ready).
2. The job is removed from the queue.
3. A `BuildingCompletedEvent` is appended to the player's event log.
4. **A push notification is dispatched** (§5.2).

This happens whether or not the player is online. There is no
"waiting for the player to confirm" step.

### 5.2 Push notification on completion

```
PushNotification {
    playerId       : uint32
    type           : "BUILDING_COMPLETE"
    title          : "Construction complete"
    body           : "Your <BlueprintName> at <Coord> is ready."
    payload        : { jobId, blueprintId, tileCoord, finishUtc }
    dedupeKey      : "build:<jobId>"   // idempotent; never sent twice
}
```

Delivery path:

- Server enqueues the notification on the player's notification stream.
- A background dispatcher pushes via the platform's push provider
  (FCM / APNs / web push), gated by the player's notification preferences.
- If the player is online at completion time, the notification is also
  delivered as an in-game toast through the live session and the push is
  suppressed (`dedupeKey`).

Failure modes:

- Push provider down → notification stays queued and is retried with
  exponential backoff for up to 24 h, then dropped (the in-game event log
  is permanent and remains the source of truth).
- Player has push disabled → server still records the event; the client
  shows it on next connect.

Same notification flow applies, with different `type`, to:

- Unit production complete (batched: at most one push per minute per
  player, summarizing recent completions).
- Research complete.
- Combat resolved (§6.3).

### 5.3 Resource refund / cancel semantics

Cancellation is an online-only action. Once offline, queued jobs run to
completion. This keeps the offline simulation a pure forward function with
no decision points.

## 6. Attack Vulnerability While Offline

### 6.1 No shield, ever

There is **no offline shield, no peace timer, no protection bubble** for
established players. A player who logs off remains a valid target for
attack from the moment of disconnect onward, identically to an online but
idle player.

Rationale:

- Offline shields create exploitable scheduling: attack-then-logoff cycles
  to launder offence into invulnerability.
- The 24 h resource cap (§4) already limits how much offline players can
  stockpile, so an attacker raiding a long-absent player gets bounded loot
  — there is no incentive to "farm absent players" beyond the cap either.
- Symmetry: an offline player's outgoing attacks also resolve on schedule
  (§3.5). Cost and benefit of being absent are matched.

The single, narrow exception is **new-player onboarding immunity**: brand
new accounts have a one-time peace window (default 24 h of *online* play
or 72 h of wall-clock time, whichever expires first). This is a tutorial
ramp, not an offline shield, and is out of scope for this document.

### 6.2 Combat resolution while offline

When an attacker (online) launches an attack against an offline target:

1. Order is queued with `etaUtc = launchUtc + travelTime`.
2. At `etaUtc`, the live server tick resolves combat using the target's
   *current authoritative state* (garrison, walls, buffs, all simulated
   in the cold tier).
3. Casualties, resource theft, building damage are applied immediately to
   the target's persistent state.
4. Both attacker and target receive a `CombatResolvedEvent` in their event
   logs.
5. The offline target is sent a push notification:
   `"Your settlement at <Coord> was attacked by <Attacker>. Losses: …"`.

The target cannot intervene. Garrison composition, defensive structures,
and any pre-set defensive orders (e.g. "withdraw to keep on attack") run
exactly as configured at logoff.

### 6.3 Resource theft and the offline cap

Stolen resources come out of the *current stockpile*, not out of the
offline-cap accrual. Sequence on a raid against an offline player:

```
target_stockpile = target.resources.wood          // current value, post-§4 cap
stolen           = min(attacker.carryCapacity, target_stockpile * RAID_FRACTION)
target.resources.wood -= stolen
attacker.resources.wood += stolen   // applied at attacker's home on return
```

This means an offline player's "safe" stockpile is whatever they had when
they logged off, plus offline-capped accrual, minus raid losses. There is
no protected reserve.

### 6.4 Defensive automation

To make offline play survivable rather than punitive, the design assumes
(specified elsewhere):

- Walls / static defenses that fire automatically.
- Configurable defensive stance per army (hold, retreat, sortie).
- Alliance reinforcements that can be parked at a player's settlement
  before logoff and fight under the host's defensive AI.

These are *systems*, not shields; they require investment and good play.

## 7. Persistence and Crash Recovery

### 7.1 Durability of the cold tier

All cold-tier state is fully persisted to the same authoritative store as
hot-tier state. There is no in-memory-only fast path that would lose
production on server restart.

Specifically the following are durable:

- `lastUpdateRealTime` per productive entity.
- All queue entries with their absolute `finishUtc`.
- The combat event log, append-only, indexed by `(playerId, utcTimestamp)`.
- Push-notification outbox with `dedupeKey`.

### 7.2 Restart semantics

After a server restart, the economy tick on first run computes elapsed
real time per entity (clamped per §2.3) and applies production. From the
player's perspective a restart is indistinguishable from being offline
during that interval — the offline simulation rules apply equally to
"player offline" and "server offline".

### 7.3 Time-skew protection

`server_now()` is monotonic. If the underlying OS clock jumps backward
(NTP correction), the economy tick treats the delta as zero rather than
negative. Forward jumps are clamped to `MAX_DT` per pass (§2.3) so a wall
clock fix cannot accidentally credit huge production in a single tick.

## 8. Configuration Constants Summary

| Constant | Default | Section |
|---|---|---|
| `ECONOMY_TICK_RATE` | 1 Hz | §2.2 |
| `HOT_TO_COLD_GRACE` | 60 s | §2.2 |
| `MAX_DT` (per economy-tick pass) | 60 s | §2.3 |
| `CATCHUP_HORIZON` | 72 h | §3.2 |
| `OFFLINE_PRODUCTION_CAP` | 24 h × rate | §4.1 |
| `PUSH_RETRY_WINDOW` | 24 h | §5.2 |
| `UNIT_PUSH_BATCH_INTERVAL` | 60 s | §5.2 |
| `RAID_FRACTION` | per balance design | §6.3 |

All constants are server-side and hot-reloadable; the client never assumes
a value.

## 9. Out of Scope

- Specific economy balance numbers (production rates, costs).
- Push provider integration details (FCM/APNs credentials, delivery SLAs).
- New-player onboarding immunity rules — referenced in §6.1, specced
  separately.
- Alliance reinforcement mechanics — referenced in §6.4, specced
  separately.
- Lag compensation for live combat — see ARCH-2.
- Persistence schema / database choice — see persistence design doc.

## 10. Acceptance Criteria Mapping

| Criterion | Section |
|---|---|
| Server-side simulation: production queues tick at real time even with no player connected | §2.1, §2.2, §2.3 |
| Catch-up simulation: on player reconnect, fast-simulate elapsed ticks (max 72h catchup) | §3.2, §3.3, §3.4 |
| Resource cap: offline accumulation limited to 24h of production to prevent overflow | §4.1, §4.2, §4.3 |
| Building construction: continues offline, notifies player via push notification on complete | §5.1, §5.2 |
| Attack vulnerability: buildings can be attacked while owner offline (no shield mechanic) | §6.1, §6.2, §6.3 |
| Document written and non-empty | this file |
