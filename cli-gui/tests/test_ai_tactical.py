"""Tests for ai_tactical.py — pure functions over state dicts, no game needed."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
ROOT = os.path.dirname(GUI)
for p in (GUI, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import ai_tactical as at  # noqa: E402


def ai_state_fixture():
    return {
        "ts": "2026-08-22 18:32:34",
        "elements": [
            {"id": 16, "name": "RUS FFG 054A", "country": 183,
             "category": "mnw.Core.ElementTools+ElementCategory.Ship",
             "host_style": "general", "lat_lon": [63.5067, 5.9886],
             "to_player_range_km": 3.57, "to_player_bearing": 239.0,
             "true_speed": 1.29, "true_heading": 25.76, "depth": None,
             "current_eot": "AheadFlank", "ordered_eot": "AheadFlank",
             "contact_count": 8, "assignment_id": 5,
             "current_assignment": {"id": 5, "type": "mnw.Core.Assignments+ASWSearch"},
             "action_prep_complete": True, "incoming_order": {}},
            {"id": 13, "name": "RUS DDG 052D", "country": 183,
             "category": "mnw.Core.ElementTools+ElementCategory.Ship",
             "host_style": "general", "lat_lon": [63.4291, 5.9770],
             "to_player_range_km": 11.08, "to_player_bearing": 199.2,
             "true_speed": 12.01, "true_heading": 196.4,
             "current_eot": "AheadStd", "contact_count": 5, "assignment_id": 3,
             "current_assignment": {"id": 3, "type": "mnw.Core.Assignments+ASWSearch"},
             "incoming_order": {"assignment_id": 7}},
        ],
    }


def ship_state_fixture():
    return {
        "ts": "2026-08-22 18:32:34",
        "identity": {"name": "(Player) Virginia B3"},
        "mission": {"active": True, "name": "AI Attack Test", "operation": "ASW",
                    "tension": "Peace"},
        "clock": {"time": "2/15/2029 1:12:59 PM", "scale": 1},
        "navigation": {"lat_lon": [63.4979, 5.9013], "heading": 82.76,
                       "speed": 1.62, "depth": 16.97,
                       "altitude": 120.4, "bottom_range": 137.4},
        "blackboard": {"_MergedContacts": "list(5)",
                       "_EnemySuspiciousContacts": "list(0)"},
        "systems": {"integrity_damage_ratio": 0.0, "ammo_offensive_ratio": 1.0,
                    "ammo_defensive_ratio": 0.8, "towed_array": "Retracted",
                    "mast_ids": [0, 1, 2, 3, 4, 5],
                    "mast_0_type": "Snorkel", "mast_0_status": "Raised",
                    "mast_0_height": 4.256,
                    "mast_1_type": "Radar1", "mast_1_status": "Raised",
                    "mast_1_height": 2.568,
                    "mast_2_type": "Photonics1", "mast_2_status": "Retracted",
                    "mast_2_height": 0.0,
                    "mast_3_type": "Photonics2", "mast_3_status": "Retracted",
                    "mast_3_height": 0.0,
                    "mast_4_type": "CommAntenna1", "mast_4_status": "Raised",
                    "mast_4_height": 1.842,
                    "mast_5_type": "CommAntenna2", "mast_5_status": "Retracted",
                    "mast_5_height": 0.0,
                    "snorkel_raised": True, "snorkel_exposed": False,
                    "snorkel_head_valve": 0, "snorkel_intake_hole": 0.1,
                    "snorkel_intake_volume": 0.0},
    }


DETECTED_DETAIL = [
    "detected: scanning ['13', '16', '18']",
    "track probe: element 0 has NO contacts in _ContactManager",
    "track probe: NO contact on player (checked 5 of 5 contacts)",
    "track probe: HIT contact on player, range=2205.4 m (id 1)",
    "DETECTED by element 16 (range 2205 m, contact id 1, 6 contacts)",
    "no _ContactManager on element 18",
    "DETECTED elements: 16",
]

ASG_DETAIL = [
    "asg: assignment probe",
    "target element id=18",
    "assignment_id=-1",
    "action_prep=True",
    "current_altitude=804.2",
    "dipping_engaged=False",
    "throttle=0.85",
    "ammunition.DefensiveCombatPowerRatio=0.75",
]

CONTACTS_DETAIL = [
    "ai-contacts: element 18: 2 contacts",
    "id 'T7', bearing=91.5, range=2205.4, course=88.0, speed=6.2",
    "id 'T9', bearing=180.0, range=9999.0, course=10.0, speed=0.0",
]


class TestParsers(unittest.TestCase):
    def test_parse_detected_detail(self):
        m = at.parse_detected_detail(DETECTED_DETAIL)
        self.assertTrue(m[16]["detected"])
        self.assertEqual(m[16]["range_m"], 2205.0)
        self.assertEqual(m[16]["contact_id"], "1")
        self.assertFalse(m[0]["detected"])       # NO contacts line
        self.assertFalse(m[18]["detected"])      # command-only host
        self.assertTrue(m[18].get("n/a"))
        self.assertIn(13, m)                     # scanned default -> False
        self.assertFalse(m[13]["detected"])

    def test_parse_asg_detail(self):
        v = at.parse_asg_detail(ASG_DETAIL)
        self.assertEqual(v["target_id"], 18)
        self.assertEqual(v["assignment_id"], -1)
        self.assertIs(v["action_prep"], True)
        self.assertIs(v["dipping_engaged"], False)
        self.assertEqual(v["current_altitude"], 804.2)
        self.assertEqual(v["throttle"], 0.85)
        self.assertEqual(v["ammunition.DefensiveCombatPowerRatio"], 0.75)

    def test_parse_asg_detail_newest_wins(self):
        col = at.Collector(at.LocalSource(tempfile.mkdtemp()), read_only=True)
        r_old = {"cmdid": 1, "action": "asg", "ts": "18:00:00",
                 "detail": ["target element id=18", "rpm=10"]}
        r_new = {"cmdid": 2, "action": "asg", "ts": "18:05:00",
                 "detail": ["target element id=18", "rpm=20"]}
        data = {"results": {"results": [r_old, r_new]}, "now": time.time()}
        col._ingest_results(data, [])
        self.assertEqual(col.asg_map[18]["rpm"], 20)

    def test_parse_ai_contacts_detail(self):
        cm = at.parse_ai_contacts_detail(CONTACTS_DETAIL)
        self.assertEqual(len(cm[18]), 2)
        c0 = cm[18][0]
        self.assertEqual(c0["id"], "T7")
        self.assertAlmostEqual(c0["range_m"], 2205.4)
        self.assertAlmostEqual(c0["bearing"], 91.5)

    def test_parse_ns_styles_and_log_detected(self):
        log = ["12:00:00 ns /14/ style=plane/sub keys(89): _SteeringDiving",
               "12:00:01 ns /18/ style=helo keys(94): _DippingSonarController",
               "12:00:02 DETECTED by element 17 (range 1500.5 m, contact id 2, 3 contacts)",
               "12:00:03 noise line"]
        self.assertEqual(at.parse_ns_styles(log),
                         {14: "plane/sub", 18: "helo"})
        det = at.parse_log_detected(log)
        self.assertTrue(det[17]["detected"])
        self.assertEqual(det[17]["range_m"], 1500.5)


class TestSanitize(unittest.TestCase):
    def test_num_nan_inf(self):
        self.assertIsNone(at._num(float("nan")))
        self.assertIsNone(at._num("NaN"))
        self.assertIsNone(at._num(float("inf")))
        self.assertIsNone(at._num("-Infinity"))
        self.assertIsNone(at._num(None))
        self.assertEqual(at._num("12.5"), 12.5)

    def test_fmt(self):
        self.assertEqual(at._fmt(None), "?")
        self.assertEqual(at._fmt(3.14159), "3.1")
        self.assertEqual(at._fmt(3.14159, 2), "3.14")
        self.assertEqual(at._fmt(float("nan")), "?")

    def test_short_enum(self):
        v = "mnw.Core.ElementTools+ElementCategory.Ship"
        self.assertEqual(at._short_enum(v, 28), "ElementCategory.Ship")
        self.assertEqual(at._short_asg("mnw.Core.Assignments+ASWSearch"), "ASWSearch")

    def test_weapons_known_and_unknown(self):
        self.assertIn("YJ-83", at.weapons_for("RUS FFG 054A"))
        self.assertIn("MK-48", at.weapons_for("(Player) Virginia B3"))
        self.assertIsNone(at.weapons_for("Mystery Thing"))

    def test_parse_list_count(self):
        self.assertEqual(at.parse_list_count("list(5)"), 5)
        self.assertIsNone(at.parse_list_count(
            "System.Collections.Generic.List`1[System.Object]"))
        self.assertIsNone(at.parse_list_count(None))


class TestNormalizeMerge(unittest.TestCase):
    def test_merge_state_plus_ghosts(self):
        els = at.normalize_elements(
            ai_state_fixture(), ship_state_fixture(),
            {16: {"detected": True, "range_m": 2205.0, "contact_id": "1"}},
            {}, {14: "plane/sub", 18: "helo"}, None, {})
        ids = [e["id"] for e in els]
        # ranged first (sorted), then ghosts without position
        self.assertEqual(ids[:1], [16])
        self.assertIn(14, ids)
        self.assertIn(18, ids)
        e14 = [e for e in els if e["id"] == 14][0]
        self.assertEqual(e14["type"], "SUB")
        e18 = [e for e in els if e["id"] == 18][0]
        self.assertEqual(e18["type"], "HEL")
        e16 = [e for e in els if e["id"] == 16][0]
        self.assertEqual(e16["eot"], "Flank")
        self.assertEqual(e16["assignment_type"], "ASWSearch")

    def test_detected_range_fills_missing_position(self):
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {99: {"detected": True, "range_m": 900.0}},
                                    {}, {}, None, {})
        e99 = [e for e in els if e["id"] == 99][0]
        self.assertAlmostEqual(e99["range_km"], 0.9)

    def test_incoming_order_flag(self):
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {}, None, {})
        e13 = [e for e in els if e["id"] == 13][0]
        self.assertTrue(e13["incoming"])

    def test_presence_merge(self):
        pres = {"elements": {"21": {"eid": 21, "category":
                "mnw.Core.ElementTools+ElementCategory.Aircraft",
                "lat": 63.1, "lon": 6.0}}}
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {}, pres, {})
        e21 = [e for e in els if e["id"] == 21][0]
        self.assertEqual(e21["src"], "presence")
        self.assertAlmostEqual(e21["lat"], 63.1)

    def test_ship_ghost_rows(self):
        """ns-style 'ship' entries become table rows (SHP) like helo/sub."""
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {17: "ship"}, None, {})
        e17 = [e for e in els if e["id"] == 17][0]
        self.assertEqual(e17["type"], "SHP")
        self.assertEqual(e17["src"], "ns:ship")
        self.assertEqual(e17["style"], "ship")
        self.assertEqual(e17["name"], "Ship #17")

    def test_has_ghost_style_includes_ship(self):
        col = at.Collector(at.LocalSource(tempfile.mkdtemp()), read_only=True)
        self.assertFalse(col._has_ghost_style())
        col.ns_styles.update({17: "ship"})
        self.assertTrue(col._has_ghost_style())
        col.ns_styles.update({14: "plane"})   # legacy probe: sub emits 'plane'
        self.assertTrue(col._has_ghost_style())

    def test_legacy_plane_ghost_is_sub_row(self):
        """Older deployed probes emit 'ns /N/ style=plane' for submarines."""
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {14: "plane"}, None, {})
        e14 = [e for e in els if e["id"] == 14][0]
        self.assertEqual(e14["type"], "SUB")
        self.assertEqual(e14["src"], "ns:plane")


class TestBuildFrame(unittest.TestCase):
    def _frame(self):
        data = {
            "now": time.mktime(time.strptime("2026-08-22 18:32:34",
                                             "%Y-%m-%d %H:%M:%S")),
            "interval": 5.0,
            "ship_state": ship_state_fixture(),
            "ai_state": ai_state_fixture(),
            "log_detected": {},
            "detected_result": {"map": {16: {"detected": True, "range_m": 2205.0,
                                             "contact_id": "1"}}, "age_s": 3.0},
            "asg_map": {},
            "ai_contacts_map": {},
            "ns_styles": {14: "plane/sub", 18: "helo"},
            "presence": None,
            "prev_ranges": {},
        }
        return at.build_frame(data)

    def test_header_and_threats(self):
        f = self._frame()
        self.assertEqual(f["header"]["player"], "(Player) Virginia B3")
        self.assertEqual(f["header"]["mission"], "AI Attack Test")
        self.assertEqual(f["threats"]["merged"], 5)
        self.assertFalse(f["threats"]["torp"])

    def test_strict_json_roundtrip_no_nan(self):
        def walk(v):
            if isinstance(v, float):
                self.assertFalse(v != v or v in (float("inf"), float("-inf")))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        f = self._frame()
        walk(f)  # no NaN/inf anywhere in the frame
        s = json.dumps(f, allow_nan=False, default=str)
        self.assertIsInstance(json.loads(s), dict)

    def test_contacts_disabled(self):
        ss = ship_state_fixture()
        ss["contacts"] = {"count": 0, "disabled": True}
        data = {
            "now": time.time(), "interval": 5.0,
            "ship_state": ss, "ai_state": ai_state_fixture(),
            "log_detected": {}, "detected_result": {}, "asg_map": {},
            "ai_contacts_map": {}, "ns_styles": {}, "presence": None,
            "prev_ranges": {},
        }
        f = at.build_frame(data)
        self.assertTrue(f["contacts"]["disabled"])
        self.assertEqual(f["contacts"]["count"], 0)

    def test_ghost_counter(self):
        data = {
            "now": time.time(), "interval": 5.0,
            "ship_state": ship_state_fixture(),
            "ai_state": ai_state_fixture(),
            "log_detected": {}, "detected_result": {}, "asg_map": {},
            "ai_contacts_map": {},
            # id 0 (contextless host module) never counts as a ghost
            "ns_styles": {14: "plane/sub", 18: "helo", 17: "ship",
                          0: "general"},
            "presence": None, "prev_ranges": {},
        }
        f = at.build_frame(data)
        self.assertEqual(f["ghosts"], 3)


class TestRenderers(unittest.TestCase):
    def setUp(self):
        data = {
            "now": time.time(), "interval": 5.0,
            "ship_state": ship_state_fixture(),
            "ai_state": ai_state_fixture(),
            "log_detected": {},
            "detected_result": {"map": {16: {"detected": True, "range_m": 2205.0,
                                             "contact_id": "1"}}, "age_s": 3.0},
            "asg_map": {18: {"target_id": 18, "current_altitude": 804.2,
                             "ammunition.DefensiveCombatPowerRatio": 0.75,
                             "ts_epoch": time.time() - 30}},
            "ai_contacts_map": {18: [{"id": "T7", "bearing": 91.5,
                                      "range_m": 2205.4, "course": 88.0,
                                      "speed": 6.2}]},
            "ns_styles": {14: "plane/sub", 18: "helo"},
            "presence": None,
            "prev_ranges": {},
        }
        self.frame = at.build_frame(data)

    def test_render_table_rows(self):
        rows = at.render_table(self.frame, 100, sel=1)
        flat = at.flatten(rows)
        self.assertIn("ID", flat[0])
        joined = "\n".join(flat)
        self.assertIn("RUS FFG 054A", joined)
        self.assertIn("YES", joined)
        self.assertIn("Flank", joined)

    def test_narrow_width_no_crash(self):
        for w in (40, 55, 63, 72, 80, 100, 140):
            rows = at.render_table(self.frame, w)
            self.assertTrue(all(isinstance(r, list) for r in rows))

    def test_sel_marker(self):
        rows_sel0 = at.flatten(at.render_table(self.frame, 100, sel=0))[1]
        rows_sel1 = at.flatten(at.render_table(self.frame, 100, sel=1))[2]
        self.assertTrue(rows_sel0.startswith(">"))
        self.assertTrue(rows_sel1.startswith(">"))

    def test_threat_bar_disabled(self):
        f = dict(self.frame)
        f["contacts"] = {"count": 0, "disabled": True, "tracks": []}
        rows = at.render_threat_bar(f, 100)
        txt = "".join(t for row in rows for t, _ in row)
        self.assertIn("disabled", txt)

    def test_threat_bar_wraps_narrow(self):
        f = dict(self.frame)
        f["elements"] = [dict(e, detected=True) for e in f["elements"]]
        for w in (40, 60, 90):
            rows = at.render_threat_bar(f, w)
            self.assertGreaterEqual(len(rows), 2, "width %d should wrap" % w)
            for row in rows:
                self.assertLessEqual(sum(len(t) for t, _ in row), w)
        joined = "".join(t for row in at.render_threat_bar(f, 40)
                         for t, _ in row)
        self.assertIn("DETECTED-BY", joined)
        self.assertIn("#13", joined)

    def test_detail_block(self):
        txt = "\n".join(at.flatten(at.render_detail(self.frame, 0, 100)))
        self.assertIn("#16", txt)
        self.assertIn("YJ-83", txt)

    def test_full_screen_lines(self):
        lines = at.flatten(at.render_frame_lines(self.frame, 90, sel=0))
        self.assertGreaterEqual(len(lines), 6)
        self.assertTrue(lines[0].startswith("MNW TACTICAL"))
        self.assertTrue(any("THREATS" in l for l in lines))
        self.assertTrue(any("OWN SHIP" in l or "POS" in l
                            for l in lines[:3]))

    def test_own_ship_panel_content(self):
        rows = at.render_own_ship_panel(self.frame, 120)
        txt = "\n".join("".join(t for t, _ in r) for r in rows)
        self.assertIn("POS", txt)
        self.assertIn("HDG", txt)
        self.assertIn("ORD CRS", txt)
        self.assertIn("MASTS", txt)
        self.assertIn("AMMO", txt)

    def test_narrow_own_ship_no_crash(self):
        for w in (40, 60, 80):
            rows = at.render_own_ship_panel(self.frame, w)
            flat = at.flatten(rows)
            self.assertTrue(all(len(l) <= max(w, 45) + 2 or w >= 40
                                for l in flat))

    def test_zero_elements_placeholder(self):
        f = dict(self.frame)
        f["elements"] = []
        lines = at.flatten(at.render_frame_lines(f, 90))
        self.assertTrue(any("no AI elements" in l for l in lines))


class TestSourcesAndCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aitac_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_source_read(self):
        with io.open(os.path.join(self.tmp, "x.json"), "w") as f:
            f.write('{"a": 1}')
        src = at.LocalSource(self.tmp)
        self.assertEqual(at.read_json_text(src.read_text("x.json")),
                         {"a": 1})
        self.assertIsNone(src.read_text("missing.json"))

    def test_split_remote(self):
        host, path = at.split_remote('masto@192.168.1.100:"/abs/dir"')
        self.assertEqual((host, path), ("masto@192.168.1.100", "/abs/dir"))
        host, path = at.split_remote("masto@h:/p")
        self.assertEqual((host, path), ("masto@h", "/p"))

    def test_send_commands_read_only(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        self.assertEqual(col.send_commands([{"action": "planes"}]), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ship_orders.json")))

    def test_send_commands_monotonic_cmdid(self):
        orders_path = os.path.join(self.tmp, "ship_orders.json")
        with io.open(orders_path, "w") as f:
            json.dump({"commands": [{"cmdid": 41, "action": "planes"}]}, f)
        col = at.Collector(at.LocalSource(self.tmp))
        ids = col.send_commands([{"action": "planes"}, {"action": "detected"}])
        self.assertEqual(ids, [42, 43])
        with io.open(orders_path) as f:
            data = json.load(f)
        self.assertEqual([c["cmdid"] for c in data["commands"]],
                         [41, 42, 43])

    def test_poll_once_empty_dir_graceful(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        f = col.poll_once()
        self.assertEqual(f["elements"], [])
        json.dumps(f, allow_nan=False, default=str)


class TestAiAttackCommand(unittest.TestCase):
    """Manual ai-attack trigger (BRIEF_scheduler_fairness follow-up): 'A'
    arms the selected element, 'y' queues {"action":"ai-attack","id":N}."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aitac_atk_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_queue_writes_payload_and_tracks_pending(self):
        col = at.Collector(at.LocalSource(self.tmp))
        self.assertTrue(col.queue_ai_attack(14))
        orders_path = os.path.join(self.tmp, "ship_orders.json")
        with io.open(orders_path) as f:
            data = json.load(f)
        self.assertEqual(data["commands"],
                         [{"action": "ai-attack", "id": 14, "cmdid": 0}])
        self.assertEqual(col.pending.get(0), "ai-attack")

    def test_read_only_never_writes(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        self.assertFalse(col.queue_ai_attack(14))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "ship_orders.json")))

    def test_rejects_invalid_ids(self):
        col = at.Collector(at.LocalSource(self.tmp))
        for bad in (None, 0, -3):
            self.assertFalse(col.queue_ai_attack(bad))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "ship_orders.json")))

    def test_answered_result_releases_pending(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.queue_ai_attack(14)
        with io.open(os.path.join(self.tmp, "ship_results.json"), "w") as f:
            json.dump({"results": [{"cmdid": 0, "action": "ai-attack",
                                    "ts": "12:00:00", "detail": []}]}, f)
        col.poll_once()
        self.assertNotIn(0, col.pending)

    def test_untracked_payload_for_blind_attack(self):
        col = at.Collector(at.LocalSource(self.tmp))
        self.assertTrue(col.queue_ai_attack(14, allow_untracked=True))
        orders_path = os.path.join(self.tmp, "ship_orders.json")
        with io.open(orders_path) as f:
            data = json.load(f)
        self.assertEqual(data["commands"],
                         [{"action": "ai-attack", "id": 14,
                           "allow_untracked": True, "cmdid": 0}])
        # plain A stays without the flag
        col2 = at.Collector(at.LocalSource(self.tmp))
        col2.queue_ai_attack(14)
        with io.open(orders_path) as f:
            data = json.load(f)
        self.assertNotIn("allow_untracked", data["commands"][-1])

    def test_ingest_success_sets_attack_status(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        with io.open(os.path.join(self.tmp, "ship_results.json"), "w") as f:
            json.dump({"results": [{"cmdid": 7, "action": "ai-attack",
                                    "ts": "12:00:00",
                                    "result": "PushOrder ok (tactical=17 assignment=98)",
                                    "ok": True}]}, f)
        col.poll_once()
        st = col.attack_status
        self.assertIsNotNone(st)
        self.assertEqual(st["cmdid"], 7)
        self.assertTrue(st["ok"])
        self.assertIn("assignment=98", st["msg"])

    def test_ingest_refusal_sets_failed_status(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        with io.open(os.path.join(self.tmp, "ship_results.json"), "w") as f:
            json.dump({"results": [{"cmdid": 9, "action": "ai-attack",
                                    "ts": "12:01:00",
                                    "result": "RuntimeError: no contact on "
                                              "player — refusing blind attack",
                                    "ok": False}]}, f)
        col.poll_once()
        st = col.attack_status
        self.assertIsNotNone(st)
        self.assertFalse(st["ok"])
        self.assertIn("refusing blind", st["msg"])


class TestOwnShipAndExt(unittest.TestCase):
    def test_own_element_ids(self):
        ss = ship_state_fixture()
        ss["player"] = {"player_id": 0, "id": 0}
        self.assertEqual(at.own_element_ids(ss), {0})

    def test_own_element_ids_player_id_wins(self):
        # live finding: player.id can mirror a HOSTILE element id (13) while
        # player_id/identity.id say 9 — 13 must stay an AI row
        ss = ship_state_fixture()
        ss["player"] = {"player_id": 9, "is_player": True, "id": 13}
        ss["identity"] = {"name": "(Player) Virginia B3", "id": 9}
        self.assertEqual(at.own_element_ids(ss), {9})
        els = at.normalize_elements(ai_state_fixture(), ss, {}, {}, {}, None, {})
        self.assertIn(13, [e["id"] for e in els])

    def test_own_element_ids_fallback_without_player_id(self):
        ss = ship_state_fixture()
        ss["player"] = {"id": 7}
        del ss["identity"]
        self.assertEqual(at.own_element_ids(ss), {7})

    def test_range_bearing_ll(self):
        km, brg = at._range_bearing_ll(63.5, 6.0, 63.5, 6.09)
        self.assertAlmostEqual(km, 4.47, delta=0.3)
        self.assertAlmostEqual(brg, 90, delta=2)
        self.assertEqual(at._range_bearing_ll(None, None, 1, 2), (None, None))

    def test_own_element_filtered_from_ai_list(self):
        ai = ai_state_fixture()
        ai["elements"].append({"id": 0, "name": "? #0"})
        ss = ship_state_fixture()
        ss["player"] = {"player_id": 0, "id": 0}
        els = at.normalize_elements(ai, ss, {}, {}, {}, None, {})
        self.assertNotIn(0, [e["id"] for e in els])

    def test_ext_map_fills_ghost(self):
        ext = {18: {"target_id": 18, "lat_lon": None, "lat": 63.51, "lon": 5.99,
                    "true_heading": 91.0, "true_speed": 6.0, "depth": 804.0,
                    "contact_count": 4, "assignment_id": -1,
                    "ts_epoch": time.time()}}
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {14: "plane/sub", 18: "helo"},
                                    None, {}, ext_map=ext)
        e18 = [e for e in els if e["id"] == 18][0]
        self.assertAlmostEqual(e18["lat"], 63.51)
        self.assertAlmostEqual(e18["heading"], 91.0)
        self.assertIsNotNone(e18["range_km"])   # computed from lat/lon
        self.assertIsNotNone(e18["bearing"])

    def test_parse_ai_state_detail(self):
        v = at.parse_ai_state_detail(
            ["ai-state: element id=18", "lat=63.51", "lon=5.99",
             "true_speed=6.0", "current_eot=AheadFlank"])
        self.assertEqual(v["target_id"], 18)
        self.assertEqual(v["lat"], 63.51)
        self.assertEqual(v["current_eot"], "AheadFlank")

    def test_ext_rotation_and_queue(self):
        tmp = tempfile.mkdtemp(prefix="aitac_ext_")
        try:
            col = at.Collector(at.LocalSource(tmp))
            col.ns_styles.update({18: "helo", 16: "ship"})
            data = {"now": time.time(), "ship_state": {},
                    "ai_state": {"elements": [{"id": 16}]}}
            self.assertEqual(col._next_ext_target(data), 18)  # helo only
            cmds = []
            orig = at.send_commands
            at.send_commands = lambda src, c, floor=-1: list(range(100, 100 + len(c)))
            try:
                col._queue_commands(data)
                self.assertEqual(sorted(col.pending.values()).count("ext"), 1)
                self.assertIn("ext", col.pending.values())
            finally:
                at.send_commands = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRound2Fixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aitac_r2_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # --- speed unit: probe speeds are m/s, display shows knots ---

    def test_kt_conversion_helper(self):
        self.assertAlmostEqual(at._kt(5.144), 10.0, places=2)   # 5.144 m/s = 10 kt
        self.assertAlmostEqual(at._kt(0.5144), 1.0, places=3)
        self.assertIsNone(at._kt(None))
        self.assertIsNone(at._kt("x"))

    def test_table_speed_shows_knots(self):
        frame = at.build_frame({"ship_state": ship_state_fixture(),
                                "ai_state": ai_state_fixture(),
                                "now": time.time()})
        rows = at.flatten(at.render_table(frame, 120))
        ffg_kt = 1.29 * at._MS_TO_KT
        self.assertIn("%sk" % ("%.1f" % ffg_kt), rows[1])       # ~2.5k not 1.29k

    def test_own_panel_speed_shows_knots(self):
        frame = at.build_frame({"ship_state": ship_state_fixture(),
                                "ai_state": ai_state_fixture(),
                                "now": time.time()})
        flat = "".join(t for row in at.render_own_ship_panel(frame, 120)
                       for t, _ in row)
        own_kt = 1.62 * at._MS_TO_KT
        self.assertIn("%.1fkt" % own_kt, flat)

    # --- element 0 is the contextless host module, never an AI row ---

    def test_element_zero_filtered_from_table(self):
        ai = ai_state_fixture()
        ai["elements"].append({"id": 0, "name": "? #0",
                               "category": "mnw.Core.ElementTools+ElementCategory.Ship"})
        els = at.normalize_elements(ai, ship_state_fixture(), {}, {}, {}, None, {})
        self.assertNotIn(0, [e["id"] for e in els])
        self.assertEqual(sorted(e["id"] for e in els), [13, 16])

    def test_asg_rotation_skips_zero_and_own(self):
        col = at.Collector(at.LocalSource(self.tmp))
        ss = ship_state_fixture()
        ss["player"] = {"player_id": 9, "id": 9}
        data = {"now": time.time(), "ship_state": ss,
                "ai_state": {"elements": [{"id": 0}, {"id": 9}, {"id": 16},
                                          {"id": 17}]}}
        self.assertEqual(col._next_asg_target(data), 16)  # first non-0/own id

    def test_ext_rotation_skips_zero(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.ns_styles.update({0: "helo", 18: "helo"})
        data = {"now": time.time(), "ship_state": {},
                "ai_state": {"elements": []}}
        self.assertEqual(col._next_ext_target(data), 18)

    # --- cmdid floor survives the probe clearing ship_orders.json ---

    def test_cmdid_floor_across_cleared_orders(self):
        orders_path = os.path.join(self.tmp, "ship_orders.json")
        col = at.Collector(at.LocalSource(self.tmp))
        with io.open(orders_path, "w") as f:
            json.dump({"commands": [{"cmdid": 7, "action": "planes"}]}, f)
        ids = col.send_commands([{"action": "detected"}])
        self.assertEqual(ids, [8])
        # probe consumed + emptied the queue -> file max resets to -1,
        # but the collector floor keeps ids monotonic
        with io.open(orders_path, "w") as f:
            json.dump({"commands": []}, f)
        ids2 = col.send_commands([{"action": "asg", "id": 16}])
        self.assertEqual(ids2, [9])

    # --- ingest stamps rotation ages with wall clock (ts may be HH:MM:SS) ---

    def test_ingest_sets_usable_ts_epoch(self):
        col = at.Collector(at.LocalSource(self.tmp))
        r = {"cmdid": 3, "action": "ai-state", "ts": "18:05:00",
             "detail": ["target element id=18", "true_speed=6.0"]}
        now = time.time()
        col.poll_once.__self__  # noqa: B018 - attribute sanity
        data = {"results": {"results": [r]}, "now": now}
        col.pending[3] = "ext"
        col._ingest_results(data, [])
        self.assertAlmostEqual(col.ext_map[18]["ts_epoch"], now, delta=1.0)

    # --- detected cadence: >= 10 s base + event trigger on sig change ---

    def test_detect_interval_min_ten_seconds(self):
        col = at.Collector(at.LocalSource(self.tmp), detect_interval=2.0)
        self.assertEqual(col.detect_interval, 10.0)

    def test_detect_due_on_signature_change(self):
        col = at.Collector(at.LocalSource(self.tmp))
        e = ai_state_fixture()["elements"][0]
        data = {"ai_state": {"elements": [e]}}
        col._track_signatures(data)                 # first sight: baseline only
        self.assertFalse(col._detect_due)
        e2 = dict(e, to_player_range_km=e["to_player_range_km"] - 0.5)
        col._track_signatures({"ai_state": {"elements": [e2]}})
        self.assertTrue(col._detect_due)

    def test_detect_scheduled_on_interval_and_event(self):
        tmp = tempfile.mkdtemp(prefix="aitac_det_")
        try:
            col = at.Collector(at.LocalSource(tmp))
            seen = []
            orig = at.send_commands
            at.send_commands = lambda src, c, floor=-1: (
                seen.extend(c) or list(range(100, 100 + len(c))))
            try:
                base = ai_state_fixture()["elements"][0]
                d1 = {"now": time.time(), "ship_state": {},
                      "ai_state": {"elements": [base]}}
                col._track_signatures(d1)
                col.cycle = 1
                col._queue_commands(d1)              # first poll: interval due
                self.assertIn({"action": "detected"}, seen)
                seen[:] = []
                col.pending.clear()                  # ingest consumed results
                col._last_detect_ts = time.time()    # just scanned...
                e2 = dict(base, contact_count=99)    # ...but state changed
                d2 = {"now": time.time(), "ship_state": {},
                      "ai_state": {"elements": [e2]}}
                col._track_signatures(d2)
                col._queue_commands(d2)
                self.assertIn({"action": "detected"}, seen)  # event re-scan
                seen[:] = []
                col.pending.clear()
                col._detect_due = False
                col._queue_commands(d2)              # no change, in window
                self.assertNotIn({"action": "detected"}, seen)
            finally:
                at.send_commands = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- cold-start ns-dump discovery for command-only hosts ---

    def test_nsdump_bootstrap_queued_when_no_styles(self):
        tmp = tempfile.mkdtemp(prefix="aitac_ns_")
        try:
            col = at.Collector(at.LocalSource(tmp))
            seen = []
            orig = at.send_commands
            at.send_commands = lambda src, c, floor=-1: (
                seen.extend(c) or list(range(100, 100 + len(c))))
            try:
                data = {"now": time.time(), "ship_state": {},
                        "ai_state": {"elements": []}}
                col.cycle = 1
                col._queue_commands(data)
                self.assertIn({"action": "ns-dump"}, seen)
                self.assertIn("nsdump", col.pending.values())
                seen[:] = []
                col._queue_commands(data)            # no retry within 10 cycles
                self.assertNotIn({"action": "ns-dump"}, seen)
                col.cycle = 11
                col.ns_styles[16] = "general"        # style exists, no ghost
                col._queue_commands(data)
                self.assertIn({"action": "ns-dump"}, seen)   # keep looking
                seen[:] = []
                col.pending.clear()
                col.cycle = 21
                col.ns_styles[18] = "helo"           # ghost found -> discovery
                col._queue_commands(data)            # stops (20 < 24: quiet)
                self.assertNotIn({"action": "ns-dump"}, seen)
                seen[:] = []
                col.pending.clear()
                del col.ns_styles[18]
                col.cycle = 26
                col._queue_commands(data)            # maintenance cadence
                self.assertIn({"action": "ns-dump"}, seen)   # re-scan hosts
            finally:
                at.send_commands = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- right-hand OWN SHIP column renderer ---

    def test_own_ship_side_rows(self):
        frame = at.build_frame({"ship_state": ship_state_fixture(),
                                "ai_state": ai_state_fixture(),
                                "now": time.time()})
        rows = at.render_own_ship_side(frame)
        self.assertGreaterEqual(len(rows), 8)
        for row in rows:
            w = sum(len(t) for t, _ in row)
            self.assertLessEqual(w, 34)
        flat = "".join(t for row in rows for t, _ in row)
        self.assertIn("POS", flat)
        self.assertIn("MAST", flat)


class TestMastSchema(unittest.TestCase):
    def _frame(self, **player_over):
        frame = at.build_frame({"ship_state": ship_state_fixture(),
                                "ai_state": ai_state_fixture(),
                                "now": time.time()})
        frame["player"].update(player_over)
        return frame

    def _text(self, rows):
        return ["".join(t for t, _ in r) for r in rows]

    def test_build_frame_masts_and_snorkel(self):
        f = at.build_frame({"ship_state": ship_state_fixture(),
                            "ai_state": ai_state_fixture(), "now": time.time()})
        p = f["player"]
        self.assertEqual(len(p["masts"]), 6)
        self.assertEqual(p["masts"][0]["type"], "Snorkel")
        self.assertEqual(p["masts"][0]["height"], 4.256)
        self.assertEqual(p["masts_up"], 3)
        self.assertEqual(p["snorkel_head_valve"], 0)
        self.assertAlmostEqual(p["snorkel_intake_hole"], 0.1)

    def test_build_frame_mast_id_fallback_scan(self):
        ss = ship_state_fixture()
        del ss["systems"]["mast_ids"]
        f = at.build_frame({"ship_state": ss, "ai_state": ai_state_fixture(),
                            "now": time.time()})
        self.assertEqual([m["id"] for m in f["player"]["masts"]],
                         [0, 1, 2, 3, 4, 5])

    def test_fill_rows_fixed_scale(self):
        R = at._mast_fill_rows
        self.assertEqual(R({"status": "Raised", "height": 4.256}), 3)
        self.assertEqual(R({"status": "Raised", "height": 2.568}), 2)
        self.assertEqual(R({"status": "Raised", "height": 1.842}), 1)
        self.assertEqual(R({"status": "Retracted", "height": 4.256}), 0)
        self.assertEqual(R({"status": "Raised", "height": None}), 0)
        self.assertEqual(R({"status": "Raised", "height": 99.0}), 4)

    def test_bars_sit_on_slot_centres(self):
        txt = self._text(at.render_mast_schema(self._frame(), 32))
        top_i = next(i for i, t in enumerate(txt) if t.startswith("┌"))
        mid = txt[top_i + 1]
        centers = [i for i, ch in enumerate(mid) if ch in "█░"]
        above = txt[:top_i]
        for row in above:                      # bars over slot centres only
            for i, ch in enumerate(row):
                if ch == "█":
                    self.assertIn(i, centers)
        for i, ch in enumerate(mid):           # hull stubs continue upward
            if ch == "█":
                self.assertTrue(any(r[i] == "█" for r in above),
                                "stub col %d has no bar above" % i)

    def test_fill_counts_per_column(self):
        txt = self._text(at.render_mast_schema(self._frame(), 32))
        top_i = next(i for i, t in enumerate(txt) if t.startswith("┌"))
        mid = txt[top_i + 1]
        above = txt[:top_i]
        cols = [i for i, ch in enumerate(mid) if ch == "█"]
        self.assertEqual(len(cols), 3)         # snorkel, radar, comm1 raised
        fills = {c: sum(1 for r in above if r[c] == "█") for c in cols}
        self.assertEqual(list(fills.values()), [3, 2, 1])   # 4.26 / 2.57 / 1.84

    def test_snorkel_head_colour_by_exposure(self):
        rows = at.render_mast_schema(self._frame(), 32)
        head = [(t, s) for r in rows for t, s in r if t == "▪"]
        self.assertEqual(len(head), 1)
        self.assertEqual(head[0][1], "blue_dim")       # fixture: submerged
        rows = at.render_mast_schema(self._frame(snorkel_exposed=True), 32)
        head = [(t, s) for r in rows for t, s in r if t == "▪"]
        self.assertEqual(head[0][1], "green")

    def test_labels_heights_and_snorkel_readout(self):
        txt = "\n".join(self._text(at.render_mast_schema(self._frame(), 32)))
        for abbr in ("SNK", "RAD1", "P1", "P2", "C1", "C2"):
            self.assertIn(abbr, txt)
        for val in ("4.3", "2.6", "1.8", "-"):
            self.assertIn(val, txt)
        self.assertIn("SNK up ", txt)          # raised, not exposed
        self.assertIn("HV0", txt)
        self.assertIn("HL0.1", txt)
        self.assertIn("VV0", txt)

    def test_verbose_mode_and_scale_hint(self):
        txt = self._text(at.render_mast_schema(self._frame(), 60))[-1]
        self.assertTrue(txt.startswith("SNORKEL up "))
        self.assertIn("INT-HOLE", txt)
        self.assertIn("INT-VOL", txt)
        self.assertIn("SCALE 0-5m", txt)

    def test_narrow_and_empty_degrade(self):
        f = self._frame()
        self.assertEqual(at.render_mast_schema(f, 24), [])
        self.assertGreaterEqual(len(at.render_mast_schema(f, 25)), 9)
        empty = {"player": {}}
        self.assertEqual(at.render_mast_schema(empty, 40), [])

    def test_integration_side_stacked_textmode(self):
        f = self._frame()
        side = self._text(at.render_own_ship_side(f))
        self.assertTrue(any(t.startswith("┌") for t in side))
        lines = self._text(at.render_frame_lines(f, 90))
        self.assertTrue(any(t.startswith("┌") for t in lines))


class TestCmdidFloor(unittest.TestCase):
    """TUI restart vs running probe: cmdid floor must include results history."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aitac_floor_")
        with open(os.path.join(self.tmp, "ship_results.json"), "w") as f:
            json.dump({"ts": "2026-08-23 17:44:43", "results": [
                {"cmdid": 12, "action": "detected", "ts": "18:00:00"},
                {"cmdid": 39, "action": "ai-state", "ts": "17:44:43"}]}, f)
        with open(os.path.join(self.tmp, "ship_orders.json"), "w") as f:
            json.dump({"commands": [{"action": "ns-dump", "cmdid": i}
                                    for i in range(3)]}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orders_last_id(self):
        data = json.load(open(os.path.join(self.tmp, "ship_orders.json")))
        return data["commands"][-1]["cmdid"]

    def test_ingest_tracks_result_cmdid_max(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.poll_once()
        self.assertEqual(col._result_cmdid_max, 39)

    def test_send_lands_above_result_history(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.poll_once()
        ids = col.send_commands([{"action": "detected"}])
        # poll_once already queued refresh/detect/nsdump -> id continues
        # above them, but ALWAYS above the results history (39)
        self.assertGreater(ids[0], 39)
        self.assertEqual(self._orders_last_id(), ids[0])

    def test_module_send_ignores_results_without_collector(self):
        ids = at.send_commands(at.LocalSource(self.tmp),
                               [{"action": "planes"}], floor=-1)
        self.assertEqual(ids, [3])            # orders max 2 + 1

    def test_watchdog_rebases_and_prunes_stale_pendings(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.pending[7] = "detect"
        col.pending_ts[7] = time.time() - 60
        col._last_cmdid = 5
        col.poll_once()
        self.assertNotIn(7, col.pending)
        # rebased above results history (39); poll_once's own sends may
        # have pushed it further, but never below the rebase point
        self.assertGreaterEqual(col._last_cmdid, 39)

    def test_watchdog_quiet_when_fresh(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.pending[8] = "asg"
        col.pending_ts[8] = time.time()
        col.poll_once()
        self.assertIn(8, col.pending)

    def test_detect_purpose_popped_on_result(self):
        col = at.Collector(at.LocalSource(self.tmp))
        col.pending[12] = "detect"
        col.pending_ts[12] = time.time()
        col.poll_once()
        self.assertNotIn(12, col.pending)

    def test_nsdump_throttles_after_three_attempts(self):
        orig = at.send_commands
        seen = []
        at.send_commands = lambda src_, c, floor=-1: (
            seen.extend(c) or list(range(100, 100 + len(c))))
        try:
            col = at.Collector(at.LocalSource(self.tmp))
            data = {"now": time.time(), "ship_state": {},
                    "ai_state": {"elements": []}}
            for cycle in (1, 11, 21):         # short cadence while attempts < 3
                col.cycle = cycle
                col._queue_commands(data)
            self.assertEqual(sum(1 for c in seen
                                 if c.get("action") == "ns-dump"), 3)
            seen[:] = []
            col.cycle = 31                    # inside the relaxed 30-cycle gap
            col._queue_commands(data)
            self.assertNotIn("ns-dump", [c.get("action") for c in seen])
            col.cycle = 51
            col._queue_commands(data)
            self.assertEqual([c.get("action") for c in seen]
                             .count("ns-dump"), 1)
        finally:
            at.send_commands = orig

    def test_ttl_defaults_sixty(self):
        col = at.Collector(at.LocalSource(self.tmp))
        self.assertEqual(col.asg_ttl, 60.0)
        self.assertEqual(col.ext_ttl, 60.0)


class TestTableHeader(unittest.TestCase):
    def setUp(self):
        self.frame = at.build_frame({
            "ship_state": ship_state_fixture(),
            "ai_state": ai_state_fixture(), "now": time.time()})

    def test_header_units_wide(self):
        txt = at.flatten(at.render_table(self.frame, 100))[0]
        for unit in ("RANGE km", "SPD kt", "DEP m", "BRG\u00b0", "HDG\u00b0"):
            self.assertIn(unit, txt)

    def test_header_first_row_hdr_style(self):
        rows = at.render_table(self.frame, 80)
        self.assertEqual(rows[0][0][1], "hdr")
        for w in range(40, 145, 5):     # never wider than the box, no crash
            row = at.render_table(self.frame, w)[0]
            self.assertLessEqual(sum(len(t) for t, _ in row), max(w, 45))

    def test_textmode_keeps_header(self):
        lines = at.flatten(at.render_frame_lines(self.frame, 90))
        self.assertTrue(any(l.lstrip().startswith("ID") and "NAME" in l
                            for l in lines))


class TestJsonSafe(unittest.TestCase):
    """Sonar pass-through tracks carry bare nan/inf live; strict JSON
    output (allow_nan=False) must never see them."""

    def _frame(self, ship):
        return at.build_frame({"ship_state": ship})

    def test_nan_inf_stripped_recursively(self):
        ss = ship_state_fixture()
        ss["sonar"] = {"count": 2, "tracks": [
            {"id": "T1", "range": float("nan"),
             "sensors": [{"range": float("nan"), "bearing": 12.5}]},
            {"id": "T2", "range": float("inf"), "bearing": 45},
        ]}
        fr = self._frame(ss)
        tracks = fr["sonar"]["tracks"]
        self.assertIsNone(tracks[0]["range"])
        self.assertIsNone(tracks[0]["sensors"][0]["range"])
        self.assertAlmostEqual(tracks[0]["sensors"][0]["bearing"], 12.5)
        self.assertIsNone(tracks[1]["range"])
        self.assertEqual(tracks[1]["bearing"], 45)
        json.dumps(fr, allow_nan=False)     # must not raise

    def test_no_sonar_section(self):
        fr = self._frame(ship_state_fixture())
        # fixture has no sonar section -> passthrough stays falsy
        self.assertFalse(fr["sonar"]["tracks"])


class TestProbeBusy(unittest.TestCase):
    """PROBE BUSY banner when ship_state stalls (game tick holes)."""

    def _frame(self, age):
        fr = at.build_frame({"ship_state": ship_state_fixture()})
        fr["ages"]["ship_state"] = age
        return fr

    def test_fresh_data_no_banner(self):
        for age in (None, 0.0, 3.0, at.PROBE_BUSY_S):
            lines = at.flatten(at.render_frame_lines(self._frame(age), 90))
            self.assertFalse(any("PROBE BUSY" in l for l in lines), age)

    def test_stalled_data_shows_banner_with_age(self):
        lines = at.flatten(at.render_frame_lines(self._frame(22.4), 90))
        busy = [l for l in lines if "PROBE BUSY" in l]
        self.assertEqual(len(busy), 1)
        self.assertIn("22s", busy[0])

    def test_probe_busy_age_helper(self):
        self.assertIsNone(at.probe_busy_age({"ages": {}}))
        self.assertIsNone(at.probe_busy_age({"ages": {"ship_state": None}}))
        self.assertIsNone(at.probe_busy_age(
            {"ages": {"ship_state": at.PROBE_BUSY_S}}))
        self.assertEqual(at.probe_busy_age(
            {"ages": {"ship_state": 16.0}}), 16.0)

    def test_banner_truncates_on_narrow_width(self):
        lines = at.flatten(at.render_frame_lines(self._frame(30.0), 40))
        busy = [l for l in lines if "PROBE BUSY" in l]
        self.assertEqual(len(busy), 1)
        self.assertLessEqual(len(busy[0]), 40)

    def test_curses_draw_consumes_two_head_lines(self):
        # draw() slices render_frame_lines[:2]; with a banner the second
        # line must be the banner so the curses path shows it too
        rows = at.render_frame_lines(self._frame(30.0), 80)
        self.assertIn("PROBE BUSY", "".join(t for t, _ in rows[1]))


class TestDatalinkJournal(unittest.TestCase):
    """DATALINK panel: transition-only event journal (BRIEF_datalink_history)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aitac_dl_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _elem(self, eid, asg=None, iord=None):
        return {"id": eid,
                "assignment_id": asg if asg is not None else -1,
                "incoming_order": {"assignment_id": iord} if iord is not None
                else {}}

    def test_order_and_adopted_transitions(self):
        prev = {17: self._elem(17)}
        cur = {17: self._elem(17, asg=7, iord=9)}
        evs = at.dl_delta_events(prev, cur, 1000.0)
        kinds = [(e["kind"], e["detail"]) for e in evs]
        self.assertIn(("ORDER", "asg#9"), kinds)
        self.assertIn(("ADOPTED", "asg#7"), kinds)
        for e in evs:
            self.assertEqual(e["ts_epoch"], 1000.0)
            self.assertEqual(e["eid"], 17)

    def test_baseline_and_unchanged_produce_nothing(self):
        base = {17: self._elem(17, asg=3, iord=2)}
        self.assertEqual(at.dl_delta_events({}, base, 1.0), [])   # baseline
        self.assertEqual(at.dl_delta_events(base, base, 1.0), []) # unchanged

    def test_negative_or_missing_ids_ignored(self):
        prev = {5: self._elem(5)}
        cur = {5: self._elem(5, asg=-1, iord=-2)}
        self.assertEqual(at.dl_delta_events(prev, cur, 1.0), [])

    def test_detected_transitions_only(self):
        evs = at.detected_delta_events({16: False}, {16: True}, 5.0)
        self.assertEqual([e["kind"] for e in evs], ["DETECTED"])
        evs = at.detected_delta_events({16: True}, {16: True}, 6.0)
        self.assertEqual(evs, [])
        evs = at.detected_delta_events({16: True}, {16: False}, 7.0)
        self.assertEqual([e["kind"] for e in evs], ["DETCLEAR"])
        # unknown element -> baseline, no event
        self.assertEqual(at.detected_delta_events({}, {9: True}, 8.0), [])

    def test_attack_event_on_ingest(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        col.pending[55] = "ai-attack"
        col._pending_attack_eid[55] = 17
        data = {"now": 123.0,
                "results": {"results": [
                    {"cmdid": 55, "action": "ai-attack", "ok": "true",
                     "result": "PushOrder ok"}]},
                "ai_state": {"elements": []}}
        col._ingest_results(data, [])
        hits = [e for e in col.dl_journal if e["kind"] == "ATTACK-OK"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["eid"], 17)
        self.assertIn("PushOrder", hits[0]["detail"])
        self.assertNotIn(55, col._pending_attack_eid)

    def test_ghost_appear_vanish_events(self):
        col = at.Collector(at.LocalSource(self.tmp), read_only=True)
        data = {"now": 10.0, "ai_state": {"elements": []}}
        col.ns_styles[18] = "helo"
        col._journal_deltas(data)
        self.assertEqual([e["kind"] for e in col.dl_journal], ["GHOST+"])
        col.ns_styles.pop(18)
        col._journal_deltas(data)
        self.assertEqual([e["kind"] for e in col.dl_journal],
                         ["GHOST+", "GHOST-"])
        col._journal_deltas(data)          # unchanged -> nothing new
        self.assertEqual(len(col.dl_journal), 2)

    def test_renderer_styles_filter_and_width(self):
        evs = [{"ts_epoch": 0, "eid": 17, "kind": "ORDER", "detail": "asg#9"},
               {"ts_epoch": 60, "eid": 16, "kind": "DETECTED", "detail": ""},
               {"ts_epoch": 120, "eid": 17, "kind": "ATTACK-FAIL",
                "detail": "no lock"}]
        rows = at.render_datalink_lines(evs, 90, eid_filter=17)
        txts = ["".join(t for t, _ in r) for r in rows]
        self.assertEqual(len(rows), 2)                       # filtered out #16
        self.assertIn("ORDER", txts[0])
        self.assertIn("ATTACK-FAIL", txts[1])                # newest last
        self.assertEqual(rows[0][0][1], "dim")               # timestamp dim
        self.assertEqual(rows[0][1][1], "cyan")              # ORDER cyan
        self.assertEqual(rows[1][1][1], "red")
        wide = at.render_datalink_lines(evs, 40)
        for r in wide:
            self.assertLessEqual(sum(len(t) for t, _ in r), 40)

    def test_frame_roundtrip_and_textmode_detail(self):
        hist = [{"ts_epoch": 30, "eid": 13, "kind": "GHOST+", "detail": "ship"}]
        fr = at.build_frame({"ship_state": ship_state_fixture(),
                             "dl_history": hist})
        self.assertEqual(fr["dl_history"], hist)
        lines = at.flatten(at.render_frame_lines(fr, 90, detail=True))
        self.assertTrue(any("DATALINK:" in l for l in lines))
        self.assertTrue(any("#13" in l and "GHOST+" in l for l in lines))
        plain = at.flatten(at.render_frame_lines(fr, 90))
        self.assertFalse(any("DATALINK:" in l for l in plain))


class TestTargetingSurface(unittest.TestCase):
    """TARGETING fields from the ai-state probe (suspects/tracked/fire)."""

    def _detail_lines(self):
        return ["ai-state: element id=17", "lat=63.51", "lon=5.99",
                "suspects_n=2", "suspects_ids=1,7",
                "tracked_cat=3", "contact_cache=5",
                "target_lat=63.60", "target_lon=5.80",
                "target_course=95.0",
                "fire_domain=Surface(2)", "fire_orient=AttackRun(1)"]

    def test_parse_targeting_keys(self):
        v = at.parse_ai_state_detail(self._detail_lines())
        self.assertEqual(v["target_id"], 17)
        self.assertEqual(v["suspects_n"], 2)
        self.assertEqual(v["suspects_ids"], "1,7")
        self.assertEqual(v["tracked_cat"], 3)
        self.assertEqual(v["contact_cache"], 5.0)
        self.assertAlmostEqual(v["target_lat"], 63.60)
        self.assertAlmostEqual(v["target_lon"], 5.80)
        self.assertAlmostEqual(v["target_course"], 95.0)
        self.assertEqual(v["fire_domain"], "Surface(2)")
        self.assertEqual(v["fire_orient"], "AttackRun(1)")

    def test_normalize_targeting_passthrough(self):
        ext = {17: dict(at.parse_ai_state_detail(self._detail_lines()),
                        ts_epoch=time.time())}
        els = at.normalize_elements(ai_state_fixture(), ship_state_fixture(),
                                    {}, {}, {17: "ship"}, None, {},
                                    ext_map=ext)
        e = [x for x in els if x["id"] == 17][0]
        self.assertEqual(e["suspects_n"], 2)
        self.assertEqual(e["suspects_ids"], "1,7")
        self.assertEqual(e["tracked_cat"], 3)
        self.assertEqual(e["contact_cache"], 5.0)
        self.assertAlmostEqual(e["target_lat"], 63.60)
        self.assertAlmostEqual(e["target_lon"], 5.80)
        self.assertAlmostEqual(e["target_course"], 95.0)
        self.assertEqual(e["fire_domain"], "Surface(2)")
        self.assertEqual(e["fire_orient"], "AttackRun(1)")

    def test_render_detail_targeting_row(self):
        ext = {17: dict(at.parse_ai_state_detail(self._detail_lines()),
                        ts_epoch=time.time())}
        fr = at.build_frame({"ship_state": ship_state_fixture(),
                             "ai_state": ai_state_fixture(),
                             "ns_styles": {17: "ship"},
                             "ext_map": ext})
        els = [e for e in fr["elements"] if e["id"] == 17]
        idx = fr["elements"].index(els[0])
        txt = "\n".join(at.flatten(at.render_detail(fr, idx, 120)))
        self.assertIn("TARGETING:", txt)
        self.assertIn("suspects=2", txt)
        self.assertIn("1,7", txt)
        self.assertIn("tracked_cat=3", txt)
        self.assertIn("cache=5.0", txt)
        self.assertIn("FIRE Surface(2) -> AttackRun(1)", txt)
        # hot element (has suspects) renders red
        rows = at.render_detail(fr, idx, 120)
        hot = [r for r in rows if any("suspects=" in t for t, _ in r)]
        self.assertTrue(hot and any(st == "red" for _, st in hot[0]))

    def test_render_detail_no_targeting_row_without_fields(self):
        ext = {17: {"target_id": 17, "lat": 63.51, "lon": 5.99,
                    "ts_epoch": time.time()}}
        fr = at.build_frame({"ship_state": ship_state_fixture(),
                             "ai_state": ai_state_fixture(),
                             "ns_styles": {17: "ship"},
                             "ext_map": ext})
        idx = [i for i, e in enumerate(fr["elements"]) if e["id"] == 17][0]
        txt = "\n".join(at.flatten(at.render_detail(fr, idx, 120)))
        self.assertNotIn("TARGETING:", txt)

    def test_stale_lock_takeover_config_key(self):
        # probe-side contract: takeover check interval is configurable
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                os.pardir, "ship_probe.py"),
                   encoding="utf-8").read()
        self.assertIn("lock_takeover_check_s", src)
        self.assertIn("_touch_heartbeat", src)


if __name__ == "__main__":
    unittest.main()
