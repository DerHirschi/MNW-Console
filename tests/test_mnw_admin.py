#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mnw_admin helper tests: helo discovery, multi-host ack, drop monitor."""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mnw_admin  # noqa: E402


def _write(path, content):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _log_dir():
    return tempfile.mkdtemp(prefix="mnw_admin_test_")


class HeloDiscoverTest(unittest.TestCase):
    def test_finds_helo_from_nsdump_log(self):
        d = _log_dir()
        try:
            _write(os.path.join(d, "ship_probe_log.txt"),
                   "05:28:43 ns-dump: ns /0/ style=general keys(19)\n"
                   "05:28:43 ns-dump: ns /13/ style=ship keys(89)\n"
                   "05:28:57 ns-dump: ns /0/ style=helo keys(21)\n"
                   "05:28:57 ns-dump: ns /18/ style=helo keys(94)\n")
            found = mnw_admin.discover_helo_elements(d)
            self.assertEqual(found.get(18), 94)
            self.assertEqual(found.get(0), 21)
            self.assertNotIn(13, found)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_empty_log(self):
        d = _log_dir()
        try:
            _write(os.path.join(d, "ship_probe_log.txt"), "")
            self.assertEqual(mnw_admin.discover_helo_elements(d), {})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class AiAttackAckTest(unittest.TestCase):
    def test_ack_found_for_element(self):
        d = _log_dir()
        try:
            since = time.strftime("%H:%M:%S", time.gmtime())
            _write(os.path.join(d, "ship_probe_log.txt"),
                   "%s ai-attack cp10: about to Order(operator=183, tactical=18, assignment=221)\n"
                   "%s ai-attack cp11: Order constructed\n"
                   "%s ai-attack cp13: PushOrder ok\n"
                   % (since, since, since))
            ln = mnw_admin.wait_ai_attack_ack(d, 18, since_ts=since, timeout=3, poll=1)
            self.assertIsNotNone(ln)
            self.assertIn("tactical=18", ln)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_ack_ignores_other_element(self):
        d = _log_dir()
        try:
            since = time.strftime("%H:%M:%S", time.gmtime())
            _write(os.path.join(d, "ship_probe_log.txt"),
                   "%s ai-attack cp10: about to Order(operator=183, tactical=13, assignment=103)\n" % since)
            ln = mnw_admin.wait_ai_attack_ack(d, 18, since_ts=since, timeout=2, poll=1)
            self.assertIsNone(ln)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class DropMonitorTest(unittest.TestCase):
    def _fake_tail(self, lines):
        def read(n=2000):
            return lines
        return read

    def test_detects_drop_block(self):
        lines = [
            "[SwicthScope] Switching Scope of Yu-7_AIR(Clone)0 from Prop to Bot",
            "  Launcher[0] MountedCat:Torpedo -> Packet:(Torpedo, 99990003)",
            "    -> FireSingle: True",
            "  Result: True",
        ]
        out = mnw_admin.monitor_player_log_drop(self._fake_tail(lines), timeout=2, poll=1)
        self.assertTrue(any("Yu-7_AIR(Clone)" in ln for ln in out))
        self.assertTrue(any("99990003" in ln for ln in out))

    def test_timeout_no_drop(self):
        out = mnw_admin.monitor_player_log_drop(self._fake_tail(["just noise"]), timeout=1, poll=1)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
