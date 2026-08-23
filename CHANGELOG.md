# Changelog

## v2.2 (2026-08-23) — Rotating section queue + background writer

Full sync with `masto/MNW-Tool/ship-probe/`. New `collect_mode="queue"` replaces
the bulk `collect_state()` tick pattern with per-section time-sliced execution
and immediate state writes via a background thread.

### ship_probe.py

- **`collect_mode: queue` (default)** — rotating section queue: each acted tick
  runs at most `sections_per_tick` sections (default 1) whose per-section
  interval has elapsed. Each completed section is merged into `_partial_state`
  and written to `ship_state.json` immediately (no full-round wait). Sections
  whose reader returns a generator are time-sliced (`section_slice_ms`, default
  6ms) across ticks. Anti-stutter: heavy sections (`sonar_arrays` ~216ms wall)
  are pumped in small chunks instead of blocking the game thread for 200+ ms.
- **`collect_mode: bulk`** — legacy mode restored: full `collect_state()` every
  `state_every` acted ticks (all sections in ONE tick).
- **Background state writer thread** — `_write_state_async()` hands state dicts
  to a daemon thread via `queue.Queue(maxsize=8)`. JSON serialization and disk
  I/O happen off the game thread (measured: sync write was ~57ms on game volume).
  Queue overflow drops oldest state (stale telemetry is worthless).
- **`min_tick_interval` (default 0)** — wall-clock throttle: skip acting when
  the last acted tick is less than this many seconds ago. Default 0 = off;
  recommended 0.2 with `tick_delay: 1` to bound the acted-tick rate.
- **`_SECTION_DEFS`** — per-section definition table: `(name, method, interval)`.
  Clock/identity extracted as `read_clock_section()` / `read_identity_section()`
  for queue mode. `read_sonar_arrays()` wrapped by `iter_sonar_arrays()` generator.
- **`os.replace()`** — atomic single-syscall replace replaces `remove()+rename()`
  pair (halved disk operations per write).
- **`_record_block()`** — measures per-acted-tick game-thread block time
  (`tick_last_s`, `tick_max_s`) in `perf` dict when `measure_perf: true`.
- **`_drain_write_errors()`** — background writer errors are surfaced in the next
  acted tick via `note_error()`.
- **`stop_writer()`** — called from `finish()` to join the writer thread.

### ship_probe_config.json

- New fields: `collect_mode`, `sections_per_tick`, `section_slice_ms`,
  `section_interval`, `min_tick_interval`.
- `tick_delay` changed 30 → 1, `max_commands_per_cycle` changed 10 → 1,
  `measure_perf` changed false → true.

### Tests

- `test_config_merges_defaults` updated to tolerate intentional config overrides
  (e.g. `measure_perf` deliberately disabled in config while code default is true).
- `test_enumerates_all_namespaces` adds `_flush_writer()` before reading
  `ai_state.json` (now written via background thread).

---

## v2.1 (2026-08-22) — Config toggles + state_every fix

Full sync with `masto/MNW-Tool/ship-probe/`. New per-section config flags,
state_every bug fix, reduced checkpoint noise.

### ship_probe.py

- **Per-section config toggles** — `read_identity`, `read_navigation`,
  `read_blackboard`, `read_systems`, `read_steering`, `read_mission`,
  `read_clock` added to `_DEFAULTS`. Each `read_*()` section gated by its
  flag; disabled sections return `{"disabled": true}`.
- **`collect_systems_components`** (default false) — gates the expensive
  Components loop in `read_systems()` (~30-50 C# calls per tank).
- **`state_every` bug fix** — was checking `tick_count % state_every` against
  the raw random-tick counter, which is always a multiple of `tick_delay`.
  When `state_every` divides `tick_delay` (e.g. 30%3==0), the condition was
  always true → `state_every` had zero effect. Now uses a separate
  `_acted_count` counter. Reduces collect_state() calls by ~67%.
- **Compact JSON output** — `indent=2` removed from `_atomic_write` for
  smaller state files.
- **Checkpoints removed** — all `self.emit("cp: ...")` lines removed from
  `collect_state()`. Inner checkpoints in `read_contacts`/`read_sonar`/`read_ai`
  remain for freeze diagnosis.
- **Default config updated** — `read_sonar_arrays: true`, `max_sonar_arrays: 4`,
  added `read_mission: true`, `read_clock: true`, `measure_perf: false`.

### ship_probe_config.json

- New fields: `read_identity`, `read_navigation`, `read_blackboard`,
  `read_systems`, `collect_systems_components`, `read_steering`,
  `read_mission`, `read_clock`, `measure_perf`.
- `read_sonar_arrays` changed `false` → `true`, `max_sonar_arrays` `8` → `4`.

---

## v2.0 (2026-08-18) — MNW-Tool ship-probe integration

Full sync with `masto/MNW-Tool/ship-probe/` (85 commits). Code files
(`ship_probe.py`, `console.py`, `deploy.py`) synced, reference docs extended,
tests added.

### Commands

- **`tracker TYPE`** — expanded with sub-modes: `tracker TYPE ID` (getter probe),
  `tracker TYPE clear` / `clearid ID`, `tracker TYPE loadsnap STR`,
  `tracker TYPE tkdump ID` (ContactData dump), `tracker new TYPE ID`,
  `tracker raw` (raw ContactManager diagnostics).
- **`tracker` notes** — documented: `GetUsed()` returns only assigned tracks,
  Track IDs ≠ ContactManager IDs, Sonar prefix via ContactManager.
- **`sonctl mark ID BEARING`** — updated to include ManualMark with DateTime.
- **`read_sonar()` rewritten** — uses safe Tracker-API (`GetContactIDs()` +
  `GetTrackerData()`) instead of freeze-prone `StrongestContact`.
- **ESM tracker prefix fix** — `GetPrefix()` returns C# enums; fixed with
  `_PREFIX_MAP` string-matching before `int()` fallback.
- **ManualMark signature corrected** — requires `(contactID, bearing, DateTime)`,
  not just `(contactID, bearing)`.
- **Results accumulation** — `dispatch_orders` now reads and concatenates old
  results instead of overwriting each tick.
- **Prefix-mapping fix** — `int(ContactType.Sonar)` returned wrong tracker index
  (1 = Radar) in IronPython; fixed with `_prefix_to_idx()` string-match.
- **`dc` command** — damage control: `dc status`, `dc bulkheads close|open`,
  `dc bulkhead <N> close|open`, `dc lights`. Fire/extinguish/flood/deflood
  disabled (freeze + mono GC crash).
- **`damage` command** — read-only integrity + compartment damage view
  (always-on in `ship_state.json` systems.*).
- **Expanded Integrity reading** — probe now reads 7 integrity ratios
  (DamageLevelRatio, OperationalLevelRatio, HullLevelRatio, HullStressRatio,
  TanksLevelRatio, SunkLevelRatio, PlateStrength), fire/flooding/sunk flags,
  and per-tank details (bulkhead, fire, flooding, level, component status).
- **Expanded lights reading** — `lights_enabled` and `lights_navstat` fields.
- **`alarm control-check`** — Integrity + Coxswain subsystem member probe
  (Bulkheads, Lights, CIWs).
- **Tests** — `test_console.py`, `test_deploy.py`, `test_probe_utils.py`,
  `test_mnw_admin.py` added from MNW-Tool.

### Safety

- **Native crash: `_InformationElemenet.Operator.CountryID`** — never use;
  resolve country via `host._Information.CountryID` instead.
- **Native crash: host `client` blackboard wrapper** — do not read arbitrary
  attributes; resolve country via `host._Information.CountryID`.
- **`Access[Type]` returns a factory** — `ctrl.Access[t]()` requires the `()`.
- **Orders-queue ownership** — only the Lock-holding FULL-Probe executes global
  actions (`tanks`, `env`, `helm`, `planes`, `plot`); command-only instances
  are restricted to element actions (`ai-attack`, `ns-dump`, `asg`, `ai-contacts`).
- **CMDID race condition** — local console and ssh batches share the same
  `ship_orders.json`; always use unique high cmdids.
- **Freeze warning** — too many native C# calls in series (e.g. 9× ManualMark,
  4× LoadSnapshot) freeze the game; keep to ≤2 C# calls per command.

### Documentation

- **README.md** — expanded tracker sub-commands, tracker notes, freeze warning.
- **Launcher Categorization** — fully documented from live testing: `CollectFlags`,
  `CollectWeaponInformation`, `Core.Select`, `GetCandidates`, `DetermineAttackSide`,
  `Fire` pipeline, DB `TargetCategory` mapping.
- **Sonar arrays** — documented per-array contact fields (Signal, Noise, Bearing,
  Range, etc.) via public `Sonars` property.
- **Mast/Periscope/Snorkel/SteeringDiving** — API surface verified live, documented
  with `GetAvailableMastIDs` quirk and freeze-safe read patterns.
- **Alarm-System Discovery** — brute-force 105+ type names via Access[T](); no
  alarm component found, GQ remains unobservable via probe.
- **Weapon/Launcher/Platform DBs** — full table inventory with IDs and categories.
- **Valve/Mast control + Active Sonar Sim** — documented in AGENTS.md.

### Deployed config (current)

```
resolve_positions: false
read_contacts: true
read_sonar: true        # safe (Tracker-API, not StrongestContact)
read_sonar_arrays: true # per-array contacts
read_ai: true
```
