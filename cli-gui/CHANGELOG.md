# Changelog — cli-gui ("Pilot-Station")

Alle nennenswerten Änderungen dieses Teilprojekts. Format angelehnt an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

### 2026-08-24 — ai_tactical: TARGETING-Block im DETAIL-Panel

Ziel: sichtbar machen, welches Ziel ein KI-Element aktuell für seine Waffen
erfasst hat (z. B. FFG-Raketenwerfer bei Ziel im Anstellwinkel). Reine
Erweiterung des bestehenden `ai-state <id>`-Probes — KEINE
WeaponController-/FireControl-Internals (dokumentierte Native-Crashes,
siehe ship-probe/AGENTS.md `do_wc_dump`).

**Neu**

- Probe (`ship_probe.py`, `do_ai_state`): neue kv-Zeilen `suspects_n`,
  `suspects_ids` (max. 8), `tracked_cat`, `contact_cache`,
  `target_lat`/`target_lon`, `target_course`, `fire_domain`, `fire_orient`
  — alle Blackboard-kv-Reads, `_try`-guarded.
- TUI: `parse_ai_state_detail` coerced die neuen Keys automatisch
  (generischer k=v-Parser); `normalize_elements` reicht sie als
  Element-Felder durch; `render_detail` zeigt unter TRACK-ON-PLAYER eine
  `TARGETING:`-Zeile (rot wenn `suspects_n > 0`, sonst normal; Zeile
  entfällt komplett ohne Felder).

### 2026-08-24 — ai_tactical: DATALINK-History-Panel (Brief Stufe 1)

Umsetzung von `BRIEF_datalink_history.md` Stufe 1 (reine TUI, kein
Probe-Change), Variante a: TAB öffnet DETAIL + darunter die `DATALINK`-Box —
Normalmodus bleibt höhenstabil.

**Neu**

- **Event-Journal im Collector** (Ringpuffer, 500 Einträge): reine
  Übergangs-Diffs pro Poll — `ORDER`/`ADOPTED` aus KI-State-Snapshots
  (`incoming_order.assignment_id`, `assignment_id`), `DETECTED`/`DETCLEAR`
  aus dem detected-Merge, `ATTACK-OK`/`ATTACK-FAIL` aus dem bestehenden
  Result-Ingest (cmdid→eid via `_pending_attack_eid`),
  `GHOST+`/`GHOST-` bei ns_styles-Änderung. Unveränderte Zustände erzeugen
  keine Events; Baseline-Elemente (erster Poll) ebenfalls nicht.
- **Renderer** `render_datalink_lines()` (pure): neueste Zeile unten,
  max ~12 Rows, Styles ts=`dim` / ORDER·ADOPTED=`cyan` /
  DETECTED=`red` / ATTACK=`green`·`red` / Ghost=`amber`; Narrow-safe.
- **Taste `l`:** Filterzyklus alle → ausgewähltes Element → alle
  (`DATALINK #id` im Box-Titel); HELP_LINE erweitert.
- **Frame-Feld `dl_history`** (letzte 200 Events) → `--json` exportiert mit.

**Bewusst nicht:** `--journal-file` (Auftraggeber-Entscheidung),
Probe-seitiges `dl-log` (Stufe 2, braucht ship-probe-Abstimmung).

**Tests**: 8 neue (`TestDatalinkJournal`) — Delta-Funktionen, Attack-
Ingest, Ghost-Zyklus, Renderer (Styles/Filter/Breite), Frame-Roundtrip,
Textmodus. Suite: 92 → 100 grün. Lokal end-to-end verifiziert
(GHOST+-Events via `--json`).

### 2026-08-24 — ai_tactical: Ship-Ghosts + Ghost-Zähler (AI Gauntlet Session)

**Befund (live, ~20:30):** In der AI-Gauntlet-Mission zeigte die TUI nur
Helo+Sub — die 4 feindlichen Schiffe fehlten. Ursachenkette: (1) der
Full-Probe-Lock fiel diesmal auf den Helo-Host, `ai_state.json` enthielt
nur dessen Namespaces (`/0/`, `/16/`); (2) die Ghost-Discovery aus den
ns-dump-Zeilen des Logs filterte auf `helo`/`plane/sub` und verwarf
`ship`; (3) der Sub-Host emittiert seinen Style nur EINMAL bei `begin()`
— die Zeile rollt aus dem 400-Zeilen-Tail. Die Schiffe selbst waren
voll funktionsfähig im Spiel (Spawns + Assignments 3–8 im Player.log).

**Neu**

- **Ship-Ghosts:** `normalize_elements` übernimmt ns_styles mit
  `ship` als Tabellenzeilen (`typ=SHP`, Fallbackname „Ship #N");
  Legacy-Style `plane` (ältere deployte Probe-Versionen klassifizieren
  Subs so) wird ebenfalls akzeptiert → `SUB`.
- **Ghost-Zähler:** Frame-Feld `ghosts` (Anzahl entdeckter ns_styles-
  Elemente ohne Player/eigene IDs) im Footer: `| ghosts:N`.
- **ns-dump-Wartungstakt:** Nach der Discovery-Phase (stoppt beim ersten
  Ghost-Style) bleibt ein langsamer Rhythmus (~24 Zyklen) aktiv, damit
  Hosts, deren einmalige begin()-Zeile aus dem Log-Tail gerollt ist,
  wiederentdeckt werden.

**Tests:** 92 bestanden (neu: ship-ghost rows, legacy-plane SUB,
ghost counter, Wartungstakt im Bootstrap-Test).

### 2026-08-24 — ai_tactical: Attack-Feedback-Banner + Taste B (Blindangriff)

**Befund (live, 07:08–07:12):** Die ersten fünf manuellen Attacks liefen
game-seitig durch (`PushOrder ok`, assignments 89–98) — die TUI zeigte es
aber nicht. Danach verweigerte der Probe den Angriff mit
`no contact on player — refusing blind attack` (Safety-Gate: das Element
hatte keinen aktiven Track mehr auf den Player; der Agent umging das
früher per `allow_untracked:true`). Beides war aus der TUI unsichtbar.

**Neu**

- **Feedback-Banner:** ai-attack-Antworten werden in `_ingest_results`
  ausgewertet (`ok` + `result`-String); `draw()` zeigt den letzten Ausgang
  ~12 s über dem Footer — `ATTACK #N OK : PushOrder ok ...` grün bzw.
  `ATTACK #N FAILED : RuntimeError: no contact on player ...` rot
  (No-Color-Fallback Bold/Reverse).
- **Taste B = BLIND:** gleiche Armier-/Bestätigungskette wie A (`y` binnen
  5 s), Banner `AI-ATTACK-BLIND ... UNTRACKED OK!`; feuert mit
  `allow_untracked:true` und überspringt damit das Track-Gate des Probes.
  `queue_ai_attack(eid, allow_untracked=False)` neuer Parameter.

Probe-seitig keine Änderung nötig (`do_ai_attack` kennt `allow_untracked`
bereits) — kein Remote-Deploy, kein Spiel-Neustart. Tests:
`test_queue_ai_attack_untracked_payload`, Ingest-Success/Refusal-Tests,
read_only-No-op für beide Keys.

### 2026-08-23 — ai_tactical: Manueller ai-attack (Taste A + y-Bestätigung)

**Neu**

- `Collector.queue_ai_attack(eid)`: schreibt
  `{"action":"ai-attack","id":N}` über den bewachten `send_commands`-Pfad
  (cmdid-Floor, read_only-No-op, Pending-Tracking mit Stagnations-Watch).
  IDs ≤0/None werden abgelehnt.
- Tastenbindung `A`: armiert das selektierte Element (invertiertes Banner
  mit Name/ID), `y` bestätigt binnen 5 s, jede andere Taste (oder Timeout)
  bricht ab — ein Fehlgriff kann kein schweres C#-Work auf dem Spielhost
  auslösen. Beantwortete Results geben den Pending-Slot frei; der
  cp-Trace bleibt im `ship_probe_log.txt`.
- Hilfe-Zeile um `A attack` ergänzt; 4 neue Tests (`TestAiAttackCommand`),
  Suite 85 OK.

Probe-seitig war alles vorhanden: `ai-attack` in `allow_commands`,
`do_ai_attack` mit Ownership-Check, multi-host safe.

### 2026-08-23 — ai_tactical: PROBE BUSY-Banner bei Tick-Löchern

**Befund (live gemessen):** Der `_random_tick_`-Hook des Spiels kommt in
Schüben mit 7–12 s Funklöchern — in diesen Fenstern schreibt der Probe
nichts (auch clock/navigation nicht), die TUI friert ein. Das vom Nutzer
gemeldete Muster „2 KI-Updates hintereinander, dann ~30 s Pause" ist die
`ai`-Sektion, die bei Tick-Schüben zweimal binnen ~11 s feuert; zusätzlich
blockiert `sonar_arrays` mit >50 Kontakten/Array ~12 s pro Array.

**Behoben (Display-Seite)**

- Neuer Amber-Banner `!! PROBE BUSY - no fresh data for Ns (game tick
  stall) !!` unter dem Header, sobald `ship_state` älter als 15 s ist —
  in curses und `--json`/Textmodus identisch (`render_frame_lines[:2]`).
  Rein funktional: `probe_busy_age(frame)` + 5 Tests; live verifiziert,
  dass bei frischen Daten (Age 1 s) korrekt kein Banner erscheint.

**Probe-seitig angefordert:** Auftrag 3 in
`../ship-probe/BRIEF_orders_prune.md` — inkrementelle State-Writes beim
Slicing (≥ alle 2 s), schlanker Kontaktmodus für sonar_arrays
(`sonar_contacts_full:false`, 6 statt ~16 Attribute/Kontakt),
AI-Write-Dämpfung (<8 s → Skip). Auftrag 1 (Pruning) ist inzwischen live:
Orders-Datei leer, Kanal verarbeitet wieder.

Suite: 81 grün.

### 2026-08-23 — ai_tactical: Command-Kanal-Deadlock nach TUI-Restart behoben

**Ursache (live verifiziert):** Der Probe hält `last_cmdid` nur im RAM
(17:01 gestartet, bis cmdid 39 verarbeitet). Nach einem TUI-Neustart schrieb
die TUI wieder ab cmdid 0 — der Probe skippt IDs ≤ last_cmdid dauerhaft und
entfernt sie nicht → Backlog ab 0 (18:13), kein Resultat mehr, DET-Spalte
frisst das Stale-Flag nach ~60 s (`max(60, 2×interval)`).

**Behoben**

- **Floor-Rebase:** Ingest trackt `_result_cmdid_max` (höchste je
  beantwortete cmdid aus `ship_results.json`); Sends flooren auf
  `max(_last_cmdid, _result_cmdid_max)` → neue IDs landen immer über der
  Probe-Historie, Kanal lebt sofort nach TUI-Restart weiter.
- **Stagnations-Watchdog:** Pendings älter 45 s werden gedroppt (Guards
  re-armen) und der Floor auf den Results-Max rebased.
- **Bug #3:** Purpose `"detect"` wurde nie aus `pending` gepoppt — nach dem
  ersten detected liefen alle weiteren automatisch leer. Zentrale
  `_pop_pending()` räumt jetzt inkl. `pending_ts` auf.
- **ns-dump-Bootstrap gedrosselt:** erste 3 Versuche im 10-Zyklen-Takt,
  danach 30 (weniger Backlog-Verschmutzung bei Missionen ohne Helo/Sub).
- **Rotation-TTLs 120 → 60 s** (`--asg-ttl` + Collector-Default).

**Doku:** README „Refresh model" auf gemessene Realität korrigiert
(Hook ~0,1–0,5 Hz hier; Sektionsrotation ~1–2 min ⇒ KI-Spalten ~1×/min;
cmdid-Vertrag dokumentiert). Probe-seitiges Pruning angefordert:
`../ship-probe/BRIEF_orders_prune.md`.

**Zusätzlich (Live-Befund):** `--json` crashte an bare `nan`/`inf` in den
Sonar-Passthrough-Tracks (`sonar.tracks[].range`, `.sensors[].range`) — neuer
rekursiver `_json_safe()`-Sanitizer vor dem Dump; beide Ausgabepfade bleiben
`allow_nan=False`. Regressionstests in `TestJsonSafe`.

**Tests**: 8 neue (`TestCmdidFloor`) — Results-Floor, Watchdog-Rebase/
Prune, detect-Pop, ns-dump-Drossel, TTL-Defaults. Suite: 76 grün.

### 2026-08-23 — ai_tactical: Spalten-Header in der AI-CONTACTS-Tabelle

- Der in `render_table()` bereits gebaute Header wurde im curses-`draw()`
  per `[1:]` verworfen — er wird jetzt als erste Box-Innenzeile gezeigt und
  scrollt nicht mit den Datenzeilen (Box `vis_n + 3`, Paging unverändert).
- Header-Labels mit Einheiten, wo die Spaltenbreite reicht: `RANGE km`,
  `SPD kt`, `DEP m`, `BRG°`/`HDG°`; schmale Spalten fallen deterministisch
  auf die nackten Kürzel zurück.

**Tests**: 3 neue (`TestTableHeader`) — Einheiten breit, hdr-Stil/Zeilen-
breite über 40–140 Spalten, Textmodus behält Header. Suite: 66 grün.

### 2026-08-23 — ai_tactical: Mast-Schema im OWN-SHIP-Rahmen

**Neu**

- Segeltuch-Schema mit 6 Mast-Slots: Balken starten in der Rumpffläche und
  ragen nach oben; Füllhöhe = Ausfahrlänge bei **fester 5-m-Skala** (max.
  4 Zeilen), exakte Ausrichtung auf die Slot-Zentren (berechnete Spalten,
  kein Freihand-ASCII). Eingefahren = dimmer Stub, Raised-ohne-Höhe = Stub
  + `?`-Label (nie fabrizierte Werte).
- Snorkel-Kopf als kleines Quadrat auf der Balkenspitze: grün bei
  `snorkel_exposed` (über Wasser), dunkelblau (`blue_dim`, neues Style-Token)
  unterwasser. Snorkel-Zeile darunter: Zustand (`down`/`up`/`up·exp`),
  `HV` head valve, `HL` intake hole, `VV` intake volume, SCALE-Hinweis wenn
  breit genug; kompakte Variante <44 Spalten.
- Neue Frame-Felder: `player.masts[]` (`id/type/status/height`, IDs aus
  `mast_ids` mit Fallback-Scan 0–5) plus `snorkel_head_valve`,
  `snorkel_intake_hole`, `snorkel_intake_volume`.
- Renderer `render_mast_schema(frame, width)` als Pure Function; eingebunden
  in rechte Spalte, gestapelten Modus und Text-/JSON-Ausgabe; Degrade auf
  `[]` unter 25 Spalten.

**Tests**: 10 neue (`TestMastSchema`) — Alignment, Füllzählung pro Spalte,
Kopf-Farbwechsel, Labels/Readout, Schmalbreite, Integration aller drei
Ausgabepfade. Suite: 63 grün.

### 2026-08-23 — ai_tactical: Live-Verifikationsrunde 2+3

**Behoben**

- **Kontakt #13 verschwand aus der Tabelle:** `ship_state.player.id` zeigt live
  auf ein fremdes Element (13 = feindlicher DDG), während `player_id`/`identity.id`
  korrekt 9 liefern. `own_element_ids()` behandelt `player_id` jetzt als
  autoritativ; Sekundärquellen zählen nur bei Übereinstimmung.
- **Element 0 (contextless Host-Modul)** erzeugte eine Junk-Zeile und verdrängte
  echte Kontakte aus dem Sichtfenster; es wird jetzt überall gefiltert
  (Tabelle, asg/ext-Rotation, Presence-Quelle).
- **asg/ext-Rotation rotierte nie:** Result-Timestamps sind `HH:MM:SS`-only,
  `parse_ts` lieferte `None` → immer derselbe Kandidat (live ~90× `asg id=0`).
  Rotation-Alter kommt jetzt vom Ingest-Wallclock.
- **cmdid-Wiederverwendung:** Der Probe leert `ship_orders.json` nach der
  Verarbeitung; cmdids starteten wieder bei 0 und kollidierten mit Pending-
  Zwecken. Collector-seitiger Monotonie-Floor (`send_commands(..., floor=)`).
- **Scroll-Fenster** folgt der Auswahl mit der echten sichtbaren Zeilenzahl
  statt `max(3, n)`.
- **THREATS-Frame ragte in die Own-Ship-Spalte:** THREATS-/DETAIL-Boxen werden
  im Side-by-Side-Modus auf die linke Spalte begrenzt; Threat-Bar wrappt jetzt
  auf schmale Breiten (`_wrap_segs`, granulare Segmente); DATA-Ages nur noch
  im Footer.

**Hinzugefügt**

- Geschwindigkeiten in **Knoten**: Probe liefert m/s (Unity-Konvention); alle
  `kt`-gelabelten Anzeigen gehen durch `_kt()` (×1.94384) — KI-Tabelle,
  Detail, OWN SHIP (Panel + Seitenspalte), AI-Kontakte.
- **Detected-Automatik**: Basiskadenz `--detect-interval` (min 10 s, default
  10 s) plus Event-Trigger bei Signaturwechsel eines Elements
  (Range/Kurs/Speed/EOT/Assignment/Orders/Prep/Kontaktzahl).
- **Ghost-Discovery**: automatischer `ns-dump` beim Kaltstart, Wiederholung
  alle 10 Zyklen bis ein Helo/Sub-Style im Probe-Log auftaucht.
- **Layout ≥100 Spalten**: OWN SHIP als rechte Instrumentspalte
  (`render_own_ship_side`); darunter weiter gestapelt.
- **Dunkelgrün heller**: `A_DIM` entfernt, Green-Brightening via `init_color`
  wo das Terminal Farbdefinitionen erlaubt.
- **NDJSON-Stream**: `--json --count N>1` gibt einen Frame pro Poll als
  Zeile aus (Automation/tests ohne TTY).
- `BRIEF_ns_dump_multihost.md` für den ship-probe-Agenten (Multi-Host-
  Antwort des ns-dump + deterministische Element-Enumeration).

**Erkenntnisse (live, remote Mission "AI Attack Test")**

- `detected` läuft automatisch; #17 trackt den Spieler (rot, YES + Range).
- ns-dump antwortet aktuell nur vom Player/FULL-Host (`/0/ general`,
  `/13/`,`/16/`,`/17/` ship) — Helo/Sub-Zeilen bleiben aus; Analyse +
  Abnahmekriterien im Brief oben. 154 Alt-`asg id=0` wurden manuell aus der
  Remote-Order-Queue entfernt.

### 2026-08-22 — ai_tactical: initiale curses-TUI

- `ai_tactical.py`: htop-artige TUI über dem Dateiprotokoll; Merge aller
  KI-Quellen (`ai_state.json`, `detected`/`asg`/`ai-contacts`-Results,
  ns-Style-Ghosts, optionale `datalink_presence.json`) in eine Tabelle.
- Panels: OWN SHIP, AI CONTACTS (scrollbar), THREATS / DETAIL #id (TAB),
  Footer mit Poll-/Data-Ages; ACS-Rahmen, grüne Phosphor-Basis mit
  semantischen Akzentfarben (rot/gelb/cyan/magenta/blau), `c` toggle,
  `--no-color` Mono-Degrade.
- Eingabe entkoppelt vom Pollen (~150 ms Tastatur-Latenz), Order-Queue gegen
  Überlappung geschützt (`planes`-Refresh, `detected`, `asg`/`ai-state`-
  Rotation TTL-basiert, `ns-dump`), `--read-only`.
- Waffen-DB statisch (052D/054A/Akula/Z-9C/Virginia), NaN/Infinity-Sanitizer,
  Narrow-Width-Degrade, `--json` Single-Frame.
- Testsuite `tests/test_ai_tactical.py`; Fixtures aus Live-Daten.
