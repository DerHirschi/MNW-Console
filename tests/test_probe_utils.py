#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ship_probe util tests: pure helpers importable without the MNW runtime."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ship_probe  # noqa: E402


class DescTest(unittest.TestCase):
    def test_truncation(self):
        out = ship_probe._desc("x" * 300, limit=100)
        self.assertLessEqual(len(out), 103)
        self.assertTrue(out.endswith("..."))

    def test_repr_fallback(self):
        self.assertEqual(ship_probe._desc("abc", limit=10), "'abc'")

    def test_none(self):
        self.assertIn("None", ship_probe._desc(None))


class SafeNumTest(unittest.TestCase):
    def test_float_rounding(self):
        self.assertEqual(ship_probe._safe_num(3.14159, digits=2), 3.14)
        self.assertEqual(ship_probe._safe_num(5), 5.0)
        self.assertEqual(ship_probe._safe_num(0), 0.0)

    def test_raises(self):
        self.assertIsNone(ship_probe._safe_num("nope"))
        self.assertIsNone(ship_probe._safe_num(None))

    def test_json_default_float_like(self):
        class FakeDouble(object):
            def __float__(self):
                return 333.857
        self.assertEqual(ship_probe._json_default(FakeDouble()), 333.857)

    def test_json_default_str_fallback(self):
        class FakeVector(object):
            def __str__(self):
                return "(1, 2, 3)"
        self.assertEqual(ship_probe._json_default(FakeVector()), "(1, 2, 3)")


class GeoConvertTest(unittest.TestCase):
    class FakeGeo(object):
        Latitude = 10.0
        Longitude = 20.0

    class FakeGeoUnderscore(object):
        _lat = 30.0
        _longt = 40.0

    class FakeGeoLatLon(object):
        latitude = 51.5
        longitude = -0.12

    def test_geo_latlon_attributes(self):
        out = ship_probe._geo_latlon(self.FakeGeo())
        self.assertEqual(out, (10.0, 20.0))

    def test_geo_latlon_underscore(self):
        out = ship_probe._geo_latlon(self.FakeGeoUnderscore())
        self.assertEqual(out, (30.0, 40.0))

    def test_geo_latlon_lowercase_full(self):
        # GeoCord exposes native lowercase latitude/longitude fields — the
        # exact pair the in-game discovery (nav_geo) surfaced. This must win
        # without any MercatorToWGS84 call.
        out = ship_probe._geo_latlon(self.FakeGeoLatLon())
        self.assertEqual(out, (51.5, -0.12))

    def test_coord_to_ll_lowercase_full(self):
        ll = ship_probe._coord_to_ll(self.FakeGeoLatLon())
        self.assertEqual(ll, (51.5, -0.12))

    def test_geo_latlon_dict(self):
        out = ship_probe._geo_latlon({"Latitude": 10.0, "Longitude": 20.0})
        self.assertEqual(out, (10.0, 20.0))

    def test_coord_to_ll(self):
        ll = ship_probe._coord_to_ll(self.FakeGeo())
        self.assertEqual(ll, (10.0, 20.0))

    def test_coord_to_ll_bad_range(self):
        ll = ship_probe._coord_to_ll({"Latitude": 99.0, "Longitude": 20.0})
        self.assertIsNone(ll)

    def test_coord_to_ll_none(self):
        self.assertIsNone(ship_probe._coord_to_ll(None))

    def test_merc_calls_no_attr_collision(self):
        # _merc_calls() must remain callable: a same-named instance attribute
        # previously shadowed the method and made tick() fail with
        # "TypeError: NoneType is not callable".
        tmp = tempfile.mkdtemp(prefix="ship_probe_merc_")
        cfg = dict(log_dir=tmp, tick_delay=30, heartbeat_every=120,
                   console_log=False, require_player=True, target_element_id=0,
                   max_contacts=50, max_commands_per_cycle=10,
                   allow_commands=[], resolve_positions=True, state_every=10)
        probe = ship_probe._Probe(cfg)
        try:
            self.assertNotIn("_merc_calls", probe.__dict__)
            calls = probe._merc_calls()
            self.assertEqual(calls, [])
            calls2 = probe._merc_calls()
            self.assertIs(calls2, calls)
        finally:
            probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class LogDirTest(unittest.TestCase):
    def test_resolve_log_dirs_first_writable(self):
        d = tempfile.mkdtemp(prefix="ship_probe_dirs_test_")
        try:
            cfg = {"log_dir": d}
            dirs = ship_probe._resolve_log_dirs(cfg)
            self.assertEqual(dirs[0], d)
            self.assertIn(d, dirs)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_config_merges_defaults(self):
        cfg = ship_probe._load_config()
        for key, default in ship_probe._DEFAULTS.items():
            self.assertEqual(cfg[key], default)


class LogTest(unittest.TestCase):
    def test_writes_all_paths(self):
        d = tempfile.mkdtemp(prefix="ship_probe_log_test_")
        try:
            path = os.path.join(d, "x.txt")
            log = ship_probe._Log([path], console=False)
            log.w("hello")
            with open(path, "r", encoding="utf-8") as f:
                self.assertIn("hello", f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ActionsTest(unittest.TestCase):
    def test_actions_tuple(self):
        self.assertEqual(ship_probe._Probe._ACTIONS,
                         ("helm", "planes", "plot", "clear-plot", "report", "probe", "ai-attack", "detected", "wc-dump", "steer", "ns-dump", "asg", "ai-contacts", "sd-dump", "tanks", "env", "alarm", "sonctl", "tracker", "masts", "explore", "tracker-new", "dc", "ai-state"))

    def test_eot_names(self):
        self.assertEqual(ship_probe._EOT_NAMES,
                         ("Stop", "Ahead13", "Ahead23", "AheadStd", "AheadFull", "AheadFlank",
                          "Astern13", "Astern23", "AsternFull", "AsternEmer"))


class DispatchOrdersTest(unittest.TestCase):
    """Queue ownership: full probe runs + clears orders, command-only runs only
    element-scoped actions and never clears (avoids re-running heavy tanks/env
    native calls across every element host -> native crash)."""

    def _probe(self, tmp, command_only=False):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10,
                   allow_commands=list(ship_probe._Probe._ACTIONS),
                   resolve_positions=False, state_every=10)
        probe = ship_probe._Probe(cfg)
        probe.command_only = command_only
        probe.host = {"__name__": "sub", "__file__": "sub.py"}
        return probe

    def _write_orders(self, tmp, commands):
        path = os.path.join(tmp, "ship_orders.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({"commands": commands}, f)

    def _read_orders(self, tmp):
        path = os.path.join(tmp, "ship_orders.json")
        if not os.path.exists(path):
            return None
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("commands") or []

    def _read_results(self, tmp):
        path = os.path.join(tmp, "ship_results.json")
        if not os.path.exists(path):
            return None
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("results") or []

    def test_full_probe_runs_all_and_clears_processed(self):
        tmp = tempfile.mkdtemp(prefix="dispatch_full_")
        probe = None
        try:
            self._write_orders(tmp, [
                {"cmdid": 0, "action": "tanks"},
                {"cmdid": 1, "action": "env"},
                {"cmdid": 2, "action": "ai-attack"},
                {"cmdid": 3, "action": "helm", "course": 90},
            ])
            probe = self._probe(tmp)
            probe.dispatch_orders()
            res = self._read_results(tmp)
            self.assertEqual(sorted(int(r["cmdid"]) for r in res), [0, 1, 2, 3])
            self.assertEqual(self._read_orders(tmp), [])
            for r in res:
                self.assertIn("ok", r)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_probe_keeps_unprocessed_orders(self):
        tmp = tempfile.mkdtemp(prefix="dispatch_keep_")
        probe = None
        try:
            self._write_orders(tmp, [
                {"cmdid": 0, "action": "tanks"},
                {"cmdid": 1, "action": "env"},
                {"cmdid": 2, "action": "planes"},
                {"cmdid": 3, "action": "planes"},
            ])
            probe = self._probe(tmp)
            probe.cfg = dict(probe.cfg, max_commands_per_cycle=2)
            probe.dispatch_orders()
            remaining = [int(c["cmdid"]) for c in self._read_orders(tmp)]
            self.assertEqual(remaining, [2, 3])
            self.assertEqual(sorted(int(r["cmdid"]) for r in self._read_results(tmp)), [0, 1])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_command_only_runs_element_actions_only_no_clear(self):
        tmp = tempfile.mkdtemp(prefix="dispatch_cmdonly_")
        probe = None
        try:
            self._write_orders(tmp, [
                {"cmdid": 0, "action": "tanks"},
                {"cmdid": 1, "action": "ai-attack"},
                {"cmdid": 2, "action": "env"},
                {"cmdid": 3, "action": "ns-dump", "id": 13},
            ])
            probe = self._probe(tmp, command_only=True)
            probe.dispatch_orders()
            res = self._read_results(tmp)
            self.assertEqual(sorted(int(r["cmdid"]) for r in res), [1, 3])
            self.assertEqual([int(c["cmdid"]) for c in self._read_orders(tmp)], [0, 1, 2, 3])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class AiAttackGuardTest(unittest.TestCase):
    """Pure-Python guard paths of do_ai_attack (no engine needed)."""

    def _probe(self, tmp, allow=()):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=list(allow),
                   resolve_positions=True, state_every=10)
        probe = ship_probe._Probe(cfg)
        probe.host = {"__name__": "sub", "__file__": "sub.py",
                      "_Information": _FakeInfo(),
                      "client": type("C", (), {"country_id": 183})()}
        return probe

    def test_missing_id(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("ai-attack",))
            res = probe.do_command({"action": "ai-attack"})
            self.assertFalse(res["ok"])
            self.assertIn("ai-attack ID", res["result"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_denied_when_not_allowed(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("helm",))
            res = probe.do_command({"action": "ai-attack", "id": 13})
            self.assertFalse(res["ok"])
            self.assertIn("denied", res["result"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_namespace(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            saved = _install_fake_pybt({})
            probe = self._probe(tmp, allow=("ai-attack",))
            res = probe.do_command({"action": "ai-attack", "id": 13})
            self.assertIsNone(res, "host without the target namespace must skip, not error")
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_element_namespace_groups_keys(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_Information": object(), "/13/_Navigation": object(),
                       "/6/_Information": object()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            kv = probe._element_namespace(13)
            self.assertEqual(sorted(kv.keys()), ["_Information", "_Navigation"])
            self.assertIsNone(probe._element_namespace(99))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_operator_id_via_host_information_country(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("ai-attack",))
            lines = []
            cid = probe._host_operator_id(lines)
            self.assertEqual(cid, 183)
            self.assertTrue(any("_Information.CountryID=183" in l for l in lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_operator_id_no_host_information(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.host = {"__name__": "sub", "__file__": "sub.py", "client": {}}
            lines = []
            cid = probe._host_operator_id(lines)
            self.assertIsNone(cid)
            self.assertTrue(any("no host _Information" in l for l in lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_element_country_via_information_elemenet(self):
        class FakeElement(object):
            def __init__(self):
                self.Operator = type("Op", (), {"CountryID": 183})()

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("ai-attack",))
            lines = []
            cid = probe._element_country({"_InformationElemenet": FakeElement()}, None, lines)
            self.assertEqual(cid, 183)
            self.assertTrue(any("country via" in l for l in lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_element_country_dumps_surfaces_on_failure(self):
        class FakeWho(object):
            def __init__(self):
                self.MembersOnly = 1

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        try:
            probe = self._probe(tmp, allow=("ai-attack",))
            lines = []
            cid = probe._element_country({}, FakeWho(), lines)
            self.assertIsNone(cid)
            self.assertTrue(any("members:" in l and "MembersOnly" in l for l in lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_registry_only_stops_before_pushorder(self):
        class FakeLink(object):
            def __init__(self):
                self.pushes = []

            def PushOrder(self, tactical, order):
                self.pushes.append((tactical, order))

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        link = FakeLink()
        saved = None
        try:
            class FakeWho(object):
                pass

            class FakeAsg(object):
                Who = FakeWho()
                Whom = FakeWho()

            class FakeElement(object):
                Operator = type("Op", (), {"CountryID": 183})()

            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 1500.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.g = _engine_g(link)
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13, "registry_only": True})
            self.assertTrue(res["ok"], res["result"])
            self.assertTrue(any("REGISTRY-ONLY" in l for l in res["detail"]))
            self.assertEqual(link.pushes, [])
            with io.open(os.path.join(tmp, ship_probe._LOG_NAME), "r", encoding="utf-8") as f:
                log = f.read()
            self.assertIn("cp9: GetAssignment[Engage] returned", log)
            self.assertNotIn("cp10:", log)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_attack_pushes_order(self):
        class FakeLink(object):
            def __init__(self):
                self.pushes = []

            def PushOrder(self, tactical, order):
                self.pushes.append((tactical, order))

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        link = FakeLink()
        saved = None
        try:
            class FakeWho(object):
                pass

            class FakeAsg(object):
                Who = FakeWho()
                Whom = FakeWho()

            class FakeElement(object):
                Operator = type("Op", (), {"CountryID": 183})()

            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 1500.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.g = _engine_g(link)
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13})
            self.assertTrue(res["ok"], res["result"])
            self.assertTrue(any("PushOrder ok" in l for l in res["detail"]))
            self.assertEqual(len(link.pushes), 1)
            self.assertEqual(link.pushes[0][0], 13)
            with io.open(os.path.join(tmp, ship_probe._LOG_NAME), "r", encoding="utf-8") as f:
                log = f.read()
            self.assertIn("cp13: PushOrder ok", log)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


    def test_ai_attack_refuses_when_not_tracked(self):
        class FakeAsg(object):
            Who = type("W", (), {})()
            Whom = type("W", (), {})()

        class FakeElement(object):
            Operator = type("Op", (), {"CountryID": 183})()

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 25000.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.g = _engine_g(None)
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            res = probe.do_command({"action": "ai-attack", "id": 13})
            self.assertFalse(res["ok"])
            self.assertIn("no contact on player", res["result"])
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ai_attack_allow_untracked_overrides(self):
        class FakeAsg(object):
            Who = type("W", (), {})()
            Whom = type("W", (), {})()

        class FakeElement(object):
            Operator = type("Op", (), {"CountryID": 183})()

        class FakeLink(object):
            def __init__(self):
                self.pushes = []

            def PushOrder(self, tactical, order):
                self.pushes.append((tactical, order))

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        link = FakeLink()
        saved = None
        try:
            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 25000.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.g = _engine_g(link)
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13, "allow_untracked": True})
            self.assertTrue(res["ok"], res["result"])
            self.assertEqual(len(link.pushes), 1)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ai_attack_domain_surface(self):
        class FakeAsg(object):
            Who = type("W", (), {})()
            Whom = type("W", (), {})()

        class FakeElement(object):
            Operator = type("Op", (), {"CountryID": 183})()

        class FakeLink(object):
            def __init__(self):
                self.pushes = []

            def PushOrder(self, tactical, order):
                self.pushes.append((tactical, order))

        captured = {}

        class FakeEngageCapture(_FakeEngageCls):
            def __init__(self, who, where, whom, base_cat, elev, spd):
                captured["base_cat"] = base_cat

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        link = FakeLink()
        saved = None
        try:
            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 25000.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            g = _engine_g(link)
            base_registry = g("ElementTools").BaseCategory
            orig = g
            probe.g = lambda name: (FakeEngageCapture if name == "Engage" else orig(name))
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13,
                                    "allow_untracked": True, "domain": "Surface"})
            self.assertTrue(res["ok"], res["result"])
            self.assertEqual(captured["base_cat"], base_registry.Surface)
            self.assertEqual(len(link.pushes), 1)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ai_attack_domain_invalid_errors(self):
        class FakeAsg(object):
            Who = type("W", (), {})()
            Whom = type("W", (), {})()

        class FakeElement(object):
            Operator = type("Op", (), {"CountryID": 183})()

        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 25000.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            g = _engine_g(None)
            orig = g
            probe.g = lambda name: orig(name)
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13,
                                    "allow_untracked": True, "domain": "Nope"})
            self.assertFalse(res["ok"])
            self.assertIn("BaseCategory.Nope", res["result"])
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ai_attack_aimpoint_uses_element_to_player_bearing(self):
        """The SearchArea orientation must be the bearing from THIS element to
        the player (Aimpoint fix 2026-08-16), NOT the player's own course —
        the engine's WeaponController.Fire uses Where.Orientation as TrueBearing."""
        class FakeAsg(object):
            Who = type("W", (), {})()
            Whom = type("W", (), {})()

        class FakeElement(object):
            Operator = type("Op", (), {"CountryID": 183})()

        class FakeLink(object):
            def __init__(self):
                self.pushes = []

            def PushOrder(self, tactical, order):
                self.pushes.append((tactical, order))

        class PlayerNavFar(object):
            def __init__(self):
                self.INS = type("INS", (), {
                    "GeoCoordinates": type("G", (), {"latitude": 13.7, "longitude": 120.0})(),
                    "TrueHeading": 45.0,
                    "TrueForwardSpeed": 6.0,
                })()
                self.DepthGauge = type("DG", (), {"Elevation": -60.0})()

        sink = {}
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        link = FakeLink()
        saved = None
        try:
            storage = {"/13/_CurrentAssignment": FakeAsg(),
                       "/13/_InformationElemenet": FakeElement(),
                       "/13/_ContactManager": _FakeContactManager({1: 15500.0}),
                       "/13/_Navigation": _FakeElementNav(13.5, 120.25)}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            probe.g = _engine_g(link, sink=sink)
            probe.player_navigation = lambda: PlayerNavFar()
            probe.active_mission = lambda: _FakeMission()
            probe.collect_state = lambda: None
            res = probe.do_command({"action": "ai-attack", "id": 13,
                                    "allow_untracked": True})
            self.assertTrue(res["ok"], res["result"])
            self.assertEqual(len(link.pushes), 1)
            _, expected_brg = ship_probe._range_bearing(13.5, 120.25, 13.7, 120.0)
            self.assertIsNotNone(expected_brg)
            self.assertIn("search_area", sink)
            self.assertEqual(sink["search_area"][3], expected_brg)
            self.assertNotEqual(expected_brg, 45.0)
            self.assertTrue(any("aimpoint:" in l and "orientation=%s" % expected_brg in l
                                for l in res["detail"]))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ai_contacts_dumps_per_element_tracks(self):
        tmp = tempfile.mkdtemp(prefix="ai_contacts_test_")
        probe = None
        saved = None
        try:
            storage = {
                "/13/_ContactManager": _FakeContactManager(
                    {7: 2000.0, 9: 55000.0}, cat={7: "Surface", 9: "Subsurface"},
                    prefix={7: "FFG", 9: "SSK"}, ident={7: "Friendly", 9: "Unknown"}),
                "/13/_Navigation": _FakeElementNav(),
                "/16/_ContactManager": _FakeContactManager({}),
                "/16/_Navigation": _FakeElementNav(14.0, 121.0),
            }
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-contacts",))
            res = probe.do_command({"action": "ai-contacts"})
            self.assertTrue(res["ok"], res["result"])
            detail = "\n".join(res["detail"])
            self.assertIn("contacts: element 13: 2 contacts", detail)
            self.assertIn("contacts: element 16: GetUsed ok (0 contacts)", detail)
            self.assertIn("cat 'Surface'", detail)
            self.assertIn("prefix 'FFG'", detail)
            self.assertIn("ident 'Friendly'", detail)
            self.assertIn("range_m=2000", detail)
            self.assertTrue(any("ai-contacts: done" in l for l in res["detail"]))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_track_probe_hit_reported(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_ContactManager": _FakeContactManager({7: 2000.0, 9: 55000.0}),
                       "/13/_Navigation": _FakeElementNav()}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            kv = probe._element_namespace(13)
            out, lines = probe._ai_track_probe(13, kv, (13.5, 120.25), [])
            self.assertTrue(out["found"])
            self.assertEqual(out["player_contact_id"], "7")
            self.assertAlmostEqual(out["player_contact_range_m"], 2000.0)
            self.assertTrue(any("HIT contact on player" in l for l in lines))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_track_probe_no_contact_manager(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            saved = _install_fake_pybt({})
            probe = self._probe(tmp, allow=("ai-attack",))
            out, lines = probe._ai_track_probe(13, {}, (13.5, 120.25), [])
            self.assertFalse(out["found"])
            self.assertIn("err", out)
            self.assertTrue(any("no _ContactManager" in l for l in lines))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_track_probe_empty_contact_list(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_ContactManager": _FakeContactManager({})}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("ai-attack",))
            kv = probe._element_namespace(13)
            out, lines = probe._ai_track_probe(13, kv, (13.5, 120.25), [])
            self.assertFalse(out["found"])
            self.assertTrue(any("NO contacts in _ContactManager" in l for l in lines))
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detected_reports_which_ai_track_player(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            # element 13 tracks player at 2000 m; element 11 has a track far away
            storage = {"/13/_ContactManager": _FakeContactManager({7: 2000.0}),
                       "/13/_Navigation": _FakeElementNav(13.5, 120.25),
                       "/11/_ContactManager": _FakeContactManager({3: 99000.0}),
                       "/11/_Navigation": _FakeElementNav(13.5, 120.25)}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("detected",))
            probe.player_navigation = lambda: _FakePlayerNav()
            res = probe.do_command({"action": "detected"})
            self.assertTrue(res["ok"], res["result"])
            self.assertIn("DETECTED elements: 13", res["result"])
            detail = "\n".join(res["detail"])
            self.assertIn("DETECTED by element 13", detail)
            self.assertIn("HIT contact on player", detail)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detected_not_detected_by_any(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            storage = {"/13/_ContactManager": _FakeContactManager({3: 99000.0}),
                       "/13/_Navigation": _FakeElementNav(13.5, 120.25)}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("detected",))
            probe.player_navigation = lambda: _FakePlayerNav()
            res = probe.do_command({"action": "detected"})
            self.assertTrue(res["ok"], res["result"])
            self.assertIn("NOT detected by any AI element", res["result"])
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detected_skips_player_namespace(self):
        tmp = tempfile.mkdtemp(prefix="ai_attack_test_")
        probe = None
        saved = None
        try:
            # player id is 15 (per _FakeInfo.GetID); its own contacts must be skipped
            storage = {"/15/_ContactManager": _FakeContactManager({7: 1.0}),
                       "/15/_Navigation": _FakeElementNav(13.5, 120.25)}
            saved = _install_fake_pybt(storage)
            probe = self._probe(tmp, allow=("detected",))
            probe.player_navigation = lambda: _FakePlayerNav()
            probe.detect_player = lambda: {"is_player": True, "player_id": 15, "id": 15}
            res = probe.do_command({"action": "detected"})
            self.assertTrue(res["ok"], res["result"])
            self.assertIn("NOT detected by any AI element", res["result"])
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class _FakeInfo(object):
    GetID = 15


def _install_fake_pybt(storage):
    """Install fake pybt.bb.blackboard module returning given Blackboard.storage.
    Returns dict of previously-installed modules for _uninstall_fake_pybt()."""
    FakeBB = type("FakeBB", (object,), {"storage": storage})
    fake_bb = type(sys)("blackboard")
    fake_bb.Blackboard = FakeBB
    fake_bb_mod = type(sys)("bb")
    fake_bb_mod.blackboard = fake_bb
    fake_pybt = type(sys)("pybt")
    fake_pybt.bb = fake_bb_mod
    saved = {}
    for name, mod in (("pybt", fake_pybt), ("pybt.bb", fake_bb_mod),
                      ("pybt.bb.blackboard", fake_bb)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _uninstall_fake_pybt(saved):
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _reset_bb_cache():
    ship_probe._BB_REF = None
    ship_probe._BB_CM = None


class _FakeInfo(object):
    GetID = 15
    Element = object()  # sentinel element
    CountryID = 183


class _FakeGeo(object):
    latitude = 13.5
    longitude = 120.25


class _FakeElementNav(object):
    def __init__(self, lat=13.5, lon=120.25):
        self.INS = type("INS", (), {
            "GeoCoordinates": type("G", (), {"latitude": lat, "longitude": lon})(),
        })()


class _FakeContactManager(object):
    """contacts: dict cid -> range (m) as the engine reports _Range.
    Optional cat/prefix/ident dicts add per-cid category/prefix/identity."""

    def __init__(self, contacts, cat=None, prefix=None, ident=None):
        self._contacts = dict(contacts)
        self._cat = cat or {}
        self._prefix = prefix or {}
        self._ident = ident or {}

    @property
    def GetUsed(self):
        return list(self._contacts.keys())

    def GetTrack(self, cid):
        if cid not in self._contacts:
            raise KeyError(cid)
        return type("Track", (), {"_Range": self._contacts[cid]})()

    def GetCategoryID(self, cid):
        if cid not in self._contacts:
            raise KeyError(cid)
        return self._cat.get(cid)

    def GetPrefix(self, cid):
        if cid not in self._contacts:
            raise KeyError(cid)
        return self._prefix.get(cid)

    def GetStandardIdentity(self, cid):
        if cid not in self._contacts:
            raise KeyError(cid)
        return self._ident.get(cid)


class _FakePlayerNav(object):
    def __init__(self):
        self.INS = type("INS", (), {
            "GeoCoordinates": _FakeGeo(),
            "TrueHeading": 45.0,
            "TrueForwardSpeed": 6.0,
        })()
        self.DepthGauge = type("DG", (), {"Elevation": -60.0})()


class _FakeEngageCls(object):
    ID = 4242

    def __init__(self, *args, **kwargs):
        pass


class _FakeOrderCls(object):
    def __init__(self, *args, **kwargs):
        pass


class _FakeMission(object):
    class GetAssignment(object):
        @staticmethod
        def __getitem__(cls):
            return lambda aid: object()

    def GetOperationType(self, aid):
        return 2


def _engine_g(link=None, sink=None):
    """Return a fake g(name) registry for the ai-attack engine path.
    When sink is a dict, SearchArea(*args) records its args under 'search_area'
    so tests can assert the aimpoint orientation passed to the engine."""

    class FakeAITools(object):
        class SearchPattern(object):
            Nothing = object()

        @staticmethod
        def SearchArea(*args):
            if sink is not None:
                sink["search_area"] = list(args)
            return object()

    class FakeETools(object):
        class BaseCategory(object):
            Subsurface = object()
            Surface = object()
            Air = object()

    class FakeICommon(object):
        class Instance(object):
            AiDataLink = None
            ScenarioManager = None

    registry = {
        "AITools": FakeAITools,
        "ElementTools": FakeETools,
        "Engage": _FakeEngageCls,
        "Order": _FakeOrderCls,
        "IActCommon": FakeICommon,
        "IPrepCommon": FakeICommon,
        "ActCommon": FakeICommon,
    }

    if link is not None:
        class _Instance(object):
            AiDataLink = link
            ScenarioManager = None
            CoordinatesManager = None
            ClockManager = None

        FakeICommon.Instance = _Instance
    return lambda name: registry.get(name)


class TickGateTest(unittest.TestCase):
    def setUp(self):
        self._orig = (ship_probe._probe, ship_probe._HOST, ship_probe._LOCK,
                      ship_probe._load_config)
        ship_probe._probe = None
        ship_probe._HOST = None
        ship_probe._LOCK = None
        _reset_bb_cache()

    def tearDown(self):
        ship_probe._stop_()
        ship_probe._probe, ship_probe._HOST, ship_probe._LOCK, ship_probe._load_config = self._orig
        _reset_bb_cache()

    def _cfg(self, tmp):
        return dict(
            log_dir=tmp, tick_delay=30, heartbeat_every=120, console_log=False,
            require_player=True, target_element_id=0, max_contacts=50,
            max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
            state_every=10)

    def _player_host(self):
        class FakeInfo(object):
            GetID = 15
            Controller = object()
        class FakeCM(object):
            Player = FakeInfo()  # Information-like with GetID + Controller
            PlayerGCID = "gcid-15"
        saved = _install_fake_pybt({"/15/_CoordinatesManager": FakeCM()})
        return saved

    def test_none_host_ignored(self):
        ship_probe.ship_probe_tick(None)
        self.assertIsNone(ship_probe._probe)
        self.assertIsNone(ship_probe._LOCK)

    def test_contextless_module_ignored(self):
        ship_probe.ship_probe_tick({"__name__": "<module>"})
        self.assertIsNone(ship_probe._probe)
        self.assertIsNone(ship_probe._LOCK)

    def test_element_host_starts_even_if_not_player(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tick_test_")
        saved = None
        try:
            ship_probe._load_config = lambda: self._cfg(tmp)
            # host element id (99) differs from player id (15): with the new
            # targeting design ANY element host that can resolve cm.Player
            # starts the probe (it targets the player via cm.Player.Controller).
            class FakeInfo(object):
                GetID = 99
                Controller = object()
            class FakeCM(object):
                Player = FakeInfo()  # resolvable player
            saved = _install_fake_pybt({"/15/_CoordinatesManager": FakeCM()})
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "client": {}, "_Navigation": object()}
            ship_probe.ship_probe_tick(host)
            self.assertIsInstance(ship_probe._probe, ship_probe._Probe)
            self.assertIsNotNone(ship_probe._LOCK)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            ship_probe._stop_()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_element_context_starts(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tick_test_")
        saved = None
        try:
            ship_probe._load_config = lambda: self._cfg(tmp)
            saved = self._player_host()
            host = {"__name__": "General_Behaviour_logic_submarine",
                    "__file__": "General_Behaviour_logic_submarine.py",
                    "_Information": _FakeInfo(), "client": {}, "_Navigation": object()}
            ship_probe.ship_probe_tick(host)
            self.assertIsInstance(ship_probe._probe, ship_probe._Probe)
            self.assertEqual(ship_probe._probe.host_label(),
                             "General_Behaviour_logic_submarine|General_Behaviour_logic_submarine.py")
            self.assertTrue(ship_probe._probe.discovery is not None)
            self.assertEqual(ship_probe._probe.discovery.get("host_script"),
                             "General_Behaviour_logic_submarine|General_Behaviour_logic_submarine.py")
            with open(os.path.join(tmp, ship_probe._LOG_NAME), "r", encoding="utf-8") as f:
                log = f.read()
            self.assertIn("SHIP PROBE START", log)
            self.assertIn("host=General_Behaviour_logic_submarine", log)
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            ship_probe._stop_()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_existing_probe_keeps_ticking(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tick_test_")
        saved = None
        try:
            ship_probe._load_config = lambda: self._cfg(tmp)
            saved = self._player_host()
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "client": {}, "_Navigation": object()}
            ship_probe.ship_probe_tick(host)
            ticks0 = ship_probe._probe.tick_count
            ship_probe.ship_probe_tick({"__name__": "operational_ai", "__file__": "op.py"})
            ship_probe.ship_probe_tick(host)
            self.assertGreater(ship_probe._probe.tick_count, ticks0)
            self.assertEqual(ship_probe._probe.host_label(), "sub|sub.py")
        finally:
            if saved:
                _uninstall_fake_pybt(saved)
            ship_probe._stop_()
            shutil.rmtree(tmp, ignore_errors=True)


class RuntimeResolutionTest(unittest.TestCase):
    class FakeCM(object):
        Player = _FakeInfo()  # Information-like with GetID (element id)
        PlayerGCID = "gcid-15"

    class FakeClient(object):
        def __init__(self, cm):
            self._CoordinatesManager = cm
            self._ScenarioManager = None
            self._CurrentTensionLevel = None

    def setUp(self):
        _reset_bb_cache()

    def tearDown(self):
        _reset_bb_cache()

    def _cfg(self, tmp):
        return dict(
            log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
            require_player=True, target_element_id=0, max_contacts=50,
            max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
            state_every=10)

    def test_coordinates_manager_via_client(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_res_")
        probe = None
        try:
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(),
                    "client": self.FakeClient(self.FakeCM()),
                    "_Navigation": object()}
            ship_probe._HOST = host
            probe = ship_probe._Probe(self._cfg(tmp))
            probe.host = host
            cm = probe.coordinates_manager()
            self.assertIs(cm, probe.host["client"]._CoordinatesManager)
            self.assertIs(probe.active_mission(), None)
        finally:
            if probe is not None:
                probe.finish()
            ship_probe._HOST = None
            shutil.rmtree(tmp, ignore_errors=True)

    def test_coordinates_manager_fallback_g(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_res_")
        probe = None
        try:
            class FakeAct(object):
                class Instance(object):
                    CoordinatesManager = self.FakeCM()
            ship_probe._HOST = None
            probe = ship_probe._Probe(self._cfg(tmp))
            self.assertIsNone(probe.host)
            g_orig = probe.g
            probe.g = lambda name: FakeAct if name == "ActCommon" else None
            cm = probe.coordinates_manager()
            self.assertIs(cm, FakeAct.Instance.CoordinatesManager)
            probe.g = g_orig
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detect_player_via_coordinates(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_res_")
        probe = None
        try:
            cm = self.FakeCM()
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(),
                    "client": self.FakeClient(cm),
                    "_Navigation": object()}
            probe = ship_probe._Probe(self._cfg(tmp))
            probe.host = host
            det = probe.detect_player()
            self.assertTrue(det["is_player"])
            self.assertEqual(det["source"], "CoordinatesManager.Player.GetID")
            self.assertEqual(det["player_id"], 15)
            self.assertEqual(det["id"], 15)
        finally:
            if probe is not None:
                probe.finish()
            ship_probe._HOST = None
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detect_player_identity(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_res_")
        probe = None
        try:
            class FakeInfoEl(object):
                GetID = 12
            class FakeCMEl(object):
                Player = FakeInfoEl()
                PlayerGCID = "gcid-12"
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": FakeInfoEl(),
                    "client": self.FakeClient(FakeCMEl()),
                    "_Navigation": object()}
            probe = ship_probe._Probe(self._cfg(tmp))
            probe.host = host
            det = probe.detect_player()
            self.assertTrue(det["is_player"])
            self.assertEqual(det["source"], "CoordinatesManager.Player.GetID")
            self.assertEqual(det["id"], 12)
        finally:
            if probe is not None:
                probe.finish()
            ship_probe._HOST = None
            shutil.rmtree(tmp, ignore_errors=True)

    def test_host_can_target_player(self):
        class FakeInfoEl(object):
            GetID = 12
            Controller = object()
        class FakeCMEl(object):
            Player = FakeInfoEl()
        saved = _install_fake_pybt({"/12/_CoordinatesManager": FakeCMEl()})
        try:
            self.assertTrue(ship_probe._host_can_target_player(
                {"__name__": "sub", "_Information": FakeInfoEl()}))
            self.assertTrue(ship_probe._host_can_target_player({}))
            self.assertIsNone(ship_probe._host_can_target_player(None))
        finally:
            _uninstall_fake_pybt(saved)
            _reset_bb_cache()

    def test_blackboard_scan_finds_coordinates_manager(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_res_")
        probe = None
        fake = self._fake_pybt_module()
        installed = self._fake_pybt_installed(fake)
        try:
            probe = ship_probe._Probe(self._cfg(tmp))
            probe.host = None
            cm = probe.coordinates_manager()
            self.assertIs(cm, fake[2].Blackboard.storage["/9/_CoordinatesManager"])
            am = probe.active_mission()
            self.assertIs(am, fake[2].Blackboard.storage["/9/_ScenarioManager"].ActiveMission)
        finally:
            if probe is not None:
                probe.finish()
            self._fake_pybt_uninstalled(installed)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_global_cm_rescans_until_found(self):
        # Blackboard.storage is a single mutable dict; a scan that finds nothing
        # must NOT be cached forever. Register the key only after the first scan.
        storage = {}
        saved = _install_fake_pybt(storage)
        try:
            self.assertIsNone(ship_probe._global_cm())
            self.assertIsNone(ship_probe._global_cm())
            storage["/12/_CoordinatesManager"] = self.FakeCM()
            cm = ship_probe._global_cm()
            self.assertIs(cm, storage["/12/_CoordinatesManager"])
            self.assertIs(cm, storage["/12/_CoordinatesManager"])
        finally:
            _uninstall_fake_pybt(saved)
            _reset_bb_cache()

    def test_global_cm_caches_found_result(self):
        cm = self.FakeCM()
        saved = _install_fake_pybt({"/12/_CoordinatesManager": cm})
        try:
            self.assertIs(ship_probe._global_cm(), cm)
            self.assertIs(ship_probe._global_cm(), cm)
        finally:
            _uninstall_fake_pybt(saved)
            _reset_bb_cache()

    def _fake_pybt_module(self):
        class FakeMission(object):
            ActiveMission = "mission-x"

        class FakeScenario(object):
            ActiveMission = FakeMission()

        class FakeBB(object):
            storage = {
                "/9/_CoordinatesManager": self.FakeCM(),
                "/9/_ScenarioManager": FakeScenario(),
                "/2/_Navigation": object(),
            }

        fake_bb = type(sys)("blackboard")
        fake_bb.Blackboard = FakeBB
        fake_bb_mod = type(sys)("bb")
        fake_bb_mod.blackboard = fake_bb
        fake_pybt = type(sys)("pybt")
        fake_pybt.bb = fake_bb_mod
        return (fake_pybt, fake_bb_mod, fake_bb)

    def _fake_pybt_installed(self, fake):
        fake_pybt, fake_bb_mod, fake_bb = fake
        saved = {}
        for name, mod in (("pybt", fake_pybt), ("pybt.bb", fake_bb_mod), ("pybt.bb.blackboard", fake_bb)):
            saved[name] = sys.modules.get(name)
            sys.modules[name] = mod
        return saved

    def _fake_pybt_uninstalled(self, installed):
        for name, saved in installed.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved


class _FakeTuple(object):
    def __init__(self, a, b):
        self.Item1 = a
        self.Item2 = b


class ContactsReadTest(unittest.TestCase):
    class FakeTrack(object):
        _Speed = 12.0
        _Range = 34000.0
        _Elevation = -5.0
        _Course = 270.0
        _RCPA = 1.5
        _TCPA = 22.0
        _BearingRate = 0.1
        _RelativeBearing = _FakeTuple(45.0, 0.0)
        _Bearing = 90.0

    class FakeCM(object):
        Count = 2
        used = [101, 202]

        @property
        def GetUsed(self):
            return self.used

        def GetCategoryID(self, cid):
            return "Submarine" if cid == 101 else "Ship"

        def GetPrefix(self, cid):
            return "TRK-%d" % cid

        def GetStandardIdentity(self, cid):
            return "Hostile" if cid == 101 else "Unknown"

        def GetTrack(self, cid):
            return ContactsReadTest.FakeTrack()

    class FakeClient(object):
        def __init__(self):
            self._ContactManager = ContactsReadTest.FakeCM()

    def _probe(self, tmp):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=True)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": self.FakeClient()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        return probe

    def test_read_contacts_via_client_manager(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_contacts_")
        probe = None
        try:
            probe = self._probe(tmp)
            out = probe.read_contacts()
            self.assertEqual(out["count"], 2)
            self.assertEqual(len(out["tracks"]), 2)
            self.assertEqual(out["tracks"][0]["category"], repr("Submarine"))
            self.assertEqual(out["tracks"][0]["identity"], repr("Hostile"))
            self.assertEqual(out["tracks"][1]["prefix"], repr("TRK-202"))
            self.assertEqual(out["tracks"][0]["speed"], 12.0)
            self.assertEqual(out["tracks"][0]["range"], 34000.0)
            self.assertEqual(out["tracks"][0]["relative_bearing"], [45.0, 0.0])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_contacts_checkpoint_trail(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_contacts_")
        probe = None
        try:
            probe = self._probe(tmp)
            probe.read_contacts()
            with open(os.path.join(tmp, "ship_probe_log.txt"), "r", encoding="utf-8") as f:
                trail = f.read()
            for marker in ("cp: contacts manager ok", "cp: contacts count=2",
                           "cp: contacts used ok", "cp: contacts track 0 ok",
                           "cp: contacts track 1 ok", "cp: contacts loop done"):
                self.assertIn(marker, trail)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_contacts_no_manager(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_contacts_")
        probe = None
        try:
            probe = self._probe(tmp)
            probe.host["client"] = type("EmptyClient", (object,), {})()
            out = probe.read_contacts()
            self.assertIn("err", out)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_contacts_prefers_player_controller(self):
        # The probe piggybacks on ANY element script (host), but the interesting
        # contacts belong to the PLAYER element. Player CM must win over host CM.
        tmp = tempfile.mkdtemp(prefix="ship_probe_contacts_")
        probe = None
        try:
            class FakePCM(object):
                Count = 1
                used = [777]
                @property
                def GetUsed(self):
                    return self.used
                def GetCategoryID(self, cid):
                    return "Submarine"
                def GetPrefix(self, cid):
                    return "SONAR-777"
                def GetStandardIdentity(self, cid):
                    return "Hostile"
                def GetTrack(self, cid):
                    return ContactsReadTest.FakeTrack()
            class FakePlayerFC(object):
                ContactManager = FakePCM()
            class FakeAccess(object):
                def __getitem__(self, t):
                    if t is not None and getattr(t, "__name__", "") == "FireControl":
                        return lambda: FakePlayerFC()
                    return None
            class FakePlayerInfo(object):
                GetID = 6
                Controller = type("Ctrl", (object,), {"Access": FakeAccess()})()
            class FakeCM(object):
                Player = FakePlayerInfo()
            probe = self._probe(tmp)
            probe.host["client"]._CoordinatesManager = FakeCM()
            # host-side FireControl/ContactManager present but must NOT win
            fake_fc = type("FireControl", (object,), {})
            orig_g = probe.g
            def g_wrap(name):
                if name == "FireControl":
                    return fake_fc
                return orig_g(name)
            probe.g = g_wrap
            out = probe.read_contacts()
            self.assertEqual(out["count"], 1)
            self.assertEqual(out["tracks"][0]["prefix"], repr("SONAR-777"))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class SonarReadTest(unittest.TestCase):
    """Tests for read_sonar() which now uses the SonarSystem tracker API
    (GetContactIDs/GetTrackerData) — the safe path, not StrongestContact."""

    class FakeContactID(object):
        def __init__(self, cid, desc=""):
            self.Item1 = cid
            self.Item2 = desc

    class FakeTrackerData(object):
        def __init__(self, bearing=90.0, rng=1500.0, sensor="LAB", track=0):
            self._Bearing = bearing
            self._Range = rng
            self._SensorID = sensor
            self._TrackID = track

    class FakeSonarSystem(object):
        def __init__(self, contacts=None):
            self._contacts = contacts if contacts is not None else [("S1", "sub_1")]
        def GetContactIDs(self):
            return [SonarReadTest.FakeContactID(cid, desc) for cid, desc in self._contacts]
        def GetTrackerData(self, cid):
            for c, d in self._contacts:
                if c == cid or str(c) == str(cid):
                    return SonarReadTest.FakeTrackerData()
            return None

    class FakeController(object):
        def __init__(self, ss):
            self._ss = ss
        def __getitem__(self, t):
            if str(t).endswith("SonarSystem"):
                return lambda: self._ss
            raise RuntimeError("unexpected Access")

    def _probe(self, tmp, sonar_flag=True, ss=None):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=True, read_sonar=sonar_flag)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": object()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        if ss is not None:
            probe._player_sonar_system = lambda: ss
        return probe

    def test_read_sonar_tracker_contacts(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_")
        probe = None
        try:
            ss = self.FakeSonarSystem([(0, "S1"), (1, "S2")])
            probe = self._probe(tmp, ss=ss)
            out = probe.read_sonar()
            self.assertEqual(out["count"], 2)
            self.assertEqual(len(out["tracks"]), 2)
            self.assertEqual(out["tracks"][0]["id"], "0")
            self.assertEqual(out["tracks"][0]["bearing"], 90.0)
            self.assertEqual(out["tracks"][0]["range"], 1500.0)
            self.assertEqual(out["tracks"][0]["sensor"], "LAB")
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_sonar_empty_contacts(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_")
        probe = None
        try:
            ss = self.FakeSonarSystem([])
            probe = self._probe(tmp, ss=ss)
            out = probe.read_sonar()
            self.assertEqual(out["count"], 0)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_sonar_no_sonar_system(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_")
        probe = None
        try:
            probe = self._probe(tmp, ss=None)
            probe._player_sonar_system = lambda: None
            out = probe.read_sonar()
            self.assertEqual(out, {})
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_sonar_checkpoint_trail(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_")
        probe = None
        try:
            ss = self.FakeSonarSystem([(0, "S1")])
            probe = self._probe(tmp, ss=ss)
            probe.read_sonar()
            with open(os.path.join(tmp, "ship_probe_log.txt"), "r", encoding="utf-8") as f:
                trail = f.read()
            for marker in ("cp: sonar tracker: SonarSystem ok",
                           "cp: sonar tracker: 1 contacts",
                           "cp: sonar tracker: 1 tracks read"):
                self.assertIn(marker, trail)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_sonar_disabled_flag(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_")
        probe = None
        try:
            probe = self._probe(tmp, sonar_flag=False)
            probe._player_sonar_system = lambda: None
            probe.player_controller = lambda: None
            class FakeCM(object):
                Player = _FakeInfo()
            probe.host["client"] = type("C", (), {"_CoordinatesManager": FakeCM()})()
            st = probe.collect_state()
            self.assertEqual(st["sonar"], {"disabled": True})
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_measure_perf_writes_section_times(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_perf_")
        probe = None
        try:
            cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                       require_player=True, target_element_id=0, max_contacts=50,
                       max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                       state_every=10, read_contacts=True, read_sonar=False,
                       measure_perf=True)
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "_Controller": object(),
                    "client": object()}
            probe = ship_probe._Probe(cfg)
            probe.host = host
            probe.player_controller = lambda: None
            class FakeCM(object):
                Player = _FakeInfo()
            probe.host["client"] = type("C", (), {"_CoordinatesManager": FakeCM()})()
            st = probe.collect_state()
            self.assertIn("perf", st)
            for sec in ("identity", "navigation", "blackboard", "systems", "contacts", "sonar", "ai", "total"):
                self.assertIn(sec, st["perf"])
                self.assertGreaterEqual(st["perf"][sec], 0.0)
            probe.cfg["measure_perf"] = False
            st2 = probe.collect_state()
            self.assertNotIn("perf", st2)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class DiscoverySonarTest(unittest.TestCase):
    class FakeSonar(object):
        StrongestContact = "would-freeze"
        Scan = None
        Track = None

    class FakeClient(object):
        def __init__(self):
            self._ActiveSonar = DiscoverySonarTest.FakeSonar()
            self._PassiveSonar = None

    def test_blackboard_sonar_maps_members_safely(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_disc_")
        probe = None
        try:
            cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                       require_player=True, target_element_id=0, max_contacts=50,
                       max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                       state_every=10, read_contacts=True, read_sonar=False)
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "_Controller": object(),
                    "client": self.FakeClient()}
            probe = ship_probe._Probe(cfg)
            probe.host = host
            probe.discovery_run()
            import json as _json
            with open(os.path.join(tmp, ship_probe._PROBE_NAME), "r", encoding="utf-8") as f:
                d = _json.load(f)
            bs = d["blackboard_sonar"]
            self.assertTrue(bs["active"]["present"])
            self.assertIn("StrongestContact", bs["active"]["members"])
            self.assertIn("Scan", bs["active"]["members"])
            self.assertFalse(bs["passive"]["present"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class _BoomGeo(object):
    # getattr on members must raise — discovery may only dir() them
    @property
    def Latitude(self):
        raise RuntimeError("no getattr on geo members")

    @property
    def Longitude(self):
        raise RuntimeError("no getattr on geo members")

    @property
    def X(self):
        raise RuntimeError("no getattr on geo members")


class _BoomINS(object):
    GeoCoordinates = _BoomGeo()


class _BoomNav(object):
    INS = _BoomINS()


class _BoomClient(object):
    def __init__(self, nav):
        self._Navigation = nav


class DiscoveryNavGeoTest(unittest.TestCase):

    def _discovery(self, tmp, host):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=True, read_sonar=False)
        probe = ship_probe._Probe(cfg)
        probe.host = host
        probe.discovery_run()
        import json as _json
        with open(os.path.join(tmp, ship_probe._PROBE_NAME), "r", encoding="utf-8") as f:
            return _json.load(f), probe

    def test_nav_geo_surfaces_members_without_getattr(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_geo_disc_")
        probe = None
        try:
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "_Controller": object(),
                    "client": _BoomClient(_BoomNav())}
            d, probe = self._discovery(tmp, host)
            ng = d["nav_geo"]
            self.assertTrue(ng["present"])
            self.assertIn("Latitude", ng["members"])
            self.assertIn("Longitude", ng["members"])
            self.assertIn("X", ng["members"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nav_geo_absent_without_client(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_geo_absent_")
        probe = None
        try:
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "_Controller": object(),
                    "client": None}
            d, probe = self._discovery(tmp, host)
            self.assertFalse(d["nav_geo"]["present"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class MercCheckpointTest(unittest.TestCase):
    class _Out(object):
        Latitude = 1.0
        Longitude = 2.0

    class _FakeCM(object):
        def __init__(self):
            self.calls = []

        def MercatorToWGS84(self, merc):
            self.calls.append(merc)
            return MercCheckpointTest._Out()

    def _probe(self, tmp, resolve=True):
        cfg = dict(log_dir=tmp, tick_delay=30, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=resolve,
                   state_every=10)
        probe = ship_probe._Probe(cfg)
        cm = self._FakeCM()
        probe._cm_cache = cm
        probe.coordinates_manager = lambda: cm
        lines = []
        probe.emit = lines.append
        return probe, cm, lines

    def test_merc_to_ll_emits_checkpoints_before_each_call(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_merc_cp_")
        probe = None
        try:
            probe, cm, lines = self._probe(tmp)
            ll = probe._merc_to_ll(object())
            self.assertEqual(ll, (1.0, 2.0))
            self.assertEqual(len(cm.calls), 1)
            self.assertIn("cp: merc CoordinatesManager.MercatorToWGS84", lines)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_merc_to_ll_skips_emits_when_resolve_disabled(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_merc_disabled_")
        probe = None
        try:
            probe, cm, lines = self._probe(tmp, resolve=False)
            self.assertIsNone(probe._merc_to_ll(object()))
            self.assertEqual(cm.calls, [])
            self.assertEqual(lines, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class _AiFakeINS(object):
    class _Geo(object):
        Latitude = 5.0
        Longitude = 10.0
    GeoCoordinates = _Geo()
    Heading = 33.0
    ForwardSpeed = 12.0
    TrueHeading = 35.0
    TrueForwardSpeed = 11.5


class RangeBearingTest(unittest.TestCase):
    def test_equator_degree(self):
        d, b = ship_probe._range_bearing(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(d, 111.19, places=2)
        self.assertAlmostEqual(b, 90.0, places=1)

    def test_north_pole(self):
        d, b = ship_probe._range_bearing(0.0, 0.0, 90.0, 0.0)
        self.assertAlmostEqual(d, 10007.54, places=2)
        self.assertAlmostEqual(b, 0.0, places=1)

    def test_known_airport_pair(self):
        # JFK -> LHR, ~5540 km, initial bearing ~56 deg (rough sanity bounds)
        d, b = ship_probe._range_bearing(40.6413, -73.7781, 51.4700, -0.4543)
        self.assertGreater(d, 5500)
        self.assertLess(d, 5600)
        self.assertGreater(b, 50)
        self.assertLess(b, 60)

    def test_bad_input(self):
        self.assertEqual(ship_probe._range_bearing(None, 0, 0, 1), (None, None))


class _AiFakeNav(object):
    class _DepthGauge(object):
        Elevation = -42.0
    DepthGauge = _DepthGauge()
    INS = _AiFakeINS()
    _CurrentAssignmentID = 7


class _AiFakeCM(object):
    Count = 4


class _AiFakeInfo(object):
    ElementName = "DDG"
    CountryID = 46
    Category = "Ship"


class AiElementsReadTest(unittest.TestCase):

    def _probe_with_storage(self, tmp, storage, player_id=6):
        """Install fake pybt blackboard storage + coordinates manager."""
        saved = _install_fake_pybt(storage)
        probe = None
        try:
            class FakeCMCoords(object):
                class Player(object):
                    GetID = player_id
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _AiFakeInfo(),
                    "_Controller": object(),
                    "client": None}
            cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                       require_player=True, target_element_id=0, max_contacts=50,
                       max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                       state_every=10, read_contacts=False, read_sonar=False,
                       read_ai=True, max_ai_elements=30)
            probe = ship_probe._Probe(cfg)
            probe.host = host
            # coordinates_manager resolves the CM from the blackboard storage:
            # fake it by pre-setting the cache.
            probe._cm_cache = FakeCMCoords()
            return probe
        finally:
            _uninstall_fake_pybt(saved)

    def test_enumerates_all_namespaces(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_ai_")
        probe = None
        try:
            storage = {
                "/6/_CoordinatesManager": object(),   # player ns (skip)
                "/11/_Navigation": _AiFakeNav(),
                "/11/_ContactManager": _AiFakeCM(),
                "/11/_Information": _AiFakeInfo(),
                "/11/_OrderedCourse": 90,
                "/11/_OrderedEOTOrder": "AheadStd",
                "/11/_AiDataLink": object(),
                "/12/_Navigation": _AiFakeNav(),
                "/12/_ContactManager": _AiFakeCM(),
                "/12/_OrderedCourse": 180,
                "/13/_CoordinatesManager": object(),  # no nav — still listed
            }
            saved = _install_fake_pybt(storage)
            try:
                probe = self._probe_with_storage(tmp, storage)
                out = probe.read_ai_elements()
                self.assertEqual(out["count"], 3)
                ids = [e["id"] for e in out["elements"]]
                self.assertEqual(ids, [11, 12, 13])
                self.assertNotIn(6, ids)  # player skipped
            finally:
                _uninstall_fake_pybt(saved)
            # ai_state.json written
            import json as _json
            with open(os.path.join(tmp, ship_probe._AI_STATE_NAME), "r", encoding="utf-8") as f:
                d = _json.load(f)
            self.assertEqual(d["count"], 3)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_per_element_fields(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_ai_fields_")
        probe = None
        try:
            storage = {
                "/11/_Navigation": _AiFakeNav(),
                "/11/_ContactManager": _AiFakeCM(),
                "/11/_Information": _AiFakeInfo(),
                "/11/_OrderedCourse": 90,
                "/11/_CurrentCourse": 88,
                "/11/_OrderedEOTOrder": "AheadStd",
                "/11/_CurrentEOTOrder": "AheadStd",
                "/11/_OrderedDepth": -35,
                "/11/_CurrentDepth": -42,
                "/11/_AiDataLink": object(),
                "/11/_AttackOps": object(),
                "/11/_ActionPrepComplete": True,
                "/11/_IncomingOrder": object(),
                "/11/_CurrentAssignment": object(),
            }
            saved = _install_fake_pybt(storage)
            try:
                probe = self._probe_with_storage(tmp, storage)
                out = probe.read_ai_elements()
                el = out["elements"][0]
                self.assertEqual(el["id"], 11)
                self.assertEqual(el["assignment_id"], 7)  # from nav._CurrentAssignmentID
                self.assertEqual(el["lat_lon"], [5.0, 10.0])
                self.assertEqual(el["heading"], 33.0)
                self.assertEqual(el["speed"], 12.0)
                self.assertEqual(el["true_heading"], 35.0)
                self.assertEqual(el["true_speed"], 11.5)
                self.assertEqual(el["depth"], -42.0)
                self.assertEqual(el["contact_count"], 4)
                self.assertEqual(el["ordered_course"], 90)
                self.assertEqual(el["ordered_eot"], "AheadStd")
                self.assertEqual(el["current_depth"], -42)
                self.assertEqual(el["name"], "DDG")
                self.assertEqual(el["country"], 46)
                self.assertEqual(el["category"], "Ship")
                self.assertTrue(el["ai_data_link"]["present"])
                self.assertTrue(el["attack_ops"]["present"])
                self.assertTrue(el["action_prep_complete"])
                self.assertTrue(el["incoming_order"]["present"])
                self.assertTrue(el["current_assignment"]["present"])
                # player unresolvable in this fake -> no geometry fields
                self.assertNotIn("to_player_range_km", el)
            finally:
                _uninstall_fake_pybt(saved)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_max_elements_truncates(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_ai_max_")
        probe = None
        try:
            storage = {
                "/11/_Navigation": _AiFakeNav(),
                "/12/_Navigation": _AiFakeNav(),
                "/13/_Navigation": _AiFakeNav(),
            }
            saved = _install_fake_pybt(storage)
            try:
                probe = self._probe_with_storage(tmp, storage)
                probe.cfg["max_ai_elements"] = 2
                out = probe.read_ai_elements()
                self.assertEqual(out["count"], 2)
                self.assertTrue(out.get("truncated"))
            finally:
                _uninstall_fake_pybt(saved)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_storage(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_ai_nostore_")
        probe = None
        try:
            saved = _install_fake_pybt({})
            try:
                probe = self._probe_with_storage(tmp, {})
                out = probe.read_ai_elements()
                self.assertEqual(out["count"], 0)
            finally:
                _uninstall_fake_pybt(saved)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_checkpoint_trail(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_ai_cp_")
        probe = None
        try:
            storage = {
                "/11/_Navigation": _AiFakeNav(),
                "/12/_Navigation": _AiFakeNav(),
            }
            saved = _install_fake_pybt(storage)
            try:
                probe = self._probe_with_storage(tmp, storage)
                probe.read_ai_elements()
                with open(os.path.join(tmp, "ship_probe_log.txt"), "r", encoding="utf-8") as f:
                    trail = f.read()
                for marker in ("cp: ai elem 11 ok", "cp: ai elem 12 ok", "ai: 2 elements"):
                    self.assertIn(marker, trail)
            finally:
                _uninstall_fake_pybt(saved)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class SonarArraysTest(unittest.TestCase):
    class FakeContact(object):
        Bearing = 123.4
        Range = 2500.0
        Elevation = -5.0
        Course = 300.0
        Speed = 12.0
        Signal = 0.9
        Noise = 0.1
        SelfNoise = 0.05
        FlowNoise = 0.02
        AmbientNoise = 0.03
        ThermalNoise = 0.0
        DopplerCoef = 0.5
        Category = "sub"
        DatabaseID = "DB1"
        BeamType = "broadband"
        ID = 7
        IsNaN = False
        RelativeBearing = _FakeTuple(50.0, 0.0)

    class FakeSensor(object):
        def __init__(self, contacts):
            self.DesignFrequency = 1500.0
            self.FrequencyRange = "LF"
            self.BeamType = "broadband"
            self.BeamPattern = "array"
            self.AoV = 60.0
            self.Toggle = 1
            self.Status = "ready"
            self.Length = 25.0
            self.Course = 90.0
            self.SensorHeading = 91.0
            self.Contacts = contacts

    class FakeSonarSystem(object):
        def __init__(self, sonars=None):
            self.Sonars = sonars

    class FakeController(object):
        def __init__(self, sonar_system):
            # Access is a GENERIC METHOD (Access<T>()) — `Access[t]` yields the
            # instantiated method, `()` invokes it. Mirror that in the fake.
            self._sonar_system = sonar_system

        @property
        def Access(self):
            class _Access(object):
                def __init__(self, sys):
                    self._sys = sys

                def __getitem__(self, t):
                    def _call():
                        return self._sys
                    return _call
            return _Access(self._sonar_system)

    class FakeCM(object):
        Player = _FakeInfo()

    def _probe(self, tmp, controller, read_arrays=True):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False,
                   read_sonar_arrays=read_arrays, max_sonar_arrays=8,
                   max_sonar_contacts=20)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": self.FakeCM()})()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        if controller is not None:
            probe.player_controller = lambda controller=controller: controller
            probe.g = lambda name: type("SonarSystemType", (), {}) if name == "SonarSystem" else None
        else:
            probe.player_controller = lambda: None
        return probe

    def _sys(self, sensors):
        return SonarArraysTest.FakeSonarSystem(sensors)

    def test_no_controller_returns_err(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            probe = self._probe(tmp, None)
            out = probe.read_sonar_arrays()
            self.assertEqual(out["err"], "no player SonarSystem")
            self.assertEqual(out["arrays"], [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sonar_system_without_sonars(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            sys = self._sys(None)
            probe = self._probe(tmp, SonarArraysTest.FakeController(sys))
            out = probe.read_sonar_arrays()
            self.assertEqual(out["arrays"], [])
            self.assertIsNone(out["err"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_enumerates_arrays_and_contacts(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            sensor = SonarArraysTest.FakeSensor([SonarArraysTest.FakeContact(),
                                                 SonarArraysTest.FakeContact()])
            sys = self._sys([sensor])
            probe = self._probe(tmp, SonarArraysTest.FakeController(sys))
            out = probe.read_sonar_arrays()
            self.assertIsNone(out["err"])
            self.assertEqual(len(out["arrays"]), 1)
            a = out["arrays"][0]
            self.assertEqual(a["type"], "FakeSensor")
            self.assertEqual(a["design_frequency"], 1500.0)
            self.assertEqual(a["contact_count"], 2)
            c = a["contacts"][0]
            self.assertEqual(c["bearing"], 123.4)
            self.assertEqual(c["range"], 2500.0)
            self.assertEqual(c["signal"], 0.9)
            self.assertEqual(c["ambient_noise"], 0.03)
            self.assertEqual(c["relative_bearing"], [50.0, 0.0])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_contact_limit_truncates(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            sensor = SonarArraysTest.FakeSensor([SonarArraysTest.FakeContact() for _ in range(30)])
            sys = self._sys([sensor])
            probe = self._probe(tmp, SonarArraysTest.FakeController(sys))
            probe.cfg["max_sonar_contacts"] = 3
            out = probe.read_sonar_arrays()
            self.assertEqual(len(out["arrays"][0]["contacts"]), 3)
            self.assertTrue(out["arrays"][0]["contacts_truncated"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_collect_state_includes_sonar_arrays_when_enabled(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            sensor = SonarArraysTest.FakeSensor([])
            sys = self._sys([sensor])
            probe = self._probe(tmp, SonarArraysTest.FakeController(sys))
            probe.host["client"]._ActiveSonar = type("AS", (), {"StrongestContact": None})()
            probe.host["client"]._PassiveSonar = type("PS", (), {"StrongestContact": None})()
            st = probe.collect_state()
            self.assertIn("sonar_arrays", st)
            self.assertEqual(len(st["sonar_arrays"]["arrays"]), 1)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_collect_state_omits_when_disabled(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonar_arr_")
        probe = None
        try:
            probe = self._probe(tmp, None, read_arrays=False)
            probe.host["client"]._ActiveSonar = type("AS", (), {"StrongestContact": None})()
            probe.host["client"]._PassiveSonar = type("PS", (), {"StrongestContact": None})()
            st = probe.collect_state()
            self.assertNotIn("sonar_arrays", st)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class MastsReadTest(unittest.TestCase):
    class FakeMastsController(object):
        Status = 3

        def GetAvailableMastIDs(self):
            return [0, 1]

        def GetMastType(self, mast_id):
            return "PERISCOPE" if mast_id == 0 else "SNORKEL"

        def GetMastStatus(self, mast_id):
            return 4 - mast_id

        def GetMastHeight(self, mast_id):
            return 8.5 - mast_id

    class FakeSnorkel(object):
        Raised = True
        IsExposed = False
        HeadValveMode = 2
        IntakeHole = 0.3
        IntakeVolume = 12.5

    class FakeSteering(object):
        PeriscopeDepth = -18.0
        SurfaceDepth = -8.0
        StandardDepth = -60.0
        MaxOperationalDepth = -350.0
        OrderedHeading = 90.0
        OrderedSpeed = 4.0
        OrderedDepth = -20.0

    class FakeTowed(object):
        def GetReelStatus(self, reel):
            return "REELED OUT"

    class FakeController(object):
        # Access is a GENERIC METHOD (Access<T>()) — `Access[t]` yields the
        # instantiated method, `()` invokes it. Mirror that in the fake.
        def __init__(self, types):
            self._types = types

        @property
        def Access(self):
            class _Access(object):
                def __init__(self, types):
                    self._types = types

                def __getitem__(self, t):
                    def _call():
                        return self._types[t]
                    return _call
            return _Access(self._types)

    def _probe(self, tmp, comps, steering=None):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": _FakeInfo()})})()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        types = {name: type("CompType_%s" % name, (), {}) for name in comps}
        probe.player_controller = lambda: MastsReadTest.FakeController(
            {types[name]: comps[name] for name in comps})
        probe.g = lambda name: types.get(name)
        if steering is not None:
            probe.player_steering = lambda: steering
        return probe

    def test_read_systems_masts_and_towed(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_")
        probe = None
        try:
            comps = {
                "MastsController": self.FakeMastsController(),
                "Snorkel": self.FakeSnorkel(),
                "TowedController": self.FakeTowed(),
                "SteeringDiving": self.FakeSteering(),
            }
            probe = self._probe(tmp, comps)
            out = probe.read_systems()
            self.assertEqual(out["mast_controller_status"], 3)
            self.assertEqual(out["mast_ids"], [0, 1])
            self.assertEqual(out["mast_0_type"], "PERISCOPE")
            self.assertEqual(out["mast_1_type"], "SNORKEL")
            self.assertEqual(out["mast_0_status"], "4")
            self.assertEqual(out["mast_0_height"], 8.5)
            self.assertEqual(out["snorkel_raised"], True)
            self.assertEqual(out["snorkel_head_valve"], 2)
            self.assertEqual(out["periscope_depth"], -18.0)
            self.assertEqual(out["max_operational_depth"], -350.0)
            self.assertEqual(out["ordered_depth"], -20.0)
            self.assertEqual(out["towed_array"], "REELED OUT")
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_systems_without_player_steering(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_")
        probe = None
        try:
            comps = {"MastsController": self.FakeMastsController()}
            probe = self._probe(tmp, comps, None)
            out = probe.read_systems()
            self.assertEqual(out["mast_ids"], [0, 1])
            self.assertNotIn("periscope_depth", out)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class IntegrityReadTest(unittest.TestCase):
    """read_systems Integrity block: ratios, flags, per-tank bulkhead/fire/
    flooding + component status (verified mnw.Mechanics.Integrity;
    .Status enum: Operational=1, Malfunctioning=2, Damaged=4)."""

    class _FakeInfo(object):
        _lat = 61.5
        _lon = 5.2
        _Heading = 90.0
        _Speed = 4.0
        _Elevation = 0.0
        _StableElevation = 0.0

    class FakeComp(object):
        def __init__(self, status, desc=""):
            self.Status = status
            self.ComponentDescription = desc

    class FakeTank(object):
        def __init__(self, bulkhead, fire, flooding, level, comps):
            self.IsBulkheadDoorOpen = bulkhead
            self.IsOnFire = fire
            self.IsFlooding = flooding
            self.LevelRatio = level
            self.Components = comps

    class FakeIntegrity(object):
        DamageLevelRatio = 0.1234
        OperationalLevelRatio = 0.98
        HullLevelRatio = 0.997
        HullStressRatio = 0.02
        TanksLevelRatio = 0.5
        SunkLevelRatio = 0.0
        PlateStrength = 1.5
        OnFire = False
        Flooding = True
        IsSunk = False

        def __init__(self):
            self.IntegrityTanks = [
                IntegrityReadTest.FakeTank(True, False, True, 0.05,
                                           [IntegrityReadTest.FakeComp(1, "Radar"),
                                            IntegrityReadTest.FakeComp(2, "Nav"),
                                            IntegrityReadTest.FakeComp(4, "Sonar"),
                                            IntegrityReadTest.FakeComp(99, "?"),
                                            object()]),
                IntegrityReadTest.FakeTank(False, False, False, 0.0,
                                           [IntegrityReadTest.FakeComp(1, "Weapons"),
                                            IntegrityReadTest.FakeComp(1, "ECM")]),
            ]

    class FakeController(object):
        # Access is a GENERIC METHOD (Access<T>()) — `Access[t]` yields the
        # instantiated method, `()` invokes it. Mirror that in the fake.
        def __init__(self, types):
            self._types = types

        @property
        def Access(self):
            class _Access(object):
                def __init__(self, types):
                    self._types = types

                def __getitem__(self, t):
                    def _call():
                        return self._types[t]
                    return _call
            return _Access(self._types)

    def _probe(self, comps=None):
        comps = comps if comps is not None else {"Integrity": self.FakeIntegrity()}
        tmp = tempfile.mkdtemp(prefix="ship_probe_integrity_")
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False,
                   collect_systems_components=True)
        types = {name: type("CompType_%s" % name, (), {}) for name in comps}
        ctrl = IntegrityReadTest.FakeController(
            {types[name]: comps[name] for name in comps})
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": self._FakeInfo(), "_Controller": ctrl,
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": self._FakeInfo()})})()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        probe.player_controller = lambda: ctrl
        probe.g = lambda name: types.get(name)
        return probe, tmp

    def test_read_systems_integrity_block(self):
        probe, tmp = None, None
        try:
            probe, tmp = self._probe()
            out = probe.read_systems()
            self.assertAlmostEqual(out["integrity_damage_ratio"], 0.1234)
            self.assertAlmostEqual(out["integrity_operational_ratio"], 0.98)
            self.assertAlmostEqual(out["integrity_hull_ratio"], 0.997)
            self.assertAlmostEqual(out["integrity_hull_stress"], 0.02)
            self.assertAlmostEqual(out["integrity_tanks_ratio"], 0.5)
            self.assertAlmostEqual(out["integrity_sunk_ratio"], 0.0)
            self.assertAlmostEqual(out["integrity_plate_strength"], 1.5)
            self.assertFalse(out["integrity_on_fire"])
            self.assertTrue(out["integrity_flooding"])
            self.assertFalse(out["integrity_sunk"])
            self.assertEqual(out["integrity_tanks"], 2)
            self.assertTrue(out["tank_0_bulkhead"])
            self.assertFalse(out["tank_0_fire"])
            self.assertTrue(out["tank_0_flooding"])
            self.assertAlmostEqual(out["tank_0_level"], 0.05)
            self.assertEqual(out["tank_0_comps_ok"], 1)
            self.assertEqual(out["tank_0_comps_malf"], 1)
            self.assertEqual(out["tank_0_comps_dmg"], 1)
            self.assertEqual(out["tank_0_comps_other"], 2)
            self.assertEqual(out["tank_0_damaged"], ["Sonar"])
            self.assertFalse(out["tank_1_bulkhead"])
            self.assertEqual(out["tank_1_comps_ok"], 2)
            self.assertEqual(out["tank_1_comps_malf"], 0)
            self.assertEqual(out["tank_1_comps_dmg"], 0)
            self.assertNotIn("tank_1_comps_other", out)
            self.assertNotIn("tank_1_damaged", out)
        finally:
            if probe is not None:
                probe.finish()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_read_systems_without_integrity(self):
        probe, tmp = None, None
        try:
            probe, tmp = self._probe({})
            out = probe.read_systems()
            self.assertNotIn("integrity_damage_ratio", out)
            self.assertNotIn("integrity_tanks", out)
        finally:
            if probe is not None:
                probe.finish()
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)


class SteeringReadTest(unittest.TestCase):
    """read_steering + do_planes control actions (fake SteeringDiving/Hydrodynamics)."""

    class FakePlane(object):
        def __init__(self, angle):
            self.FlapAngle = angle
            self.SurfaceID = 0
            self.Status = 1

    class FakeHydro(object):
        def __init__(self):
            self.ForwardPlanes = [SteeringReadTest.FakePlane(5.0), SteeringReadTest.FakePlane(4.0)]
            self.ForwardPlanesType = "STANDARD"

        def GetForwardPlane(self, i):
            return SteeringReadTest.FakePlane(5.0)

        def GetSternPlane(self, i):
            return SteeringReadTest.FakePlane(-7.0)

        def GetRudder(self, i):
            return SteeringReadTest.FakePlane(3.0)

    class FakeManeuvering(object):
        TPK = 2.5
        STW = 4.0

    class FakeSteering(object):
        OrderedEOT = 7
        OrderedSpeed = 4.0
        OrderedHeading = 90.0
        OrderedDepth = -20.0
        BowPlanesRetracted = False
        ForwardPlanesLocked = True
        IntSternPlanesLocked = False
        SurfaceDepth = -8.0
        StandardDepth = -60.0
        MaxOperationalDepth = -350.0
        PeriscopeDepth = -18.0
        DefaultEOT = 4
        Cavitation = "Low"
        Scope = "Player"

        def __init__(self):
            self.calls = []

        def SetForwardPlanes(self, v):
            self.calls.append(("SetForwardPlanes", v))

        def SetSternPlanes(self, v):
            self.calls.append(("SetSternPlanes", v))

        def SetRudder(self, v):
            self.calls.append(("SetRudder", v))

        def SetBubble(self, v):
            self.calls.append(("SetBubble", v))

        def CatchBubble(self):
            self.calls.append(("CatchBubble",))

        def ReleaseBubble(self):
            self.calls.append(("ReleaseBubble",))

        def AutoTrim(self):
            self.calls.append(("AutoTrim",))

        def ManualTrim(self):
            self.calls.append(("ManualTrim",))

    def _probe(self, tmp, steering):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": _FakeInfo()})})()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        probe.player_controller = lambda: None
        probe._component = lambda tname, owner="self": (
            ("ok", steering) if tname == "SteeringDiving" else ("err", "missing"))
        probe.g = lambda name: None
        probe.collect_state = lambda: None
        probe._hydro = lambda: self.FakeHydro()
        probe._maneuvering = lambda: self.FakeManeuvering()
        return probe

    def test_read_steering_keys(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            steering = self.FakeSteering()
            probe = self._probe(tmp, steering)
            out = probe.read_steering()
            self.assertEqual(out["ordered_eot"], 7)
            self.assertEqual(out["ordered_heading"], 90.0)
            self.assertEqual(out["forward_planes_locked"], True)
            self.assertEqual(out["cavitation"], "Low")
            self.assertEqual(out["scope"], "Player")
            self.assertEqual(out["forward_plane_angles"], [5.0, 4.0])
            self.assertEqual(out["forward_planes_type"], "STANDARD")
            self.assertEqual(out["tpk"], 2.5)
            self.assertEqual(out["stw"], 4.0)
            self.assertNotIn("auto_trim", out)
            self.assertNotIn("depth_bands", out)
            self.assertNotIn("steering_mode", out)
            self.assertNotIn("max_plane_rate_of_turn", out)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_steering_missing(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            probe = self._probe(tmp, None)
            out = probe.read_steering()
            self.assertIn("err", out)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_read_steering_with_getters(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            steering = self.FakeSteering()
            probe = self._probe(tmp, steering)
            out = probe.read_steering()
            self.assertNotIn("stern_plane_angles", out)
            self.assertNotIn("rudder_plane_angles", out)
            out = probe.read_steering(with_getters=True)
            self.assertEqual(out["stern_plane_angles"], [-7.0, -7.0, -7.0, -7.0])
            self.assertEqual(out["rudder_plane_angles"], [3.0, 3.0])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_planes_writes(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            steering = self.FakeSteering()
            probe = self._probe(tmp, steering)
            lines = probe.do_planes({"action": "planes", "fwd": 10.0, "stern": -12.0,
                                     "rudder": 5.0, "bubble": -2.0, "autotrim": False})
            self.assertIn(("SetForwardPlanes", 10.0), steering.calls)
            self.assertIn(("SetSternPlanes", -12.0), steering.calls)
            self.assertIn(("SetRudder", 5.0), steering.calls)
            self.assertIn(("SetBubble", -2.0), steering.calls)
            self.assertIn(("ManualTrim",), steering.calls)
            joined = "\n".join(lines)
            self.assertIn("SetForwardPlanes(10.0): ok", joined)
            self.assertIn("planes fwd=", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_planes_read_only(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            steering = self.FakeSteering()
            probe = self._probe(tmp, steering)
            lines = probe.do_planes({"action": "planes"})
            self.assertEqual(steering.calls, [])
            joined = "\n".join(lines)
            self.assertIn("ordered: eot=7", joined)
            self.assertIn("stern=[-7.0, -7.0, -7.0, -7.0]", joined)
            self.assertIn("rudder=[3.0, 3.0]", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_planes_bubble_on(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_steer_")
        probe = None
        try:
            steering = self.FakeSteering()
            probe = self._probe(tmp, steering)
            lines = probe.do_planes({"action": "planes", "bubble_on": True})
            joined = "\n".join(lines)
            self.assertIn("CatchBubble: ok", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class TanksEnvTest(unittest.TestCase):
    """do_tanks / do_env probes (fake Hydrostatics/MBTManager/TnCManager/
    EnvironmentalSystem) - read-only + isolated write paths."""

    class FakeMBT(object):
        TotalLevel = 711.66
        MeanLevelRatio = 0.35
        TotalCapacity = 2048.0
        Capacity = 1024.0
        Length = 3
        MBTAction = "Idle"
        MBTsAction = "Idle"
        calls = []

        def __init__(self):
            self.calls = []

        def Level(self, i):
            return [0.5, 0.4, 0.3][i]

        def LevelRatio(self, i):
            return [0.5, 0.4, 0.3][i]

        def GetBank(self, i):
            return [1, 2, 3][i]

        def GetBankValve(self, i):
            return [True, True, False][i]

        def IsVentOpen(self, i):
            return True

        def IsBlowerOpen(self, i):
            return False

        def Flood(self):
            self.calls.append("Flood")

        def Drain(self):
            self.calls.append("Drain")

        def Blow(self):
            self.calls.append("Blow")

        def Charge(self):
            self.calls.append("Charge")

        def ToggleVent(self):
            self.calls.append("ToggleVent")

        def ToggleBlower(self):
            self.calls.append("ToggleBlower")

        def SetBankValve(self, i):
            self.calls.append(("SetBankValve", i))

    class FakeFloodValve(object):
        def __init__(self):
            self.calls = []
            self.OpenRatio = 0.0

        def SetRatio(self, ratio):
            self.calls.append(("SetRatio", ratio))
            self.OpenRatio = ratio

    class FakeTnC(object):
        TotalLevel = 12.5
        TotalCapacity = 50.0
        Capacity = 2
        TrimMode = "Auto"

        def __init__(self):
            self.calls = []
            self.TrimFloodValve = TanksEnvTest.FakeFloodValve()

        def Level(self, i):
            return 6.0 if i == 0 else 6.5

        def LevelRatio(self, i):
            return 0.5 if i == 0 else 0.6

        def GetTrimPump(self, i):
            return "pump0"

        def GetTrimValveStatus(self, i):
            return "open"

        def TrimFlood(self):
            self.calls.append("TrimFlood")

        def TrimDrain(self, i=0):
            self.calls.append(("TrimDrain", i))

        def TrimTransfer(self):
            self.calls.append("TrimTransfer")

        def TrimCirculation(self):
            self.calls.append("TrimCirculation")

        def FloodTrim(self):
            self.calls.append("FloodTrim")

        def StopFloodTrim(self):
            self.calls.append("StopFloodTrim")

        def ToggleTrimPump(self, pidx, pval):
            self.calls.append(("ToggleTrimPump", pidx, pval))

        def SetTrimPumpRPM(self, pidx, rpm):
            self.calls.append(("SetTrimPumpRPM", pidx, rpm))

        def SetTrimValveStatus(self, vidx, vval):
            self.calls.append(("SetTrimValveStatus", vidx, vval))

        def StartCirculation(self):
            self.calls.append("StartCirculation")

        def StopCirculation(self):
            self.calls.append("StopCirculation")

        def SetTrimMode(self, mode):
            self.calls.append(("SetTrimMode", mode))
            self.TrimMode = mode

    class FakeHydro(object):
        SL = 0.0
        NL = 63.1
        RoB = 10.0
        Displacement = 6800.0
        FlowNoise = None
        SoundSources = None
        MBT = None
        TnC = None

        def __init__(self, mbt, tnc):
            self.MBT = mbt
            self.TnC = tnc

    class FakeEnv(object):
        _Realistic = True
        _TempAccuracy = 0.5
        _SSPiD = 3
        SSP = None
        TrueSSP = None
        TP = None
        TrueTP = None
        SpecialDepth = None
        TrueSpecialDepth = None
        Analysis = None
        TrueAnalysis = None
        RayTraceOutput = None
        _Trace = None
        calls = []

        def __init__(self):
            self.calls = []

        def get_SSP(self):
            self.calls.append("get_SSP")
            return TanksEnvTest.FakeSSP()

        def get_TP(self):
            self.calls.append("get_TP")
            return [0.0, 25.0, 50.0, 100.0]

        def RayTrace(self, *a):
            raise RuntimeError("not called")

    class FakeSSP(object):
        _Temperatures = [8.0, 7.0, 6.0, 5.0]
        _DepthIndexes = [0, 1, 2, 3]
        _SpecialDepths = [0, 25, 100]
        _Velocities = [1500.0, 1498.0, 1495.0]

    def _probe(self, tmp, hydro, env, steering=None):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py", "_Information": _FakeInfo()}
        probe = ship_probe._Probe(cfg)
        probe.host = host

        def fake_component(tname, owner="self"):
            if tname == "Hydrostatics":
                return ("ok", hydro) if hydro is not None else ("err", "missing")
            if tname == "EnvironmentalSystem":
                return ("ok", env) if env is not None else ("err", "missing")
            return ("err", "missing")

        probe._component = fake_component
        probe._component_any = lambda tname, prefer="player": ("err", None)
        probe._steering = lambda: (steering if steering is not None else (_ for _ in ()).throw(RuntimeError("no steering")))
        probe.emit = lambda *a, **k: None
        probe.player_controller = lambda: None
        probe.collect_state = lambda: None
        probe._blackboard_storage = lambda: None
        probe.host_get = lambda key: None
        probe.g = lambda name: None
        return probe

    def test_do_tanks_readonly(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            mbt = self.FakeMBT()
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(mbt, tnc)
            probe = self._probe(tmp, hydro, None)
            lines = probe.do_tanks({"action": "tanks"})
            joined = "\n".join(lines)
            self.assertIn("Hydrostatics dir(", joined)
            self.assertIn("MBTManager via Hydrostatics.MBT", joined)
            self.assertIn("MBTManager.Flood -> callable", joined)
            self.assertIn("MBTManager.TotalLevel = 711.66", joined)
            self.assertIn("MBTManager.Level(0) = 0.5", joined)
            self.assertIn("MBTManager.IsVentOpen(0) = True", joined)
            self.assertIn("TnCManager via Hydrostatics.TnC", joined)
            self.assertIn("TnCManager.TotalLevel = 12.5", joined)
            self.assertIn("TnCManager.GetTrimPump(0) = pump0", joined)
            self.assertEqual(mbt.calls, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_write(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            mbt = self.FakeMBT()
            hydro = self.FakeHydro(mbt, self.FakeTnC())
            probe = self._probe(tmp, hydro, None)
            lines = probe.do_tanks({"action": "tanks", "vent": True, "blow": True})
            joined = "\n".join(lines)
            self.assertIn("write vent (ToggleVent): ok", joined)
            self.assertIn("write blow (Blow): ok", joined)
            self.assertEqual(mbt.calls, ["ToggleVent", "Blow"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    class FakeTrimValveStatus(object):
        Closed = object()
        In = object()
        Out = object()

    def test_do_tanks_trim_write(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            probe._clr_type = lambda name: (self.FakeTrimValveStatus
                                            if name == "TrimValveStatus" else None)
            cmd = {"action": "tanks", "pump": True, "rpm": "100",
                   "tdrain": True, "valve": "0 open",
                   "tctl": "SetTrimMode Manual"}
            lines = probe.do_tanks(cmd)
            joined = "\n".join(lines)
            self.assertIn("write pump (ToggleTrimPump(0, True)): ok", joined)
            self.assertIn("write rpm (SetTrimPumpRPM(0, 100.0)): ok", joined)
            self.assertIn("write tdrain (TrimDrain): ok", joined)
            self.assertIn("write valve (open SetTrimValveStatus(0,", joined)
            self.assertIn("tctl SetTrimMode('Manual')", joined)
            self.assertEqual(tnc.calls, [
                ("ToggleTrimPump", 0, True), ("SetTrimPumpRPM", 0, 100.0),
                ("SetTrimValveStatus", 0, self.FakeTrimValveStatus.In),
                ("SetTrimMode", "Manual"), ("TrimDrain", 0)])
            self.assertEqual(tnc.TrimMode, "Manual")
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_fvalve(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            lines = probe.do_tanks({"action": "tanks", "fvalve": "open"})
            joined = "\n".join(lines)
            self.assertIn("write fvalve (SetRatio 1.0): ok", joined)
            self.assertEqual(tnc.TrimFloodValve.calls, [("SetRatio", 1.0)])
            lines = probe.do_tanks({"action": "tanks", "fvalve": "ratio 0.25"})
            joined = "\n".join(lines)
            self.assertIn("write fvalve (SetRatio 0.25): ok", joined)
            self.assertEqual(tnc.TrimFloodValve.calls[-1], ("SetRatio", 0.25))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_fill_drain(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            probe._clr_type = lambda name: (self.FakeTrimValveStatus
                                            if name == "TrimValveStatus" else None)
            lines = probe.do_tanks({"action": "tanks", "fill": "0 3 7"})
            joined = "\n".join(lines)
            self.assertIn("write fill (fvalve SetRatio 1.0): ok", joined)
            for ft in (0, 3, 7):
                self.assertIn("write fill (SetTrimValveStatus(%d, In)): ok" % ft,
                              joined)
            self.assertEqual(tnc.TrimFloodValve.calls, [("SetRatio", 1.0)])
            self.assertEqual(
                [c for c in tnc.calls if c[0] == "SetTrimValveStatus"],
                [("SetTrimValveStatus", ft, self.FakeTrimValveStatus.In)
                 for ft in (0, 3, 7)])
            lines = probe.do_tanks({"action": "tanks", "drainall": "all"})
            joined = "\n".join(lines)
            self.assertIn("write drain (SetTrimValveStatus(0, Out)): ok", joined)
            self.assertIn("write drain (TrimDrain(0)): ok", joined)
            self.assertEqual(
                [c for c in tnc.calls if c[0] == "SetTrimValveStatus"][3:],
                [("SetTrimValveStatus", ft, self.FakeTrimValveStatus.Out)
                 for ft in range(8)])
            self.assertIn(("TrimDrain", 0), [c for c in tnc.calls])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_tctl_enum_ref(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            probe._clr_type = lambda name: (self.FakeTrimValveStatus
                                            if name == "TrimValveStatus" else None)
            lines = probe.do_tanks({"action": "tanks",
                                    "tctl": "SetTrimValveStatus 1 @TrimValveStatus.Closed"})
            joined = "\n".join(lines)
            self.assertIn("tctl SetTrimValveStatus(1, <object object at", joined)
            self.assertEqual(tnc.calls, [("SetTrimValveStatus", 1,
                                          self.FakeTrimValveStatus.Closed)])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_tctl_enum_ref_error(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            lines = probe.do_tanks({"action": "tanks",
                                    "tctl": "SetTrimValveStatus 0 @TrimValveStatus.Open"})
            joined = "\n".join(lines)
            self.assertIn("tctl SetTrimValveStatus: arg err: type 'TrimValveStatus' not resolvable", joined)
            self.assertEqual(tnc.calls, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_tctl_info(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            probe._clr_type = lambda name: (self.FakeTrimValveStatus
                                            if name == "TrimValveStatus" else None)
            lines = probe.do_tanks({"action": "tanks", "tctl": "@@info TrimValveStatus"})
            joined = "\n".join(lines)
            self.assertIn("@@info TrimValveStatus =", joined)
            self.assertIn("Closed", joined)
            self.assertEqual(tnc.calls, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tanks_tctl_rejects_unknown(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tanks_")
        probe = None
        try:
            tnc = self.FakeTnC()
            hydro = self.FakeHydro(self.FakeMBT(), tnc)
            probe = self._probe(tmp, hydro, None)
            lines = probe.do_tanks({"action": "tanks",
                                    "tctl": "DeleteFile /etc/passwd"})
            joined = "\n".join(lines)
            self.assertIn("tctl DeleteFile: not in allowed set", joined)
            self.assertEqual(tnc.calls, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    class FakeAlarm(object):
        Name = "General Alarm"
        Active = True
        CurrentAlarm = "Flooding"

        def IsActive(self):
            return True

    def test_do_alarm_found(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_alarm_")
        probe = None
        try:
            alarm = self.FakeAlarm()
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, None)

            def comp_any(tname, prefer="player"):
                if tname == "AlarmManager":
                    return ("player", "ok", alarm)
                return ("player", "err", None)

            probe._component_any = comp_any
            probe._blackboard_storage = lambda: {"/9/_RiggingState": object()}
            lines = probe.do_alarm({"action": "alarm"})
            joined = "\n".join(lines)
            self.assertIn("alarms: AlarmManager FOUND via player Access", joined)
            self.assertIn("AlarmManager dir(", joined)
            self.assertIn("AlarmManager.CurrentAlarm = Flooding", joined)
            self.assertIn("AlarmManager.IsActive -> callable", joined)
            self.assertIn("blackboard alarm/rigging keys:", joined)
            self.assertIn("/9/_RiggingState", joined)
            self.assertNotIn("no alarm/rigging component resolved", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_alarm_rigging_only_nothing(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_alarm_")
        probe = None
        try:
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, None)
            probe._component_any = lambda tname, prefer="player": ("player", "err", None)
            probe._blackboard_storage = lambda: None
            lines = probe.do_alarm({"action": "alarm", "sub": "rigging"})
            joined = "\n".join(lines)
            self.assertIn("no alarm/rigging component resolved", joined)
            tried = ("Rigging", "RiggingManager")
            self.assertIn("RiggingManager", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_sonctl_no_system(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonctl_")
        probe = None
        try:
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            probe._player_sonar_system = lambda: None
            lines = probe.do_sonctl({"action": "sonctl", "sub": "ids"})
            self.assertIn("no player SonarSystem", "\n".join(lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_sonctl_ids(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonctl_")
        probe = None
        try:
            class FakeSS:
                def GetContactIDs(self):
                    return [("id1", "Bearing 45"), ("id2", "Bearing 90")]
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            probe._player_sonar_system = lambda: FakeSS()
            lines = probe.do_sonctl({"action": "sonctl", "sub": "ids"})
            joined = "\n".join(lines)
            self.assertIn("2 items", joined)
            self.assertIn("id1", joined)
            self.assertIn("id2", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_sonctl_auto(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_sonctl_")
        probe = None
        try:
            calls = []
            class FakeSS:
                def SetAutoTrackState(self, flag):
                    calls.append(flag)
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            probe._player_sonar_system = lambda: FakeSS()
            lines = probe.do_sonctl({"action": "sonctl", "sub": "auto", "val": "on"})
            self.assertEqual(calls, [True])
            self.assertIn("ok", "\n".join(lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tracker_no_fc(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tracker_")
        probe = None
        try:
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            lines = probe.do_tracker({"action": "tracker", "sub": ""})
            self.assertIn("no FireControl", "\n".join(lines))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tracker_summary(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tracker_")
        probe = None
        try:
            class FakeFC:
                class ContactManager:
                    GetUsed = []
                    def GetPrefix(self, cid): return 0
                RadarTrackerManager = None
                ESMTrackerManager = None
                VisualTrackerManager = None
                RadioTrackerManager = None
                WeaponTrackerManager = None
                AISTrackerManager = None
                ActiveInterceptTrackerManager = None
                ManualSonarTrackerManager = None
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            probe._component = lambda tname, owner="self": ("ok", FakeFC()) if tname == "FireControl" else ("err", "missing")
            lines = probe.do_tracker({"action": "tracker", "sub": ""})
            joined = "\n".join(lines)
            self.assertIn("TrackerManagers", joined)
            self.assertIn("radar", joined)
            self.assertIn("not present", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_tracker_radar_contacts(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tracker_")
        probe = None
        try:
            class FakeTrack:
                _Range = 5000.0
                _Bearing = 45.0
                _Speed = 10.0
                _Course = 180.0
                _BearingRate = 0.5
                _Elevation = 0.0
            class FakeTM:
                Cycle = 2.0
                Range = 90000.0
                def GetBearing(self, cid): return 45.0
                def GetRange(self, cid): return 5000.0
            class FakeCM:
                GetUsed = ["c1", "c2"]
                def GetPrefix(self, cid):
                    return 1 if cid == "c1" else 0  # c1=radar, c2=visual
                def GetCategoryID(self, cid): return "SeaSurf"
                def GetStandardIdentity(self, cid): return "Unknown"
                def GetTrack(self, cid): return FakeTrack()
            class FakeFC:
                RadarTrackerManager = FakeTM()
                VisualTrackerManager = None
                ESMTrackerManager = None
                RadioTrackerManager = None
                WeaponTrackerManager = None
                AISTrackerManager = None
                ActiveInterceptTrackerManager = None
                ManualSonarTrackerManager = None
                ContactManager = FakeCM()
            probe = self._probe(tmp, self.FakeHydro(None, None), None)
            probe._component = lambda tname, owner="self": ("ok", FakeFC()) if tname == "FireControl" else ("err", "missing")
            lines = probe.do_tracker({"action": "tracker", "sub": "radar"})
            joined = "\n".join(lines)
            self.assertIn("type 1 = radar", joined)
            self.assertIn("Cycle", joined)
            self.assertIn("1 contacts", joined)
            self.assertIn("c1", joined)
            self.assertIn("c1", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    class FakeAccess(object):
        def __init__(self, types):
            self._types = types

        def __dir__(self):
            return list(self._types)

        def __getitem__(self, name):
            return self._types[name]

    class FakeCtrl(object):
        def __init__(self, types):
            self.Access = TanksEnvTest.FakeAccess(types)

    def test_do_alarm_access_registry(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_alarm_")
        probe = None
        try:
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, None)
            probe._component_any = lambda tname, prefer="player": ("player", "err", None)
            probe._blackboard_storage = lambda: None
            ctrl = self.FakeCtrl({
                "Navigation": None, "AlarmManager": None,
                "RiggingController": None, "MBTManager": None})
            probe.host_get = lambda key: ctrl if key == "_Controller" else None
            lines = probe.do_alarm({"action": "alarm"})
            joined = "\n".join(lines)
            self.assertIn("Access: 4 dir() types", joined)
            self.assertIn("AlarmManager", joined)
            self.assertIn("RiggingController", joined)
            self.assertIn("Access alarm/rigging/damage types: AlarmManager, RiggingController", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_alarm_integrity(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_alarm_")
        probe = None
        try:
            class FakeIntegrity(object):
                DamageLevelRatio = 0.42
                IsDamaged = True
                Flooding = False
                def Repair(self):
                    pass
            class FakeCoxswain(object):
                Bulkheads = "Bulkheads object"
                Lights = "Lights object"
                CIWs = "CIWs object"
                GQ = False
                RepairTeams = 2
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, None)

            def comp_any(tname, prefer="player"):
                if tname == "Integrity":
                    return ("player", "ok", FakeIntegrity())
                if tname == "Coxswain":
                    return ("player", "ok", FakeCoxswain())
                return ("player", "err", None)

            def comp(tname, owner="self"):
                if tname == "Integrity":
                    return ("ok", FakeIntegrity())
                if tname == "Coxswain":
                    return ("ok", FakeCoxswain())
                return ("err", None)

            probe._component_any = comp_any
            probe._component = comp
            probe._blackboard_storage = lambda: {"/0/_FloodAlarm": True}
            lines = probe.do_alarm({"action": "alarm", "sub": "integrity"})
            joined = "\n".join(lines)
            self.assertIn("Integrity.DamageLevelRatio = 0.42", joined)
            self.assertIn("Integrity.IsDamaged = True", joined)
            self.assertIn("Integrity.Flooding = False", joined)
            self.assertIn("Coxswain.GQ = False", joined)
            self.assertIn("Coxswain.RepairTeams = 2", joined)
            self.assertIn("bb: /0/_FloodAlarm = True", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_env_readonly(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_env_")
        probe = None
        try:
            env = self.FakeEnv()
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, env)
            lines = probe.do_env({"action": "env"})
            joined = "\n".join(lines)
            self.assertIn("env dir(", joined)
            self.assertIn("env.get_SSP -> callable", joined)
            self.assertIn("env own_sound.NL = 63.1", joined)
            self.assertNotIn("get_SSP() ERR", joined)
            self.assertEqual(env.calls, [])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_env_ssp(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_env_")
        probe = None
        try:
            env = self.FakeEnv()
            env.SSP = self.FakeSSP()
            env.TrueSSP = self.FakeSSP()
            env.TP = [0.0, 25.0, 50.0, 100.0]
            env.SpecialDepth = [0, 25, 100]
            probe = self._probe(tmp, self.FakeHydro(None, None), env)
            lines = probe.do_env({"action": "env", "ssp": True})
            joined = "\n".join(lines)
            self.assertEqual(env.calls, [])
            self.assertIn("env SSP() obj._Temperatures len=4", joined)
            self.assertIn("env SSP() obj._SpecialDepths len=3", joined)
            self.assertIn("env TP() obj.len=4", joined)
            self.assertIn("env SpecialDepth() obj.len=3", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_env_ignores_settings_flag(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_env_")
        probe = None
        try:
            env = self.FakeEnv()
            probe = self._probe(tmp, self.FakeHydro(None, None), env)
            lines = probe.do_env({"action": "env", "settings": True})
            joined = "\n".join(lines)
            self.assertNotIn("env settings", joined)
            self.assertIn("env: environment probe", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


    def test_discovery_tracker_managers(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_tm_")
        probe = None
        try:
            class FakeTM:
                pass
            tm_visual = FakeTM()
            tm_visual.Tracks = []
            tm_radar = FakeTM()
            tm_radar.Tracks = [("c1",)]
            tm_esm = FakeTM()
            tm_radio = FakeTM()
            class FakeFC:
                VisualTrackerManager = tm_visual
                RadarTrackerManager = tm_radar
                ESMTrackerManager = tm_esm
                RadioTrackerManager = tm_radio
                WeaponTrackerManager = None
                AISTrackerManager = tm_visual
                ActiveInterceptTrackerManager = None
                ManualSonarTrackerManager = None
            fc = FakeFC()
            hydro = self.FakeHydro(None, None)
            probe = self._probe(tmp, hydro, None)
            orig_component = probe._component
            def patched_component(tname, owner="self"):
                if tname == "FireControl":
                    return ("ok", fc)
                return orig_component(tname, owner)
            probe._component = patched_component
            probe._blackboard_storage = lambda: None
            out = probe.discovery_run()
            tm = out.get("tracker_managers", {})
            self.assertIn("Visual", tm)
            self.assertTrue(tm["Visual"]["present"])
            self.assertIn("Radar", tm)
            self.assertTrue(tm["Radar"]["present"])
            self.assertIn("ESM", tm)
            self.assertTrue(tm["ESM"]["present"])
            self.assertFalse(tm["Weapon"]["present"])
            self.assertFalse(tm["ManualSonar"]["present"])
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class MastsWriteTest(unittest.TestCase):
    """do_masts control actions (fake MastsController with write methods)."""

    class FakeMastsController(object):
        Status = 2

        def __init__(self):
            self.calls = []
            self._mast_ids = [0, 1, 2]
            self._mast_types = {0: "PERISCOPE", 1: "RADAR", 2: "SNORKEL"}
            self._mast_status = {0: "Raised", 1: "Retracted", 2: "Retracted"}
            self._mast_height = {0: 1.0, 1: 0.0, 2: 0.0}

        def GetAvailableMastIDs(self):
            return self._mast_ids

        def GetMastType(self, mast_id):
            return self._mast_types.get(mast_id, "UNKNOWN")

        def GetMastStatus(self, mast_id):
            return self._mast_status.get(mast_id, "Retracted")

        def GetMastHeight(self, mast_id):
            return self._mast_height.get(mast_id, 0.0)

        def SetMast(self, mast_id, status):
            self.calls.append(("SetMast", mast_id, status))
            self._mast_status[mast_id] = {0: "Retracted", 1: "Moving", 2: "Raised"}.get(status, "?")

        def RetractAllMasts(self):
            self.calls.append("RetractAllMasts")
            for mid in self._mast_ids:
                self._mast_status[mid] = "Retracted"
                self._mast_height[mid] = 0.0

        def SetMastHeightFraction(self, mast_id, frac):
            self.calls.append(("SetMastHeightFraction", mast_id, frac))
            self._mast_height[mast_id] = frac

        def RotatePeriscope(self, mast_id, frac):
            self.calls.append(("RotatePeriscope", mast_id, frac))

        def ChangeMastStatus(self, mast_id, status):
            self.calls.append(("ChangeMastStatus", mast_id, status))

    class FakeController(object):
        def __init__(self, types):
            self._types = types

        @property
        def Access(self):
            class _Access(object):
                def __init__(self, types):
                    self._types = types

                def __getitem__(self, t):
                    def _call():
                        return self._types[t]
                    return _call
            return _Access(self._types)

    def _probe(self, tmp, masts_ctrl):
        cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": _FakeInfo()})})()}
        probe = ship_probe._Probe(cfg)
        probe.host = host
        ctrl_type = type("CompType_MastsController", (), {})
        probe.player_controller = lambda: self.FakeController(
            {ctrl_type: masts_ctrl})
        probe.g = lambda name: ctrl_type if name == "MastsController" else None
        return probe

    def test_do_masts_no_sub(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts"})
            joined = "\n".join(lines)
            self.assertIn("mast 0 [PERISCOPE]", joined)
            self.assertIn("mast 1 [RADAR]", joined)
            self.assertIn("mast 2 [SNORKEL]", joined)
            self.assertIn("snorkel mast: 2", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_raise(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "raise", "id": "1"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("SetMast", 1, 2))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_retract(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "retract", "id": "0"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("SetMast", 0, 0))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_retract_all(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "retract-all"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], "RetractAllMasts")
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_raise_all(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "raise-all"})
            joined = "\n".join(lines)
            self.assertIn("raise-all:", joined)
            # 3 masts -> 3 SetMast calls
            setmast_calls = [c for c in mc.calls if isinstance(c, tuple) and c[0] == "SetMast"]
            self.assertEqual(len(setmast_calls), 3)
            for c in setmast_calls:
                self.assertEqual(c[2], 2)  # Raised
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_height(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "height", "id": "1", "val": "0.75"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("SetMastHeightFraction", 1, 0.75))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_periscope(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "periscope", "id": "0", "val": "0.3"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("RotatePeriscope", 0, 0.3))
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_snorkel_raise(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "snorkel_raise"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("SetMast", 2, 2))  # snorkel_id=2, Raised=2
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_snorkel_retract(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            mc = self.FakeMastsController()
            probe = self._probe(tmp, mc)
            lines = probe.do_masts({"action": "masts", "sub": "snorkel_retract"})
            joined = "\n".join(lines)
            self.assertIn("ok", joined)
            self.assertEqual(mc.calls[-1], ("SetMast", 2, 0))  # snorkel_id=2, Retracted=0
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_do_masts_no_controller(self):
        tmp = tempfile.mkdtemp(prefix="ship_probe_masts_write_")
        probe = None
        try:
            cfg = dict(log_dir=tmp, tick_delay=1, heartbeat_every=120, console_log=False,
                       require_player=True, target_element_id=0, max_contacts=50,
                       max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False,
                   collect_systems_components=True)
            host = {"__name__": "sub", "__file__": "sub.py",
                    "_Information": _FakeInfo(), "_Controller": object(),
                    "client": type("C", (), {"_CoordinatesManager": type(
                        "CM", (), {"Player": _FakeInfo()})})()}
            probe = ship_probe._Probe(cfg)
            probe.host = host
            probe.player_controller = lambda: None
            probe.g = lambda name: None
            lines = probe.do_masts({"action": "masts"})
            joined = "\n".join(lines)
            self.assertIn("no MastsController", joined)
        finally:
            if probe is not None:
                probe.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class SonctlExploreTest(unittest.TestCase):
    """do_sonctl explore: full sonar/audio/bearing exploration dump."""

    def _probe(self, log_dir, ss, ctrl=None, bb=None):
        cfg = dict(log_dir=log_dir, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": _FakeInfo()})})()}
        p = ship_probe._Probe(cfg)
        p.host = host
        p._player_sonar_system = lambda: ss
        p.player_controller = lambda: ctrl
        p._blackboard_storage = lambda: bb
        p._blackboard_cache = bb
        return p

    def test_explore_no_sonar_system(self):
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            p = self._probe(tmp, ss=None)
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "all"})
            self.assertIn("no player SonarSystem", "\n".join(lines))
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_sonarsystem_dir(self):
        """Verify explore dumps SonarSystem properties and marks bearing-like hits."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakeSonarsItem:
                name = "bow_array"
                sensor_type = "Passive"
                Bearing = 45.0
                Range = 2000.0
                ScanHeading = 90.0

                def __dir__(self):
                    return ["name", "sensor_type", "Bearing", "Range", "ScanHeading"]

            class FakeSS:
                ActiveContactCount = 3
                PingActive = False
                ScanBearing = 120.0
                HeadphoneBearing = 60.0

                def __dir__(self):
                    return ["ActiveContactCount", "PingActive", "ScanBearing",
                            "HeadphoneBearing", "GetContactIDs", "Sonars"]

                def Sonars(self):
                    return [FakeSonarsItem()]

                def GetContactIDs(self):
                    return []
            # Sonars must be a property, not a method, on the fake
            FakeSS.Sonars = [FakeSonarsItem()]

            p = self._probe(tmp, ss=FakeSS())
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "all"})
            joined = "\n".join(lines)
            self.assertIn("SonarSystem dir()", joined)
            self.assertIn("HeadphoneBearing: 60.0", joined)
            self.assertIn("ScanBearing: 120.0", joined)
            self.assertIn("Sonars:", joined)
            self.assertIn("bow_array", joined)
            self.assertIn("EXPLORATION SUMMARY", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_passive_sonar_access(self):
        """Verify explore accesses PassiveSonar via Access[T]."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakePassive:
                ListeningBearing = 30.0
                Volume = 0.8

                def __dir__(self):
                    return ["ListeningBearing", "Volume"]
            class FakeCtrl:
                class Access:
                    def __getitem__(self, tname):
                        if tname == "PassiveSonar":
                            return lambda: FakePassive()
                        raise KeyError(tname)
            p = self._probe(tmp, ss=type("SS", (), {
                "GetContactIDs": lambda: [],
                "__dir__": lambda: ["GetContactIDs"],
                "Sonars": [],
            })())
            p.player_controller = lambda: FakeCtrl()
            # ensure g() resolves PassiveSonar type
            p.g = lambda name: name if name == "PassiveSonar" else None
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "all"})
            joined = "\n".join(lines)
            self.assertIn("PassiveSonar", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_blackboard_keys(self):
        """Verify explore dumps ALL blackboard keys."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakeBB:
                Keys = ["sonar_bearing", "audio_volume", "depth", "speed",
                        "sonar_ping_active", "fuel_remaining"]
                def __getitem__(self, k):
                    return {"sonar_bearing": 90.0, "audio_volume": 0.5,
                            "depth": 100.0, "speed": 5.0,
                            "sonar_ping_active": True, "fuel_remaining": 80.0}.get(k)
            p = self._probe(tmp, ss=type("SS", (), {
                "GetContactIDs": lambda: [], "__dir__": lambda: ["GetContactIDs"],
                "Sonars": [],
            })())
            p._blackboard_storage = lambda: FakeBB()
            p._blackboard_cache = FakeBB()
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "blackboard"})
            joined = "\n".join(lines)
            self.assertIn("Blackboard ALL keys", joined)
            self.assertIn("sonar_bearing", joined)
            self.assertIn("depth", joined)
            self.assertIn("fuel_remaining", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_brute_access_scan(self):
        """Verify brute target scans all Access[T] types for bearing-like attrs."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakeHydrophone:
                HydroBearing = 75.0
                def __dir__(self):
                    return ["HydroBearing"]
            class FakeCtrl:
                class Access:
                    def __getitem__(self, tname):
                        if tname == "HydrophoneArray":
                            return lambda: FakeHydrophone()
                        raise KeyError(tname)
            p = self._probe(tmp, ss=type("SS", (), {
                "GetContactIDs": lambda: [], "__dir__": lambda: ["GetContactIDs"],
                "Sonars": [],
            })())
            p.player_controller = lambda: FakeCtrl()
            p.g = lambda name: name  # resolve all types
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "brute"})
            joined = "\n".join(lines)
            self.assertIn("Brute-force", joined)
            self.assertIn("HydrophoneArray", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_cached_contacts(self):
        """Verify explore dumps CachedContacts properties."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakeContact:
                Bearing = 45.0
                Range = 3000.0
                Signal = "Strong"
                Comment = "test contact"
                def __dir__(self):
                    return ["Bearing", "Range", "Signal", "Comment"]
            class FakeDict:
                Keys = ["c1"]
                def __getitem__(self, k):
                    return FakeContact() if k == "c1" else None
            p = self._probe(tmp, ss=type("SS", (), {
                "GetContactIDs": lambda: [], "__dir__": lambda: ["GetContactIDs"],
                "Sonars": [], "CachedContacts": FakeDict(),
            })())
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "all"})
            joined = "\n".join(lines)
            self.assertIn("CachedContacts", joined)
            self.assertIn("c1", joined)
            self.assertIn("Bearing", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_summary_line(self):
        """Verify the summary section appears."""
        tmp = tempfile.mkdtemp(prefix="sonctl_explore_")
        p = None
        try:
            class FakeSS:
                SomeBearingProp = 10.0
                def __dir__(self):
                    return ["SomeBearingProp"]
            p = self._probe(tmp, ss=FakeSS())
            lines = p.do_sonctl({"action": "sonctl", "sub": "explore", "target": "all"})
            joined = "\n".join(lines)
            self.assertIn("EXPLORATION SUMMARY", joined)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)


class ExploreFullTest(unittest.TestCase):
    """do_explore: full internal structure dump to ship_explore.json."""

    def _probe(self, log_dir, ss=None, ctrl=None, bb=None, elem=None):
        cfg = dict(log_dir=log_dir, tick_delay=1, heartbeat_every=120, console_log=False,
                   require_player=True, target_element_id=0, max_contacts=50,
                   max_commands_per_cycle=10, allow_commands=[], resolve_positions=True,
                   state_every=10, read_contacts=False, read_sonar=False)
        host = {"__name__": "sub", "__file__": "sub.py",
                "_Information": _FakeInfo(), "_Controller": object(),
                "client": type("C", (), {"_CoordinatesManager": type(
                    "CM", (), {"Player": _FakeInfo()})})()}
        p = ship_probe._Probe(cfg)
        p.host = host
        p._player_sonar_system = lambda: ss
        p._blackboard_storage = lambda: bb
        # player_info returns an object with .Element
        if elem is not None:
            class FakeInfo:
                Element = elem
            p._player_cache = FakeInfo()
        p.player_controller = lambda: ctrl
        return p

    def test_explore_writes_json(self):
        """do_explore creates ship_explore.json with expected structure."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            class FakeElem:
                Name = "TestSub"
                Position = "pos"
            class FakeSS:
                ActiveContactCount = 2
                def __dir__(self):
                    return ["ActiveContactCount", "GetContactIDs", "Sonars"]
                Sonars = []
                def GetContactIDs(self):
                    return []
            p = self._probe(tmp, ss=FakeSS(), elem=FakeElem())
            lines = p.do_explore({"action": "explore"})
            joined = "\n".join(lines)
            self.assertIn("done", joined)
            # check file was written
            path = os.path.join(tmp, "ship_explore.json")
            self.assertTrue(os.path.isfile(path))
            with io.open(path, "r") as f:
                data = json.load(f)
            self.assertIn("ts", data)
            self.assertIn("summary", data)
            self.assertIn("access", data)
            self.assertIn("sonar_system", data)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_player_element(self):
        """do_explore captures player element dir()."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            class FakeElem:
                Name = "USS Test"
                Heading = 90.0
                def __dir__(self):
                    return ["Name", "Heading"]
            p = self._probe(tmp, elem=FakeElem())
            p.do_explore({"action": "explore"})
            path = os.path.join(tmp, "ship_explore.json")
            with io.open(path, "r") as f:
                data = json.load(f)
            pe = data.get("player_element", {})
            prop_names = [a["name"] for a in pe.get("properties", [])]
            self.assertIn("Name", prop_names)
            self.assertIn("Heading", prop_names)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_access_components(self):
        """do_explore resolves Access[T] types."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            class FakeNav:
                Lat = 10.5
                Lon = 20.3
                def __dir__(self):
                    return ["Lat", "Lon"]
            class FakeAccess:
                def __getitem__(self, tname):
                    if tname == "Navigation":
                        return lambda: FakeNav()
                    raise KeyError(tname)
            class FakeCtrl:
                pass
            ctrl = FakeCtrl()
            ctrl.Access = FakeAccess()
            p = self._probe(tmp, ctrl=ctrl)
            p.g = lambda name: name if name == "Navigation" else None
            p.do_explore({"action": "explore"})
            path = os.path.join(tmp, "ship_explore.json")
            with io.open(path, "r") as f:
                data = json.load(f)
            access = data.get("access", {})
            self.assertIn("Navigation", access)
            nav = access["Navigation"]
            prop_names = [a["name"] for a in nav.get("properties", [])]
            self.assertIn("Lat", prop_names)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_blackboard(self):
        """do_explore captures all blackboard keys."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            class FakeBB:
                Keys = ["depth", "speed", "sonar_bearing"]
                def __getitem__(self, k):
                    return {"depth": 100.0, "speed": 5.0,
                            "sonar_bearing": 90.0}.get(k)
            p = self._probe(tmp, bb=FakeBB())
            p.do_explore({"action": "explore"})
            path = os.path.join(tmp, "ship_explore.json")
            with io.open(path, "r") as f:
                data = json.load(f)
            bb = data.get("blackboard", {})
            self.assertIn("depth", bb.get("keys", []))
            self.assertIn("sonar_bearing", bb.get("keys", []))
            self.assertEqual(bb["values"]["depth"], "100.0")
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_summary(self):
        """do_explore writes summary stats."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            class FakeElem:
                Name = "Test"
                def __dir__(self):
                    return ["Name"]
            p = self._probe(tmp, elem=FakeElem())
            p.do_explore({"action": "explore"})
            path = os.path.join(tmp, "ship_explore.json")
            with io.open(path, "r") as f:
                data = json.load(f)
            summary = data.get("summary", {})
            self.assertIn("access_types", summary)
            self.assertIn("player_props", summary)
            self.assertIn("blackboard_keys", summary)
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explore_no_player(self):
        """do_explore handles missing player gracefully."""
        tmp = tempfile.mkdtemp(prefix="explore_full_")
        p = None
        try:
            p = self._probe(tmp)
            p._player_cache = None
            p.player_info = lambda: None
            lines = p.do_explore({"action": "explore"})
            path = os.path.join(tmp, "ship_explore.json")
            self.assertTrue(os.path.isfile(path))
            with io.open(path, "r") as f:
                data = json.load(f)
            self.assertNotIn("player_element", data)
            self.assertIn("done", "\n".join(lines))
        finally:
            if p is not None:
                p.finish()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
