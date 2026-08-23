# Changelog — cli-gui ("Pilot-Station")

Alle nennenswerten Änderungen dieses Teilprojekts. Format angelehnt an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/).

## [Unreleased]

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
