#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MNW AI Tactical View - htop-style curses TUI for the ship-probe file protocol.

Answers "who is tracking the player, when, how exactly" for every AI element
(ships AND command-only hosts like helo / submarine that never appear in
ai_state.json).

Data flow (file protocol, no network beyond optional ssh fetch):

  ai_state.json        full-state AI ships (poll; refreshes only every ~90 s)
  ship_state.json      player state (poll; same cadence)
  ship_results.json    command answers (detected / asg / ai-contacts)
  ship_probe_log.txt   authoritative event log (ns-style discovery, fallbacks)
  datalink_presence.json  OPTIONAL (ai-agent mod); used only if present

The probe's config (tick_delay=30 + state_every=3) means ship_state.json /
ai_state.json refresh only about every 90 s wall-clock. This tool therefore
does NOT passively wait for fresh files: it drives the probe through API
commands queued in ship_orders.json:

  {"action":"planes"}          read-only no-op that ends with collect_state()
                               -> forces a fresh ship_state.json + ai_state.json
  {"action":"detected"}        per-element player-track scan (HIT/NO lines)
  {"action":"asg","id":N}      per-element ammo/threat/assignment/rpm values
  {"action":"ai-contacts"}     every element's own contact list
  {"action":"dl-reports"}      tactical-AI operator report dump (player-centred)

Results are matched by cmdid (single in-flight command per type keeps the
last-writer-wins results file unambiguous).

Usage:
  python3 ai_tactical.py --log-dir <dir>                    local poll
  python3 ai_tactical.py --remote user@host:"/abs/log/dir"  ssh fetch
      [--interval 5] [--count N] [--json] [--no-color] [--read-only]
      [--detect-interval 10] [--asg-ttl 60]

The detected scan runs on its own clock: base cadence --detect-interval
(min 10 s) plus an immediate re-scan whenever any AI element's state
signature (range/heading/speed/assignment/orders/prep/contact count)
changes. Command-only hosts are discovered via one automatic ns-dump on
cold start; their ai-state is then probed in rotation.

Keys: q quit | p pause | +/- interval | up/down select | TAB detail expand
      d detect now | e ai-state probe | a ai-contacts sweep
      r force state refresh | c colors
      A queue ai-attack on the selected element (y confirms)
      B queue BLIND ai-attack (allow_untracked; y confirms)

Layout: >= 100 cols shows OWN SHIP as a right-hand instrument column;
narrower terminals stack it above the table.
"""

import argparse
import collections
import curses
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_PROBE = os.path.join(_ROOT, "ship-probe")
for p in (_HERE, _PROBE):
    if p not in sys.path:
        sys.path.insert(0, p)

from mnw_admin import _short_type, read_json

STATE_FILE = "ship_state.json"
AI_STATE_FILE = "ai_state.json"
ORDERS_FILE = "ship_orders.json"
RESULTS_FILE = "ship_results.json"
LOG_FILE = "ship_probe_log.txt"
PRESENCE_FILE = "datalink_presence.json"
CONFIG_FILE = "ship_probe_config.json"

STYLES = ("red", "amber", "green", "cyan", "dim", "hdr", "white")

_EOT_SHORT = {
    "Stop": "Stop", "Ahead13": "13", "Ahead23": "23", "AheadStd": "Std",
    "AheadFull": "Full", "AheadFlank": "Flank",
    "Astern13": "A13", "Astern23": "A23", "AsternFull": "AFull",
    "AsternEmer": "AEm", "SetKnots": "Kts", "SetTurns": "Trn",
}

_WEAPONS_DB = (
    (("052d",), "YJ-18 x61 SSM | Yu-7 x4 TOR"),
    (("054a",), "YJ-83 x9 SSM | Type-87 x2 ASW-RKT"),
    (("akula", "971"), "UGST Fizik-1 x8 TOR"),
    (("z-9", "z9", "helo"), "Yu-7-Air x2 TOR"),
    (("virginia",), "MK-48 ADCAP x4 TOR"),
)


def _num(v):
    """Sanitized float: None on missing/garbage/NaN/Infinity."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _json_safe(v):
    """Recursively strip NaN/Infinity from pass-through structures so strict
    JSON output never sees them (probe sonar tracks carry bare nan/inf)."""
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return _num(v)


def _fmt(v, digits=1, unit=""):
    n = _num(v)
    if n is None:
        return "?"
    s = "%.*f%s" % (digits, n, unit)
    if digits > 0:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# Probe speeds are m/s (Unity convention); every "kt"-labelled display value
# goes through _kt().
_MS_TO_KT = 1.94384


def _kt(v):
    """m/s -> knots (float) or None if the input is missing/garbage."""
    n = _num(v)
    return None if n is None else n * _MS_TO_KT


def _short_enum(v, limit=28):
    """'mnw.Core.ContactTools+CategoryID.Air' -> 'CategoryID.Air'."""
    if v is None:
        return "?"
    s = str(v).split("+")[-1].strip("'<>")
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    return s


def _short_asg(t):
    """'mnw.Core.Assignments+ASWSearch' -> 'ASWSearch'."""
    t = _short_type(t)
    if not t:
        return None
    return str(t).split("+")[-1].rsplit(".", 1)[-1]


def weapons_for(name):
    if not name:
        return None
    low = str(name).lower()
    for keys, arm in _WEAPONS_DB:
        for k in keys:
            if k in low:
                return arm
    return None


def parse_list_count(v):
    """'_MergedContacts' may arrive as int count OR 'list(N)' repr string."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"list\((\d+)\)", str(v))
    if m:
        return int(m.group(1))
    n = _num(v)
    return int(n) if n is not None else None


def parse_detected_detail(lines):
    """Parse a 'detected' result detail into {eid: {...}}.

    Positive evidence: 'DETECTED by element N (range X m, contact id C, K contacts)'.
    Negative evidence per scanned id (from 'detected: scanning [..]'):
      id-bearing NO lines ('has NO contacts', 'no _ContactManager') are kept,
      remaining scanned ids default to {'detected': False}.
    """
    out = {}
    scanned = []
    m = re.search(r"scanning \[(.*)\]", "\n".join(ln for ln in lines if "scanning [" in ln))
    if m:
        for tok in re.findall(r"'?(\d+)'?", m.group(1)):
            eid = int(tok)
            if eid not in scanned:
                scanned.append(eid)
    for ln in lines:
        mm = re.search(r"DETECTED by element (\d+) \(range ([\d.]+) m, contact id ([^,]+), (\d+) contacts\)", ln)
        if mm:
            eid = int(mm.group(1))
            out[eid] = {"detected": True, "range_m": float(mm.group(2)),
                        "contact_id": mm.group(3).strip(), "contacts": int(mm.group(4))}
            continue
        mm = re.search(r"element (\d+) has NO contacts", ln)
        if mm:
            out[int(mm.group(1))] = {"detected": False, "range_m": None,
                                     "contact_id": None, "contacts": 0}
            continue
        mm = re.search(r"no _ContactManager on element (\d+)", ln)
        if mm:
            out[int(mm.group(1))] = {"detected": False, "range_m": None,
                                     "contact_id": None, "n/a": True}
    for eid in scanned:
        if eid not in out:
            out[eid] = {"detected": False, "range_m": None, "contact_id": None}
    return out


def parse_asg_detail(lines):
    """Parse an 'asg' result detail into a flat dict of coerced values."""
    out = {}
    for ln in lines:
        mm = re.match(r"target element id=(\d+)", ln.strip())
        if mm:
            out["target_id"] = int(mm.group(1))
            continue
        mm = re.match(r"^([A-Za-z_0-9.]+)=(.*)$", ln.strip())
        if not mm:
            continue
        key, raw = mm.group(1), mm.group(2).strip()
        if raw in ("True", "False"):
            out[key] = raw == "True"
            continue
        n = _num(raw)
        if n is not None and re.match(r"^-?[\d.]+$", raw):
            out[key] = n
        else:
            out[key] = raw
    return out


_CONTACT_ALIASES = {"range": "range_m", "crs": "course", "spd": "speed",
                    "brg": "bearing"}


def parse_ai_state_detail(lines):
    """Parse an 'ai-state <id>' result detail (same k=v shape as 'asg')."""
    out = parse_asg_detail(lines)
    if "target_id" not in out:
        for ln in lines:
            mm = re.search(r"element id=(\d+)", ln)
            if mm:
                out["target_id"] = int(mm.group(1))
                break
    return out


def parse_ai_contacts_detail(lines):
    """Parse an 'ai-contacts' result detail into {eid: [contact dicts]}."""
    out = {}
    cur = None
    for ln in lines:
        mm = re.search(r"contacts: element (\d+): no _ContactManager", ln)
        if mm:
            out.setdefault(int(mm.group(1)), [])
            cur = None
            continue
        mm = re.search(r"contacts: element (\d+): (\d+) contacts", ln)
        if mm:
            cur = int(mm.group(1))
            out.setdefault(cur, [])
            continue
        s = ln.strip()
        if cur is None or not s.startswith("id"):
            continue
        c = {}
        for i, kv in enumerate(s.split(", ")):
            if "=" in kv:
                k, v = kv.split("=", 1)
                k = _CONTACT_ALIASES.get(k.strip(), k.strip())
                n = _num(v)
                c[k] = n if n is not None else v.strip()
            elif i == 0:
                tok = kv.strip()
                c["id"] = re.sub(r"^id[\s=:]*", "", tok).strip("'\" ") or tok
        out[cur].append(c)
    return out


_REPORT_META_KEYS = ("country_id", "ai_realism", "validity", "cycles",
                     "count_agents", "count_all_agents")


def _parse_report_row(s):
    """One '  src=5 op=183 ... player=14.2km@312deg ... (partial)' report line."""
    rep = {}
    for k, v in re.findall(r"(\w+)=([^\s]+)", s):
        if k == "player":
            m = re.match(r"([\d.]+)km@(-?[\d.]+)deg", v)
            if m:
                rep["player_km"] = _num(m.group(1))
                rep["brg"] = _num(m.group(2))
            continue
        n = _num(v)
        rep[k] = n if n is not None else v
    if "partial" in s:
        rep["partial"] = True
    return rep


def parse_dl_reports_detail(lines):
    """Parse an 'dl-reports' result detail into
    {"operators_list": [...], "operators": {op: {"meta": {...}, "counts": {...},
     "reports": [...], "entries": N, "shown": N, "mode": "player"|"all"}}}."""
    out = {"operators": {}}
    cur_op = None
    for ln in lines or []:
        s = ln.strip()
        mm = re.match(r"^dl-reports: player at lat=([-0-9.]+) lon=([-0-9.]+)"
                      r"(?: via (\S+))?", s)
        if mm:
            out["player"] = {"lat": _num(mm.group(1)),
                             "lon": _num(mm.group(2)),
                             "via": mm.group(3)}
            continue
        mm = re.match(r"^dl-reports: (all reports|player-centred reports)", s)
        if mm:
            out["mode"] = "all" if mm.group(1).startswith("all") else "player"
            continue
        mm = re.match(r"^dl-reports: operators=([\d,]+)", s)
        if mm:
            out["operators_list"] = [int(x) for x in mm.group(1).split(",")
                                     if x.strip().isdigit()]
            continue
        mm = re.match(r"^== operator (\d+) ==", s)
        if mm:
            cur_op = int(mm.group(1))
            out["operators"][cur_op] = {"meta": {}, "counts": {}, "reports": []}
            continue
        if cur_op is None:
            continue
        o = out["operators"][cur_op]
        mm = re.match(r"^(%s)=(-?[\d.]+)$" % "|".join(_REPORT_META_KEYS), s)
        if mm:
            o["meta"][mm.group(1)] = _num(mm.group(2))
            continue
        mm = re.match(r"^([\w .]+)=(\d+)$", s)
        if mm and mm.group(1) in ("initial_reports", "theater fused",
                                  "active_agents", "assignments",
                                  "aggregated_reports"):
            o["counts"][mm.group(1)] = int(mm.group(2))
            continue
        mm = re.match(r"^([\w .]+)=dict\((\d+)\)$", s)
        if mm and mm.group(1) in ("initial_reports", "theater fused",
                                  "active_agents", "assignments",
                                  "aggregated_reports"):
            o["counts"][mm.group(1)] = int(mm.group(2))
            continue
        mm = re.match(
            r"^last_reports entries=(\d+) shown=(\d+) \(mode=(\w+)\)$", s)
        if mm:
            o["entries"] = int(mm.group(1))
            o["shown"] = int(mm.group(2))
            o["mode"] = mm.group(3)
            continue
        mm = re.match(r"^last_reports entries=(\d+)", s)
        if mm:
            o["entries"] = int(mm.group(1))
            continue
        mm = re.match(r"^shown=(\d+)", s)
        if mm:
            o["shown"] = int(mm.group(1))
            continue
        mm = re.match(r"^report (.*)$", s)
        if mm:
            o["reports"].append(_parse_report_row(mm.group(1)))
            continue
        if s.startswith("src="):
            o["reports"].append(_parse_report_row(s))
    return out


def parse_ns_styles(log_lines):
    """{eid: style} from 'ns /N/ style=helo keys(K): ...' log lines."""
    out = {}
    for ln in log_lines:
        mm = re.search(r"ns /(\d+)/ style=([a-z/_-]+)", ln)
        if mm:
            out[int(mm.group(1))] = mm.group(2)
    return out


def parse_log_detected(log_lines):
    """Fallback detected map from 'DETECTED by element N (...)' log lines."""
    out = {}
    for ln in log_lines:
        mm = re.search(r"DETECTED by element (\d+) \(range ([\d.]+) m", ln)
        if mm:
            out[int(mm.group(1))] = {"detected": True, "range_m": float(mm.group(2)),
                                     "contact_id": None}
    return out


def parse_ts(s):
    """'2026-08-22 18:32:34' -> epoch, else None."""
    if not s:
        return None
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def data_age(state, now=None):
    ts = parse_ts((state or {}).get("ts"))
    if ts is None:
        return None
    age = (now if now is not None else time.time()) - ts
    return max(0.0, age)


def _range_str(km):
    km = _num(km)
    if km is None:
        return "?"
    if km < 1.0:
        return "%dm" % round(km * 1000)
    return "%.1fkm" % km


def _brg(deg):
    d = _num(deg)
    return ("%03d" % round(d)) + "\u00b0" if d is not None else "?"


def _range_bearing_ll(plat, plon, lat, lon):
    """Equirectangular approx: (range_km, bearing_deg) or (None, None)."""
    try:
        plat, plon = float(plat), float(plon)
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    dlat = math.radians(lat - plat)
    dlon = math.radians(lon - plon)
    y = math.sin(dlon) * math.cos(math.radians(lat))
    x = math.cos(math.radians(plat)) * math.sin(math.radians(lat)) - \
        math.sin(math.radians(plat)) * math.cos(math.radians(lat)) * math.cos(dlon)
    brg = round(math.degrees(math.atan2(y, x))) % 360.0
    km = 6371.0 * math.sqrt(dlat ** 2 +
                             (math.cos(math.radians((lat + plat) / 2.0)) * dlon) ** 2)
    return round(km, 3), round(brg, 1)


def own_element_ids(ship_state):
    """All ids that refer to the player's own element (never an AI row).

    `player.player_id` (CoordinatesManager.Player.GetID) is authoritative.
    Secondary mirrors (`player.id`, `identity.id`) only count when they AGREE
    with it — live missions showed player.id pointing at a hostile element,
    which would silently filter a real contact.
    """
    ss = ship_state or {}
    ids = set()
    pid = None
    try:
        pid = int((ss.get("player") or {}).get("player_id"))
    except (TypeError, ValueError):
        pid = None
    if pid is not None:
        ids.add(pid)
    for sec, key in (("player", "id"), ("identity", "id")):
        try:
            v = int((ss.get(sec) or {}).get(key))
        except (TypeError, ValueError):
            continue
        if pid is None or v == pid:
            ids.add(v)
    return ids


_TYP_LABELS = {"SUB": "Submarine", "HEL": "Helicopter", "SHP": "Ship"}


def normalize_elements(ai_state, ship_state, detected_map, asg_map,
                       ns_styles, presence, prev_ranges, now=None,
                       ext_map=None):
    """Merge every known AI source into one normalized element dict list.

    Sources merged per id:
      ai_state entry | detected_map | asg_map | ext_map (ai-state probes,
      command-only hosts) | ns_styles | datalink_presence (optional).
    Own-element ids (see own_element_ids) are always excluded.
    """
    own_ids = own_element_ids(ship_state)
    player_id = None
    ps = (ship_state or {}).get("player") or {}
    try:
        player_id = int(ps.get("player_id"))
    except (TypeError, ValueError):
        player_id = None
    plat_lon = ((ship_state or {}).get("navigation") or {}).get("lat_lon") or []
    ids = []
    by_id = {}
    for e in ((ai_state or {}).get("elements") or []):
        eid = e.get("id")
        # id 0 is the contextless host module (no position/state) — it is not
        # an AI contact and would only push real rows out of the table window
        if eid is None or eid == 0 or eid in own_ids:
            continue
        ids.append(eid)
        by_id[eid] = {"raw": e, "src": "state"}
    pres = (presence or {}).get("elements") or {}
    for k, p in pres.items():
        try:
            eid = int(p.get("eid", k))
        except (TypeError, ValueError):
            continue
        if eid == 0 or eid in own_ids or player_id is not None and eid == player_id:
            continue
        if eid not in by_id:
            ids.append(eid)
            by_id[eid] = {"raw": {}, "src": "presence"}
        by_id[eid]["presence"] = p
    for eid, style in (ns_styles or {}).items():
        if eid in own_ids or eid == player_id or eid == 0 or eid in by_id:
            continue
        if style in ("helo", "plane/sub", "plane", "ship"):
            ids.append(eid)
            by_id[eid] = {"raw": {}, "src": "ns:%s" % style, "style": style}
    for eid, det in (detected_map or {}).items():
        if eid == player_id or eid in own_ids or eid == 0 or eid in by_id:
            continue
        ids.append(eid)
        by_id[eid] = {"raw": {}, "src": "detected"}

    els = []
    for eid in sorted(set(ids)):
        info = by_id.get(eid, {})
        e = dict(info.get("raw") or {})
        ext = (ext_map or {}).get(eid) or {}
        if ext:
            # ai-state probe fills command-only hosts; state entries win
            for k, v in ext.items():
                if k not in ("target_id", "ts_epoch") and e.get(k) is None:
                    e[k] = v
        det = (detected_map or {}).get(eid) or {}
        ag = (asg_map or {}).get(eid) or {}
        ca = e.get("current_assignment") or {}
        iord = e.get("incoming_order") or {}
        ll = e.get("lat_lon") or []
        prev_km = (prev_ranges or {}).get(eid, {}).get("km")
        km = _num(e.get("to_player_range_km"))
        brg = e.get("to_player_bearing")
        elat = ll[0] if len(ll) > 1 else e.get("lat")
        elon = ll[1] if len(ll) > 1 else e.get("lon")
        if elat is not None and elon is not None and len(plat_lon) > 1 \
                and km is None:
            km, brg2 = _range_bearing_ll(plat_lon[0], plat_lon[1], elat, elon)
            brg = brg if brg is not None else brg2
        if km is None:
            drm = _num(det.get("range_m"))
            km = drm / 1000.0 if drm is not None else None
        style = info.get("style") or ns_styles.get(eid) or e.get("host_style")
        cat_raw = e.get("category") or ""
        if "Submarine" in cat_raw or style in ("plane/sub", "plane"):
            typ = "SUB"
        elif "Aircraft" in cat_raw or style == "helo":
            typ = "HEL"
        elif cat_raw or style == "ship":
            typ = "SHP"
        else:
            typ = "?"
        name = e.get("name") or ""
        if not name:
            pres_p = info.get("presence") or {}
            pc = _short_enum(pres_p.get("category"), 20)
            pc = "" if pc == "?" else pc
            pc = (pc.replace("ElementCategory.", "").replace("CategoryID.", "")
                    or _TYP_LABELS.get(typ, typ))
            name = "%s #%d" % (pc or _TYP_LABELS.get(typ, typ), eid)
        iord_id = _num(iord.get("assignment_id"))
        incoming = bool(iord) and iord_id is not None and iord_id >= 0
        det_ts_epoch = parse_ts(det.get("ts"))
        el = {
            "id": eid,
            "name": name,
            "type": typ,
            "style": style,
            "src": info.get("src", "?"),
            "country": e.get("country") or (info.get("presence") or {}).get("country"),
            "lat": _num(ll[0]) if len(ll) > 1 else (
                _num(e.get("lat")) if e.get("lat") is not None
                else _num((info.get("presence") or {}).get("lat"))),
            "lon": _num(ll[1]) if len(ll) > 1 else (
                _num(e.get("lon")) if e.get("lon") is not None
                else _num((info.get("presence") or {}).get("lon"))),
            "range_km": km,
            "delta": (_num(prev_km) - km) if km is not None and _num(prev_km) is not None else None,
            "bearing": brg,
            "speed": _num(e.get("true_speed", e.get("speed"))),
            "heading": _num(e.get("true_heading", e.get("heading"))),
            "depth": _num(e.get("depth")),
            "eot": _EOT_SHORT.get(str(e.get("current_eot") or e.get("ordered_eot") or ""),
                                  str(e.get("current_eot") or "?")[:5]),
            "contacts": e.get("contact_count"),
            "assignment_id": e.get("assignment_id", ag.get("assignment_id")),
            "assignment_type": _short_asg(ca.get("type") or e.get("assignment_type")),
            "action_prep": e.get("action_prep_complete")
            if isinstance(e.get("action_prep_complete"), bool)
            else (e.get("action_prep") if isinstance(e.get("action_prep"), bool) else None),
            "incoming": incoming,
            "detected": det.get("detected"),
            "det_range_m": det.get("range_m"),
            "det_contact": det.get("contact_id"),
            "det_stale": bool(det.get("stale")),
            "det_age": max(0, int((now or time.time()) - det_ts_epoch)) if det_ts_epoch else None,
            "ammo_off": _num(ag.get("ammunition.OffensiveCombatPowerRatio")),
            "ammo_def": _num(ag.get("ammunition.DefensiveCombatPowerRatio")),
            "torp_threat": ag.get("torpedo_threat"),
            "miss_threat": ag.get("missile_threat"),
            "air_threat": ag.get("aircraft_threat"),
            "dipping": ag.get("dipping_engaged"),
            "rpm": _num(ag.get("rpm")),
            "throttle": _num(ag.get("throttle")),
            "altitude": _num(ag.get("current_altitude")),
            "weapons": weapons_for(name),
            "asg_ts": ag.get("ts"),
            "det_ts": det.get("ts"),
            "suspects_n": _num(e.get("suspects_n")),
            "suspects_ids": e.get("suspects_ids"),
            "tracked_cat": _num(e.get("tracked_cat")),
            "contact_cache": e.get("contact_cache"),
            "target_lat": _num(e.get("target_lat")),
            "target_lon": _num(e.get("target_lon")),
            "target_course": _num(e.get("target_course")),
            "fire_domain": e.get("fire_domain"),
            "fire_orient": e.get("fire_orient"),
        }
        els.append(el)
    els.sort(key=lambda x: (x["range_km"] is None, x["range_km"] if x["range_km"] is not None else 0, x["id"]))
    return els


def build_frame(data):
    """Pure merge of raw sources -> one renderable frame dict."""
    now = data.get("now", time.time())
    ship = data.get("ship_state") or {}
    ai = data.get("ai_state") or {}
    detected_map = dict(data.get("log_detected") or {})
    freshest_det = data.get("detected_result") or {}
    if freshest_det.get("map"):
        for eid, d in freshest_det["map"].items():
            d2 = dict(d)
            d2["ts"] = freshest_det.get("ts")
            if eid in detected_map and not d.get("detected"):
                continue
            d2["stale"] = (freshest_det.get("age_s") or 0) > max(60.0, 2.0 * data.get("interval", 5.0))
            detected_map[eid] = d2
    asg_map = data.get("asg_map") or {}
    els = normalize_elements(ai, ship, detected_map, asg_map,
                             data.get("ns_styles") or {},
                             data.get("presence"),
                             data.get("prev_ranges") or {}, now=now,
                             ext_map=data.get("ext_map") or {})
    # ghost census: every discovered ns-style element that is not the player,
    # not an own-ship id, and NOT already merged into ai_state.json (since
    # 2026-08-29 command-only hosts contribute their ids -> they render as
    # real rows, not ghosts)
    try:
        pid = int(((ship.get("player") or {}).get("player_id")))
    except (TypeError, ValueError):
        pid = None
    own_ids = own_element_ids(ship)
    state_ids = set()
    for e in ((ai or {}).get("elements") or []):
        try:
            state_ids.add(e["id"])
        except Exception:
            pass
    ghost_count = sum(1 for eid, st in (data.get("ns_styles") or {}).items()
                      if eid and st and eid != 0 and eid not in own_ids
                      and eid not in state_ids
                      and (pid is None or eid != pid))
    bb = ship.get("blackboard") or {}
    con = ship.get("contacts") or {}
    disabled = bool(con.get("disabled"))
    tracks = con.get("tracks") or []
    sonar = ship.get("sonar") or {}
    nav = ship.get("navigation") or {}
    ident = ship.get("identity") or {}
    mission = ship.get("mission") or {}
    clock = ship.get("clock") or {}
    systems = ship.get("systems") or {}

    def _masts():
        """Per-mast state for the schematic; ids from probe or key scan 0-5."""
        mids = systems.get("mast_ids")
        if not isinstance(mids, list):
            mids = [i for i in range(6) if systems.get("mast_%d_type" % i)]
        out = []
        for i in mids:
            try:
                mid = int(i)
            except (TypeError, ValueError):
                continue
            out.append({
                "id": mid,
                "type": systems.get("mast_%d_type" % mid),
                "status": systems.get("mast_%d_status" % mid),
                "height": _num(systems.get("mast_%d_height" % mid)),
            })
        return out

    masts = _masts()
    masts_up = sum(1 for m in masts if m["status"] == "Raised")
    masts_total = len(masts)
    frame = {
        "now": now,
        "interval": data.get("interval", 5.0),
        "header": {
            "mission": mission.get("name"),
            "operation": mission.get("operation"),
            "datetime": mission.get("datetime"),
            "tension": mission.get("tension"),
            "clock": clock.get("time"),
            "player": ident.get("name"),
            "paused": data.get("paused", False),
            "mode": data.get("mode", ""),
        },
        "ages": {
            "ship_state": data_age(ship, now),
            "ai_state": data_age(ai, now),
            "detected": (data.get("detected_result") or {}).get("age_s"),
        },
        "elements": els,
        "threats": {
            "merged": parse_list_count(bb.get("_MergedContacts")),
            "suspicious": parse_list_count(bb.get("_EnemySuspiciousContacts")),
            "torp": any(e.get("torp_threat") for e in els),
            "miss": any(e.get("miss_threat") for e in els),
            "air": any(e.get("air_threat") for e in els),
        },
        "contacts": {
            "count": len(tracks) if tracks else (con.get("count") if not disabled else 0),
            "disabled": disabled,
            "tracks": [{
                "id": t.get("id"),
                "category": _short_enum(t.get("category"), 16),
                "identity": _short_enum(t.get("identity"), 12),
                "bearing": t.get("bearing"),
                "range_m": _num(t.get("range")),
                "course": _num(t.get("course")),
                "speed": _num(t.get("speed")),
            } for t in tracks[:8]],
        },
        "sonar": {
            "count": sonar.get("count") if isinstance(sonar, dict) else None,
            "tracks": _json_safe(sonar.get("tracks"))
            if isinstance(sonar, dict)
            else [],
        },
        "player": {
            "name": ident.get("name"),
            "lat_lon": nav.get("lat_lon") or [],
            "heading": _num(nav.get("heading")),
            "true_heading": _num(nav.get("true_heading")),
            "speed": _num(nav.get("speed")),
            "true_speed": _num(nav.get("true_speed")),
            "depth": _num(nav.get("depth")),
            "altitude": _num(nav.get("altitude")),
            "bottom_range": _num(nav.get("bottom_range")),
            "ordered_course": _num(nav.get("_orderedcourse", nav.get("ordered_course"))),
            "current_course": _num(nav.get("_currentcourse", nav.get("heading"))),
            "ordered_eot": nav.get("_orderedeotorder") or nav.get("ordered_eot"),
            "ordered_depth": _num(nav.get("_ordereddepth", nav.get("ordered_depth"))),
            "rpm": _num(nav.get("_currentrpm", systems.get("rpm"))),
            "damage": _num(systems.get("integrity_damage_ratio")),
            "ammo_off": _num(systems.get("ammo_offensive_ratio")),
            "ammo_def": _num(systems.get("ammo_defensive_ratio")),
            "towed": systems.get("towed_array"),
            "masts_up": masts_up,
            "masts_total": masts_total,
            "masts": masts,
            "snorkel_raised": systems.get("snorkel_raised"),
            "snorkel_exposed": systems.get("snorkel_exposed"),
            "snorkel_head_valve": _num(systems.get("snorkel_head_valve")),
            "snorkel_intake_hole": _num(systems.get("snorkel_intake_hole")),
            "snorkel_intake_volume": _num(systems.get("snorkel_intake_volume")),
        },
        "orders_pending": sorted((data.get("pending") or {}).keys()),
        "read_only": data.get("read_only", False),
        "ai_contacts": data.get("ai_contacts_map") or {},
        "ghosts": ghost_count,
        "dl_history": data.get("dl_history") or [],
        "dl_reports": data.get("dl_reports_map") or {},
    }
    return frame


def _seg(text, style=None):
    return (str(text), style)


def threat_style(el):
    """Semantic color token for one table row."""
    if el.get("detected"):
        return "red"
    km = el.get("range_km")
    if km is not None and km < 2.0:
        return "red"
    if km is not None and km < 5.0:
        return "amber"
    if km is not None:
        return "green"
    return "dim"


def detected_cell(el, stale=False):
    if el.get("detected") is None:
        if el.get("src") in ("ns:helo", "ns:plane/sub") or el.get("style") in ("helo", "plane/sub"):
            return "n/a", "dim"
        return "?", "dim"
    if el["detected"]:
        rng = el.get("det_range_m")
        cell = "YES %s" % _range_str((rng or 0) / 1000.0) if rng else "YES"
        if stale:
            cell += "*"
        return cell, "red"
    return "no", "green"


def assignment_cell(el):
    t = el.get("assignment_type")
    aid = el.get("assignment_id")
    parts = []
    if t:
        parts.append(t.upper() if t != "Engage" else "ENGAGE")
    if aid is not None and _num(aid) is not None and int(_num(aid)) >= 0:
        parts.append("#%d" % int(_num(aid)))
    if el.get("incoming"):
        parts.append("+ord")
    if el.get("dipping"):
        parts.append("dip")
    return " ".join(parts) if parts else ("-" if el.get("src") == "state" else "?")


def _table_cols(width):
    cols = [("ID", 3), ("NAME", 14), ("TYP", 3), ("RANGE", 8),
            ("BRG", 4), ("SPD", 6), ("HDG", 4)]
    if width >= 88:
        cols.append(("DEP", 5))
    cols.append(("EOT", 5))
    if width >= 80:
        cols.append(("KTG", 3))
    cols.append(("DET", 10))
    fixed = sum(w + 1 for _, w in cols)
    cols.append(("ASSIGNMENT", max(6, width - fixed)))
    return cols


def render_table(frame, width, sel=0):
    """AI table rows as segment lists. Narrow widths drop columns."""
    els = frame["elements"]
    cols = _table_cols(width)
    col_idx = {c[0]: i for i, c in enumerate(cols)}

    def cells_for(e):
        vals = {
            "ID": str(e["id"]),
            "NAME": e["name"][:cols[col_idx["NAME"]][1]],
            "TYP": e["type"],
            "RANGE": _range_str(e["range_km"]) + {-1: "\u25bc", 1: "\u25b2"}.get(_sign(e.get("delta")), ""),
            "BRG": _brg(e["bearing"]),
            "SPD": (_fmt(_kt(e["speed"]), 1) + "k") if _num(e["speed"]) is not None else "?",
            "HDG": _brg(e["heading"]),
            "DEP": (_fmt(e["depth"], 0) if e["depth"] is not None else "-"),
            "EOT": str(e["eot"])[:5],
            "KTG": str(e["contacts"]) if e["contacts"] is not None else "?",
            "DET": detected_cell(e)[0],
            "ASSIGNMENT": assignment_cell(e),
        }
        return [vals[c[0]][:c[1]] for c in cols]

    # header label -> (label with unit, min column width); falls back to the
    # plain abbrev when the column is too narrow for it
    _HDR_UNITS = {
        "RANGE": ("RANGE km", 8),
        "BRG": ("BRG\u00b0", 4),
        "SPD": ("SPD kt", 6),
        "HDG": ("HDG\u00b0", 4),
        "DEP": ("DEP m", 5),
    }

    def hdr_label(name, w):
        full = _HDR_UNITS.get(name)
        return full[0] if full and len(full[0]) <= w else name

    rows = [[_seg(" %-*s" % (width - 1,
            "".join(hdr_label(c[0], c[1]).ljust(c[1] + 1)
                    for c in cols)[:width - 1]), "hdr")]]
    for i, e in enumerate(els):
        vals = cells_for(e)
        base = threat_style(e)
        dcell_i = col_idx.get("DET")
        dsty = detected_cell(e)[1]
        asg_i = len(cols) - 1
        acell = vals[asg_i]
        segs = []
        for j, c in enumerate(cols):
            st = base
            if j == dcell_i:
                st = dsty
            elif j == asg_i and ("ENGAGE" in acell or "+ord" in acell):
                st = "cyan"
            pad = "" if j == asg_i else " "
            segs.append(_seg(vals[j].ljust(c[1]) + pad, st))
        if sel == i:
            segs = [_seg(">", "amber")] + segs
        else:
            segs = [_seg(" ", None)] + segs
        rows.append(segs)
    return rows


def _sign(v):
    if v is None:
        return 0
    if v > 0.01:
        return -1
    if v < -0.01:
        return 1
    return 0


def _dl_ts(epoch):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(epoch)))
    except (TypeError, ValueError):
        return "??:??:??"


_DL_KIND_STYLES = {
    "ORDER": "cyan", "ADOPTED": "cyan",
    "DETECTED": "red", "DETCLEAR": "dim",
    "ATTACK-OK": "green", "ATTACK-FAIL": "red",
    "GHOST+": "amber", "GHOST-": "amber",
}


def dl_delta_events(prev, cur, now):
    """ORDER/ADOPTED transitions between two {eid: raw-elem} snapshots.
    Elements absent from prev are baseline -> no events (no startup flood)."""
    out = []
    for eid, e in cur.items():
        p = prev.get(eid)
        if not isinstance(p, dict):
            continue
        iord = e.get("incoming_order") or {}
        iord_id = (_num(iord.get("assignment_id"))
                   if isinstance(iord, dict) else None)
        p_iord = p.get("incoming_order") or {}
        p_iord_id = (_num(p_iord.get("assignment_id"))
                     if isinstance(p_iord, dict) else None)
        if iord_id is not None and iord_id >= 0 and iord_id != p_iord_id:
            out.append({"ts_epoch": now, "eid": eid, "kind": "ORDER",
                        "detail": "asg#%s" % int(iord_id)})
        asg = _num(e.get("assignment_id"))
        pasg = p.get("assignment_id")
        if asg is not None and asg >= 0 and asg != pasg:
            out.append({"ts_epoch": now, "eid": eid, "kind": "ADOPTED",
                        "detail": "asg#%s" % int(asg)})
    return out


def detected_delta_events(prev, cur, now):
    """Transition-only detected events (prev/cur: {eid: bool}) — repeated
    identical states never journal (no flooding)."""
    out = []
    for eid, on in cur.items():
        if eid in prev and prev[eid] != bool(on):
            out.append({"ts_epoch": now, "eid": eid,
                        "kind": "DETECTED" if on else "DETCLEAR",
                        "detail": ""})
    return out


def render_datalink_lines(events, width, eid_filter=None, max_rows=12):
    """DATALINK journal rows, newest at bottom; dim timestamp + kind-styled
    remainder. Pure; safe on missing fields."""
    evs = [e for e in (events or [])
           if eid_filter is None or e.get("eid") == eid_filter]
    out = []
    for e in evs[-max(0, int(max_rows)):]:
        kind = str(e.get("kind") or "?")
        ts_txt = _dl_ts(e.get("ts_epoch"))
        rest = ("  #%-3s %-11s %s" % (e.get("eid"), kind,
                                      str(e.get("detail") or "")))
        out.append([_seg(ts_txt[:width], "dim"),
                    _seg(rest[:max(0, width - len(ts_txt))],
                         _DL_KIND_STYLES.get(kind))])
    return out


def render_detail(frame, sel, width):
    els = frame["elements"]
    if not els:
        return [[_seg("no element selected", "dim")]]
    e = els[min(sel, len(els) - 1)]
    rows = []
    title = "DETAIL %s (#%d)" % (e["name"], e["id"])
    rows.append([_seg(title, "hdr"), _seg(" src=%s style=%s" % (e["src"], e.get("style") or "-"), "dim")])
    pos = "pos %s %s" % (_fmt(e["lat"], 3), _fmt(e["lon"], 3)) if e["lat"] is not None else "pos ?"
    rows.append([_seg("%s | rng %s brg %s | spd %skt hdg %s dep %s alt %s" % (
        pos, _range_str(e["range_km"]), _brg(e["bearing"]), _fmt(_kt(e["speed"]), 1),
        _brg(e["heading"]),
        _fmt(e["depth"], 0) if e["depth"] is not None else "-",
        _fmt(e["altitude"], 0) if e["altitude"] is not None else "-"))])
    at = e.get("assignment_type") or "?"
    aprep = "prep=%s" % ({True: "Y", False: "N"}.get(e.get("action_prep"), "?"))
    rows.append([
        _seg("ASG: %s #%s incoming=%s" % (
            at.upper(), _fmt(e.get("assignment_id"), 0),
            "yes" if e.get("incoming") else "no"),
             "cyan" if str(at).lower() == "engage" else None),
        _seg(" %s rpm=%s thr=%s alt=%s" % (
            aprep, _fmt(e["rpm"], 0),
            _fmt(e["throttle"], 2) if e["throttle"] is not None else "?",
            _fmt(e["altitude"], 0) if e["altitude"] is not None else "?"), "dim"),
    ])
    flags = []
    flags.append(("TORP " + ("!" if e.get("torp_threat") else "-"), "red" if e.get("torp_threat") else "dim"))
    flags.append(("MISS " + ("!" if e.get("miss_threat") else "-"), "red" if e.get("miss_threat") else "dim"))
    flags.append(("AIR " + ("!" if e.get("air_threat") else "-"), "red" if e.get("air_threat") else "dim"))
    ammos = "AMMO off=%s def=%s" % (_fmt(e["ammo_off"], 2), _fmt(e["ammo_def"], 2))
    dip = " DIP=%s" % ("on" if e.get("dipping") else "off") if e.get("dipping") is not None else ""
    row = [_seg(ammos + dip)]
    for txt, st in flags:
        row.append(_seg(" " + txt, st))
    rows.append(row)
    wtxt = e.get("weapons") or "armament unknown (not in static DB)"
    rows.append([_seg("ARMS(static DB): ", "dim"), _seg(wtxt)])
    dcell, dsty = detected_cell(e, stale=bool(e.get("det_stale")))
    dline = "TRACK-ON-PLAYER: %s" % dcell
    if e.get("det_contact"):
        dline += " contact=%s" % e["det_contact"]
    if e.get("det_age") is not None:
        dline += " (%ss ago)" % e["det_age"]
    rows.append([_seg(dline, dsty)])
    tgt = []
    if e.get("suspects_n") is not None:
        tgt.append("suspects=%s" % int(e["suspects_n"]))
    if e.get("suspects_ids"):
        sid = str(e["suspects_ids"])
        tgt.append(sid if len(sid) <= 24 else sid[:21] + "…")
    if e.get("tracked_cat") is not None:
        tgt.append("tracked_cat=%s" % int(e["tracked_cat"]))
    if e.get("contact_cache"):
        tgt.append("cache=%s" % e["contact_cache"])
    tgeo = ""
    if e.get("target_lat") is not None and e.get("target_lon") is not None:
        tgeo = " @%s" % _fmt_ll([e["target_lat"], e["target_lon"]])
    if e.get("target_course") is not None:
        tgeo += " crs=%s" % _brg(e["target_course"])
    fire = ""
    if e.get("fire_domain") or e.get("fire_orient"):
        fire = " | FIRE %s -> %s" % (e.get("fire_domain") or "?",
                                     e.get("fire_orient") or "?")
    if tgt or tgeo or fire:
        hot = (e.get("suspects_n") or 0) > 0
        rows.append([_seg("TARGETING: ", "dim"),
                     _seg((" ".join(tgt) + tgeo + fire).strip(),
                          "red" if hot else None)])
    # tactical-AI operator report dump (dl-reports, key g): what the OODA
    # loop of each operator knows about the player right now
    dlr = frame.get("dl_reports") or {}
    ops = dlr.get("operators") or {}
    plr = dlr.get("player") or {}
    if ops:
        for op in sorted(ops):
            o = ops[op]
            meta = o.get("meta") or {}
            hdr = "REPORTS(op %s·%s): " % (op, o.get("mode") or dlr.get("mode") or "player")
            if plr.get("lat") is not None and plr.get("lon") is not None:
                hdr += " @%s" % _fmt_ll([plr.get("lat"), plr.get("lon")])
            rows.append([_seg(hdr, "hdr"),
                         _seg(" real=%s val=%s cyc=%s ag=%s shown=%s/%s" % (
                             meta.get("ai_realism", "?"), meta.get("validity", "?"),
                             meta.get("cycles", "?"), meta.get("count_agents", "?"),
                             o.get("shown", "?"), o.get("entries", "?")), "dim")])
            reps = (o.get("reports") or [])
            if not reps:
                rows.append([_seg("  no player-centred reports", "dim")])
            for rep in reps[:8]:
                km = ""
                if rep.get("player_km") is not None:
                    km = " @%s" % _range_str(rep["player_km"])
                    if rep.get("brg") is not None:
                        km += "@%s" % _brg(rep["brg"])
                rows.append([_seg("  src=%s" % (rep.get("src", "?")), "dim"),
                             _seg(" %s%s crs=%s spd=%skt%s" % (
                                 _fmt_ll([rep.get("lat"), rep.get("lon")]),
                                 km, _fmt(rep.get("course"), 0),
                                 _fmt(rep.get("speed"), 1),
                                 " part" if rep.get("partial") else ""))])
    return rows


def render_player_bar(frame, width):
    p = frame.get("player") or {}
    depth = p.get("depth")
    seg = [_seg("PLAYER %s" % (p.get("name") or "?"), "hdr"),
           _seg(" hdg %s spd %skt depth %sm btm %sm towed %s dmg %.0f%% AMMO %s/%s" % (
               _brg(p.get("heading")), _fmt(_kt(p.get("speed")), 1),
               _fmt(depth, 0), _fmt(p.get("bottom_range"), 0),
               str(p.get("towed") or "?"),
               (p.get("damage") or 0.0) * 100.0,
               _fmt(p.get("ammo_off"), 2), _fmt(p.get("ammo_def"), 2)))]
    return [seg]


def _fmt_ll(lat_lon):
    """[63.4979, 5.9013] -> '63.4979N 005.9013E' or '?'."""
    ll = lat_lon or []
    lat = _num(ll[0]) if len(ll) > 1 else None
    lon = _num(ll[1]) if len(ll) > 1 else None
    if lat is None or lon is None:
        return "?"
    return "%.4f%s %07.4f%s" % (abs(lat), "N" if lat >= 0 else "S",
                                abs(lon), "E" if lon >= 0 else "W")


def render_own_ship_panel(frame, width):
    """Two-line own-sub instrument summary (values in blue accent)."""
    p = frame.get("player") or {}

    def v(label, val):
        return [_seg("%s " % label, "dim"), _seg(str(val), "blue")]

    line1 = v("POS", _fmt_ll(p.get("lat_lon")))
    line1 += [_seg("  HDG ", "dim"), _seg(_brg(p.get("heading")), "blue")]
    line1 += [_seg("(T%s) " % _brg(p.get("true_heading")), "dim"),
              _seg("SPD ", "dim"), _seg("%skt" % _fmt(_kt(p.get("speed")), 1), "blue")]
    line1 += [_seg("(T%skt) " % _fmt(_kt(p.get("true_speed")), 1), "dim"),
              _seg("DEP ", "dim"), _seg("%sm" % _fmt(p.get("depth"), 0), "blue")]
    line1 += [_seg(" ALT ", "dim"), _seg("%sm" % _fmt(p.get("altitude"), 0), "blue"),
              _seg(" BTM ", "dim"), _seg("%sm" % _fmt(p.get("bottom_range"), 0), "blue")]

    eot = str(p.get("ordered_eot") or "?")
    eot = _EOT_SHORT.get(eot, eot[:5])
    dmg = p.get("damage")
    snork = p.get("snorkel_raised")
    sn_state = "?" if snork is None else \
        ("exp" if p.get("snorkel_exposed") else ("up" if snork else "down"))
    line2 = v("ORD CRS", _brg(p.get("ordered_course")))
    line2 += [_seg(" EOT ", "dim"),
              _seg(eot, "amber" if eot not in ("Stop", "?") else "blue"),
              _seg(" DEP ", "dim"), _seg("%sm" % _fmt(p.get("ordered_depth"), 0), "blue"),
              _seg(" RPM ", "dim"), _seg(_fmt(p.get("rpm"), 0), "blue")]
    line2 += [_seg("  MASTS ", "dim"),
              _seg("%s/%s up" % (p.get("masts_up"), p.get("masts_total")), "bright"),
              _seg(" SNORKEL ", "dim"), _seg(sn_state, "cyan" if snork else "dim")]
    line2 += [_seg(" TOWED ", "dim"), _seg(_short_enum(p.get("towed"), 10), "bright")]
    dmg_style = "red" if (dmg or 0) > 0.05 else "green"
    line2 += [_seg(" DMG ", "dim"), _seg("%.0f%%" % ((dmg or 0.0) * 100.0), dmg_style)]
    line2 += [_seg(" AMMO ", "dim"), _seg("%s/%s" % (_fmt(p.get("ammo_off"), 2),
                                                     _fmt(p.get("ammo_def"), 2)), "blue")]
    return [line1, line2]


def render_own_ship_side(frame, box_w=34):
    """Vertical own-sub instrument panel for the right-hand column."""
    p = frame.get("player") or {}
    rows = []

    def v(label, val, st="blue"):
        return [_seg("%-*s" % (6, label), "dim"), _seg(str(val), st)]

    eot = str(p.get("ordered_eot") or "?")
    eot = _EOT_SHORT.get(eot, eot[:5])
    dmg = p.get("damage")
    snork = p.get("snorkel_raised")
    sn_state = "?" if snork is None else \
        ("exp" if p.get("snorkel_exposed") else ("up" if snork else "down"))
    rows.append(v("POS", _fmt_ll(p.get("lat_lon"))))
    rows.append(v("HDG", "%s (T%s)" % (_brg(p.get("heading")),
                                       _brg(p.get("true_heading")))))
    rows.append(v("SPD", "%skt (T%skt)" % (_fmt(_kt(p.get("speed")), 1),
                                           _fmt(_kt(p.get("true_speed")), 1))))
    rows.append(v("DEP", "%sm  ALT %sm  BTM %sm" % (
        _fmt(p.get("depth"), 0), _fmt(p.get("altitude"), 0),
        _fmt(p.get("bottom_range"), 0))))
    rows.append(v("ORD", "CRS %s  EOT %s" % (_brg(p.get("ordered_course")), eot)))
    rows.append([_seg("%-6s" % "", "dim"),
                 _seg("DEP %sm  RPM %s" % (_fmt(p.get("ordered_depth"), 0),
                                           _fmt(p.get("rpm"), 0)), "blue")])
    rows.append(v("MAST", "%s/%s up  SNK %s" % (p.get("masts_up"),
                                                p.get("masts_total"), sn_state),
                  "bright"))
    rows.append(v("TOW", _short_enum(p.get("towed"), 12), "bright"))
    dmg_style = "red" if (dmg or 0) > 0.05 else "green"
    rows.append(v("DMG", "%.0f%%  AMMO %s/%s" % ((dmg or 0.0) * 100.0,
                                                 _fmt(p.get("ammo_off"), 2),
                                                 _fmt(p.get("ammo_def"), 2)), dmg_style))
    rows += render_mast_schema(frame, box_w - 2)
    return rows


# Mast schematic: fixed 5 m scale, up to 4 fill rows above the sail box.
_MAST_SCALE_M = 5.0
_MAST_ROWS_ABOVE = 4

_MAST_PREFIX = (("snorkel", "SNK"), ("radar", "RAD"),
                ("photonics", "P"), ("commantenna", "C"))


def _mast_abbr(m, counters):
    """Snorkel->SNK, Radar1->RAD, Photonics2->P2, CommAntenna1->C1."""
    t = str(m.get("type") or "").lower()
    for pre, ab in _MAST_PREFIX:
        if t.startswith(pre):
            digits = "".join(ch for ch in t if ch.isdigit())
            if digits:
                return ab + digits[0]
            if len(ab) > 1:
                return ab
            counters[ab] = counters.get(ab, 0) + 1
            return "%s%d" % (ab, counters[ab])
    return "M%s" % (m.get("id"),)


def _mast_fill_rows(m):
    """Fill rows above the sail for one mast (0 = retracted/unknown)."""
    if m.get("status") != "Raised":
        return 0
    h = _num(m.get("height"))
    if h is None or h <= 0:
        return 0
    return max(1, min(_MAST_ROWS_ABOVE,
                      int(round(h / _MAST_SCALE_M * _MAST_ROWS_ABOVE))))


def render_mast_schema(frame, width):
    """Sail schematic: one fill bar per mast, sitting exactly on its slot
    centre; bars start inside the sail area and rise by Ausfahrlänge at a
    fixed 5 m scale. Snorkel head marker turns green above the surface.
    Pure function; [] when the width cannot fit the narrowest drawing."""
    p = frame.get("player") or {}
    masts = p.get("masts") or []
    if not masts:
        return []
    cw = None
    for cand in (5, 4, 3):
        if cand * 6 + 7 <= width:
            cw = cand
            break
    if cw is None:
        return []
    n = min(len(masts), 6)
    centers = [1 + i * (cw + 1) + cw // 2 for i in range(n)]
    total_w = n * (cw + 1) + 1

    def merge(chars):
        segs, cur, buf = [], None, ""
        for ch, st in chars:
            s = None if ch == " " else st
            if buf and s == cur:
                buf += ch
            else:
                if buf:
                    segs.append((buf, cur))
                cur, buf = s, ch
        if buf:
            segs.append((buf, cur))
        return segs

    def grid_row(extra=None):
        row = [(" ", None)] * total_w
        for col, cell in (extra or {}).items():
            if 0 <= col < total_w:
                row[col] = cell
        return row

    fills = [_mast_fill_rows(m) for m in masts[:n]]
    snork_idx = next((i for i, m in enumerate(masts[:n])
                      if str(m.get("type") or "").lower().startswith("snorkel")),
                     None)
    exposed = bool(p.get("snorkel_exposed"))
    head_row = (_MAST_ROWS_ABOVE - fills[snork_idx] - 1) \
        if snork_idx is not None and fills[snork_idx] > 0 else -1

    # bar area: blocks stack upward from the sail top edge; the snorkel head
    # square rides one row above the snorkel bar's topmost block
    rows = []
    for j in range(_MAST_ROWS_ABOVE):
        cells = {}
        for i in range(n):
            if (_MAST_ROWS_ABOVE - 1 - j) < fills[i]:
                cells[centers[i]] = ("█", "bright")
        if j == head_row:
            cells[centers[snork_idx]] = ("▪",
                                         "green" if exposed else "blue_dim")
        rows.append(merge(grid_row(cells)))

    # sail box top edge / interior with the in-hull bar stubs / bottom edge
    rows.append([_seg("┌" + "┬".join(["─" * cw] * n) + "┐")])
    mid = grid_row({centers[i]: (("█", "bright")
                                 if masts[i].get("status") == "Raised"
                                 else ("░", "dim")) for i in range(n)})
    for b in range(n + 1):
        mid[b * (cw + 1)] = ("│", None)
    rows.append(merge(mid))
    rows.append([_seg("└" + "┴".join(["─" * cw] * n) + "┘")])

    counters = {}
    abbrs = [_mast_abbr(m, counters) for m in masts[:n]]

    def label_row(texts, style):
        cells = {}
        for i in range(n):
            txt = texts[i][:cw]
            start = centers[i] - len(txt) // 2
            for k, ch in enumerate(txt):
                cells[start + k] = (ch, style)
        return merge(grid_row(cells))

    rows.append(label_row(abbrs, "dim"))
    heights = []
    for m in masts[:n]:
        h = _num(m.get("height"))
        heights.append("?" if h is None else ("-" if h <= 0 else _fmt(h, 1)))
    rows.append(label_row(heights, "blue"))

    sn_up = p.get("snorkel_raised")
    sn_state = "?" if sn_up is None else \
        ("up·exp" if p.get("snorkel_exposed") else ("up" if sn_up else "down"))
    sn_style = "green" if (sn_up and p.get("snorkel_exposed")) else \
        ("cyan" if sn_up else "dim")
    compact = width < 44
    srow = [_seg("SNK " if compact else "SNORKEL ", "dim"),
            _seg(sn_state, sn_style)]
    fmts = [(" HV", 0), (" HL", 2), (" VV", 2)] if compact else \
           [("   HV ", 0), ("   INT-HOLE ", 2), ("   INT-VOL ", 2)]
    keys = ("snorkel_head_valve", "snorkel_intake_hole",
            "snorkel_intake_volume")
    for (lab, dig), key in zip(fmts, keys):
        srow.append(_seg(lab, "dim"))
        srow.append(_seg(_fmt(p.get(key), dig), "blue"))
    tail_len = len("   SCALE 0-%dm" % int(_MAST_SCALE_M))
    cur_len = sum(len(t) for t, _ in srow)
    if cur_len + tail_len <= width:
        srow.append(_seg("   SCALE 0-%dm" % int(_MAST_SCALE_M), "dim"))
    rows.append(srow)
    return rows


def _wrap_segs(segs, width):
    """Greedy word-wrap for a segment row -> list of rows fitting width."""
    tokens = []
    for text, style in segs:
        parts = str(text).split(" ")
        for i, p in enumerate(parts):
            if p:
                tokens.append((p, style))
            if i < len(parts) - 1:
                tokens.append((" ", style))
    rows, cur, ln = [], [], 0
    for tok_text, tok_style in tokens:
        if cur and ln + len(tok_text) > width:
            rows.append(cur)
            cur, ln = [], 0
            if tok_text == " ":
                continue
        cur.append((tok_text, tok_style))
        ln += len(tok_text)
    if cur:
        rows.append(cur)
    return rows or [[("", None)]]


def render_threat_bar(frame, width):
    th = frame["threats"]
    con = frame["contacts"]
    det_by = [e["id"] for e in frame["elements"] if e.get("detected")]
    segs = [_seg("THREATS", "hdr"),
            _seg(" Merged=%s" % _fmt(th["merged"], 0)),
            _seg(" Susp=%s" % _fmt(th["suspicious"], 0), "dim"),
            _seg(" Torp=%s" % ("!" if th["torp"] else "0"),
                 "red" if th["torp"] else "dim"),
            _seg(" Miss=%s" % ("!" if th["miss"] else "0"),
                 "red" if th["miss"] else "dim"),
            _seg(" Air=%s" % ("!" if th["air"] else "0"),
                 "red" if th["air"] else "dim")]
    if det_by:
        segs.append(_seg(" DETECTED-BY:", "red"))
        segs.append(_seg(" %s" % ",".join("#%d" % i for i in det_by), "red"))
    if con["disabled"]:
        segs.append(_seg(" CONTACTS:", "dim"))
        segs.append(_seg(" none (disabled)", "dim"))
    else:
        segs.append(_seg(" CONTACTS-on-player:", "dim"))
        segs.append(_seg(" %s" % con["count"],
                         "amber" if con["count"] else "dim"))
    return _wrap_segs(segs, width)


def render_frame_lines(frame, width, sel=0, detail=False):
    """Full plain-text screen (for --json export and tests)."""
    h = frame["header"]
    head = "MNW TACTICAL | %s (%s) | %s | tension %s | %s%s%s" % (
        h.get("mission") or "?", h.get("operation") or "?",
        str(h.get("clock") or "?").replace("/", "."), h.get("tension") or "?",
        h.get("player") or "?",
        " | PAUSED" if h.get("paused") else "",
        " | %s RO" % h["mode"].upper() if frame.get("read_only") else "")
    lines = [[_seg(head[:width], "hdr")]]
    busy = probe_busy_age(frame)
    if busy is not None:
        txt = "!! PROBE BUSY - no fresh data for %ds (game tick stall) !!" \
              % int(busy)
        pad = max(0, (width - len(txt)) // 2)
        lines.append([_seg((" " * pad + txt)[:width], "amber")])
    lines += render_own_ship_panel(frame, width)
    lines += render_mast_schema(frame, width)
    if not frame.get("elements"):
        lines.append([_seg("-- no AI elements detected --", "dim")])
    else:
        lines += render_table(frame, width, sel=sel)
    lines += render_threat_bar(frame, width)
    if detail:
        lines += render_detail(frame, sel, width)
        dl = render_datalink_lines(frame.get("dl_history") or [], width,
                                   max_rows=12)
        if dl:
            lines.append([_seg("DATALINK:", "dim")])
            lines += dl
    return lines


def flatten(rows):
    return ["".join(t for t, _ in r) for r in rows]


class LocalSource(object):
    def __init__(self, log_dir):
        self.log_dir = log_dir

    def _path(self, name):
        return os.path.join(self.log_dir, name)

    def read_text(self, name, tail=None):
        try:
            with io.open(self._path(name), "r", encoding="utf-8", errors="replace") as f:
                if tail is None:
                    return f.read()
                lines = f.read().splitlines()
                return "\n".join(lines[-tail:])
        except IOError:
            return None

    def write_atomic(self, name, text):
        path = self._path(name)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        if os.name == "posix":
            os.replace(tmp, path)
        else:
            if os.path.isfile(path):
                os.remove(path)
            os.rename(tmp, path)

    def label(self):
        return self.log_dir


class SshSource(object):
    """Reads/writes probe files over `ssh host` (BatchMode, key auth)."""

    def __init__(self, remote, timeout=8):
        self.host, self.rpath = split_remote(remote)
        self.timeout = timeout

    def _run(self, cmd, input_text=None):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % self.timeout,
                self.host, cmd]
        p = subprocess.run(argv, input=input_text, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=self.timeout + 4)
        if p.returncode != 0:
            raise IOError("ssh failed (%d): %s" % (p.returncode, p.stderr.strip()[:200]))
        return p.stdout

    def read_text(self, name, tail=None):
        path = "%s/%s" % (self.rpath, name)
        cmd = "cat %s" % shlex.quote(path)
        if tail is not None:
            cmd = "tail -n %d %s" % (int(tail), shlex.quote(path))
        # missing remote file -> empty output, exit 0 (same as LocalSource -> None)
        return self._run("{ test -e %s && %s ; } || true"
                         % (shlex.quote(path), cmd))

    def write_atomic(self, name, text):
        path = "%s/%s" % (self.rpath, name)
        tmp = path + ".tmp"
        self._run("cat > %s && mv %s %s" % (shlex.quote(tmp), shlex.quote(tmp), shlex.quote(path)),
                  input_text=text)

    def label(self):
        return "%s:%s" % (self.host, self.rpath)


def split_remote(remote):
    if ":" in remote:
        host, _, path = remote.partition(":")
    else:
        host, path = remote, "."
    return host.strip(), path.strip().strip('"').rstrip("/")


def send_commands(source, cmds, floor=-1):
    """Append commands to ship_orders.json with monotonic cmdids. Returns assigned ids.

    floor: lowest cmdid this process has already used — the probe empties the
    orders file after dispatch, so the file's max alone can restart at 0 and
    reuse cmdids (breaking result attribution).
    """
    orders = read_json_text(source.read_text(ORDERS_FILE)) or {"commands": []}
    existing = orders.get("commands") if isinstance(orders, dict) else None
    if not isinstance(existing, list):
        existing = []
    nxt = max(max([c.get("cmdid", -1) for c in existing if isinstance(c, dict)] or [-1]),
              floor) + 1
    ids = []
    new = list(existing)
    for cmd in cmds:
        c = dict(cmd)
        c["cmdid"] = nxt
        new.append(c)
        ids.append(nxt)
        nxt += 1
    if ids:
        source.write_atomic(ORDERS_FILE, json.dumps({"commands": new}, indent=2))
    return ids


def read_json_text(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception as e:
        return {"__error__": "%s: %s" % (type(e).__name__, str(e))}


class Collector(object):
    """Poll loop state machine: reads sources, queues API-cmd refreshes."""

    def __init__(self, source, interval=5.0, read_only=False,
                 detect_interval=10.0, asg_ttl=60.0, ext_ttl=60.0,
                 refresh_after=45.0, count=None, auto_detect=True):
        self.source = source
        self.interval = interval
        self.read_only = read_only
        self.detect_interval = max(10.0, float(detect_interval))
        self.asg_ttl = asg_ttl
        self.ext_ttl = ext_ttl
        self.refresh_after = refresh_after
        self.count = count
        self.auto_detect = bool(auto_detect)
        self._config_detect_auto = None
        self._config_checked = False
        self.cycle = 0
        self.pending = {}
        self.pending_ts = {}           # cid -> queue time (stagnation watch)
        self.prev_ranges = {}
        self.detected_map = {}
        self.detected_result = {}
        self.asg_map = {}
        self.contacts_map = {}
        self.ext_map = {}
        self.dl_reports_map = {}
        self.ns_styles = {}
        self.refresh_queued = False
        self.paused = False
        self._last_cmdid = -1          # floor vs probe clearing ship_orders.json
        self._result_cmdid_max = -1    # highest cmdid ever seen in results
        self._last_detect_ts = 0.0     # time-based detect cadence (>= 10 s)
        self._detect_due = False       # event trigger: element sig changed
        self.sigs = {}                 # eid -> last seen element signature
        self._last_nsdump_cycle = -1000
        self._nsdump_attempts = 0
        # last manual ai-attack outcome (key A/B feedback banner)
        self.attack_status = None
        # datalink journal (Stufe-1 event log per BRIEF_datalink_history.md)
        self.dl_journal = collections.deque(maxlen=500)
        self.dl_filter = None          # None = all events, else one element id
        self._dl_prev_elems = {}       # eid -> raw elem (ORDER/ADOPTED base)
        self._dl_prev_det = {}         # eid -> bool (DETECTED transitions)
        self._dl_prev_ns = {}          # eid -> style (ghost appear/vanish)
        self._pending_attack_eid = {}  # cmdid -> target eid (journal only)
        self._ingested_cmdids = set()  # rotation dedup: ext/asg maps only
        # updated from re-ingestion of ship_results.json

    def send_commands(self, cmds):
        """Guarded write path: no-op list in read_only mode."""
        if self.read_only or not cmds:
            return []
        # floor above BOTH our own history and every cmdid the probe has ever
        # answered: after a TUI restart the running probe's last_cmdid stays
        # in RAM, and ids below it are skipped (and kept) forever
        floor = max(self._last_cmdid, self._result_cmdid_max)
        ids = send_commands(self.source, cmds, floor=floor)
        if ids:
            self._last_cmdid = max(self._last_cmdid, max(ids))
        return ids

    def poll_once(self):
        data = {
            "now": time.time(),
            "interval": self.interval,
            "pending": self.pending,
            "prev_ranges": self.prev_ranges,
            "ns_styles": self.ns_styles,
            "read_only": self.read_only,
            "paused": self.paused,
            "mode": "ssh" if hasattr(self.source, "host") else "local",
        }
        data["ship_state"] = read_json_text(self.source.read_text(STATE_FILE))
        data["ai_state"] = read_json_text(self.source.read_text(AI_STATE_FILE))
        data["results"] = read_json_text(self.source.read_text(RESULTS_FILE))
        data["presence"] = read_json_text(self.source.read_text(PRESENCE_FILE))
        log_tail = self.source.read_text(LOG_FILE, tail=400)
        log_lines = (log_tail or "").splitlines()
        data["log_lines"] = log_lines
        self.ns_styles.update(parse_ns_styles(log_lines))
        self._ingest_results(data, log_lines)
        data["log_detected"] = parse_log_detected(log_lines)
        data["detected_result"] = self.detected_result
        data["asg_map"] = self.asg_map
        data["ext_map"] = self.ext_map
        data["ai_contacts_map"] = self.contacts_map
        data["dl_history"] = list(self.dl_journal)[-200:]
        data["dl_reports_map"] = self.dl_reports_map
        if not self.paused:
            self.cycle += 1
            self._track_signatures(data)
            if not self.read_only:
                self._queue_commands(data)
            self._update_prev(data)
        return build_frame(data)

    def _pop_pending(self, cid):
        self.pending.pop(cid, None)
        self.pending_ts.pop(cid, None)

    def _ingest_results(self, data, log_lines):
        self.detected_result = {}
        results = (data.get("results") or {}).get("results") or []
        newest_det = None
        for r in results:
            if not isinstance(r, dict):
                continue
            cid = r.get("cmdid")
            if isinstance(cid, int) and cid > self._result_cmdid_max:
                # highest id the probe ever answered — floor anchor for
                # sends after a TUI restart (probe keeps last_cmdid in RAM)
                self._result_cmdid_max = cid
            action = r.get("action")
            purpose = self.pending.get(cid)
            if action == "detected" or purpose == "detect":
                newest_det = r if newest_det is None or str(r.get("ts", "")) >= str(newest_det.get("ts", "")) else newest_det
            if purpose == "detect":
                self._pop_pending(cid)
            if purpose == "asg" or action == "asg":
                vals = parse_asg_detail(r.get("detail") or [])
                tid = vals.get("target_id")
                if tid is not None:
                    # Only update asg_map for NEW results (first ingestion).
                    # Re-ingestion of ship_results.json on every cycle would
                    # refresh ts_epoch to data["now"], making all rotation
                    # targets appear fresh — _next_asg_target never fires.
                    if cid not in self._ingested_cmdids:
                        ts_epoch = data["now"]
                        vals["ts_epoch"] = ts_epoch
                        self.asg_map[tid] = vals
                if purpose == "asg":
                    self._pop_pending(cid)
            if purpose == "ext" or action == "ai-state":
                vals = parse_ai_state_detail(r.get("detail") or [])
                tid = vals.get("target_id")
                if tid is not None:
                    # Only update ext_map for NEW results (first ingestion).
                    # Re-ingestion refreshes ts_epoch to data["now"] which
                    # makes _next_ext_target always see age < ext_ttl → no
                    # new ai-state commands ever sent for subs/helos.
                    if cid not in self._ingested_cmdids:
                        ts_epoch = data["now"]
                        vals["ts_epoch"] = ts_epoch
                        self.ext_map[tid] = vals
                if purpose == "ext":
                    self._pop_pending(cid)
            elif purpose == "contacts":
                self.contacts_map = parse_ai_contacts_detail(r.get("detail") or [])
                self._pop_pending(cid)
            elif purpose == "dl":
                self.dl_reports_map = parse_dl_reports_detail(r.get("detail") or [])
                self._pop_pending(cid)
            elif purpose == "refresh":
                self._pop_pending(cid)
                self.refresh_queued = False
            elif purpose == "nsdump":
                detail = r.get("detail") or []
                self.ns_styles.update(parse_ns_styles(detail))
                self._pop_pending(cid)
            elif purpose == "ai-attack" or action == "ai-attack":
                # answered: surface the outcome (key A/B feedback) - the
                # result string carries either "PushOrder ok ..."/"Engage
                # created ..." or the refusal reason
                ok = str(r.get("ok", "")).lower() == "true"
                msg = str(r.get("result") or "")
                if not msg:
                    det = r.get("detail") or []
                    if det:
                        msg = "; ".join(str(x) for x in det[-2:])
                if purpose == "ai-attack":
                    self._pop_pending(cid)
                self.attack_status = {
                    "cmdid": cid, "ok": ok, "msg": msg[:120],
                    "ts_epoch": time.time(),
                }
                aeid = self._pending_attack_eid.pop(cid, None)
                if aeid is not None:
                    self._journal_event(aeid,
                                        "ATTACK-OK" if ok else "ATTACK-FAIL",
                                        msg)
            if isinstance(cid, int) and (action == "asg" or action == "ai-state"):
                self._ingested_cmdids.add(cid)
        if newest_det is not None:
            age = None
            ts = parse_ts(newest_det.get("ts"))
            if ts is not None:
                age = max(0.0, data["now"] - ts)
            self.detected_result = {"map": parse_detected_detail(newest_det.get("detail") or []),
                                    "ts": newest_det.get("ts"), "age_s": age}
        # Cap the dedup set: keep last 1000 cmdids to prevent unbounded growth
        if len(self._ingested_cmdids) > 1000 and self._result_cmdid_max > 0:
            floor = self._result_cmdid_max - 500
            self._ingested_cmdids = {c for c in self._ingested_cmdids if c > floor}
        for eid, det in self.detected_result.get("map", {}).items():
            cur = self.detected_map.get(eid)
            if cur is None or det.get("detected") or not cur.get("detected"):
                self.detected_map[eid] = dict(det, ts=self.detected_result.get("ts"))
        self._journal_deltas(data)

    def _journal_event(self, eid, kind, detail="", ts=None):
        """Append one DATALINK journal entry (bounded ring buffer)."""
        try:
            ts_epoch = time.time() if ts is None else float(ts)
        except (TypeError, ValueError):
            ts_epoch = time.time()
        self.dl_journal.append({"ts_epoch": ts_epoch, "eid": eid,
                                "kind": kind, "detail": str(detail or "")[:80]})

    def _journal_deltas(self, data):
        """Journal ORDER/ADOPTED/DETECTED/ghost transitions for this poll.
        Pure-diff: unchanged states never produce events."""
        now = data.get("now", time.time())
        cur = {}
        for e in ((data.get("ai_state") or {}).get("elements") or []):
            if e.get("id") is not None:
                cur[e.get("id")] = e
        for ev in dl_delta_events(self._dl_prev_elems, cur, now):
            self.dl_journal.append(ev)
        det_now = {eid: bool((d or {}).get("detected"))
                   for eid, d in self.detected_map.items()}
        for ev in detected_delta_events(self._dl_prev_det, det_now, now):
            self.dl_journal.append(ev)
        gone = sorted(eid for eid in self._dl_prev_ns
                      if eid not in self.ns_styles)
        new = sorted(eid for eid, st in self.ns_styles.items()
                     if eid not in self._dl_prev_ns)
        for eid in gone:
            self._journal_event(eid, "GHOST-",
                                self._dl_prev_ns.get(eid) or "", now)
        for eid in new:
            self._journal_event(eid, "GHOST+",
                                self.ns_styles.get(eid) or "", now)
        self._dl_prev_elems = cur
        self._dl_prev_det = det_now
        self._dl_prev_ns = dict(self.ns_styles)

    def _auto_detect_enabled(self):
        """Auto-detected-scan gate: CLI auto_detect AND the probe-side config
        key detect_auto (default true). One config switch on the probe side
        therefore disables the periodic scan cluster-wide (crash escape hatch
        for fragile missions); manual 'd' still sends the command (the probe
        answers with a disabled-scan hint)."""
        if not self.auto_detect:
            return False
        if not self._config_checked:
            self._config_checked = True
            try:
                cfg = read_json_text(self.source.read_text(CONFIG_FILE)) or {}
                self._config_detect_auto = bool(cfg.get("detect_auto", True))
            except Exception:
                self._config_detect_auto = True
        return self._config_detect_auto is not False

    def _queue_commands(self, data):
        cmds = []
        # stagnation watchdog: pendings older than 45 s mean the probe never
        # answered (e.g. TUI restart wrote cmdids below the running probe's
        # last_cmdid — it skips those forever). Drop the dead pendings so the
        # guards re-arm, and rebase the floor above every id the probe has
        # ever answered (results file is the persistent record).
        stale_cut = data["now"] - 45.0
        stale = [cid for cid, t in self.pending_ts.items() if t < stale_cut]
        if stale:
            for cid in stale:
                self._pop_pending(cid)
            if self._result_cmdid_max > self._last_cmdid:
                self._last_cmdid = self._result_cmdid_max
        ship_ts = (data.get("ship_state") or {}).get("ts")
        ts = parse_ts(ship_ts)
        stale = ts is None or (data["now"] - ts) > self.refresh_after
        has_refresh_inflight = any(v == "refresh" for v in self.pending.values())
        if stale and not has_refresh_inflight:
            cmds.append({"action": "planes"})
        # ghost discovery: helo/sub/ship rows come from ns-style log lines;
        # after a mission restart the 400-line tail has none, so keep queueing
        # one ns-dump until a ghost style actually shows up - first 3 tries at
        # a short cadence, then a relaxed one to keep the orders queue clean.
        # Once ANY ghost is known, drop to a slow MAINTENANCE cadence: hosts
        # whose single begin() dump rolled out of the log tail (legacy probes
        # emit the sub's 'plane' style exactly once) are re-found this way.
        if not self._has_ghost_style():
            gap = 10 if self._nsdump_attempts < 3 else 30
            if self.cycle - self._last_nsdump_cycle >= gap:
                cmds.append({"action": "ns-dump"})
                self._last_nsdump_cycle = self.cycle
                self._nsdump_attempts += 1
        elif self.cycle - self._last_nsdump_cycle >= 24:
            cmds.append({"action": "ns-dump"})
            self._last_nsdump_cycle = self.cycle
        # detected scan: base cadence (>= 10 s) plus event trigger whenever
        # any element's signature changed (range/heading/assignment/...) —
        # but only when auto-detect is enabled (CLI flag + probe config)
        due = (self._detect_due or
               (data["now"] - self._last_detect_ts) >= self.detect_interval) \
            and self._auto_detect_enabled()
        has_detect = any(v == "detect" for v in self.pending.values())
        if due and not has_detect:
            cmds.append({"action": "detected"})
            self._last_detect_ts = data["now"]
            self._detect_due = False
        target = self._next_asg_target(data)
        if target is not None:
            cmds.append({"action": "asg", "id": int(target)})
        target_ext = self._next_ext_target(data)
        if target_ext is not None:
            cmds.append({"action": "ai-state", "id": int(target_ext)})
        if not cmds:
            return
        ids = self.send_commands(cmds)
        purposes = []
        for c in cmds:
            if c["action"] == "planes":
                purposes.append("refresh")
                self.refresh_queued = True
            elif c["action"] == "detected":
                purposes.append("detect")
            elif c["action"] == "asg":
                purposes.append("asg")
            elif c["action"] == "ai-state":
                purposes.append("ext")
            elif c["action"] == "ai-contacts":
                purposes.append("contacts")
            elif c["action"] == "ns-dump":
                purposes.append("nsdump")
        for i, p in zip(ids, purposes):
            self.pending[i] = p
            self.pending_ts[i] = data["now"]

    def _has_ghost_style(self):
        return any(s in ("helo", "plane/sub", "plane", "ship")
                   for s in self.ns_styles.values())

    def _next_asg_target(self, data):
        own = own_element_ids(data.get("ship_state") or {})
        ids = []
        for e in ((data.get("ai_state") or {}).get("elements") or []):
            eid = e.get("id")
            # id 0 is the contextless host module (no assignment data) and
            # own-element ids are the player — never waste probes on them
            if eid is None or eid == 0 or eid in own:
                continue
            ids.append(eid)
        for eid in sorted(self.ns_styles):
            if self.ns_styles[eid] in ("helo", "plane/sub") and eid not in ids \
                    and eid != 0 and eid not in own:
                ids.append(eid)
        best, best_age = None, -1.0
        for eid in ids:
            info = self.asg_map.get(eid) or {}
            age = data["now"] - info["ts_epoch"] if info.get("ts_epoch") else None
            if age is None:
                return eid
            if age > best_age:
                best, best_age = eid, age
        return best if best_age > self.asg_ttl else None

    def _next_ext_target(self, data):
        """Rotation over command-only hosts (helo / sub) lacking ai_state."""
        state_ids = set()
        for e in ((data.get("ai_state") or {}).get("elements") or []):
            if e.get("id") is not None:
                state_ids.add(e["id"])
        own = own_element_ids(data.get("ship_state") or {})
        candidates = [eid for eid in sorted(self.ns_styles)
                      if eid != 0 and eid not in own and eid not in state_ids]
        best, best_age = None, -1.0
        for eid in candidates:
            info = self.ext_map.get(eid) or {}
            age = data["now"] - info["ts_epoch"] if info.get("ts_epoch") else None
            if age is None:
                return eid
            if age > best_age:
                best, best_age = eid, age
        return best if best_age > self.ext_ttl else None

    def force_detect(self):
        if self.read_only or any(v == "detect" for v in self.pending.values()):
            return False
        ids = self.send_commands([{"action": "detected"}])
        if ids:
            self.pending[ids[0]] = "detect"
            self.pending_ts[ids[0]] = time.time()
            self._last_detect_ts = time.time()
            self._detect_due = False
            return True
        return False

    def force_contacts(self):
        if self.read_only or any(v == "contacts" for v in self.pending.values()):
            return False
        ids = self.send_commands([{"action": "ai-contacts"}])
        if ids:
            self.pending[ids[0]] = "contacts"
            self.pending_ts[ids[0]] = time.time()
            return True
        return False

    def force_dl(self):
        """Queue one dl-reports probe (player-centred operator reports with
        full deep reads of every report object)."""
        if self.read_only or any(v == "dl" for v in self.pending.values()):
            return False
        ids = self.send_commands([{"action": "dl-reports", "deep": True,
                                   "all_reps": True}])
        if ids:
            self.pending[ids[0]] = "dl"
            self.pending_ts[ids[0]] = time.time()
            return True
        return False

    def force_refresh(self):
        if self.read_only or any(v == "refresh" for v in self.pending.values()):
            return False
        ids = self.send_commands([{"action": "planes"}])
        if ids:
            self.pending[ids[0]] = "refresh"
            self.pending_ts[ids[0]] = time.time()
            return True
        return False

    def force_ext(self, eid=None):
        """Queue one ai-state probe (default: next command-only host)."""
        if self.read_only:
            return False
        if any(v == "ext" for v in self.pending.values()):
            return False
        data = {"now": time.time(), "ship_state": read_json_text(
            self.source.read_text(STATE_FILE))}
        target = eid if eid is not None else self._next_ext_target(data)
        if target is None:
            return False
        ids = self.send_commands([{"action": "ai-state", "id": int(target)}])
        if ids:
            self.pending[ids[0]] = "ext"
            self.pending_ts[ids[0]] = time.time()
            return True
        return False

    def queue_ai_attack(self, eid, allow_untracked=False):
        """Manual strike order on the selected element: writes
        {"action":"ai-attack","id":N} via the guarded send path. The probe's
        do_ai_attack runs the heavy C# work on its own host (multi-host safe,
        ownership-checked); this only queues and tracks the cmdid.
        allow_untracked=true fires blind (probe's track gate is skipped) -
        bound to key B; key A keeps the safe refuse-if-untracked behavior."""
        if self.read_only or eid is None or int(eid) <= 0:
            return False
        cmd = {"action": "ai-attack", "id": int(eid)}
        if allow_untracked:
            cmd["allow_untracked"] = True
        ids = self.send_commands([cmd])
        if ids:
            self.pending[ids[0]] = "ai-attack"
            self.pending_ts[ids[0]] = time.time()
            self._pending_attack_eid[ids[0]] = int(eid)
            return True
        return False

    def _element_sig(e):
        """Compact change signature of one raw ai_state element."""
        km = _num(e.get("to_player_range_km"))
        hdg = _num(e.get("true_heading", e.get("heading")))
        spd = _num(e.get("true_speed", e.get("speed")))
        iord = e.get("incoming_order") or {}
        iord_id = _num(iord.get("assignment_id")) if isinstance(iord, dict) else None
        prep = e.get("action_prep_complete")
        return (
            round(km, 2) if km is not None else None,
            round(hdg) if hdg is not None else None,
            round(spd, 1) if spd is not None else None,
            e.get("assignment_id"),
            iord_id if iord_id is not None and iord_id >= 0 else None,
            prep if isinstance(prep, bool) else None,
            e.get("contact_count"),
        )

    _element_sig = staticmethod(_element_sig)

    def _track_signatures(self, data):
        """Event trigger for the detected scan: fire when any element changes."""
        changed = False
        cur = {}
        for e in ((data.get("ai_state") or {}).get("elements") or []):
            eid = e.get("id")
            if eid is None or eid == 0:
                continue
            sig = self._element_sig(e)
            cur[eid] = sig
            if self.sigs.get(eid) != sig:
                changed = True
        # only react to *re*changes once we have seen a first state
        if self.sigs and changed:
            self._detect_due = True
        self.sigs = cur

    def _update_prev(self, data):
        ai = data.get("ai_state") or {}
        for e in (ai.get("elements") or []):
            eid = e.get("id")
            km = _num(e.get("to_player_range_km"))
            if eid is None or km is None:
                continue
            old = self.prev_ranges.get(eid, {})
            self.prev_ranges[eid] = {"km": km, "prev": old.get("km")}


# ----------------------------------------------------------------
# curses UI (green phosphor base + semantic state accents)
# ----------------------------------------------------------------

_PAIRS = {
    "green": curses.COLOR_GREEN,
    "white": curses.COLOR_WHITE,
    "red": curses.COLOR_RED,
    "yellow": curses.COLOR_YELLOW,
    "cyan": curses.COLOR_CYAN,
    "magenta": curses.COLOR_MAGENTA,
    "blue": curses.COLOR_BLUE,
}

# style token -> (pair color, extra attrs). Tokens used by renderers:
#   green/bright/dim  phosphor base (values / emphasis / secondary)
#   hdr               title bars (black-on-green via _attr)
#   red               close threat / detected-you (bold)
#   amber             medium range / caution
#   cyan              fresh orders / active links
#   magenta           weapon threats (torpedo/missile/dipping)
#   blue              own-ship instrument values
#   blue_dim          submerged / inactive water-related markers
#   white             neutral highlight
_STYLES = {
    "green": ("green", 0),
    "bright": ("green", curses.A_BOLD),
    "dim": ("green", 0),   # secondary text: plain green (A_DIM was unreadable)
    "hdr": ("green", curses.A_BOLD),
    "red": ("red", curses.A_BOLD),
    "amber": ("yellow", 0),
    "amber_b": ("yellow", curses.A_BOLD),
    "cyan": ("cyan", 0),
    "magenta": ("magenta", curses.A_BOLD),
    "blue": ("blue", curses.A_BOLD),
    "blue_dim": ("blue", 0),
    "white": ("white", 0),
}


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    if curses.can_change_color():
        try:
            # lift the base green toward a readable mint on dark terminals
            curses.init_color(curses.COLOR_GREEN, 350, 1000, 400)
        except curses.error:
            pass
    for i, fg in enumerate(_PAIRS.values(), start=1):
        curses.init_pair(i, fg, -1)
    curses.init_pair(len(_PAIRS) + 1, curses.COLOR_BLACK, curses.COLOR_GREEN)


def _attr(style, colors=True):
    if not style:
        return 0
    if style == "rev":
        return curses.A_REVERSE if colors else curses.A_REVERSE
    if style == "sel":
        return (curses.A_REVERSE | curses.A_BOLD) if colors else curses.A_BOLD
    spec = _STYLES.get(style)
    if not spec:
        return 0
    fg, extra = spec
    if style == "hdr":
        if colors:
            return curses.color_pair(len(_PAIRS) + 1) | curses.A_BOLD
        return curses.A_REVERSE | curses.A_BOLD
    if not colors:
        return extra
    n = list(_PAIRS.keys()).index(fg) + 1
    return curses.color_pair(n) | extra


def _draw_rows(scr, rows, y0, width, colors=True, x0=0, max_h=None):
    h, w = scr.getmaxyx()
    for ri, row in enumerate(rows):
        y = y0 + ri
        if y >= h - 1 or (max_h is not None and ri >= max_h):
            break
        x = x0
        try:
            for text, style in row:
                if x >= x0 + width:
                    break
                t = text[:max(0, x0 + width - x)]
                scr.addnstr(y, x, t, x0 + width - x, _attr(style, colors))
                x += len(t)
        except curses.error:
            pass


def _draw_box(scr, y0, width, height, title, colors=True, x0=0):
    """Bordered panel with embedded title; returns (inner_y, inner_x, inner_w)."""
    h, w = scr.getmaxyx()
    width = max(4, min(width, w - x0))
    height = max(2, min(height, h - y0))
    attr_b = _attr("green", colors)
    try:
        scr.hline(y0, x0 + 1, curses.ACS_HLINE, width - 2, attr_b)
        scr.hline(y0 + height - 1, x0 + 1, curses.ACS_HLINE, width - 2, attr_b)
        scr.vline(y0 + 1, x0, curses.ACS_VLINE, height - 2, attr_b)
        scr.vline(y0 + 1, x0 + width - 1, curses.ACS_VLINE, height - 2, attr_b)
        scr.addch(y0, x0, curses.ACS_ULCORNER, attr_b)
        scr.addch(y0, x0 + width - 1, curses.ACS_URCORNER, attr_b)
        scr.addch(y0 + height - 1, x0, curses.ACS_LLCORNER, attr_b)
        scr.addch(y0 + height - 1, x0 + width - 1, curses.ACS_LRCORNER, attr_b)
        scr.addnstr(y0, x0 + 2, " %s " % title, max(0, width - 4), _attr("hdr", colors))
    except curses.error:
        pass
    return y0 + 1, x0 + 1, width - 2


HELP_LINE = " q quit | \u2191\u2193 sel | TAB detail | d detect | e ai-state | a contacts | g dl-rep | r refresh | p pause | +/- intv | c color | l dl-flt | A attack | B blind "

PROBE_BUSY_S = 15.0


def probe_busy_age(frame):
    """Seconds since the last ship_state refresh when the probe looks stalled
    (game tick holes), else None. Pure; safe on missing ages."""
    age = (frame.get("ages") or {}).get("ship_state")
    if age is None:
        return None
    return age if age > PROBE_BUSY_S else None

DETAIL_ROWS_BASE = 7


SIDE_MIN_WIDTH = 100
SIDE_BOX_W = 34


def run_curses(scr, collector, args):
    colors = not args.no_color
    if colors:
        _init_colors()
    vis = [3]  # visible table rows, kept updated by draw() for scroll clamping

    def draw(fr, sel, offset, detail):
        scr.erase()
        h, w = scr.getmaxyx()
        width = min(w, 200)
        side_w = SIDE_BOX_W if width >= SIDE_MIN_WIDTH else 0
        left_w = width - side_w
        head_lines = render_frame_lines(fr, left_w if side_w else width,
                                        sel=sel, detail=False)[:2]

        _draw_rows(scr, head_lines, 0, width, colors)
        y = len(head_lines)
        all_trows = (render_table(fr, left_w, sel=sel)
                     if fr.get("elements") else [])
        hdr_row = all_trows[0] if all_trows else None
        table_rows = all_trows[1:]
        bar_w = (left_w if side_w else width) - 2
        threat_rows = render_threat_bar(fr, bar_w)

        _draw_rows(scr, head_lines, 0, width, colors)
        y = len(head_lines)

        if not side_w:
            own_rows = render_own_ship_panel(fr, width)
            own_rows += render_mast_schema(fr, width - 2)
            iy, ix, iw = _draw_box(scr, y, width, len(own_rows) + 2,
                                   "OWN SHIP", colors)
            _draw_rows(scr, own_rows, iy, iw, colors, x0=ix)
            y += len(own_rows) + 2

        footer_y = h - 2  # 2 rows reserved: help line (h-1) + attack status (h-2)
        det_h = max(DETAIL_ROWS_BASE, (footer_y - y) // 2) if detail \
            else len(threat_rows) + 2
        avail = max(3, footer_y - det_h - y)
        vis_n = max(3, min(len(table_rows), avail - 3))
        vis[0] = vis_n
        total = len(table_rows)
        title = "AI CONTACTS"
        if total:
            title += " %d-%d/%d" % (offset + 1, min(offset + vis_n, total), total)
        cy, cx, cw = _draw_box(scr, y, left_w, vis_n + 3, title, colors)
        if hdr_row is not None:
            _draw_rows(scr, [hdr_row], cy, cw, colors, x0=cx)
        _draw_rows(scr, table_rows[offset:offset + vis_n], cy + 1, cw,
                   colors, x0=cx, max_h=vis_n)
        y += vis_n + 3

        if side_w and fr is not None:
            # right column runs from under the header down to the footer;
            # THREATS/DETAIL stay inside the LEFT column so nothing bleeds
            # into this box
            own_side = render_own_ship_side(fr)
            oy, ox, ow = _draw_box(scr, 1, side_w, min(len(own_side) + 2,
                                                       footer_y - 1),
                                   "OWN SHIP", colors, x0=left_w)
            _draw_rows(scr, own_side, oy, ow, colors, x0=ox,
                       max_h=max(0, footer_y - oy))

        if detail and fr.get("elements"):
            el = fr["elements"][min(sel, len(fr["elements"]) - 1)]
            det_rows = render_detail(fr, sel, cw)
            ac = fr.get("ai_contacts") or {}
            if ac.get(el["id"]):
                det_rows.append([_seg("AI-CONTACTS:", "dim")])
                for c in ac[el["id"]][:max(0, det_h - len(det_rows) - 2)]:
                    det_rows.append([_seg(" %s rng=%s brg=%s crs=%s spd=%skt" % (
                        c.get("id", "?"), _fmt(c.get("range_m"), 0),
                        _brg(c.get("bearing")), _fmt(c.get("course"), 0),
                        _fmt(_kt(c.get("speed")), 1)), None)])
            ty, tx, tw = _draw_box(scr, y, cw + 2, det_h,
                                   "DETAIL #%s" % el["id"], colors)
            _draw_rows(scr, det_rows, ty, tw, colors, x0=tx, max_h=det_h - 1)
            # DATALINK journal below DETAIL (detail mode only, variant a of
            # the brief: TAB toggles both; normal layout never shifts)
            dl_all = render_datalink_lines(fr.get("dl_history") or [], cw,
                                           eid_filter=collector.dl_filter)
            y2 = y + det_h
            dl_h = min(len(dl_all) + 2, max(0, footer_y - y2))
            if dl_h >= 3:
                # auto-scroll: show newest entries that fit (bottom slice)
                dl_rows = dl_all[-max(0, dl_h - 1):]
                dty, dtx, dtw = _draw_box(
                    scr, y2, cw + 2, dl_h,
                    "DATALINK%s" % (" #%s" % collector.dl_filter
                                    if collector.dl_filter is not None else ""),
                    colors)
                _draw_rows(scr, dl_rows, dty, dtw, colors, x0=dtx,
                           max_h=dl_h - 1)
        else:
            ty, tx, tw = _draw_box(scr, y, left_w if side_w else width, det_h,
                                   "THREATS", colors)
            _draw_rows(scr, threat_rows, ty, tw, colors, x0=tx)

        ages = fr.get("ages") or {}
        sa = ages.get("ship_state")
        aa = ages.get("ai_state")
        footer = "%s| poll %.0fs | data s:%s ai:%s | ghosts:%d%s%s" % (
            HELP_LINE, collector.interval,
            "%ss" % _fmt(sa, 0) if sa is not None else "?",
            "%ss" % _fmt(aa, 0) if aa is not None else "?",
            fr.get("ghosts", 0),
            " | PAUSED" if collector.paused else "",
            " RO" if collector.read_only else "")
        try:
            scr.addnstr(h - 1, 0, footer[:width - 1], width - 1,
                        _attr("dim", colors))
        except curses.error:
            pass
        # last manual ai-attack outcome (keys A/B): show for ~12 s
        st = collector.attack_status
        if st and time.time() - st.get("ts_epoch", 0) < 12.0:
            mark = "OK" if st.get("ok") else "FAILED"
            line = " ATTACK #%s %s : %s " % (
                st.get("cmdid"), mark, st.get("msg") or "")
            style = "green" if st.get("ok") else "red"
            attr = _attr(style, colors) if colors else \
                (curses.A_BOLD if st.get("ok") else
                 curses.A_REVERSE | curses.A_BOLD)
            try:
                scr.addnstr(h - 2, 0, line[:width - 1], width - 1, attr)
            except curses.error:
                pass
        scr.refresh()

    sel = 0
    offset = 0
    detail = False
    frame = None
    last_poll = 0.0
    done = 0
    while True:
        now = time.time()
        if frame is None or (not collector.paused and
                             now - last_poll >= collector.interval):
            h, w = scr.getmaxyx()
            msg = " fetching (%s) ..." % collector.source.label()
            try:
                scr.addnstr(h - 1, 0, msg[:w - 1], w - 1,
                            _attr("amber", colors) if colors else curses.A_BOLD)
                scr.refresh()
            except curses.error:
                pass
            frame = collector.poll_once()
            last_poll = time.time()
            done += 1
            sel = min(sel, max(0, len(frame["elements"]) - 1))
            draw(frame, sel, offset, detail)
            if args.count is not None and done >= args.count:
                return frame
        scr.timeout(150)
        ch = scr.getch()
        if ch == -1:
            continue
        nels = len(frame["elements"])
        if ch in (ord("q"), 27):
            return frame
        elif ch in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = min(max(0, nels - 1), sel + 1)
        elif ch == ord("\t"):
            detail = not detail
        elif ch == ord("p"):
            collector.paused = not collector.paused
            if not collector.paused:
                last_poll = 0.0
        elif ch in (ord("+"), ord("=")):
            collector.interval = min(60.0, collector.interval * 1.5)
        elif ch in (ord("-"), ord("_")):
            collector.interval = max(2.0, collector.interval / 1.5)
        elif ch == ord("d"):
            collector.force_detect()
        elif ch == ord("e"):
            collector.force_ext()
        elif ch == ord("a"):
            collector.force_contacts()
        elif ch == ord("g"):
            collector.force_dl()
        elif ch == ord("r"):
            collector.force_refresh()
        elif ch == ord("c"):
            colors = not colors
        elif ch == ord("l"):
            # datalink filter cycle: all -> selected element -> all
            if collector.dl_filter is None and nels:
                collector.dl_filter = frame["elements"][sel]["id"]
            else:
                collector.dl_filter = None
        elif ch in (ord("A"), ord("B")):
            # manual ai-attack on the selected element - two-step confirm so
            # a stray keypress cannot fire heavy C# work on the game host.
            # A = safe (refused when the element has no track on player),
            # B = blind (allow_untracked:true overrides the probe's gate)
            blind = ch == ord("B")
            if nels:
                e = frame["elements"][sel]
                msg = " AI-ATTACK%s %s #%d : %sy = fire, any other = cancel " % (
                    "-BLIND" if blind else "", e.get("name") or "?", e["id"],
                    "UNTRACKED OK! " if blind else "")
                draw(frame, sel, offset, detail)
                try:
                    h2, w2 = scr.getmaxyx()
                    scr.addnstr(h2 - 1, 0, msg[:w2 - 1], w2 - 1,
                                curses.A_REVERSE | curses.A_BOLD)
                    scr.refresh()
                except curses.error:
                    pass
                scr.timeout(5000)
                ch2 = scr.getch()
                scr.timeout(150)
                if ch2 == ord("y"):
                    collector.queue_ai_attack(e["id"],
                                              allow_untracked=blind)
        # scroll window follows the selection using the real visible row count
        if nels and sel < offset:
            offset = sel
        if nels and sel >= offset + vis[0]:
            offset = sel - vis[0] + 1
        draw(frame, sel, offset, detail)


def main(argv=None):
    ap = argparse.ArgumentParser(description="MNW AI Tactical View (htop-style)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--log-dir", help="probe log dir (local files)")
    src.add_argument("--remote", help='ssh fetch: user@host:"/abs/log/dir"')
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="dump one frame as strict JSON and exit")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--read-only", action="store_true", help="never write ship_orders.json")
    ap.add_argument("--detect-interval", type=float, default=10.0,
                    help="detected scan base cadence in seconds (min 10; also "
                         "fires early when an element's state changes)")
    ap.add_argument("--no-auto-detect", action="store_true",
                    help="disable the periodic detected scan (keep manual d; "
                         "escape hatch for missions that crash on the scan)")
    ap.add_argument("--asg-ttl", type=float, default=60.0, help="re-query asg after N seconds")
    args = ap.parse_args(argv)

    if args.remote:
        source = SshSource(args.remote)
    else:
        source = LocalSource(args.log_dir or os.getcwd())
    collector = Collector(source, interval=args.interval, read_only=args.read_only,
                          detect_interval=args.detect_interval, asg_ttl=args.asg_ttl,
                          count=args.count, auto_detect=not args.no_auto_detect)
    if args.json:
        def _dump(frame):
            return {"source": source.label(), "frame": frame,
                    "lines": flatten(render_frame_lines(frame, 100))}
        if args.count is not None and args.count > 1:
            # NDJSON stream: one compact frame per poll (automation/tests)
            for _ in range(args.count):
                sys.stdout.write(json.dumps(_dump(collector.poll_once()),
                                            allow_nan=False, default=str) + "\n")
                sys.stdout.flush()
            return 0
        out = _dump(collector.poll_once())
        json.dump(out, sys.stdout, indent=2, allow_nan=False, default=str)
        sys.stdout.write("\n")
        return 0
    try:
        return curses.wrapper(run_curses, collector, args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
