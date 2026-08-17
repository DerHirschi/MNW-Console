# MNW Ship Probe

External data probe + control console for **Modern Naval Warfare**. Runs inside
the game (piggybacked onto existing element scripts), reads **all** own-ship
state (navigation, systems, contacts, sonar, mission, clock) for the player
element and exposes a control surface: helm / plot / report / probe / sonar /
tracker / alarm / tanks / env / planes.

No second `.kyt` package is created — the probe is injected into the existing
scripting package (`hal_9025.kyt` or similar), so the game's script extraction
stays intact.

## Files

| File | Purpose |
|------|---------|
| `ship_probe.py` | In-game probe script (runtime AI API, `_random_tick_` hooks). Writes state, reads orders. |
| `ship_probe_config.json` | Probe config (mirrors `_DEFAULTS` inside `ship_probe.py`). |
| `deploy.py` | Deployer: inject/execute/package modes, verify, remove, purge-execute. |
| `console.py` | External CLI: state/watch/probe/helm/plot/clear-plot/report/sonar/tracker/sonctl/tanks/env/alarm/planes/masts/results/log/status. |
| `tests/` | `unittest` suite (no external deps): deployer patching, console protocol, probe utils. |

## How it works

The game only executes element-bound scripts (those attached to ships/subs in
missions). `deploy.py` appends a guarded block into the `_random_tick_` of the 8
scripts the game provably executes:

```python
# ship_probe piggyback v1
try:
    import ship_probe
    ship_probe.ship_probe_tick(globals())
except Exception:
    pass
```

The probe elects itself leader via `ship_probe.lock` (stale after 30 min), so
only one element runs it even though all elements execute the hook. All output
goes to the first writable dir from `log_dir` → script dir → cwd → HOME → /tmp.

File protocol (all JSON, no network):

| File | Direction | Content |
|------|-----------|---------|
| `ship_state.json` | probe → console | Player element state (identity, navigation, systems, contacts, sonar, mission, clock) |
| `ship_probe.json` | probe → console | API discovery map: component status, attributes, blackboard keys |
| `ship_orders.json` | console → probe | `{"commands": [{"cmdid": N, "action": ...}]}` |
| `ship_results.json` | probe → console | Command results keyed by `cmdid` (accumulates across ticks) |
| `ship_probe_log.txt` | probe → console | Tailable event log |
| `ship_probe.lock` | probe | Leader election lock |

## Tick Timing

The game calls `_random_tick_()` on element-bound scripts approximately **every
~1 second**. The probe's `tick_delay` config controls how many of these random
ticks to skip before acting:

| `tick_delay` | Probe interval | Notes |
|-------------|----------------|-------|
| `1` | ~1 s | Fastest. High CPU load on the game thread. |
| `10` | ~10 s | Good for active control (helm, sonar tracking). |
| `30` | ~30 s | **Default.** Balanced for monitoring + control. |
| `60` | ~60 s | Low overhead, suitable for passive monitoring. |
| `120` | ~120 s | Very low overhead, state updates only. |

**Minimum:** `1` (act on every random tick — use with care, high game-thread load).

**Recommended range:** `10`–`60`. Below `10` the probe may impact game
performance; above `60` command response feels sluggish.

Additionally, `state_every` controls how many `tick_delay` cycles do a full
C# state collection. Between full collects, only order dispatch runs (cheap file
reads). With `tick_delay=30` + `state_every=3`, state refreshes every ~90 s
wall-clock while orders dispatch every ~30 s.

```
# Example: responsive sonar tracking
{"tick_delay": 10, "state_every": 1}

# Example: balanced monitoring (default)
{"tick_delay": 30, "state_every": 3}

# Example: passive logging
{"tick_delay": 60, "state_every": 5}
```

**`max_commands_per_cycle`:** orders are processed in batches of this size per
tick. Default `10`. If more commands are pending, the overflow is processed in
the next tick. Results accumulate across ticks (no data loss).

## Deploy

```
python3 deploy.py --game-root <MNW_DIR> [--backup]
python3 deploy.py --game-root <MNW_DIR> --verify
python3 deploy.py --game-root <MNW_DIR> --remove
python3 deploy.py --game-root <MNW_DIR> --purge-execute
```

- Default mode is **inject** (into the scripting `.kyt` + loose `Execute/_Source`
  tree sync). `--execute` for extracted dev copies, `--package` only for tests.
- `--remove` strips package AND loose tree (both must go, or the game
  re-extracts the probe on next start).
- The piggyback marker `# ship_probe piggyback v1` is distinct from the
  director's `# director piggyback v1`, so both can coexist in one package.

## Console

```
python3 console.py --game-root <MNW_DIR>              # REPL
python3 console.py --game-root <MNW_DIR> state        # one-shot
python3 console.py --game-root <MNW_DIR> helm 045 AheadStd 60
python3 console.py --log-dir <dir> watch 2
```

### State & Monitoring

| Command | Description |
|---------|-------------|
| `state` | Show current player state (nav, systems, contacts, sonar, mission) |
| `watch [INTERVAL] [COUNT]` | Auto-refresh state (default 3 s) |
| `probe` | Show API discovery map (ship_probe.json) |
| `results [N]` | Show last N command results |
| `log [N]` | Tail last N lines of ship_probe_log.txt |
| `status` | Show probe lock/state status |
| `diag` | One-shot diagnostic (results + ai-attack log markers + tail) |

### Navigation & Control

| Command | Description |
|---------|-------------|
| `helm COURSE [EOT] [DEPTH]` | Set course (deg), EOT order, depth (m). Flags: `--env N`, `--snap`, `--bubble ANGLE`, `--autotrim on\|off` |
| `plot LAT LON` | Plot a route to the position |
| `clear-plot` | Clear the current plot |
| `report` | Trigger ReportToHQ |
| `planes [fwd\|stern\|rudder ANGLE]` | Show/set control surface angles. Also: `rudder release`, `bubble on\|off`, `autotrim on\|off`, `bow RETRACT\|EXTEND`, `lockfwd on\|off`, `lockint on\|off` |

### Sonar

| Command | Description |
|---------|-------------|
| `sonar [--detail]` | Show sonar tracker contacts (bearing/range/sensor per contact) + per-array contacts |
| `sonctl auto on\|off` | Enable/disable auto-tracking of new contacts |
| `sonctl ids` | List tracked contact IDs (via GetContactIDs) |
| `sonctl data ID` | Show tracker data for contact (bearing, range, sensor via GetTrackerData) |
| `sonctl mark ID BEARING` | Manually mark a contact bearing |
| `sonctl track ID` | Track a contact (overload — currently broken, use auto on instead) |
| `sonctl untrack GUID TYPE` | Untrack a contact |
| `sonctl diag` | Diagnostic: dump all SonarSystem fields + CachedContacts with dir() |

### Radar / ESM / Fire Control

| Command | Description |
|---------|-------------|
| `tracker [TYPE]` | Show FireControl TrackerManagers + contacts. Types: `visual`, `radar`, `esm`, `radio`, `weapon`, `ais`, `active`, `manual` |
| `radar` | Shortcut for `tracker radar` |
| `esm` | Shortcut for `tracker esm` |

### AI & Combat

| Command | Description |
|---------|-------------|
| `ai [ID] [--watch] [--registry-only]` | List AI elements / show one element |
| `ai-attack ID [--registry-only] [--allow-untracked] [--domain X]` | Order AI element to engage player (domain: Subsurface, Surface, Air) |
| `detected` | Which AI elements hold a contact/track on the player |
| `ai-contacts` | Dump every AI element's own contacts + tracks |
| `steer ID LAT LON [--speed K]` | Move AI element to position (Transit) |
| `wc ID` | Dump AI element WeaponController internals |
| `asg ID` | Dump element's current assignment |
| `ns-dump` | Dump all /N/ blackboard namespace keys |

### Systems

| Command | Description |
|---------|-------------|
| `masts` | Show mast / snorkel / periscope / towed array state |
| `tanks` | Read-only ballast/trim/valve probe (MBTManager + TnCManager + Hydrostatics) |
| `tanks vent\|flood\|drain\|...` | Live tank control (state-changing!) |
| `tanks fill [TANKS...]` | Fill procedure (Hand trim) |
| `tanks drainall [TANKS...]` | Drain procedure |
| `env` | SonarSim environment scan (EnvironmentalSystem, SSP, bathymetry) |
| `env ssp` | Read SSP/TP/Analysis properties live |
| `alarm` | Scan ship alarm + rigging components + blackboard keys |
| `sd-dump` | Dump SteeringDiving + component members (debug) |

### EOT Orders

`Stop`, `Ahead13`, `Ahead23`, `AheadStd`, `AheadFull`, `AheadFlank`,
`Astern13`, `Astern23`, `AsternFull`, `AsternEmer`

## Config (`ship_probe_config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `log_dir` | `""` | Where probe files are written (else script dir/cwd/HOME/tmp) |
| `tick_delay` | `30` | Probe acts every N random ticks (~1 s each). Range: 1–120, recommended 10–60 |
| `heartbeat_every` | `120` | Emit tick heartbeat every N ticks |
| `console_log` | `true` | Also mirror log lines to the engine console |
| `require_player` | `true` | Only collect state for the player element |
| `target_element_id` | `0` | Fallback player id (used when detection fails) |
| `max_contacts` | `50` | Contact list cap per state |
| `max_commands_per_cycle` | `10` | Orders processed per tick (overflow in next tick, accumulates) |
| `allow_commands` | `[helm, planes, ...]` | Whitelist of actions |
| `resolve_positions` | `false` | Resolve contact coords via Mercator when possible |
| `state_every` | `3` | Full state collection every N tick_delay cycles |
| `read_contacts` | `true` | Read ContactManager contacts |
| `read_sonar` | `true` | Read sonar tracker (GetContactIDs/GetTrackerData — safe, no StrongestContact) |
| `read_sonar_arrays` | `true` | Read per-array sonar contacts |
| `read_ai` | `true` | Read AI element states |
| `max_sonar_contacts` | `20` | Sonar tracker contact cap |
| `max_sonar_arrays` | `8` | Sonar array enumeration cap |
| `max_ai_elements` | `30` | AI element enumeration cap |

## Safety notes

- Never a second `.kyt` in `Var/Scripts/Packages/` — breaks script extraction.
- The probe reads `CoordinatesManager.Player` / `PlayerGCID` for player
  detection; it never calls `m.Player` (known Unity main-thread freeze).
- `_mission_started` is built-in in MNW missions — unrelated to this runtime
  probe; don't add mission-side `_P.*` calls to `ship_probe.py`.
- Fully exit and relaunch MNW after deploy/remove so loose files are refreshed.
- **StrongestContact is a freeze source.** `read_sonar()` uses the safe
  `GetContactIDs()`/`GetTrackerData()` tracker API instead.
- **Accessing `.Overloads` on C# methods freezes the game.** Never use
  reflection introspection on overloaded methods from the probe.
- **`m.Player` freezes the Unity main thread.** Use `CoordinatesManager.Player`.

## Tests

```
python3 -m unittest discover -s tests -v
```

Covers deployer patching (idempotency, round-trip, CRLF, legacy upgrade),
console command parsing and the orders/cmdid protocol, and the pure helpers of
`ship_probe.py` (importable without the game runtime).
