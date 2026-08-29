# Changelog

## v2.10 (2026-08-29) — Multi-host ai_state merge, blackboard helo gate, TUI ghost fix

Sync ship_probe + cli-gui from `masto/MNW-Tool/`. Command-only hosts (helo, submarine)
now contribute their element rows into `ai_state.json`, so ghosts in the TUI get real
positions. Helicopter full-probe exclusion now uses a blackboard signature instead of
the caller filename.

### ship_probe.py

- **Multi-host `ai_state.json` merge** — the full probe and every command-only element
  host now write only their OWN element ids and keep the other hosts' entries. A plain
  overwrite would clobber helo/sub entries and back. New async writer mode
  `ai_state_merge` (`_enqueue_ai_merge` / `_writer_ai_state_merge`): background
  read-merge-replace, queue-full drops superseded snapshots, writer thread serializes
  so the read-merge-replace never races within a host.
- **`_maybe_contribute_ai_state()`** — command-only hosts run a light per-own-element
  pass (`_read_own_ai_elements`) mirroring the field set of `read_ai_elements`, filtered
  to ids on THIS host (identified by their blackboard namespaces). Rate-limited by
  `_AI_WRITE_MIN_S`. Contextless manager namespaces without `_Navigation` are skipped so
  they don't pollute the file with positionless ghosts.
- **`_host_is_helo()`** — helicopter full-probe exclusion now detects via blackboard
  signing keys (`_DippingSonarController`, `_DippingSonarOps`, `_DippingEngaged`)
  instead of `_caller_file().lower()`. MNW runs element scripts via exec making the
  caller's `co_filename` literally `'<string>'`, so the filename check never matched.
- **disasm references cleaned** (5 occurrences).

### cli-gui/ai_tactical.py

- **Ghost census excludes state rows** — elements now merged into `ai_state.json`
  (contributed by their command-only hosts) render as real rows, not ghosts. The ghost
  count filters out ids already present in `ai_state.json`.

### Tests

- ship_probe: 257 green. cli-gui: 105 green.

## v2.9 (2026-08-25) — Multi-host skip, rotation dedup, watch 0.5s, traceback format

Sync ship_probe + console + cli-gui from `masto/MNW-Tool/`. Fixes a multi-host
race where the full probe consumed element-targeting commands before the correct
command-only host could pick them up. Adds rotation dedup in the TUI, faster watch
interval, and traceback formatting in the probe.

### ship_probe.py

- **Multi-host namespace skip** — when the full probe holds the lock it now skips
  element-targeting commands (`ai-attack`, `ns-dump`, `asg`, `ai-contacts`, `ai-state`,
  `steer`, `wc-dump`) whose target element ID is NOT in the full probe's own namespace.
  Skipped commands stay in the file for the correct command-only host to pick up.
  Prevents "element not found" for every host when the full probe consumes the order
  first.
- **`_ELEMENT_ACTIONS` expanded** — includes `steer` and `wc-dump` (new element-targeting
  commands).
- **Command-only pruning** — command-only hosts now prune their OWN processed_ids from
  the order queue (previously only the full probe pruned). Floor-based stale pruning
  remains full-probe only.
- **Traceback formatting** — `note_error()` now includes the last 4 traceback frames
  in the error line for easier debugging.
- **disasm references cleaned** (6 occurrences).

### console.py

- **`_WATCH_INTERVAL = 0.5`** — watch default 3s → 0.5s (probe refreshes at ~2.9/s;
  faster polling shows fresher data).
- **Section age display** — `print_state()` shows per-section ages (`_sec_ts`) when
  present: `sections (age): nav 2s | sys 5s | ...`.
- **`ai-state` command** — REPL + one-shot now accept `ai-state ID` for per-element
  detail queries.

### cli-gui/ai_tactical.py

- **Rotation dedup** — `_ingested_cmdids` set tracks which asg/ext results have been
  ingested. Re-ingestion of `ship_results.json` on every cycle no longer refreshes
  `ts_epoch` to `data["now"]`, which was preventing rotation targets from ever
  appearing stale. Capped at 1000 entries.
- **Footer 2-row reservation** — `footer_y = h - 2` reserves space for help line +
  attack status line.
- **DATALINK auto-scroll** — shows newest entries that fit (bottom slice of `dl_all`).

### Tests

- ship_probe: 257 green. cli-gui: 105 green.

## v2.8 (2026-08-24) — Lock heartbeat, takeover, helo gate, TARGETING, DATALINK history

Sync ship_probe + cli-gui from `masto/MNW-Tool/`. Fixes a critical multi-host bug
where a dead full-probe owner (e.g. helicopter crash) blocked state writes forever.
Adds command-only host takeover, helicopter lock gate, detected/timing diagnostics,
and two new TUI features (TARGETING block, DATALINK history panel).

### ship_probe.py

- **`_LOCK_STALE_S` 30 min → 60 s** with heartbeat — the full probe now TOUCHES
  the lock file mtime every acted tick (`_touch_heartbeat()`). A lock older than 60 s
  while the game is running means the owner died; any command-only host may now take
  over. Was 30 min with NO heartbeat: a dead owner (helo crash 2026-08-24) blocked
  takeover for the whole window and state writes stayed dead ("gametick stale" forever).
- **`_maybe_takeover()`** — rate-limited stale-lock takeover for command-only hosts.
  Checks lock age periodically (`lock_takeover_check_s`, default 15 s + per-host jitter),
  wins the `O_EXCL` race, builds a fresh full probe via `begin()`, demotes the old one.
- **`_demote()`** — stops acting WITHOUT releasing the lock (a takeover winner may
  already own it). Sets `_dead=True`, stops writer, closes log.
- **Helicopter lock gate** — hosts whose caller file path contains "helicopter" are
  forced command-only and never attempt `_acquire_lock()`. Two consecutive sessions
  the lock landed on the Z-9C host: run 1 died (state writes dead), run 2 hung
  natively ~60 s into hot contact.
- **`tick()` early exit on `_dead`** — demoted probes return immediately.
- **`_lock_path` stored on probe** — used by `_touch_heartbeat()` and `_demote()`.
- **Detected freeze diagnostics** — per-element `_ai_track_probe()` timing logged
  (`detected: elem N track probe M ms`), total elapsed logged at end.
- **TARGETING surface in `do_ai_state`** — new blackboard-kv reads: `suspects_n`,
  `suspects_ids`, `tracked_cat`, `contact_cache`, `target_lat/lon`, `target_course`,
  `fire_domain`, `fire_orient`. Pure kv reads, no FireControl internals.
- **`_LOCK_STALE_S` reduced to 60 s** (from 30 min) — enough for a command-only
  host to detect a dead full probe within one check cycle.
- **disasm references cleaned** (5 occurrences).

### cli-gui/ai_tactical.py

- **TARGETING block in DETAIL** — `parse_ai_state_detail` coerces new keys;
  `render_detail` shows `TARGETING:` line (red when `suspects_n > 0`).
- **DATALINK history panel** — event journal tracking order dispatches, assignment
  changes, incoming orders, attack phase transitions. Capped at 60 entries.
  `datalog_add()` / `render_datalink_history()` / curses panel.
- **Ghost census** — counts elements that were seen but disappeared (no state update
  for >30 s). `ghost_count()` / `render_ghost_summary()`.
- **Ship + legacy plane ghosts** — ghost census now includes ship and legacy plane
  elements that dropped out, not just helos/subs.
- **`l` filter key** — toggles DATALINK history panel visibility.
- **Tip renderer** — context-sensitive help tips at bottom of screen.
- **`probe_died_ms` overlay** — amber banner when probe age exceeds threshold.

### cli-gui/tests/test_ai_tactical.py

- 17 new tests: TARGETING detail, DATALINK history, ghost census, ship ghosts,
  tip renderer, `l` filter, probe_died_ms overlay, takeover config key.
  Suite: 105 green.

### Tests

- ship_probe: 257 green. cli-gui: 105 green. Full suite: 362 green.

## v2.7 (2026-08-24) — Deadline scheduler + courier sections

Sync ship_probe + cli-gui from `masto/MNW-Tool/`. Replaces the fixed-rotation
section queue with a deadline scheduler that always runs the most overdue
section first, plus a courier mechanism to keep tiny sections alive during long
generator slices. Config unchanged.

### ship_probe.py

- **Deadline scheduler** — `_collect_next_section()` now builds a candidate list
  of overdue sections (elapsed time > interval), sorts by lateness, and runs the
  most overdue first. Replaces the old round-robin index rotation whose tick-
  count intervals produced burst/starve patterns at variable tick rates.
- **`_SLICE_COURIER_MAX_MS = 4.0`** — while a generator section (sonar_arrays)
  is mid-slice, one overdue tiny section (measured cost ≤ 4 ms) may run
  alongside it per pump. Prevents clock/navigation/blackboard starvation during
  long sonar_arrays passes (~216 ms). Controlled by `slice_courier` config
  (default true).
- **`_MID_SLICE_FLUSH_S` tightened 2.0 → 1.2** — fits the measured `_random_tick_`
  spacing of 0.4–1.8 s; keeps worst-case instrument blackout near ~2.5 s.
- **Cost EMA** (`_sec_cost_ema`) — rolling average of each section reader's
  wall-clock cost drives courier eligibility without requiring `measure_perf`.
- **`_log_sched_state()`** — diagnostic (rate-limited 1/s) showing the most
  overdue sections and any running slice job.
- **disasm references cleaned** (8 occurrences).

### Tests

- SonarArraysTest `test_enumerates_arrays_and_contacts` updated to use
  `full_contacts=True` (new default is slim mode — 9 fields instead of 16).
  Suite: 257 + 88 = 345 green.

---

## v2.6 (2026-08-23) — Performance: mid-slice flush, slim sonar, AI dampening + PROBE BUSY

Sync ship_probe + cli-gui from `masto/MNW-Tool/`. Three probe-side performance
optimizations (tick-hole resilience, reduced C# call volume, AI write dampening)
and a TUI-side PROBE BUSY banner.

### ship_probe.py

- **`_MID_SLICE_FLUSH_S = 2.0`** — while a section generator (sonar_arrays) is
  mid-slice, flush a partial snapshot at least every 2 s so instruments never
  black out. Injects a fresh clock reading into `_partial_state` and calls
  `_flush_partial_state()`.
- **`_AI_WRITE_MIN_S = 8.0`** — skip `read_ai_elements()` entirely when the last
  write is younger than 8 s. Caches the last result and serves it; emits a
  single skip note per window. Eliminates the bursty double-update within ~11 s
  that was pure waste.
- **`sonar_contacts_full` (default false)** — slim contact field set: bearing,
  range, elevation, course, speed, signal, noise, doppler, category + nan (~9
  C# reads per contact instead of 16). Exotic noise-split, database_id,
  beam_type, id, relative_bearing stay behind the full flag.
- **disasm references cleaned** (8 occurrences).

### ship_probe_config.json

- New field: `sonar_contacts_full` (default false).

### cli-gui/ai_tactical.py

- **PROBE BUSY banner** — amber `!! PROBE BUSY - no fresh data for Ns (game
  tick stall) !!` under the header when `ship_state` age exceeds 15 s. Works
  in curses and `--json`/text mode. `probe_busy_age(frame)` helper + `PROBE_BUSY_S = 15.0`.
- **Curses draw uses 2 head lines** — banner + header both rendered at top.

### cli-gui/tests/test_ai_tactical.py

- 5 new `TestProbeBusy` cases: fresh=no banner, stalled=banner with age, helper
  returns None/age, narrow truncation, curses 2-line head. Suite: 81 green.

### cli-gui/CHANGELOG.md + README.md

- PROBE BUSY banner documented.

---

## v2.5 (2026-08-23) — ship_probe: multi-host delivery + stale-order pruning

Sync ship_probe from `masto/MNW-Tool/`. Adds element-action grace window for
multi-host delivery, results history cap, stale-order pruning, and ns-dump
summary line. cli-gui unchanged.

### ship_probe.py

- **`element_action_grace_s` (default 5.0)** — holds element-action commands
  (ns-dump/asg/ai-state/ai-attack/detected) in `ship_orders.json` for a grace
  window so command-only hosts (separate interpreters, own tick phase) can see
  and answer them. The full probe previously ate all processed orders
  immediately, winning the read/prune race against the other hosts.
- **`_RESULTS_KEEP = 300`** — caps accumulated results history. Without a cap
  both `ship_results.json` and `console_results` grow O(session length),
  costing O(n) synchronous reads on every command tick.
- **Stale-order pruning** — ids ≤ `last_floor` are detected on the first read
  and removed alongside processed ids. External writers that restart their
  cmdid floor no longer deadlock the channel until probe restart. Logs
  `orders: pruned N stale cmdid(s)`.
- **ns-dump summary line** — machine-parsable `ns-dump: elements=[/13:style,...]`
  listing every namespace this host sees, so element discovery survives even
  when individual ns lines fall out of the log tail.
- **asg/ai-state error handling** — raises `RuntimeError("element not found")`
  instead of returning `None`, giving consumers a recognizable negative answer.
- **disasm references cleaned** (8 occurrences — same set re-introduced by
  upstream on each sync).

### ship_probe_config.json

- New field: `element_action_grace_s` (default 5.0).

---

## v2.4 (2026-08-23) — cli-gui: cmdid deadlock fix + table headers + NaN sanitizer

Sync ship_probe + cli-gui/ from `masto/MNW-Tool/`. cli-gui fixes a TUI-restart
cmdid deadlock, adds column headers with units, and sanitizes NaN/Inf in sonar
tracks. ship_probe: disasm references cleaned (8 occurrences).

### cli-gui/ai_tactical.py

- **cmdid floor deadlock fix** — after a TUI restart the running probe keeps
  `last_cmdid` in RAM; old sends used a floor from the TUI's own history only,
  which fell below the probe's watermark → commands skipped forever. Floor now
  uses `max(own history, highest cmdid ever answered in ship_results.json)`.
- **Stagnation watchdog** — pendings older than 45 s are dropped and the floor
  rebased onto the results record; guards re-arm immediately.
- **detect-purpose pop** — `"detect"` pending is now properly removed on result
  (was never popped, blocking subsequent detects).
- **ns-dump throttle** — first 3 attempts at 10-cycle cadence, then relaxed to
  30 cycles to reduce orders-queue pollution.
- **Rotation TTLs 120 → 60 s** (`--asg-ttl` default).
- **`_json_safe()` sanitizer** — recursively strips NaN/Inf from sonar track
  passthrough structures; `--json` no longer crashes on bare `nan`/`inf`.
- **Column headers with units** — `RANGE km`, `SPD kt`, `DEP m`, `BRG°`/`HDG°`
  when column width allows; narrow columns fall back to plain abbreviations.
  Header row is pinned (doesn't scroll with data).

### cli-gui/tests/test_ai_tactical.py

- 11 new tests: `TestCmdidFloor` (8 cases — floor, watchdog, detect-pop,
  ns-dump throttle, TTL defaults), `TestTableHeader` (3 cases — units, hdr
  style, text mode), `TestJsonSafe` (2 cases — recursive NaN/Inf stripping).
  Suite: 76 green.

### cli-gui/CHANGELOG.md

- Two new entries: cmdid deadlock fix and column headers.

### cli-gui/README.md

- Refresh model section updated with measured cadence (0.1–0.5 Hz tick hook,
  ~1–2 min full rotation). cmdid contract documented.

---

## v2.3 (2026-08-23) — cli-gui: mast schematic with snorkel readout

Sync cli-gui/ from `masto/MNW-Tool/cli-gui/`. Adds a sail-mast schematic to
the OWN SHIP frame with per-mast fill bars, snorkel head marker, and readout
line (state, head valve, intake hole/volume, scale hint). 10 new tests.

### ai_tactical.py

- **Mast schematic** — `render_mast_schema(frame, width)`: 6-slot sail drawing
  with fill bars rising from hull at a fixed 5 m scale (max 4 rows). Bars sit
  exactly on slot centres; retracted masts show a dim hull stub, raised-but-
  unknown height shows stub + `?` label. Snorkel bar carries a small head
  square: green when `snorkel_exposed` (above surface), dark blue (`blue_dim`)
  while submerged. Below the drawing: type abbreviations (SNK/RAD1/P1/P2/C1/C2)
  and heights in metres, plus snorkel readout line (state, HV, HL, VV, scale
  hint). Degrades to `[]` below 25 columns.
- **New frame fields** — `player.masts[]` (`id/type/status/height`), plus
  `snorkel_head_valve`, `snorkel_intake_hole`, `snorkel_intake_volume`.
- **`blue_dim` style token** — new curses attribute for submerged/inactive
  water-related markers.
- **Renderer integration** — wired into right-hand column, stacked layout,
  and text/JSON output paths.

### Tests

- 10 new `TestMastSchema` cases: alignment, fill counts per column, snorkel
  head colour by exposure, labels/readout, narrow/empty degrade, integration
  with side-stacked + text mode. Suite: 63 green.

### cli-gui/README.md

- Added mast schematic section with layout description.

### cli-gui/CHANGELOG.md

- Added entry for mast schematic feature.

---

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
