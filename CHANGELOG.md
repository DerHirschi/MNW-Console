# Changelog

## v2.0 (2026-08-18) — MNW-Tool ship-probe integration

Full sync with `masto/MNW-Tool/ship-probe/` (85 commits). Code files
(`ship_probe.py`, `console.py`, `deploy.py`) were already identical; this
release documents all live-verified knowledge and extends the README/AGENTS
reference docs.

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

- **AGENTS.md** — sanitized: removed all disassembly/DLL/IL trace references while
  preserving verified API knowledge and live-tested findings.
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
- **Valve/Mast control + Active Sonar Sim** — documented in `PLAN_active_sonar_sim.md`.

### Deployed config (current)

```
resolve_positions: false
read_contacts: true
read_sonar: true        # safe (Tracker-API, not StrongestContact)
read_sonar_arrays: true # per-array contacts
read_ai: true
```
