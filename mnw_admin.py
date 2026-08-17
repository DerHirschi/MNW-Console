#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MNW Ship Probe - reusable admin functions.

Thin, importable helpers for the recurring diagnostic queries we keep making
by hand (read ai_state, summarize AI elements, send ai-attack, wait for the
result, tail the log). Designed to be reused by the future admin tool UI and
by console.py. All functions are pure file-I/O against a probe log dir; they
never touch the game or the engine directly.

Protocol (probe writes / admin reads):
  <log_dir>/ai_state.json       <- per-element AI picture
  <log_dir>/ship_results.json   <- command results (cmdid-keyed)
  <log_dir>/ship_orders.json    -> commands (write this)
  <log_dir>/ship_probe_log.txt  <- event log (tail)

Typical ai-attack flow (2-phase, registry vs push):
  send_ai_attack(log_dir, 10, registry_only=True)  -> diagnose registry
  send_ai_attack(log_dir, 10)                      -> full PushOrder
"""

import io
import json
import os
import time

ORDERS_FILE = "ship_orders.json"
RESULTS_FILE = "ship_results.json"
AI_STATE_FILE = "ai_state.json"
LOG_FILE = "ship_probe_log.txt"


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
    data = read_json(os.path.join(log_dir, ORDERS_FILE))
    if isinstance(data, dict) and isinstance(data.get("commands"), list):
        ids = [c.get("cmdid", 0) for c in data["commands"] if isinstance(c, dict)]
        return max(ids) + 1 if ids else 0
    return 0


def send_command(log_dir, command):
    """Queue one command dict (action + args). Returns its cmdid."""
    cmdid = next_cmdid(log_dir)
    cmd = dict(command)
    cmd["cmdid"] = cmdid
    data = read_json(os.path.join(log_dir, ORDERS_FILE))
    existing = data.get("commands", []) if isinstance(data, dict) and isinstance(data.get("commands"), list) else []
    atomic_write(os.path.join(log_dir, ORDERS_FILE), {"commands": existing + [cmd]})
    return cmdid


def send_ai_attack(log_dir, element_id, registry_only=False, allow_untracked=True, domain=None):
    """Queue an ai-attack on an AI element. registry_only=True stops before
    Order/PushOrder (diagnoses the Assignments registry). domain overrides
    the Engage assignment's BaseCategory (e.g. "Surface" for surface-mounted
    weapons; defaults to "Subsurface")."""
    cmd = {
        "action": "ai-attack",
        "id": int(element_id),
        "registry_only": bool(registry_only),
        "allow_untracked": bool(allow_untracked),
    }
    if domain is not None:
        cmd["domain"] = str(domain)
    return send_command(log_dir, cmd)


def send_ns_dump(log_dir, element_id=None):
    """Queue an ns-dump (all /N/ namespaces, or one element's view when
    element_id is given). Returns the cmdid."""
    cmd = {"action": "ns-dump"}
    if element_id is not None:
        cmd["id"] = int(element_id)
    return send_command(log_dir, cmd)


def discover_helo_elements(log_dir, min_keys=30):
    """Find helo element IDs from the probe event log.

    Helos run a command-only host and NEVER appear in ai_state.json, so the
    probe log's ns-dump lines ('ns /N/ style=helo keys(K)') are the only
    place to discover them. Returns {element_id: key_count} for every helo
    namespace logged (both the small /0/-style 21-key one and the full
    94-key Z-9C host)."""
    found = {}
    for ln in grep_log(log_dir, "style=helo", limit=1000):
        try:
            left, _, right = ln.partition("ns /")
            nid = int(right.split("/", 1)[0])
            keys = int(right.split("keys(")[1].split(")")[0])
        except Exception:
            continue
        if keys >= min_keys or nid == 0:
            found[nid] = max(found.get(nid, 0), keys)
    return found


def wait_ai_attack_ack(log_dir, element_id, since_ts=None, timeout=90, poll=2):
    """Wait until the probe log shows an ai-attack PushOrder-ok for an element.

    Multi-host safe: every element host reads the same orders file, so
    ship_results.json is last-writer-wins and useless for helo (command-only)
    hosts. This greps ship_probe_log.txt for the chain's
    'cp10: about to Order(... tactical=<id> ...)' followed by 'cp13: PushOrder
    ok' that happened at/after since_ts (HH:MM:SS string or None). Returns the
    matching log line or None on timeout."""
    if since_ts is None:
        since_ts = time.strftime("%H:%M:%S", time.gmtime())
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = grep_log(log_dir, "cp10: about to Order", limit=400)
        for ln in lines:
            if "tactical=%d" % int(element_id) not in ln:
                continue
            if _line_ts_ge(ln, since_ts):
                return ln
        time.sleep(poll)
    return None


def _line_ts_ge(line, ts):
    try:
        return line.split(" ", 1)[0] >= ts
    except Exception:
        return True


def monitor_player_log_drop(player_log, timeout=600, poll=3):
    """Tail Player.log for a helo weapon drop.

    Looks for the Yu-7_AIR(Clone) spawn + 'Packet:(Torpedo, 99990003)' +
    'FireSingle: True' block that proves a Z-9C dropped its torpedo. player_log
    is the local path (or a callable returning the tail lines, e.g. an ssh
    wrapper). Returns the matched fire block lines or [] on timeout."""
    marks = ("Yu-7_AIR(Clone)", "Packet:(Torpedo, 99990003)")
    seen = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = _read_tail(player_log)
        for ln in lines:
            for m in marks:
                if m in ln:
                    seen.add(m)
        if "Yu-7_AIR(Clone)" in seen:
            return _drop_context(lines, marks)
        time.sleep(poll)
    return []


def _read_tail(player_log, n=2000):
    if callable(player_log):
        return player_log(n) or []
    try:
        with io.open(player_log, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except IOError:
        return []


def _drop_context(lines, marks):
    out = []
    for i, ln in enumerate(lines):
        if any(m in ln for m in marks):
            out.append(ln)
            for j in range(i + 1, min(i + 10, len(lines))):
                out.append(lines[j])
            break
    return out


def read_results(log_dir):
    data = read_json(os.path.join(log_dir, RESULTS_FILE))
    return data.get("results", []) if isinstance(data, dict) else []


def result_for_cmdid(log_dir, cmdid):
    for r in read_results(log_dir):
        if r.get("cmdid") == cmdid:
            return r
    return None


def wait_result(log_dir, cmdid, timeout=45, poll=2):
    """Block until a result for cmdid appears (or timeout). Returns the result
    dict or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = result_for_cmdid(log_dir, cmdid)
        if r is not None:
            return r
        time.sleep(poll)
    return None


def read_ai_state(log_dir):
    return read_json(os.path.join(log_dir, AI_STATE_FILE))


def summarize_elements(ai_state):
    """Flatten ai_state.elements into compact dicts for display/tooling."""
    out = []
    for e in (ai_state or {}).get("elements", []):
        ca = e.get("current_assignment") or {}
        iord = e.get("incoming_order") or {}
        ll = e.get("lat_lon") or []
        out.append({
            "id": e.get("id"),
            "name": e.get("name"),
            "category": e.get("category"),
            "lat": ll[0] if len(ll) > 1 else None,
            "lon": ll[1] if len(ll) > 1 else None,
            "range_km": e.get("to_player_range_km"),
            "speed": e.get("true_speed"),
            "course": e.get("true_heading"),
            "assignment_id": e.get("assignment_id"),
            "assignment_type": _short_type(ca.get("type")),
            "incoming_assignment_id": iord.get("assignment_id", iord.get("assignment_id_err")),
            "contacts": e.get("contact_count"),
            "action_prep": e.get("action_prep_complete"),
        })
    return out


def _short_type(t):
    """Collapse '<class 'Engage'>' / '<class 'mnw...ASW'>' to 'Engage'/'ASW'."""
    if not t:
        return None
    t = str(t)
    if "<class '" in t:
        t = t.split("<class '", 1)[1].rsplit("'", 1)[0]
        t = t.rsplit(".", 1)[-1]
    return t or None


def print_elements(elements):
    if not elements:
        print("(no AI elements)")
        return 1
    print("%-4s %-20s %-12s %-10s %-9s %-6s %-6s %-8s %-8s" % (
        "id", "name", "type", "range_km", "spd", "crs", "asg", "asg_type", "contacts"))
    for e in elements:
        asg_t = (e.get("assignment_type") or "?")
        print("%-4s %-20s %-12s %-10s %-9s %-6s %-6s %-8s %-8s" % (
            e.get("id"), (e.get("name") or "?")[:20], (e.get("category") or "?")[:12],
            _fmt(e.get("range_km"), 1), _fmt(e.get("speed"), 1), _fmt(e.get("course"), 0),
            _fmt(e.get("assignment_id"), 0), asg_t[:8], _fmt(e.get("contacts"), 0)))
    return 0


def tail_log(log_dir, n=20):
    path = os.path.join(log_dir, LOG_FILE)
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except IOError:
        return []
    return lines[-max(1, n):]


def grep_log(log_dir, pattern, limit=50):
    """Return log lines containing pattern (case-insensitive), newest last."""
    try:
        with io.open(os.path.join(log_dir, LOG_FILE), "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except IOError:
        return []
    hits = [ln for ln in lines if pattern.lower() in ln.lower()]
    return hits[-max(1, limit):]


def monitor_element(log_dir, element_id, interval=3, count=None):
    """Watch one AI element's range + assignment until count ticks."""
    i = 0
    while count is None or i < count:
        st = read_ai_state(log_dir)
        el = None
        for e in (st or {}).get("elements", []):
            if e.get("id") == element_id:
                el = e
                break
        if el is None:
            print("(element %d not in ai_state yet)" % element_id)
        else:
            ca = el.get("current_assignment") or {}
            asg_t = _short_type(ca.get("type")) or "?"
            ll = el.get("lat_lon") or []
            print("%s  rng=%-9s hdg=%-7s spd=%-6s asg_id=%-4s asg=%-8s lat=%.3f lon=%.3f" % (
                time.strftime("%H:%M:%S"),
                _fmt(el.get("to_player_range_km"), 1, "km"),
                _fmt(el.get("true_heading"), 0),
                _fmt(el.get("true_speed"), 1),
                _fmt(el.get("assignment_id"), 0),
                asg_t[:8],
                ll[0] if len(ll) > 1 else -1,
                ll[1] if len(ll) > 1 else -1,
            ))
        i += 1
        if count is None or i < count:
            time.sleep(interval)


def _fmt(v, digits=2, unit=""):
    if v is None:
        return "?"
    try:
        if isinstance(v, (int, float)):
            return ("%.*f%s" % (digits, v, unit)).rstrip("0").rstrip(".")
    except Exception:
        pass
    return str(v)
