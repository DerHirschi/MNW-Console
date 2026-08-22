#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MNW Ship Probe Console - external control CLI.

Talks to the in-game ship_probe.py via file protocol (no network):

  <log_dir>/ship_state.json     <- probe writes (read this)
  <log_dir>/ship_probe.json     <- probe API discovery map
  <log_dir>/ship_orders.json    -> probe reads (write this)
  <log_dir>/ship_results.json   <- probe writes command results
  <log_dir>/ship_probe_log.txt  <- probe event log (tail)

The probe tracks/controls the PLAYER element only.

Usage:
  python3 console.py --game-root <MNW install>              interactive REPL
  python3 console.py --game-root <MNW install> state        one-shot
  python3 console.py --game-root <MNW install> helm 045 15
  python3 console.py --log-dir <dir> watch 2

Commands (REPL + one-shot):
  state                       show current player state
  probe                       re-run API discovery (ship_probe.json)
  watch [interval_s] [count]  auto-refresh state
  helm COURSE [EOT] [DEPTH]   set course (deg), EOT order, depth (m)
  plot LAT LON                plot a route to the position
  clear-plot                  clear the current plot
  report                      trigger ReportToHQ
  ai-attack ID [--registry-only] [--allow-untracked] [--domain X]
  ai [ID] [--registry-only]   list AI elements / show one element
  steer ID LAT LON [--speed K] move AI element ID to position (Transit)
  wc ID                       dump AI element WeaponController internals
masts [raise|retract|...]      mast control (no arg=show state; raise/retract
                            <id>; retract-all; raise-all; height <id> <frac>;
                            periscope <id> <frac>; snorkel raise|retract)
  diag                        one-shot diagnostic (results + markers + tail)
results [N]                 show command results
  log     [N]                 tail ship_probe_log.txt
  status                      show probe status
  help
  quit / exit

EOT orders: Stop, AheadStd, Ahead13, AheadFull, AheadFlank
"""

import argparse
import io
import json
import os
import sys
import time

from mnw_admin import (
    _short_type,
    atomic_write,
    grep_log,
    monitor_element,
    next_cmdid,
    read_ai_state,
    read_json,
    read_results,
    send_ai_attack,
    summarize_elements,
    tail_log,
)

STATE_FILE = "ship_state.json"
PROBE_FILE = "ship_probe.json"
ORDERS_FILE = "ship_orders.json"
RESULTS_FILE = "ship_results.json"
LOG_FILE = "ship_probe_log.txt"
LOCK_FILE = "ship_probe.lock"

_EOT_NAMES = ("Stop", "Ahead13", "Ahead23", "AheadStd", "AheadFull", "AheadFlank",
              "Astern13", "Astern23", "AsternFull", "AsternEmer")

_PLANE_USAGE = ("planes [fwd ANGLE | stern ANGLE | rudder ANGLE | rudder release | "
                "bubble on|off | bubble ANGLE | bubble release | autotrim on|off | "
                "bow RETRACT|EXTEND | lockfwd on|off | lockint on|off]")

TANKS_USAGE = ("tanks [vent | flood | drain | blow | charge | blower | bank N |  "
               "pump [N on|off] | rpm [N] RPM | tdrain | tflood | ttransfer | "
               "tcirc | valve N open|close | fvalve [read|open|close|ratio N] |  "
               "fill [TANKS...] | drainall [TANKS...] | tctl METHOD [ARGS...]]  "
               "(no arg = read-only ballast/trim probe; writes are state-changing)")
ENV_USAGE = ("env [ssp]  "
             "(no arg = read-only EnvironmentalSystem scan; ssp reads SSP/TP/Analysis properties)")
ALARM_USAGE = ("alarm [alarms | rigging | integrity | brute]  "
               "(no arg = scan; integrity = live damage state; brute = 200+ types)")
DC_USAGE = ("dc [status | bulkheads close|open | bulkhead <0-9> close|open | "
            "fire <0-9> | extinguish <0-9> | flood <0-9> | deflood <0-9>]  "
            "(damage control: bulkheads proven safe, fire/flood experimental)")


def resolve_log_dir(args):
    if getattr(args, "log_dir", None):
        return args.log_dir
    if getattr(args, "game_root", None):
        return os.path.join(args.game_root, "Var", "Scripts", "Execute", "_Source")
    return os.getcwd()


def read_json(path):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except IOError:
        return None
    except Exception as e:
        return {"__error__": "%s: %s" % (type(e).__name__, str(e))}


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    if os.path.isfile(path):
        os.remove(path)
    os.rename(tmp, path)


def next_cmdid(log_dir):
    """Next free cmdid. Counts both the pending orders file AND the results
    file, so a stale result (same cmdid as a fresh command) can never be
    matched again - the cmdid space only moves forward."""
    seen = set()
    for fname in (ORDERS_FILE, RESULTS_FILE):
        data = read_json(os.path.join(log_dir, fname))
        if not isinstance(data, dict):
            continue
        coll = data.get("commands")
        if coll is None:
            coll = data.get("results")
        if not isinstance(coll, list):
            continue
        for c in coll:
            if isinstance(c, dict):
                try:
                    seen.add(int(c.get("cmdid", 0)))
                except (ValueError, TypeError):
                    pass
    return max(seen) + 1 if seen else 0


def send_commands(log_dir, commands):
    if not commands:
        return 0
    cmds = []
    cmdid = next_cmdid(log_dir)
    for c in commands:
        c = dict(c)
        c["cmdid"] = cmdid
        cmds.append(c)
        cmdid += 1
    data = read_json(os.path.join(log_dir, ORDERS_FILE))
    existing = data.get("commands", []) if isinstance(data, dict) and isinstance(data.get("commands"), list) else []
    atomic_write(os.path.join(log_dir, ORDERS_FILE), {"commands": existing + cmds})
    return len(cmds)


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _fmt(v, digits=2, unit=""):
    if v is None:
        return "?"
    try:
        if isinstance(v, (int, float)):
            return ("%.*f%s" % (digits, v, unit)).rstrip("0").rstrip(".")
    except Exception:
        pass
    return str(v)


def print_state(d):
    if d is None:
        print("(no ship_state.json - is the game running the ship probe?)")
        return 1
    if isinstance(d, dict) and d.get("__error__"):
        print("state file error: %s" % d["__error__"])
        return 1
    print("== ship state (ts=%s) ==" % d.get("ts", "?"))
    p = d.get("player") or {}
    print("player: id=%s is_player=%s source=%s" % (
        p.get("id", "?"), p.get("is_player"), p.get("source", "?")))
    i = d.get("identity") or {}
    if i:
        print("identity: %s | cat=%s | assignment=%s | country=%s" % (
            i.get("name", "?"), i.get("category", "?"),
            i.get("assignment", "?"), i.get("country", "?")))
    n = d.get("navigation") or {}
    ll = n.get("lat_lon") or []
    if isinstance(ll, list) and len(ll) > 1:
        lat_s, lon_s = _fmt(ll[0], 3), _fmt(ll[1], 3)
    else:
        lat_s = lon_s = "?"
    print("nav: pos=%s,%s | hdg=%s | spd=%s | elev=%s | depth=%s | bot=%s | rpm=%s" % (
        lat_s, lon_s,
        _fmt(n.get("heading"), 0), _fmt(n.get("speed"), 1),
        _fmt(n.get("altitude"), 1), _fmt(n.get("depth"), 1),
        _fmt(n.get("bottom_range"), 1), _fmt(n.get("_currentrpm"), 0)))
    s = d.get("systems") or {}
    if s:
        flags = []
        for label, key in (("fire", "integrity_on_fire"), ("flood", "integrity_flooding"),
                           ("sunk", "integrity_sunk")):
            if s.get(key) is not None:
                flags.append("%s=%s" % (label, "yes" if s[key] else "no"))
        if flags:
            flags = " | " + " ".join(flags)
        else:
            flags = ""
        print("systems: damage=%s | off=%s | def=%s | towed=%s%s" % (
            _fmt(s.get("integrity_damage_ratio"), 3), _fmt(s.get("ammo_offensive_ratio"), 3),
            _fmt(s.get("ammo_defensive_ratio"), 3), s.get("towed_array", "?"), flags))
        for k in ("mast_controller_status", "mast_ids"):
            if s.get(k) is not None:
                print("  %s: %s" % (k, s[k]))
        for k in ("snorkel_raised", "snorkel_exposed", "snorkel_head_valve",
                  "snorkel_intake_hole", "snorkel_intake_volume",
                  "periscope_depth", "surface_depth", "standard_depth",
                  "max_operational_depth"):
            if s.get(k) is not None:
                print("  %s: %s" % (k, _fmt(s[k], 1) if isinstance(s[k], (int, float)) else s[k]))
        for k in ("mast_0_type", "mast_0_status", "mast_0_height",
                  "mast_1_type", "mast_1_status", "mast_1_height"):
            if s.get(k) is not None:
                print("  %s: %s" % (k, s[k]))
    b = d.get("blackboard") or {}
    print("ordered: course=%s | eot=%s | depth=%s | wp=%s | wp_idx=%s" % (
        _fmt(n.get("_orderedcourse"), 0), n.get("_orderedeotorder", "?"),
        _fmt(n.get("_ordereddepth"), 1), _fmt(n.get("plot_count"), 0),
        b.get("_waypointiterator", "?")))
    c = d.get("clock") or {}
    if c:
        print("clock: %s | scale=%s" % (c.get("time", "?"), _fmt(c.get("scale"), 1)))
    m = d.get("mission") or {}
    if m:
        print("mission: %s | op=%s | tension=%s | datetime=%s" % (
            m.get("name", "?"), m.get("operation", "?"), m.get("tension", "?"), m.get("datetime", "?")))
    raw_contacts = d.get("contacts") or {}
    contacts = raw_contacts.get("tracks") if isinstance(raw_contacts, dict) else raw_contacts
    contacts = contacts or []
    if contacts:
        print("-- contacts (%d) --" % len(contacts))
        for e in contacts[:30]:
            print("  %-10s %-9s rng=%-9s brg=%-7s crs=%-6s spd=%-5s id=%s" % (
                e.get("type", "?"), e.get("identity", "?"),
                _fmt(e.get("range"), 0, "m"), _fmt(e.get("bearing"), 1),
                _fmt(e.get("course"), 0), _fmt(e.get("speed"), 1), e.get("id", "?")))
    son = d.get("sonar") or {}
    if son:
        print("sonar: %s" % son)
    return 0


def format_damage(s):
    """Render the systems' integrity/compartment damage section into display
    lines. Pure helper (no IO) so the console 'damage' command and tests share
    exactly one rendering path."""
    if not s:
        return ["  (no systems data)"]
    if s.get("integrity_damage_ratio") is None:
        return ["  (no integrity data - ship lacks Integrity component?)"]
    lines = ["  damage=%-8s oper=%-8s hull=%-8s stress=%-8s tanks=%-8s sink=%-8s plate=%s" % (
        _fmt(s.get("integrity_damage_ratio"), 4),
        _fmt(s.get("integrity_operational_ratio"), 4),
        _fmt(s.get("integrity_hull_ratio"), 4),
        _fmt(s.get("integrity_hull_stress"), 4),
        _fmt(s.get("integrity_tanks_ratio"), 4),
        _fmt(s.get("integrity_sunk_ratio"), 4),
        _fmt(s.get("integrity_plate_strength"), 4))]
    flags = []
    for label, key, yes, no in (
            ("fire", "integrity_on_fire", "yes", "no"),
            ("flooding", "integrity_flooding", "yes", "no"),
            ("sunk", "integrity_sunk", "yes", "no")):
        v = s.get(key)
        if v is not None:
            flags.append("%s=%s" % (label, yes if v else no))
    ntanks = s.get("integrity_tanks")
    if ntanks is not None:
        nfire = nflood = nbulk = 0
        for i in range(int(ntanks)):
            pref = "tank_%d" % i
            if s.get(pref + "_fire"):
                nfire += 1
            if s.get(pref + "_flooding"):
                nflood += 1
            if s.get(pref + "_bulkhead"):
                nbulk += 1
        flags.append("tanks=%s (%s fire, %s flooding, %s bulkheads shut)" % (
            ntanks, nfire, nflood, nbulk))
    if flags:
        lines.append("  %s" % " | ".join(flags))
    for i in range(int(ntanks or 0)):
        pref = "tank_%d" % i
        bits = []
        lv = s.get(pref + "_level")
        if lv is not None:
            bits.append("level=%s" % _fmt(lv, 3))
        for label, key, open_s, shut_s in (
                ("bulkhead", pref + "_bulkhead", "open", "shut"),
                ("fire", pref + "_fire", "yes", "no"),
                ("flooding", pref + "_flooding", "yes", "no")):
            v = s.get(key)
            if v is not None:
                bits.append("%s=%s" % (label, open_s if v else shut_s))
        for label, key in (("ok", pref + "_comps_ok"), ("malf", pref + "_comps_malf"),
                           ("dmg", pref + "_comps_dmg"), ("other", pref + "_comps_other")):
            v = s.get(key)
            if v is not None:
                bits.append("%s=%d" % (label, int(v)))
        damaged = s.get(pref + "_damaged")
        if damaged:
            bits.append("damaged: %s" % ", ".join(str(d) for d in damaged[:8]))
        lines.append("  tank %d: %s" % (i, " | ".join(bits)))
    return lines


def cmd_damage(log_dir):
    """Read-only damage view: renders the always-on integrity section that
    read_systems() in the probe polls into ship_state.json (systems.*). No
    probe round-trip - the data is refreshed by the probe's state cycle."""
    d = read_json(os.path.join(log_dir, STATE_FILE))
    if d is None or (isinstance(d, dict) and d.get("__error__")):
        print_state(d)
        return 1
    print("== damage (ts=%s) ==" % d.get("ts", "?"))
    for line in format_damage((d.get("systems") or {})):
        print(line)
    return 0


def _fmt_sonar_contact(c, idx):
    print("  [%d] brg=%-8s rng=%-9s sgn=%-8s nse=%-8s sns=%-8s dpl=%-6s crs=%-6s spd=%-5s %s %s" % (
        idx, _fmt(c.get("bearing"), 1), _fmt(c.get("range"), 0, "m"),
        _fmt(c.get("signal"), 1), _fmt(c.get("noise"), 1), _fmt(c.get("self_noise"), 1),
        _fmt(c.get("doppler"), 3), _fmt(c.get("course"), 0), _fmt(c.get("speed"), 1),
        c.get("category", ""), c.get("id", "")))


def cmd_sonar(log_dir, detail=False):
    """Evaluate the player's sonar contacts from ship_state.json.

    Uses the `sonar` (SonarSystem tracker contacts via GetContactIDs/GetTrackerData)
    and `sonar_arrays` (per-array contact list) sections."""
    st = read_json(os.path.join(log_dir, STATE_FILE))
    if not isinstance(st, dict):
        print("(no ship_state.json - is the game running the probe?)")
        return 1
    son = st.get("sonar") or {}
    arrays = st.get("sonar_arrays") or {}
    if son.get("disabled"):
        print("sonar: read disabled in probe config (read_sonar:false)")
    elif son.get("err"):
        print("sonar: %s" % son["err"])
    elif son.get("count", 0) > 0:
        tracks = son.get("tracks") or []
        print("== sonar tracker (%d contacts) ==" % son["count"])
        for t in tracks[:30]:
            sensors = t.get("sensors") or []
            brg = t.get("bearing")
            rng = t.get("range")
            sensor = t.get("sensor", "?")
            line = "  id=%-6s brg=%-8s rng=%-9s sensor=%s" % (
                t.get("id", "?"), _fmt(brg, 1), _fmt(rng, 0, "m"), sensor)
            if len(sensors) > 1:
                extras = ["%s=%s@%sm" % (s.get("sensor","?"), _fmt(s.get("bearing"),1), _fmt(s.get("range"),0,"m")) for s in sensors if s.get("sensor") != sensor]
                if extras:
                    line += " (%s)" % ", ".join(extras[:3])
            print(line)
    else:
        print("sonar: no contacts")
    alist = arrays.get("arrays") if isinstance(arrays, dict) else None
    if alist is None and isinstance(arrays, dict) and arrays.get("disabled"):
        print("sonar_arrays: read disabled in probe config (read_sonar_arrays:false)")
        return 0
    if not alist:
        print("sonar_arrays: none")
        return 0
    print("== sonar arrays (%d) ==" % len(alist))
    for a in alist:
        ainfo = "  %s%s status=%s freq=%s aov=%s course=%s" % (
            a.get("type", "?"), " idx=%d" % a.get("index", "?") if "index" in a else "",
            a.get("status", "?"), _fmt(a.get("design_frequency"), 0),
            _fmt(a.get("aov"), 0), _fmt(a.get("course"), 0))
        print(ainfo)
        contacts = a.get("contacts") or []
        if not contacts:
            print("    (no contacts)")
            continue
        print("    %d contact(s):" % a.get("contact_count", len(contacts)))
        for i, c in enumerate(contacts[:20]):
            _fmt_sonar_contact(c, i)
            if detail:
                for k in ("relative_bearing", "elevation", "flow_noise", "ambient_noise",
                          "thermal_noise", "database_id", "beam_type", "nan"):
                    if c.get(k) is not None:
                        print("      %s=%s" % (k, _fmt(c[k], 3) if isinstance(c[k], (int, float)) else c[k]))
        if len(contacts) > 20:
            print("    ... %d more" % (len(contacts) - 20))
    return 0


EXPLORE_FILE = "ship_explore.json"

def cmd_explore(log_dir):
    """Read ship_explore.json (written by do_explore) and print a human-readable summary."""
    path = os.path.join(log_dir, EXPLORE_FILE)
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (IOError, OSError, ValueError):
        print("(no ship_explore.json — run 'explore' first, wait for probe cycle)")
        return 1
    # Save a local copy in the working directory
    try:
        local_copy = os.path.join(os.getcwd(), EXPLORE_FILE)
        with io.open(local_copy, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    if not isinstance(data, dict):
        print("(invalid ship_explore.json)")
        return 1
    print("=== FULL EXPLORATION DUMP (%s) ===" % data.get("ts", "?"))
    # --- Player Element ---
    pe = data.get("player_element")
    if pe:
        print("\n-- Player Element dir() [%d attrs] --" % len(pe.get("attrs", [])))
        for a in pe.get("attrs", []):
            print("  %s: %s" % (a["name"], a.get("val", "?")))
        calls = pe.get("callables", [])
        if calls:
            print("  [%d callables: %s]" % (len(calls), ", ".join(calls[:20])))
    # --- Access[T] components ---
    access = data.get("access", {})
    if access:
        for tname, tdata in sorted(access.items()):
            if isinstance(tdata, dict) and tdata.get("error"):
                continue
            attrs = tdata if isinstance(tdata, dict) else {}
            prop_list = attrs.get("properties", [])
            call_list = attrs.get("callables", [])
            print("\n-- Access[%s] [%d props, %d callables] --" % (
                tname, len(prop_list), len(call_list)))
            for a in prop_list:
                print("  %s: %s" % (a["name"], a.get("val", "?")))
            if call_list:
                print("  [%d callables: %s]" % (len(call_list), ", ".join(call_list[:15])))
    # --- Blackboard ---
    bb = data.get("blackboard")
    if bb:
        keys = bb.get("keys", [])
        print("\n-- Blackboard [%d keys] --" % len(keys))
        for k in keys:
            vr = bb.get("values", {}).get(k)
            tag = " <<" if any(x in k.lower() for x in ("bearing", "sonar", "audio", "headphone")) else ""
            print("  %s: %s%s" % (k, vr if vr is not None else "?", tag))
    # --- SonarSystem ---
    ss = data.get("sonar_system")
    if ss:
        prop_list = ss.get("properties", [])
        call_list = ss.get("callables", [])
        print("\n-- SonarSystem [%d props, %d callables] --" % (len(prop_list), len(call_list)))
        for a in prop_list:
            print("  %s: %s" % (a["name"], a.get("val", "?")))
        if call_list:
            print("  [%d callables: %s]" % (len(call_list), ", ".join(call_list[:15])))
        sonars = ss.get("sonars", [])
        if sonars:
            print("  Sonars: %d items" % len(sonars))
            for si, s in enumerate(sonars):
                print("    [%d] %s" % (si, s))
        cc = ss.get("cached_contacts", [])
        if cc:
            print("  CachedContacts: %d items" % len(cc))
            for c in cc[:5]:
                if isinstance(c, dict):
                    print("    key=%s %s" % (c.get("key", "?"), c.get("attrs", "")))
    # --- Summary ---
    summary = data.get("summary", {})
    if summary:
        print("\n-- Summary --")
        for k, v in summary.items():
            print("  %s: %s" % (k, v))
    print("\nFull data: %s" % path)
    return 0


def cmd_masts(log_dir):
    """Show the player's mast / snorkel / periscope state from ship_state.json
    (systems section: mast_*, snorkel_*, *_depth, towed_array)."""
    st = read_json(os.path.join(log_dir, STATE_FILE))
    if not isinstance(st, dict):
        print("(no ship_state.json - is the game running the probe?)")
        return 1
    s = st.get("systems") or {}
    if not s:
        print("(no systems section in ship_state.json)")
        return 1
    print("== masts / periscope ==")
    print("controller: status=%s | ids=%s%s" % (
        s.get("mast_controller_status", "?"), s.get("mast_ids", "?"),
        (" (%s)" % s["mast_ids_err"]) if s.get("mast_ids_err") else ""))
    for k in sorted(s):
        if k.startswith("mast_") and k not in ("mast_ids", "mast_controller_status", "mast_ids_err", "mast_ids_source"):
            v = s[k]
            print("  %-22s %s" % (k, _fmt(v, 1) if isinstance(v, (int, float)) else v))
    print("-- snorkel --")
    for k in ("snorkel_raised", "snorkel_exposed", "snorkel_head_valve",
              "snorkel_intake_hole", "snorkel_intake_volume"):
        if s.get(k) is not None:
            print("  %-22s %s" % (k, _fmt(s[k], 2) if isinstance(s[k], (int, float)) else s[k]))
    print("-- depths --")
    for k in ("periscope_depth", "surface_depth", "standard_depth",
              "max_operational_depth", "ordered_depth"):
        if s.get(k) is not None:
            print("  %-22s %s" % (k, _fmt(s[k], 2)))
    if s.get("towed_array") is not None:
        print("towed array: %s" % s["towed_array"])
    return 0


def _fmt_plane_list(v):
    if not isinstance(v, list):
        return str(v)
    return "[%s]" % ", ".join(_fmt(x, 2) if isinstance(x, (int, float)) else str(x) for x in v)


def cmd_planes_state(log_dir):
    """Read-only view of the player's steering/control-surface state from the
    `steering` section of ship_state.json (written every probe tick)."""
    st = read_json(os.path.join(log_dir, STATE_FILE))
    if not isinstance(st, dict):
        print("(no ship_state.json - is the game running the probe?)")
        return 1
    s = st.get("steering") or {}
    if not s:
        print("(no steering section in ship_state.json - probe too old or read_steering:false)")
        return 1
    if s.get("err"):
        print("steering err: %s" % s["err"])
    print("== control surfaces ==")
    print("ordered:  eot=%s  speed=%s kt  heading=%s deg  depth=%s m" % (
        _fmt(s.get("ordered_eot"), 0), _fmt(s.get("ordered_speed"), 2),
        _fmt(s.get("ordered_heading"), 1), _fmt(s.get("ordered_depth"), 1)))
    print("planes:   fwd=%s  stern=%s  rudder=%s  type=%s" % (
        _fmt_plane_list(s.get("forward_plane_angles")), _fmt_plane_list(s.get("stern_plane_angles")),
        _fmt_plane_list(s.get("rudder_plane_angles")), s.get("forward_planes_type", "?")))
    print("locks:    fwd=%s  int-stern=%s  bow_retracted=%s" % (
        s.get("forward_planes_locked"), s.get("int_stern_planes_locked"),
        s.get("bow_planes_retracted")))
    print("bubble:   autotrim=%s  max_plane_rate=%s deg/s  steering_mode=%s" % (
        s.get("auto_trim"), _fmt(s.get("max_plane_rate_of_turn"), 1), s.get("steering_mode")))
    print("depths:   periscope=%s  standard=%s  max_op=%s  surface=%s m  bands=%s" % (
        _fmt(s.get("periscope_depth"), 1), _fmt(s.get("standard_depth"), 1),
        _fmt(s.get("max_operational_depth"), 1), _fmt(s.get("surface_depth"), 1), s.get("depth_bands")))
    print("drive:    tpk=%s  stw=%s kt  default_eot=%s" % (
        _fmt(s.get("tpk"), 1), _fmt(s.get("stw"), 1), _fmt(s.get("default_eot"), 0)))
    return 0


def cmd_ai(log_dir, nid=None, registry_only=False):
    st = read_ai_state(log_dir)
    if not isinstance(st, dict) or st.get("__error__"):
        print("(no ai_state.json - is the game running the probe?)")
        return 1
    if nid is None:
        return print_elements(summarize_elements(st))
    el = None
    for e in st.get("elements", []):
        if e.get("id") == nid:
            el = e
            break
    if el is None:
        print("element %d not in ai_state.json" % nid)
        return 1
    ca = el.get("current_assignment") or {}
    iord = el.get("incoming_order") or {}
    ll = el.get("lat_lon") or []
    asg_t = _short_type(ca.get("type")) or "?"
    print("== AI element %d ==" % nid)
    print("identity:   name=%s | country=%s | category=%s" % (
        el.get("name", "?"), el.get("country", "?"), el.get("category", "?")))
    print("position:   lat=%.4f lon=%.4f | rng=%.1f km | brg=%.0f" % (
        ll[0] if len(ll) > 1 else -1, ll[1] if len(ll) > 1 else -1,
        el.get("to_player_range_km") or 0, el.get("to_player_bearing") or 0))
    print("movement:   hdg=%.0f | spd=%.1f | depth=%s" % (
        el.get("true_heading") or 0, el.get("true_speed") or 0, _fmt(el.get("depth"), 1)))
    print("assignment: id=%s | type=%s | contacts=%s | prep=%s" % (
        _fmt(el.get("assignment_id"), 0), asg_t, el.get("contact_count", "?"),
        el.get("action_prep_complete", "?")))
    print("incoming:   assignment_id=%s" % (iord.get("assignment_id", iord.get("assignment_id_err", "?"))))
    if registry_only:
        print("diag: sending ai-attack registry_only=True on element %d ..." % nid)
    return 0


def cmd_diag(log_dir):
    """One-shot diagnostic: last results + ai-attack markers + tail."""
    st = read_ai_state(log_dir)
    print("== diag %s ==" % time.strftime("%H:%M:%S"))
    print("ai_state: %s elements" % (len(st.get("elements", [])) if isinstance(st, dict) else 0))
    cmd_results(log_dir, 5)
    for pat in ("ai-attack cp0", "ai-attack cpa", "ai-attack cpb",
                "ai-attack cp2", "ai-attack cp5", "ai-attack cp10",
                "ai-attack cp13", "PushOrder ok"):
        hits = grep_log(log_dir, pat, limit=3)
        for h in hits:
            print("  LOG: %s" % h)
    return 0


def print_elements(elements):
    if not elements:
        print("(no AI elements)")
        return 1
    print("%-4s %-18s %-8s %-8s %-6s %-6s %-6s %-9s %-8s" % (
        "id", "name", "type", "rng_km", "spd", "crs", "asg", "asg_type", "contacts"))
    for e in elements:
        print("%-4s %-18s %-8s %-8s %-6s %-6s %-6s %-9s %-8s" % (
            e.get("id"), (e.get("name") or "?")[:18], (e.get("category") or "?")[:8],
            _fmt(e.get("range_km"), 1), _fmt(e.get("speed"), 1), _fmt(e.get("course"), 0),
            _fmt(e.get("assignment_id"), 0), (e.get("assignment_type") or "?")[:9],
            _fmt(e.get("contacts"), 0)))
    return 0


def cmd_watch(log_dir, interval, count):
    i = 0
    while count is None or i < count:
        os.system("clear" if os.name == "posix" else "cls")
        print_state(read_json(os.path.join(log_dir, STATE_FILE)))
        i += 1
        if count is None or i < count:
            time.sleep(interval)


def cmd_probe_file(log_dir):
    d = read_json(os.path.join(log_dir, PROBE_FILE))
    if not isinstance(d, dict) or d.get("__error__"):
        print("(no ship_probe.json yet - run 'probe' inside the game or wait)")
        return 1
    print("== API discovery (ts=%s, host=%s) ==" % (d.get("ts", "?"), d.get("host_script", "?")))
    comps = d.get("components") or {}
    for key in sorted(comps):
        c = comps[key]
        print("  %-22s %-10s %s" % (key, c.get("status"), c.get("type", "")))
    keys = d.get("blackboard_keys") or []
    if keys:
        print("-- blackboard keys (%d) --" % len(keys))
        for k in sorted(keys):
            print("  %s" % k)
    errs = d.get("errors") or []
    if errs:
        print("-- errors --")
        for e in errs[-10:]:
            print("  %s" % e)
    return 0


def cmd_results(log_dir, n=10):
    data = read_json(os.path.join(log_dir, RESULTS_FILE))
    if not isinstance(data, dict):
        print("(no ship_results.json yet)")
        return 1
    for r in (data.get("results") or [])[-n:]:
        print("[%s] #%s %s: %s" % (r.get("ts"), r.get("cmdid"), r.get("action"), r.get("result")))
        for ln in (r.get("detail") or [])[-4:]:
            print("    %s" % ln)
    return 0


def cmd_result_for(log_dir, cmdid, wait=3.0, interval=0.5, action=None):
    """Wait up to `wait` s for the probe to answer a command and print its
    full result/detail for the given cmdid from ship_results.json.

    When `action` is given, only a result whose action matches is accepted -
    stale results from earlier commands with the same cmdid are skipped.

    Returns 0 on found, 1 on timeout. ship_results.json is last-writer-wins
    across hosts, so for AI-side commands cross-check ship_probe_log.txt."""
    deadline = time.time() + wait
    while time.time() < deadline:
        data = read_json(os.path.join(log_dir, RESULTS_FILE))
        if isinstance(data, dict):
            for r in (data.get("results") or []):
                try:
                    if int(r.get("cmdid")) != int(cmdid):
                        continue
                    if action is not None and str(r.get("action")) != action:
                        continue
                    print("[%s] #%s %s: %s" % (r.get("ts"), r.get("cmdid"), r.get("action"), r.get("result")))
                    for ln in (r.get("detail") or []):
                        print("    %s" % ln)
                    return 0
                except (ValueError, TypeError):
                    continue
        time.sleep(interval)
    print("(no result for cmdid %s yet - check 'log' / ship_probe_log.txt)" % cmdid)
    return 1


def cmd_action_dump(log_dir, cmd_dict, wait=12.0):
    """Queue a control command and print its FULL result/detail lines once the
    probe answered. tanks/env produce their useful output only in the detail
    (SSP arrays, MBT/TnC levels, bank states), not in ship_state.json."""
    cmdid = next_cmdid(log_dir)
    n = send_commands(log_dir, [cmd_dict])
    print("queued %d command(s) - waiting for probe cycle..." % n)
    return cmd_result_for(log_dir, cmdid, wait=wait, action=str(cmd_dict.get("action")))


def cmd_log(log_dir, n=20):
    path = os.path.join(log_dir, LOG_FILE)
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except IOError:
        print("(no ship_probe_log.txt)")
        return 1
    for ln in lines[-max(1, n):]:
        print(ln)
    return 0


def cmd_status(log_dir):
    path = os.path.join(log_dir, LOCK_FILE)
    st = read_json(os.path.join(log_dir, STATE_FILE))
    print("lock:  %s" % ("held" if os.path.isfile(path) else "free"))
    if not isinstance(st, dict) or st.get("__error__"):
        print("state: (none yet - game running the probe?)")
        return 1
    print("state: ts=%s" % st.get("ts", "?"))
    return 0


def parse_action(words):
    if not words:
        return None, None
    action = words[0].lower()
    args = words[1:]
    if action == "helm":
        if not args:
            return None, "helm COURSE [EOT] [DEPTH] [--env N] [--snap] [--bubble X] [--autotrim on|off]"
        try:
            course = float(args[0])
        except ValueError:
            return None, "helm COURSE [EOT] [DEPTH]"
        cmd = {"action": action, "course": course}
        pos = args[1:]
        i = 0
        while i < len(pos) and not pos[i].startswith("--"):
            a = pos[i]
            if "eot" not in cmd:
                if a in _EOT_NAMES:
                    cmd["eot"] = a
                    i += 1
                    continue
                return None, "invalid EOT %r (valid: %s)" % (a, ", ".join(_EOT_NAMES))
            if "depth" not in cmd:
                try:
                    cmd["depth"] = float(a)
                    i += 1
                    continue
                except ValueError:
                    return None, "helm COURSE [EOT] [DEPTH]"
            break
        tail = pos[i:]
        if "--env" in tail:
            i = tail.index("--env")
            if i + 1 < len(tail):
                try:
                    cmd["env"] = int(tail[i + 1])
                except ValueError:
                    return None, "helm --env N (N=0 periscope,1 shallow,2 deep,3 maximum)"
        if "--snap" in tail:
            cmd["snap"] = True
        if "--bubble" in tail:
            i = tail.index("--bubble")
            if i + 1 < len(tail):
                try:
                    cmd["bubble"] = float(tail[i + 1])
                except ValueError:
                    return None, "helm --bubble ANGLE"
        if "--autotrim" in tail:
            i = tail.index("--autotrim")
            if i + 1 < len(tail) and tail[i + 1] in ("on", "off"):
                cmd["autotrim"] = tail[i + 1] == "on"
            else:
                return None, "helm --autotrim on|off"
        return cmd, None
    if action == "planes":
        if not args:
            return {"action": action}, None
        sub = args[0].lower()
        rest = args[1:]
        try:
            if sub in ("fwd", "forward"):
                return {"action": action, "fwd": float(rest[0])}, None
            if sub in ("stern",):
                return {"action": action, "stern": float(rest[0])}, None
            if sub == "rudder":
                if rest and rest[0].lower() == "release":
                    return {"action": action, "release_rudder": True}, None
                return {"action": action, "rudder": float(rest[0])}, None
            if sub == "bubble":
                if rest and rest[0].lower() == "release":
                    return {"action": action, "release_bubble": True}, None
                if rest and rest[0].lower() in ("on", "off"):
                    return {"action": action, "bubble_on": rest[0].lower() == "on"}, None
                return {"action": action, "bubble": float(rest[0])}, None
            if sub == "autotrim":
                if rest and rest[0].lower() in ("on", "off"):
                    return {"action": action, "autotrim": rest[0].lower() == "on"}, None
                return None, _PLANE_USAGE
            if sub == "bow":
                if rest and rest[0].lower() in ("retract", "extend"):
                    return {"action": action, "bow": rest[0].lower() == "retract"}, None
                return None, "planes bow RETRACT|EXTEND"
            if sub in ("lockfwd", "lock-forward"):
                if rest and rest[0].lower() in ("on", "off"):
                    return {"action": action, "lockfwd": rest[0].lower() == "on"}, None
                return None, "planes lockfwd on|off"
            if sub in ("lockint", "lock-int"):
                if rest and rest[0].lower() in ("on", "off"):
                    return {"action": action, "lockint": rest[0].lower() == "on"}, None
                return None, "planes lockint on|off"
        except (ValueError, IndexError):
            return None, _PLANE_USAGE
        return None, _PLANE_USAGE
    if action == "plot":
        if len(args) != 2:
            return None, "plot LAT LON"
        try:
            return {"action": action, "lat": float(args[0]), "lon": float(args[1])}, None
        except ValueError:
            return None, "plot LAT LON"
    if action == "clear-plot":
        return {"action": action}, None
    if action == "report":
        return {"action": action}, None
    if action == "probe":
        return {"action": action}, None
    if action == "ai-attack":
        if not args:
            return None, "ai-attack ID [--registry-only] [--allow-untracked] [--domain X]"
        try:
            nid = int(args[0])
        except ValueError:
            return None, "ai-attack ID [--registry-only] [--allow-untracked] [--domain X]"
        cmd = {"action": action, "id": nid}
        if "--registry-only" in args[1:]:
            cmd["registry_only"] = True
        if "--allow-untracked" in args[1:]:
            cmd["allow_untracked"] = True
        if "--domain" in args[1:]:
            di = args[1:].index("--domain")
            if di + 1 < len(args[1:]):
                cmd["domain"] = args[1:][di + 1]
            else:
                return None, "ai-attack ID [--registry-only] [--allow-untracked] [--domain X]"
        return cmd, None
    if action == "steer":
        if len(args) < 3:
            return None, "steer ID LAT LON [--speed KNOTS] [--registry-only]"
        try:
            nid = int(args[0])
            lat = float(args[1])
            lon = float(args[2])
        except ValueError:
            return None, "steer ID LAT LON [--speed KNOTS] [--registry-only]"
        cmd = {"action": action, "id": nid, "lat": lat, "lon": lon}
        if "--speed" in args[3:]:
            si = args[3:].index("--speed")
            if si + 1 < len(args[3:]):
                try:
                    cmd["speed"] = float(args[3:][si + 1])
                except ValueError:
                    return None, "steer ID LAT LON [--speed KNOTS] [--registry-only]"
        if "--registry-only" in args[3:]:
            cmd["registry_only"] = True
        return cmd, None
    if action == "ai":
        if not args:
            return None, "ai [ID] [--watch] [--registry-only]"
        try:
            cmd = {"action": "ai", "id": int(args[0])}
        except ValueError:
            return None, "ai [ID] [--watch] [--registry-only]"
        if "--watch" in args[1:]:
            cmd["watch"] = True
        if "--registry-only" in args[1:]:
            cmd["registry_only"] = True
        return cmd, None
    if action == "wc":
        if not args:
            return None, "wc ID"
        try:
            cmd = {"action": "wc-dump", "id": int(args[0])}
        except ValueError:
            return None, "wc ID"
        return cmd, None
    if action == "sonar":
        cmd = {"action": "sonar"}
        if "--detail" in args:
            cmd["detail"] = True
        return cmd, None
    if action in ("detected", "ai-contacts", "ns-dump"):
        return {"action": action}, None
    if action == "sd-dump":
        return {"action": action}, None
    if action == "tanks":
        sub = args[0].lower() if args else None
        if not sub:
            return {"action": action}, None
        if sub in ("vent", "flood", "drain", "blow", "charge", "blower"):
            return {"action": action, sub: True}, None
        if sub == "bank":
            if len(args) < 2:
                return None, "tanks bank N"
            try:
                return {"action": action, "bank": int(args[1])}, None
            except ValueError:
                return None, "tanks bank N"
        if sub == "pump":
            if len(args) >= 2:
                try:
                    int(args[1])
                    rest = args[2] if len(args) >= 3 else "on"
                    return {"action": action, "pump": "%s %s" % (args[1], rest)}, None
                except ValueError:
                    return {"action": action, "pump": "0 %s" % args[1]}, None
            return {"action": action, "pump": True}, None
        if sub == "rpm":
            if len(args) < 2:
                return None, "tanks rpm N  (or: tanks rpm PUMP N)"
            return {"action": action, "rpm": " ".join(args[1:])}, None
        if sub in ("tdrain", "tflood", "ttransfer", "tcirc"):
            return {"action": action, sub: True}, None
        if sub == "fvalve":
            if len(args) < 2:
                return None, "tanks fvalve [read|open|close|ratio N]"
            return {"action": action, "fvalve": " ".join(args[1:])}, None
        if sub in ("fill", "drainall"):
            if len(args) == 1:
                return {"action": action, sub: "all"}, None
            tanks = " ".join(args[1:])
            return {"action": action, sub: tanks}, None
        if sub == "valve":
            if len(args) < 3:
                return None, "tanks valve N [open|close|0|1]"
            return {"action": action, "valve": "%s %s" % (args[1], args[2])}, None
        if sub == "tctl":
            if len(args) < 2:
                return None, "tanks tctl METHOD [ARGS...]"
            return {"action": action, "tctl": " ".join(args[1:])}, None
        return None, TANKS_USAGE
    if action == "env":
        sub = args[0].lower() if args else None
        if not sub:
            return {"action": action}, None
        if sub in ("ssp", "all"):
            return {"action": action, "ssp": True}, None
        return None, ENV_USAGE
    if action == "alarm":
        sub = args[0].lower() if args else None
        if not sub:
            return {"action": action}, None
        if sub in ("alarms", "rigging", "integrity", "brute"):
            return {"action": action, "sub": sub}, None
        return None, ALARM_USAGE
    if action in ("rig", "rigging"):
        if args:
            return None, "rig  (alias for 'alarm rigging')"
        return {"action": "alarm", "sub": "rigging"}, None
    if action == "asg":
        if not args:
            return None, "asg ID"
        try:
            return {"action": "asg", "id": int(args[0])}, None
        except ValueError:
            return None, "asg ID"
    if action == "sonctl":
        if not args:
            return {"action": action, "sub": ""}, None
        sub = args[0].lower()
        if sub == "auto":
            if len(args) < 2 or args[1].lower() not in ("on", "off"):
                return None, "sonctl auto on|off"
            return {"action": action, "sub": "auto", "val": args[1].lower()}, None
        if sub == "ids":
            return {"action": action, "sub": "ids"}, None
        if sub == "track":
            if len(args) < 2:
                return None, "sonctl track CONTACT_ID"
            return {"action": action, "sub": "track", "cid": args[1]}, None
        if sub == "untrack":
            if len(args) < 3:
                return None, "sonctl untrack GUID TYPE"
            return {"action": action, "sub": "untrack", "guid": args[1],
                    "type": int(args[2]) if args[2].isdigit() else args[2]}, None
        if sub == "data":
            if len(args) < 2:
                return None, "sonctl data CONTACT_ID"
            return {"action": action, "sub": "data", "cid": args[1]}, None
        if sub == "mark":
            if len(args) < 3:
                return None, "sonctl mark CONTACT_ID BEARING"
            return {"action": action, "sub": "mark", "cid": args[1],
                    "bearing": args[2]}, None
        if sub == "diag":
            return {"action": action, "sub": "diag"}, None
        if sub == "explore":
            target = args[1] if len(args) > 1 else "all"
            return {"action": action, "sub": "explore", "target": target}, None
        return None, "sonctl auto|ids|track|untrack|data|mark|diag|explore"
    if action == "tracker":
        if not args:
            return {"action": action, "sub": ""}, None
        sub = args[0].lower()
        if sub == "raw":
            return {"action": action, "sub": "raw"}, None
        if sub.isdigit():
            sub = {"0": "visual", "1": "radar", "2": "esm", "3": "radio",
                   "4": "weapon", "5": "ais", "6": "active", "7": "manual"}.get(sub, sub)
        if sub in ("visual", "radar", "esm", "radio", "weapon", "ais", "active", "manual"):
            # tracker TYPE [ID|clear|clearid ID|loadsnap STR|tkdump ID]
            if len(args) == 1:
                return {"action": action, "sub": sub}, None
            if args[1].lower() == "clear":
                return {"action": action, "sub": sub, "mode": "clear"}, None
            if args[1].lower() == "clearid":
                if len(args) < 3:
                    return None, "tracker %s clearid <ID>" % sub
                return {"action": action, "sub": sub, "mode": "clearid", "trackid": args[2]}, None
            if args[1].lower() == "loadsnap":
                if len(args) < 3:
                    return None, "tracker %s loadsnap <SNAPSHOT>" % sub
                return {"action": action, "sub": sub, "mode": "loadsnap", "snap": args[2]}, None
            if args[1].lower() == "tkdump":
                if len(args) < 3:
                    return None, "tracker %s tkdump <ID>" % sub
                return {"action": action, "sub": sub, "mode": "tkdump", "trackid": args[2]}, None
            # tracker TYPE ID -> getter probe
            return {"action": action, "sub": sub, "trackid": args[1]}, None
        if sub == "new":
            # tracker new TYPE ID  -> manual track creation via TrackerManager.New
            if len(args) < 3:
                return None, "tracker new <TYPE> <ID>"
            ttype = args[1].lower()
            if ttype.isdigit():
                ttype = {"0": "visual", "1": "radar", "2": "esm", "3": "radio",
                         "4": "weapon", "5": "ais", "6": "active", "7": "manual"}.get(ttype, ttype)
            if ttype not in ("visual", "radar", "esm", "radio", "weapon", "ais", "active", "manual"):
                return None, "tracker new <TYPE> <ID>  (TYPE: visual|radar|esm|radio|weapon|ais|active|manual or 0-7)"
            return {"action": "tracker-new", "type": ttype, "id": args[2]}, None
        return None, "tracker [TYPE [ID|clear|clearid ID|loadsnap STR|tkdump ID]] | tracker new <TYPE> <ID> | tracker raw"
    if action in ("radar", "esm"):
        return {"action": "tracker", "sub": action}, None
    if action == "diag":
        return {"action": "diag"}, None
    if action == "damage":
        return {"action": action}, None
    if action == "dc":
        sub = args[0].lower() if args else None
        if not sub:
            return None, DC_USAGE
        if sub == "status":
            return {"action": action, "sub": "status"}, None
        if sub == "bulkheads":
            if len(args) < 2 or args[1].lower() not in ("close", "open"):
                return None, "dc bulkheads close|open"
            return {"action": action, "sub": "bulkheads", "val": args[1].lower()}, None
        if sub == "bulkhead":
            if len(args) < 3:
                return None, "dc bulkhead <0-9> close|open"
            try:
                idx = int(args[1])
            except ValueError:
                return None, "dc bulkhead <0-9> close|open"
            if args[2].lower() not in ("close", "open"):
                return None, "dc bulkhead <0-9> close|open"
            return {"action": action, "sub": "bulkhead", "idx": idx, "val": args[2].lower()}, None
        if sub == "lights":
            return {"action": action, "sub": "lights"}, None
        if sub == "fire":
            if len(args) < 2:
                return None, "dc fire <0-9>"
            try:
                return {"action": action, "sub": "fire", "idx": int(args[1])}, None
            except ValueError:
                return None, "dc fire <0-9>"
        if sub == "extinguish":
            if len(args) < 2:
                return None, "dc extinguish <0-9>"
            try:
                return {"action": action, "sub": "extinguish", "idx": int(args[1])}, None
            except ValueError:
                return None, "dc extinguish <0-9>"
        if sub == "flood":
            if len(args) < 2:
                return None, "dc flood <0-9>"
            try:
                return {"action": action, "sub": "flood", "idx": int(args[1])}, None
            except ValueError:
                return None, "dc flood <0-9>"
        if sub == "deflood":
            if len(args) < 2:
                return None, "dc deflood <0-9>"
            try:
                return {"action": action, "sub": "deflood", "idx": int(args[1])}, None
            except ValueError:
                return None, "dc deflood <0-9>"
        return None, DC_USAGE
    if action == "masts":
        sub = args[0].lower() if args else None
        if not sub:
            return {"action": "masts"}, None
        if sub in ("raise", "retract"):
            if len(args) < 2:
                return None, "masts %s <id>" % sub
            return {"action": "masts", "sub": sub, "id": args[1]}, None
        if sub == "retract-all":
            return {"action": "masts", "sub": "retract-all"}, None
        if sub == "raise-all":
            return {"action": "masts", "sub": "raise-all"}, None
        if sub == "height":
            if len(args) < 3:
                return None, "masts height <id> <0.0-1.0>"
            return {"action": "masts", "sub": "height", "id": args[1], "val": args[2]}, None
        if sub == "periscope":
            if len(args) < 3:
                return None, "masts periscope <id> <0.0-1.0>"
            return {"action": "masts", "sub": "periscope", "id": args[1], "val": args[2]}, None
        if sub == "snorkel":
            if len(args) < 3:
                return None, "masts snorkel raise|retract"
            return {"action": "masts", "sub": "snorkel_%s" % args[2].lower()}, None
        return None, "masts [raise|retract|retract-all|raise-all|height|periscope|snorkel]"
    if action == "explore":
        return {"action": "explore"}, None
    return None, None


HELP = """\
state                       show current player state
probe                       show API discovery map (ship_probe.json)
watch [interval_s] [count]  auto-refresh state (default 3s)
helm COURSE [EOT] [DEPTH]   set course (deg), EOT order, depth (m)
                            [--env N] depth band (0 periscope..3 max)
                            [--snap] [--bubble ANGLE] [--autotrim on|off]
planes                      show control surfaces (fwd/stern/rudder angles,
                            locks, bubble, depth bands)
planes fwd|stern|rudder A   set plane/rudder angle (deg)
planes rudder release       drop rudder autopilot hold
planes bubble on|off       catch/release trim bubble (CatchBubble/ReleaseBubble)
planes bubble ANGLE        try SetBubble (may be absent on live build)
planes autotrim on|off      auto-trim loop (CatchBubble)
planes bow RETRACT|EXTEND   bow planes (retract flag)
planes lockfwd on|off       lock bow planes
planes lockint on|off       lock inner stern planes
plot LAT LON                plot a route to the position
clear-plot                  clear the current plot
report                      trigger ReportToHQ
ai-attack ID [--registry-only] [--allow-untracked] [--domain X]
                            order AI element ID to engage the player (domain =
                            BaseCategory: Subsurface default, Surface, Air, ...)
ai [ID] [--registry-only]   list AI elements / show one element (--registry-only
                            queues a registry diagnostic ai-attack first)
wc ID                       dump AI element WeaponController internals
detected                    which AI elements hold a contact/track on the player
ai-contacts                 dump every AI element's own contacts + tracks
ns-dump                     dump every /N/ blackboard namespace's keys
asg ID                      dump element ID's current assignment as values
sd-dump                     dump SteeringDiving + component members (debug)
tanks                       read-only ballast/trim/valve probe (MBTManager +
                            TnCManager + Hydrostatics + SteeringDiving) +
                            full detail dump
tanks vent|flood|drain|...  live tank write (state-changing, use with care)
                            (drain|blow|charge|blower|bank N|pump|rpm N|
                            tdrain|tflood|ttransfer|tcirc|
                            valve N open|close|tctl METHOD [ARGS...])
tanks fill [TANKS...]       fill procedure (Hand trim): flood valve open +
                            tank valves In, no pump (live-verified)
tanks drainall [TANKS...]  drain procedure: tank valves Out + TrimDrain(0);
                            NOTE outboard valve is GUI-only (no API object)
env                         SonarSim environment scan (EnvironmentalSystem,
                            SSP/RayTrace/bathymetry members, own sound) +
                            full detail dump
env ssp                     read SSP/TP/Analysis properties live (arrays)
alarm [integrity|brute]        scan alarm+rigging components; integrity=live
                            damage state dump; brute=200+ type names
damage                      integrity + compartment damage state (always-on in
                            ship_state.json systems.*; detail view here)
dc                          damage control (write commands to probe)
  dc status                   same as 'damage' (read-only)
  dc bulkheads close|open     ship-level bulkheads (CloseBulkheads/OpenBulkheads)
  dc bulkhead <0-9> close|open  per-tank bulkhead (SetBulkheadStatus)
  dc lights                   show NAVSTAT codes + lights state (read-only)
  dc fire <0-9>               DISABLED (freeze + mono GC crash)
  dc extinguish <0-9>         DISABLED (freeze + mono GC crash)
  dc flood <0-9>              DISABLED (freeze + mono GC crash)
  dc deflood <0-9>            DISABLED (freeze + mono GC crash)
sonctl auto|ids|track|...   sonar tracker control (auto on|off, ids, track ID,
                            untrack GUID TYPE, data ID, mark ID BEARING, diag)
tracker [TYPE]              FireControl tracker managers + contacts by sensor
tracker TYPE ID             probe all getters for a track id (GetBearing/GetRange/...)
tracker TYPE clear          clear all tracks (Clear()/Clear(0) attempts)
tracker TYPE clearid <ID>   remove one track
tracker TYPE loadsnap <STR> LoadSnapshot test (1-arg + 2-arg attempt)
tracker TYPE tkdump <ID>    dump ContactData fields of GetTrack(cid)
tracker new <TYPE> <ID>     manually create a track on a TrackerManager (New)
                            (radar|esm|visual|radio|weapon|ais|active|manual or 0-7)
tracker raw                 raw ContactManager diagnostics (GetUsed ids + prefixes)
radar / esm                 shortcut for tracker radar / tracker esm
sonar [--detail]            player sonar tracker contacts (bearing/range/sensor
                            per contact from GetContactIDs/GetTrackerData) +
                            per-array contacts (--detail adds noise/doppler)
masts                       show mast / snorkel / periscope / towed state
explore                     full internal structure dump -> ship_explore.json
                            (player element, all Access[T] components, blackboard,
                            sonar system — everything in one shot, no restart needed)
diag                        one-shot: results + ai-attack log markers + tail
results [N]                 show command results
log     [N]                 tail ship_probe_log.txt
status                      show lock/state status
help                        this help
quit / exit                 leave

EOT orders: Stop, Ahead13, Ahead23, AheadStd, AheadFull, AheadFlank,
            Astern13, Astern23, AsternFull, AsternEmer
"""


def repl(log_dir):
    print("MNW ship probe console - log dir: %s" % log_dir)
    print("Type 'help' for commands. 'quit' to leave.")
    while True:
        try:
            line = input("ship> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        words = line.split()
        head = words[0].lower()
        if head in ("quit", "exit"):
            break
        if head == "help":
            print(HELP)
            continue
        if head == "state":
            print_state(read_json(os.path.join(log_dir, STATE_FILE)))
            continue
        if head == "sonar":
            cmd_sonar(log_dir, detail="--detail" in words[1:])
            continue
        if head == "masts":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "planes":
            if len(words) == 1:
                cmd_planes_state(log_dir)
            else:
                cmd, err = parse_action(words)
                if err:
                    print("usage: %s" % err)
                    continue
                if cmd is None:
                    continue
                cmdid = next_cmdid(log_dir)
                n = send_commands(log_dir, [cmd])
                print("queued %d command(s) - waiting for probe cycle..." % n)
                time.sleep(1.2)
                cmd_result_for(log_dir, cmdid, wait=8.0)
                cmd_planes_state(log_dir)
            continue
        if head == "tanks":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "env":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "alarm":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "sonctl":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "tracker":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head in ("radar", "esm"):
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            cmd_action_dump(log_dir, cmd)
            continue
        if head == "explore":
            cmdid = next_cmdid(log_dir)
            n = send_commands(log_dir, [{"action": "explore"}])
            print("queued explore - waiting for probe cycle (this takes ~2-3 ticks)...")
            time.sleep(6.0)
            return cmd_explore(log_dir)
        if head == "probe":
            cmd_probe_file(log_dir)
            continue
        if head == "damage":
            cmd_damage(log_dir)
            continue
        if head == "dc":
            cmd, err = parse_action(words)
            if err:
                print("usage: %s" % err)
                continue
            if cmd is None:
                continue
            if cmd.get("sub") == "status":
                cmd_damage(log_dir)
            else:
                cmd_action_dump(log_dir, cmd)
            continue
        if head == "status":
            cmd_status(log_dir)
            continue
        if head == "watch":
            interval = float(words[1]) if len(words) > 1 else 3.0
            count = int(words[2]) if len(words) > 2 else None
            cmd_watch(log_dir, interval, count)
            continue
        if head == "results":
            n = int(words[1]) if len(words) > 1 else 10
            cmd_results(log_dir, n)
            continue
        if head == "log":
            n = int(words[1]) if len(words) > 1 else 20
            cmd_log(log_dir, n)
            continue
        cmd, err = parse_action(words)
        if err:
            print("usage: %s" % err)
            continue
        if cmd is None:
            print("unknown command %r - try 'help'" % head)
            continue
        if cmd["action"] == "ai":
            if cmd.get("registry_only"):
                print("sending registry-only diag on element %d ..." % cmd["id"])
                send_ai_attack(log_dir, cmd["id"], registry_only=True)
                time.sleep(2.0)
            if cmd.get("watch"):
                interval = float(words[1]) if len(words) > 1 else 3.0
                try:
                    while True:
                        cmd_ai(log_dir, cmd.get("id"))
                        time.sleep(interval)
                except KeyboardInterrupt:
                    print()
                    continue
            return cmd_ai(log_dir, cmd.get("id"))
        if cmd["action"] == "diag":
            return cmd_diag(log_dir)
        cmdid = next_cmdid(log_dir)
        n = send_commands(log_dir, [cmd])
        print("queued %d command(s) - waiting for probe cycle..." % n)
        if cmd["action"] in ("detected", "ai-contacts", "ns-dump", "asg", "sonctl", "tracker", "tracker-new"):
            time.sleep(1.5)
            cmd_result_for(log_dir, cmdid, wait=8.0)
        else:
            time.sleep(1.5)
            print_state(read_json(os.path.join(log_dir, STATE_FILE)))


def main():
    ap = argparse.ArgumentParser(description="MNW ship probe console")
    ap.add_argument("--game-root", help="MNW install dir (resolves Var/Scripts/Execute/_Source)")
    ap.add_argument("--log-dir", help="probe log dir (overrides --game-root)")
    ap.add_argument("--watch", action="store_true", help="continuous state watch")
    ap.add_argument("--watch-interval", type=float, default=3.0)
    ap.add_argument("--watch-count", type=int, default=None)
    args, rest = ap.parse_known_args()
    log_dir = resolve_log_dir(args)
    if not os.path.isdir(log_dir):
        print("log dir not found: %s" % log_dir)
        return 2
    if args.watch:
        cmd_watch(log_dir, args.watch_interval, args.watch_count)
        return 0
    if rest:
        if rest[0].lower() == "probe":
            return cmd_probe_file(log_dir)
        if rest[0].lower() == "damage":
            return cmd_damage(log_dir)
        if rest[0].lower() == "ai":
            nid = None
            registry_only = False
            if len(rest) > 1:
                try:
                    nid = int(rest[1])
                except ValueError:
                    pass
            if "--registry-only" in rest[1:]:
                registry_only = True
            if registry_only:
                send_ai_attack(log_dir, nid, registry_only=True)
                time.sleep(2.0)
            return cmd_ai(log_dir, nid, registry_only=registry_only)
        if rest[0].lower() == "diag":
            return cmd_diag(log_dir)
        if rest[0].lower() == "sonar":
            return cmd_sonar(log_dir, detail="--detail" in rest[1:])
        if rest[0].lower() == "masts":
            cmd, err = parse_action(rest)
            if err:
                print("usage: %s" % err)
                return 2
            if cmd is None:
                print("unknown command %r" % rest[0])
                return 2
            return cmd_action_dump(log_dir, cmd)
        if rest[0].lower() == "planes":
            if len(rest) == 1:
                return cmd_planes_state(log_dir)
            cmd, err = parse_action(rest)
            if err:
                print("usage: %s" % err)
                return 2
            cmdid = next_cmdid(log_dir)
            n = send_commands(log_dir, [cmd])
            print("queued %d command(s)" % n)
            time.sleep(1.2)
            return cmd_result_for(log_dir, cmdid, wait=8.0)
        if rest[0].lower() in ("tanks", "env", "alarm", "sonctl", "tracker", "masts"):
            cmd, err = parse_action(rest)
            if err:
                print("usage: %s" % err)
                return 2
            if cmd is None:
                print("unknown command %r" % rest[0])
                return 2
            return cmd_action_dump(log_dir, cmd)
        if rest[0].lower() == "explore":
            cmdid = next_cmdid(log_dir)
            n = send_commands(log_dir, [{"action": "explore"}])
            print("queued explore - waiting for probe cycle (~6s)...")
            time.sleep(6.0)
            return cmd_explore(log_dir)
        cmd, err = parse_action(rest)
        if err:
            print("usage: %s" % err)
            return 2
        if cmd is None:
            print("unknown command %r" % rest[0])
            return 2
        n = send_commands(log_dir, [cmd])
        print("queued %d command(s)" % n)
        time.sleep(1.5)
        return print_state(read_json(os.path.join(log_dir, STATE_FILE)))
    repl(log_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
