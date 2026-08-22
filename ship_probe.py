# -*- coding: utf-8 -*-
"""
MNW Ship Data Probe
===================
In-game runtime probe that reads ALL own-ship data (position, course, speed,
depth, systems, contacts, sonar, mission state) from the host element's
script context and writes it as JSON. It also supports a small control set
(helm / plot / clear-plot / report / probe) to verify that the runtime API
can not only be read but also steered.

Deploy via deploy.py (piggyback into the game AI scripts, same mechanism as
the director). The game calls _random_tick_ on element-bound scripts; the
piggyback block calls ship_probe_tick(globals()) with the host context.

Outputs (into the resolved log dir, see _resolve_log_dirs):
  ship_state.json      latest own-ship snapshot (written only for the player)
  ship_probe.json      one-time component discovery result (API capability map)
  ship_orders.json     command queue (outside -> game), cmdid protocol
  ship_results.json    command results (game -> outside)
  ship_probe_log.txt   tailable event log
  ship_probe.lock      leader election lock
"""

import os
import sys
import json
import time
import io
import traceback
import re

_CONFIG_FILE = "ship_probe_config.json"
_LOG_NAME = "ship_probe_log.txt"
_STATE_NAME = "ship_state.json"
_PROBE_NAME = "ship_probe.json"
_AI_STATE_NAME = "ai_state.json"
_AI_FRAG_PREFIX = "ai_frag."
_AI_FRAG_TTL_S = 120
_API_PROBE_NAME = "ship_probe_api.json"
_ORDERS_NAME = "ship_orders.json"
_RESULTS_NAME = "ship_results.json"
_LOCK_NAME = "ship_probe.lock"
_LOCK_STALE_S = 30 * 60

_DEFAULTS = {
    "log_dir": "",
    "tick_delay": 30,
    "heartbeat_every": 120,
    "console_log": True,
    "require_player": True,
    "target_element_id": 0,
    "max_contacts": 50,
    "max_commands_per_cycle": 10,
    "allow_commands": ["helm", "planes", "sd-dump", "tanks", "env", "plot", "clear-plot", "report", "probe", "ai-attack", "detected", "wc-dump", "steer", "ns-dump", "asg", "ai-contacts", "alarm", "sonctl", "tracker", "masts", "explore", "tracker-new", "dc"],
    "resolve_positions": False,
    "state_every": 3,
    "read_contacts": True,
    "read_sonar": True,
    "read_sonar_arrays": False,
    "max_sonar_arrays": 8,
    "max_sonar_contacts": 20,
    "read_ai": True,
    "max_ai_elements": 30,
    "measure_perf": False,
}

_MAST_STATUS = {"retracted": 0, "moving": 1, "raised": 2}

# Singletons / types the game injects into element scripts. When we run via
# an older piggyback block (no host globals), resolve them by clr-importing
# the mnw.* modules directly.
_CLR_FALLBACKS = {
    "IActCommon": ("mnw.Core", "mnw.Scenarios", "mnw"),
    "IPrepCommon": ("mnw.Core", "mnw.Scenarios", "mnw"),
    "ActCommon": ("mnw.Core", "mnw.Scenarios", "mnw"),
    "PrepCommon": ("mnw.Core", "mnw.Scenarios", "mnw"),
    "ScenarioManager": ("mnw.Scenarios", "mnw"),
    "GeoCord": ("mnw.Core", "mnw"),
    "Waypoint": ("mnw.Core", "mnw"),
    "ElementTools": ("mnw.Core", "mnw"),
    "ContactTools": ("mnw.Core", "mnw"),
    "MathTools": ("mnw.Core", "mnw"),
    "NavTools": ("mnw.Core", "mnw"),
    "MechTools": ("mnw.Core", "mnw"),
    "ClockManager": ("mnw.Core", "mnw.Systems", "mnw"),
    "CoordinatesManager": ("mnw.Core", "mnw.Systems", "mnw"),
    "Navigation": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "SteeringDiving": ("mnw.Systems", "mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "Integrity": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "AmmunitionStorage": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "Maneuvering": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "Coxswain": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "FireControl": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "LauncherController": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "Order": ("mnw.AI", "mnw"),
    "AITools": ("mnw.AI", "mnw.Scenarios.Missions", "mnw.Scenarios", "mnw.Scripting", "mnw.Core", "mnw"),
    "Engage": ("mnw.Scenarios.Missions.Assignments", "mnw.Scenarios.Missions", "mnw"),
    "ASW": ("mnw.Scenarios.Missions.Assignments", "mnw.Scenarios.Missions", "mnw"),
    "Transit": ("mnw.Scenarios.Missions.Assignments", "mnw.Scenarios.Missions", "mnw"),
    "TransitSpeed": ("mnw.Scenarios.Missions.Assignments", "mnw.Scenarios.Missions", "mnw"),
    "TowedController": ("mnw.Systems", "mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "MastsController": ("mnw.Systems", "mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "Snorkel": ("mnw.Systems", "mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "DepthGauge": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "AltitudedGauge": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "OpticalSystem": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "ActiveSonar": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "PassiveSonar": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
    "SonarSystem": ("mnw.Systems", "mnw.unity", "mnw"),
    "UnityScriptComponent": ("mnw.Core", "mnw.unity", "mnw.Scenarios", "mnw"),
}

_probe = None
_HOST = None
_LOCK = None

# Names a script must carry in its globals() to be treated as an element-bound
# script (has injected blackboard/identity). operational_ai.py and other
# contextless modules run as exec("<module>") WITHOUT these, so the probe must
# not let them win the leader election.
_HOST_KEYS = ("_Information", "client", "_Navigation")

# Cache for the global CoordinatesManager resolved from Blackboard.storage.
# Keys live under "<namespace>/_CoordinatesManager"; the value is the SAME
# ActCommon.Instance.CoordinatesManager for every element client.
_BB_REF = None
_BB_CM = None

# EOT order names the game's MechTools.EOTOrder enum exposes (verified in the
# AI scripts + disasm of mnw.shared.dll). Console sends the name; the probe
# resolves it via the enum. Full telegraph set: AsternEmer=0, AsternFull=1,
# Astern23=2, Astern13=3, Stop=4, Ahead13=5, Ahead23=6, AheadStd=7, AheadFull=8,
# AheadFlank=9, SetKnots=10 (SetSpeed), SetTurns=11 (SetTurns), SetTurnsForKnot=12 (SetTPK).
_EOT_NAMES = ("Stop", "Ahead13", "Ahead23", "AheadStd", "AheadFull", "AheadFlank",
              "Astern13", "Astern23", "AsternFull", "AsternEmer")


def _global_cm():
    """CoordinatesManager from the shared pybt Blackboard.storage (module-level,
    no _Probe / lock required). Returns None if unavailable.

    Storage is a single mutable class-attribute dict, so a scan that finds
    nothing must NOT be cached (keys may be registered a moment later); only a
    successful hit is memoized."""
    global _BB_REF, _BB_CM
    try:
        from pybt.bb.blackboard import Blackboard
        bb = Blackboard.storage
    except Exception:
        return None
    if _BB_CM is not None and bb is _BB_REF:
        return _BB_CM
    _BB_REF = bb
    _BB_CM = None
    try:
        for k, v in bb.items():
            if isinstance(k, str) and k.endswith("/_CoordinatesManager"):
                _BB_CM = v
                break
    except Exception:
        pass
    return _BB_CM


_GATE_LAST_TS = 0.0
_GATE_MIN_INTERVAL = 5.0


def _record_gate(host, result):
    """Record the last gate decision for the throttled gate-diagnostic file."""
    global _GATE_LAST_TS
    now = time.time()
    if result is not True and now - _GATE_LAST_TS < _GATE_MIN_INTERVAL:
        return
    _GATE_LAST_TS = now
    info = None
    try:
        info = host.get("_Information") if host else None
    except Exception:
        pass
    d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
         "host": (host or {}).get("__name__", "?"),
         "result": result}
    if info is not None:
        try:
            d["my_id"] = _desc(info.GetID, 40)
        except Exception:
            pass
        try:
            d["my_element"] = _desc(info.Element, 140)
        except Exception as e:
            d["my_element"] = "ERR %s" % e
    cm = _global_cm()
    if cm is None:
        d["cm"] = "none"
    else:
        d["cm"] = "ok"
        try:
            d["cm_player"] = _desc(cm.Player, 140)
        except Exception as e:
            d["cm_player"] = "ERR %s" % e
        try:
            pp = cm.Player
            d["cm_player_id"] = _desc(getattr(pp, "GetID", None), 40)
        except Exception as e:
            d["cm_player_id"] = "ERR %s" % e
        try:
            pp = cm.Player
            attrs = {}
            for a in ("Controller", "Element", "ElementName", "CountryID", "Coordinates",
                      "Course", "Speed", "Elevation", "GetID"):
                attrs[a] = _desc(getattr(pp, a, "<missing>"), 60)
            d["cm_player_attrs"] = attrs
        except Exception as e:
            d["cm_player_attrs"] = "ERR %s" % e
        try:
            pp = cm.Player
            ctrl = pp.Controller
            if ctrl is None:
                d["cm_player_controller"] = "None"
            else:
                d["cm_player_controller"] = _desc(ctrl, 120)
                acc = _try(lambda: ctrl.Access)
                d["cm_player_access"] = "ok" if acc[0] == "ok" else acc[1][:80]
                types = {}
                if isinstance(host, dict):
                    for tn in ("Navigation", "SteeringDiving", "DepthGauge"):
                        types[tn] = host.get(tn)
                for cname, t in types.items():
                    if t is None:
                        d["access_" + cname] = "no type in host"
                        continue
                    r = _try(lambda t=t: ctrl.Access[t]())
                    d["access_" + cname] = "ok" if r[0] == "ok" else r[1][:80]
        except Exception as e:
            d["cm_player_controller"] = "ERR %s" % _desc(e, 80)
    try:
        m = _global_mission()
        if m is None:
            d["mission"] = "none"
        else:
            d["mission"] = "ok"
            try:
                d["mission_player"] = _desc(m.Player, 140)
            except Exception as e:
                d["mission_player"] = "ERR %s" % e
            try:
                mp = m.Player
                d["mission_player_id"] = _desc(getattr(mp, "GetID", None), 40)
            except Exception as e:
                d["mission_player_id"] = "ERR %s" % e
    except Exception as e:
        d["mission"] = "ERR %s" % e
    try:
        dirs = _resolve_log_dirs(_load_config())
        path = os.path.join(dirs[0], "ship_probe_gate.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        if result is True:
            marker = os.path.join(dirs[0], "ship_probe_gate_player.json")
            with io.open(marker, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
    except Exception:
        pass


def _global_mission():
    """ActiveMission from the shared pybt Blackboard.storage (module-level).
    Returns None if unavailable."""
    try:
        from pybt.bb.blackboard import Blackboard
        bb = Blackboard.storage
    except Exception:
        return None
    try:
        for k, v in bb.items():
            if isinstance(k, str) and k.endswith("/_ScenarioManager"):
                if v is not None and v.ActiveMission is not None:
                    return v.ActiveMission
    except Exception:
        pass
    return None


def _host_can_target_player(host):
    """True if the player element is resolvable from this host (cm.Player).

    The player element runs NO python script (it is player-controlled), so no
    host can ever BE the player. Instead the probe runs on any element script
    and targets the player via CoordinatesManager.Player (an mnw.Core.Information
    exposing .Controller, same API the host scripts use on their own _Information)."""
    if host is None:
        return None
    try:
        cm = _global_cm()
        if cm is None:
            _record_gate(host, None)
            return None
        pp = cm.Player
        if pp is None:
            _record_gate(host, None)
            return None
        pid = _try(lambda: int(pp.GetID))
        if pid[0] != "ok":
            _record_gate(host, None)
            return None
        ctrl = _try(lambda: pp.Controller)
        ok = ctrl[0] == "ok" and ctrl[1] is not None
        _record_gate(host, ok)
        return ok
    except Exception:
        _record_gate(host, None)
        return None


def _cfg_path():
    try:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), _CONFIG_FILE)
    except Exception:
        return _CONFIG_FILE


def _load_config():
    cfg = dict(_DEFAULTS)
    try:
        with io.open(_cfg_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # Merge allow_commands: file may be stale (e.g. from an older kyt extract).
    # Add any actions from _DEFAULTS that are missing in the loaded config.
    if isinstance(cfg.get("allow_commands"), list) and isinstance(_DEFAULTS.get("allow_commands"), list):
        merged = list(cfg["allow_commands"])
        for a in _DEFAULTS["allow_commands"]:
            if a not in merged:
                merged.append(a)
        cfg["allow_commands"] = merged
    return cfg


def _resolve_log_dirs(cfg):
    cands = []
    if cfg.get("log_dir"):
        cands.append(str(cfg["log_dir"]))
    try:
        cands.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    cands.append(os.getcwd())
    cands.append(os.path.expanduser("~"))
    cands.append("/tmp")
    out = []
    for c in cands:
        try:
            if not os.path.isdir(c):
                os.makedirs(c)
            if os.access(c, os.W_OK) and c not in out:
                out.append(c)
        except Exception:
            continue
    return out or cands[:1] or ["."]


def _debug_console(text):
    d = None
    try:
        d = globals().get("_HOST", {}).get("_Debug") or globals().get("_Debug")
    except Exception:
        pass
    if d is not None:
        try:
            d.Log(str(text))
            return
        except Exception:
            pass
    try:
        print(text)
    except Exception:
        pass


class _Log(object):
    def __init__(self, paths, console):
        self.console = console
        self.fs = []
        for p in paths:
            try:
                self.fs.append(io.open(p, "a", encoding="utf-8"))
            except Exception:
                pass

    def w(self, text):
        line = "%s %s" % (time.strftime("%H:%M:%S"), text)
        for f in self.fs:
            try:
                f.write(line + "\n")
                f.flush()
            except Exception:
                pass
        if self.console:
            _debug_console("ship_probe: " + line)

    def close(self):
        for f in self.fs:
            try:
                f.close()
            except Exception:
                pass
        self.fs = []


def _try(fn, *a, **k):
    try:
        return ("ok", fn(*a, **k))
    except Exception as e:
        return ("err", "%s: %s" % (type(e).__name__, str(e)))


def _desc(v, limit=160):
    try:
        s = repr(v)
    except Exception:
        return "<repr failed>"
    if len(s) > limit:
        return s[:limit] + "..."
    return s


TNC_CTL_ALLOWED = frozenset((
    "TrimFlood", "TrimDrain", "TrimTransfer", "TrimCirculation",
    "FloodTrim", "StopFloodTrim", "ToggleTrimPump", "SetTrimPumpRPM",
    "SetTrimValveStatus", "StartCirculation", "StopCirculation",
    "SetBubble", "SetTrim", "SetLevel", "SetTrimMode", "SetMode",
))


def _parse_ctl_arg(a):
    if isinstance(a, bool):
        return a
    if isinstance(a, (int, float)):
        return a
    s = str(a).strip()
    if s in ("True", "true"):
        return True
    if s in ("False", "false"):
        return False
    if s in ("None", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


class _EnumRefError(object):
    def __init__(self, msg):
        self.msg = msg

    def __repr__(self):
        return "<enum-ref-error: %s>" % self.msg


_CLR_TYPE_MODULES = ("mnw.Mechanics", "mnw.Core", "mnw.Systems", "mnw")


def _geo_latlon(coords):
    pairs = (("latitude", "longitude"), ("Latitude", "Longitude"), ("Lat", "Long"), ("_lat", "_longt"),
             ("lat", "long"), ("Lat", "Lon"), ("_Latitude", "_Longitude"))
    for a, b in pairs:
        try:
            return (float(getattr(coords, a)), float(getattr(coords, b)))
        except Exception:
            try:
                return (float(coords[a]), float(coords[b]))
            except Exception:
                continue
    return (None, None)


def _coord_to_ll(v):
    ll = _geo_latlon(v)
    if ll[0] is None:
        return None
    try:
        lat, lon = float(ll[0]), float(ll[1])
    except Exception:
        return None
    if abs(lat) <= 90.0 and abs(lon) <= 360.0:
        return (lat, lon)
    return None


def _range_bearing(lat1, lon1, lat2, lon2):
    """Great-circle distance (km) and initial bearing (deg) between two WGS84
    points. Pure Python (haversine) — NO engine calls, safe at all latitudes.
    Returns (None, None) on bad input."""
    try:
        lat1, lon1, lat2, lon2 = (float(x) for x in (lat1, lon1, lat2, lon2))
    except Exception:
        return (None, None)
    import math
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return (round(R * c, 2), round(brg, 1))


def _safe_num(v, digits=2):
    try:
        return round(float(v), digits)
    except Exception:
        return None


def _json_default(o):
    # pythonnet values (System.Double/Single/Decimal etc.) are not
    # json-serializable; normalize numeric-ish objects to float and everything
    # else to its string representation so state writes never fail.
    try:
        return float(o)
    except Exception:
        pass
    try:
        return str(o)
    except Exception:
        return "<unserializable>"


class _Probe(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.tick_count = 0
        self.state_count = 0
        self._dc_deferred = []
        self.log_dirs = _resolve_log_dirs(cfg)
        self.log_dir = self.log_dirs[0]
        self.log = _Log([os.path.join(d, _LOG_NAME) for d in self.log_dirs], bool(cfg.get("console_log")))
        self.errors = []
        self.last_cmdid = None
        self.console_results = []
        self.host = _HOST
        self.discovery = None
        self.player_state = None
        self._merc_conv = None
        self._merc_hit = None
        self._eot_enum = None
        self._eot_enum_err = None
        self._cm_cache = None
        self._player_cache = None

    # ---------------------------------------------------------------
    # resolution helpers
    # ---------------------------------------------------------------

    def g(self, name):
        try:
            if self.host is not None and name in self.host:
                return self.host[name]
        except Exception:
            pass
        v = globals().get(name)
        if v is not None:
            return v
        for modname in _CLR_FALLBACKS.get(name, ()):
            try:
                m = __import__(modname, fromlist=["*"])
                v = getattr(m, name, None)
                if v is not None:
                    return v
            except Exception:
                continue
        return None

    def host_get(self, key):
        if self.host is None:
            return None
        try:
            return self.host.get(key)
        except Exception:
            return None

    # ---------------------------------------------------------------
    # player targeting (the player element runs no script, so every
    # element script resolves the player via cm.Player and targets it)
    # ---------------------------------------------------------------

    def player_info(self):
        """Resolve the player's mnw.Core.Information via CoordinatesManager.Player.
        Same class as the host's _Information, so .Controller/.Element work."""
        if self._player_cache is not None:
            return self._player_cache
        cm = self.coordinates_manager()
        if cm is None:
            return None
        try:
            pinfo = cm.Player
        except Exception:
            pinfo = None
        if pinfo is None:
            return None
        self._player_cache = pinfo
        return pinfo

    def player_controller(self):
        pinfo = self.player_info()
        if pinfo is None:
            return None
        try:
            return pinfo.Controller
        except Exception:
            return None

    def player_navigation(self):
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        t = self.g("Navigation")
        if t is None:
            return None
        try:
            return ctrl.Access[t]()
        except Exception:
            return None

    def player_steering(self):
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        t = self.g("SteeringDiving")
        if t is None:
            return None
        try:
            return ctrl.Access[t]()
        except Exception:
            return None

    def player_mechtools(self):
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        mt = self.g("MechTools")
        if mt is not None:
            try:
                return mt
            except Exception:
                pass
        return None

    def active_mission(self):
        nav = self.host_get("client")
        if nav is not None:
            try:
                sm = nav._ScenarioManager
                if sm is not None:
                    return sm.ActiveMission
            except Exception:
                pass
        bb = self._blackboard_storage()
        if bb is not None:
            try:
                for k, v in bb.items():
                    if isinstance(k, str) and k.endswith("/_ScenarioManager"):
                        if v is not None and v.ActiveMission is not None:
                            return v.ActiveMission
            except Exception:
                pass
        try:
            sm = self.g("IPrepCommon").Instance.ScenarioManager
            return sm.ActiveMission
        except Exception:
            return None

    def _blackboard_storage(self):
        try:
            from pybt.bb.blackboard import Blackboard
            return Blackboard.storage
        except Exception:
            return None

    def _bb_scan(self, suffix):
        bb = self._blackboard_storage()
        if not bb:
            return None
        try:
            items = list(bb.items())
        except Exception:
            return None
        for k, v in items:
            if isinstance(k, str) and k.endswith(suffix):
                return v
        return None

    def coordinates_manager(self):
        if self._cm_cache is not None:
            return self._cm_cache
        nav = self.host_get("client")
        if nav is not None:
            try:
                cm = nav._CoordinatesManager
                if cm is not None:
                    self._cm_cache = cm
                    return cm
            except Exception:
                pass
        cm = self._bb_scan("/_CoordinatesManager")
        if cm is not None:
            self._cm_cache = cm
            return cm
        for name in ("IActCommon", "ActCommon", "IPrepCommon"):
            try:
                cm = self.g(name).Instance.CoordinatesManager
                if cm is not None:
                    self._cm_cache = cm
                    return cm
            except Exception:
                continue
        return None

    def clock_manager(self):
        cm = self.coordinates_manager()
        try:
            ck = cm.ClockManager
            if ck is not None:
                return ck
        except Exception:
            pass
        nav = self.host_get("client")
        if nav is not None:
            try:
                ck = nav._ClockManager
                if ck is not None:
                    return ck
            except Exception:
                pass
        ck = self._bb_scan("/_ClockManager")
        if ck is not None:
            return ck
        try:
            ck = self.g("ActCommon").Instance.ClockManager
            return ck
        except Exception:
            return None

    # ---------------------------------------------------------------
    # logging
    # ---------------------------------------------------------------

    def emit(self, text):
        self.log.w(text)

    def note_error(self, where, e):
        msg = "%s: %s: %s" % (where, type(e).__name__, str(e))
        self.errors.append(msg)
        if len(self.errors) > 50:
            self.errors = self.errors[-50:]
        self.emit("ERROR(%s): %s" % (where, msg))

    def _atomic_write(self, fname, obj):
        path = os.path.join(self.log_dir, fname)
        tmp = path + ".tmp"
        try:
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2, default=_json_default)
            if os.path.isfile(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception as e:
            self.note_error("write_" + fname, e)
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    # ---------------------------------------------------------------
    # own-element data (READ side)
    # ---------------------------------------------------------------

    def read_identity(self, info):
        out = {}
        for name, fn in (
            ("id", lambda: int(info.GetID)),
            ("name", lambda: str(info.ElementName)),
            ("country", lambda: int(info.CountryID)),
            ("category", lambda: str(info.Category)),
            ("assignment", lambda: int(info.Element.AssignmentID)),
            ("course", lambda: float(info.Course)),
            ("speed", lambda: float(info.Speed)),
            ("elevation", lambda: float(info.Elevation)),
            ("dimensions", lambda: list(info.Dimensions)),
        ):
            r = _try(fn)
            if r[0] == "ok":
                out[name] = r[1]
            else:
                out[name + "_err"] = r[1][:60]
        crd = _try(lambda: info.Coordinates)
        if crd[0] == "ok":
            ll = _coord_to_ll(crd[1])
            if ll is not None:
                out["lat_lon"] = list(ll)
        return out

    def _component(self, tname, owner="self"):
        t = self.g(tname)
        if t is None:
            return ("no_type", None)
        if owner == "player":
            ctrl = self.player_controller()
            if ctrl is None:
                return ("no_controller", None)
            r = _try(lambda t=t: ctrl.Access[t]())
            if r[0] == "ok":
                return ("ok", r[1])
            return ("err", r[1])
        ctrl = self.host_get("_Controller")
        if ctrl is None:
            return ("no_controller", None)
        r = _try(lambda t=t: ctrl.Access[t]())
        if r[0] == "ok":
            return ("ok", r[1])
        return ("err", r[1])

    def _component_any(self, tname, prefer="player"):
        """Resolve a component trying host then player controller (or reverse).
        The probe piggybacks an element script; masts/towed may live on either.
        Access[T]() returns None (not an error) when a ship lacks the component,
        so None counts as a miss and we move to the next controller."""
        order = ("host", "player") if prefer == "host" else ("player", "host")
        for owner in order:
            st, comp = self._component(tname, owner=owner)
            if st == "ok" and comp is not None:
                return (owner, st, comp)
        return (order[-1], "err", None)

    def _resolve_tnc(self):
        """Resolve the TnCManager (trim tanks) for the current player ship."""
        st, hs = self._component("Hydrostatics", owner="player")
        if st != "ok" or hs is None:
            st, hs = self._component("Hydrostatics")
        if st == "ok" and hs is not None:
            for fname in ("TnC", "get_TnC"):
                r = _try(lambda fname=fname: getattr(hs, fname))
                if r[0] == "ok" and r[1] is not None:
                    return r[1]
        st, tnc = self._component_any("TnCManager")
        if st == "ok" and tnc is not None:
            return tnc
        return None

    def _clr_type(self, name):
        """Resolve a CLR type object (e.g. an enum) by name. Order:
        1. host globals, 2. pythonnet clr.GetClrType (fq + short),
        3. the mnw.* modules, 4. a live TnC instance (GetTrimValveStatus's
        return type), 5. probe module globals."""
        t = self.g(name)
        if t is not None:
            return t
        clr = self.host_get("clr")
        if clr is not None:
            for fq in (name, "mnw.Mechanics." + name, "mnw." + name,
                       "Mechanics." + name):
                try:
                    t = clr.GetClrType(fq)
                except Exception:
                    t = None
                if t is not None:
                    return t
        for modname in _CLR_TYPE_MODULES:
            try:
                m = __import__(modname, fromlist=["*"])
                t = getattr(m, name, None)
                if t is not None:
                    return t
            except Exception:
                continue
        if name.lower() in ("trimvalvestatus", "valvestatus", "trimvalve"):
            try:
                tnc = self._resolve_tnc()
                if tnc is not None:
                    r = _try(lambda: tnc.GetTrimValveStatus(0))
                    if r[0] == "ok" and r[1] is not None and not isinstance(r[1], str):
                        return type(r[1])
            except Exception:
                pass
        return None

    def _parse_ctl_arg(self, a):
        """tctl argument parsing with enum refs: '@Type.Member' resolves a CLR
        enum member via _clr_type; a bare '@...' is a reported error."""
        s = str(a).strip()
        if s.startswith("@"):
            body = s[1:]
            if "." not in body:
                return _EnumRefError("@ needs Type.Member form")
            tname, mname = body.split(".", 1)
            t = self._clr_type(tname)
            if t is None:
                return _EnumRefError("type %r not resolvable" % tname)
            try:
                return getattr(t, mname)
            except Exception as e:
                return _EnumRefError("member %r.%r: %s" % (tname, mname, _desc(e, 60)))
        return _parse_ctl_arg(a)

    def read_navigation(self):
        nav = self.host_get("client")
        out = {}
        nav_obj = None
        pnav = self.player_navigation()
        if pnav is not None:
            nav_obj = pnav
        if nav_obj is None and nav is not None:
            r = _try(lambda: nav._Navigation)
            if r[0] == "ok":
                nav_obj = r[1]
        if nav_obj is None:
            st, nav_obj = self._component("Navigation", owner="player")
            if st != "ok":
                st, nav_obj = self._component("Navigation")
                if st != "ok":
                    return {"err": "no Navigation component"}
        self.emit("cp: nav geocoords")
        r = _try(lambda: nav_obj.INS.GeoCoordinates)
        if r[0] == "ok":
            ll = _coord_to_ll(r[1])
            if ll is not None:
                out["lat_lon"] = list(ll)
            else:
                self.emit("cp: nav merc convert")
                merc = _try(lambda: nav_obj.INS.GeoCoordinates)
                if merc[0] == "ok":
                    ll2 = self._merc_to_ll(merc[1])
                    if ll2 is not None:
                        out["lat_lon"] = list(ll2)
                        out["lat_lon_source"] = "mercator"
        for name, fn in (
            ("heading", lambda: float(nav_obj.INS.Heading)),
            ("speed", lambda: float(nav_obj.INS.ForwardSpeed)),
            ("true_heading", lambda: float(nav_obj.INS.TrueHeading)),
            ("true_speed", lambda: float(nav_obj.INS.TrueForwardSpeed)),
        ):
            r = _try(fn)
            if r[0] == "ok":
                out[name] = _safe_num(r[1])
            else:
                out[name + "_err"] = r[1][:60]
        vel = _try(lambda: nav_obj.INS.Velocity)
        if vel[0] == "ok":
            try:
                out["velocity"] = [_safe_num(vel[1].x), _safe_num(vel[1].y), _safe_num(vel[1].z)]
            except Exception:
                pass
        dg = _try(lambda: nav_obj.DepthGauge.Elevation)
        if dg[0] == "ok":
            out["depth"] = _safe_num(dg[1])
        else:
            st, dgc = self._component("DepthGauge")
            if st == "ok":
                r2 = _try(lambda: dgc.Elevation)
                if r2[0] == "ok":
                    out["depth"] = _safe_num(r2[1])
        ag = _try(lambda: nav_obj.AltitudedGauge.Elevation)
        if ag[0] == "ok":
            out["altitude"] = _safe_num(ag[1])
        br = _try(lambda: nav_obj.BottomRanging.Range)
        if br[0] == "ok":
            out["bottom_range"] = _safe_num(br[1])
        # current plot
        plot = _try(lambda: nav_obj.Plot.AvailableWaypointsIndex())
        if plot[0] == "ok" and plot[1] is not None:
            try:
                idx = list(plot[1])
                out["plot_count"] = len(idx)
                wp = []
                wp_class = self.g("Waypoint")
                if wp_class is not None and nav is not None:
                    getr = _try(lambda: nav_obj.Plot.GetWaypoint[wp_class])
                    if getr[0] == "ok" and callable(getr[1]):
                        for i in idx[:12]:
                            r2 = _try(lambda i=i: nav_obj.Plot.GetWaypoint[wp_class](i)._Coordinates)
                            if r2[0] == "ok":
                                ll2 = _coord_to_ll(r2[1])
                                if ll2 is not None:
                                    wp.append(list(ll2))
                                else:
                                    wp.append(_desc(r2[1], 60))
                out["plot_waypoints"] = wp
            except Exception:
                pass
        # ordered values from blackboard (host script)
        if nav is not None:
            for k in ("_CurrentCourse", "_OrderedCourse", "_CurrentEOTOrder", "_OrderedEOTOrder",
                      "_CurrentDepth", "_OrderedDepth", "_CurrentRPM"):
                r = _try(lambda k=k: getattr(nav, k))
                if r[0] == "ok":
                    v = r[1]
                    try:
                        v = int(v) if isinstance(v, (int, float)) else str(v)
                    except Exception:
                        pass
                    out[k.lower()] = v
        return out

    def read_blackboard(self):
        nav = self.host_get("client")
        out = {}
        if nav is None:
            return {"err": "no host blackboard (client)"}
        keys = ("_WaypointIterator", "_WaypointIndexArray", "_CurrentAssignmentID",
                "_ReportTickCount", "_ActionPrepComplete", "_CurrentTensionLevel",
                "_EnemySuspiciousContacts", "_MergedContacts", "_LastWaypointReached",
                "_TransitReached", "_OwnshipLength")
        for k in keys:
            r = _try(lambda k=k: getattr(nav, k))
            if r[0] == "ok":
                v = r[1]
                if k == "_MergedContacts":
                    try:
                        v = "list(%d)" % len(v)
                    except Exception:
                        v = _desc(v, 80)
                elif k == "_EnemySuspiciousContacts":
                    try:
                        v = "list(%d)" % len(v)
                    except Exception:
                        v = _desc(v, 80)
                out[k] = v
            else:
                out[k + "_err"] = r[1][:60]
        return out

    def read_systems(self):
        out = {}
        # Integrity (disasm-verified mnw.Mechanics.Integrity, all public
        # property reads; tank components carry IIntegrity.Status enum:
        # Operational=1, Malfunctioning=2, Damaged=4 — see damage command)
        st, comp = self._component("Integrity")
        if st == "ok" and comp is not None:
            for name, fn in (
                ("integrity_damage_ratio", lambda: comp.DamageLevelRatio),
                ("integrity_operational_ratio", lambda: comp.OperationalLevelRatio),
                ("integrity_hull_ratio", lambda: comp.HullLevelRatio),
                ("integrity_hull_stress", lambda: comp.HullStressRatio),
                ("integrity_tanks_ratio", lambda: comp.TanksLevelRatio),
                ("integrity_sunk_ratio", lambda: comp.SunkLevelRatio),
                ("integrity_plate_strength", lambda: comp.PlateStrength),
            ):
                r = _try(fn)
                if r[0] == "ok":
                    out[name] = _safe_num(r[1], 4)
            for name, fn in (
                ("integrity_on_fire", lambda: bool(comp.OnFire)),
                ("integrity_flooding", lambda: bool(comp.Flooding)),
                ("integrity_sunk", lambda: bool(comp.IsSunk)),
            ):
                r = _try(fn)
                if r[0] == "ok":
                    out[name] = r[1]
            r = _try(lambda: list(comp.IntegrityTanks))
            if r[0] == "ok" and r[1]:
                tanks = r[1][:50]
                out["integrity_tanks"] = len(tanks)
                for i, tank in enumerate(tanks):
                    pref = "tank_%d" % i
                    for name, fn in (
                        ("bulkhead", lambda tank=tank: bool(tank.IsBulkheadDoorOpen)),
                        ("fire", lambda tank=tank: bool(tank.IsOnFire)),
                        ("flooding", lambda tank=tank: bool(tank.IsFlooding)),
                    ):
                        r2 = _try(fn)
                        if r2[0] == "ok":
                            out["%s_%s" % (pref, name)] = r2[1]
                    r2 = _try(lambda tank=tank: tank.LevelRatio)
                    if r2[0] == "ok":
                        out["%s_level" % pref] = _safe_num(r2[1], 4)
                    r2 = _try(lambda tank=tank: list(tank.Components))
                    if r2[0] != "ok" or not r2[1]:
                        continue
                    counts = {"ok": 0, "malf": 0, "dmg": 0, "other": 0}
                    damaged = []
                    for c in r2[1][:32]:
                        r3 = _try(lambda c=c: int(c.Status))
                        if r3[0] != "ok":
                            counts["other"] += 1
                            continue
                        s = r3[1]
                        if s == 1:
                            counts["ok"] += 1
                        elif s == 2:
                            counts["malf"] += 1
                        elif s == 4:
                            counts["dmg"] += 1
                            r4 = _try(lambda c=c: str(c.ComponentDescription))
                            if r4[0] == "ok" and r4[1]:
                                damaged.append(r4[1][:40])
                        else:
                            counts["other"] += 1
                    out["%s_comps_ok" % pref] = counts["ok"]
                    out["%s_comps_malf" % pref] = counts["malf"]
                    out["%s_comps_dmg" % pref] = counts["dmg"]
                    if counts["other"]:
                        out["%s_comps_other" % pref] = counts["other"]
                    if damaged:
                        out["%s_damaged" % pref] = damaged[:8]
        # Ammunition
        st, comp = self._component("AmmunitionStorage")
        if st == "ok":
            for name, fn in (
                ("ammo_offensive_ratio", lambda: comp.OffensiveCombatPowerRatio),
                ("ammo_defensive_ratio", lambda: comp.DefensiveCombatPowerRatio),
            ):
                r = _try(fn)
                if r[0] == "ok":
                    out[name] = _safe_num(r[1], 4)
        # Maneuvering
        st, comp = self._component("Maneuvering")
        if st == "ok":
            r = _try(lambda: comp.CMP.RPM)
            if r[0] == "ok":
                out["rpm"] = _safe_num(r[1], 1)
        # Towed array (may live on host or player controller)
        where, st, comp = self._component_any("TowedController")
        if st == "ok":
            r = _try(lambda: comp.GetReelStatus(0))
            if r[0] == "ok":
                out["towed_array"] = str(r[1])
        # Masts (may live on host or player controller). MastsController
        # exposes methods (GetMastHeight/GetMastStatus/GetMastType/
        # GetAvailableMastIDs), the snorkel carries Raised/IsExposed/
        # HeadValveMode and SteeringDiving carries the periscope depths.
        where, st, comp = self._component_any("MastsController")
        if st == "ok":
            r = _try(lambda: int(comp.Status))
            if r[0] == "ok":
                out["mast_controller_status"] = r[1]
            ids = _try(lambda: list(comp.GetAvailableMastIDs()))
            if ids[0] == "ok" and ids[1]:
                out["mast_ids"] = ids[1]
                for mast_id in ids[1][:6]:
                    pref = "mast_%s" % mast_id
                    for name, fn in (
                        ("type", lambda id=mast_id: str(comp.GetMastType(id))),
                        ("status", lambda id=mast_id: str(comp.GetMastStatus(id))),
                        ("height", lambda id=mast_id: float(comp.GetMastHeight(id))),
                    ):
                        r2 = _try(fn)
                        if r2[0] == "ok":
                            out["%s_%s" % (pref, name)] = r2[1]
            else:
                # GetAvailableMastIDs came back empty or failed - probe the
                # first few IDs directly so we still report per-mast values.
                out["mast_ids"] = []
                if ids[0] != "ok":
                    out["mast_ids_err"] = ids[1][:80]
                probed = []
                for mast_id in range(0, 6):
                    t = _try(lambda id=mast_id: str(comp.GetMastType(id)))
                    if t[0] != "ok":
                        break
                    probed.append(mast_id)
                    pref = "mast_%s" % mast_id
                    for name, fn in (
                        ("type", lambda id=mast_id: str(comp.GetMastType(id))),
                        ("status", lambda id=mast_id: str(comp.GetMastStatus(id))),
                        ("height", lambda id=mast_id: float(comp.GetMastHeight(id))),
                    ):
                        r2 = _try(fn)
                        if r2[0] == "ok":
                            out["%s_%s" % (pref, name)] = r2[1]
                if probed:
                    out["mast_ids"] = probed
                    out["mast_ids_source"] = "probe"
        # Snorkel (mast positions)
        where, st, comp = self._component_any("Snorkel")
        if st == "ok":
            for name, fn in (
                ("snorkel_raised", lambda: bool(comp.Raised)),
                ("snorkel_exposed", lambda: bool(comp.IsExposed)),
                ("snorkel_head_valve", lambda: int(comp.HeadValveMode)),
                ("snorkel_intake_hole", lambda: float(comp.IntakeHole)),
                ("snorkel_intake_volume", lambda: float(comp.IntakeVolume)),
            ):
                r = _try(fn)
                if r[0] == "ok":
                    out[name] = r[1]
        # SteeringDiving: periscope / depth bands
        where, st, sd = self._component_any("SteeringDiving")
        if st == "ok":
            for name, fn in (
                ("periscope_depth", lambda: float(sd.PeriscopeDepth)),
                ("surface_depth", lambda: float(sd.SurfaceDepth)),
                ("standard_depth", lambda: float(sd.StandardDepth)),
                ("max_operational_depth", lambda: float(sd.MaxOperationalDepth)),
                ("ordered_heading", lambda: float(sd.OrderedHeading)),
                ("ordered_speed", lambda: float(sd.OrderedSpeed)),
                ("ordered_depth", lambda: float(sd.OrderedDepth)),
            ):
                r = _try(fn)
                if r[0] == "ok":
                    out[name] = r[1]
        # Coxswain (bulkheads / lights / CIWs)
        st, comp = self._component("Coxswain")
        if st == "ok":
            r = _try(lambda: comp.Bulkheads)
            if r[0] == "ok":
                out["bulkheads"] = _desc(r[1], 80)
            r = _try(lambda: comp.Lights)
            if r[0] == "ok" and r[1] is not None:
                lights = r[1]
                out["lights_enabled"] = _try(lambda: bool(lights.IsSystemEnabled))[1] if _try(lambda: bool(lights.IsSystemEnabled))[0] == "ok" else None
                nav = _try(lambda: str(lights.NAVSTATCodes))
                out["lights_navstat"] = nav[1] if nav[0] == "ok" else None
            r = _try(lambda: comp.CIWs)
            if r[0] == "ok":
                out["ciws"] = _desc(r[1], 80)
        # FireControl / ContactManager
        st, comp = self._component("FireControl")
        if st == "ok":
            cm = _try(lambda: comp.ContactManager)
            if cm[0] == "ok":
                out["contact_manager"] = _desc(cm[1], 80)
        return out

    def read_steering(self, with_getters=False):
        """SteeringDiving control state (EOT, ordered values, locks, depth bands,
        cavitation) + Hydrodynamics/Maneuvering component data. ALL reads are
        property getters / field access + indexing only - no Unity-object method
        calls (freeze rule) - so this is safe to run every tick.

        with_getters=True additionally reads stern/rudder angles via the
        Hydrodynamics GetSternPlane(i)/GetRudder(i) methods (verified live,
        no freeze) - CONTROL-command use only (do_planes read-out), never tick.

        Live member map (verified 2026-08-16 via sd-dump):
          SteeringDiving public props: OrderedEOT/Speed/Heading/Depth, DefaultEOT,
            BowPlanesRetracted, ForwardPlanesLocked, IntSternPlanesLocked,
            SurfaceDepth, PeriscopeDepth, StandardDepth, MaxOperationalDepth,
            Cavitation, Scope, Navigation. Private fields (_SteeringMode,
            _MaxPlaneRateOfTurn, _AutoTrim, _DepthEnvelopes) are NOT bindable in
            the embedded interpreter (AttributeError) - dropped here.
          Access[Hydrodynamics]: ForwardPlanes (Array[HydroSurface], public),
            ForwardPlanesType. SternPlanes/Rudders are NOT public - only via
            GetSternPlane(i)/GetRudder(i).
          Access[Maneuvering]: TPK, STW (public), SL/NL (sound levels)."""
        out = {}
        sd = None
        try:
            sd = self._steering()
        except Exception as e:
            out["err"] = _desc(e, 100)
            return out
        if sd is None:
            out["err"] = "no SteeringDiving"
            return out
        for name, fn in (
            ("ordered_eot", lambda: int(sd.OrderedEOT)),
            ("ordered_speed", lambda: float(sd.OrderedSpeed)),
            ("ordered_heading", lambda: float(sd.OrderedHeading)),
            ("ordered_depth", lambda: float(sd.OrderedDepth)),
            ("bow_planes_retracted", lambda: bool(sd.BowPlanesRetracted)),
            ("forward_planes_locked", lambda: bool(sd.ForwardPlanesLocked)),
            ("int_stern_planes_locked", lambda: bool(sd.IntSternPlanesLocked)),
            ("surface_depth", lambda: float(sd.SurfaceDepth)),
            ("standard_depth", lambda: float(sd.StandardDepth)),
            ("max_operational_depth", lambda: float(sd.MaxOperationalDepth)),
            ("periscope_depth", lambda: float(sd.PeriscopeDepth)),
            ("default_eot", lambda: int(sd.DefaultEOT)),
            ("cavitation", lambda: str(sd.Cavitation)),
            ("scope", lambda: str(sd.Scope)),
        ):
            r = _try(fn)
            if r[0] == "ok":
                out[name] = r[1]
            else:
                out[name + "_err"] = r[1][:60]
        # Hydrodynamics: forward plane angles via public array (indexing only)
        hd = self._hydro()
        if hd is not None:
            arr = _try(lambda: hd.ForwardPlanes)
            if arr[0] == "ok" and arr[1] is not None:
                try:
                    n = len(arr[1])
                except Exception:
                    n = 0
                items = []
                for i in range(min(n, 8)):
                    r2 = _try(lambda i=i: arr[1][i].FlapAngle)
                    items.append(_safe_num(r2[1]) if r2[0] == "ok" else None)
                out["forward_plane_angles"] = items
            ft = _try(lambda: str(hd.ForwardPlanesType))
            if ft[0] == "ok":
                out["forward_planes_type"] = ft[1]
            if with_getters:
                for label, getter, count in (("stern_plane_angles", "GetSternPlane", 4),
                                             ("rudder_plane_angles", "GetRudder", 2)):
                    items = []
                    for i in range(count):
                        r2 = _try(lambda getter=getter, i=i: getattr(hd, getter)(i).FlapAngle)
                        items.append(_safe_num(r2[1]) if r2[0] == "ok" else None)
                    out[label] = items
        else:
            out["hydro_err"] = "no Hydrodynamics"
        # Maneuvering: TPK / STW (public props, no method calls)
        mv = self._maneuvering()
        if mv is not None:
            for name, fn in (
                ("tpk", lambda: float(mv.TPK)),
                ("stw", lambda: float(mv.STW)),
            ):
                r2 = _try(fn)
                if r2[0] == "ok":
                    out[name] = r2[1]
        return out

    def _hydro(self):
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        t = self.g("Hydrodynamics")
        if t is None:
            return None
        r = _try(lambda t=t: ctrl.Access[t]())
        return r[1] if r[0] == "ok" else None

    def _maneuvering(self):
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        t = self.g("Maneuvering")
        if t is None:
            return None
        r = _try(lambda t=t: ctrl.Access[t]())
        return r[1] if r[0] == "ok" else None

    def read_contacts(self):
        out = {"count": 0, "tracks": []}
        fcm = None
        # Target the PLAYER element first (cm.Player.Controller) — the probe
        # piggybacks on any element script but the player is the interesting one.
        st, comp = self._component("FireControl", owner="player")
        if st == "ok":
            r = _try(lambda: comp.ContactManager)
            if r[0] == "ok":
                fcm = r[1]
        # Fallback: host's own FireControl / blackboard ContactManager.
        if fcm is None:
            ctrl = self.host_get("_Controller")
            if ctrl is not None:
                st, comp = self._component("FireControl")
                if st == "ok":
                    r = _try(lambda: comp.ContactManager)
                    if r[0] == "ok":
                        fcm = r[1]
        if fcm is None:
            nav = self.host_get("client")
            if nav is not None:
                r = _try(lambda: nav._ContactManager)
                if r[0] == "ok":
                    fcm = r[1]
        if fcm is None:
            out["err"] = "no ContactManager"
            return out
        self.emit("cp: contacts manager ok")
        r = _try(lambda: fcm.Count)
        if r[0] != "ok":
            out["err"] = r[1][:60]
            return out
        out["count"] = int(r[1])
        self.emit("cp: contacts count=%d" % out["count"])
        used = _try(lambda: fcm.GetUsed)
        if used[0] != "ok":
            out["err"] = used[1][:60]
            return out
        self.emit("cp: contacts used ok")
        maxc = int(self.cfg.get("max_contacts", 50))
        for i, cid in enumerate(used[1]):
            if i >= maxc:
                out["truncated"] = True
                break
            t = {"id": _desc(cid, 40)}
            cat = _try(lambda cid=cid: fcm.GetCategoryID(cid))
            if cat[0] == "ok":
                t["category"] = _desc(cat[1], 40)
            pref = _try(lambda cid=cid: fcm.GetPrefix(cid))
            if pref[0] == "ok":
                t["prefix"] = _desc(pref[1], 40)
            ident = _try(lambda cid=cid: fcm.GetStandardIdentity(cid))
            if ident[0] == "ok":
                t["identity"] = _desc(ident[1], 40)
            tr = _try(lambda cid=cid: fcm.GetTrack(cid))
            self.emit("cp: contacts track %d ok" % i)
            if tr[0] == "ok":
                tk = tr[1]
                for name, attr in (
                    ("speed", "_Speed"), ("range", "_Range"), ("elevation", "_Elevation"),
                    ("course", "_Course"), ("rcpa", "_RCPA"), ("tcpa", "_TCPA"),
                    ("bearing_rate", "_BearingRate"), ("relative_bearing", "_RelativeBearing"),
                    ("bearing", "_Bearing"),
                ):
                    r2 = _try(lambda attr=attr: getattr(tk, attr))
                    if r2[0] == "ok":
                        v = r2[1]
                        if attr in ("_RelativeBearing", "_Bearing") and v is not None:
                            try:
                                v = [_safe_num(v.Item1), _safe_num(v.Item2)]
                            except Exception:
                                v = _safe_num(v)
                        else:
                            v = _safe_num(v) if isinstance(v, (int, float)) else v
                        t[name] = v
            out["tracks"].append(t)
        self.emit("cp: contacts loop done")
        return out

    def _player_sonar(self, tname):
        """Resolve a sonar component on the PLAYER element.

        The ONLY safe source is the blackboard-storage scan: an element
        registers its own namespaced sonar key ('/N/_ActiveSonar') when it has
        that component. Controller.Access[Type]() on an element WITHOUT the
        component HANGS the game instead of raising (freeze verified
        2026-08-13: player sub has only HFS, no ActiveSonar) — so it is NEVER
        called here. Returns None if the player has no such component. NOTE
        (2026-08-15): the Virginia player element runs NO script, so its
        sonar arrays are NOT on the blackboard — use `_player_sonar_system` +
        `read_sonar_arrays` for the player's own arrays instead."""
        key = "_ActiveSonar" if tname == "ActiveSonar" else "_PassiveSonar"
        bb = self._blackboard_storage()
        if not bb:
            return None
        try:
            items = list(bb.items())
        except Exception:
            return None
        pid = None
        det = self.detect_player()
        try:
            pid = int(det.get("player_id") or 0)
        except Exception:
            pid = None
        # (1) the player's own namespaced key, e.g. '/6/_ActiveSonar'
        for k, v in items:
            if not isinstance(k, str) or not k.endswith("/" + key):
                continue
            if pid is not None and k.startswith("/%d/" % pid):
                return v
        # (2) any other element's namespaced key as a fallback
        for k, v in items:
            if isinstance(k, str) and k.endswith("/" + key):
                return v
        return None

    def read_sonar(self):
        """Read sonar tracker data via SonarSystem.GetContactIDs/GetTrackerData.

        SAFE PATH: uses the SonarSystem tracker API (same as sonctl commands)
        instead of StrongestContact which enters the crash-prone
        ContactManager/MergedContacts pipeline. Returns tracker contacts with
        bearing, range, sensorID, trackID — per-contact, not just strongest."""
        out = {}
        ss = self._player_sonar_system()
        if ss is None:
            self.emit("cp: sonar tracker: no SonarSystem")
            return out
        self.emit("cp: sonar tracker: SonarSystem ok")
        r = _try(lambda: ss.GetContactIDs())
        if r[0] != "ok":
            out["err"] = "GetContactIDs: %s" % _desc(r[1], 60)
            self.emit("cp: sonar tracker: GetContactIDs err")
            return out
        ids = r[1]
        if ids is None or len(ids) == 0:
            out["count"] = 0
            self.emit("cp: sonar tracker: 0 contacts")
            return out
        try:
            count = len(ids)
        except Exception:
            count = 0
        out["count"] = count
        self.emit("cp: sonar tracker: %d contacts" % count)
        maxc = int(self.cfg.get("max_sonar_contacts", 20))
        tracks = []
        for i, item in enumerate(ids[:maxc]):
            try:
                cid = item.Item1 if hasattr(item, "Item1") else item
            except Exception:
                cid = item
            td = _try(lambda cid=cid: ss.GetTrackerData(int(cid)))
            if td[0] != "ok":
                continue
            data = td[1]
            if data is None:
                continue
            t = {"id": str(cid)}
            try:
                items = list(data)
            except Exception:
                items = [data]
            sensors = []
            for item in items:
                s = {}
                for name, attrs in [("bearing", ("_Bearing", "Bearing")), ("range", ("_Range", "Range")), ("sensor", ("_SensorID", "SensorID")), ("track", ("_TrackID", "TrackID"))]:
                    for attr in attrs:
                        rv = _try(lambda a=attr: getattr(item, a))
                        if rv[0] == "ok" and rv[1] is not None:
                            v = rv[1]
                            s[name] = _safe_num(v) if isinstance(v, (int, float)) else str(v)[:40]
                            break
                if s:
                    sensors.append(s)
            if sensors:
                t["sensors"] = sensors
                best = next((s for s in sensors if s.get("range") is not None and s["range"] == s["range"]), sensors[0])
                t["bearing"] = best.get("bearing")
                t["range"] = best.get("range")
                t["sensor"] = best.get("sensor")
            tracks.append(t)
        out["tracks"] = tracks
        self.emit("cp: sonar tracker: %d tracks read" % len(tracks))
        return out

    def _player_sonar_system(self):
        """Resolve the PLAYER's mnw.Systems.SonarSystem via ctrl.Access[SonarSystem]().

        Controller.Access[Type]() was suspected to hang on elements that LACK
        the component (freeze 2026-08-13), but disassembly (rva 0x3f918) shows
        Access is a pure _Components-Dictionary lookup (no Unity calls), the
        gate probe proved access_* ok on the PLAYER controller, and the live
        run confirmed Access[SonarSystem]() = ok. The player Virginia is
        sonar-equipped, so SonarSystem is guaranteed present -> Access[] is
        safe. The old _Components field scan is dead (that field is fdPrivate,
        invisible to pythonnet getattr — same for the nested .Controller and
        .Hub, which have no such fields exposed at all). Returns the
        SonarSystem instance or None."""
        ctrl = self.player_controller()
        if ctrl is None:
            return None
        t = self.g("SonarSystem")
        if t is None:
            self._sonar_diag_once("cp: sonar system: SonarSystem type not resolvable")
            return None
        self._sonar_diag_once("cp: sonar system: Access[SonarSystem] ...")
        r = _try(lambda t=t: ctrl.Access[t]())
        self._sonar_diag_once("cp: sonar system: Access[SonarSystem] done")
        if r[0] != "ok":
            self._sonar_diag_once("cp: sonar system: Access err %s" % _desc(r[1], 80))
            return None
        return r[1]

    def _sonar_diag_once(self, line):
        """Emit a sonar diagnostic line only once per probe run (the state loop
        calls _player_sonar_system every tick; checkpoints would spam the log)."""
        if getattr(self, "_sonar_diag_emitted", False):
            return
        self._sonar_diag_emitted = True
        self.emit(line)

    def read_sonar_arrays(self):
        """Enumerate the player's SonarSystem.Sonars (List<ISensor>) — the actual
        sonar arrays (hull + towed broadband passive etc.). Per array: identity
        fields, contact count and per-contact signal/noise breakdown. ONLY field
        reads and public property access — no Controller.Access, no Track access.
        Frozen sonar systems (no Sonars populated yet) yield an empty list."""
        out = {"arrays": [], "err": None}
        sys = self._player_sonar_system()
        if sys is None:
            out["err"] = "no player SonarSystem"
            self.emit("cp: sonar arrays: no player SonarSystem")
            return out
        self.emit("cp: sonar arrays: player SonarSystem ok")
        maxa = int(self.cfg.get("max_sonar_arrays", 8))
        maxc = int(self.cfg.get("max_sonar_contacts", 20))
        r = _try(lambda: sys.Sonars)
        if r[0] != "ok":
            out["err"] = "no Sonars property (%s)" % (r[1] or "")[:40]
            return out
        if r[1] is None:
            # sensors not cached yet (CacheSensors runs lazily) -> empty arrays
            self.emit("cp: sonar arrays: Sonars not cached yet")
            return out
        arr = r[1]
        try:
            items = list(arr)
        except Exception:
            out["err"] = "_Sonars not enumerable"
            return out
        for i, sensor in enumerate(items):
            if i >= maxa:
                out["truncated"] = True
                break
            self.emit("cp: sonar array %d" % i)
            a = {"index": i}
            tn = _try(lambda sensor=sensor: type(sensor).__name__)
            if tn[0] == "ok":
                a["type"] = tn[1]
            for name, attr in (
                ("design_frequency", "DesignFrequency"), ("frequency_range", "FrequencyRange"),
                ("beam_type", "BeamType"), ("beam_pattern", "BeamPattern"),
                ("aov", "AoV"), ("toggle", "Toggle"), ("status", "Status"),
                ("length", "Length"), ("course", "Course"), ("sensor_heading", "SensorHeading"),
            ):
                rr = _try(lambda attr=attr: getattr(sensor, attr))
                if rr[0] == "ok":
                    v = rr[1]
                    if name in ("design_frequency", "aov", "length"):
                        a[name] = _safe_num(v)
                    elif isinstance(v, (int, float)):
                        a[name] = _safe_num(v)
                    elif name in ("beam_type", "beam_pattern", "status", "type"):
                        a[name] = _desc(v, 40)
                    else:
                        a[name] = _desc(v, 40)
            contacts = _try(lambda: sensor.Contacts)
            if contacts[0] != "ok":
                a["contacts_err"] = contacts[1][:60]
            else:
                clist = contacts[1]
                try:
                    citems = list(clist)
                except Exception:
                    a["contacts_err"] = "not enumerable"
                    citems = []
                a["contact_count"] = len(citems)
                self.emit("cp: sonar array %d contacts=%d" % (i, len(citems)))
                cl = []
                for j, c in enumerate(citems):
                    if j >= maxc:
                        a["contacts_truncated"] = True
                        break
                    ct = {}
                    for name, attr in (
                        ("bearing", "Bearing"), ("range", "Range"), ("elevation", "Elevation"),
                        ("course", "Course"), ("speed", "Speed"), ("signal", "Signal"),
                        ("noise", "Noise"), ("self_noise", "SelfNoise"),
                        ("flow_noise", "FlowNoise"), ("ambient_noise", "AmbientNoise"),
                        ("thermal_noise", "ThermalNoise"), ("doppler", "DopplerCoef"),
                        ("category", "Category"), ("database_id", "DatabaseID"),
                        ("beam_type", "BeamType"), ("id", "ID"),
                    ):
                        rc = _try(lambda attr=attr: getattr(c, attr))
                        if rc[0] == "ok":
                            v = rc[1]
                            if isinstance(v, (int, float)):
                                ct[name] = _safe_num(v)
                            else:
                                ct[name] = _desc(v, 30)
                    relb = _try(lambda: c.RelativeBearing)
                    if relb[0] == "ok" and relb[1] is not None:
                        try:
                            ct["relative_bearing"] = [_safe_num(relb[1].Item1), _safe_num(relb[1].Item2)]
                        except Exception:
                            ct["relative_bearing"] = _safe_num(relb[1])
                    cnan = _try(lambda: c.IsNaN)
                    if cnan[0] == "ok":
                        ct["nan"] = bool(cnan[1])
                    cl.append(ct)
                a["contacts"] = cl
            out["arrays"].append(a)
        self.emit("cp: sonar arrays loop done")
        return out

    # ---------------------------------------------------------------
    # AI-element enumeration (reads ALL element namespaces from the
    # shared Blackboard.storage — each AI script registers a /N/ ns)
    # ---------------------------------------------------------------

    def _ai_namespaces(self):
        """Distinct element namespaces present in Blackboard.storage.
        Keys look like '/13/_Navigation'; the ns is the element id."""
        bb = self._blackboard_storage()
        if not bb:
            return []
        try:
            items = list(bb.items())
        except Exception:
            return []
        ns = set()
        for k, v in items:
            if isinstance(k, str):
                parts = k.split("/")
                if len(parts) >= 3 and parts[1].isdigit():
                    ns.add(parts[1])
        return sorted(ns)

    def read_ai_elements(self):
        """Enumerate all AI element namespaces in Blackboard.storage and
        capture identity/position/assignment/contact-count per element.
        Writes ai_state.json. Player namespace is skipped (player data is
        already in ship_state.json). Every field is _try-guarded; contact
        access is limited to .Count (track access = freeze risk)."""
        out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "count": 0, "elements": []}
        bb = self._blackboard_storage()
        if not bb:
            out["err"] = "no blackboard storage"
            return out
        try:
            items = list(bb.items())
        except Exception:
            out["err"] = "storage unreadable"
            return out
        # group keys by namespace
        namespaces = {}
        for k, v in items:
            if not isinstance(k, str):
                continue
            parts = k.split("/")
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            namespaces.setdefault(parts[1], {})[parts[2]] = v
        pid = None
        det = self.detect_player()
        try:
            pid = int(det.get("player_id") or 0)
        except Exception:
            pid = None
        # player reference position (native GeoCord fields — no Mercator call)
        player_ll = None
        pnav = self.player_navigation()
        if pnav is not None:
            r = _try(lambda: pnav.INS.GeoCoordinates)
            if r[0] == "ok":
                player_ll = _coord_to_ll(r[1])
        max_el = int(self.cfg.get("max_ai_elements", 30))
        elements = []
        for ns in sorted(namespaces, key=lambda x: int(x)):
            if len(elements) >= max_el:
                out["truncated"] = True
                break
            nid = int(ns)
            if pid is not None and nid == pid:
                continue
            kv = namespaces[ns]
            el = {"id": nid, "is_player": False}
            # helo/plane marker: dipping-sonar keys are helo-specific
            if any(k in kv for k in ("_DippingSonarController", "_DippingSonarOps", "_DippingEngaged")):
                el["is_helo"] = True
            elif any(k in kv for k in ("_AttackOps", "_TrackedCategoryID")):
                el["host_style"] = "ship"
            if "_SelfInfo" in kv:
                el["host_style"] = "general"
            # identity (if the script registered _Information)
            info = kv.get("_Information")
            if info is None:
                self.emit("cp: ai elem %d no _Information - trying _SelfInfo" % nid)
                info = kv.get("_SelfInfo")
                if info is not None:
                    el["identity_src"] = "_SelfInfo"
            if info is not None:
                for name, fn in (
                    ("name", lambda: str(info.ElementName)),
                    ("country", lambda: int(info.CountryID)),
                    ("category", lambda: str(info.Category)),
                ):
                    r = _try(fn)
                    if r[0] == "ok":
                        el[name] = r[1]
            # position / heading / speed from _Navigation
            nav = kv.get("_Navigation")
            if nav is not None:
                self.emit("cp: ai elem %d pos" % nid)
                r = _try(lambda: nav.INS.GeoCoordinates)
                if r[0] == "ok":
                    ll = _coord_to_ll(r[1])
                    if ll is not None:
                        el["lat_lon"] = list(ll)
                    else:
                        self.emit("cp: ai elem %d merc" % nid)
                        ll2 = self._merc_to_ll(r[1])
                        if ll2 is not None:
                            el["lat_lon"] = list(ll2)
                            el["lat_lon_source"] = "mercator"
                for name, attr in (
                    ("heading", "Heading"), ("speed", "ForwardSpeed"),
                    ("true_heading", "TrueHeading"), ("true_speed", "TrueForwardSpeed"),
                    ("depth", "DepthGauge.Elevation"),
                ):
                    if attr == "DepthGauge.Elevation":
                        r = _try(lambda: nav.DepthGauge.Elevation)
                    else:
                        r = _try(lambda attr=attr: getattr(nav.INS, attr))
                    if r[0] == "ok":
                        el[name] = _safe_num(r[1])
            # AI -> player geometry (pure Python, no engine call)
            if player_ll is not None and el.get("lat_lon"):
                rng, brg = _range_bearing(player_ll[0], player_ll[1],
                                         el["lat_lon"][0], el["lat_lon"][1])
                if rng is not None:
                    el["to_player_range_km"] = rng
                    el["to_player_bearing"] = brg
            # assignment
            asg = kv.get("_CurrentAssignmentID")
            if asg is not None:
                r = _try(lambda: int(asg))
                if r[0] == "ok":
                    el["assignment_id"] = r[1]
            else:
                r = _try(lambda: nav._CurrentAssignmentID) if nav is not None else ("err", "no nav")
                if r[0] == "ok":
                    el["assignment_id"] = int(r[1])
            for name, key in (("ordered_course", "_OrderedCourse"),
                              ("ordered_eot", "_OrderedEOTOrder"),
                              ("current_course", "_CurrentCourse"),
                              ("current_eot", "_CurrentEOTOrder"),
                              ("ordered_depth", "_OrderedDepth"),
                              ("current_depth", "_CurrentDepth")):
                v = kv.get(key)
                if v is None:
                    continue
                r = _try(lambda v=v: int(v) if isinstance(v, (int, float)) else str(v))
                if r[0] == "ok":
                    el[name] = r[1]
            # contact count only (track access = freeze risk)
            cmgr = kv.get("_ContactManager")
            if cmgr is not None:
                r = _try(lambda: cmgr.Count)
                if r[0] == "ok":
                    el["contact_count"] = int(r[1])
            # action-prep flag (plain bool field, safe to read)
            prep = kv.get("_ActionPrepComplete")
            if prep is not None:
                r = _try(lambda: bool(prep))
                if r[0] == "ok":
                    el["action_prep_complete"] = r[1]
            # incoming order surface (dir() only — no getattr on members)
            iord = kv.get("_IncomingOrder")
            if iord is not None:
                io_surf = {
                    "present": True,
                    "type": _desc(type(iord), 80),
                    "members": sorted(m for m in dir(iord) if not m.startswith("_"))[:40],
                }
                r = _try(lambda: int(iord.AssignmentID))
                if r[0] == "ok":
                    io_surf["assignment_id"] = r[1]
                else:
                    io_surf["assignment_id_err"] = r[1][:60]
                el["incoming_order"] = io_surf
            # current assignment surface (dir() only — .Where getters are NOT
            # touched; Coordinates.Item2 lives under the assignment)
            casg = kv.get("_CurrentAssignment")
            if casg is not None:
                ca_surf = {
                    "present": True,
                    "type": _desc(type(casg), 80),
                    "members": sorted(m for m in dir(casg) if not m.startswith("_"))[:40],
                }
                for attr, out_key in (("Who", "who_id"), ("Whom", "whom_id")):
                    r = _try(lambda attr=attr: getattr(casg, attr))
                    if r[0] == "ok" and r[1] is not None:
                        rid = _try(lambda r1=r[1]: int(r1.GetID))
                        if rid[0] == "ok":
                            ca_surf[out_key] = rid[1]
                        else:
                            ca_surf[out_key + "_err"] = rid[1][:60]
                r = _try(lambda: casg.ID)
                if r[0] == "ok":
                    ca_surf["id"] = _try(lambda r1=r[1]: int(r1))[1] if r[1] is not None else r[1]
                el["current_assignment"] = ca_surf
            # AiDataLink presence (dir() only — no getattr on members)
            alink = kv.get("_AiDataLink")
            if alink is not None:
                members = sorted(m for m in dir(alink) if not m.startswith("_"))
                el["ai_data_link"] = {"present": True, "members": members[:40]}
            # AttackOps surface (dir() only — invoking = shipAttack pattern)
            aops = kv.get("_AttackOps")
            if aops is not None:
                members = sorted(m for m in dir(aops) if not m.startswith("_"))
                el["attack_ops"] = {"present": True, "members": members[:40]}
            elements.append(el)
            self.emit("cp: ai elem %d ok" % nid)
        out["count"] = len(elements)
        out["elements"] = elements
        self._atomic_write(_AI_STATE_NAME, out)
        self.emit("ai: %d elements -> %s" % (len(elements), _AI_STATE_NAME))
        return out

    def read_mission(self):
        out = {}
        m = self.active_mission()
        if m is None:
            out["active"] = False
            return out
        out["active"] = True
        for name, fn in (
            ("name", lambda: str(m.Name)),
            ("operation", lambda: str(m.OperationType)),
            ("datetime", lambda: str(m.DateTime)),
        ):
            r = _try(fn)
            if r[0] == "ok":
                out[name] = r[1]
        # diplomacy / tension via host blackboard if present, else mission
        nav = self.host_get("client")
        if nav is not None:
            r = _try(lambda: nav._CurrentTensionLevel)
            if r[0] == "ok":
                out["tension"] = str(r[1])
        if "tension" not in out:
            d = _try(lambda: m.Diplomacy.GetTensionLevel)
            if d[0] == "ok":
                out["tension"] = str(d[1])
        info = self.host_get("_Information")
        if info is not None:
            my_id = _try(lambda: int(info.GetID))
            my_op = _try(lambda: int(info.CountryID))
            if my_id[0] == "ok" and my_op[0] == "ok":
                d = _try(lambda: m.Diplomacy)
                if d[0] == "ok":
                    fx = _try(lambda: d[0].GetFactions(my_op[1], 0))
                    # GetFactions needs CoalitionStatus; try friendly/enemy
                    out["diplomacy_err"] = "see probe"
        return out

    # ---------------------------------------------------------------
    # position conversion
    # ---------------------------------------------------------------

    def _merc_calls(self):
        if self._merc_conv is not None:
            return self._merc_conv
        calls = []
        cm = self.coordinates_manager()
        if cm is not None:
            m = _try(lambda: cm.MercatorToWGS84)
            if m[0] == "ok" and callable(m[1]):
                calls.append(("CoordinatesManager.MercatorToWGS84", m[1]))
        gc = self.g("GeoCord")
        if gc is not None:
            m = _try(lambda: gc.MercatorToWGS84)
            if m[0] == "ok" and callable(m[1]):
                calls.append(("GeoCord.MercatorToWGS84", m[1]))
        self._merc_conv = calls
        return calls

    def _merc_to_ll(self, merc):
        if merc is None or not self.cfg.get("resolve_positions", True):
            return None
        calls = self._merc_calls() or []
        if not calls:
            return None
        if self._merc_hit is not None:
            label, fn = calls[self._merc_hit]
            self.emit("cp: merc %s" % label)
            r = _try(lambda fn=fn, merc=merc: fn(merc))
            if r[0] == "ok":
                ll = _coord_to_ll(r[1])
                if ll is not None:
                    return ll
            self._merc_hit = None
            return None
        for idx, (label, fn) in enumerate(calls):
            self.emit("cp: merc %s" % label)
            r = _try(lambda fn=fn, merc=merc: fn(merc))
            if r[0] == "ok":
                ll = _coord_to_ll(r[1])
                if ll is not None:
                    self._merc_hit = idx
                    self.emit("pos: merc->ll via %s" % label)
                    return ll
        return None

    # ---------------------------------------------------------------
    # player detection
    # ---------------------------------------------------------------

    def detect_player(self):
        """Resolve the player element (the probe now TARGETS the player via
        cm.Player instead of requiring the host to BE the player)."""
        tgt = int(self.cfg.get("target_element_id") or 0)
        cm = self.coordinates_manager()
        if cm is not None:
            try:
                player_el = cm.Player
                if player_el is not None:
                    pid = _try(lambda: int(player_el.GetID))
                    if pid[0] == "ok":
                        info = self.host_get("_Information")
                        my_id = None
                        if info is not None:
                            my_id_r = _try(lambda: int(info.GetID))
                            if my_id_r[0] == "ok":
                                my_id = my_id_r[1]
                        if tgt > 0:
                            return {"is_player": pid[1] == tgt,
                                    "source": "target_element_id",
                                    "player_id": pid[1], "id": my_id}
                        return {"is_player": True,
                                "source": "CoordinatesManager.Player.GetID",
                                "player_id": pid[1], "id": my_id}
            except Exception:
                pass
        # Numeric diagnostics only (Player/PlayerGCID are not ints).
        if cm is not None:
            for attr in ("Player", "PlayerGCID"):
                r = _try(lambda attr=attr: getattr(cm, attr))
                if r[0] != "ok" or r[1] is None:
                    continue
                pid = _try(lambda: int(r[1]))
                if pid[0] != "ok":
                    continue
                return {"is_player": False, "source": "CoordinatesManager." + attr,
                        "player_id": pid[1], "id": None,
                        "reason": "numeric fallback only (diagnostic)"}
        info = self.host_get("_Information")
        my_id = None
        if info is not None:
            my_id_r = _try(lambda: int(info.GetID))
            if my_id_r[0] == "ok":
                my_id = my_id_r[1]
        return {"is_player": False, "reason": "player detection failed (no cm.Player)",
                "id": my_id}

    # ---------------------------------------------------------------
    # discovery (API capability map)
    # ---------------------------------------------------------------

    def discovery_run(self):
        self.emit("cp: discovery begin")
        out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "host_script": self.host_label(),
               "components": {}, "blackboard_keys": [], "errors": []}
        components = (
            ("Navigation", "Navigation"), ("SteeringDiving", "SteeringDiving"),
            ("Integrity", "Integrity"), ("AmmunitionStorage", "AmmunitionStorage"),
            ("Maneuvering", "Maneuvering"), ("Coxswain", "Coxswain"),
            ("FireControl", "FireControl"), ("TowedController", "TowedController"),
            ("MastsController", "MastsController"), ("Snorkel", "Snorkel"),
            ("DepthGauge", "DepthGauge"),
            ("OpticalSystem", "OpticalSystem"), ("ActiveSonar", "ActiveSonar"),
            ("PassiveSonar", "PassiveSonar"), ("fbw", "UnityScriptComponent(fbw)"),
        )
        for key, tname in components:
            if tname == "UnityScriptComponent(fbw)":
                ctrl = self.host_get("_Controller")
                if ctrl is None:
                    out["components"][key] = {"status": "no_controller"}
                    continue
                r = _try(lambda ctrl=ctrl: ctrl.Access["UnityScriptComponent"]("fbw"))
            else:
                st, comp = self._component(tname)
                r = ("ok", comp) if st == "ok" else ("err", st if st == "no_type" else comp)
            if r[0] != "ok":
                out["components"][key] = {"status": r[0], "detail": _desc(r[1], 120)}
                continue
            attrs = {}
            for a in dir(r[1]):
                if a.startswith("_"):
                    continue
                v = _try(lambda a=a: getattr(r[1], a))
                if v[0] == "ok" and not callable(v[1]):
                    try:
                        attrs[a] = _desc(v[1], 100)
                    except Exception:
                        pass
            out["components"][key] = {"status": "ok", "type": _desc(r[1], 80), "attrs": attrs}
        nav = self.host_get("client")
        if nav is not None:
            keys = []
            for k in dir(nav):
                if k.startswith("_"):
                    keys.append(k)
            out["blackboard_keys"] = keys
        out["blackboard_sonar"] = {}
        if nav is not None:
            for label, key in (("active", "_ActiveSonar"), ("passive", "_PassiveSonar")):
                son = _try(lambda key=key: getattr(nav, key))
                if son[0] != "ok" or son[1] is None:
                    out["blackboard_sonar"][label] = {"present": False}
                    continue
                # SAFE surface map: dir() names only, NO getattr on members
                # (e.g. StrongestContact is a property whose getter enters the
                # crash/freeze-prone ContactManager pipeline).
                members = []
                for m in dir(son[1]):
                    if not m.startswith("_"):
                        members.append(m)
                out["blackboard_sonar"][label] = {
                    "present": True,
                    "type": _desc(type(son[1]), 80),
                    "members": sorted(members),
                }
        # scan the whole blackboard storage for namespaced sonar keys —
        # the player element registers its own namespace ('/6/_ActiveSonar').
        out["blackboard_sonar_scan"] = {"keys": []}
        bb = self._blackboard_storage()
        if bb:
            try:
                items = list(bb.items())
            except Exception:
                items = []
            for k, v in items:
                if isinstance(k, str) and ("_ActiveSonar" in k or "_PassiveSonar" in k or "_DepthGauge" in k):
                    entry = {"key": k}
                    try:
                        entry["type"] = _desc(type(v), 80)
                    except Exception:
                        pass
                    out["blackboard_sonar_scan"]["keys"].append(entry)
            out["blackboard_sonar_scan"]["count"] = len(out["blackboard_sonar_scan"]["keys"])
        # nav_geo: surface the Navigation.INS.GeoCoordinates struct's member
        # NAMES via dir() only — NO getattr on members and NO method calls
        # (CoordinatesManager.MercatorToWGS84 freezes the engine; a property
        # getter could too). Used to pick a safe lat/lon resolver.
        out["nav_geo"] = {"present": False}
        nav = self.host_get("client")
        if nav is not None:
            r = _try(lambda: nav._Navigation)
            if r[0] == "ok" and r[1] is not None:
                r2 = _try(lambda: r[1].INS.GeoCoordinates)
                if r2[0] == "ok" and r2[1] is not None:
                    try:
                        members = sorted(m for m in dir(r2[1]) if not m.startswith("_"))
                    except Exception as e:
                        members = ["ERR " + _desc(e, 120)]
                    out["nav_geo"] = {
                        "present": True,
                        "type": _desc(type(r2[1]), 80),
                        "members": members,
                    }
        # player controller surface: dir() NAMES only, NO getattr on members
        # (a property getter could enter the crash-prone ContactManager
        # pipeline). Used to find the component-dictionary field that holds
        # SonarSystem (the A1 note assumed '_Components', but getattr on the
        # live player controller raised AttributeError).
        out["player_controller"] = {"present": False}
        ctrl = self.player_controller()
        if ctrl is not None:
            try:
                names = sorted(dir(ctrl))
            except Exception as e:
                names = ["ERR " + _desc(e, 120)]
            out["player_controller"] = {
                "present": True,
                "type": _desc(type(ctrl), 80),
                "member_count": len(names),
                "underscore_members": [n for n in names if n.startswith("_")],
                "public_members": [n for n in names if not n.startswith("_")],
                "has_components_field": "_Components" in names,
            }
            # depth probe of component-dictionary candidates. getattr only on
            # getter properties (Access, AccessComponents, Interfaces, Hub,
            # Controller, Scope, ScopeSelector) — those are pure field readers
            # like Access[] on the host's own controller. GetKeys/Register are
            # plain method refs (we do NOT call them, just describe).
            cand = ("Access", "AccessComponents", "Interfaces", "Hub",
                    "Controller", "Scope", "ScopeSelector", "GetKeys", "Register")
            probe = {}
            for name in cand:
                if name not in names:
                    probe[name] = "no member"
                    continue
                r = _try(lambda name=name: getattr(ctrl, name))
                if r[0] != "ok":
                    probe[name] = "ERR %s" % _desc(r[1], 80)
                    continue
                v = r[1]
                if v is None:
                    probe[name] = "None"
                    continue
                if callable(v):
                    probe[name] = "callable %s" % _desc(type(v), 60)
                    continue
                probe[name] = _desc(v, 100)
            out["player_controller_probe"] = probe
            # nested lookups: the player Controller has a nested .Controller
            # (also mnw.Core.Bus.Controller) and a .Hub — the component
            # dictionary may live on either. dir()-NAMES ONLY — NO getattr on
            # the nested objects (getattr on _Registry/_Buses/_ComponentDict of
            # the inner Controller/Hub hung the Unity main thread on 2026-08-16,
            # twice). Checkpointed so the next start pinpoints any regression.
            self.emit("cp: discovery player nested begin")
            nested = {}
            for label, fn in (("inner_controller", lambda: getattr(ctrl, "Controller")),
                              ("hub", lambda: getattr(ctrl, "Hub"))):
                r = _try(fn)
                if r[0] != "ok":
                    nested[label] = {"getattr": "ERR %s" % _desc(r[1], 80)}
                    continue
                o = r[1]
                entry = {"type": _desc(type(o), 80)}
                if o is None:
                    entry["value"] = "None"
                    nested[label] = entry
                    continue
                entry["value"] = _desc(o, 100)
                try:
                    names = sorted(dir(o))
                except Exception as e:
                    names = ["ERR " + _desc(e, 120)]
                entry["member_count"] = len(names)
                entry["underscore_members"] = [n for n in names if n.startswith("_")]
                entry["public_members"] = [n for n in names if not n.startswith("_")]
                nested[label] = entry
            out["player_controller_nested"] = nested
            self.emit("cp: discovery player nested done")
            # sonar system: resolve via ctrl.Access[SonarSystem]() — verified
            # freeze-safe on the PLAYER controller (gate probe + live run:
            # access_* ok for Navigation/SteeringDiving/DepthGauge/SonarSystem).
            # Dump the Sonars public property presence + dir() names, no calls.
            self.emit("cp: discovery sonar access begin")
            ss = self._player_sonar_system()
            self.emit("cp: discovery sonar access done")
            sonar_sys = {"present": False}
            if ss is not None:
                try:
                    names = sorted(dir(ss))
                except Exception as e:
                    names = ["ERR " + _desc(e, 120)]
                sonar_sys = {
                    "present": True,
                    "type": _desc(type(ss), 80),
                    "underscore_members": [n for n in names if n.startswith("_")],
                    "public_members": [n for n in names if not n.startswith("_")],
                }
            out["sonar_system"] = sonar_sys
        # FireControl TrackerManager discovery — dir() only, no method calls.
        out["tracker_managers"] = {}
        try:
            fc_st, fc_comp = self._component("FireControl", owner="player")
        except Exception:
            fc_st, fc_comp = "err", None
        if fc_st == "ok" and fc_comp is not None:
            tm_names = ("Visual", "Radar", "ESM", "Radio",
                        "Weapon", "AIS", "ActiveIntercept", "ManualSonar")
            for tm_key in tm_names:
                attr = tm_key + "TrackerManager"
                r = _try(lambda attr=attr: getattr(fc_comp, attr))
                if r[0] != "ok" or r[1] is None:
                    out["tracker_managers"][tm_key] = {"present": False}
                    continue
                tm = r[1]
                try:
                    members = sorted(m for m in dir(tm) if not m.startswith("_"))
                except Exception as e:
                    members = ["ERR " + _desc(e, 120)]
                out["tracker_managers"][tm_key] = {
                    "present": True,
                    "type": _desc(type(tm), 80),
                    "members": members,
                }
                # try read-only surface: Tracks/Count/Length if available
                for prop in ("Tracks", "Count", "Length", "GetCount"):
                    pr = _try(lambda prop=prop: getattr(tm, prop))
                    if pr[0] == "ok":
                        val = pr[1]
                        if callable(val):
                            out["tracker_managers"][tm_key][prop] = "callable"
                        else:
                            try:
                                out["tracker_managers"][tm_key][prop] = _desc(val, 100)
                            except Exception:
                                pass
        self.discovery = out
        self._atomic_write(_PROBE_NAME, out)
        self.emit("discovery: %d components, %d blackboard keys" % (
            sum(1 for c in out["components"].values() if c.get("status") == "ok"),
            len(out["blackboard_keys"])))
        return out

    # ---------------------------------------------------------------
    # live API probe (READ-ONLY surface map; no actions are executed)
    # ---------------------------------------------------------------

    def _api_dump_members(self, obj, max_members=600):
        members = {}
        try:
            names = sorted(n for n in dir(obj) if not n.startswith("_"))
        except Exception as e:
            return {"error": _desc(e, 120)}
        for name in names[:max_members]:
            try:
                v = getattr(obj, name)
            except Exception as e:
                members[name] = {"kind": "getter_error", "err": _desc(e, 100)}
                continue
            if callable(v):
                members[name] = {"kind": "callable"}
                continue
            try:
                t = _desc(type(v), 60)
            except Exception:
                t = "?"
            members[name] = {"kind": "value", "type": t, "repr": _desc(v, 120)}
        return members

    def api_probe_run(self):
        """Live-test the runtime API surface (read-only) and write
        ship_probe_api.json. Resolves the singletons the director could not
        reach (IActCommon/ActCommon/PrepCommon via host globals + blackboard)."""
        out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "host": self.host_label(), "targets": {}, "errors": []}
        targets = {}
        def add_target(label, obj):
            targets[label] = {"present": obj is not None}
            if obj is not None:
                targets[label]["members"] = self._api_dump_members(obj)
        # 1) class singletons reachable via host globals (from pybt import *)
        for name in ("IActCommon", "ActCommon", "IPrepCommon", "PrepCommon"):
            r = _try(lambda name=name: self.g(name).Instance)
            if r[0] == "ok":
                add_target("%s.Instance" % name, r[1])
            else:
                targets["%s.Instance" % name] = {"present": False, "err": _desc(r[1], 120)}
        # 2) CoordinatesManager (blackboard scan is the reliable path)
        add_target("CoordinatesManager", self.coordinates_manager())
        # 3) ScenarioManager via client blackboard
        sm = None
        nav = self.host_get("client")
        if nav is not None:
            r = _try(lambda: nav._ScenarioManager)
            if r[0] == "ok":
                sm = r[1]
        add_target("ScenarioManager", sm)
        # 4) the element client itself
        add_target("client", nav)
        out["targets"] = targets
        out["errors"] = self.errors[-20:]
        self._atomic_write(_API_PROBE_NAME, out)
        self.emit("api probe: %d targets dumped" % len(targets))
        return out

    # ---------------------------------------------------------------
    # state collection
    # ---------------------------------------------------------------

    def collect_state(self):
        perf = {}
        t0 = time.time()
        pinfo = self.player_info()
        det = self.detect_player()
        if self.cfg.get("require_player") and not det.get("is_player"):
            self.emit("skip state: not player (%s) host=%s" % (det.get("reason", "?"), self.host_label()))
            self.player_state = None
            return None
        st = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "player": det}
        if pinfo is not None:
            st["identity"] = self.read_identity(pinfo)
        elif self.host is not None:
            info = self.host_get("_Information")
            if info is not None:
                st["identity"] = self.read_identity(info)
        self.emit("cp: identity done")
        if self.cfg.get("measure_perf"):
            perf["identity"] = round(time.time() - t0, 6)
        st["navigation"] = self.read_navigation()
        self.emit("cp: navigation done")
        if self.cfg.get("measure_perf"):
            perf["navigation"] = round(time.time() - t0, 6)
        st["blackboard"] = self.read_blackboard()
        self.emit("cp: blackboard done")
        if self.cfg.get("measure_perf"):
            perf["blackboard"] = round(time.time() - t0, 6)
        st["systems"] = self.read_systems()
        self.emit("cp: systems done")
        if self.cfg.get("measure_perf"):
            perf["systems"] = round(time.time() - t0, 6)
        if self.cfg.get("read_steering", True):
            st["steering"] = self.read_steering()
            self.emit("cp: steering done")
        else:
            st["steering"] = {"disabled": True}
            self.emit("cp: steering done (disabled)")
        if self.cfg.get("read_contacts", True):
            st["contacts"] = self.read_contacts()
        else:
            st["contacts"] = {"count": 0, "disabled": True}
        self.emit("cp: contacts done")
        if self.cfg.get("measure_perf"):
            perf["contacts"] = round(time.time() - t0, 6)
        if self.cfg.get("read_sonar", True):
            st["sonar"] = self.read_sonar()
            self.emit("cp: sonar done")
        else:
            st["sonar"] = {"disabled": True}
            self.emit("cp: sonar done (disabled)")
        if self.cfg.get("read_sonar_arrays", False):
            st["sonar_arrays"] = self.read_sonar_arrays()
            self.emit("cp: sonar arrays done")
        if self.cfg.get("measure_perf"):
            perf["sonar"] = round(time.time() - t0, 6)
        if self.cfg.get("read_ai", True):
            self.read_ai_elements()
            self.emit("cp: ai done")
        else:
            self.emit("cp: ai done (disabled)")
        if self.cfg.get("measure_perf"):
            perf["ai"] = round(time.time() - t0, 6)
        st["mission"] = self.read_mission()
        self.emit("cp: mission done")
        st["clock"] = {}
        try:
            cmgr = self.clock_manager()
            if cmgr is None:
                st["clock"]["err"] = "no clock manager"
            else:
                t = _try(lambda: str(cmgr.Time))
                if t[0] == "ok":
                    st["clock"]["time"] = t[1]
                ts = _try(lambda: float(cmgr.TimeScale))
                if ts[0] == "ok":
                    st["clock"]["scale"] = ts[1]
        except Exception as e:
            st["clock"]["err"] = _desc(e, 100)
        self.player_state = st
        if self.cfg.get("measure_perf"):
            st["perf"] = perf
            st["perf"]["total"] = round(time.time() - t0, 6)
        if self._atomic_write(_STATE_NAME, st):
            self.state_count += 1
        return st

    # ---------------------------------------------------------------
    # command dispatch (CONTROL side)
    # ---------------------------------------------------------------

    _ACTIONS = ("helm", "planes", "plot", "clear-plot", "report", "probe", "ai-attack", "detected", "wc-dump", "steer", "ns-dump", "asg", "ai-contacts", "sd-dump", "tanks", "env", "alarm", "sonctl", "tracker", "masts", "explore", "tracker-new", "dc")

    # Commands whose native access is ELEMENT-scoped (target a specific
    # element id in the CALLING host's interpreter namespace). Command-only
    # probes (one per element script that resolves the player) may run these;
    # all other actions are executed ONLY by the lock-holding full probe.
    _ELEMENT_ACTIONS = ("ai-attack", "ns-dump", "asg", "ai-contacts")

    @staticmethod
    def _cmdid_of(c):
        try:
            return int(c.get("cmdid"))
        except Exception:
            return None

    def dispatch_orders(self):
        path = os.path.join(self.log_dir, _ORDERS_NAME)
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except IOError:
            return
        except Exception as e:
            self.note_error("orders_read", e)
            return
        cmds = data.get("commands") or []
        if not isinstance(cmds, list):
            cmds = []
        command_only = bool(getattr(self, "command_only", False))
        max_cmds = max(1, int(self.cfg.get("max_commands_per_cycle", 10)))
        results = []
        processed = 0
        processed_ids = set()
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            cmdid = self._cmdid_of(cmd)
            if cmdid is None:
                continue
            if self.last_cmdid is not None and cmdid <= self.last_cmdid:
                continue
            if processed >= max_cmds:
                break
            if command_only and str(cmd.get("action") or "") not in self._ELEMENT_ACTIONS:
                continue
            processed += 1
            self.last_cmdid = cmdid
            processed_ids.add(cmdid)
            if self.cfg.get("measure_perf"):
                _cmd_t0 = time.time()
            res = self.do_command(cmd)
            if res is not None:
                if self.cfg.get("measure_perf"):
                    res = dict(res, perf_ms=round((time.time() - _cmd_t0) * 1000.0, 3))
                results.append(res)
                self.console_results.append(res)
        if results:
            try:
                prev_path = os.path.join(self.log_dir, _RESULTS_NAME)
                with io.open(prev_path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                old = prev.get("results") if isinstance(prev, dict) else []
                if not isinstance(old, list):
                    old = []
            except Exception:
                old = []
            self._atomic_write(_RESULTS_NAME, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "results": old + results})
        if not command_only and processed_ids:
            # Only the lock-holding full probe owns the queue. Command-only
            # instances (one per element script, separate interpreter each)
            # must NOT re-run heavy commands like tanks/env across every tick
            # and every (re-)initialization - repeated native C# calls crashed
            # the game (mono native crash 2026-08-16). Drop just the cmdids
            # this instance ran; re-read so concurrent appends survive.
            try:
                with io.open(path, "r", encoding="utf-8") as f:
                    cur = json.load(f)
                cur_cmds = cur.get("commands") if isinstance(cur, dict) else None
                if isinstance(cur_cmds, list):
                    kept = [c for c in cur_cmds
                            if not (isinstance(c, dict) and self._cmdid_of(c) in processed_ids)]
                    self._atomic_write(_ORDERS_NAME, {"commands": kept})
            except Exception as e:
                self.note_error("orders_clear", e)

    def do_command(self, cmd):
        action = str(cmd.get("action") or "")
        base = {"cmdid": cmd.get("cmdid"), "action": action, "ts": time.strftime("%H:%M:%S")}
        fn = getattr(self, "do_" + action.replace("-", "_"), None)
        if action not in self._ACTIONS or fn is None:
            return dict(base, ok=False, result="unknown action (actions: %s)" % ", ".join(self._ACTIONS))
        allowed = set(str(a) for a in (self.cfg.get("allow_commands") or []))
        if allowed and action not in allowed:
            return dict(base, ok=False, result="denied (not in allow_commands: %s)" % sorted(allowed))
        try:
            lines = fn(cmd)
            if lines is None:
                return None
            return dict(base, ok=True, result=(lines[-1] if lines else "ok"), detail=lines)
        except Exception as e:
            return dict(base, ok=False, result="%s: %s" % (type(e).__name__, str(e)))

    def _client(self):
        nav = self.host_get("client")
        if nav is None:
            raise RuntimeError("no host blackboard (client) - helm/plot need it")
        return nav

    def _steering(self):
        pctrl = self.player_controller()
        if pctrl is not None:
            t = self.g("SteeringDiving")
            if t is not None:
                r = _try(lambda t=t: pctrl.Access[t]())
                if r[0] == "ok":
                    return r[1]
        st, sd = self._component("SteeringDiving")
        if st != "ok":
            raise RuntimeError("SteeringDiving unavailable: %s" % sd)
        return sd

    def do_sd_dump(self, cmd):
        """DEBUG (temporary): dump SteeringDiving member names + probe candidate
        attributes. Pure dir()/getattr + indexing, no method calls - freeze-safe."""
        lines = ["sd-dump: SteeringDiving member dump"]
        sd = self._steering()
        try:
            names = sorted(str(n) for n in dir(sd))
        except Exception as e:
            raise RuntimeError("dir failed: %s" % _desc(e, 80))
        lines.append("dir count=%d" % len(names))
        chunk = "sd-dump members: " + ", ".join(names)
        for off in range(0, len(chunk), 1400):
            self.emit(chunk[off:off + 1400])
        candidates = [
            "AutoTrim", "ManualTrim", "SetBubble", "CatchBubble", "ReleaseBubble",
            "ToggleBowPlanes", "LockForwardPlanes", "LockIntSternPlanes",
            "SetForwardPlanes", "SetSternPlanes", "SetRudder", "ReleaseRudder",
            "SetDepth", "SetHeading", "SetEOT", "SetSpeed", "SetTPK", "SetTurns",
            "PresetDepth", "Navigation", "EOT", "Controller", "Scope", "Cavitation",
            "OrderedEOT", "OrderedSpeed", "OrderedHeading", "OrderedDepth",
            "BowPlanesRetracted", "ForwardPlanesLocked", "IntSternPlanesLocked",
            "DefaultEOT", "SurfaceDepth", "PeriscopeDepth", "StandardDepth",
            "MaxOperationalDepth", "_Scope", "SMRI",
        ]
        for name in candidates:
            if name not in names:
                self.emit("sd-dump absent: %s" % name)
                continue
            r = _try(lambda name=name: getattr(sd, name))
            if r[0] == "ok":
                v = r[1]
                try:
                    val = str(v)
                except Exception:
                    val = "<unprintable>"
                self.emit("sd-dump get %s = %s (type %s)" % (name, val[:100], _desc(type(v), 40)))
                lines.append("%s = %s" % (name, val[:100]))
            else:
                self.emit("sd-dump get %s ERR %s" % (name, _desc(r[1], 60)))
        # resolve related components via the player controller Access indexer
        # (same pattern as _component/_steering; freeze-safe, established live)
        ctrl = self.player_controller()
        for cname in ("Hydrodynamics", "Maneuvering", "MechTools", "Propulsion",
                      "Hydrostatics", "Navigation"):
            t = self.g(cname)
            if t is None:
                self.emit("sd-dump cmp %s: no type (g() None)" % cname)
                continue
            r = _try(lambda t=t: ctrl.Access[t]())
            if r[0] != "ok":
                self.emit("sd-dump cmp %s: Access ERR %s" % (cname, _desc(r[1], 60)))
                continue
            comp = r[1]
            if comp is None:
                self.emit("sd-dump cmp %s: Access -> None (component absent)" % cname)
                continue
            try:
                cnames = sorted(str(n) for n in dir(comp))
            except Exception as e:
                self.emit("sd-dump cmp %s: dir ERR %s" % (cname, _desc(e, 60)))
                continue
            chunk = "sd-dump cmp %s dir(%d): %s" % (cname, len(cnames), ", ".join(cnames))
            for off in range(0, len(chunk), 1400):
                self.emit(chunk[off:off + 1400])
            probe = {
                "Hydrodynamics": ("ForwardPlanes", "ForwardPlanesType",
                                  "SternPlanes", "Rudders",
                                  "BowPlanes", "Planes", "FlapAngle", "Angles",
                                  "DepthEnvelope", "DepthEnvelopes", "Bubble",
                                  "MaxPlaneRateOfTurn", "SteeringMode",
                                  "ForwardPlanesType", "SternPlanesType",
                                  "GetForwardPlane", "GetSternPlane", "GetRudder"),
                "Maneuvering": ("TPK", "STW", "RPM", "ShaftRPM", "Turns",
                                "Speed", "SpeedThroughWater", "Knots",
                                "Propulsion", "OrderedTPK", "CMP", "SL", "NL",
                                "Status", "SoundSources"),
                "MechTools": ("EOTOrder", "Telegrams", "Telegraph", "OrderedEOT",
                              "OrderedSpeed", "SetEOT", "TPK", "STW"),
                "Propulsion": ("RPM", "ShaftRPM", "Power", "Speed", "TPK", "STW"),
                "Hydrostatics": ("Displacement", "Trim", "Bubble", "Draft",
                                 "Weight", "CenterOfBuoyancy", "SL", "NL", "RoB",
                                 "TnC", "FlowNoise", "MBT", "StabilizerEfficiency"),
                "Navigation": ("DepthEnvelope", "DepthEnvelopes", "Maneuvering",
                               "TPK", "STW", "Hydrodynamics", "OrderedDepth",
                               "CurrentDepth", "INS", "Plot", "Pathfinder",
                               "_DepthEnvelopes", "Bubble", "AutoTrim",
                               "DepthGauge", "AltitudedGauge", "SpeedLog",
                               "GNSS", "BottomRanging", "TerrainCorrelation"),
            }.get(cname, ())
            for pname in probe:
                if pname not in cnames:
                    self.emit("sd-dump cmp %s absent: %s" % (cname, pname))
                    continue
                r2 = _try(lambda pname=pname: getattr(comp, pname))
                if r2[0] == "ok":
                    v = r2[1]
                    try:
                        val = str(v)
                    except Exception:
                        val = "<unprintable>"
                    self.emit("sd-dump cmp %s get %s = %s (type %s)"
                              % (cname, pname, val[:90], _desc(type(v), 40)))
                else:
                    self.emit("sd-dump cmp %s get %s ERR %s"
                              % (cname, pname, _desc(r2[1], 60)))
            # targeted freeze-risk tests (control command only, never the tick)
            if cname == "Hydrodynamics":
                arr = _try(lambda: comp.ForwardPlanes)
                if arr[0] == "ok" and arr[1] is not None:
                    try:
                        n = len(arr[1])
                    except Exception:
                        n = 0
                    for i in range(min(n, 8)):
                        r2 = _try(lambda i=i: comp.ForwardPlanes[i].FlapAngle)
                        self.emit("sd-dump cmp Hydro fwd[%d].FlapAngle = %s" % (i, r2[1]))
                for getter, count in (("GetForwardPlane", 4), ("GetSternPlane", 4), ("GetRudder", 2)):
                    if getter not in cnames:
                        continue
                    for i in range(count):
                        r2 = _try(lambda getter=getter, i=i: getattr(comp, getter)(i).FlapAngle)
                        self.emit("sd-dump cmp Hydro %s(%d).FlapAngle = %s"
                                  % (getter, i, r2[1] if r2[0] == "ok" else r2[1]))
            elif cname == "Navigation" and "DepthGauge" in cnames:
                dg = _try(lambda: comp.DepthGauge)
                if dg[0] == "ok" and dg[1] is not None:
                    try:
                        dgn = sorted(str(n) for n in dir(dg[1]))
                    except Exception:
                        dgn = []
                    chunk = "sd-dump cmp Nav DepthGauge dir(%d): %s" % (len(dgn), ", ".join(dgn))
                    for off in range(0, len(chunk), 1400):
                        self.emit(chunk[off:off + 1400])
            elif cname == "Maneuvering":
                for pname in ("CMP", "SL", "NL", "Status", "SoundSources"):
                    if pname not in cnames:
                        continue
                    r2 = _try(lambda pname=pname: getattr(comp, pname))
                    self.emit("sd-dump cmp Maneuvering %s = %s (type %s)"
                              % (pname, str(r2[1])[:60] if r2[0] == "ok" else r2[1],
                                 _desc(type(r2[1]), 30) if r2[0] == "ok" else ""))
        return lines

    def _probe_members(self, comp, label, candidates, lines):
        """dir() + getattr probes of a component. dir() goes to the log only
        (keeps ship_results.json small); candidates go to both. Freeze-safe."""
        try:
            names = sorted(str(n) for n in dir(comp))
        except Exception as e:
            lines.append("%s dir ERR %s" % (label, _desc(e, 60)))
            return
        lines.append("%s dir(%d)" % (label, len(names)))
        chunk = "tanks/env %s dir(%d): %s" % (label, len(names), ", ".join(names))
        for off in range(0, len(chunk), 1400):
            self.emit(chunk[off:off + 1400])
        for cand in candidates:
            if cand not in names:
                lines.append("%s absent: %s" % (label, cand))
                continue
            r = _try(lambda cand=cand: getattr(comp, cand))
            if r[0] == "ok":
                v = r[1]
                if callable(v):
                    lines.append("%s.%s -> callable %s" % (label, cand, _desc(type(v), 40)))
                else:
                    try:
                        val = str(v)
                    except Exception:
                        val = "<unprintable>"
                    lines.append("%s.%s = %s (%s)" % (label, cand, val[:80], _desc(type(v), 40)))
            else:
                lines.append("%s.%s ERR %s" % (label, cand, _desc(r[1], 60)))

    def do_tanks(self, cmd):
        """Ballast / trim / valve probe (read-only by default) + optional writes.

        Resolves Hydrostatics and its MBTManager (main ballast tanks) /
        TnCManager (trim & compensation) via the player-controller Access
        indexer and reports dir() + member probes of the IL-verified API
        (Flood/Drain/Blow/Charge/vents/low-pressure blower/bank valves/levels
        on MBT; trim tanks/pumps/valves on TnC), plus which SteeringDiving
        high-level tank methods exist on the live build. Pure dir()/getattr +
        indexing - freeze-safe.

        Write sub-commands (control path, STATE-CHANGING - only use when the
        sub is surfaced or ballast state doesn't matter): `tanks vent|flood|
        drain|blow|charge|blower` toggle the MBTManager method, `tanks bank N`
        calls SetBankValve(N)."""
        lines = ["tanks: ballast/trim probe"]
        st, hs = self._component("Hydrostatics", owner="player")
        if st != "ok" or hs is None:
            st, hs = self._component("Hydrostatics")
        if st != "ok" or hs is None:
            lines.append("no Hydrostatics component (%s)" % hs)
            return lines
        self.emit("tanks cp0: Hydrostatics resolved")
        self._probe_members(hs, "Hydrostatics", [
            "Displacement", "Trim", "Bubble", "Draft", "Weight",
            "CenterOfBuoyancy", "SL", "NL", "RoB", "StabilizerEfficiency",
            "OceanBehaviour", "RealismCoefficient", "Snorkel", "FlowNoise",
            "SoundSources", "MBT", "TnC", "get_SL", "get_NL", "get_MBT",
            "get_TnC", "get_FlowNoise", "get_SoundSources", "get_Displacement",
            "get_Trim", "get_RoB", "get_OceanBehaviour", "get_Snorkel",
        ], lines)
        mbt = None
        mbt_src = None
        for fname in ("MBT", "get_MBT"):
            r = _try(lambda fname=fname: getattr(hs, fname))
            if r[0] == "ok" and r[1] is not None:
                mbt = r[1]
                mbt_src = fname
                break
        if mbt is None:
            st, mbt = self._component_any("MBTManager")
            if st == "ok" and mbt is not None:
                lines.append("MBTManager via Access")
        if mbt is None:
            lines.append("MBTManager: not resolvable")
        else:
            if mbt_src is not None:
                lines.append("MBTManager via Hydrostatics.%s" % mbt_src)
            self.emit("tanks cp1: MBTManager resolved (%s)" % _desc(type(mbt), 40))
            self._probe_members(mbt, "MBTManager", [
                "Flood", "Drain", "Blow", "Charge", "SetLevelRatio",
                "ToggleVent", "ToggleBlower", "SetBankValve", "IsVentOpen",
                "IsBlowerOpen", "TotalLevel", "get_TotalLevel", "Level",
                "LevelRatio", "Vent", "Blower", "BankValve", "Valve", "Valves",
                "EmergencyBlow", "HighPressure", "LowPressure", "Pump",
                "Pumps", "Pressure", "State", "Status", "Tanks", "MainBallast",
                "GetLevel", "GetVent", "GetBlower", "VentOpen", "BlowerOpen",
                "SetVent", "SetBlower", "SetFlood", "SetDrain", "SetBlow",
                "OpenVent", "CloseVent", "OpenBlower", "CloseBlower",
            ], lines)
            for pname in ("TotalLevel", "MeanLevelRatio", "TotalCapacity",
                          "Capacity", "Length", "MBTAction", "MBTsAction"):
                r = _try(lambda pname=pname: getattr(mbt, pname))
                if r[0] == "ok" and not callable(r[1]):
                    lines.append("MBTManager.%s = %s" % (pname, str(r[1])[:40]))
                elif r[0] == "ok":
                    lines.append("MBTManager.%s -> callable" % pname)
                else:
                    lines.append("MBTManager.%s ERR %s" % (pname, _desc(r[1], 50)))
            try:
                nbank = int(_try(lambda: mbt.Length)[1]) if _try(lambda: mbt.Length)[0] == "ok" else 0
            except Exception:
                nbank = 0
            for i in range(min(max(nbank, 0), 8)):
                for mname, label in (("Level", "Level"), ("LevelRatio", "LevelRatio"),
                                     ("GetBank", "Bank"), ("GetBankValve", "BankValve")):
                    r = _try(lambda mname=mname, i=i: getattr(mbt, mname)(i))
                    lines.append("MBTManager.%s(%d) = %s" % (
                        label, i, str(r[1])[:40] if r[0] == "ok" else r[1]))
                for mname in ("IsVentOpen", "IsBlowerOpen"):
                    r = _try(lambda mname=mname, i=i: getattr(mbt, mname)(i))
                    lines.append("MBTManager.%s(%d) = %s" % (
                        mname, i, r[1] if r[0] == "ok" else r[1]))
        tnc = None
        tnc_src = None
        for fname in ("TnC", "get_TnC"):
            r = _try(lambda fname=fname: getattr(hs, fname))
            if r[0] == "ok" and r[1] is not None:
                tnc = r[1]
                tnc_src = fname
                break
        if tnc is None:
            st, tnc = self._component_any("TnCManager")
            if st == "ok" and tnc is not None:
                lines.append("TnCManager via Access")
        if tnc is None:
            lines.append("TnCManager: not resolvable")
        else:
            if tnc_src is not None:
                lines.append("TnCManager via Hydrostatics.%s" % tnc_src)
            self.emit("tanks cp2: TnCManager resolved (%s)" % _desc(type(tnc), 40))
            self._probe_members(tnc, "TnCManager", [
                "Trim", "TrimTank", "TrimTanks", "TrimValve", "TrimValves",
                "TrimPump", "Pump", "Pumps", "Drain", "DrainValve",
                "DrainPump", "Transfer", "TransferPump", "Compensate",
                "Compensation", "Bubble", "TrimAngle", "Forward", "Aft",
                "Port", "Starboard", "Water", "Ballast", "SetTrim", "SetLevel",
                "Level", "LevelRatio", "Status", "State", "get_Trim",
                "get_Bubble", "get_Level", "get_LevelRatio", "get_TrimPump",
                "SetBubble",
            ], lines)
            for pname in ("TotalLevel", "TotalCapacity", "Capacity",
                          "TrimFloodValve"):
                r = _try(lambda pname=pname: getattr(tnc, pname))
                if r[0] == "ok" and not callable(r[1]):
                    lines.append("TnCManager.%s = %s" % (pname, str(r[1])[:40]))
                    if pname == "TrimFloodValve":
                        self._probe_members(r[1], "TrimFloodValve", [
                            "State", "Value", "Output", "Regulated",
                            "IsOpen", "IsClosed", "Target", "Current",
                            "Open", "Close", "Set", "SetState", "SetValue",
                        ], lines)
                elif r[0] == "ok":
                    lines.append("TnCManager.%s -> callable" % pname)
                else:
                    lines.append("TnCManager.%s ERR %s" % (pname, _desc(r[1], 50)))
            ntrim = 0
            for i in range(8):
                r = _try(lambda i=i: tnc.Level(i))
                if r[0] != "ok":
                    break
                ntrim = i + 1
            for i in range(min(max(ntrim, 0), 8)):
                for mname in ("Level", "LevelRatio"):
                    r = _try(lambda mname=mname, i=i: getattr(tnc, mname)(i))
                    lines.append("TnCManager.%s(%d) = %s" % (
                        mname, i, str(r[1])[:40] if r[0] == "ok" else r[1]))
                for mname in ("GetTrimPump", "GetTrimValveStatus"):
                    r = _try(lambda mname=mname, i=i: getattr(tnc, mname)(i))
                    lines.append("TnCManager.%s(%d) = %s" % (
                        mname, i, str(r[1])[:50] if r[0] == "ok" else r[1]))
            for mname in ("TrimFlood", "TrimDrain", "TrimTransfer",
                          "TrimCirculation", "FloodTrim", "StopFloodTrim",
                          "ToggleTrimPump", "SetTrimPumpRPM",
                          "SetTrimValveStatus", "StartCirculation",
                          "StopCirculation"):
                r = _try(lambda mname=mname: getattr(tnc, mname))
                lines.append("TnCManager.%s %s" % (
                    mname, "-> callable" if r[0] == "ok" and callable(r[1]) else
                    ("absent" if r[0] != "ok" else "-> value")))
            for pname in ("TrimMode", "AutoTrim", "Manual", "Mode",
                          "Automatic", "TrimAuto", "Auto", "IsManual"):
                r = _try(lambda pname=pname: getattr(tnc, pname))
                if r[0] == "ok":
                    lines.append("TnCManager.%s = %s" % (pname, str(r[1])[:50]))
            for mname in ("SetTrimMode", "SetMode", "SetManual",
                          "EnableAutoTrim", "DisableAutoTrim", "set_TrimMode",
                          "SetAutoTrim"):
                r = _try(lambda mname=mname: getattr(tnc, mname))
                if r[0] == "ok":
                    lines.append("TnCManager.%s -> %s" % (
                        mname, "callable" if callable(r[1]) else str(r[1])[:50]))
        try:
            sd = self._steering()
        except Exception as e:
            sd = None
            lines.append("SteeringDiving: %s" % _desc(e, 60))
        if sd is not None:
            try:
                sdn = sorted(str(n) for n in dir(sd))
            except Exception:
                sdn = []
            for c in ("SetToggleVent", "SetToggleBlower", "SetMBTFlood",
                      "SetMBTDrain", "SetMBTBlow", "SetMBTBankValve",
                      "SurfaceOperation", "AutoCrewDive", "DiveCO",
                      "AutoCrewSurface", "StandardSurfaceCO",
                      "EmergencySurfaceCO", "AutoCrewPresetDepth",
                      "BlowMainBallast", "EmergencyBlow", "ManualDive",
                      "AutoCrew", "ReleaseDepth"):
                lines.append("SteeringDiving.%s %s" % (
                    c, "present" if c in sdn else "absent"))
        for wname, mname, nargs in (("vent", "ToggleVent", 0),
                                    ("blower", "ToggleBlower", 0),
                                    ("flood", "Flood", 0),
                                    ("drain", "Drain", 0),
                                    ("blow", "Blow", 0),
                                    ("charge", "Charge", 0),
                                    ("bank", "SetBankValve", 1)):
            if not cmd.get(wname):
                continue
            if mbt is None:
                lines.append("write %s: no MBTManager" % wname)
                continue
            try:
                mnames = [str(n) for n in dir(mbt)]
            except Exception:
                mnames = []
            if mname not in mnames:
                lines.append("write %s: %s absent on MBTManager" % (wname, mname))
                continue
            if nargs == 0:
                r = _try(lambda mname=mname: getattr(mbt, mname)())
            else:
                arg = int(cmd[wname])
                r = _try(lambda mname=mname, arg=arg: getattr(mbt, mname)(arg))
            lines.append("write %s (%s): %s" % (
                wname, mname, "ok" if r[0] == "ok" else r[1]))
        tnc = self._resolve_tnc()
        if any(cmd.get(k) for k in ("pump", "rpm", "valve", "tctl", "fvalve",
                                    "tdrain", "tflood", "ttransfer",
                                    "tcirc", "fill", "drainall")):
            if tnc is None:
                lines.append("write trim: no TnCManager")
                return lines
            tnames = []
            try:
                tnames = [str(n) for n in dir(tnc)]
            except Exception:
                pass
            if cmd.get("pump"):
                pidx = 0
                pval = True
                if isinstance(cmd["pump"], str):
                    pparts = str(cmd["pump"]).split()
                    try:
                        pidx = int(pparts[0])
                    except (ValueError, IndexError):
                        pidx = 0
                    pval = (len(pparts) < 2 or
                            pparts[1].lower() in ("1", "on", "true", "start", "yes"))
                r = _try(lambda pidx=pidx, pval=pval: tnc.ToggleTrimPump(pidx, pval))
                lines.append("write pump (ToggleTrimPump(%d, %s)): %s" % (
                    pidx, pval, "ok" if r[0] == "ok" else r[1]))
            if cmd.get("rpm") is not None:
                pparts = str(cmd["rpm"]).split()
                try:
                    pidx = int(pparts[0]) if len(pparts) > 1 else 0
                    rpm = float(pparts[-1])
                except (ValueError, IndexError):
                    pidx, rpm = None, None
                if rpm is None:
                    lines.append("write rpm: need numeric (e.g. rpm 100 or rpm 0 100)")
                elif "SetTrimPumpRPM" not in tnames:
                    lines.append("write rpm: SetTrimPumpRPM absent")
                else:
                    r = _try(lambda pidx=pidx, rpm=rpm: tnc.SetTrimPumpRPM(pidx, rpm))
                    lines.append("write rpm (SetTrimPumpRPM(%d, %s)): %s" % (
                        pidx, rpm, "ok" if r[0] == "ok" else r[1]))
            if cmd.get("valve") is not None:
                parts = str(cmd["valve"]).split()
                vidx = None
                varg = "open"
                vref = "@TrimValveStatus.In"
                try:
                    vidx = int(parts[0])
                    varg = parts[1] if len(parts) > 1 else "open"
                    closed = varg.lower() in ("close", "closed", "0", "false", "off", "0.0")
                    vref = "@TrimValveStatus.Closed" if closed else "@TrimValveStatus.In"
                except (ValueError, IndexError):
                    lines.append("write valve: need 'N [open|close|0|1]'")
                if vidx is not None:
                    if "SetTrimValveStatus" not in tnames:
                        lines.append("write valve: SetTrimValveStatus absent")
                    else:
                        vval = self._parse_ctl_arg(vref)
                        if isinstance(vval, _EnumRefError):
                            lines.append("write valve: %s" % vval)
                        else:
                            r = _try(lambda vidx=vidx, vval=vval: tnc.SetTrimValveStatus(vidx, vval))
                            lines.append("write valve (%s SetTrimValveStatus(%d, %s)): %s" % (
                                varg, vidx, vval, "ok" if r[0] == "ok" else r[1]))
            if cmd.get("fill"):
                # Fill procedure (live-verified 2026-08-16): TrimMode=Hand,
                # TrimFloodValve open (SetRatio 1.0), tank valves -> In, NO pump.
                # arg: "all" | "TANKS..." (space-separated idx) | "N"
                fparts = str(cmd["fill"]).split()
                ftanks = []
                if fparts:
                    for p in fparts:
                        try:
                            ftanks.append(int(p))
                        except ValueError:
                            pass
                if not ftanks:
                    ftanks = list(range(8))
                try:
                    fv = getattr(tnc, "TrimFloodValve")
                except Exception as e:
                    fv = None
                    lines.append("write fill: TrimFloodValve absent: %s" % _desc(e, 60))
                if fv is not None:
                    r = _try(lambda: fv.SetRatio(1.0))
                    lines.append("write fill (fvalve SetRatio 1.0): %s" % (
                        "ok" if r[0] == "ok" else r[1]))
                for ft in sorted(set(ftanks)):
                    if "SetTrimValveStatus" not in tnames:
                        lines.append("write fill: SetTrimValveStatus absent")
                        break
                    vval = self._parse_ctl_arg("@TrimValveStatus.In")
                    if isinstance(vval, _EnumRefError):
                        lines.append("write fill: %s" % vval)
                        break
                    r = _try(lambda ft=ft, vval=vval: tnc.SetTrimValveStatus(ft, vval))
                    lines.append("write fill (SetTrimValveStatus(%d, In)): %s" % (
                        ft, "ok" if r[0] == "ok" else r[1]))
            if cmd.get("drainall"):
                # Drain procedure (live-verified 2026-08-16): tank valves -> Out,
                # TrimDrain(0) coroutine; OUTBOARD valve is GUI-only (no API object
                # found), trim pumps = inter-tank transfer (NOT drain pump).
                dparts = str(cmd["drainall"]).split()
                dtanks = []
                for p in dparts:
                    try:
                        dtanks.append(int(p))
                    except ValueError:
                        pass
                if not dtanks:
                    dtanks = list(range(8))
                for dt in sorted(set(dtanks)):
                    if "SetTrimValveStatus" not in tnames:
                        lines.append("write drain: SetTrimValveStatus absent")
                        break
                    vval = self._parse_ctl_arg("@TrimValveStatus.Out")
                    if isinstance(vval, _EnumRefError):
                        lines.append("write drain: %s" % vval)
                        break
                    r = _try(lambda dt=dt, vval=vval: tnc.SetTrimValveStatus(dt, vval))
                    lines.append("write drain (SetTrimValveStatus(%d, Out)): %s" % (
                        dt, "ok" if r[0] == "ok" else r[1]))
                r = _try(lambda: tnc.TrimDrain(0))
                lines.append("write drain (TrimDrain(0)): %s" % (
                    "ok" if r[0] == "ok" else r[1]))
            if cmd.get("fvalve") is not None:
                fparts = str(cmd["fvalve"]).split()
                fop = fparts[0].lower() if fparts else "read"
                fv = None
                try:
                    fv = getattr(tnc, "TrimFloodValve")
                except Exception as e:
                    lines.append("write fvalve: TrimFloodValve absent: %s" % _desc(e, 60))
                if fv is not None:
                    if fop == "read":
                        for pname in ("OpenRatio", "RegulatedOutput", "OutputVolume",
                                      "_OpenRatio", "_OutputVolume"):
                            r = _try(lambda pname=pname: getattr(fv, pname))
                            lines.append("TrimFloodValve.%s = %s" % (
                                pname, str(r[1])[:50] if r[0] == "ok" else r[1]))
                    elif fop in ("open", "on"):
                        ratio = float(fparts[1]) if len(fparts) > 1 else 1.0
                        r = _try(lambda ratio=ratio: fv.SetRatio(ratio))
                        lines.append("write fvalve (SetRatio %s): %s" % (
                            ratio, "ok" if r[0] == "ok" else r[1]))
                    elif fop in ("close", "off"):
                        r = _try(lambda: fv.SetRatio(0.0))
                        lines.append("write fvalve (SetRatio 0.0): %s" % (
                            "ok" if r[0] == "ok" else r[1]))
                    elif fop == "ratio":
                        try:
                            ratio = float(fparts[1])
                        except (ValueError, IndexError):
                            ratio = None
                        if ratio is None:
                            lines.append("write fvalve: need 'ratio N'")
                        else:
                            r = _try(lambda ratio=ratio: fv.SetRatio(ratio))
                            lines.append("write fvalve (SetRatio %s): %s" % (
                                ratio, "ok" if r[0] == "ok" else r[1]))
                    else:
                        lines.append("write fvalve: need read|open|close|ratio N")
            if cmd.get("tctl"):
                parts = str(cmd["tctl"]).split()
                mname = parts[0]
                if mname == "@@info":
                    if len(parts) < 2:
                        lines.append("@@info: need TypeName")
                    else:
                        t = self._clr_type(parts[1])
                        if t is None:
                            lines.append("@@info %s: not resolvable" % parts[1])
                        else:
                            lines.append("@@info %s = %s" % (parts[1], _desc(type(t), 60)))
                            try:
                                members = sorted(str(n) for n in dir(t)
                                                 if not n.startswith("_"))
                            except Exception as e:
                                members = ["<dir ERR %s>" % _desc(e, 40)]
                            lines.append("@@info %s members(%d): %s" % (
                                parts[1], len(members), ", ".join(members)))
                else:
                    cargs = [self._parse_ctl_arg(a) for a in parts[1:]]
                    if mname not in TNC_CTL_ALLOWED:
                        lines.append("tctl %s: not in allowed set" % mname)
                    elif mname not in tnames:
                        lines.append("tctl %s: absent on TnCManager" % mname)
                    else:
                        err = [c for c in cargs if isinstance(c, _EnumRefError)]
                        if err:
                            lines.append("tctl %s: arg err: %s" % (mname, err[0].msg))
                        else:
                            fn = getattr(tnc, mname)
                            try:
                                if callable(fn):
                                    rv = fn(*cargs)
                                    lines.append("tctl %s(%s) = %s" % (
                                        mname, ", ".join(repr(a) for a in cargs),
                                        str(rv)[:60]))
                                else:
                                    lines.append("tctl %s = %s" % (mname, str(fn)[:60]))
                            except Exception as e:
                                lines.append("tctl %s(%s) EXC %s" % (
                                    mname, ", ".join(repr(a) for a in cargs),
                                    _desc(e, 80)))
            for wname, mname in (("tdrain", "TrimDrain"), ("tflood", "TrimFlood"),
                                 ("ttransfer", "TrimTransfer"),
                                 ("tcirc", "TrimCirculation")):
                if not cmd.get(wname):
                    continue
                if mname not in tnames:
                    lines.append("write %s: %s absent" % (wname, mname))
                    continue
                r = _try(lambda mname=mname: getattr(tnc, mname)())
                lines.append("write %s (%s): %s" % (
                    wname, mname, "ok" if r[0] == "ok" else r[1]))
        return lines

    def _component_via_field(self, obj, field_names, label):
        """Resolve a sub-component via getattr on obj (e.g. Hydrostatics.MBT)."""
        for fname in field_names:
            r = _try(lambda fname=fname: getattr(obj, fname))
            if r[0] == "ok" and r[1] is not None:
                return r[1]
        return None

    def do_alarm(self, cmd):
        """Ship alarm + rigging discovery (control command).

        Sub-commands:
          alarm            probe candidate type names via Access[T]() on
                           both player+host, scan ALL blackboard keys,
                           and dump player child GameObjects
          alarm alarms     only the Alarms* family
          alarm rigging    only the Rigging* family
          alarm brute      brute-force scan 200+ type names via Access[T]()
        Freeze-safe: only dir()/getattr + Access[T]() calls."""
        sub = str(cmd.get("sub") or "").lower()
        lines = ["alarm: ship alarm/rigging scan"]
        families = {
            "alarms": ("Alarms", "Alarm", "AlarmSystem", "AlarmManager",
                       "AlarmsManager", "ShipAlarms", "AlarmControl",
                       "AlarmSignaller", "AlarmController",
                       "DamageControl", "DamageManager", "DCManager",
                       "ShipControl", "ShipManager", "DamageState",
                       "GeneralAlarm", "CollisionAlarm", "FloodAlarm",
                       "FireAlarm", "LeakAlarm"),
            "rigging": ("Rigging", "RiggingManager", "RiggingController",
                        "RiggingState", "RiggingSystem", "ShipRigging",
                        "RiggingData", "RiggingComponent", "RiggingControl",
                        "Mast", "MastManager", "Masts", "MastsController",
                        "Snorkel", "Periscope", "Antenna", "AntennaManager",
                        "PeriscopeManager", "SnorkelManager"),
        }
        if sub == "alarms":
            families = {"alarms": families["alarms"]}
        elif sub == "rigging":
            families = {"rigging": families["rigging"]}
        elif sub == "integrity":
            lines = ["alarm integrity: live damage state dump"]
            st, comp = self._component("Integrity")
            if st == "ok" and comp is not None:
                self.emit("alarm integrity: Integrity resolved")
                self._probe_members(comp, "Integrity", [
                    "DamageLevelRatio", "HullDamage", "CompartmentDamage",
                    "Flooding", "Fire", "Leak", "Pressure", "Integrity",
                    "MaxDamage", "DamageState", "Damage", "IsDestroyed",
                    "IsDamaged", "IsRepairing", "RepairRate",
                ], lines)
                for pname in ("DamageLevelRatio", "HullDamage", "CompartmentDamage",
                              "Flooding", "Fire", "Leak", "Pressure", "Integrity",
                              "MaxDamage", "DamageState", "Damage", "IsDestroyed",
                              "IsDamaged", "IsRepairing", "RepairRate"):
                    r = _try(lambda pname=pname: getattr(comp, pname))
                    if r[0] == "ok" and not callable(r[1]):
                        lines.append("Integrity.%s = %s" % (pname, str(r[1])[:60]))
                    elif r[0] == "ok":
                        lines.append("Integrity.%s -> callable" % pname)
            else:
                lines.append("no Integrity component (%s)" % st)
            st2, cox = self._component("Coxswain")
            if st2 == "ok" and cox is not None:
                self.emit("alarm integrity: Coxswain resolved")
                self._probe_members(cox, "Coxswain", [
                    "Bulkheads", "Lights", "CIWs", "State", "Status",
                    "DamageControl", "GeneralQuarters", "GQ", "BattleStations",
                    "RepairTeams", "FireSuppression", "FloodDoors",
                    "IsGQ", "IsBattleStations", "QuartersState",
                ], lines)
                for pname in ("Bulkheads", "Lights", "CIWs", "State", "Status",
                              "DamageControl", "GeneralQuarters", "GQ",
                              "BattleStations", "RepairTeams", "FireSuppression",
                              "FloodDoors", "IsGQ", "IsBattleStations",
                              "QuartersState"):
                    r = _try(lambda pname=pname: getattr(cox, pname))
                    if r[0] == "ok" and not callable(r[1]):
                        lines.append("Coxswain.%s = %s" % (pname, str(r[1])[:60]))
                    elif r[0] == "ok":
                        lines.append("Coxswain.%s -> callable" % pname)
            else:
                lines.append("no Coxswain component (%s)" % st2)
            bb = self._blackboard_storage()
            if bb:
                try:
                    items = list(bb.items())
                except Exception:
                    items = []
                for k, v in items:
                    if isinstance(k, str) and re.search(
                            r"alarm|damage|flood|fire|leak|bulkhead|gq|"
                            r"quarters|battle|vent|hatch|repair", k, re.I):
                        lines.append("bb: %s = %s" % (k, _desc(v, 40)))
            return lines
        if sub == "control-check":
            lines = ["alarm control-check: damage/damage-control surface"]
            st_i, comp_i = self._component("Integrity")
            st_c, comp_c = self._component("Coxswain")
            if st_i == "ok" and comp_i is not None:
                # Integrity control-relevant members
                ctrl_names = [
                    "SetPointFire", "ExtinguishPointFromFire",
                    "BeginTankFlooding", "StopTankFlooding",
                    "ExplosionDamage", "ImpactDamage", "CollisionDamage",
                    "Shock", "Damage", "ForceDamage",
                ]
                for m in ctrl_names:
                    r = _try(lambda m=m: getattr(comp_i, m))
                    tag = "callable" if r[0] == "ok" and callable(r[1]) else ("val=%s" % str(r[1])[:40] if r[0] == "ok" else r[0])
                    lines.append("Integrity.%s -> %s" % (m, tag))
                # Tanks
                tanks_count = _try(lambda: comp_i.IntegrityTanks.Count)
                tcnt = tanks_count[1] if tanks_count[0] == "ok" else 0
                lines.append("IntegrityTanks count = %s" % str(tcnt))
                tank_methods = [
                    "SetBulkheadStatus", "SetFire", "ExtinguishFire",
                    "BeginFlooding", "StopFlooding",
                    "ForceDamage", "Shock", "Clear",
                ]
                for i in range(min(tcnt if isinstance(tcnt, int) else 0, 10)):
                    tank = _try(lambda i=i: comp_i.IntegrityTanks[i])
                    if tank[0] != "ok" or tank[1] is None:
                        continue
                    tank_obj = tank[1]
                    present = []
                    absent = []
                    for m in tank_methods:
                        r = _try(lambda m=m: getattr(tank_obj, m))
                        if r[0] == "ok" and r[1] is not None:
                            present.append(m + ("()" if callable(r[1]) else "=%s" % str(r[1])[:30]))
                        else:
                            absent.append(m)
                    lines.append("Tank[%d]: present=%s absent=%s" % (
                        i, ",".join(present) or "-", ",".join(absent) or "-"))
            else:
                lines.append("no Integrity (%s)" % st_i)
            if st_c == "ok" and comp_c is not None:
                # Coxswain subsystems
                for sub_name in ("Bulkheads", "Lights", "CIWs"):
                    r = _try(lambda sub_name=sub_name: getattr(comp_c, sub_name))
                    if r[0] == "ok" and r[1] is not None:
                        obj = r[1]
                        members = [m for m in dir(obj) if not m.startswith("_")]
                        ctrl_relevant = [m for m in members if m in (
                            "CloseBulkheads", "OpenBulkheads", "IsSystemEnabled",
                            "SetCode", "SetLightState", "LightsOff", "LightsOn",
                            "EnableCIWs", "DisableCIWs",
                        )]
                        lines.append("Coxswain.%s: %d members, ctrl=%s" % (
                            sub_name, len(members), ",".join(ctrl_relevant) or "-"))
                    else:
                        lines.append("Coxswain.%s: not resolved" % sub_name)
            else:
                lines.append("no Coxswain (%s)" % st_c)
            return lines
        member_hints = (
            "Name", "State", "Active", "IsActive", "CurrentAlarm",
            "AlarmType", "AlarmSeverity", "Raise", "Trigger", "Signal",
            "Light", "Horn", "Klaxon", "Buzzer", "Panel", "Annunciator",
            "RiggingState", "Rigged", "Unrigged", "Masts", "Antenna",
            "Periscope", "Snorkel", "Schnorchel", "Mast", "SetRigging",
            "SetState", "Configure", "IsRigged",
        )
        for fam, names in families.items():
            for tname in names:
                owner, st, comp = self._component_any(tname)
                if st != "ok" or comp is None:
                    continue
                lines.append("%s: %s FOUND via %s Access (%s)" % (
                    fam, tname, owner, _desc(type(comp), 60)))
                self._probe_members(comp, tname, member_hints, lines)
        ctrl = self.host_get("_Controller")
        if ctrl is not None:
            acc = _try(lambda: getattr(ctrl, "Access"))
            if acc[0] == "ok" and acc[1] is not None:
                try:
                    acc_names = sorted(set(
                        m for m in dir(acc[1])
                        if not m.startswith("_") and m[0].isupper()))
                except Exception:
                    acc_names = []
                if acc_names:
                    lines.append("Access: %d dir() types" % len(acc_names))
                    hits = sorted(set(k for k in acc_names if re.search(
                        r"alarm|rigg|mast|snorkel|periscope|antenna|hatch|"
                        r"fire|flood|leak|gas|vent|scope|raise|damage", k, re.I)))
                    if hits:
                        lines.append("Access alarm/rigging/damage types: %s"
                                     % ", ".join(hits))
                    else:
                        lines.append("Access: no alarm/rigging/damage types via dir()")
        if sub == "brute":
            brute_names = sorted(set(
                families.get("alarms", ()) + families.get("rigging", ()) + (
                "Ballast", "BallastManager", "Hydrostatics", "SteeringDiving",
                "SonarSystem", "RadarSystem", "ESMSystem", "EWSystem",
                "Communications", "NavSystem", "Navigation",
                "WeaponsManager", "WeaponsSystem", "TorpedoSystem",
                "Countermeasures", "Decoys", "NoiseMaker", "BubbleMaker",
                "Propulsion", "Engine", "Reactor", "Turbine", "Shaft",
                "Rudder", "DivePlanes", "Fairwater", "SternPlanes",
                "Helm", "Depth", "Speed", "Course",
                "DamageControl", "FireSuppression", "FloodControl",
                "CompartmentManager", "Compartment", "Bulkhead",
                "Watertight", "SealState", "Hull", "PressureHull",
                "Battery", "AIP", "Diesel", "Generator",
                "Combat", "FireControl", "Targeting", "SonarOperator",
                "CIC", "Bridge", "ControlRoom", "Conn",
                "AlarmPanel", "AlarmState", "AlarmLight", "AlarmHorn",
                "GeneralQuarters", "BattleStations", "RedAlert",
                "CollisionAlert", "FloodAlert", "FireAlert",
                "RigForSurface", "RigForSubmerge", "RiggingState",
                "PeriscopeDepth", "SnorkelDepth",
                )))
            lines.append("brute: probing %d type names..." % len(brute_names))
            found = []
            for tname in brute_names:
                owner, st, comp = self._component_any(tname)
                if st == "ok" and comp is not None:
                    found.append("%s via %s" % (tname, owner))
            if found:
                lines.append("brute FOUND: %s" % "; ".join(found))
            else:
                lines.append("brute: nothing found")
        bb = self._blackboard_storage()
        if bb:
            try:
                items = list(bb.items())
            except Exception:
                items = []
            lines.append("blackboard: %d total keys" % len(items))
            alarm_hits = []
            for k, v in items:
                if isinstance(k, str) and re.search(
                        r"alarm|rigg|mast|snorkel|scope|antenna|flood|fire|"
                        r"leak|damage|hatch|vent", k, re.I):
                    try:
                        alarm_hits.append("%s=%s (%s)" % (k, _desc(v, 40), _desc(type(v), 30)))
                    except Exception:
                        alarm_hits.append(k)
            if alarm_hits:
                lines.append("blackboard alarm/rigging keys: %s" %
                             "; ".join(sorted(alarm_hits)))
            else:
                lines.append("blackboard: no alarm/rigging/damage keys")
                if items:
                    all_keys = sorted(k for k, v in items if isinstance(k, str))
                    lines.append("blackboard ALL keys: %s" % ", ".join(all_keys[:50]))
                    if len(all_keys) > 50:
                        lines.append("  ... (%d more)" % (len(all_keys) - 50))
        if len(lines) == 1:
            tried = sorted(set(n for names in families.values() for n in names))
            lines.append("no alarm/rigging component resolved (tried: %s)"
                         % ", ".join(tried))
        return lines

    def do_dc(self, cmd):
        """Damage control (control command).

        Sub-commands:
          dc status                     same as alarm integrity (read-only)
          dc bulkheads close|open       ship-level bulkheads (CloseBulkheads/OpenBulkheads)
          dc bulkhead <0-9> close|open  per-tank bulkhead (SetBulkheadStatus)
          dc lights                     show NAVSTAT codes + lights state (read-only)
          dc fire <0-9>                 DISABLED (freeze + mono GC crash)
          dc extinguish <0-9>           DISABLED (freeze + mono GC crash)
          dc flood <0-9>                DISABLED (freeze + mono GC crash)
          dc deflood <0-9>              DISABLED (freeze + mono GC crash)

        fire/extinguish/flood/deflood DISABLED: both C# method calls
        (AddBehaviour freeze) and setattr() (mono GC crash via pythonnet
        reflection) are unsafe on IntegrityTank objects.
        Use in-game damage control instead."""
        sub = str(cmd.get("sub") or "").lower()
        lines = ["dc: damage control"]
        if sub == "status":
            return self.do_alarm({"sub": "integrity"})
        # resolve Integrity + Coxswain
        st_i, integ = self._component("Integrity")
        st_c, cox = self._component("Coxswain")
        if sub == "bulkheads":
            val = cmd.get("val", "")
            if st_c != "ok" or cox is None:
                lines.append("no Coxswain (%s)" % st_c)
                return lines
            bh_obj = _try(lambda: getattr(cox, "Bulkheads"))
            if bh_obj[0] != "ok" or bh_obj[1] is None:
                lines.append("no Bulkheads subsystem")
                return lines
            bh = bh_obj[1]
            if val == "close":
                r = _try(lambda: bh.CloseBulkheads())
                lines.append("CloseBulkheads(): %s" % ("ok" if r[0] == "ok" else r[1]))
            elif val == "open":
                r = _try(lambda: bh.OpenBulkheads())
                lines.append("OpenBulkheads(): %s" % ("ok" if r[0] == "ok" else r[1]))
            else:
                lines.append("usage: dc bulkheads close|open")
            return lines
        if sub == "bulkhead":
            idx = cmd.get("idx")
            val = cmd.get("val", "")
            if idx is None or not isinstance(idx, int) or idx < 0:
                lines.append("usage: dc bulkhead <0-9> close|open")
                return lines
            if st_i != "ok" or integ is None:
                lines.append("no Integrity (%s)" % st_i)
                return lines
            tc = _try(lambda: integ.IntegrityTanks.Count)
            if tc[0] != "ok" or not isinstance(tc[1], int) or idx >= tc[1]:
                lines.append("invalid tank index %s (count=%s)" % (idx, tc[1] if tc[0] == "ok" else "?"))
                return lines
            tank = _try(lambda idx=idx: integ.IntegrityTanks[idx])
            if tank[0] != "ok" or tank[1] is None:
                lines.append("cannot access tank %d" % idx)
                return lines
            close = val == "close"
            r = _try(lambda idx=idx, v=close: integ.IntegrityTanks[idx].SetBulkheadStatus(v))
            lines.append("tank %d SetBulkheadStatus(%s): %s" % (
                idx, close, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "lights":
            st_c, comp = self._component("Coxswain")
            if st_c != "ok" or comp is None:
                lines.append("no Coxswain (%s)" % st_c)
                return lines
            r = _try(lambda: comp.Lights)
            if r[0] != "ok" or r[1] is None:
                lines.append("no Lights subsystem (%s)" % r[1])
                return lines
            lights = r[1]
            en = _try(lambda: bool(lights.IsSystemEnabled))
            nav = _try(lambda: str(lights.NAVSTATCodes))
            lines.append("Lights enabled=%s" % (en[1] if en[0] == "ok" else "?"))
            lines.append("NAVSTATCodes (current): %s" % (nav[1] if nav[0] == "ok" else "?"))
            # Enum all NAVSTATCodes values
            et = self.g("ElementTools")
            if et is not None:
                navtype = _try(lambda: et.NAVSTATCodes)
                if navtype[0] == "ok" and navtype[1] is not None:
                    names = _try(lambda: list(__import__("System").Enum.GetNames(navtype[1])))
                    if names[0] == "ok":
                        lines.append("NAVSTATCodes enum (%d values):" % len(names[1]))
                        for n in names[1]:
                            val = _try(lambda n=n: int(__import__("System").Enum.Parse(navtype[1], n)))
                            lines.append("  %s = %s" % (n, val[1] if val[0] == "ok" else "?"))
                    else:
                        lines.append("Enum.GetNames failed: %s" % names[1])
                else:
                    lines.append("NAVSTATCodes type: %s" % navtype[1])
            else:
                lines.append("ElementTools not available")
            return lines
        if sub in ("fire", "extinguish", "flood", "deflood"):
            lines.append("dc %s: DISABLED — all IntegrityTank write methods and" % sub)
            lines.append("  even setattr() on Unity objects freeze the game")
            lines.append("  (pythonnet reflection hits Unity main thread).")
            lines.append("  Use in-game damage control instead.")
            return lines
        lines.append("usage: dc [status|bulkheads close|open|bulkhead <0-9> close|open|"
                     "lights|fire <0-9>|extinguish <0-9>|flood <0-9>|deflood <0-9>]")
        return lines

    MASTS_USAGE = (
        "masts                          show mast / snorkel / periscope state\n"
        "masts raise <id>               raise a single mast\n"
        "masts retract <id>             retract a single mast\n"
        "masts retract-all              retract all masts\n"
        "masts raise-all                raise all masts\n"
        "masts height <id> <0.0-1.0>    set fractional mast height\n"
        "masts periscope <id> <0.0-1.0> rotate periscope\n"
        "masts snorkel raise            raise snorkel\n"
        "masts snorkel retract          retract snorkel\n"
    )

    def do_masts(self, cmd):
        """Mast / snorkel / periscope control (control command).

        Sub-commands:
          masts                          show mast state (read-only)
          masts raise <id>               raise a single mast
          masts retract <id>             retract a single mast
          masts retract-all              retract all masts
          masts raise-all                raise all masts
          masts height <id> <0.0-1.0>    set fractional mast height
          masts periscope <id> <0.0-1.0> rotate periscope
          masts snorkel raise            raise snorkel
          masts snorkel retract          retract snorkel

        MastStatusList: Retracted=0, Moving=1, Raised=2.
        Freeze-safe: wraps all writes in _try()."""
        sub = str(cmd.get("sub") or "").lower()
        lines = ["masts: mast control"]
        where, st, comp = self._component_any("MastsController")
        if st != "ok" or comp is None:
            lines.append("no MastsController (%s)" % (st,))
            return lines
        self.emit("masts: MastsController resolved via %s" % where)
        # read mast IDs + types + status
        mast_ids = []
        mast_types = {}
        r = _try(lambda: list(comp.GetAvailableMastIDs()))
        if r[0] == "ok" and r[1]:
            mast_ids = r[1]
        else:
            for mid in range(0, 8):
                t = _try(lambda mid=mid: str(comp.GetMastType(mid)))
                if t[0] != "ok":
                    break
                mast_ids.append(mid)
        for mid in mast_ids[:8]:
            t = _try(lambda mid=mid: str(comp.GetMastType(mid)))
            if t[0] == "ok":
                mast_types[mid] = t[1]
        # find snorkel mast ID
        snorkel_id = None
        for mid, mtype in mast_types.items():
            if "SNORKEL" in mtype.upper():
                snorkel_id = mid
                break
        # --- read-only: show state ---
        if not sub:
            rc = _try(lambda: int(comp.Status))
            lines.append("controller status=%s | ids=%s" % (
                rc[1] if rc[0] == "ok" else "?", mast_ids))
            for mid in mast_ids[:8]:
                s = _try(lambda mid=mid: str(comp.GetMastStatus(mid)))
                h = _try(lambda mid=mid: float(comp.GetMastHeight(mid)))
                lines.append("  mast %s [%s] status=%s height=%s" % (
                    mid, mast_types.get(mid, "?"),
                    s[1] if s[0] == "ok" else "?",
                    "%.2f" % h[1] if h[0] == "ok" else "?"))
            if snorkel_id is not None:
                lines.append("snorkel mast: %s" % snorkel_id)
            return lines
        # --- write commands ---
        def _mast_enum(val):
            if isinstance(val, int):
                return val
            return _MAST_STATUS.get(str(val).lower(), None)
        if sub == "raise":
            mid = _try(lambda: int(cmd.get("id", -1)))[1] if cmd.get("id") is not None else None
            if mid is None or mid < 0:
                lines.append("usage: masts raise <id>")
                return lines
            enum_val = _mast_enum("raised")
            r = _try(lambda mid=mid, ev=enum_val: comp.SetMast(mid, ev))
            lines.append("raise mast %s (SetMast(%s, %s)): %s" % (
                mid, mid, enum_val, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "retract":
            mid = _try(lambda: int(cmd.get("id", -1)))[1] if cmd.get("id") is not None else None
            if mid is None or mid < 0:
                lines.append("usage: masts retract <id>")
                return lines
            enum_val = _mast_enum("retracted")
            r = _try(lambda mid=mid, ev=enum_val: comp.SetMast(mid, ev))
            lines.append("retract mast %s (SetMast(%s, %s)): %s" % (
                mid, mid, enum_val, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "retract-all":
            r = _try(lambda: comp.RetractAllMasts())
            lines.append("retract-all (RetractAllMasts): %s" % (
                "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "raise-all":
            enum_val = _mast_enum("raised")
            results = []
            for mid in mast_ids[:8]:
                r = _try(lambda mid=mid, ev=enum_val: comp.SetMast(mid, ev))
                results.append("mast %s: %s" % (mid, "ok" if r[0] == "ok" else r[1]))
            lines.append("raise-all: %s" % "; ".join(results))
            return lines
        if sub == "height":
            mid = None
            frac = None
            if cmd.get("id") is not None:
                mid = _try(lambda: int(cmd["id"]))[1]
            if cmd.get("val") is not None:
                frac = _try(lambda: float(cmd["val"]))[1]
            if mid is None or frac is None or mid < 0:
                lines.append("usage: masts height <id> <0.0-1.0>")
                return lines
            r = _try(lambda mid=mid, frac=frac: comp.SetMastHeightFraction(mid, frac))
            lines.append("height mast %s -> %.2f (SetMastHeightFraction(%s, %s)): %s" % (
                mid, frac, mid, frac, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "periscope":
            mid = None
            frac = None
            if cmd.get("id") is not None:
                mid = _try(lambda: int(cmd["id"]))[1]
            if cmd.get("val") is not None:
                frac = _try(lambda: float(cmd["val"]))[1]
            if mid is None or frac is None or mid < 0:
                lines.append("usage: masts periscope <id> <0.0-1.0>")
                return lines
            r = _try(lambda mid=mid, frac=frac: comp.RotatePeriscope(mid, frac))
            lines.append("periscope mast %s -> %.2f (RotatePeriscope(%s, %s)): %s" % (
                mid, frac, mid, frac, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "snorkel_raise":
            if snorkel_id is None:
                lines.append("no snorkel mast found (mast types: %s)" % mast_types)
                return lines
            enum_val = _mast_enum("raised")
            r = _try(lambda sid=snorkel_id, ev=enum_val: comp.SetMast(sid, ev))
            lines.append("snorkel raise (SetMast(%s, %s)): %s" % (
                snorkel_id, enum_val, "ok" if r[0] == "ok" else r[1]))
            return lines
        if sub == "snorkel_retract":
            if snorkel_id is None:
                lines.append("no snorkel mast found (mast types: %s)" % mast_types)
                return lines
            enum_val = _mast_enum("retracted")
            r = _try(lambda sid=snorkel_id, ev=enum_val: comp.SetMast(sid, ev))
            lines.append("snorkel retract (SetMast(%s, %s)): %s" % (
                snorkel_id, enum_val, "ok" if r[0] == "ok" else r[1]))
            return lines
        lines.append("unknown sub-command: %s" % sub)
        lines.append("usage: %s" % self.MASTS_USAGE)
        return lines

    # ------------------------------------------------------------------
    # explore: full internal structure dump (one-shot)
    # ------------------------------------------------------------------

    _KNOWN_ACCESS_TYPES = [
        "Navigation", "SteeringDiving", "Integrity",
        "AmmunitionStorage", "Maneuvering", "Coxswain",
        "FireControl", "TowedController", "MastsController",
        "Snorkel", "DepthGauge", "OpticalSystem",
        "ActiveSonar", "PassiveSonar", "fbw",
        "Hydrodynamics", "SonarSystem", "EnvironmentalSystem",
        "ESMSystem", "RadarSystem", "Hydrostatics",
        "HydrophoneArray", "SonarAudio", "SonarBearing",
        "SonarDisplay", "AudioMixer", "AudioSystem",
        "SonarHeadphone", "TowedArray", "SonarController",
        "SonarProcessing", "SonarManager", "ActivePassive",
        "Rigging", "RiggingManager", "AlarmManager",
    ]

    def _explore_obj(self, obj, max_depth=2, depth=0, path=""):
        """Recursively dir() an object and return {properties, callables, children}."""
        result = {"properties": [], "callables": []}
        try:
            attrs = sorted(dir(obj))
        except Exception as e:
            result["error"] = _desc(e, 80)
            return result
        for attr in attrs:
            if attr.startswith("_"):
                continue
            rv = _try(lambda a=attr, o=obj: getattr(o, a))
            if rv[0] != "ok":
                continue
            v = rv[1]
            if callable(v) and not isinstance(v, (int, float, str, bool)):
                result["callables"].append(attr)
            else:
                entry = {"name": attr, "val": _desc(v, 200)}
                # recurse into child objects
                if (depth < max_depth and v is not None
                        and not isinstance(v, (int, float, str, bool, type))
                        and not callable(v)):
                    try:
                        child_attrs = [a for a in dir(v) if not a.startswith("_")]
                    except Exception:
                        child_attrs = []
                    if 3 < len(child_attrs) < 150:
                        child = self._explore_obj(v, max_depth - 1, depth + 1,
                                                  "%s.%s" % (path, attr))
                        if child.get("properties"):
                            entry["child"] = child
                result["properties"].append(entry)
        return result

    def do_explore(self, cmd):
        """Full internal structure dump — writes ship_explore.json locally.

        Freeze-safe: only property reads and dir(), no method calls that
        touch Unity main thread."""
        out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "summary": {}}
        lines = ["explore: starting full dump..."]

        # --- 1) Player element dir() ---
        info = self.player_info()
        if info is not None:
            elem = _try(lambda: info.Element)
            if elem[0] == "ok" and elem[1] is not None:
                out["player_element"] = self._explore_obj(elem[1], max_depth=1)
                n = len(out["player_element"].get("properties", []))
                nc = len(out["player_element"].get("callables", []))
                lines.append("player_element: %d props, %d callables" % (n, nc))

        # --- 2) All Access[T] components ---
        ctrl = self.player_controller()
        out["access"] = {}
        scanned = 0
        if ctrl is not None:
            for tname in self._KNOWN_ACCESS_TYPES:
                t = self.g(tname)
                if t is None:
                    continue
                r = _try(lambda t=t: ctrl.Access[t]())
                if r[0] != "ok" or r[1] is None:
                    continue
                obj = r[1]
                data = self._explore_obj(obj, max_depth=1)
                out["access"][tname] = data
                scanned += 1
            lines.append("access: %d types resolved" % scanned)

        # --- 3) Blackboard ---
        bb = self._blackboard_storage()
        if bb is not None:
            bb_data = {"keys": [], "values": {}}
            keys_r = _try(lambda: list(bb.Keys))
            if keys_r[0] == "ok":
                bb_data["keys"] = [str(k) for k in keys_r[1]]
                for k in keys_r[1]:
                    vr = _try(lambda k=k: bb[k])
                    if vr[0] == "ok":
                        bb_data["values"][str(k)] = _desc(vr[1], 200)
            out["blackboard"] = bb_data
            lines.append("blackboard: %d keys" % len(bb_data["keys"]))

        # --- 4) SonarSystem deep dive ---
        ss = self._player_sonar_system()
        if ss is not None:
            ss_data = self._explore_obj(ss, max_depth=1)
            # Sonars items
            sonars_r = _try(lambda: ss.Sonars)
            ss_data["sonars"] = []
            if sonars_r[0] == "ok" and sonars_r[1] is not None:
                try:
                    items = list(sonars_r[1])
                except Exception:
                    items = []
                ss_data["sonars_detail"] = []
                for si, s in enumerate(items[:10]):
                    sname = _try(lambda s=s: str(getattr(s, "name",
                                  getattr(s, "Name", "?"))))
                    ss_data["sonars"].append(
                        "[%d] %s" % (si, sname[1] if sname[0] == "ok" else "?"))
                    # Safe property reads only — no dir(), no method calls
                    sdata = {"index": si, "name": sname[1] if sname[0] == "ok" else "?"}
                    for attr in ("ID", "BeamType", "SensorHeading", "Toggle",
                                 "DesignFrequency", "AoV", "Length"):
                        rv = _try(lambda a=attr, s=s: getattr(s, a))
                        if rv[0] == "ok" and not callable(rv[1]):
                            v = rv[1]
                            if hasattr(v, "x") and hasattr(v, "y"):
                                sdata[attr] = "(%.1f, %.1f)" % (v.x, v.y)
                            else:
                                sdata[attr] = _desc(v, 100)
                    ss_data["sonars_detail"].append(sdata)
            out["sonar_system"] = ss_data
            lines.append("sonar_system: %d props, %d sonars" % (
                len(ss_data.get("properties", [])),
                len(ss_data.get("sonars", []))))

        # --- 4a) ScopeSelector deep dive (headphone/bearing candidate) ---
        ss2 = self._player_sonar_system()
        if ss2 is not None:
            ss_r = _try(lambda: ss2.ScopeSelector)
            if ss_r[0] == "ok" and ss_r[1] is not None:
                sel = ss_r[1]
                # Safe: just callables list, no dir() on Unity object
                calls_r = _try(lambda: [m for m in dir(sel) if not m.startswith("_") and callable(getattr(sel, m))])
                sel_data = {"callables": calls_r[1] if calls_r[0] == "ok" else [],
                           "GetType": _desc(_try(lambda: sel.GetType())[1], 100)
                           if _try(lambda: sel.GetType())[0] == "ok" else "?"}
                out["scope_selector"] = sel_data
                lines.append("scope_selector: %d callables" % len(sel_data["callables"]))

        # SonarSystem — safe reads only
        ss3 = self._player_sonar_system()
        if ss3 is not None:
            # SortByBearing (returns contact IDs sorted by bearing)
            sbb = _try(lambda: ss3.SortByBearing())
            if sbb[0] == "ok" and sbb[1] is not None:
                try:
                    items = list(sbb[1])[:10]
                    ss_data["sort_by_bearing"] = [_desc(x, 50) for x in items]
                except Exception:
                    pass

        # --- 4b) Hydrostatics SoundSources (mnw.Audio.SoundSource objects) ---
        hs_type = self.g("Hydrostatics")
        if hs_type is not None and ctrl is not None:
            r = _try(lambda: ctrl.Access[hs_type]())
            if r[0] == "ok" and r[1] is not None:
                hs_obj = r[1]
                ss_r = _try(lambda: hs_obj.SoundSources)
                if ss_r[0] == "ok" and ss_r[1] is not None:
                    try:
                        src_items = list(ss_r[1])
                    except Exception:
                        src_items = []
                    hs_sound = []
                    for si, src in enumerate(src_items):
                        if src is None:
                            continue
                        sdata = self._explore_obj(src, max_depth=1)
                        sdata["index"] = si
                        # Try GetOutputData
                        god = _try(lambda src=src: src.GetOutputData())
                        if god[0] == "ok" and god[1] is not None:
                            sdata["GetOutputData"] = _desc(god[1], 300)
                        hs_sound.append(sdata)
                    out["hydrostatics_soundsources"] = hs_sound
                    lines.append("hydrostatics_soundsources: %d items" % len(hs_sound))

        # --- 4c) Tracker dump with bearings (find contact at ~5°) ---
        ss4 = self._player_sonar_system()
        if ss4 is not None:
            tcids_r = _try(lambda: list(ss4.GetContactIDs()))
            if tcids_r[0] == "ok" and tcids_r[1]:
                tracker_out = []
                for cid in tcids_r[1][:20]:
                    td = _try(lambda cid=cid: ss4.GetTrackerData(cid))
                    if td[0] == "ok" and td[1] is not None:
                        t = td[1]
                        entry = {"id": cid}
                        for a in ("_Bearing", "_Range", "_SensorID", "_TrackID",
                                  "_ClassifiedType", "_LastBearing", "_SignalLevel"):
                            rv = _try(lambda a=a, t=t: getattr(t, a))
                            if rv[0] == "ok" and rv[1] is not None:
                                entry[a.lstrip("_")] = _desc(rv[1], 100)
                        tracker_out.append(entry)
                out["tracker_dump"] = tracker_out
                lines.append("tracker_dump: %d contacts" % len(tracker_out))

        # --- 5) Summary ---
        total_props = len(out.get("player_element", {}).get("properties", []))
        total_callables = len(out.get("player_element", {}).get("callables", []))
        for tname, td in out.get("access", {}).items():
            if isinstance(td, dict):
                total_props += len(td.get("properties", []))
                total_callables += len(td.get("callables", []))
        out["summary"] = {
            "access_types": scanned,
            "player_props": total_props,
            "player_callables": total_callables,
            "blackboard_keys": len(out.get("blackboard", {}).get("keys", [])),
            "sonar_sonars": len(out.get("sonar_system", {}).get("sonars", [])),
            "sonar_cached": len(out.get("sonar_system", {}).get("cached_contacts", [])),
        }
        lines.append("summary: %d access types, %d props, %d callables, %d bb keys" % (
            scanned, total_props, total_callables, out["summary"]["blackboard_keys"]))

        # --- 6) Write to file ---
        try:
            path = os.path.join(self.log_dir, "ship_explore.json")
            self._atomic_write("ship_explore.json", out)
            lines.append("written: %s" % path)
        except Exception as e:
            lines.append("write error: %s" % _desc(e, 80))

        lines.append("explore: done")
        return lines

    SONCTL_USAGE = (
        "sonctl auto on|off              enable/disable auto-tracking\n"
        "sonctl ids                      list tracked contact IDs\n"
        "sonctl track CONTACT_ID         start tracking a contact\n"
        "sonctl untrack GUID TYPE        stop tracking (TYPE: 0=Visual..7=ManualSonar)\n"
        "sonctl data CONTACT_ID          tracker data (bearing, range, rate)\n"
        "sonctl mark CONTACT_ID BEARING  manual sonar mark\n"
        "sonctl diag                     dump all SonarSystem + tracker fields\n"
        "sonctl explore [all|brute|bb]   full sonar/audio/bearing exploration dump\n"
    )
    # TrackerManager type names ordered by index (from PopulateTrackerManagers IL)
    SONCTL_TM_NAMES = ("Visual", "Radar", "ESM", "Radio",
                       "Weapon", "AIS", "ActiveIntercept", "ManualSonar")

    def do_sonctl(self, cmd):
        """SonarSystem tracker control (control command).

        Sub-commands:
          sonctl auto on|off        enable/disable SetAutoTrackState
          sonctl ids                list tracked contact IDs via GetContactIDs
          sonctl track CONTACT_ID   start tracking via TrackContact
          sonctl untrack GUID TYPE  stop tracking via UntrackContact
          sonctl data CONTACT_ID    tracker data via GetTrackerData
          sonctl mark CONTACT_ID B  manual sonar mark via ManualMark(cid, bearing, DateTime)
          sonctl explore [target]   full sonar/audio/bearing exploration dump

        Freeze-safe: all calls are property reads, dict lookups, or simple
        field writes — no Unity main-thread calls."""
        ss = self._player_sonar_system()
        if ss is None:
            return ["sonctl: no player SonarSystem"]
        sub = str(cmd.get("sub") or "").lower()
        if not sub:
            return ["sonctl: " + self.SONCTL_USAGE.replace("\n", "\n  ")]
        lines = ["sonctl %s:" % sub]
        if sub == "auto":
            val = str(cmd.get("val") or "").lower()
            if val not in ("on", "off"):
                return ["sonctl auto: need on|off"]
            flag = val == "on"
            r = _try(lambda: ss.SetAutoTrackState(flag))
            lines.append("SetAutoTrackState(%s): %s" % (flag, "ok" if r[0] == "ok" else r[1]))
        elif sub == "ids":
            r = _try(lambda: ss.GetContactIDs())
            if r[0] != "ok":
                lines.append("GetContactIDs: %s" % r[1])
            else:
                ids = r[1]
                if ids is None:
                    lines.append("GetContactIDs: None")
                else:
                    try:
                        count = len(ids)
                    except Exception:
                        count = "?"
                    lines.append("GetContactIDs: %s items" % count)
                    try:
                        for item in ids:
                            try:
                                cid = item.Item1 if hasattr(item, "Item1") else item
                                desc = item.Item2 if hasattr(item, "Item2") else ""
                                lines.append("  id=%s desc=%s" % (cid, str(desc)[:40]))
                            except Exception as e:
                                lines.append("  item: %s" % _desc(e, 60))
                    except Exception:
                        pass
        elif sub == "track":
            cid = str(cmd.get("cid") or "").strip()
            if not cid:
                return ["sonctl track: need CONTACT_ID"]
            # TrackContact overloads — do NOT access .Overloads (FREEZE!)
            # Just try both combos silently
            r = _try(lambda: ss.TrackContact(cid, -1))
            if r[0] == "ok":
                lines.append("TrackContact(str, int): ok")
            else:
                r2 = _try(lambda: ss.TrackContact(int(cid), -1))
                if r2[0] == "ok":
                    lines.append("TrackContact(int, int): ok")
                else:
                    lines.append("TrackContact: both overloads failed: %s / %s" % (r[1], r2[1]))
        elif sub == "untrack":
            guid = str(cmd.get("guid") or "").strip()
            ttype = int(cmd.get("type") or -1)
            if not guid or ttype < 0:
                return ["sonctl untrack: need GUID and TYPE (0=Visual..7=ManualSonar)"]
            r = _try(lambda: ss.UntrackContact(guid, ttype))
            val = r[1] if r[0] == "ok" else r[1]
            lines.append("UntrackContact(%s, %d): %s" % (guid, ttype, val))
        elif sub == "data":
            cid = str(cmd.get("cid") or "").strip()
            if not cid:
                return ["sonctl data: need CONTACT_ID"]
            r = _try(lambda: ss.GetTrackerData(int(cid)))
            if r[0] != "ok":
                lines.append("GetTrackerData(%s): %s" % (cid, r[1]))
            else:
                data = r[1]
                if data is None:
                    lines.append("GetTrackerData(%s): None" % cid)
                else:
                    try:
                        count = len(data)
                    except Exception:
                        count = "?"
                    lines.append("GetTrackerData(%s): %s items" % (cid, count))
                    try:
                        for td in data:
                            parts = []
                            for attr in ("_TrackID", "_SensorID", "_Bearing",
                                         "_BearingRate", "_Range"):
                                try:
                                    parts.append("%s=%s" % (attr.lstrip("_"),
                                                getattr(td, attr)))
                                except Exception:
                                    pass
                            lines.append("  %s" % ", ".join(parts))
                    except Exception:
                        try:
                            lines.append("  raw: %s" % _desc(data, 120))
                        except Exception:
                            pass
        elif sub == "mark":
            cid = str(cmd.get("cid") or "").strip()
            bearing = cmd.get("bearing")
            if not cid or bearing is None:
                return ["sonctl mark: need CONTACT_ID BEARING"]
            try:
                bearing = float(bearing)
            except (ValueError, TypeError):
                return ["sonctl mark: BEARING must be a number"]
            # ManualMark(cid, bearing, DateTime) — use current time
            import System
            now = System.DateTime.Now
            r = _try(lambda: ss.ManualMark(int(cid), bearing, now))
            lines.append("ManualMark(%s, %s, DateTime.Now): %s" % (cid, bearing,
                        "ok" if r[0] == "ok" else r[1]))
        elif sub == "diag":
            lines.append("sonctl diag: SonarSystem fields")
            for attr in sorted(dir(ss)):
                if attr.startswith("_"):
                    continue
                rv = _try(lambda a=attr: getattr(ss, a))
                if rv[0] == "ok":
                    v = rv[1]
                    if callable(v) and not isinstance(v, (int, float, str, bool)):
                        lines.append("  %s: <callable>" % attr)
                    else:
                        lines.append("  %s: %s" % (attr, _desc(v, 100)))
            cc = _try(lambda: ss.CachedContacts)
            if cc[0] == "ok" and cc[1]:
                try:
                    keys = list(cc[1].Keys)
                except Exception:
                    keys = []
                lines.append("  -- CachedContacts: %d items --" % len(keys))
                for k in keys:
                    ic = _try(lambda k=k: cc[1][k])
                    if ic[0] == "ok" and ic[1] is not None:
                        obj = ic[1]
                        tname = _try(lambda: type(obj).__name__)
                        lines.append("    IContact key=%s type=%s:" % (k, tname[1] if tname[0] == "ok" else "?"))
                        dump_obj = obj
                        for a in sorted(dir(dump_obj)):
                            if a.startswith("_"):
                                continue
                            rv2 = _try(lambda a=a, o=dump_obj: getattr(o, a))
                            if rv2[0] == "ok":
                                v2 = rv2[1]
                                if callable(v2):
                                    continue
                                lines.append("      %s: %s" % (a, _desc(v2, 80)))
                        for guess in ("Comment", "Label", "Note", "Description",
                                       "Tag", "Name", "UserComment", "Text",
                                       "Annotation", "Remark", "Callsign",
                                       "TypeComment", "ContactComment"):
                            rv3 = _try(lambda g=guess, o=obj: getattr(o, g))
                            if rv3[0] == "ok":
                                lines.append("      ** %s: %s **" % (guess, _desc(rv3[1], 80)))
            r = _try(lambda: ss.GetContactIDs())
            if r[0] == "ok" and r[1]:
                for item in r[1][:3]:
                    try:
                        cid = item.Item1 if hasattr(item, "Item1") else item
                    except Exception:
                        cid = item
                    lines.append("  -- contact %s dir() --" % cid)
                    td = _try(lambda cid=cid: ss.GetTrackerData(cid))
                    if td[0] == "ok" and td[1] is not None:
                        try:
                            items = list(td[1])
                        except Exception:
                            items = [td[1]]
                        for ti, tditem in enumerate(items[:2]):
                            lines.append("    tracker item %d:" % ti)
                            for a in sorted(dir(tditem)):
                                if a.startswith("_"):
                                    continue
                                rv2 = _try(lambda a=a, tditem=tditem: getattr(tditem, a))
                                if rv2[0] == "ok":
                                    v = rv2[1]
                                    if not callable(v):
                                        lines.append("      %s: %s" % (a, _desc(v, 80)))
        elif sub == "explore":
            target = str(cmd.get("target") or "all").lower()
            lines.append("=== SONAR FULL EXPLORATION (one-shot dump) ===")
            hits = []

            def _deep_dir(obj, label, depth=0, max_depth=2):
                prefix = "  " * depth
                lines.append("%s-- %s dir() --" % (prefix, label))
                try:
                    attrs = sorted(dir(obj))
                except Exception as e:
                    lines.append("%s  dir() failed: %s" % (prefix, _desc(e, 60)))
                    return
                for attr in attrs:
                    if attr.startswith("_"):
                        continue
                    rv = _try(lambda a=attr, o=obj: getattr(o, a))
                    if rv[0] != "ok":
                        continue
                    v = rv[1]
                    if callable(v) and not isinstance(v, (int, float, str, bool)):
                        lines.append("%s  %s: <callable>" % (prefix, attr))
                    else:
                        lines.append("%s  %s: %s" % (prefix, attr, _desc(v, 120)))
                        al = attr.lower()
                        if any(kw in al for kw in ("bearing", "headphone", "listen", "select",
                                                    "audio", "mixer", "sample", "volume",
                                                    "frequency", "ping", "active_bearing")):
                            hits.append((label, attr, v))
                # recurse into non-callable non-primitive children
                if depth < max_depth:
                    for attr in attrs:
                        if attr.startswith("_"):
                            continue
                        rv = _try(lambda a=attr, o=obj: getattr(o, a))
                        if rv[0] != "ok":
                            continue
                        v = rv[1]
                        if v is None or isinstance(v, (int, float, str, bool, type)):
                            continue
                        if callable(v):
                            continue
                        try:
                            child_attrs = [a for a in dir(v) if not a.startswith("_")]
                        except Exception:
                            continue
                        if len(child_attrs) > 3:
                            _deep_dir(v, "%s.%s" % (label, attr), depth + 1, max_depth)

            # --- 1) SonarSystem full dir ---
            _deep_dir(ss, "SonarSystem")

            # --- 2) Sonars items: full dir on each ---
            sonars_r = _try(lambda: ss.Sonars)
            if sonars_r[0] == "ok" and sonars_r[1] is not None:
                try:
                    items = list(sonars_r[1])
                    lines.append("-- Sonars: %d items --" % len(items))
                    for si, s in enumerate(items[:8]):
                        sname = _try(lambda s=s: str(getattr(s, "name", "?")))
                        stype = _try(lambda s=s: str(getattr(s, "sensor_type", "?")))
                        lines.append("  [%d] name=%s type=%s" % (
                            si, sname[1] if sname[0] == "ok" else "?",
                            stype[1] if stype[0] == "ok" else "?"))
                        _deep_dir(s, "Sonars[%d]" % si, depth=1, max_depth=1)
                except Exception as e:
                    lines.append("  Sonars error: %s" % _desc(e, 60))

            # --- 3) CachedContacts: full dir on each contact ---
            cc = _try(lambda: ss.CachedContacts)
            if cc[0] == "ok" and cc[1] is not None:
                try:
                    keys = list(cc[1].Keys)
                    lines.append("-- CachedContacts: %d items --" % len(keys))
                    for k in keys[:5]:
                        ic = _try(lambda k=k: cc[1][k])
                        if ic[0] == "ok" and ic[1] is not None:
                            _deep_dir(ic[1], "CachedContact[%s]" % k, depth=1, max_depth=1)
                except Exception as e:
                    lines.append("  CachedContacts error: %s" % _desc(e, 60))

            # --- 4) Access[T] components: PassiveSonar, ActiveSonar, + sonar-related ---
            ctrl = self.player_controller()
            if ctrl is not None:
                lines.append("-- Access[T] sonar/audio components --")
                for tname in ("PassiveSonar", "ActiveSonar", "HydrophoneArray",
                              "SonarAudio", "SonarBearing", "SonarDisplay",
                              "AudioMixer", "AudioSystem", "SonarHeadphone",
                              "ActivePassive", "TowedArray", "TowedController",
                              "SonarController", "SonarProcessing"):
                    t = self.g(tname)
                    if t is not None:
                        r = _try(lambda t=t: ctrl.Access[t]())
                        if r[0] == "ok" and r[1] is not None:
                            _deep_dir(r[1], "Access[%s]" % tname, max_depth=1)
                        else:
                            lines.append("  Access[%s]: not available (%s)" % (tname, r[1]))
                    else:
                        lines.append("  Access[%s]: type not in registry" % tname)

            # --- 5) ALL known Access[T] types: scan for bearing/audio in dir() ---
            if target in ("all", "brute"):
                lines.append("-- Brute-force: ALL known Access[T] types dir() scan --")
                ALL_ACCESS_TYPES = [
                    "Navigation", "SteeringDiving", "Integrity",
                    "AmmunitionStorage", "Maneuvering", "Coxswain",
                    "FireControl", "TowedController", "MastsController",
                    "Snorkel", "DepthGauge", "OpticalSystem",
                    "ActiveSonar", "PassiveSonar", "fbw",
                    "Hydrodynamics", "SonarSystem", "EnvironmentalSystem",
                    "ESMSystem", "RadarSystem", "Hydrostatics",
                    # extra guesses for audio/bearing
                    "HydrophoneArray", "SonarAudio", "SonarBearing",
                    "SonarDisplay", "AudioMixer", "AudioSystem",
                    "SonarHeadphone", "TowedArray", "SonarController",
                    "SonarProcessing", "SonarManager", "ActivePassive",
                ]
                scanned = 0
                for tname in ALL_ACCESS_TYPES:
                    t = self.g(tname)
                    if t is None:
                        continue
                    r = _try(lambda t=t: ctrl.Access[t]())
                    if r[0] != "ok" or r[1] is None:
                        lines.append("  Access[%s]: not available (%s)" % (tname, r[1]))
                        continue
                    obj = r[1]
                    lines.append("  Access[%s]: OK" % tname)
                    scanned += 1
                    try:
                        attrs = dir(obj)
                    except Exception:
                        continue
                    for attr in attrs:
                        if attr.startswith("_"):
                            continue
                        al = attr.lower()
                        if any(kw in al for kw in ("bearing", "headphone", "listen",
                                                    "select", "audio", "mixer",
                                                    "sample", "volume", "frequency",
                                                    "ping", "hydro", "sensor")):
                            rv = _try(lambda a=attr, o=obj: getattr(o, a))
                            if rv[0] == "ok" and not callable(rv[1]):
                                lines.append("    >> %s.%s = %s" % (
                                    tname, attr, _desc(rv[1], 120)))
                                hits.append(("Access[%s]" % tname, attr, rv[1]))
                    # also full dir for sonar-related types
                    if tname in ("PassiveSonar", "ActiveSonar", "SonarSystem",
                                 "HydrophoneArray", "TowedArray", "TowedController"):
                        for attr in attrs:
                            if attr.startswith("_"):
                                continue
                            rv = _try(lambda a=attr, o=obj: getattr(o, a))
                            if rv[0] == "ok":
                                v = rv[1]
                                if not callable(v):
                                    lines.append("    %s.%s = %s" % (
                                        tname, attr, _desc(v, 100)))
                lines.append("  (scanned %d Access[T] types)" % scanned)

            # --- 6) Blackboard: ALL keys ---
            if target in ("all", "bb", "blackboard"):
                lines.append("-- Blackboard ALL keys --")
                bb = self._blackboard_storage()
                if bb is not None:
                    keys_r = _try(lambda: list(bb.Keys))
                    if keys_r[0] == "ok":
                        for k in keys_r[1]:
                            kl = str(k).lower()
                            vr = _try(lambda k=k: bb[k])
                            if vr[0] == "ok":
                                tag = ""
                                if any(kw in kl for kw in ("bearing", "sonar", "audio",
                                                           "headphone", "listen", "scan",
                                                           "ping", "hydro", "mixer")):
                                    tag = " >>"
                                lines.append("  %s: %s%s" % (k, _desc(vr[1], 100), tag))

            # --- 7) Summary ---
            lines.append("=== EXPLORATION SUMMARY ===")
            if hits:
                lines.append("Found %d bearing/audio/headphone candidates:" % len(hits))
                for src, attr, val in hits:
                    lines.append("  %s.%s = %s" % (src, attr, _desc(val, 120)))
            else:
                lines.append("No bearing/audio/headphone props found.")
                lines.append("Look through the dump above for any clues.")
        else:
            lines.append("unknown sub: %s" % sub)
            lines.append(self.SONCTL_USAGE)
        return lines

    # TrackerManager type→index mapping (from PopulateTrackerManagers IL)
    TRACKER_TYPES = {
        "visual": 0, "radar": 1, "esm": 2, "radio": 3,
        "weapon": 4, "ais": 5, "active": 6, "manual": 7,
    }
    TRACKER_NAMES = {v: k for k, v in TRACKER_TYPES.items()}
    TRACKER_TM_ATTRS = ("VisualTrackerManager", "RadarTrackerManager", "ESMTrackerManager",
                        "RadioTrackerManager", "WeaponTrackerManager", "AISTrackerManager",
                        "ActiveInterceptTrackerManager", "ManualSonarTrackerManager")

    TRACKER_USAGE = (
        "tracker                        summary of all TrackerManagers\n"
        "tracker TYPE                   detailed view (radar|esm|visual|radio|...)\n"
        "tracker TYPE ID                probe all getters for track id\n"
        "tracker TYPE clear             clear all tracks\n"
        "tracker TYPE clearid ID        remove one track\n"
        "tracker TYPE loadsnap STR      LoadSnapshot test\n"
        "tracker TYPE tkdump ID         dump GetTrack(cid) object fields\n"
        "tracker new TYPE ID            create a track via TrackerManager.New\n"
        "  TYPE = visual|radar|esm|radio|weapon|ais|active|manual (or 0-7)\n"
    )

    def _resolve_tracker_type(self, name):
        """Return (index, attr_name) for a tracker type name, number or None."""
        name = name.lower().strip()
        if name in self.TRACKER_TYPES:
            idx = self.TRACKER_TYPES[name]
            return idx, self.TRACKER_TM_ATTRS[idx]
        if name.isdigit():
            idx = int(name)
            if idx in self.TRACKER_NAMES:
                return idx, self.TRACKER_TM_ATTRS[idx]
        # Sonar prefix has no FireControl TrackerManager (SonarSystem tracker instead)
        if name == "sonar" or name == "8":
            return 8, "SonarSystem"
        # also accept the attribute name directly (e.g. "ActiveIntercept")
        for i, attr in enumerate(self.TRACKER_TM_ATTRS):
            if name == attr.lower():
                return i, attr
        return None

    @staticmethod
    def _prefix_to_idx(pv, prefix_map):
        """Map a GetPrefix() result to a tracker index.

        String match FIRST (C# enum names like 'mnw.Core.ContactTools+
        ContactType.Sonar'), int() only as fallback. Reason: int() on an
        IronPython enum can return the enum's underlying value mid-range
        (live 2026-08-18: ContactType.Sonar int-cast matched tracker index 1
        = Radar), so Sonar contacts were miscounted as radar contacts."""
        if pv is None:
            return None
        try:
            ps = str(pv)
        except Exception:
            ps = ""
        for ename, eidx in prefix_map.items():
            if ename.lower() in ps.lower():
                return eidx
        try:
            return int(pv)
        except (TypeError, ValueError, OverflowError):
            return None

    def do_tracker(self, cmd):
        """FireControl TrackerManager probe + contact filtering (control command).

        Sub-commands:
          tracker              summary of all 8 TrackerManagers (type, range, cycle)
          tracker TYPE         detailed view: TrackerManager properties + contacts
                               filtered by sensor prefix (GetPrefix on ContactManager)
          tracker TYPE ID      probe all TrackerManager getters for a track id
          tracker TYPE clear   clear all tracks on this TrackerManager
          tracker TYPE clearid ID  remove one track
          tracker TYPE loadsnap STR  LoadSnapshot test

        Freeze-safe: property reads + GetUsed/GetPrefix/GetTrack on
        ContactManager (same pattern as read_contacts). Max 12 getter calls
        on a track id (single track probe, no loops)."""
        sub = str(cmd.get("sub") or "").lower()
        lines = ["tracker:"]
        # Map ContactType enum names to tracker indices (Sonar=8 is SonarSystem, no FireControl manager)
        _PREFIX_MAP = {
            "Visual": 0, "Radar": 1, "ESM": 2, "Radio": 3,
            "Weapon": 4, "AIS": 5, "ActiveIntercept": 6, "ManualSonar": 7,
            "Sonar": 8,
        }
        # resolve FireControl
        st, fc = self._component("FireControl", owner="player")
        if st != "ok" or fc is None:
            st2, fc = self._component("FireControl")
            st = st2
        if st != "ok" or fc is None or not hasattr(fc, "ContactManager"):
            lines.append("no FireControl")
            return lines
        # get ContactManager
        r = _try(lambda: fc.ContactManager)
        fcm = r[1] if r[0] == "ok" else None
        if fcm is None:
            lines.append("no ContactManager")
            return lines
        # get ContactManager.GetUsed for contact enumeration
        used_r = _try(lambda: fcm.GetUsed)
        used = used_r[1] if used_r[0] == "ok" else None
        # summary: read all TrackerManagers
        if not sub:
            lines.append("TrackerManagers (player FireControl):")
            for i, attr in enumerate(self.TRACKER_TM_ATTRS):
                label = self.TRACKER_NAMES.get(i, attr)
                r = _try(lambda attr=attr: getattr(fc, attr))
                if r[0] != "ok" or r[1] is None:
                    lines.append("  %d %-16s not present" % (i, label))
                    continue
                tm = r[1]
                props = {}
                for pname in ("Cycle", "Range", "ControllerID"):
                    pr = _try(lambda pname=pname: getattr(tm, pname))
                    if pr[0] == "ok":
                        props[pname] = _safe_num(pr[1]) if isinstance(pr[1], (int, float)) else _desc(pr[1], 30)
                # count contacts with matching prefix
                count = 0
                if used is not None:
                    for cid in used:
                        pfx = _try(lambda cid=cid: fcm.GetPrefix(cid))
                        if pfx[0] != "ok":
                            continue
                        pfx_idx = self._prefix_to_idx(pfx[1], _PREFIX_MAP)
                        if pfx_idx is not None and pfx_idx == i:
                            count += 1
                lines.append("  %d %-16s cycle=%-6s range=%-10s contacts=%d" % (
                    i, label,
                    props.get("Cycle", "?"),
                    props.get("Range", "?"),
                    count))
            # sonar contacts (prefix 8, SonarSystem tracker - no FireControl manager)
            sonar_count = 0
            if used is not None:
                for cid in used:
                    pfx = _try(lambda cid=cid: fcm.GetPrefix(cid))
                    if pfx[0] != "ok":
                        continue
                    if self._prefix_to_idx(pfx[1], _PREFIX_MAP) == 8:
                        sonar_count += 1
            lines.append("  8 sonar           cycle=-      range=??         contacts=%d  (SonarSystem tracker)" % sonar_count)
            return lines
        # detailed view for one type
        if sub == "raw":
            lines.append("raw ContactManager diagnostics:")
            lines.append("  fc resolve: player-ok=%s host-ok=%s" % (
                self._component("FireControl", owner="player")[0],
                self._component("FireControl")[0]))
            for cname in ("GetUsed", "GetUsedCount", "Count", "GetContactIDs", "GetID"):
                c = _try(lambda cname=cname: getattr(fcm, cname))
                if c[0] != "ok":
                    continue
                v = c[1]
                if callable(v):
                    for argname, arg in (("()", None),):
                        cv = _try(lambda arg=arg: v() if arg is None else v(arg))
                        if cv[0] == "ok":
                            lines.append("  fcm.%s() -> %s" % (cname, _desc(cv[1], 200)))
                        else:
                            lines.append("  fcm.%s() -> ERR %s" % (cname, str(cv[1])[:80]))
                else:
                    lines.append("  fcm.%s -> %s" % (cname, _desc(v, 200)))
            if used is not None:
                lines.append("  GetUsed() -> %d ids" % len(list(used)))
                for cid in list(used)[:24]:
                    pfx = _try(lambda cid=cid: fcm.GetPrefix(cid))
                    lines.append("    id=%r prefix=%s" % (
                        _desc(cid, 40), _desc(pfx[1], 60) if pfx[0] == "ok" else pfx[1]))
            else:
                lines.append("  GetUsed() -> unavailable")
            return lines
        resolved = self._resolve_tracker_type(sub)
        if resolved is None:
            lines.append("unknown type: %s" % sub)
            lines.append(self.TRACKER_USAGE)
            return lines
        idx, attr = resolved
        label = self.TRACKER_NAMES.get(idx, attr)
        lines.append("type %d = %s:" % (idx, label))
        # SonarSystem-tracker contacts (prefix 8) - no FireControl TrackerManager
        if idx == 8:
            if used is None:
                lines.append("  contacts: GetUsed unavailable")
                return lines
            matching = []
            maxc = int(self.cfg.get("max_contacts", 50))
            for cid in used:
                if len(matching) >= maxc:
                    break
                pfx = _try(lambda cid=cid: fcm.GetPrefix(cid))
                if pfx[0] != "ok":
                    continue
                if self._prefix_to_idx(pfx[1], _PREFIX_MAP) != 8:
                    continue
                t = {"id": _desc(cid, 40)}
                cat = _try(lambda cid=cid: fcm.GetCategoryID(cid))
                if cat[0] == "ok":
                    t["cat"] = _desc(cat[1], 20)
                ident = _try(lambda cid=cid: fcm.GetStandardIdentity(cid))
                if ident[0] == "ok":
                    t["ident"] = _desc(ident[1], 20)
                tr = _try(lambda cid=cid: fcm.GetTrack(cid))
                if tr[0] == "ok":
                    tk = tr[1]
                    for tname, attr2 in (("rng", "_Range"), ("brg", "_Bearing"),
                                         ("spd", "_Speed"), ("crs", "_Course"),
                                         ("br", "_BearingRate"), ("elev", "_Elevation")):
                        rr = _try(lambda attr2=attr2: getattr(tk, attr2))
                        if rr[0] == "ok":
                            v = rr[1]
                            if attr2 in ("_Bearing",) and v is not None:
                                try:
                                    v = [_safe_num(v.Item1), _safe_num(v.Item2)]
                                except Exception:
                                    v = _safe_num(v)
                            else:
                                v = _safe_num(v) if isinstance(v, (int, float)) else v
                            t[tname] = v
                matching.append(t)
            lines.append("  %d sonar contacts:" % len(matching))
            for t in matching:
                parts = ["id=%s" % t.get("id", "?")]
                for k in ("cat", "ident", "rng", "brg", "spd", "crs", "br", "elev"):
                    if k in t:
                        parts.append("%s=%s" % (k, t[k]))
                lines.append("    %s" % " ".join(parts))
            return lines
        # TrackerManager properties
        r = _try(lambda: getattr(fc, attr))
        if r[0] != "ok" or r[1] is None:
            lines.append("  not present")
            return lines
        tm = r[1]
        for pname in ("Cycle", "Range", "ControllerID", "SMRI"):
            pr = _try(lambda pname=pname: getattr(tm, pname))
            if pr[0] == "ok":
                lines.append("  %s = %s" % (pname, _desc(pr[1], 40)))
        # public methods
        try:
            mnames = sorted(m for m in dir(tm) if not m.startswith("_") and callable(getattr(tm, m, None)))
        except Exception:
            mnames = []
        if mnames:
            lines.append("  methods: %s" % ", ".join(mnames[:20]))
        # mode: clear / clearid / loadsnap / trackid probe
        mode = str(cmd.get("mode") or "").lower()
        trackid = str(cmd.get("trackid") or "").strip()
        if mode == "clear":
            cl0 = _try(lambda: tm.Clear())
            lines.append("  Clear(): %s" % ("ok" if cl0[0] == "ok" else cl0[1]))
            if cl0[0] != "ok":
                cl = _try(lambda: tm.Clear(0))
                lines.append("  Clear(0): %s" % ("ok" if cl[0] == "ok" else cl[1]))
            return lines
        if mode == "clearid":
            if not trackid:
                lines.append("  clearid: need TRACK_ID")
                return lines
            try:
                tidn = int(trackid)
            except (ValueError, TypeError):
                tidn = None
            if tidn is not None:
                c1 = _try(lambda: tm.ClearID(tidn))
                lines.append("  ClearID(%d): %s" % (tidn, "ok" if c1[0] == "ok" else c1[1]))
            else:
                c1 = _try(lambda: tm.ClearID(trackid))
                lines.append("  ClearID(%r): %s" % (trackid, "ok" if c1[0] == "ok" else c1[1]))
            return lines
        if mode == "loadsnap":
            snap = str(cmd.get("snap") or "").strip()
            if not snap:
                lines.append("  loadsnap: need SNAPSHOT string")
                return lines
            ls0 = _try(lambda: tm.LoadSnapshot(snap))
            lines.append("  LoadSnapshot(%r): %s" % (snap[:40], "ok" if ls0[0] == "ok" else ls0[1]))
            if ls0[0] != "ok":
                ls = _try(lambda: tm.LoadSnapshot(snap, ""))
                lines.append("  LoadSnapshot(%r, ''): %s" % (snap[:40], "ok" if ls[0] == "ok" else ls[1]))
            return lines
        if mode == "tkdump":
            if not trackid:
                lines.append("  tkdump: need TRACK_ID (ContactManager id)")
                return lines
            try:
                tidn = int(trackid)
            except (ValueError, TypeError):
                tidn = None
            tk = None
            miss = None
            if tidn is not None:
                gr = _try(lambda: fcm.GetTrack(tidn))
                if gr[0] == "ok":
                    tk = gr[1]
                else:
                    miss = gr[1]
            if tk is None:
                gr = _try(lambda: fcm.GetTrack(trackid))
                if gr[0] == "ok":
                    tk = gr[1]
                else:
                    miss = gr[1]
            if tk is None and used is not None:
                for cid in used:
                    gr = _try(lambda cid=cid: fcm.GetTrack(cid))
                    if gr[0] == "ok" and gr[1] is not None:
                        lines.append("  (id %r not found%s - using first used id %r)" % (
                            trackid, (": %s" % str(miss)[:60]) if miss is not None else "", _desc(cid, 40)))
                        tk = gr[1]
                    break
            lines.append("  track %s object:" % trackid)
            if tk is None:
                lines.append("    (none)")
                return lines
            gt = _try(lambda: tk.GetType())
            lines.append("    type: %s" % (_desc(gt[1]) if gt[0] == "ok" else gt[1]))
            names = []
            try:
                names = sorted(n for n in dir(tk) if not n.startswith("__"))
            except Exception as e:
                lines.append("    dir() failed: %s" % e)
            # classic fields first (single property read each)
            classic = [n for n in names if n.startswith("_")]
            simple = [n for n in names if not n.startswith("_") and not n[0].isupper()]
            for n in (classic[:24] + simple[:16]):
                vr = _try(lambda n=n: getattr(tk, n))
                if vr[0] != "ok":
                    lines.append("    .%s = ERR %s" % (n, str(vr[1])[:60]))
                    continue
                v = vr[1]
                if v is None:
                    lines.append("    .%s = None" % n)
                elif isinstance(v, (int, float, str, bool)):
                    lines.append("    .%s = %s" % (n, _safe_num(v) if isinstance(v, (int, float)) else v))
                else:
                    # try to unwrap numeric-ish objects (Single/structs), else type name
                    num = _try(lambda v=v: float(v))
                    if num[0] == "ok" and isinstance(num[1], float):
                        lines.append("    .%s = %s" % (n, num[1]))
                        continue
                    got = _try(lambda v=v: str(v))
                    if got[0] == "ok" and got[1] and got[1].startswith("("):
                        lines.append("    .%s = %s" % (n, got[1][:80]))
                        continue
                    lines.append("    .%s = <%s>" % (n, type(v).__name__))
            return lines
        if trackid:
            # probe all TrackerManager getters for this track id
            try:
                tidn = int(trackid)
            except (ValueError, TypeError):
                tidn = None
            lines.append("  track %s getters:" % trackid)
            getters = ("GetBearing", "GetRange", "GetSpeed", "GetCourse",
                       "GetBearingRate", "GetElevation", "GetCPA", "GetContactID",
                       "GetTimestamp", "GetRelativeBearing", "GetBC", "GetCoordinates")
            for gname in getters:
                g = _try(lambda gname=gname: getattr(tm, gname)(tidn if tidn is not None else trackid))
                if g[0] != "ok":
                    continue
                v = g[1]
                if v is None:
                    continue
                txt = _desc(v, 80)
                lines.append("    %s: %s" % (gname, txt))
            return lines
        # contacts filtered by prefix
        if used is None:
            lines.append("  contacts: GetUsed unavailable")
            return lines
        matching = []
        maxc = int(self.cfg.get("max_contacts", 50))
        for cid in used:
            if len(matching) >= maxc:
                break
            pfx = _try(lambda cid=cid: fcm.GetPrefix(cid))
            if pfx[0] != "ok":
                continue
            # String match first (enum name), int() only as fallback
            pfx_idx = self._prefix_to_idx(pfx[1], _PREFIX_MAP)
            if pfx_idx is None or pfx_idx != idx:
                continue
            t = {"id": _desc(cid, 40)}
            cat = _try(lambda cid=cid: fcm.GetCategoryID(cid))
            if cat[0] == "ok":
                t["cat"] = _desc(cat[1], 20)
            ident = _try(lambda cid=cid: fcm.GetStandardIdentity(cid))
            if ident[0] == "ok":
                t["ident"] = _desc(ident[1], 20)
            tr = _try(lambda cid=cid: fcm.GetTrack(cid))
            if tr[0] == "ok":
                tk = tr[1]
                for tname, attr2 in (("rng", "_Range"), ("brg", "_Bearing"),
                                     ("spd", "_Speed"), ("crs", "_Course"),
                                     ("br", "_BearingRate"), ("elev", "_Elevation")):
                    rr = _try(lambda attr2=attr2: getattr(tk, attr2))
                    if rr[0] == "ok":
                        v = rr[1]
                        if attr2 in ("_Bearing",) and v is not None:
                            try:
                                v = [_safe_num(v.Item1), _safe_num(v.Item2)]
                            except Exception:
                                v = _safe_num(v)
                        else:
                            v = _safe_num(v) if isinstance(v, (int, float)) else v
                        t[tname] = v
            # also try TrackerManager getter methods on the contact
            for mname in ("GetBearing", "GetRange", "GetSpeed", "GetCourse", "GetBearingRate"):
                mr = _try(lambda cid=cid, mname=mname: getattr(tm, mname)(cid))
                if mr[0] == "ok" and mr[1] is not None:
                    key = mname.lower().replace("get", "")
                    if key not in t:
                        t[key] = _safe_num(mr[1]) if isinstance(mr[1], (int, float)) else _desc(mr[1], 30)
            matching.append(t)
        lines.append("  %d contacts (prefix=%d):" % (len(matching), idx))
        for t in matching:
            parts = ["id=%s" % t.get("id", "?")]
            for k in ("cat", "ident", "rng", "brg", "spd", "crs", "br", "elev"):
                if k in t:
                    parts.append("%s=%s" % (k, t[k]))
            lines.append("    %s" % " ".join(parts))
        return lines

    def do_tracker_new(self, cmd):
        """Manually create a track on a FireControl TrackerManager via New().

        usage: tracker new TYPE ID  (TYPE: esm|radio|radar|..., ID: track id)

        Freeze-safe: max 3 _try calls (int, str, int+int variants)."""
        lines = ["tracker new:"]
        sub = str(cmd.get("type") or "").lower()
        tid = str(cmd.get("id") or "").strip()
        if not sub or not tid:
            lines.append("usage: tracker new TYPE ID")
            return lines
        resolved = self._resolve_tracker_type(sub)
        if resolved is None:
            lines.append("unknown type: %s" % sub)
            lines.append(self.TRACKER_USAGE)
            return lines
        idx, attr = resolved
        st, fc = self._component("FireControl", owner="player")
        if st != "ok" or fc is None:
            st, fc = self._component("FireControl")
        if st != "ok" or fc is None:
            lines.append("no FireControl")
            return lines
        r = _try(lambda: getattr(fc, attr))
        if r[0] != "ok" or r[1] is None:
            lines.append("  %s not present" % attr)
            return lines
        tm = r[1]
        label = self.TRACKER_NAMES.get(idx, sub)
        lines.append("type %d = %s, creating track '%s'..." % (idx, label, tid))
        # Try int first
        try:
            tid_int = int(tid)
        except (ValueError, TypeError):
            tid_int = None
        attempts = []
        a1 = _try(lambda: tm.New(tid_int if tid_int is not None else tid))
        attempts.append(("New(%s)" % repr(tid_int if tid_int is not None else tid), a1))
        if a1[0] != "ok" and tid_int is not None:
            a2 = _try(lambda: tm.New(tid))
            attempts.append(("New(%r)" % tid, a2))
        if a1[0] != "ok" and tid_int is not None:
            a3 = _try(lambda: tm.New(tid_int, tid_int))
            attempts.append(("New(%d, %d)" % (tid_int, tid_int), a3))
        for desc, res in attempts:
            lines.append("  %s: %s" % (desc, "ok" if res[0] == "ok" else str(res[1])[:100]))
        return lines

    def do_env(self, cmd):
        """SonarSim Phase-1 environment probe (control command).

        Sub-commands:
          env            read-only: EnvironmentalSystem dir() + member types
                         (no method calls, freeze-safe)
          env ssp        additionally read the public SSP/TP/Analysis properties
                         and probe the returned arrays/objects (each isolated
                         with _try + cp markers in the log)
        """
        lines = ["env: environment probe"]
        st, env = self._component("EnvironmentalSystem", owner="player")
        if st != "ok" or env is None:
            st, env = self._component("EnvironmentalSystem")
        if st != "ok" or env is None:
            lines.append("no EnvironmentalSystem component (%s)" % env)
            return lines
        self.emit("env cp0: EnvironmentalSystem resolved")
        try:
            names = sorted(str(n) for n in dir(env))
        except Exception as e:
            names = []
            lines.append("env dir ERR %s" % _desc(e, 60))
        lines.append("env dir(%d)" % len(names))
        chunk = "env EnvironmentalSystem dir(%d): %s" % (len(names), ", ".join(names))
        for off in range(0, len(chunk), 1400):
            self.emit(chunk[off:off + 1400])
        for c in ("_Realistic", "_TempAccuracy", "_VelocityAccuracy",
                  "_DepthAccuracy", "_UserDataRatio", "_SSPiD", "_SSPManager",
                  "_RayTraceManager", "_OceanSurface", "_OceanFloor",
                  "get_SSP", "get_TrueSSP", "get_TP", "get_TrueTP",
                  "get_Analysis", "get_TrueAnalysis", "RayTrace",
                  "get_RayTraceOutput", "SSPManager", "RayTraceManager",
                  "SSPiD", "OceanSurface", "OceanFloor", "Realistic",
                  "SSP", "TrueSSP", "TP", "TrueTP", "SpecialDepth",
                  "TrueSpecialDepth", "Analysis", "TrueAnalysis",
                  "RayTraceOutput", "_Trace", "RetrieveConfiguration",
                  "Scope", "ScopeSelector"):
            if c not in names:
                lines.append("env absent: %s" % c)
                continue
            r = _try(lambda c=c: getattr(env, c))
            if r[0] == "ok":
                v = r[1]
                if callable(v):
                    lines.append("env.%s -> callable %s" % (c, _desc(type(v), 40)))
                else:
                    try:
                        val = str(v)
                    except Exception:
                        val = "<unprintable>"
                    lines.append("env.%s = %s (%s)" % (c, val[:80], _desc(type(v), 40)))
            else:
                lines.append("env.%s ERR %s" % (c, _desc(r[1], 60)))
        if cmd.get("ssp"):
            for propname in ("SSP", "TrueSSP", "TP", "TrueTP",
                             "SpecialDepth", "TrueSpecialDepth",
                             "Analysis", "TrueAnalysis",
                             "RayTraceOutput", "_Trace"):
                if propname not in names:
                    lines.append("env %s absent (skip)" % propname)
                    continue
                self.emit("env cp: reading %s ..." % propname)
                r = _try(lambda propname=propname: getattr(env, propname))
                if r[0] != "ok":
                    lines.append("env %s ERR %s" % (propname, _desc(r[1], 80)))
                    continue
                v = r[1]
                if callable(v):
                    lines.append("env %s -> callable (not called)" % propname)
                    continue
                self.emit("env cp: %s -> %s" % (propname, _desc(type(v), 60)))
                if v is None:
                    lines.append("env %s -> None" % propname)
                    continue
                self._probe_env_data(propname, v, lines)
        if "RayTrace" in names:
            lines.append("env RayTrace present (not called in phase 1)")
        rtm = None
        for cand in ("_RayTraceManager", "RayTraceManager"):
            if cand in names:
                r = _try(lambda cand=cand: getattr(env, cand))
                if r[0] == "ok" and r[1] is not None:
                    rtm = r[1]
                    break
        if rtm is not None:
            try:
                rnames = sorted(str(n) for n in dir(rtm))
            except Exception:
                rnames = []
            chunk = "env RayTraceManager dir(%d): %s" % (len(rnames), ", ".join(rnames))
            for off in range(0, len(chunk), 1400):
                self.emit(chunk[off:off + 1400])
            for c in ("CollectBathymetricData", "GetContacts", "get_Results",
                      "Results", "_ElementIDs", "_ElementIDpairRtID"):
                if c in rnames:
                    lines.append("env RayTraceManager.%s present" % c)
        st, hs = self._component("Hydrostatics", owner="player")
        if st != "ok" or hs is None:
            st, hs = self._component("Hydrostatics")
        if st == "ok" and hs is not None:
            for c in ("SL", "NL", "RoB", "FlowNoise", "SoundSources",
                      "Displacement"):
                r = _try(lambda c=c: getattr(hs, c))
                lines.append("env own_sound.%s = %s" % (
                    c, str(r[1])[:60] if r[0] == "ok" else r[1]))
        else:
            lines.append("env own_sound: no Hydrostatics")
        return lines

    def _probe_env_data(self, label, v, lines):
        """Probe an object returned by get_SSP/get_TP (SSP arrays/manager)."""
        if isinstance(v, (list, tuple)):
            n = len(v)
            sample = str(list(v[:min(n, 8)]))
            lines.append("env %s() obj.len=%d sample=%s" % (label, n, sample))
            return
        d = _try(lambda: dir(v))
        if d[0] != "ok":
            lines.append("env %s() obj dir ERR %s" % (label, _desc(d[1], 60)))
            return
        members = sorted(str(n) for n in d[1])
        chunk = "env %s() obj dir(%d): %s" % (label, len(members), ", ".join(members))
        for off in range(0, len(chunk), 1400):
            self.emit(chunk[off:off + 1400])
        for c in ("_MaxDepths", "_Temperatures", "_Salinities", "_Pressures",
                  "_Densities", "_Viscosities", "_Velocities", "_DepthIndexes",
                  "_SpecialDepths", "GetTemperatures", "GenerateDepthArray",
                  "GetDepthID", "GetSpecialDepth", "GetDepth", "GetVelocity"):
            if c not in members:
                lines.append("env %s() obj absent: %s" % (label, c))
                continue
            r2 = _try(lambda c=c, v=v: getattr(v, c))
            if r2[0] != "ok":
                lines.append("env %s() obj.%s ERR %s" % (label, c, _desc(r2[1], 60)))
                continue
            v2 = r2[1]
            if callable(v2):
                lines.append("env %s() obj.%s -> callable" % (label, c))
                continue
            try:
                n = len(v2)
                if n and n < 2000 and hasattr(v2, "__getitem__"):
                    sample = str(list(v2[:min(n, 8)]))
                else:
                    sample = ""
                lines.append("env %s() obj.%s len=%d %s" % (label, c, n, sample))
            except Exception:
                lines.append("env %s() obj.%s = %s" % (label, c, str(v2)[:80]))

    def _eot(self, name):
        if not name:
            raise RuntimeError("EOT order name missing (e.g. AheadStd)")
        if name not in _EOT_NAMES:
            raise RuntimeError("invalid EOT order %r (valid: %s)" % (name, ", ".join(_EOT_NAMES)))
        if self._eot_enum is None and self._eot_enum_err is None:
            nav = self._client()
            r = _try(lambda: nav._MechTools.EOTOrder)
            if r[0] == "ok":
                self._eot_enum = r[1]
            else:
                mt = self.g("MechTools")
                r2 = _try(lambda: mt.EOTOrder)
                if r2[0] == "ok":
                    self._eot_enum = r2[1]
                else:
                    self._eot_enum_err = r[1] + " / " + r2[1]
        if self._eot_enum is None:
            raise RuntimeError("EOTOrder enum unavailable: %s" % self._eot_enum_err)
        r = _try(lambda: getattr(self._eot_enum, name))
        if r[0] != "ok":
            raise RuntimeError("EOTOrder.%s: %s" % (name, r[1]))
        return r[1]

    def _nav(self):
        pnav = self.player_navigation()
        if pnav is not None:
            return pnav
        nav = self._client()
        r = _try(lambda: nav._Navigation)
        if r[0] != "ok":
            raise RuntimeError("no Navigation: %s" % r[1])
        return r[1]

    def _mech(self):
        pctrl = self.player_controller()
        if pctrl is not None:
            mt = self.g("MechTools")
            if mt is not None:
                r = _try(lambda: mt.EOTOrder)
                if r[0] == "ok":
                    return mt
        nav = self._client()
        r = _try(lambda: nav._MechTools)
        if r[0] != "ok":
            raise RuntimeError("no _MechTools: %s" % r[1])
        return r[1]

    def do_helm(self, cmd):
        nav = self._client()
        lines = []
        if cmd.get("course") is not None:
            try:
                course = float(cmd["course"])
                try:
                    nav._OrderedCourse = course
                    lines.append("blackboard _OrderedCourse=%s" % course)
                except Exception:
                    lines.append("blackboard _OrderedCourse unavailable")
            except Exception as e:
                lines.append("course write failed: %s" % _desc(e, 80))
        if cmd.get("eot"):
            eot = self._eot(str(cmd["eot"]))
            try:
                nav._OrderedEOTOrder = eot
                lines.append("blackboard _OrderedEOTOrder=%s" % cmd["eot"])
            except Exception:
                lines.append("blackboard _OrderedEOTOrder unavailable")
        if cmd.get("depth") is not None:
            try:
                depth = float(cmd["depth"])
                try:
                    nav._OrderedDepth = depth
                    lines.append("blackboard _OrderedDepth=%s" % depth)
                except Exception:
                    lines.append("blackboard _OrderedDepth unavailable")
            except Exception as e:
                lines.append("depth write failed: %s" % _desc(e, 80))
        # direct steering (bypasses blackboard, acts immediately)
        sd = self._steering()
        if cmd.get("course") is not None:
            # SetHeading(heading, snapToNorth) - snap only when explicitly asked
            snap = bool(cmd.get("snap"))
            r = _try(lambda: sd.SetHeading(float(cmd["course"]), snap))
            if r[0] != "ok":
                r = _try(lambda: sd.SetHeading(float(cmd["course"])))
            lines.append("SetHeading(%s%s): %s" % (
                cmd["course"], ", snap" if snap else "", "ok" if r[0] == "ok" else r[1]))
        if cmd.get("eot"):
            r = _try(lambda: sd.SetEOT(self._eot(str(cmd["eot"]))))
            lines.append("SetEOT: %s" % ("ok" if r[0] == "ok" else r[1]))
        if cmd.get("depth") is not None:
            # SetDepth(depth, envelopeIndex) - band defaults to 0 (Periscope)
            env = int(cmd.get("env", 0))
            r = _try(lambda: sd.SetDepth(float(cmd["depth"]), env))
            if r[0] != "ok":
                r = _try(lambda: sd.SetDepth(float(cmd["depth"])))
            lines.append("SetDepth(%s, env=%d): %s" % (cmd["depth"], env, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("bubble") is not None:
            bubble = float(cmd["bubble"])
            r = _try(lambda: sd.SetBubble(bubble))
            if r[0] == "ok":
                lines.append("SetBubble(%s): ok" % bubble)
            else:
                lines.append("SetBubble(%s): %s (live: 'planes bubble on|off' = CatchBubble/ReleaseBubble)" % (bubble, r[1]))
        if cmd.get("autotrim") is not None:
            fn = sd.AutoTrim if bool(cmd["autotrim"]) else sd.ManualTrim
            r = _try(lambda fn=fn: fn())
            lines.append("AutoTrim(%s): %s" % ("on" if cmd["autotrim"] else "off",
                                               "ok" if r[0] == "ok" else r[1]))
        self.collect_state()
        return lines

    def do_planes(self, cmd):
        """Control and read the player's control surfaces.

        Writes (each optional):
          fwd ANGLE | stern ANGLE | rudder ANGLE        plane/rudder angle (deg)
          rudder release | bubble release               drop autopilot hold
          bubble ANGLE                                  ordered trim bubble
          autotrim on|off                               auto-trim loop
          bow RETRACT|EXTEND                            bow planes
          lockfwd on|off | lockint on|off               plane locks

        Always ends with a fresh read-out of the control-surface state
        (plane angles, ordered values, depth bands, locks)."""
        sd = self._steering()
        lines = []
        if cmd.get("fwd") is not None:
            v = float(cmd["fwd"])
            r = _try(lambda: sd.SetForwardPlanes(v))
            lines.append("SetForwardPlanes(%s): %s" % (v, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("stern") is not None:
            v = float(cmd["stern"])
            r = _try(lambda: sd.SetSternPlanes(v))
            lines.append("SetSternPlanes(%s): %s" % (v, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("rudder") is not None:
            v = float(cmd["rudder"])
            r = _try(lambda: sd.SetRudder(v))
            lines.append("SetRudder(%s): %s" % (v, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("release_rudder"):
            r = _try(lambda: sd.ReleaseRudder())
            lines.append("ReleaseRudder: %s" % ("ok" if r[0] == "ok" else r[1]))
        if cmd.get("bubble") is not None:
            # live SteeringDiving has NO SetBubble (verified); CatchBubble()/ReleaseBubble()
            # are the live bubble controls. SetBubble is only attempted for the
            # rare build that has it; a clear note is emitted otherwise.
            v = float(cmd["bubble"])
            r = _try(lambda: sd.SetBubble(v))
            if r[0] == "ok":
                lines.append("SetBubble(%s): ok" % v)
            else:
                lines.append("SetBubble(%s): %s (live: use 'planes bubble on|off' = CatchBubble/ReleaseBubble)" % (v, r[1]))
        if cmd.get("bubble_on") is not None:
            if cmd["bubble_on"]:
                # CatchBubble needs 1 positional arg (verified live; likely a
                # callback/trigger arg). Try with True first, fall back to no-arg.
                r = _try(lambda: sd.CatchBubble(True))
                if r[0] != "ok":
                    r = _try(lambda: sd.CatchBubble())
                lines.append("CatchBubble: %s" % ("ok" if r[0] == "ok" else r[1]))
            else:
                r = _try(lambda: sd.ReleaseBubble())
                lines.append("ReleaseBubble: %s" % ("ok" if r[0] == "ok" else r[1]))
        if cmd.get("release_bubble"):
            r = _try(lambda: sd.ReleaseBubble())
            lines.append("ReleaseBubble: %s" % ("ok" if r[0] == "ok" else r[1]))
        if cmd.get("autotrim") is not None:
            fn = sd.AutoTrim if bool(cmd["autotrim"]) else sd.ManualTrim
            r = _try(lambda fn=fn: fn())
            lines.append("AutoTrim(%s): %s" % ("on" if cmd["autotrim"] else "off",
                                               "ok" if r[0] == "ok" else r[1]))
        if cmd.get("bow") is not None:
            flag = bool(cmd["bow"])
            r = _try(lambda: sd.ToggleBowPlanes(flag, None))
            if r[0] != "ok":
                r = _try(lambda: sd.ToggleBowPlanes(flag))
            lines.append("ToggleBowPlanes(%s): %s" % (flag, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("lockfwd") is not None:
            flag = bool(cmd["lockfwd"])
            r = _try(lambda: sd.LockForwardPlanes(flag, None))
            if r[0] != "ok":
                r = _try(lambda: sd.LockForwardPlanes(flag))
            lines.append("LockForwardPlanes(%s): %s" % (flag, "ok" if r[0] == "ok" else r[1]))
        if cmd.get("lockint") is not None:
            flag = bool(cmd["lockint"])
            r = _try(lambda: sd.LockIntSternPlanes(flag, None))
            if r[0] != "ok":
                r = _try(lambda: sd.LockIntSternPlanes(flag))
            lines.append("LockIntSternPlanes(%s): %s" % (flag, "ok" if r[0] == "ok" else r[1]))
        # read-out (with_getters: stern/rudder via Hydrodynamics Get*() - live-verified)
        r = _try(lambda: self.read_steering(with_getters=True))
        if r[0] == "ok":
            s = r[1]
            lines.append("planes fwd=%s stern=%s rudder=%s type=%s" % (
                s.get("forward_plane_angles"), s.get("stern_plane_angles"),
                s.get("rudder_plane_angles"), s.get("forward_planes_type")))
            lines.append("ordered: eot=%s speed=%s heading=%s depth=%s cav=%s" % (
                s.get("ordered_eot"), s.get("ordered_speed"), s.get("ordered_heading"),
                s.get("ordered_depth"), s.get("cavitation")))
            lines.append("locks: fwd=%s intstern=%s bow_retracted=%s tpk=%s stw=%s" % (
                s.get("forward_planes_locked"), s.get("int_stern_planes_locked"),
                s.get("bow_planes_retracted"), s.get("tpk"), s.get("stw")))
        else:
            lines.append("read_steering: %s" % r[1])
        self.collect_state()
        return lines

    def do_plot(self, cmd):
        nav = self._client()
        lines = []
        lat = float(cmd["lat"])
        lon = float(cmd["lon"])
        gc = self.g("GeoCord")
        if gc is None:
            raise RuntimeError("GeoCord unavailable")
        dest = gc(lat, lon)
        nav_obj = self._nav()
        start = _try(lambda: nav_obj.INS.GeoCoordinates)
        if start[0] != "ok":
            raise RuntimeError("no own position: %s" % start[1])
        start = start[1]
        wp_class = self.g("Waypoint")
        if wp_class is None:
            raise RuntimeError("Waypoint unavailable")
        wp_list = _try(lambda: nav._WaypointList)
        if wp_list[0] != "ok":
            raise RuntimeError("no _WaypointList on blackboard")
        wp_list = wp_list[1]
        r = _try(lambda: nav_obj.Pathfinder.Route(start, dest)
                 .GenerateGrid(20.5, 60000, 60)
                 .Plot[wp_class](wp_list, -46, 3000, True, False, True))
        if r[0] != "ok":
            lines.append("pathfinder: %s" % r[1])
        # ClearPlot BEFORE AddMultipleWaypoints (old order wiped the route:
        # live test 06:02 returned "plot waypoints=0" with Add->Clear).
        r3 = _try(lambda: nav_obj.Plot.ClearPlot())
        lines.append("ClearPlot (pre-existing): %s" % ("ok" if r3[0] == "ok" else "skipped"))
        r2 = _try(lambda: nav_obj.Plot.AddMultipleWaypoints[wp_class](wp_list))
        lines.append("AddMultipleWaypoints: %s" % ("ok" if r2[0] == "ok" else r2[1]))
        try:
            nav._WaypointIterator = 0
            idx = nav_obj.Plot.AvailableWaypointsIndex()
            try:
                lines.append("plot waypoints=%d" % len(idx))
            except Exception:
                lines.append("plot waypoints index set")
        except Exception as e:
            lines.append("iterator: %s" % _desc(e, 80))
        self.collect_state()
        return lines

    def do_clear_plot(self, cmd):
        nav_obj = self._nav()
        r = _try(lambda: nav_obj.Plot.ClearPlot())
        if r[0] != "ok":
            raise RuntimeError("ClearPlot: %s" % r[1])
        self.collect_state()
        return ["plot cleared"]

    def do_report(self, cmd):
        nav = self._client()
        fn = _try(lambda: nav._ReportToHQ)
        if fn[0] == "ok" and callable(fn[1]):
            r = _try(lambda: fn[1]())
            if r[0] != "ok":
                raise RuntimeError("ReportToHQ: %s" % r[1])
            return ["HQ report sent"]
        raise RuntimeError("no _ReportToHQ on host blackboard")

    def do_probe(self, cmd):
        self.discovery_run()
        return ["discovery written to %s" % _PROBE_NAME]

    # ---------------------------------------------------------------
    # ai-attack: push an Engage order at the player to an AI element.
    # Replicates the engine's own give_orders pipeline (Engage assignment
    # + Order + AiDataLink.PushOrder). Every step is _try-guarded; the
    # return lines include registry diagnostics so a failed attack is
    # debuggable instead of silent. Never freezes: all calls are plain
    # constructors/getters, no coordinate conversion.
    #
    # Country resolution: AI element namespaces do NOT register
    # _Information (only _InformationElemenet == _Information.Element and
    # _ElementID). _element_country() tries Operator.CountryID /
    # CountryID on _InformationElemenet and _CurrentAssignment.Who, and
    # dumps the object surfaces into the result lines if unresolved.
    # ---------------------------------------------------------------

    def _element_namespace(self, nid):
        """Return {key: value} for namespace /<nid>/ from blackboard storage."""
        bb = self._blackboard_storage()
        if not bb:
            return None
        try:
            items = list(bb.items())
        except Exception:
            return None
        prefix = "/%d/" % nid
        kv = {}
        for k, v in items:
            if isinstance(k, str) and k.startswith(prefix):
                kv[k[len(prefix):]] = v
        return kv or None

    @staticmethod
    def _int_or_skip(r):
        """Return int(r[1]) if the _try succeeded and converts, else None."""
        if r[0] == "ok":
            try:
                return int(r[1])
            except Exception:
                return None
        return None

    def _host_operator_id(self, lines):
        """Resolve the AI operator's country id from the HOST's own _Information
        (int(info.CountryID) — the verified crash-free pattern used by
        read_identity/read_ai_elements every tick).

        NOT via:
          - _InformationElemenet.Operator.CountryID  -> native crash
          - client.country_id / client.* attribute    -> client is a
            blackboard path wrapper; arbitrary attribute reads crash natively
        Returns an int, or None."""
        info = self.host_get("_Information")
        if info is not None:
            self.emit("ai-attack cp0c0: about to host _Information.CountryID")
            r = _try(lambda: int(info.CountryID))
            self.emit("ai-attack cp0c1: host _Information.CountryID returned %s" % r[0])
            if r[0] == "ok":
                lines.append("operator_id via host _Information.CountryID=%d" % r[1])
                return r[1]
            lines.append("operator id: host _Information.CountryID: %s" % r[1])
        else:
            lines.append("operator id: no host _Information")
        return None

    def _element_country(self, kv, who, lines):
        """Resolve an AI element's operator country id. Tries, in order:
        1. the element's _InformationElemenet (== _Information.Element) via
           Operator.CountryID / CountryID / Operator,
        2. the element's _CurrentAssignment.Who via CountryID / Operator.
        On total failure dumps the object surfaces into lines for debugging.
        Returns an int country id, or None."""
        candidates = []

        ie = kv.get("_InformationElemenet")
        if ie is not None:
            candidates.append(("InformationElemenet", ie))
        if who is not None:
            candidates.append(("CurrentAssignment.Who", who))

        step = 0
        for label, obj in candidates:
            for path, fn in (
                ("Operator.CountryID", lambda obj=obj: obj.Operator.CountryID),
                ("Operator.Country", lambda obj=obj: obj.Operator.Country),
                ("CountryID", lambda obj=obj: obj.CountryID),
                ("Country", lambda obj=obj: obj.Country),
                ("Operator", lambda obj=obj: obj.Operator),
            ):
                step += 1
                self.emit("ai-attack cpc%d: about to %s.%s" % (step, label, path))
                r = _try(fn)
                self.emit("ai-attack cpc%d: %s.%s returned %s" % (step, label, path, r[0]))
                v = self._int_or_skip(r)
                if v is not None and v >= 0:
                    lines.append("country via %s.%s=%s" % (label, path, v))
                    return v

        for label, obj in candidates:
            try:
                members = [m for m in dir(obj) if not m.startswith("_")][:40]
                lines.append("%s members: %s" % (label, ", ".join(members)))
            except Exception as e:
                lines.append("%s dir failed: %s" % (label, _desc(e, 80)))
        return None

    def _ai_track_probe(self, nid, kv, player_ll, lines):
        """Ask the AI element's OWN _ContactManager whether it has a contact on
        the player. The engine only builds an attack solution from reported
        contacts (give_orders.py feeds Engage from opfor_report.Telemetry), so
        an attack on a player the AI does not track is a blind fire at a
        coordinate. Returns (found, detail_lines).
        Every access is _try-guarded; read_sonar stays disabled."""
        out = {"checked": 0, "found": False, "err": None}
        cmgr = kv.get("_ContactManager")
        if cmgr is None:
            return out, ["track probe: no _ContactManager on element %d" % nid]
        r = _try(lambda: cmgr.GetUsed)
        if r[0] != "ok":
            out["err"] = "GetUsed: %s" % (r[1] or r[1])
            return out, ["track probe: %s" % out["err"]]
        used = r[1]
        if not used or len(used) == 0:
            return out, ["track probe: element %d has NO contacts in _ContactManager" % nid]

        nav = kv.get("_Navigation")
        own_ll = None
        if nav is not None:
            r = _try(lambda: nav.INS.GeoCoordinates)
            if r[0] == "ok":
                own_ll = _coord_to_ll(r[1])
        ai_player_km = None
        if own_ll is not None:
            ai_player_km, _ = _range_bearing(own_ll[0], own_ll[1], player_ll[0], player_ll[1])

        out["element_contacts"] = len(used)
        out["ai_player_km"] = ai_player_km
        self.emit("ai-attack cpa: %d contacts, ai->player %s km" % (len(used), ai_player_km))
        limit = int(self.cfg.get("max_contacts", 50))
        for cid in used:
            if out["checked"] >= limit:
                break
            out["checked"] += 1
            r = _try(lambda cid=cid: cmgr.GetTrack(cid))
            if r[0] != "ok":
                continue
            tk = r[1]
            rng = _try(lambda: float(tk._Range))
            if rng[0] != "ok":
                continue
            rng_m = rng[1]
            if ai_player_km is not None and rng_m is not None:
                diff_km = abs(rng_m / 1000.0 - ai_player_km)
                if diff_km <= 3.0:
                    out["found"] = True
                    out["player_contact_range_m"] = round(rng_m, 1)
                    out["player_contact_id"] = _desc(cid, 40)
                    lines.append("track probe: HIT contact on player, range=%.0f m (id %s)"
                                 % (rng_m, _desc(cid, 40)))
                    self.emit("ai-attack cpb: player tracked (range %.0f m)" % rng_m)
                    return out, lines
        lines.append("track probe: NO contact on player (checked %d of %d contacts)"
                     % (out["checked"], len(used)))
        self.emit("ai-attack cpc: player NOT tracked")
        return out, lines

    def do_detected(self, cmd):
        """Report which AI elements currently hold a contact/track on the player.

        Iterates every /N/ blackboard namespace and runs the same track-probe
        as ai-attack (_ai_track_probe): reads the element's own _ContactManager,
        compares each contact's GetTrack._Range against the true AI->player
        distance (tolerance 3 km). Every access is _try-guarded.

        Returns a detail line per AI element that tracks the player plus a
        summary line 'DETECTED' or 'NOT detected by any AI element'."""
        lines = ["detected: scanning %s" % _desc(self._ai_namespaces(), 80)]
        player_ll = None
        pnav = self.player_navigation()
        if pnav is not None:
            r = _try(lambda: pnav.INS.GeoCoordinates)
            if r[0] == "ok":
                player_ll = _coord_to_ll(r[1])
        if player_ll is None:
            raise RuntimeError("no player position (nav unresolved)")
        lines.append("detected: player at lat=%.4f lon=%.4f" % (player_ll[0], player_ll[1]))

        pid = None
        try:
            pid = int(self.detect_player().get("player_id") or 0)
        except Exception:
            pid = None

        found = []
        bb = self._blackboard_storage()
        if bb is None:
            raise RuntimeError("no blackboard storage")
        items = []
        try:
            items = list(bb.items())
        except Exception as e:
            raise RuntimeError("storage unreadable: %s" % _desc(e, 80))
        namespaces = {}
        for k, v in items:
            if not isinstance(k, str):
                continue
            parts = k.split("/")
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            namespaces.setdefault(parts[1], {})[parts[2]] = v
        for ns in sorted(namespaces, key=lambda x: int(x)):
            try:
                nid = int(ns)
            except Exception:
                continue
            if pid is not None and nid == pid:
                continue
            kv = namespaces[ns]
            probe, tlines = self._ai_track_probe(nid, kv, player_ll, lines)
            lines.extend(tlines)
            if probe.get("found"):
                found.append({"id": nid,
                              "range_m": probe.get("player_contact_range_m"),
                              "contact_id": probe.get("player_contact_id"),
                              "contacts": probe.get("element_contacts")})
        if found:
            for f in found:
                lines.append("DETECTED by element %d (range %.0f m, contact id %s, %d contacts)" % (
                    f["id"], f["range_m"] or 0, _desc(f["contact_id"], 40), f["contacts"] or 0))
            lines.append("DETECTED elements: %s" % ", ".join(str(f["id"]) for f in found))
            return lines
        lines.append("NOT detected by any AI element")
        return lines

    def do_ai_contacts(self, cmd):
        """Dump every AI element's OWN _ContactManager contacts + tracks.

        Per /N/ namespace: _ContactManager.GetUsed -> GetCategoryID/GetPrefix/
        GetStandardIdentity/GetTrack, reading the same freeze-safe track fields
        (_Speed/_Range/_Course/_Bearing/...) proven in the player contacts
        reader. Every access is _try-guarded; never touches FireControl/
        WeaponController internals (native-crash rule). Returns one 'contacts:'
        detail line per element plus per-track lines."""
        lines = ["ai-contacts: scanning %s" % _desc(self._ai_namespaces(), 80)]
        bb = self._blackboard_storage()
        if bb is None:
            raise RuntimeError("no blackboard storage")
        items = []
        try:
            items = list(bb.items())
        except Exception as e:
            raise RuntimeError("storage unreadable: %s" % _desc(e, 80))
        namespaces = {}
        for k, v in items:
            if not isinstance(k, str):
                continue
            parts = k.split("/")
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            namespaces.setdefault(parts[1], {})[parts[2]] = v
        pid = None
        try:
            pid = int(self.detect_player().get("player_id") or 0)
        except Exception:
            pid = None
        maxc = int(self.cfg.get("max_contacts", 50))
        for ns in sorted(namespaces, key=lambda x: int(x)):
            try:
                nid = int(ns)
            except Exception:
                continue
            if pid is not None and nid == pid:
                continue
            kv = namespaces[ns]
            cmgr = kv.get("_ContactManager")
            if cmgr is None:
                lines.append("contacts: element %d: no _ContactManager" % nid)
                continue
            r = _try(lambda: cmgr.GetUsed)
            if r[0] != "ok" or not r[1]:
                lines.append("contacts: element %d: GetUsed %s (0 contacts)" % (
                    nid, r[0]))
                continue
            used = r[1]
            lines.append("contacts: element %d: %d contacts" % (nid, len(used)))
            checked = 0
            for cid in used:
                if checked >= maxc:
                    lines.append("contacts:   ...truncated at %d" % maxc)
                    break
                checked += 1
                cat = _try(lambda cid=cid: cmgr.GetCategoryID(cid))
                pref = _try(lambda cid=cid: cmgr.GetPrefix(cid))
                ident = _try(lambda cid=cid: cmgr.GetStandardIdentity(cid))
                tk = _try(lambda cid=cid: cmgr.GetTrack(cid))
                parts = ["id %s" % _desc(cid, 24)]
                if cat[0] == "ok":
                    parts.append("cat %s" % _desc(cat[1], 24))
                if pref[0] == "ok":
                    parts.append("prefix %s" % _desc(pref[1], 24))
                if ident[0] == "ok":
                    parts.append("ident %s" % _desc(ident[1], 24))
                if tk[0] == "ok":
                    t = tk[1]
                    for name, attr in (
                        ("range_m", "_Range"), ("speed", "_Speed"), ("course", "_Course"),
                        ("elev", "_Elevation"), ("bearing_rate", "_BearingRate"),
                        ("relative_bearing", "_RelativeBearing"), ("bearing", "_Bearing"),
                    ):
                        r2 = _try(lambda attr=attr: getattr(t, attr))
                        if r2[0] != "ok":
                            continue
                        v = r2[1]
                        if attr in ("_RelativeBearing", "_Bearing") and v is not None:
                            try:
                                v = [_safe_num(v.Item1), _safe_num(v.Item2)]
                            except Exception:
                                v = _safe_num(v)
                        elif isinstance(v, (int, float)):
                            v = _safe_num(v)
                        if v is not None:
                            parts.append("%s=%s" % (name, v))
                lines.append("  " + ", ".join(parts))
        lines.append("ai-contacts: done")
        return lines

    def do_ns_dump(self, cmd):
        """Dump every /N/ blackboard namespace's keys (no C# access).

        Useful for identifying which host script owns which element: the
        key set of a namespace tells whether it is a ship host
        (_SelfInfo/_AttackOps), plane host (_ElementID/_InformationElemenet
        but no _SelfInfo), submarine host, or helicopter host
        (_DippingSonar*). Pure storage reads - never freezes."""
        lines = ["ns-dump: namespaces on this host:"]
        bb = self._blackboard_storage()
        if bb is None:
            raise RuntimeError("no blackboard storage")
        items = []
        try:
            items = list(bb.items())
        except Exception as e:
            raise RuntimeError("storage unreadable: %s" % _desc(e, 80))
        namespaces = {}
        for k, v in items:
            if not isinstance(k, str):
                continue
            parts = k.split("/")
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            namespaces.setdefault(parts[1], {})[parts[2]] = v
        for ns in sorted(namespaces, key=lambda x: int(x)):
            kv = namespaces[ns]
            keylist = sorted(kv.keys())
            style = "?"
            if any(k in kv for k in ("_DippingSonarController", "_DippingSonarOps", "_DippingEngaged")):
                style = "helo"
            elif "_SelfInfo" in kv and "_AttackOps" in kv:
                style = "ship"
            elif "_ElementID" in kv and "_SelfInfo" not in kv:
                style = "plane/sub"
            elif "_SelfInfo" in kv:
                style = "general"
            line = "ns /%s/ style=%s keys(%d): %s" % (ns, style, len(keylist), ", ".join(keylist))
            lines.append(line)
            self.emit("ns-dump: " + line)
        self.emit("ns-dump: host done (%d namespaces)" % len(namespaces))
        return lines

    def do_asg(self, cmd):
        """Report an element's current assignment as VALUES (not keys).

        Reads _CurrentAssignmentID / _IncomingOrder.AssignmentID /
        _ActionPrepComplete / _CurrentCourse from the /N/ namespace and emits
        them. Pure storage reads + the safe host-Information.CountryID pattern -
        never freezes. Multi-host safe: answers only on the host that owns the
        /N/ namespace (cp0x skip otherwise)."""
        lines = ["asg: assignment probe"]
        try:
            nid = int(cmd["id"])
        except Exception:
            raise RuntimeError("asg ID")
        lines.append("target element id=%d" % nid)
        kv = self._element_namespace(nid)
        if kv is None:
            self.emit("asg cp0x: no /%d/ namespace on this host - skipping (multi-host safe)" % nid)
            return None
        self.emit("asg cp0a: namespace ok (%d keys)" % len(kv))
        for key, label in (("_CurrentAssignmentID", "assignment_id"),
                           ("_ActionPrepComplete", "action_prep"),
                           ("_CurrentCourse", "current_course"),
                           ("_OrderedCourse", "ordered_course"),
                           ("_CurrentAltitude", "current_altitude"),
                           ("_OrderedAltitude", "ordered_altitude"),
                           ("_CurrentThrottleRatio", "throttle"),
                           ("_CurrentRPM", "rpm"),
                           ("_DippingEngaged", "dipping_engaged"),
                           ("_EmergencyManeuver", "emergency_maneuver"),
                           ("_MissileThreat", "missile_threat"),
                           ("_TorpedoThreat", "torpedo_threat"),
                           ("_AircraftThreat", "aircraft_threat"),
                           ("_EngageEvasMan", "engage_evasion")):
            v = kv.get(key)
            if v is None:
                continue
            r = _try(lambda v=v: int(v) if isinstance(v, (int, float)) else v)
            if r[0] == "ok":
                lines.append("%s=%s" % (label, r[1]))
                self.emit("asg %s=%s" % (label, r[1]))
            else:
                lines.append("%s=<not int: %s>" % (label, _desc(v, 40)))
                self.emit("asg %s=<not int>" % label)
        link = kv.get("_AiDataLink")
        if link is not None:
            for attr in ("InboxLength", "OutboxLength"):
                r = _try(lambda attr=attr: int(getattr(link, attr)()))
                if r[0] != "ok":
                    r = _try(lambda attr=attr: int(getattr(link, attr)))
                if r[0] == "ok":
                    lines.append("ai_datalink.%s=%s" % (attr, r[1]))
                    self.emit("asg ai_datalink.%s=%s" % (attr, r[1]))
                else:
                    lines.append("ai_datalink.%s=<err: %s>" % (attr, r[1]))
        ammo = kv.get("_AmmunitionStorage")
        if ammo is not None:
            for attr in ("OffensiveCombatPowerRatio", "DefensiveCombatPowerRatio"):
                r = _try(lambda attr=attr: getattr(ammo, attr))
                if r[0] == "ok":
                    val = r[1]
                    if callable(val):
                        r = _try(lambda val=val: val())
                    else:
                        r = _try(lambda val=val: float(val))
                if r[0] == "ok":
                    lines.append("ammunition.%s=%s" % (attr, r[1]))
                    self.emit("asg ammunition.%s=%s" % (attr, r[1]))
                else:
                    lines.append("ammunition.%s=<err: %s>" % (attr, _desc(r[1], 40)))
                    self.emit("asg ammunition.%s=<err>" % attr)
        order = kv.get("_IncomingOrder")
        if order is not None:
            r = _try(lambda: int(order.AssignmentID))
            if r[0] == "ok":
                lines.append("incoming_order_assignment_id=%d" % r[1])
                self.emit("asg incoming_order_assignment_id=%d" % r[1])
        asg = kv.get("_CurrentAssignment")
        if asg is not None:
            r = _try(lambda: str(asg))
            if r[0] == "ok":
                lines.append("current_assignment=%s" % _desc(r[1], 100))
                self.emit("asg current_assignment=%s" % _desc(r[1], 100))
        self.emit("asg done")
        return lines

    def do_ai_attack(self, cmd):
        lines = []
        try:
            nid = int(cmd["id"])
        except Exception:
            raise RuntimeError("ai-attack ID")
        lines.append("target element id=%d" % nid)

        registry_only = bool(cmd.get("registry_only"))

        kv = self._element_namespace(nid)
        if kv is None:
            self.emit("ai-attack cp0x: no /%d/ namespace on this host - skipping (multi-host safe)" % nid)
            return None
        self.emit("ai-attack cp0a: namespace ok (%d keys)" % len(kv))
        iele = kv.get("_InformationElemenet")
        self.emit("ai-attack cp0a1: _InformationElemenet type=%s repr=%s" % (type(iele).__name__ if iele is not None else "None", _desc(iele, 80)))
        self.emit("ai-attack cp0a2: namespace keys: %s" % sorted(kv.keys()))

        asg = kv.get("_CurrentAssignment")
        who = _try(lambda: asg.Who) if asg is not None else ("err", "no _CurrentAssignment")
        whom = _try(lambda: asg.Whom) if asg is not None else ("err", "no _CurrentAssignment")
        if who[0] != "ok" or whom[0] != "ok" or who[1] is None or whom[1] is None:
            lines.append("_CurrentAssignment who/whom incomplete (%s / %s) — falling back to element refs" % (who[1], whom[1]))
            self.emit("ai-attack cp0b1: fallback to element refs")
            who = ("ok", kv.get("_InformationElemenet")) if kv.get("_InformationElemenet") is not None else who
            who_el = kv.get("_InformationElemenet")
            if who_el is None:
                sinfo = kv.get("_SelfInfo")
                if sinfo is not None:
                    self.emit("ai-attack cp0b2: _SelfInfo type=%s repr=%s" % (type(sinfo).__name__, _desc(sinfo, 80)))
                    r = _try(lambda: sinfo.Element)
                    self.emit("ai-attack cp0b3: _SelfInfo.Element -> %s %s" % (r[0], _desc(r[1], 80)))
                    if r[0] == "ok" and r[1] is not None:
                        who_el = r[1]
            if who_el is None:
                for k, v in sorted(kv.items()):
                    tn = type(v).__name__
                    if "Element" in tn or ("Information" in tn and k in ("_SelfInfo", "_Information")):
                        r = _try(lambda v=v: getattr(v, "Element", None))
                        cand = r[1] if r[0] == "ok" else None
                        if cand is not None:
                            self.emit("ai-attack cp0b4: candidate %s -> Element %s" % (k, _desc(cand, 80)))
                            who_el = cand
                            break
                        if "Element" in tn:
                            self.emit("ai-attack cp0b4: direct Element-typed value %s (%s)" % (k, tn))
                            who_el = v
                            break
            if who_el is not None:
                who = ("ok", who_el)
            else:
                who = ("err", "no element ref for who (no _InformationElemenet/_SelfInfo in /%d/)" % nid)
            p_el = None
            pinfo = self.player_info()
            if pinfo is not None:
                r = _try(lambda: pinfo.Element)
                if r[0] == "ok" and r[1] is not None:
                    p_el = r[1]
            if p_el is None:
                cm = self.coordinates_manager()
                if cm is not None:
                    r = _try(lambda: cm.Player)
                    if r[0] == "ok" and r[1] is not None:
                        p_el = r[1]
            whom = ("ok", p_el) if p_el is not None else ("err", "no player element for whom")
        if who[1] is None or whom[1] is None:
            raise RuntimeError("who/whom resolve failed: %s / %s" % (who[1], whom[1]))
        lines.append("who/whom resolved (who=%s whom=%s)" % (_desc(who[1], 60), _desc(whom[1], 60)))
        self.emit("ai-attack cp0b: who/whom resolved")

        operator_id = self._host_operator_id(lines)
        if operator_id is None:
            raise RuntimeError("operator id unresolved via host client (surfaces dumped above)")
        lines.append("operator_id=%d" % operator_id)
        self.emit("ai-attack cp0c: operator_id=%d" % operator_id)

        pnav = self.player_navigation()
        if pnav is None:
            raise RuntimeError("no player navigation")
        self.emit("ai-attack cp0d: player_navigation ok")
        r = _try(lambda: pnav.INS.GeoCoordinates)
        if r[0] != "ok":
            raise RuntimeError("player coords: %s" % r[1])
        target_geo = r[1]
        r = _try(lambda: float(target_geo.latitude))
        lat = r[1] if r[0] == "ok" else None
        r = _try(lambda: float(target_geo.longitude))
        lon = r[1] if r[0] == "ok" else None
        r = _try(lambda: float(pnav.INS.TrueHeading))
        tcourse = r[1] if r[0] == "ok" else 0.0
        r = _try(lambda: float(pnav.DepthGauge.Elevation))
        elev = r[1] if r[0] == "ok" else -90.0
        r = _try(lambda: float(pnav.INS.TrueForwardSpeed))
        spd = r[1] if r[0] == "ok" else 0.0
        lines.append("target player lat=%s lon=%s elev=%s spd=%s" % (lat, lon, elev, spd))

        self.emit("ai-attack cp1: player nav resolved")
        track_probe, track_lines = self._ai_track_probe(nid, kv, (lat, lon), lines)
        lines.extend(track_lines)
        if not track_probe.get("found") and not cmd.get("allow_untracked"):
            raise RuntimeError("no contact on player — refusing blind attack (pass allow_untracked:true to override)")

        aitools = self.g("AITools")
        if aitools is None:
            raise RuntimeError("AITools unavailable")
        r = _try(lambda: aitools.SearchPattern.Nothing)
        if r[0] != "ok":
            raise RuntimeError("AITools.SearchPattern.Nothing: %s" % r[1])
        search_pattern = r[1]
        # Aimpoint fix (2026-08-16): WeaponController.Fire uses
        # Where.Orientation as its TrueBearing, NOT the live track. Setting the
        # orientation to the player's own course made the FFG fire at ~45deg
        # (Candidates: [] / Result: False in every block). Use the bearing from
        # THIS element to the player instead so the fire solution points at the
        # player. Fallback to player course if the element's nav is unreadable.
        own_ll = None
        own_nav = kv.get("_Navigation")
        if own_nav is not None:
            r = _try(lambda: own_nav.INS.GeoCoordinates)
            if r[0] == "ok" and r[1] is not None:
                own_ll = _coord_to_ll(r[1])
        aim_orientation = tcourse
        if own_ll is not None and lat is not None and lon is not None:
            _, aim_brg = _range_bearing(own_ll[0], own_ll[1], lat, lon)
            if aim_brg is not None:
                aim_orientation = aim_brg
        lines.append("aimpoint: own=%s player=%s -> orientation=%s (player course was %s)" % (
            own_ll, (lat, lon), aim_orientation, tcourse))
        self.emit("ai-attack cp2a: aim orientation=%s" % aim_orientation)
        r = _try(lambda: aitools.SearchArea(target_geo, 0, 0, aim_orientation, search_pattern))
        if r[0] != "ok":
            raise RuntimeError("AITools.SearchArea: %s" % r[1])
        where = r[1]
        lines.append("SearchArea at player created")
        self.emit("ai-attack cp2: SearchArea ok")

        engage_cls = self.g("Engage")
        etools = self.g("ElementTools")
        if engage_cls is None or etools is None:
            raise RuntimeError("Engage/ElementTools unavailable")
        domain_name = str(cmd.get("domain", "Subsurface"))
        r = _try(lambda: getattr(etools.BaseCategory, domain_name))
        if r[0] != "ok":
            raise RuntimeError("ElementTools.BaseCategory.%s: %s" % (domain_name, r[1]))
        base_cat = r[1]
        lines.append("domain=%s" % domain_name)
        self.emit("ai-attack cp3: about to call Engage(...) constructor (domain=%s)" % domain_name)
        self.emit("ai-attack cp3a: who type=%s repr=%s" % (type(who[1]).__name__, _desc(who[1], 80)))
        self.emit("ai-attack cp3b: whom type=%s repr=%s" % (type(whom[1]).__name__, _desc(whom[1], 80)))
        r = _try(lambda: engage_cls(who[1], where, whom[1], base_cat, elev, spd))
        if r[0] != "ok":
            raise RuntimeError("Engage constructor: %s" % r[1])
        engage = r[1]
        self.emit("ai-attack cp4: Engage constructor returned")
        r = _try(lambda: int(engage.ID))
        if r[0] != "ok":
            raise RuntimeError("engage.ID: %s" % r[1])
        assignment_id = r[1]
        lines.append("Engage created (assignment_id=%d)" % assignment_id)
        self.emit("ai-attack cp5: Engage.ID=%d" % assignment_id)

        mission = self.active_mission()
        diag = []
        if mission is not None:
            self.emit("ai-attack cp6: about to GetOperationType(%d)" % assignment_id)
            r = _try(lambda: int(mission.GetOperationType(assignment_id)))
            diag.append("GetOperationType(%d)=%s" % (assignment_id, r[1] if r[0] == "ok" else r[1]))
            self.emit("ai-attack cp7: GetOperationType returned")
            if engage_cls is not None:
                self.emit("ai-attack cp8: about to GetAssignment[Engage](%d)" % assignment_id)
                r2 = _try(lambda: mission.GetAssignment[engage_cls](assignment_id))
                diag.append("GetAssignment[Engage](%d): %s" % (assignment_id, "ok" if r2[0] == "ok" else r2[1]))
                self.emit("ai-attack cp9: GetAssignment[Engage] returned")
        else:
            diag.append("no active mission (registry check skipped)")
        lines.extend(diag)

        if registry_only:
            lines.append("REGISTRY-ONLY: stopped before Order/PushOrder (assignment_id=%d)" % assignment_id)
            self.collect_state()
            return lines

        order_cls = self.g("Order")
        if order_cls is None:
            raise RuntimeError("Order unavailable")
        self.emit("ai-attack cp10: about to Order(operator=%d, tactical=%d, assignment=%d)" % (operator_id, nid, assignment_id))
        r = _try(lambda: order_cls(operator_id, nid, assignment_id))
        if r[0] != "ok":
            raise RuntimeError("Order constructor: %s" % r[1])
        order = r[1]
        self.emit("ai-attack cp11: Order constructed")

        link = None
        elem_link = kv.get("_AiDataLink")
        if elem_link is not None:
            r = _try(lambda: elem_link.PushOrder(nid, order))
            if r[0] == "ok":
                link = elem_link
                self.emit("ai-attack cp12: element _AiDataLink PushOrder(%d) ok" % nid)
        if link is None:
            iact = self.g("IActCommon")
            r = _try(lambda: iact.Instance.AiDataLink)
            if r[0] != "ok":
                raise RuntimeError("AiDataLink: %s" % r[1])
            link = r[1]
            self.emit("ai-attack cp12: AiDataLink resolved, about to PushOrder(%d)" % nid)
            r = _try(lambda: link.PushOrder(nid, order))
        if r[0] != "ok":
            raise RuntimeError("PushOrder: %s" % r[1])
        lines.append("PushOrder ok (tactical=%d assignment=%d)" % (nid, assignment_id))
        self.emit("ai-attack cp13: PushOrder ok")
        if r[0] != "ok":
            raise RuntimeError("PushOrder: %s" % r[1])
        lines.append("PushOrder ok (tactical=%d assignment=%d)" % (nid, assignment_id))
        self.emit("ai-attack cp13: PushOrder ok")

        self.collect_state()
        return lines

    # ---------------------------------------------------------------
    # steer: push a Transit assignment (move to a waypoint) to an AI
    # element via AiDataLink, WITHOUT an Engage. Replicates the engine's
    # Transit(who, where, speed) model + Order(operator, tactical, id)
    # pipeline, exactly like do_ai_attack does for Engage.
    # Usage: {"action":"steer","id":N,"lat":..,"lon":..,"speed":..}
    # ---------------------------------------------------------------

    def _resolve_ai_who(self, nid, kv, lines, tag="ai-attack"):
        """Resolve the AI element reference (the Who of an assignment).
        Returns ("ok", element_ref) or ("err", reason).
        Same resolution ladder as do_ai_attack: _InformationElemenet first,
        then _SelfInfo.Element, then scan for an Element-typed key."""
        who = ("err", "no element ref for who (no _InformationElemenet/_SelfInfo in /%d/)" % nid)
        iele = kv.get("_InformationElemenet")
        if iele is not None:
            who = ("ok", iele)
            return who
        sinfo = kv.get("_SelfInfo")
        if sinfo is not None:
            r = _try(lambda: sinfo.Element)
            if r[0] == "ok" and r[1] is not None:
                who = ("ok", r[1])
                return who
        for k, v in sorted(kv.items()):
            tn = type(v).__name__
            if "Element" in tn:
                who = ("ok", v)
                return who
            if "Information" in tn:
                r = _try(lambda: getattr(v, "Element", None))
                if r[0] == "ok" and r[1] is not None:
                    who = ("ok", r[1])
                    return who
        return who

    def do_steer(self, cmd):
        lines = []
        try:
            nid = int(cmd["id"])
        except Exception:
            raise RuntimeError("steer ID")
        lines.append("steer element id=%d" % nid)

        kv = self._element_namespace(nid)
        if kv is None:
            self.emit("steer cp0x: no /%d/ namespace on this host - skipping (multi-host safe)" % nid)
            return None
        self.emit("steer cp0a: namespace ok (%d keys)" % len(kv))

        who = self._resolve_ai_who(nid, kv, lines, "steer")
        if who[0] != "ok" or who[1] is None:
            raise RuntimeError("no element ref in /%d/ namespace: %s" % (nid, who[1]))
        lines.append("who resolved (%s)" % _desc(who[1], 60))
        self.emit("steer cp0b: who resolved (%s)" % _desc(who[1], 60))

        operator_id = self._host_operator_id(lines)
        if operator_id is None:
            raise RuntimeError("operator id unresolved via host client")
        lines.append("operator_id=%d" % operator_id)
        self.emit("steer cp0c: operator_id=%d" % operator_id)

        lat = float(cmd["lat"])
        lon = float(cmd["lon"])
        speed = float(cmd.get("speed", 15.0))
        gc = self.g("GeoCord")
        if gc is None:
            raise RuntimeError("GeoCord unavailable")
        target_geo = gc(lat, lon)
        aitools = self.g("AITools")
        if aitools is None:
            raise RuntimeError("AITools unavailable")
        r = _try(lambda: aitools.SearchPattern.Nothing)
        if r[0] != "ok":
            raise RuntimeError("AITools.SearchPattern.Nothing: %s" % r[1])
        r = _try(lambda: aitools.SearchArea(target_geo, 0, 0, 0.0, r[1]))
        if r[0] != "ok":
            raise RuntimeError("AITools.SearchArea: %s" % r[1])
        dest = r[1]
        lines.append("destination lat=%s lon=%s speed=%s" % (lat, lon, speed))
        self.emit("steer cp1: destination resolved (SearchArea at target)")

        transit_cls = self.g("Transit")
        if transit_cls is None:
            raise RuntimeError("Transit class unavailable")
        # Transit ctor takes (who, dest, TransitSpeed enum), not a float.
        # Verified enum members (mnw.scenarios.dll .TransitSpeed):
        #   Silent, Cruise, High   (NO "Low" — that was the pre-verify bug).
        ts_cls = self.g("TransitSpeed")
        if ts_cls is None:
            # TransitSpeed is a NESTED enum (Transit.TransitSpeed), not a
            # module-level name — the IL type lives at
            # mnw.Scenarios.Missions.Assignments.Transit+TransitSpeed. Fall
            # back to the attribute on the Transit class itself.
            r = _try(lambda: getattr(transit_cls, "TransitSpeed"))
            if r[0] != "ok" or r[1] is None:
                raise RuntimeError("TransitSpeed unavailable (module attr and Transit.TransitSpeed both failed): %s" % r[1])
            ts_cls = r[1]
        ts_val = str(cmd.get("transit_speed", "High")).strip()
        if ts_val not in ("Silent", "Cruise", "High"):
            ts_val = "High"
        r = _try(lambda: getattr(ts_cls, ts_val))
        if r[0] != "ok":
            raise RuntimeError("TransitSpeed.%s: %s" % (ts_val, r[1]))
        ts = r[1]
        lines.append("transit_speed=%s" % ts_val)
        self.emit("steer cp1a: TransitSpeed.%s resolved" % ts_val)
        self.emit("steer cp2: about to call Transit(who, dest, %s)" % ts_val)
        r = _try(lambda: transit_cls(who[1], dest, ts))
        if r[0] != "ok":
            raise RuntimeError("Transit constructor: %s" % r[1])
        transit = r[1]
        self.emit("steer cp3: Transit constructor returned")
        r = _try(lambda: int(transit.ID))
        if r[0] != "ok":
            raise RuntimeError("transit.ID: %s" % r[1])
        assignment_id = r[1]
        lines.append("Transit created (assignment_id=%d)" % assignment_id)
        self.emit("steer cp4: Transit.ID=%d" % assignment_id)

        mission = self.active_mission()
        if mission is not None:
            r = _try(lambda: int(mission.GetOperationType(assignment_id)))
            diag = "GetOperationType(%d)=%s" % (assignment_id, r[1] if r[0] == "ok" else r[1])
            lines.append(diag)
            self.emit("steer cp5: %s" % diag)

        if cmd.get("registry_only"):
            lines.append("REGISTRY-ONLY: stopped before Order/PushOrder (assignment_id=%d)" % assignment_id)
            self.collect_state()
            return lines

        order_cls = self.g("Order")
        if order_cls is None:
            raise RuntimeError("Order unavailable")
        self.emit("steer cp6: about to Order(operator=%d, tactical=%d, assignment=%d)" % (operator_id, nid, assignment_id))
        r = _try(lambda: order_cls(operator_id, nid, assignment_id))
        if r[0] != "ok":
            raise RuntimeError("Order constructor: %s" % r[1])
        order = r[1]
        self.emit("steer cp7: Order constructed")

        link = None
        elem_link = kv.get("_AiDataLink")
        if elem_link is not None:
            r = _try(lambda: elem_link.PushOrder(nid, order))
            if r[0] == "ok":
                link = elem_link
                self.emit("steer cp8: element _AiDataLink PushOrder(%d) ok" % nid)
        if link is None:
            iact = self.g("IActCommon")
            r = _try(lambda: iact.Instance.AiDataLink)
            if r[0] != "ok":
                raise RuntimeError("AiDataLink: %s" % r[1])
            link = r[1]
            self.emit("steer cp8: AiDataLink resolved, about to PushOrder(%d)" % nid)
            r = _try(lambda: link.PushOrder(nid, order))
        if r[0] != "ok":
            raise RuntimeError("PushOrder: %s" % r[1])
        lines.append("PushOrder ok (tactical=%d assignment=%d)" % (nid, assignment_id))
        self.emit("steer cp9: PushOrder ok")

        self.collect_state()
        return lines

    # ---------------------------------------------------------------
    # wc-dump: WeaponController internal state of an AI element.
    # Answers: which BaseCategory / AttackRanges / AttackSide combos do
    # the launchers expose, what the current assignment's Domain /
    # Where.Orientation is, and what munitions are in the magazine.
    # (mirrors the CategorizeWeaponSystems/Fire data from mnw.unity)
    # ---------------------------------------------------------------

    def do_wc_dump(self, cmd):
        lines = []
        try:
            nid = int(cmd["id"])
        except Exception:
            raise RuntimeError("wc-dump ID")
        lines.append("wc-dump element id=%d" % nid)

        kv = self._element_namespace(nid)
        if kv is None:
            self.emit("wc-dump cp0x: no /%d/ namespace on this host - skipping (multi-host safe)" % nid)
            return None
        lines.append("namespace ok (%d keys)" % len(kv))

        def fmt_enum(v):
            if v is None:
                return "None"
            try:
                iv = int(v)
            except Exception:
                return _desc(v, 60)
            try:
                name = str(v)
            except Exception:
                name = str(iv)
            return "%s(%d)" % (name, iv)

        # ---- 1. assignment surface -----------------------------------
        # ONLY the safe access patterns already proven in ai_state /
        # do_ai_attack / General_Behaviour_logic.py:456 (the game itself
        # reads _CurrentAssignment.Where.Orientation / .Domain for Fire).
        # Do NOT reach into FireControl/WeaponController internals here —
        # those private getters crash natively (verified 2026-08-14, game
        # hard-crashed mid-wc-dump on the DDG host).
        asg = kv.get("_CurrentAssignment")
        if asg is not None:
            lines.append("assignment: type=%s" % _desc(type(asg), 60))
            r = _try(lambda: asg.ID)
            if r[0] == "ok":
                lines.append("  asg.ID=%s" % _desc(r[1], 40))
            else:
                lines.append("  asg.ID: %s" % r[1])
            r = _try(lambda: asg.Domain)
            if r[0] == "ok":
                lines.append("  asg.Domain=%s" % fmt_enum(r[1]))
            else:
                lines.append("  asg.Domain: %s" % r[1])
            r = _try(lambda: asg.Where)
            if r[0] == "ok" and r[1] is not None:
                w = r[1]
                r2 = _try(lambda: w.Orientation)
                if r2[0] == "ok":
                    lines.append("  asg.Where.Orientation=%s" % fmt_enum(r2[1]))
                else:
                    lines.append("  asg.Where.Orientation: %s" % r2[1])
                r2 = _try(lambda: w.TargetPoint)
                if r2[0] == "ok" and r2[1] is not None:
                    lines.append("  asg.Where.TargetPoint present")
            else:
                lines.append("  asg.Where: %s" % (r[1] if r[0] != "ok" else "None"))
            for attr in ("Who", "Whom"):
                r = _try(lambda a=attr: getattr(asg, a))
                if r[0] == "ok" and r[1] is not None:
                    rid = _try(lambda x=r[1]: int(x.GetID))
                    lines.append("  asg.%s=%s" % (attr, rid[1] if rid[0] == "ok" else _desc(r[1], 40)))
        else:
            lines.append("  no _CurrentAssignment in namespace")

        # ---- 1b. LIVE client assignment --------------------------------
        # NOT dumped from the host's live `client` object. The host `client`
        # is a DIFFERENT element than the target /N/ namespace, and reaching
        # into its live _FireControl/_CurrentAssignment objects hangs the
        # Unity main thread (verified 2026-08-15: wc-dump on the DDG host
        # froze the game; last log line `cp: mission done`, Player.log same
        # nanosecond, mnw 192% CPU). Only the shared-Blackboard kv snapshot
        # above is safe to read from any host.

        # ---- 2. element identity -------------------------------------
        info = kv.get("_Information")
        if info is not None:
            r = _try(lambda: info.Category)
            lines.append("  element Category=%s" % (fmt_enum(r[1]) if r[0] == "ok" else r[1]))
            r = _try(lambda: int(info.CountryID))
            lines.append("  element CountryID=%s" % (r[1] if r[0] == "ok" else r[1]))

        # ---- 3. WeaponController internals -----------------------------
        # INTENTIONALLY NOT dumped: _BaseCategories / _AttackRanges /
        # _AttackSides / _WeaponSystems / _*Categorized / _AttackRangeInformations
        # / _AmmunitionStorage all crashed natively on the DDG host
        # (2026-08-14, game hard-crash mid-wc-dump). Those private C#
        # getters are unsafe to introspect from the Python bridge.
        # The Fire args we need (asg.Domain, asg.Where.Orientation) are
        # captured above. Weapon-side category data stays with the static
        # IL disassembly (disasm/wc_all.il).

        # ---- 3b. launcher enumeration ------------------------------
        # INTENTIONALLY NOT dumped live: enumerating launchers means
        # `client._FireControl.Controller.Access[LauncherController]()`
        # on the HOST's live client — the exact Component-resolution path
        # that froze the Unity main thread (2026-08-15, mnw 192% CPU, both
        # logs stopped on the same nanosecond). AGENTS.md Z.80 rule: no
        # un-gated live Component/Access[] reads. Launcher inventory stays
        # a static-disassembly concern (disasm/wc_all.il).
        lines.append("  launcher inventory: static disassembly only (no live Access)")
        return lines

    # ---------------------------------------------------------------
    # loop
    # ---------------------------------------------------------------

    def _drain_dc_deferred(self):
        """Execute deferred AddBehaviour/RemoveBehaviour calls at tick START.

        These must run before the engine iterates its behaviour list.
        The freeze was caused by calling these mid-tick (concurrent
        modification of the tick listener list)."""
        q = self._dc_deferred
        self._dc_deferred = []
        for fn, desc in q:
            r = _try(fn)
            if r[0] != "ok":
                self.emit("dc deferred %s failed: %s" % (desc, r[1]))

    def tick(self):
        self.tick_count += 1
        if self.tick_count % max(1, int(self.cfg.get("tick_delay", 30))) != 0:
            return
        self._drain_dc_deferred()
        if getattr(self, "command_only", False):
            try:
                self.dispatch_orders()
            except Exception as e:
                self.note_error("tick_command_only", e)
            return
        try:
            if self.active_mission() is None:
                self.emit("no active mission - stopping")
                self.finish()
                return
        except Exception:
            pass
        # Throttle the heavy state collection: state_every controls how many
        # tick_delay cycles do a full C# collect (state_every=1 == old
        # behavior). Between full collects we only dispatch orders (cheap file
        # reads) so the probe does not stall the Unity host every cycle.
        state_every = max(1, int(self.cfg.get("state_every", 1)))
        if self.tick_count % state_every == 0:
            try:
                self.collect_state()
            except Exception as e:
                self.note_error("tick", e)
        try:
            self.dispatch_orders()
        except Exception as e:
            self.note_error("tick_orders", e)
        if self.tick_count % max(1, int(self.cfg.get("heartbeat_every", 120))) == 0:
            self.emit("tick=%d states=%d" % (self.tick_count, self.state_count))

    def host_label(self):
        try:
            if self.host is None:
                return "-"
            return "%s|%s" % (self.host.get("__name__", "?"), self.host.get("__file__", "?"))
        except Exception:
            return "?"

    def begin(self):
        self.emit("============================================")
        self.emit("SHIP PROBE START")
        self.emit("log_dir=%s" % self.log_dir)
        self.emit("config=%s" % json.dumps(self.cfg))
        host_keys = []
        if self.host is not None:
            for k in _HOST_KEYS + ("__name__", "__file__"):
                if k in self.host:
                    host_keys.append(k)
        self.emit("host=%s keys=%s" % (self.host_label(), host_keys))
        self.emit("caller=%s" % _caller_file())
        host_has = {}
        if self.host is not None:
            for k in ("IActCommon", "ActCommon", "IPrepCommon", "_Controller",
                      "client", "clr", "ScenarioManager", "_Navigation", "Blackboard"):
                host_has[k] = k in self.host
        self.emit("host globals: %s" % json.dumps(host_has))
        bb = self._blackboard_storage()
        if bb is None:
            self.emit("blackboard: not importable")
        else:
            try:
                n = len(bb)
            except Exception:
                n = -1
            hits = []
            try:
                for k in bb:
                    if isinstance(k, str) and k.endswith("_CoordinatesManager"):
                        hits.append(k)
                    if len(hits) >= 5:
                        break
            except Exception:
                pass
            self.emit("blackboard: %d keys, cm_keys=%s" % (n, hits))
        cm = self.coordinates_manager()
        self.emit("coordinates_manager: %s" % ("ok" if cm is not None else "None"))
        am = self.active_mission()
        self.emit("active_mission: %s" % ("ok" if am is not None else "None"))
        ck = self.clock_manager()
        self.emit("clock_manager: %s" % ("ok" if ck is not None else "None"))
        info = self.host_get("_Information")
        if info is None:
            self.emit("no _Information in host - releasing lock for a real element script")
            raise RuntimeError("no _Information in host (contextless module)")
        det = self.detect_player()
        self.emit("player detection: %s" % json.dumps(det))
        self._warmup_clr()
        if getattr(self, "command_only", False):
            self.emit("SHIP PROBE READY (command-only mode: no state writes, no discovery)")
            return
        self.discovery_run()
        self.api_probe_run()
        self.emit("SHIP PROBE READY (state writes for player only)")

    def _warmup_clr(self):
        """Import the CLR namespaces used by ai-attack/steer at probe START
        (not lazily at command time). Lazy __import__ of the Assignments module
        from inside an element host's command dispatch was observed to hang
        (CLR import lock vs. the game's main-thread marshal). Pre-importing
        here means the command path only ever does dict/attr lookups.
        Mirrors the EOT-enum warmup in _eot()."""
        for name in ("Transit", "TransitSpeed", "Engage", "ASW", "AITools",
                     "ElementTools", "Order", "GeoCord", "IActCommon", "IPrepCommon"):
            try:
                v = self.g(name)
                self.emit("clr_warmup %s=%s" % (name, _desc(v, 70) if v is not None else "None"))
            except Exception as e:
                self.emit("clr_warmup %s ERROR: %s: %s" % (name, type(e).__name__, e))
        try:
            tr = self.g("Transit")
            if tr is not None:
                r = _try(lambda: getattr(tr, "TransitSpeed"))
                self.emit("clr_warmup Transit.TransitSpeed=%s" % (_desc(r[1], 70) if r[0] == "ok" else "ERR:" + str(r[1])))
                if r[0] == "ok" and r[1] is not None:
                    for m in ("Silent", "Cruise", "High"):
                        r2 = _try(lambda m=m: getattr(r[1], m))
                        self.emit("clr_warmup Transit.TransitSpeed.%s=%s" % (m, "ok" if r2[0] == "ok" else str(r2[1])))
        except Exception as e:
            self.emit("clr_warmup Transit.TransitSpeed ERROR: %s: %s" % (type(e).__name__, e))

    def finish(self):
        self.emit("SHIP PROBE END (states=%d)" % self.state_count)
        self.release_lock()
        self.log.close()

    def release_lock(self):
        try:
            p = os.path.join(self.log_dir, _LOCK_NAME)
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass


def _acquire_lock():
    try:
        dirs = _resolve_log_dirs(_load_config())
        if not dirs:
            return None
        path = os.path.join(dirs[0], _LOCK_NAME)
        if os.path.exists(path):
            try:
                if time.time() - os.path.getmtime(path) < _LOCK_STALE_S:
                    return None
            except Exception:
                return None
            try:
                os.remove(path)
            except Exception:
                return None
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, ("%s\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        except Exception:
            pass
        os.close(fd)
        return path
    except Exception:
        return None


_HOSTS_LOGGED = set()


def _caller_file():
    """Best-effort file name of the host script that called ship_probe_tick.
    __name__/__file__ in the MNW host globals are literal '<module>', so we
    walk the Python stack to find the caller instead."""
    try:
        import sys
        fr = sys._getframe(2)
        while fr is not None:
            fn = getattr(fr, "f_code", None)
            name = getattr(fn, "co_filename", None) if fn is not None else None
            if name and "ship_probe" not in str(name):
                return str(name).split("/")[-1]
            fr = fr.f_back
    except Exception:
        pass
    return "?"


def _gate_reject_log(host, result, before_keys=False):
    """One-shot per-host diagnostic line so every host that reaches the
    piggyback is visible in the log (last-writer gate file only ever showed the
    winning host). Each host is a separate interpreter, so a module-level set
    records one line per host for its whole lifetime."""
    label = "?"
    if host is not None:
        label = "%s|%s" % (host.get("__name__", "?"), host.get("__file__", "?"))
    label = "%s caller=%s" % (label, _caller_file())
    key = label
    if key in _HOSTS_LOGGED:
        return
    _HOSTS_LOGGED.add(key)
    cm = _global_cm()
    cmd = "none" if cm is None else "ok"
    pid = "none"
    ctrl = "none"
    if before_keys:
        cmd = "n/a (no host keys)"
    elif cm is not None:
        try:
            pp = cm.Player
            if pp is None:
                pid = "Player None"
            else:
                pid = _desc(getattr(pp, "GetID", None), 20)
                try:
                    cctrl = pp.Controller
                    ctrl = "ok" if cctrl is not None else "Controller None"
                except Exception as e:
                    ctrl = "ERR %s" % _desc(e, 60)
        except Exception as e:
            pid = "ERR %s" % _desc(e, 60)
    nkeys = 0
    try:
        from pybt.bb.blackboard import Blackboard
        nkeys = len(Blackboard.storage)
    except Exception:
        pass
    dirs = _resolve_log_dirs(_load_config())
    for p in dirs:
        try:
            with io.open(os.path.join(p, "ship_probe_log.txt"), "a", encoding="utf-8") as f:
                f.write("%s GATE-REJECT host=%s result=%s cm=%s player=%s controller=%s storage_keys=%d\n" % (
                    time.strftime("%H:%M:%S"), label, result, cmd, pid, ctrl, nkeys))
            break
        except Exception:
            continue


def ship_probe_tick(host=None):
    global _probe, _HOST, _LOCK
    if _probe is not None:
        if _probe is False:
            return
        try:
            _probe.tick()
        except Exception:
            pass
        return
    if host is None:
        return
    if not any(k in host for k in _HOST_KEYS):
        # contextless module (operational_ai.py or similar): never take the
        # lock here - a real element script ticks right after and must win.
        _gate_reject_log(host, "no-host-keys", before_keys=True)
        return
    # Gate: probe targets the player via cm.Player.Controller. Every element
    # script that can resolve the player may run a probe. The LOCK decides who
    # is the full probe (writes state files); other hosts run command-only so
    # ai-attack/ns-dump still reach THEIR element namespace (each element host
    # owns a separate blackboard storage / interpreter).
    ok = _host_can_target_player(host)
    if ok is not True:
        _gate_reject_log(host, ok)
        return
    lock_path = _acquire_lock()
    try:
        _HOST = host
        _probe = _Probe(_load_config())
        if lock_path is None:
            _probe.command_only = True
        else:
            _LOCK = lock_path
        _probe.begin()
        _debug_console("ship_probe: started, log_dir=%s, host=%s, lock=%s, mode=%s" % (
            _probe.log_dir,
            host.get("__name__", "?") if host is not None else "-",
            lock_path if lock_path is not None else "-",
            "command-only" if getattr(_probe, "command_only", False) else "full"))
    except Exception:
        try:
            if lock_path is not None:
                os.remove(lock_path)
        except Exception:
            pass
        _LOCK = None
        _probe = None
        _debug_console("ship_probe: init FAILED")
        return
    try:
        _probe.tick()
    except Exception:
        pass


def _start_():
    ship_probe_tick()


def _random_tick_():
    ship_probe_tick()


def _stop_():
    global _probe, _HOST, _LOCK
    if isinstance(_probe, _Probe):
        try:
            _probe.finish()
        except Exception:
            pass
    _probe = None
    _HOST = None
    _LOCK = None
