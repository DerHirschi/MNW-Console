# MNW Pilot-Station (CLI-GUI subproject)

Display/monitor layer for the MNW Ship Probe. Three deliverables:

1. **Live `masts` monitor** — an auto-refreshing mast/snorkel/periscope/towed display
   built on the existing `console.py` `masts` command.
2. **Pilot-Station instrument panel** (`pilot_station.py`) — a full control-room-style
   panel: navigation, masts/depths, sonar arrays, contacts, status strip.
3. **AI Enemy Overview** (`ai_overview.py`) — htop/nvtop-style tactical display of all
   AI units with range, heading, detected status, threat level.

## How it works

The probe (running inside the game) writes JSON state to a log dir; the display layer
only reads those files and renders. No network, no in-game changes.

| File | Role |
|------|------|
| `ship_state.json` | player state (nav, systems/masts, sonar_arrays, contacts, blackboard threats) |
| `ai_state.json` | per-AI-element state (position, heading, speed, assignment, contact count) |
| `ship_results.json` | command results including `detected` output (last-writer-wins) |
| `ship_orders.json` | outbound commands (display sends `detected` periodically) |
| `ship_probe_log.txt` | event log |

## Quickstart

```sh
# interactive console, live masts monitor against a running game
python3 console.py --game-root "<MNW install>" masts --watch

# pilot-station panel
python3 pilot_station.py --log-dir "<MNW>/Var/Scripts/Execute/_Source" --interval 3

# AI enemy overview (htop-style tactical display)
python3 ai_overview.py --log-dir "<MNW>/Var/Scripts/Execute/_Source" --interval 3
```

The game refreshes `ship_state.json` roughly every 30 s wall-clock, so a 3 s poll
interval is just a smooth re-render of the last frame.

## Layout

```
cli-gui/
├── AGENTS.md        engineering guide + three task briefs + data-access rules
├── README.md        this file
├── pilot_station.py # Task brief 2 (not yet implemented)
├── ai_tactical.py   # Task brief 3, realized as full curses TUI (see below)
└── tests/           headless renderer tests
```

## AI Tactical View (`ai_tactical.py`)

Task-brief-3 display ("AI Enemy Overview"), realized as an htop-style **curses TUI**
that merges every known AI data source into one table — including command-only hosts
(helo, Akula) that never appear in `ai_state.json`.

### What it shows

| Column | Source |
|--------|--------|
| ID/NAME/TYP | `ai_state.json`, ghost rows from probe-log `ns /N/ style=...` lines and optional `datalink_presence.json` |
| RANGE/BRG | `to_player_range_km/bearing`; for ghosts computed from `ai-state` probe lat/lon |
| SPD/HDG/DEP/EOT | element state + EOT short form (`Flank/Std/13/23/Stop`) |
| KTG | contact count; DET | `detected` result: `YES <range>` / `no` / `n/a` (command-only) / `?` |
| ASSIGNMENT | current assignment id+type, `*` when action-prep complete |

Panels (bordered): **OWN SHIP** (position/course/speed/depth, ordered helm/EOT/depth/
RPM, masts, snorkel, towed, damage, ammo), **AI CONTACTS** (the table, scrollable,
with range-window indicator in the title and a column header with units —
`RANGE km`, `SPD kt`, `DEP m`, `BRG°`/`HDG°`), **THREATS** (blackboard counts,
detected-by list, contacts-on-player) — or **DETAIL #id** (TAB) with weapons DB
match, ammo ratios, dipping sonar/rpm/throttle/altitude and per-element AI contacts.

At ≥ 100 columns OWN SHIP becomes a right-hand instrument column next to the table;
narrower terminals stack it above.

Speeds are shown in **knots** — the probe reports m/s (Unity convention); every
`kt`-labelled value goes through the `_kt()` conversion. The player's own element is
filtered from the AI table: `player.player_id` is authoritative; `player.id` /
`identity.id` only count when they agree (live missions showed `player.id` mirroring a
hostile element id, which would silently hide a real contact). Element 0 (contextless
host module) is never rendered or probed.

### Look & feel

Green phosphor base (dark terminal); semantic accent colors on top: red bold =
close threat/detected-you, yellow = medium range/caution, cyan = fresh orders,
magenta = weapon threats, blue = own-ship instrument values. Degrades to plain
mono emphasis with `--no-color`.

### Mast schematic (OWN SHIP frame)

Below the instrument lines the OWN SHIP box renders the sail as a 6-slot
rectangle; each mast is a bar that starts inside the hull and rises above the
sail by its Ausfahrlänge at a **fixed 5 m scale** (4 fill rows max). Bars sit
exactly on their slot centres; raised-but-unknown height shows only the hull
stub with a `?` label. The snorkel bar carries a small head square: green when
`snorkel_exposed` (head above the surface), dark blue while submerged. Under
the drawing: per-slot type abbreviations (SNK/RAD1/P1/P2/C1/C2) and heights in
metres (`-` = retracted), plus a snorkel readout line — state
(`down`/`up`/`up · exposed`, colour-matched to the head), head valve, intake
hole and intake volume, and a scale hint where width allows. The schema needs
≥25 columns and disappears gracefully below that. Same renderer runs in the
right-hand column layout, the stacked layout and `--json`/text mode.

### Refresh model

Input is decoupled from polling: keys react within ~150 ms; the data poll runs on its
own interval timer. **Real cadence (measured 2026-08-23):** the in-game `_random_tick_`
hook fires at roughly 0.1–0.5 Hz depending on mission state and focus — NOT the
~2.9/s quoted elsewhere for active gameplay. The probe's queue mode writes
`ship_state.json` after every section run, so own-ship sections refresh every few
ticks, but a full rotation including `ai` takes ~1–2 minutes: AI columns update about
once per minute. That is probe/game-side physics, not a TUI limit.

The TUI drives extra freshness through API commands (guarded against overlap):
`planes` (no-op state refresh when ship_state ts is older than 45 s), `detected`
(base cadence `--detect-interval`, min 10 s, PLUS an immediate re-scan whenever any
element's state signature changes: range/heading/speed/assignment/orders/prep/contact
count), rotating `asg <id>` and `ai-state <id>` probes (TTL-based, default 60 s;
command-only hosts included). Command-only hosts are discovered automatically via
periodic `ns-dump`s until a helo/sub namespace shows up in the probe log — first 3
tries at a short cadence, then relaxed to keep the orders queue clean.

**cmdid contract:** ids stay monotonic even when the probe empties the orders file.
The floor is `max(own history, highest cmdid ever answered in ship_results.json)`,
and a stagnation watchdog drops pendings older than 45 s and rebases onto the
results record — so a TUI restart cannot deadlock the channel against the running
probe's in-RAM `last_cmdid` anymore (probe-side pruning tracked in
`../ship-probe/BRIEF_orders_prune.md`). `--read-only` never writes `ship_orders.json`.

When the game stops handing ticks to the probe (measured 7–12 s holes), all data
freezes at once. The TUI then shows an amber `PROBE BUSY - no fresh data for Ns`
banner under the header until `ship_state` refreshes again (> 15 s age triggers it).
Probe-side mitigations (incremental writes while slicing, slim sonar contacts,
AI write dampening) are specified as task 3 in `BRIEF_orders_prune.md`; the
follow-up scheduling work (flush during slice jobs, due-first section
scheduling on a seconds basis) is specified in
`../ship-probe/BRIEF_scheduler_fairness.md`.

The `ai-state` action is **deployed live** (see `../ship-probe/AGENTS.md`);
helo/sub ghost rows fill in as the probes come back. If command-only hosts never
answer the discovery `ns-dump`, that is a probe-side issue — tracked as task 2 in
`../ship-probe/BRIEF_orders_prune.md`
(multi-host answer + deterministic element enumeration).

### DATALINK history panel

TAB opens the DETAIL box plus a `DATALINK` box underneath it — a transition-only
event journal of the AI datalink, newest line at the bottom (max ~12 rows):

- `ORDER` (cyan) — element received `_IncomingOrder` for a new assignment id
- `ADOPTED` (cyan) — `_CurrentAssignment` changed
- `DETECTED` / `DETCLEAR` (red / dim) — element gained or lost a track on you
- `ATTACK-OK` / `ATTACK-FAIL` (green / red) — manual ai-attack outcome (A/B)
- `GHOST+` / `GHOST-` (amber) — command-only host discovered or vanished

Key `l` cycles the filter all → selected element → all (`DATALINK #id` in the box
title). Built client-side from state diffs only (no extra probe commands, no C#
calls); keeps 500 events in RAM and resets on TUI restart; `--json` exports the
latest 200 as `frame.dl_history`. Probe-side message-level logging (`dl-log`)
is future work — see `BRIEF_datalink_history.md`.

### Usage

```sh
python3 ai_tactical.py --log-dir "<MNW>/Var/Scripts/Execute/_Source" [--interval 5]
python3 ai_tactical.py --remote 'masto@192.168.1.100:"/abs/log/dir"'   # SSH fetch
python3 ai_tactical.py --log-dir <dir> --json --read-only              # one frame as JSON
python3 ai_tactical.py --remote '...' --json --count 30                # NDJSON stream (1 frame/poll)
```

Keys: `q` quit · `↑/↓` select · TAB detail + DATALINK · `d` detect now · `e` ai-state probe ·
`a` ai-contacts now · `r` force refresh · `p` pause · `+/-` interval · `c` toggle color ·
`l` datalink filter.
`A` + `y` queues an ai-attack on the selected element (probe refuses when the
element has no track on the player); `B` + `y` fires blind (`allow_untracked`,
overrides the gate). The last attack's outcome shows as a banner for ~12 s.
Flags: `--count N`, `--no-color`, `--read-only`, `--detect-interval S` (min 10),
`--asg-ttl S`.

Tests: `tests/test_ai_tactical.py` (88 cases: parsers, merge incl. own-id filter and
ext-state fill, own-ship panel + side column, renderers incl. kt conversion,
narrow widths, NaN sanitization, strict-JSON round-trip, monotonic cmdid floor,
detect scheduling + event trigger, ns-dump bootstrap).

## AI Enemy Overview (brief spec)

## AI Enemy Overview

The AI overview (`ai_overview.py`) renders a compact tactical table of all AI units,
color-coded by threat level and detection status. It polls `ai_state.json` for element
data and sends `detected` commands to check which enemies have a track on the player.

### Example display

```
┌─ AI OVERVIEW ──────────────────────────────────────────────────────────────────┐
│ 3 AI units | Player: SSN-774 Virginia | Clock: 14:23:45 | Tension: High      │
├────┬──────────────┬─────┬───────┬──────┬──────┬─────┬─────┬──────┬────────────┤
│ ID │ Name         │Type │ Range │ Brg  │ Spd  │Hdg  │EOT  │Detct │ Assignment │
├────┼──────────────┼─────┼───────┼──────┼──────┼─────┼─────┼──────┼────────────┤
│ 13 │RUS DDG 052D  │ FFG │ 6.0km │ 306° │10.6kt│298° │Flank│  no  │ ASW #3     │
│ 16 │RUS FFG 054A  │ FFG │ 0.4km │ 139° │13.7kt│223° │Std  │2.1km │ ASW #5 ◄◄ │
│ 17 │RUS DDG 052D  │ DDG │ 8.2km │ 045° │12.1kt│042° │Flank│  no  │ Transit    │
├────┴──────────────┴─────┴───────┴──────┴──────┴─────┴─────┴──────┴────────────┤
│ DETECTED: #16 | THREATS: Merged=5 Susp=0 Torp=0 Miss=0 Air=0                 │
│ CONTACTS: none (disabled)                                                      │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Color coding

| Color | Condition |
|-------|-----------|
| RED bold | Range < 2 km OR detected player |
| YELLOW | Range < 5 km |
| GREEN | Range >= 5 km (transit) |
| CYAN | Element has fresh orders |
| DIM | Command-only host (no state) |

### Usage

```sh
python3 ai_overview.py --log-dir <dir> [--interval 3] [--count N] [--json] [--no-color]
```

- `--interval 3` — poll interval in seconds (default: 3)
- `--count N` — exit after N refreshes (for tests / automation)
- `--json` — dump one rendered frame as JSON (no ANSI, for tests)
- `--no-color` — disable ANSI colors (dumb terminals / CI)

### Data sources

- `ai_state.json` — all AI elements: position, heading, speed, range to player,
  bearing to player, contact count, assignment, EOT, action prep status
- `ship_results.json` — latest `detected` result: which elements have a track on
  the player, with contact range
- `ship_state.json → blackboard` — threat data (`_MergedContacts`, `_TorpedoThreat`,
  `_MissileThreat`, `_AircraftThreat`)
- `ship_state.json → contacts.tracks` — tracks on the player (may be disabled)

### Edge cases handled

- Zero AI elements → placeholder message
- Missing fields → `?` placeholder, no crash
- NaN/Infinity → sanitized via `_safe_num`
- Narrow terminal → columns truncated, numeric data preserved
- Disabled contacts → "no contacts (disabled)"
- No detected result yet → `?` in Detected column
- Stale detected result → `*` appended to values

## Data-format cheat sheet (player sub)

- `systems.mast_<id>_{type,status,height}` — ids 0–5: `Snorkel, Radar1, Photonics1,
  Photonics2, CommAntenna1, CommAntenna2`. `mast_ids_err` is EXPECTED
  (`GetAvailableMastIDs` needs 1 arg in the embedded interpreter); the probe falls back
  to probing ids directly.
- `systems.periscope_depth/surface_depth/standard_depth/max_operational_depth` — depth bands (m).
- `systems.snorkel_*`, `systems.towed_array` (`Extended`/`Retracted`).
- `navigation.lat_lon`, `heading/true_heading`, `speed/true_speed`, `depth`, `bottom_range`.
- `steering` — control surfaces (every tick): `forward_plane_angles` (live
  FlapAngle via public Hydrodynamics array), `ordered_eot/speed/heading/depth`,
  `bow_planes_retracted/forward_planes_locked/int_stern_planes_locked`, `tpk/stw`,
  `cavitation`, `scope`, depth-band constants. `stern_plane_angles`/`rudder_plane_angles`
  via Get*() getters only in the `planes` read-out. Great for a planes/controls gauge.
- `sonar_arrays.arrays[]` — real sonar data (4 arrays live); `contacts` section may be
  `{"count":0,"disabled":true}` when `read_contacts` is off.
- `ai_state.json` — per AI element: `id, name, category, country, lat_lon, heading,
  speed, to_player_range_km, to_player_bearing, contact_count, current_assignment,
  ordered_course/eot/depth, current_course/eot/depth, action_prep_complete`.
- `ship_results.json → detected` — which AI elements track the player: `HIT contact on
  player, range=NNN m` / `NO contact on player` / summary `DETECTED elements: <ids>`.

Control commands available in `console.py` (via `ship_orders.json`): `planes` read/view,
`planes fwd|stern|rudder ANGLE`, `planes bubble on|off` (CatchBubble/ReleaseBubble),
`planes autotrim on|off`, `planes bow RETRACT|EXTEND`, `planes lockfwd|lockint on|off`,
`helm` with full telegraph EOT set + `--env N`/`--snap`/`--bubble`/`--autotrim` flags,
`tanks` (read-only ballast/trim probe: MBTManager + TnCManager + Hydrostatics; writes
`tanks vent|flood|drain|blow|charge|blower|bank N` are state-changing), `env`
(SonarSim environment scan; `env ssp` reads the live SSP/TP/Analysis properties),
`detected` (scan AI elements for tracks on player), `dc` (damage control: bulkheads,
lights NAVSTAT codes).

Full key inventory + processing rules (sanitize NaN/Infinity, never fabricate `None`,
`disabled` handling): see `AGENTS.md`.

## Tests

```sh
python3 -m unittest discover -s cli-gui/tests -q
```

## See also

- Parent repo `../AGENTS.md` — probe/deploy/game-side engineering knowledge.
- `../README.md` — full tool usage.
