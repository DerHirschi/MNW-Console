#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Console tests: parse_action, command queue protocol (cmdid, merge)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console  # noqa: E402


class ParseActionTest(unittest.TestCase):
    def test_helm_full(self):
        cmd, err = console.parse_action(["helm", "045", "AheadStd", "60"])
        self.assertIsNone(err)
        self.assertEqual(cmd["action"], "helm")
        self.assertEqual(cmd["course"], 45.0)
        self.assertEqual(cmd["eot"], "AheadStd")
        self.assertEqual(cmd["depth"], 60.0)

    def test_helm_course_only(self):
        cmd, err = console.parse_action(["helm", "90"])
        self.assertIsNone(err)
        self.assertEqual(cmd, {"action": "helm", "course": 90.0})

    def test_helm_invalid_eot(self):
        cmd, err = console.parse_action(["helm", "90", "Fast"])
        self.assertIsNone(cmd)
        self.assertIn("EOT", err)

    def test_helm_missing_course(self):
        cmd, err = console.parse_action(["helm"])
        self.assertIsNone(cmd)
        self.assertIn("helm COURSE", err)

    def test_plot(self):
        cmd, err = console.parse_action(["plot", "61.5", "5.2"])
        self.assertIsNone(err)
        self.assertEqual(cmd, {"action": "plot", "lat": 61.5, "lon": 5.2})

    def test_plot_bad_number(self):
        cmd, err = console.parse_action(["plot", "61.5", "abc"])
        self.assertIsNone(cmd)
        self.assertIn("plot LAT LON", err)

    def test_clear_plot(self):
        cmd, err = console.parse_action(["clear-plot"])
        self.assertEqual(cmd, {"action": "clear-plot"})
        self.assertIsNone(err)

    def test_report(self):
        cmd, err = console.parse_action(["report"])
        self.assertEqual(cmd, {"action": "report"})
        self.assertIsNone(err)

    def test_probe(self):
        cmd, err = console.parse_action(["probe"])
        self.assertEqual(cmd, {"action": "probe"})
        self.assertIsNone(err)

    def test_damage(self):
        cmd, err = console.parse_action(["damage"])
        self.assertEqual(cmd, {"action": "damage"})
        self.assertIsNone(err)

    def test_dc_no_args(self):
        cmd, err = console.parse_action(["dc"])
        self.assertIsNone(cmd)
        self.assertIn("dc", err)

    def test_dc_status(self):
        cmd, err = console.parse_action(["dc", "status"])
        self.assertEqual(cmd, {"action": "dc", "sub": "status"})
        self.assertIsNone(err)

    def test_dc_bulkheads_close(self):
        cmd, err = console.parse_action(["dc", "bulkheads", "close"])
        self.assertEqual(cmd, {"action": "dc", "sub": "bulkheads", "val": "close"})
        self.assertIsNone(err)

    def test_dc_bulkheads_open(self):
        cmd, err = console.parse_action(["dc", "bulkheads", "open"])
        self.assertEqual(cmd, {"action": "dc", "sub": "bulkheads", "val": "open"})
        self.assertIsNone(err)

    def test_dc_bulkheads_bad_val(self):
        cmd, err = console.parse_action(["dc", "bulkheads", "maybe"])
        self.assertIsNone(cmd)
        self.assertIn("dc bulkheads", err)

    def test_dc_bulkhead_close(self):
        cmd, err = console.parse_action(["dc", "bulkhead", "3", "close"])
        self.assertEqual(cmd, {"action": "dc", "sub": "bulkhead", "idx": 3, "val": "close"})
        self.assertIsNone(err)

    def test_dc_bulkhead_open(self):
        cmd, err = console.parse_action(["dc", "bulkhead", "0", "open"])
        self.assertEqual(cmd, {"action": "dc", "sub": "bulkhead", "idx": 0, "val": "open"})
        self.assertIsNone(err)

    def test_dc_bulkhead_missing_args(self):
        cmd, err = console.parse_action(["dc", "bulkhead"])
        self.assertIsNone(cmd)
        self.assertIn("dc bulkhead", err)

    def test_dc_bulkhead_bad_idx(self):
        cmd, err = console.parse_action(["dc", "bulkhead", "abc", "close"])
        self.assertIsNone(cmd)
        self.assertIn("dc bulkhead", err)

    def test_dc_fire(self):
        cmd, err = console.parse_action(["dc", "fire", "5"])
        self.assertEqual(cmd, {"action": "dc", "sub": "fire", "idx": 5})
        self.assertIsNone(err)

    def test_dc_extinguish(self):
        cmd, err = console.parse_action(["dc", "extinguish", "2"])
        self.assertEqual(cmd, {"action": "dc", "sub": "extinguish", "idx": 2})
        self.assertIsNone(err)

    def test_dc_flood(self):
        cmd, err = console.parse_action(["dc", "flood", "7"])
        self.assertEqual(cmd, {"action": "dc", "sub": "flood", "idx": 7})
        self.assertIsNone(err)

    def test_dc_deflood(self):
        cmd, err = console.parse_action(["dc", "deflood", "1"])
        self.assertEqual(cmd, {"action": "dc", "sub": "deflood", "idx": 1})
        self.assertIsNone(err)

    def test_dc_fire_bad_idx(self):
        cmd, err = console.parse_action(["dc", "fire", "abc"])
        self.assertIsNone(cmd)
        self.assertIn("dc fire", err)

    def test_ai_attack(self):
        cmd, err = console.parse_action(["ai-attack", "13"])
        self.assertEqual(cmd, {"action": "ai-attack", "id": 13})
        self.assertIsNone(err)

    def test_ai_attack_missing_id(self):
        cmd, err = console.parse_action(["ai-attack"])
        self.assertIsNone(cmd)
        self.assertIn("ai-attack ID", err)

    def test_ai_attack_bad_id(self):
        cmd, err = console.parse_action(["ai-attack", "abc"])
        self.assertIsNone(cmd)
        self.assertIn("ai-attack ID", err)

    def test_unknown(self):
        cmd, err = console.parse_action(["fire", "everything"])
        self.assertIsNone(cmd)
        self.assertIsNone(err)

    def test_helm_new_eot_names(self):
        cmd, err = console.parse_action(["helm", "045", "Astern23", "120", "--env", "2", "--snap", "--bubble", "3"])
        self.assertIsNone(err)
        self.assertEqual(cmd["eot"], "Astern23")
        self.assertEqual(cmd["env"], 2)
        self.assertTrue(cmd["snap"])
        self.assertEqual(cmd["bubble"], 3.0)

    def test_helm_invalid_env(self):
        cmd, err = console.parse_action(["helm", "045", "Stop", "10", "--env", "x"])
        self.assertIsNone(cmd)
        self.assertIn("--env N", err)

    def test_planes_read(self):
        cmd, err = console.parse_action(["planes"])
        self.assertEqual(cmd, {"action": "planes"})
        self.assertIsNone(err)

    def test_planes_fwd(self):
        cmd, err = console.parse_action(["planes", "fwd", "10"])
        self.assertEqual(cmd, {"action": "planes", "fwd": 10.0})
        self.assertIsNone(err)

    def test_planes_forward_negative(self):
        cmd, err = console.parse_action(["planes", "forward", "-5"])
        self.assertEqual(cmd, {"action": "planes", "fwd": -5.0})
        self.assertIsNone(err)

    def test_planes_stern(self):
        cmd, err = console.parse_action(["planes", "stern", "20"])
        self.assertEqual(cmd, {"action": "planes", "stern": 20.0})
        self.assertIsNone(err)

    def test_planes_rudder_release(self):
        cmd, err = console.parse_action(["planes", "rudder", "release"])
        self.assertEqual(cmd, {"action": "planes", "release_rudder": True})
        self.assertIsNone(err)

    def test_planes_bubble_release(self):
        cmd, err = console.parse_action(["planes", "bubble", "release"])
        self.assertEqual(cmd, {"action": "planes", "release_bubble": True})
        self.assertIsNone(err)

    def test_planes_bubble_on(self):
        cmd, err = console.parse_action(["planes", "bubble", "on"])
        self.assertEqual(cmd, {"action": "planes", "bubble_on": True})
        self.assertIsNone(err)

    def test_planes_bubble_off(self):
        cmd, err = console.parse_action(["planes", "bubble", "off"])
        self.assertEqual(cmd, {"action": "planes", "bubble_on": False})
        self.assertIsNone(err)

    def test_helm_snap_without_eot(self):
        cmd, err = console.parse_action(["helm", "90", "--snap"])
        self.assertIsNone(err)
        self.assertEqual(cmd, {"action": "helm", "course": 90.0, "snap": True})

    def test_helm_flags_only(self):
        cmd, err = console.parse_action(["helm", "270", "--env", "3", "--autotrim", "off"])
        self.assertIsNone(err)
        self.assertEqual(cmd["course"], 270.0)
        self.assertEqual(cmd["env"], 3)
        self.assertFalse(cmd["autotrim"])

    def test_helm_depth_then_flag(self):
        cmd, err = console.parse_action(["helm", "045", "Stop", "60", "--snap"])
        self.assertIsNone(err)
        self.assertEqual(cmd["eot"], "Stop")
        self.assertEqual(cmd["depth"], 60.0)
        self.assertTrue(cmd["snap"])

    def test_planes_autotrim(self):
        cmd, err = console.parse_action(["planes", "autotrim", "off"])
        self.assertEqual(cmd, {"action": "planes", "autotrim": False})
        self.assertIsNone(err)

    def test_planes_bow(self):
        cmd, err = console.parse_action(["planes", "bow", "retract"])
        self.assertEqual(cmd, {"action": "planes", "bow": True})
        self.assertIsNone(err)

    def test_planes_lockfwd(self):
        cmd, err = console.parse_action(["planes", "lockfwd", "on"])
        self.assertEqual(cmd, {"action": "planes", "lockfwd": True})
        self.assertIsNone(err)

    def test_planes_lockint(self):
        cmd, err = console.parse_action(["planes", "lockint", "on"])
        self.assertEqual(cmd, {"action": "planes", "lockint": True})
        self.assertIsNone(err)

    def test_planes_bad_sub(self):
        cmd, err = console.parse_action(["planes", "warp"])
        self.assertIsNone(cmd)
        self.assertIn("planes [fwd", err)

    def test_planes_bad_number(self):
        cmd, err = console.parse_action(["planes", "stern", "abc"])
        self.assertIsNone(cmd)
        self.assertIn("planes [fwd", err)

    def test_tanks_readonly(self):
        cmd, err = console.parse_action(["tanks"])
        self.assertEqual(cmd, {"action": "tanks"})
        self.assertIsNone(err)

    def test_tanks_vent(self):
        cmd, err = console.parse_action(["tanks", "vent"])
        self.assertEqual(cmd, {"action": "tanks", "vent": True})
        self.assertIsNone(err)

    def test_tanks_blow(self):
        cmd, err = console.parse_action(["tanks", "blow"])
        self.assertEqual(cmd, {"action": "tanks", "blow": True})
        self.assertIsNone(err)

    def test_tanks_bank(self):
        cmd, err = console.parse_action(["tanks", "bank", "2"])
        self.assertEqual(cmd, {"action": "tanks", "bank": 2})
        self.assertIsNone(err)

    def test_tanks_bank_bad(self):
        cmd, err = console.parse_action(["tanks", "bank", "x"])
        self.assertIsNone(cmd)
        self.assertIn("tanks bank N", err)

    def test_tanks_bad_sub(self):
        cmd, err = console.parse_action(["tanks", "warp"])
        self.assertIsNone(cmd)
        self.assertIn("tanks [vent", err)

    def test_tanks_pump(self):
        cmd, err = console.parse_action(["tanks", "pump"])
        self.assertEqual(cmd, {"action": "tanks", "pump": True})
        cmd, err = console.parse_action(["tanks", "pump", "0", "off"])
        self.assertEqual(cmd, {"action": "tanks", "pump": "0 off"})

    def test_tanks_rpm(self):
        cmd, err = console.parse_action(["tanks", "rpm", "100"])
        self.assertEqual(cmd, {"action": "tanks", "rpm": "100"})
        cmd, err = console.parse_action(["tanks", "rpm", "0", "100"])
        self.assertEqual(cmd, {"action": "tanks", "rpm": "0 100"})

    def test_tanks_fvalve(self):
        cmd, err = console.parse_action(["tanks", "fvalve", "open"])
        self.assertEqual(cmd, {"action": "tanks", "fvalve": "open"})
        cmd, err = console.parse_action(["tanks", "fvalve", "ratio", "0.5"])
        self.assertEqual(cmd, {"action": "tanks", "fvalve": "ratio 0.5"})
        cmd, err = console.parse_action(["tanks", "fvalve"])
        self.assertIsNone(cmd)
        self.assertIn("tanks fvalve", err)

    def test_tanks_fill_drain(self):
        cmd, err = console.parse_action(["tanks", "fill"])
        self.assertEqual(cmd, {"action": "tanks", "fill": "all"})
        self.assertIsNone(err)
        cmd, err = console.parse_action(["tanks", "fill", "0", "3", "7"])
        self.assertEqual(cmd, {"action": "tanks", "fill": "0 3 7"})
        cmd, err = console.parse_action(["tanks", "drainall"])
        self.assertEqual(cmd, {"action": "tanks", "drainall": "all"})
        self.assertIsNone(err)
        cmd, err = console.parse_action(["tanks", "drainall", "1", "2"])
        self.assertEqual(cmd, {"action": "tanks", "drainall": "1 2"})

    def test_env_readonly(self):
        cmd, err = console.parse_action(["env"])
        self.assertEqual(cmd, {"action": "env"})
        self.assertIsNone(err)

    def test_env_ssp(self):
        cmd, err = console.parse_action(["env", "ssp"])
        self.assertEqual(cmd, {"action": "env", "ssp": True})
        self.assertIsNone(err)

    def test_env_all(self):
        cmd, err = console.parse_action(["env", "all"])
        self.assertEqual(cmd["action"], "env")
        self.assertTrue(cmd["ssp"])
        self.assertIsNone(err)

    def test_env_bad_sub(self):
        cmd, err = console.parse_action(["env", "warp"])
        self.assertIsNone(cmd)
        self.assertIn("env [ssp", err)

    def test_alarm_readonly(self):
        cmd, err = console.parse_action(["alarm"])
        self.assertEqual(cmd, {"action": "alarm"})
        self.assertIsNone(err)

    def test_alarm_alarms(self):
        cmd, err = console.parse_action(["alarm", "alarms"])
        self.assertEqual(cmd, {"action": "alarm", "sub": "alarms"})
        self.assertIsNone(err)

    def test_alarm_rigging(self):
        cmd, err = console.parse_action(["alarm", "rigging"])
        self.assertEqual(cmd, {"action": "alarm", "sub": "rigging"})
        self.assertIsNone(err)

    def test_alarm_bad_sub(self):
        cmd, err = console.parse_action(["alarm", "warp"])
        self.assertIsNone(cmd)
        self.assertIn("alarm [alarms", err)

    def test_sonctl_auto(self):
        cmd, err = console.parse_action(["sonctl", "auto", "on"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "auto", "val": "on"})
        self.assertIsNone(err)

    def test_sonctl_ids(self):
        cmd, err = console.parse_action(["sonctl", "ids"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "ids"})
        self.assertIsNone(err)

    def test_sonctl_track(self):
        cmd, err = console.parse_action(["sonctl", "track", "abc123"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "track", "cid": "abc123"})
        self.assertIsNone(err)

    def test_sonctl_data(self):
        cmd, err = console.parse_action(["sonctl", "data", "c1"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "data", "cid": "c1"})
        self.assertIsNone(err)

    def test_sonctl_mark(self):
        cmd, err = console.parse_action(["sonctl", "mark", "c1", "90.5"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "mark", "cid": "c1",
                               "bearing": "90.5"})
        self.assertIsNone(err)

    def test_sonctl_no_args(self):
        cmd, err = console.parse_action(["sonctl"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": ""})
        self.assertIsNone(err)

    def test_sonctl_explore_default(self):
        cmd, err = console.parse_action(["sonctl", "explore"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "explore", "target": "all"})
        self.assertIsNone(err)

    def test_sonctl_explore_brute(self):
        cmd, err = console.parse_action(["sonctl", "explore", "brute"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "explore", "target": "brute"})
        self.assertIsNone(err)

    def test_sonctl_explore_bb(self):
        cmd, err = console.parse_action(["sonctl", "explore", "blackboard"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "explore", "target": "blackboard"})
        self.assertIsNone(err)

    def test_sonctl_diag(self):
        cmd, err = console.parse_action(["sonctl", "diag"])
        self.assertEqual(cmd, {"action": "sonctl", "sub": "diag"})
        self.assertIsNone(err)

    def test_explore(self):
        cmd, err = console.parse_action(["explore"])
        self.assertEqual(cmd, {"action": "explore"})
        self.assertIsNone(err)

    def test_tracker_summary(self):
        cmd, err = console.parse_action(["tracker"])
        self.assertEqual(cmd, {"action": "tracker", "sub": ""})
        self.assertIsNone(err)

    def test_tracker_radar(self):
        cmd, err = console.parse_action(["tracker", "radar"])
        self.assertEqual(cmd, {"action": "tracker", "sub": "radar"})
        self.assertIsNone(err)

    def test_tracker_esm(self):
        cmd, err = console.parse_action(["tracker", "esm"])
        self.assertEqual(cmd, {"action": "tracker", "sub": "esm"})
        self.assertIsNone(err)

    def test_radar_alias(self):
        cmd, err = console.parse_action(["radar"])
        self.assertEqual(cmd, {"action": "tracker", "sub": "radar"})
        self.assertIsNone(err)

    def test_esm_alias(self):
        cmd, err = console.parse_action(["esm"])
        self.assertEqual(cmd, {"action": "tracker", "sub": "esm"})
        self.assertIsNone(err)

    def test_tracker_bad_type(self):
        cmd, err = console.parse_action(["tracker", "warp"])
        self.assertIsNone(cmd)
        self.assertIn("tracker", err)


class OrderQueueTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ship_probe_console_test_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cmdid_increments(self):
        n = console.send_commands(self.dir, [{"action": "report"}])
        self.assertEqual(n, 1)
        self.assertEqual(console.next_cmdid(self.dir), 1)
        n = console.send_commands(self.dir, [{"action": "probe"}, {"action": "clear-plot"}])
        self.assertEqual(n, 2)
        self.assertEqual(console.next_cmdid(self.dir), 3)

    def test_merge_preserves_existing(self):
        console.send_commands(self.dir, [{"action": "report"}])
        console.send_commands(self.dir, [{"action": "probe"}])
        data = console.read_json(os.path.join(self.dir, console.ORDERS_FILE))
        self.assertEqual(len(data["commands"]), 2)
        self.assertEqual([c["cmdid"] for c in data["commands"]], [0, 1])
        self.assertEqual([c["action"] for c in data["commands"]], ["report", "probe"])

    def test_orders_file_roundtrip(self):
        console.send_commands(self.dir, [{"action": "helm", "course": 45.0, "eot": "AheadStd"}])
        data = console.read_json(os.path.join(self.dir, console.ORDERS_FILE))
        cmd = data["commands"][0]
        self.assertEqual(cmd["cmdid"], 0)
        self.assertEqual(cmd["action"], "helm")
        self.assertEqual(cmd["course"], 45.0)


class ActionDumpTest(unittest.TestCase):
    """cmd_action_dump: queues the command, prints full detail from
    ship_results.json once the probe answered, and only then returns.
    next_cmdid is patched to a fixed value so a pre-seeded result can be
    matched deterministically (real next_cmdid is covered by OrderQueueTest +
    test_next_cmdid_avoids_stale_results)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ship_probe_dump_test_")
        self._orig_next = console.next_cmdid
        console.next_cmdid = lambda d, _v=7: _v

    def tearDown(self):
        console.next_cmdid = self._orig_next
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_result(self, cmdid, action, detail):
        path = os.path.join(self.dir, console.RESULTS_FILE)
        console.atomic_write(path, {"ts": "12:00:00", "results": [
            {"cmdid": cmdid, "action": action, "ts": "12:00:00", "ok": True,
             "result": detail[-1] if detail else "ok", "detail": detail}]})

    def test_dump_prints_result_detail(self):
        detail = ["tanks: ballast/trim probe", "MBTManager.TotalLevel = 711.66",
                  "MBTManager.IsVentOpen(0) = True"]
        self._write_result(7, "tanks", detail)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_action_dump(self.dir, {"action": "tanks"}, wait=2.0)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("queued 1 command(s)", text)
        self.assertIn("#7 tanks", text)
        self.assertIn("tanks: ballast/trim probe", text)
        self.assertIn("MBTManager.TotalLevel = 711.66", text)
        orders = console.read_json(os.path.join(self.dir, console.ORDERS_FILE))
        self.assertEqual([c["cmdid"] for c in orders["commands"]], [7])
        self.assertEqual(orders["commands"][0]["action"], "tanks")

    def test_dump_env_ssp(self):
        detail = ["env: environment probe", "env SSP() obj._Temperatures len=4 [8.0, 7.0, 6.0, 5.0]"]
        self._write_result(7, "env", detail)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_action_dump(self.dir, {"action": "env", "ssp": True}, wait=2.0)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("#7 env", text)
        self.assertIn("env SSP() obj._Temperatures len=4", text)

    def test_dump_skips_stale_result(self):
        detail = ["env: environment probe", "env SSP() obj._Temperatures len=4 [8.0, 7.0, 6.0, 5.0]"]
        self._write_result(7, "env", detail)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_action_dump(self.dir, {"action": "probe"}, wait=1.0)
        self.assertEqual(rc, 1)
        self.assertIn("no result for cmdid 7 yet", out.getvalue())

    def test_dump_timeout_returns_1(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_action_dump(self.dir, {"action": "probe"}, wait=1.0)
        self.assertEqual(rc, 1)
        self.assertIn("no result for cmdid 7 yet", out.getvalue())

    def test_next_cmdid_avoids_stale_results(self):
        console.next_cmdid = self._orig_next
        console.send_commands(self.dir, [{"action": "tanks"}])
        console.atomic_write(os.path.join(self.dir, console.RESULTS_FILE),
                             {"ts": "12:00:00", "results": [
                                 {"cmdid": 0, "action": "alarm", "detail": []}]})
        self.assertEqual(console.next_cmdid(self.dir), 1)

    def test_result_for_matches_action(self):
        console.atomic_write(os.path.join(self.dir, console.RESULTS_FILE),
                             {"ts": "12:00:00", "results": [
                                 {"cmdid": 0, "action": "alarm", "detail": [],
                                  "result": "stale alarm"},
                                 {"cmdid": 1, "action": "tanks", "detail": ["tanks probe"],
                                  "result": "ok"}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_result_for(self.dir, 1, wait=0.5, action="tanks")
        self.assertEqual(rc, 0)
        self.assertIn("#1 tanks", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = console.cmd_result_for(self.dir, 0, wait=0.5, action="tanks")
        self.assertEqual(rc, 1)


class ReplAiDispatchTest(unittest.TestCase):
    """`ai [ID]` must be served locally from ai_state.json, never queued as an
    in-game order (regression: cmd.get("watch") is None made the local branch
    never match, so `ai 13` printed the ship state and queued a denied order)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ship_probe_repl_test_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_ai_local_dispatch_no_watch_key(self):
        import unittest.mock as mock
        called = []

        def fake_cmd_ai(log_dir, nid=None, registry_only=False):
            called.append(nid)
            return 0

        fake_inputs = iter(["ai 13", "quit"])
        with mock.patch.object(console, "input", side_effect=lambda _prompt: next(fake_inputs)), \
                mock.patch.object(console, "cmd_ai", side_effect=fake_cmd_ai), \
                mock.patch.object(console, "send_commands", side_effect=AssertionError(
                    "ai must NOT queue an in-game order")), \
                mock.patch.object(console, "print_state", side_effect=AssertionError(
                    "ai must not print the ship state")):
            console.repl(self.dir)
        self.assertEqual(called, [13])

    def test_ai_parse_has_no_watch_key(self):
        cmd, err = console.parse_action(["ai", "13"])
        self.assertIsNone(err)
        self.assertEqual(cmd["action"], "ai")
        self.assertEqual(cmd["id"], 13)
        self.assertIsNone(cmd.get("watch"))


class FormatDamageTest(unittest.TestCase):
    """format_damage rendering of the integrity/compartment systems section."""

    def _systems(self):
        return {
            "integrity_damage_ratio": 0.1234,
            "integrity_operational_ratio": 0.98,
            "integrity_hull_ratio": 0.997,
            "integrity_hull_stress": 0.02,
            "integrity_tanks_ratio": 0.5,
            "integrity_sunk_ratio": 0.0,
            "integrity_plate_strength": 1.5,
            "integrity_on_fire": False,
            "integrity_flooding": True,
            "integrity_sunk": False,
            "integrity_tanks": 2,
            "tank_0_bulkhead": True,
            "tank_0_fire": False,
            "tank_0_flooding": True,
            "tank_0_level": 0.05,
            "tank_0_comps_ok": 6,
            "tank_0_comps_malf": 1,
            "tank_0_comps_dmg": 1,
            "tank_0_damaged": ["Sonar"],
            "tank_1_bulkhead": False,
            "tank_1_fire": False,
            "tank_1_flooding": False,
            "tank_1_level": 0.0,
            "tank_1_comps_ok": 5,
        }

    def test_full_systems(self):
        lines = console.format_damage(self._systems())
        self.assertEqual(len(lines), 4)
        self.assertIn("damage=0.1234", lines[0])
        self.assertIn("oper=0.98", lines[0])
        self.assertIn("plate=1.5", lines[0])
        self.assertIn("flooding=yes", lines[1])
        self.assertIn("tanks=2 (0 fire, 1 flooding, 1 bulkheads shut)", lines[1])
        self.assertIn("tank 0:", lines[2])
        self.assertIn("bulkhead=open", lines[2])
        self.assertIn("flooding=yes", lines[2])
        self.assertIn("ok=6", lines[2])
        self.assertIn("malf=1", lines[2])
        self.assertIn("dmg=1", lines[2])
        self.assertIn("damaged: Sonar", lines[2])
        self.assertIn("tank 1:", lines[3])
        self.assertIn("bulkhead=shut", lines[3])

    def test_no_systems(self):
        self.assertEqual(console.format_damage(None), ["  (no systems data)"])
        self.assertEqual(console.format_damage({}), ["  (no systems data)"])

    def test_no_integrity(self):
        self.assertEqual(console.format_damage({"ammo_offensive_ratio": 0.5}),
                         ["  (no integrity data - ship lacks Integrity component?)"])


if __name__ == "__main__":
    unittest.main()
