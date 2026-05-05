# WC3-Parity Gameplay Systems Contract

**Task:** ARCH-5
**Status:** Design
**Sprint:** 2533

## 1. Overview

This document specifies the gameplay systems required to reach Warcraft III
("WC3") feature parity for the RTS layer of the project. It is the contract that
client, server, and AI subsystems agree on. All numeric tables here are the
authoritative defaults; balancing tweaks must edit this document and bump the
data version.

Design pillars:

- **Server-authoritative.** All gameplay state mutations happen on the server.
  The client only renders and submits commands; see `server_tick_design.md`.
- **Data-driven.** Heroes, abilities, items, units, and buildings are defined
  as data tables loaded at match start. Logic is generic; numbers live in data.
- **Deterministic.** Given an identical command stream and seed, the simulation
  is reproducible. RNG is sampled from a per-match seeded stream.
- **Geohash world.** Resources, fog state, and unit positions are anchored to
  the geohash world grid (see `geo_coordinate_system.md`).

## 2. Hero System

### 2.1 Levels and XP

Heroes have **5 levels** (1 → 5). XP is earned by killing units, destroying
buildings, and completing match objectives. The XP threshold is the cumulative
XP required to reach the *start* of that level.

| Level | XP Threshold (cumulative) | XP To Next |
|-------|---------------------------|------------|
| 1     | 0                         | 200        |
| 2     | 200                       | 400        |
| 3     | 600                       | 700        |
| 4     | 1300                      | 1100       |
| 5     | 2400                      | —          |

XP awards (default):

- Kill creep tier 1: 25 XP
- Kill creep tier 2: 60 XP
- Kill enemy unit: 40 XP
- Kill enemy hero: 100 XP × victim level
- Destroy enemy building: 75 XP

XP is shared between heroes within a 1200-unit radius of the kill site.

### 2.2 Stat scaling

Each hero has three primary stats: **STR** (Strength), **AGI** (Agility),
**INT** (Intelligence). One stat is the hero's *primary* stat and contributes
to their main damage type.

| Stat | Per-Point Effect                                          |
|------|------------------------------------------------------------|
| STR  | +25 max HP, +0.05 HP/sec regen                             |
| AGI  | +1 attack speed %, +0.30 armor                             |
| INT  | +15 max mana, +0.05 mana/sec regen                         |

If the primary stat is `P`, base attack damage adds `+1 damage per point of P`.

Per-level stat growth is hero-class specific. Default growth table:

| Hero Class | STR/lvl | AGI/lvl | INT/lvl | Primary | Base HP | Base Mana | Base Damage |
|------------|---------|---------|---------|---------|---------|-----------|-------------|
| Warrior    | +3.0    | +1.5    | +1.0    | STR     | 600     | 200       | 24-30       |
| Ranger     | +1.5    | +3.0    | +1.5    | AGI     | 500     | 250       | 20-26       |
| Mage       | +1.0    | +1.5    | +3.0    | INT     | 450     | 350       | 18-24       |

Stat values at level N = base + (N - 1) × per-level growth.

### 2.3 Abilities (per hero)

Each hero has **3 abilities**: 2 active + 1 passive.

- **Active 1:** unlockable at level 1, ranks 1-3 at hero levels 1/3/5.
- **Active 2:** unlockable at level 1, ranks 1-3 at hero levels 1/3/5.
- **Passive:** unlockable at level 2, ranks 1-3 at hero levels 2/4/5.

The hero gains 1 ability point on level-up, spent on a single rank in any
unlocked ability. Ability levels do not auto-grant; the player chooses.

## 3. Ability System

### 3.1 Targeting types

| Targeting   | Description                                              |
|-------------|----------------------------------------------------------|
| `self`      | Targets the caster. No selection required.               |
| `unit`      | Targets a single unit (friend or foe per ability rules). |
| `point`     | Targets a world point (x,y,z).                           |
| `aoe`       | Targets a point with a radius `r`; affects all valid units within `r`. |

For `unit` and `aoe`, allowed-target filters apply (e.g. `enemy`, `ally`,
`organic`, `mechanical`, `air`, `ground`, `hero`, `non-hero`).

### 3.2 Ability schema

Every ability declares:

| Field          | Type            | Description                              |
|----------------|-----------------|------------------------------------------|
| `id`           | string          | Stable identifier.                       |
| `name`         | string          | Display name.                            |
| `targeting`    | enum            | One of `self`/`unit`/`point`/`aoe`.      |
| `aoe_radius`   | float           | World units; required if `aoe`.          |
| `range`        | float           | Max cast range (0 = self only).          |
| `cast_time`    | float (sec)     | Channel/wind-up before effect resolves.  |
| `cooldown`     | float (sec)     | Time before ability can be cast again.   |
| `mana_cost`    | int             | Mana subtracted on cast resolve.         |
| `effect`       | EffectSpec      | Damage/heal/buff/summon spec.            |
| `is_passive`   | bool            | If true, no cast; effect is always-on.   |

Cast resolution: on cast, mana is reserved and the cast timer starts. If the
caster moves, dies, or is silenced before cast completes, mana is refunded
and the cooldown is **not** triggered. On successful resolve, mana is
consumed and cooldown begins.

### 3.3 Example ability rows

| ID            | Targeting | Range | AoE | Cast | CD  | Mana | Effect                          |
|---------------|-----------|-------|-----|------|-----|------|---------------------------------|
| `fireball`    | point     | 700   | 200 | 0.40 | 6.0 | 75   | 100/175/250 magic dmg in AoE    |
| `heal_self`   | self      | 0     | —   | 0.20 | 12  | 50   | Restore 200/300/400 HP          |
| `aura_armor`  | self      | 0     | —   | 0    | 0   | 0    | Passive: +1/+2/+3 armor 900 rad |

## 4. Item System

### 4.1 Inventory

Each hero has **6 inventory slots** (slots 0-5). Items occupy exactly one slot.
Stackable consumables stack up to a per-item `max_stack` (default 1, charges
items default 5).

Drop rules: items can be dropped at the hero's current position. Dropped items
persist on the ground until picked up or the match ends.

### 4.2 Item types

| Type         | Slot Behavior              | Use                                          |
|--------------|----------------------------|----------------------------------------------|
| `weapon`     | Permanent stat bonus       | One weapon item active at a time (no dual). |
| `armor`      | Permanent stat bonus       | Multiple armor items stack additively.       |
| `consumable` | Charged; depletes on use   | Active activation (potion, scroll).          |
| `artifact`   | Permanent + active ability | Grants a bonus ability while held.           |

### 4.3 Stat bonuses

Items contribute additively to hero effective stats. Bonus fields (any subset):

- `+hp`, `+mana`
- `+str`, `+agi`, `+int`
- `+armor`
- `+damage`
- `+attack_speed_pct`
- `+movement_speed_pct`
- `+hp_regen`, `+mana_regen`
- `+ability:<id>` (artifact only — grants the ability while held)

Effective stat = base + level growth + Σ item bonuses + Σ buff modifiers.

### 4.4 Example item rows

| ID                | Type       | Bonuses                          | Charges |
|-------------------|------------|----------------------------------|---------|
| `iron_sword`      | weapon     | +damage 8                        | —       |
| `chain_mail`      | armor      | +armor 4, +hp 100                | —       |
| `health_potion`   | consumable | +hp 250 instant                  | 5       |
| `crown_of_kings`  | artifact   | +str 6, +ability:`royal_command` | —       |

## 5. Fog of War

### 5.1 Cell states

Fog is tracked per **vision cell** (geohash precision configured per-match,
default = world grid level 22). Each cell has one of **3 states** per player:

| State              | Render               | Server visibility                |
|--------------------|----------------------|----------------------------------|
| `never_seen`       | Black (full fog)     | No entities revealed.            |
| `seen_not_visible` | Greyed terrain only  | Static terrain + last-seen building snapshot revealed. Units NOT revealed. |
| `visible`          | Full color, live     | All entities revealed live.      |

State transitions:

- `never_seen → visible` when any owned unit's vision radius covers the cell.
- `visible → seen_not_visible` when no owned unit has vision on that cell this
  tick (after a 0.5s grace period to avoid flicker).
- `seen_not_visible → visible` on re-entering vision.
- `seen_not_visible → never_seen` is **not** a valid transition (memory is
  permanent for the match).

### 5.2 Vision sources

- Units have `sight_radius_day` and `sight_radius_night`.
- Buildings have a fixed `sight_radius`.
- Wards / scout abilities place a temporary vision source.
- Vision is blocked by `cliff_high` terrain and `tree` blockers (LoS check).

### 5.3 Server behavior

The server computes per-player vision once per tick. Snapshot deltas to the
client only include entities whose owning cell is `visible` for that player.
Building snapshots in `seen_not_visible` cells are sent with a `last_seen_tick`
timestamp and are not updated until the cell becomes `visible` again.

## 6. Unit Commands

The command set is closed; clients may only submit one of the following:

| Command          | Params                          | Behavior                                                                 |
|------------------|---------------------------------|--------------------------------------------------------------------------|
| `Move`           | target_point                    | Path to point. Ignore enemies. Stop on arrival.                          |
| `AttackMove`     | target_point                    | Path to point; auto-acquire and attack any enemy in `acquisition_range` along the way. |
| `AttackUnit`     | target_unit_id                  | Path into `attack_range` of unit, then attack until dead/out of range.   |
| `Patrol`         | target_point                    | Treat current pos and target as endpoints; AttackMove between them forever. |
| `Stop`           | —                               | Cancel current order; remain in place; auto-acquire allowed.             |
| `Hold`           | —                               | Remain in place; do not chase; attack in-range enemies only.             |
| `UseAbility`     | ability_id, target (per type)   | Cast ability per Section 3.                                              |
| `GatherResource` | target_node_id                  | Path to node, gather per Section 7. Workers only.                        |

Command queueing: holding the queue modifier on the client appends; otherwise
the new command replaces the active order. Server validates command legality
(unit owns ability, target exists, range/path reachable).

## 7. Resource System

Three resources, all integer, no decimals:

### 7.1 Gold

- **Sources:** gold mines (depletable static nodes) and **geohash cells**
  flagged `gold_rich` in the world data (yield over time when worked).
- **Mine extraction:** worker enters mine, removed from world for `gather_time`,
  emerges with `gold_per_trip` (default 10), returns to nearest town hall to
  deposit.
- **Geohash cell yield:** a worker assigned to a `gold_rich` cell deposits
  `cell_gold_per_min` gold per minute while present.
- **Cap:** per-player gold cap = 1,000,000.

### 7.2 Lumber

- **Sources:** trees, and biome-flagged `forest` cells.
- **Trees** have `lumber_per_tree` (default 50) and are destroyed when depleted.
- **Forest biome cells** regenerate trees at `regen_rate` per minute up to the
  cell's tree cap.
- Worker chops tree → carries `lumber_per_trip` (default 5) → returns to drop
  point.

### 7.3 Food / Population

- **Food cap** is the soft population limit. Trained units consume `food_cost`
  food. New training is disallowed if `current_food + cost > food_cap`.
- **Sources of food cap:** farms (+10), town hall tier 1/2/3 (+10/+15/+20).
- **Hard cap:** 100 food regardless of farms beyond that.
- Food is **not** spent like gold/lumber; it is reserved while the unit lives
  and freed on death.

## 8. Building System

Buildings are constructed by workers, occupy a footprint on the world grid,
have HP and armor, and run production tables.

### 8.1 Building list

| ID           | Role                | HP   | Armor | Build Time | Cost (G/L) | Footprint |
|--------------|---------------------|------|-------|------------|------------|-----------|
| `barracks`   | Train infantry      | 1500 | 5     | 60s        | 200/60     | 4x4       |
| `farm`       | +10 food cap        | 500  | 0     | 20s        | 80/20      | 2x2       |
| `tower`      | Static defense      | 800  | 5     | 30s        | 100/30     | 2x2       |
| `mine`       | Gold extraction hub | 1500 | 5     | 0s (placed on node) | 0/0 | 4x4 |
| `blacksmith` | Research upgrades   | 1200 | 5     | 60s        | 140/60     | 4x4       |
| `altar`      | Train heroes (win condition) | 1800 | 5 | 60s | 180/50    | 4x4       |

### 8.2 Production tables

Each producer has a queue (max length 7). A unit/upgrade in queue advances
when prior items finish. Cancellation refunds 100% of paid cost.

**Barracks** produces:

| Unit ID    | Cost (G/L) | Food | Build Time | HP   | DPS  | Notes                |
|------------|------------|------|------------|------|------|----------------------|
| `footman`  | 135/0      | 2    | 20s        | 420  | 12.5 | Melee, defend ability|
| `archer`   | 130/10     | 2    | 19s        | 245  | 16.4 | Ranged 500           |
| `knight`   | 245/60     | 4    | 45s        | 835  | 35.7 | Melee, requires T2   |

**Tower** does not produce; it attacks (DPS 26, range 700, no upgrades base).

**Mine** does not produce; it is the deposit point and gather hub.

**Blacksmith** produces upgrades:

| Upgrade ID         | Cost (G/L) | Time | Effect                         |
|--------------------|------------|------|--------------------------------|
| `melee_weapons_1`  | 100/100    | 60s  | +1 damage to melee units       |
| `armor_1`          | 100/100    | 60s  | +2 armor to all infantry       |
| `ranged_weapons_1` | 125/75     | 60s  | +1 damage to ranged units      |

**Altar of Heroes** produces heroes (one of each class per match):

| Hero ID    | Cost (G/L) | Food | Build Time |
|------------|------------|------|------------|
| `warrior`  | 425/100    | 5    | 55s        |
| `ranger`   | 425/100    | 5    | 55s        |
| `mage`     | 425/100    | 5    | 55s        |

**Farm** does not produce; it grants +10 food cap on completion.

### 8.3 Tech tiers

- **Tier 1 (T1):** default; `barracks`, `farm`, `tower`, `mine`, `altar`.
- **Tier 2 (T2):** unlocked by upgrading town hall (cost 320G/210L, 60s).
  Enables `blacksmith`, `knight`.
- **Tier 3 (T3):** unlocked by second town hall upgrade (cost 500G/300L, 90s).
  Enables tier-2 upgrades and tier-2 hero abilities.

## 9. Victory Condition

### 9.1 Primary objective

**A player loses when their `altar` building is destroyed.**
The last surviving player (or team) wins.

If an altar is destroyed but the player has builders, they may rebuild within
a 30-second grace window; if no altar exists at the end of the window, the
player is eliminated.

### 9.2 Match timer

Maximum match length: **60 minutes** of in-game time.

If the timer expires with multiple players still alive:

1. Compute each player's score = `0.5 × buildings_destroyed_value
   + 0.3 × units_killed_value + 0.2 × resources_gathered`.
2. Highest score wins. Ties resolved by most altars destroyed, then most
   buildings standing, then by player slot order.

### 9.3 Surrender

A player may surrender at any time (`/surrender`). This counts as their altar
being destroyed for ranking purposes.

## 10. Data Versioning

This document corresponds to **gameplay data version 1.0.0**. Any change to
numeric tables (XP thresholds, costs, stats, timers) must:

1. Update the affected table here.
2. Bump the version (`MAJOR.MINOR.PATCH`): MAJOR for breaking schema changes,
   MINOR for new entities, PATCH for balance.
3. Update the data-table fixtures in `engine/data/` to match.

## 11. Open questions

- Day/night cycle: is sight radius modulation in scope for v1? (Currently
  modeled as `sight_radius_day`/`sight_radius_night`, but day/night driver
  is undefined.)
- Neutral creep camps: design covered conceptually under XP rewards but
  spawn tables are deferred to a separate doc (ARCH-?).
- Ladder/MMR scoring beyond a single match is out of scope here.
