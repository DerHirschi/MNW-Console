# Agent Guide — MNW CLI-GUI Subproject ("Pilot-Station")

Subproject of the MNW Ship Probe. Delivers the **display/monitor layer** on top of the
probe's JSON file protocol: a live `masts` monitor in the existing `console.py` and a
new **Pilot-Station** CLI-GUI instrument panel. Read `../AGENTS.md` (parent repo guide)
first — this file only covers what the display layer needs, plus the three task briefs.

The parent repo owns the probe (`ship_probe.py`), the deployer (`deploy.py`), the
console CLI (`console.py`), `mnw_admin.py` and the unit tests. **Nothing in this
subproject may touch the in-game side.** We only READ/WRITE JSON files and render.

## Where the data lives (file protocol — read this first)

The probe (inside the game) writes these files into its `log_dir`. No network.

| File | Direction | Meaning |
|------|-----------|---------|
| `ship_state.json` | probe → display | PLAYER element state, refreshed every `state_every` ticks (~35 s wall-clock at `tick_delay:30, state_every:3`) |
| `ai_state.json` | probe → display | per-AI-element state (ships that write state; helo/sub hosts are command-only and do NOT appear here) |
| `ship_probe.json` | probe → display | API discovery map (rarely needed by display) |
| `ship_probe_log.txt` | probe → display | tailable event log; per-host authoritative answers |
| `ship_orders.json` | display → probe | `{"commands":[{"cmdid":N,"action":"..."}]}`, atomic replace, `cmdid` monotonically increasing |
| `ship_results.json` | probe → display | results keyed by `cmdid`; **last-writer-wins across hosts — do NOT build UI on it**; read `ship_probe_log.txt` for per-command truth |

**Resolving `log_dir`:** the probe writes to the first writable dir of `log_dir` (config)
→ script dir → cwd → HOME → /tmp. Live remote: `console.py --game-root "<MNW install>"`
resolves to `<MNW>/Var/Scripts/Execute/_Source/`. For tests/local demo: `--log-dir <dir>`
with a `ship_state.json` you generate. There is **no live data without a running game** —
make every renderer degrade gracefully on missing files (the `read_json` helpers in
`../mnw_admin.py` return `None` on IOError and `{"__error__": ...}` on JSON errors).

**Polling:** the game only refreshes on its own cadence. Do NOT hammer the file — poll
at the same cadence the state refreshes (state JSON ~every 30 s) or a divisor of it;
the console's existing `watch` command uses `interval_s` (default 3 s) + optional count.

## Verified live key inventory (from a running mission, Virginia player sub)

Player sub data — this is the "instrument panel" source of truth. `?`/`None`/`err` means
the read was gated or absent: **do NOT fabricate values.**

### `systems` (43 keys) — masts / snorkel / depths / weapons state

| Key | Type | Example (live) |
|-----|------|----------------|
| `mast_controller_status` | int | `0` |
| `mast_ids` | list[int] | `[0,1,2,3,4,5]` |
| `mast_ids_source` | str | `"probe"` (fallback `"fallback"` if `GetAvailableMastIDs` failed) |
| `mast_ids_err` | str? | `"TypeError: GetAvailableMastIDs() takes exactly 1 argument (0 given)"` — expected; ID probe fallback is what fills `mast_ids` |
| `mast_<id>_type` | str | `Snorkel, Radar1, Photonics1, Photonics2, CommAntenna1, CommAntenna2` (ids 0–5) |
| `mast_<id>_status` | str | `Raised` / `Retracted` |
| `mast_<id>_height` | float | `4.256` (m) — `0.0` when retracted |
| `snorkel_raised` | bool | `True` |
| `snorkel_exposed` | bool | `False` |
| `snorkel_head_valve` | int | `0` |
| `snorkel_intake_hole` | float | `0.1` |
| `snorkel_intake_volume` | float | `0.0` |
| `periscope_depth` | float | `17.07` (m) |
| `surface_depth` | float | `4.15` |
| `standard_depth` | float | `47.25` |
| `max_operational_depth` | float | `335.0` |
| `ordered_depth` | float | `17.07` |
| `ordered_heading` | float | `45.0` |
| `ordered_speed` | float | `5.0` |
| `towed_array` | str | `Extended` / `Retracted` |
| `integrity_damage_ratio` | float | `0.0` |
| `ammo_offensive_ratio` / `ammo_defensive_ratio` | float | `1.0` |
| `rpm` | float | `0.0` |
| `bulkheads`/`lights`/`ciws`/`contact_manager` | object repr | **reprs only — no usable state. Do NOT render as data.** |
| `lights_enabled` | bool | `True` |
| `lights_navstat` | str | `mnw.Core.ElementTools+NAVSTATCodes.Anchored` |

Mast status transitions ARE observable live (09:31 all `Retracted`/0.0 → later session
`mast_0 Snorkel Raised` 4.256, `mast_1 Radar1 Raised` 2.568, towed `Extended`). Good
monitor test case.

### `steering` — control surfaces / rudder / bubble / EOT (new, every tick)

Written by `read_steering()` (probe `read_steering:true` default). All values are
property getters / field access + indexing — no Unity method calls, so the tick loop
stays freeze-safe. Forward plane angles are the live `FlapAngle` of each element in
the public `Hydrodynamics.ForwardPlanes` array.

`read_steering(with_getters=True)` additionally fills `stern_plane_angles` /
`rudder_plane_angles` via the `Hydrodynamics.GetSternPlane(i)/GetRudder(i)` getters
(live-verified freeze-free, but control-path only) — that is what the `planes`
read-out produces. Private fields (`_AutoTrim`, `_DepthEnvelopes`, `_SteeringMode`,
`_MaxPlaneRateOfTurn`) are NOT bindable in the embedded interpreter (AttributeError),
so `auto_trim`/`depth_bands`/`steering_mode`/`max_plane_rate_of_turn` are absent.

| Key | Type | Meaning |
|-----|------|---------|
| `ordered_eot` | int | `EOTOrder` enum value (`AsternEmer=0 … AheadFlank=9`, `SetKnots=10`, `SetTurns=11`, `SetTurnsForKnot=12`) |
| `ordered_speed` | float | ordered speed (kt) |
| `ordered_heading` | float | ordered heading (deg) |
| `ordered_depth` | float | ordered depth (m) |
| `bow_planes_retracted` / `forward_planes_locked` / `int_stern_planes_locked` | bool | plane locks |
| `forward_plane_angles` | list[float] | live `FlapAngle` per forward surface (public array) |
| `stern_plane_angles` / `rudder_plane_angles` | list[float] | via Get*() getters — only in `planes` read-out (`with_getters=True`) |
| `forward_planes_type` | str | Hydrodynamics plane config (`BowRetractableSlide`) |
| `tpk` / `stw` | float | Maneuvering turns/kt + speed-through-water |
| `periscope_depth`/`standard_depth`/`max_operational_depth`/`surface_depth` | float | depth bands (m, also in `systems`) |
| `default_eot` | int | `DefaultEOT` telegraph |
| `cavitation` | str | `Low`/… cavitation state |
| `scope` | str | control scope (`Player`) |
| `err` | str? | present when SteeringDiving unavailable |

Note: `SetBubble(x)` does NOT exist live (AttributeError). Bubble control =
`planes bubble on|off` → `CatchBubble(...)`/`ReleaseBubble()`; `CatchBubble` needs 1
positional arg (do_planes tries `CatchBubble(True)` with no-arg fallback).

Use for: a **controls/planes panel** (plane angles as sliders/gauges, ordered helm, EOT
telegram, locks, autotrim), plus per-refresh diff highlighting (`fwd plane 5.0 → 4.2`).

### Control commands (display → probe via `ship_orders.json`)

- `helm COURSE [EOT] [DEPTH] [--env N] [--snap] [--bubble X] [--autotrim on|off]`
  — full EOT set now: `Stop, Ahead13, Ahead23, AheadStd, AheadFull, AheadFlank,
  Astern13, Astern23, AsternFull, AsternEmer`; `--env N` = depth band 0..3.
- `planes` — read-only control-surface state (queues a fresh probe read + cached view).
- `planes fwd|stern|rudder ANGLE` (deg, planes clamp to ±25) · `planes rudder release` ·
  `planes bubble ANGLE` · `planes bubble release` · `planes autotrim on|off` ·
  `planes bow RETRACT|EXTEND` · `planes lockfwd on|off` · `planes lockint on|off`.

### `navigation` (15 keys)

`lat_lon [lat,lon]`, `heading`, `true_heading`, `speed`, `true_speed`, `velocity [x,y,z]`,
`depth` (positive down), `altitude`, `bottom_range`, `plot_count`, `plot_waypoints
[[lat,lon],…≤12]`, plus blackboard mirrors `_currentcourse`, `_orderedcourse`,
`_currenteotorder`, `_orderedeotorder`, `_currentrpm` (note: ordered course/eot/depth
also exist under `systems.ordered_*`; `_ordereddepth` is in `navigation`).

### `sonar_arrays` (4 arrays live, `read_sonar_arrays:true`)

`arrays[]` each: `index`, `type`, `design_frequency`, `frequency_range`, `beam_type`,
`beam_pattern`, `aov`, `toggle`, `status`, `length`, `course`, `sensor_heading`,
`contact_count`, `contacts_truncated`, `contacts[]` with per-contact: `bearing`,
`relative_bearing`, `range`, `elevation`, `course`, `speed`, `signal`, `noise`,
`self_noise`, `flow_noise`, `ambient_noise`, `thermal_noise`, `doppler`, `category`,
`database_id`, `beam_type`, `id`, `nan`. Gate: `read_sonar_arrays` in the probe config
(was `false` on remote historically; now live with 4 arrays). `err` key present when read failed.

### `contacts` (gated by `read_contacts`; remote currently `{"count":0,"disabled":true}`)

`count` + `tracks[]`: `id`, `type`, `category`, `prefix`, `identity`, `speed`, `range`,
`elevation`, `course`, `rcpa`, `tcpa`, `bearing_rate`, `relative_bearing`, `bearing`.
Known quirks: `course` can be `NaN`, `bearing_rate` `Infinity`, `identity` `Unknown` —
Python's `json` parses bare `NaN`/`Infinity` (lenient) but strict JSON consumers break;
sanitize via a `_safe_num`-style guard before rendering. `disabled:true` (no `tracks`)
means the probe config turned the read off — render as "no contacts (disabled)" not "empty".

### `sonar` (gated by `read_sonar`; off on remote)

`{"active":{...},"passive":{...}}` StrongestContact per band. Off on the player sub (HFS
only) — the `sonar_arrays` path is the real sonar source of truth.

### Other sections the pilot display uses

`identity` (`name, country, category, assignment, elevation, dimensions, speed, course, id`),
`player` (`player_id, is_player, source, id`), `navigation` ordered mirrors,
`blackboard` (`_waypointiterator, _currentassignmentid, _currenttensionlevel` string, …),
`mission` (`active, name, operation, datetime, tension`), `clock` (`time, scale`),
`perf` (per-section ms, only with `measure_perf:true`).

### `ai_state.json` (per AI element, gated by `read_ai`)

Per element: `id, name, country, category, host_style, lat_lon, heading, speed,
true_heading, true_speed, depth, to_player_range_km, to_player_bearing, assignment_id,
ordered_course/eot/depth, current_course/eot/depth, contact_count, action_prep_complete,
incoming_order, current_assignment, ai_data_link, attack_ops`. **Command-only hosts
(helo, submarine Akula) never appear** — their answers only land in `ship_probe_log.txt`.

### `detected` command output (via `ship_results.json`)

The `detected` action scans each AI element's `ContactManager` for a track on the
player. Output in `ship_results.json` detail array:

- `HIT contact on player, range=2086 m (id 3, 5 contacts)` — element has a track on player
- `NO contact on player (checked N of M contacts)` — element has contacts but not on player
- `no _ContactManager on element N` — command-only host, cannot probe
- `DETECTED elements: <comma-separated ids>` — summary line (the `result` field)

**Reliability caveat:** `ship_results.json` is last-writer-wins across hosts. The display
should parse the LATEST result entry matching `action: "detected"` and accept staleness.
A `ts` timestamp is present on each result for age display.

## Processing rules (must-follow)

- **Never fabricate.** `None`, `?`, `err` fields mean gated/absent — show a placeholder.
- **Sanitize NaN/Infinity** (bare tokens; lenient JSON ok in Python) before strict output.
- **`disabled:true` sections** → render as "disabled", not empty.
- **`ship_results.json` is last-writer-wins across hosts** — never build UI on it; for
  per-command truth grep `ship_probe_log.txt`.
- **Writes are atomic replace** (write tmp + `os.replace`); `cmdid` monotonically
  increasing (see `../mnw_admin.py:next_cmdid`). Never append, never renumber.
- **No new dependencies** — stdlib only (the parent's tests are plain `unittest`, no
  external deps; the display must follow, incl. any GUI: curses or ANSI strings).
- **Unit-testable headlessly** — every renderer must take a state dict (or log dir) and
  return/produce text without a game running; tests feed fixture JSON.
- Terminal width: renderers must degrade gracefully when the terminal is narrow.

---

## Task brief 1 — Live `masts` monitor in `../console.py`

Goal: turn the current one-shot `masts` (already wired at `console.py:287`, REPL
`masts` + one-shot dispatch at `:650`/`:744`) into a **live monitor** mode.

Deliverables:
1. `masts --watch [interval_s] [count]` (or a `watch-masts` command — pick the spelling
   that fits the existing CLI and document it in `console.py`'s docstring/`HELP`) that
   re-reads `ship_state.json` in a loop (clear screen, like `cmd_watch` at `:387`).
2. **Panel layout** (fixed-width, narrow-safe):
   - Header: `player`, `identity.name`, game `clock.time`.
   - Depth band bar: `surface_depth / periscope_depth / standard_depth / current depth /
     max_operational_depth` with a marker for `depth` (and `ordered_depth`).
   - Mast table: id | type | status | height m (rows from `mast_<id>_*`, ids from
     `mast_ids`; if `mast_ids` missing, scan `mast_<id>_type` keys 0–5). Show
     `mast_ids_err` as a one-line note when present (it is expected).
   - Snorkel line: `raised/exposed/head_valve/intake_hole/intake_volume`.
   - Towed + ordered helm line: `towed_array`, `ordered_heading/speed/depth`.
3. Reuse the existing `_fmt`, `read_json`, `STATE_FILE` helpers. Keep `cmd_masts` as the
   one-shot that the watch loop calls per refresh (same core renderer).
4. Tests in `../tests/` (fixture `ship_state.json` incl. a raised-mast case): watch loop
   terminates on `count`, renders all mast rows, narrow-terminal doesn't crash.
5. `README.md`/`console.py` docstring: document `masts --watch`.

## Task brief 2 — Pilot-Station CLI-GUI instrument panel

Goal: a new headless-friendly **instrument panel** (`pilot_station.py` in this folder)
rendering the player sub like a real control-room display, live-updating.

Deliverables:
1. Entry: `python3 pilot_station.py --log-dir <dir> [--interval 3] [--count N]` plus
   `--json` mode that dumps one rendered frame as JSON (for tests/automation).
2. Panels (single screen, ANSI/curses, refresh loop; each panel degrades to `?`/empty):
   - **Nav**: `lat_lon`, `heading/true_heading`, `speed/true_speed`, `depth/altitude`,
     `bottom_range`, `rpm`, ordered course/eot/depth + `_waypointiterator`.
   - **Masts/Snorkel/Depths**: same content as Task brief 1's panel — share the renderer.
   - **Sonar**: `sonar_arrays` — per-array summary (type, bearing, status, contact_count)
     and a contact list (bearing/range/course/speed/signal/noise) with the `_safe_num`
     sanitization rule. Fall back to `sonar` section if arrays absent.
   - **Contacts**: `contacts.tracks` table (type/identity/range/bearing/course/speed),
     honoring `disabled`.
   - **Status strip**: damage ratio, ammo off/def ratios, towed, mission name/op,
     tension, `clock`.
3. Layout must fit ~80×24 and not crash at smaller widths; highlight state changes
   between refreshes (e.g. mast raised→retracted) if cheap.
4. **Reuse `../console.py` helpers** (`_fmt`, `read_json`, `STATE_FILE`) rather than
   duplicating — import from the parent (`sys.path`), do not copy code.
5. Tests in this folder (`tests/`): render each panel from fixture JSON headlessly;
   `--json` mode round-trips; no-data state renders placeholders not exceptions.
6. Update this README with usage.

## Task brief 3 — AI Enemy Overview Display (htop/nvtop-style)

Goal: a new **AI enemy overview** (`ai_overview.py` in this folder) rendering a
compact, color-coded tactical summary of all AI units — like htop shows processes,
this shows hostile/contact units with their state, distance, heading, assignment,
and threat level. Live-updating via the same file-protocol polling.

### Data sources

| Source | File | Key data |
|--------|------|----------|
| AI elements | `ai_state.json` | Per element: `id, name, category, country, lat_lon, heading, speed, true_heading, true_speed, depth, to_player_range_km, to_player_bearing, contact_count, current_assignment, ordered_course/eot/depth, current_course/eot/depth, action_prep_complete, incoming_order` |
| Detected | `ship_results.json` (latest `detected` result) | Which elements have a track on the player: per-element `HIT/NO contact on player` + range + contact id; summary `DETECTED elements: <ids>` |
| Contacts | `ship_state.json → contacts.tracks[]` | Tracks on the player: `id, type, category, identity, bearing, range, course, speed, elevation` |
| Threats | `ship_state.json → blackboard` | `_MergedContacts`, `_EnemySuspiciousContacts`, `_TorpedoThreat`, `_MissileThreat`, `_AircraftThreat`, `_Dangerous*Torp/Miss/Air RCPA` thresholds |
| Player | `ship_state.json → identity, navigation` | Player position for relative bearing calculations |

**Note:** `ai_state.json` only contains elements with full state (ship/host_style=general).
Command-only hosts (helo, submarine Akula) do NOT appear — their data is only in
`ship_probe_log.txt`. The display must handle zero elements gracefully.

**`contacts.tracks`** may be `{"count":0,"disabled":true}` when `read_contacts` is off.
The display must render "no contacts (disabled)" not crash.

### Detected integration

The display sends `{\"action\":\"detected\"}` via `ship_orders.json` at each refresh
cycle. The probe scans each AI element's `ContactManager` for a track on the player.
Results land in `ship_results.json` — `last-writer-wins across hosts`, so the display
parses the LATEST result entry matching `action:"detected"` and checks the `ts` field
for staleness (warn if older than 2× poll interval).

Per-element `detected` output (from `detail` array):
- `HIT contact on player, range=2086 m (id 3, 5 contacts)` — element detected player
- `NO contact on player (checked N of M contacts)` — has contacts, not on player
- `no _ContactManager on element N` — command-only host, cannot probe
- Summary: `DETECTED elements: <ids>` — comma-separated list of detecting element IDs

The display merges this into the AI table to show a **Detected** column:
- `YES range` — detected player (with range from the detected output)
- `no` — has contacts but not on player
- `n/a` — no ContactManager (command-only)
- `?` — no detected result available yet (stale or not sent)

### Display layout (80×24 terminal target)

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

### Columns

| Column | Source | Format |
|--------|--------|--------|
| ID | `ai_state.id` | int |
| Name | `ai_state.name` | truncated, max 12 chars |
| Type | `ai_state.category` | `Ship`/`Sub`/`Helo`/`Air` (3 chars) |
| Range | `ai_state.to_player_range_km` | `X.Xkm` or `XXXm` if <1km |
| Brg | `ai_state.to_player_bearing` | `NNN°` |
| Spd | `ai_state.speed` | `NN.Nkt` |
| Hdg | `ai_state.heading` | `NNN°` |
| EOT | `ai_state.current_eot` or `ordered_eot` | short form: `Flank`/`Full`/`Std`/`13`/`23`/`Stop` |
| Detct | `detected` result per element | `YES X.Xkm` / `no` / `n/a` / `?` |
| Assignment | `ai_state.current_assignment.id` + type | Truncated, max 11 chars |

### Color coding (ANSI, degrades to plain text on dumb terminals)

| Condition | Color | Meaning |
|-----------|-------|---------|
| `to_player_range_km < 2` | **RED** bold | Close threat — within weapon range |
| `to_player_range_km < 5` | **YELLOW** | Medium range |
| `to_player_range_km >= 5` | **GREEN** | Far / transit |
| `detected = YES` (any range) | **RED** bold | Has a track on player — you are spotted |
| `action_prep_complete = True` + attack assignment | **RED** bold | Preparing to engage |
| `incoming_order` present + `assignment_id != -1` | **CYAN** | Has fresh orders |
| Element not in `ai_state.json` (command-only) | **DIM** | Known but no state |

### Threat summary bar

Bottom rows aggregate threat data:
1. **Detected line**: comma-separated IDs of elements that detected the player
2. **Threat line**: `_MergedContacts` count, `_EnemySuspiciousContacts` count,
   `_TorpedoThreat` / `_MissileThreat` / `_AircraftThreat` presence
3. **Contacts line**: contact count on player from `contacts.tracks` (if not disabled)

### Entry point

```sh
python3 ai_overview.py --log-dir <dir> [--interval 3] [--count N] [--json] [--no-color]
```

- `--interval 3`: poll every 3 s (same as pilot-station)
- `--count N`: exit after N refreshes (for tests / automation)
- `--json`: dump one rendered frame as JSON (no ANSI, for tests)
- `--no-color`: disable ANSI colors (for dumb terminals / CI)

### Deliverables

1. `ai_overview.py` in this folder — standalone entry point, no game-side changes.
2. **Renderer function** `render_ai_overview(ai_state, ship_state, detected, width=80)` →
   list of text lines. Pure function over state dicts (testable without game).
3. **Detected parser** `parse_detected(ship_results)` → dict mapping element ID to
   `{"detected": bool, "range_m": int|None, "contact_id": str|None}`.
4. **Color helper** `_colorize(text, color)` that degrades to plain text when
   `os.environ.get("TERM")` is dumb or `--no-color` flag is set.
5. **Threat classifier** `_threat_level(element, detected_info)` → `close`/`detected`/
   `medium`/`far`/`unknown` based on range + detected status.
6. **Order sender** `send_detected_cmd(log_dir)` — writes `{\"action\":\"detected\"}`
   to `ship_orders.json` using the next cmdid (imported from `../mnw_admin.py`).
7. Reuse `../console.py` helpers (`read_json`, `_safe_num`) via import, not copy.
8. Tests in `tests/test_ai_overview.py`: render from fixture `ai_state.json` (3 units
   at various ranges), fixture `ship_results.json` with detected hits/misses, verify
   color codes, verify zero-elements case, verify disabled-contacts case,
   `--json` mode round-trip.
9. Update this README with usage and layout.

### Edge cases

- **Zero AI elements**: render header + "no AI elements detected" placeholder.
- **Missing fields**: `to_player_range_km` absent (element host without position data)
  → show `?` for range/brg, do not crash.
- **NaN/Infinity in course/speed**: sanitize via `_safe_num` before rendering.
- **Terminal narrower than 80 cols**: truncate Name and Assignment columns, keep
  numeric columns intact.
- **`disabled:true` contacts section**: render "no contacts (disabled)" in threat bar.
- **No `detected` result yet**: show `?` in Detected column; omit detected summary line.
- **Stale `detected` result**: if `ts` is older than 2× poll interval, append `*` to
  detected column values (e.g. `YES 2.1km*`).
- **`ship_results.json` missing or corrupt**: gracefully degrade — Detected column shows
  `?`, detected summary omitted.

### Nice-to-haves (stretch, not required)

- Sort by range (closest first) — default sort order.
- Delta highlighting: if a unit's range decreased since last refresh, flash yellow.
- `--sort range|name|id` flag.
- `--filter country=RUS` to show only specific nations.

## Definition of done (all three tasks)

- All renderers are pure functions over state dicts (unit-testable without the game).
- Tests green: `python3 -m unittest discover -s tests -q` from the repo root AND
  `python3 -m unittest discover -s cli-gui/tests -q` from the repo root.
- No new dependencies, no in-game changes, README + console docstring updated.
- Live smoke (optional, remote): `--log-dir "<MNW>/Var/Scripts/Execute/_Source"`.

## Context

Parent repo: `/home/masto/MNW-Tool/ship-probe/` (local HEAD `2ef14ae`). Remote mirror
`masto@192.168.1.100:/home/masto/MNW-Utils/Tools/mnw-ship-probe/` (git `caf8fcf`).
Live remote log dir: `/media/games/SteamLibrary/steamapps/common/Modern Naval Warfare/Var/Scripts/Execute/_Source/`.
The probe/deployer/game-side knowledge (freeze rules, deploy `--inject`, data pipeline)
is in `../AGENTS.md` — read it before touching anything that references the probe.
