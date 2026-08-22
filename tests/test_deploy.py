#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deployer tests: patch_script / strip_patch idempotency, round-trip, CRLF."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deploy  # noqa: E402

MARKER = "# ship_probe piggyback v1"


def make_script(text=None, crlf=False):
    nl = "\r\n" if crlf else "\n"
    body = text if text is not None else (
        "def _random_tick_():%s"
        "    if self is None:%s"
        "        return%s"
    )
    return (body % (nl, nl, nl)).encode("utf-8")


class PatchScriptTest(unittest.TestCase):
    def test_injects_block_into_random_tick(self):
        data = make_script()
        out = deploy.patch_script(data).decode("utf-8")
        self.assertIn(MARKER, out)
        self.assertIn("ship_probe.ship_probe_tick(globals())", out)
        self.assertIn("try:", out)
        self.assertIn("except Exception:", out)
        # block sits inside _random_tick_, before any following def
        self.assertLess(out.index(MARKER), out.index("def ") if "def " in out[out.index(MARKER):] else len(out))

    def test_idempotent(self):
        data = make_script()
        once = deploy.patch_script(data)
        twice = deploy.patch_script(once)
        self.assertEqual(once, twice)
        self.assertIn(MARKER, once.decode("utf-8"))

    def test_strip_restores_original(self):
        data = make_script()
        patched = deploy.patch_script(data)
        stripped = deploy.strip_patch(patched)
        self.assertEqual(stripped, data)

    def test_strip_idempotent(self):
        data = make_script()
        patched = deploy.patch_script(data)
        once = deploy.strip_patch(patched)
        twice = deploy.strip_patch(once)
        self.assertEqual(once, twice)

    def test_crlf_handling(self):
        data = make_script(crlf=True)
        patched = deploy.patch_script(data)
        self.assertIn(b"\r\n", patched)
        self.assertIn(MARKER.encode(), patched)
        self.assertEqual(deploy.strip_patch(patched), data)

    def test_no_random_tick_untouched(self):
        data = b"def _start_():\n    pass\n"
        self.assertEqual(deploy.patch_script(data), data)
        self.assertEqual(deploy.strip_patch(data), data)

    def test_no_marker_no_strip(self):
        data = make_script()
        self.assertEqual(deploy.strip_patch(data), data)

    def test_legacy_block_upgraded(self):
        data = make_script()
        legacy = data.decode("utf-8").replace(
            "    if self is None:",
            "    # ship_probe piggyback v1\n"
            "    try:\n"
            "        import ship_probe\n"
            "        ship_probe.ship_probe_tick()\n"
            "    except Exception:\n"
            "        pass\n"
            "    if self is None:",
        ).encode("utf-8")
        out = deploy.patch_script(legacy).decode("utf-8")
        self.assertIn("ship_probe.ship_probe_tick(globals())", out)
        self.assertEqual(out.count(MARKER), 1)

    def test_block_inserted_after_body(self):
        data = b"def _random_tick_():\n    a = 1\n    b = 2\n"
        out = deploy.patch_script(data).decode("utf-8")
        self.assertIn("    a = 1\n    b = 2\n    # ship_probe piggyback v1\n", out)


class StripRegexTest(unittest.TestCase):
    def test_strip_any_call_form(self):
        variants = [
            "ship_probe.ship_probe_tick(globals())",
            "ship_probe.ship_probe_tick()",
            "ship_probe.ship_probe_tick(globals() )",
        ]
        for call in variants:
            text = (
                "def _random_tick_():\n"
                "    a = 1\n"
                "    # ship_probe piggyback v1\n"
                "    try:\n"
                "        import ship_probe\n"
                "        %s\n"
                "    except Exception:\n"
                "        pass\n"
                "    b = 2\n" % call
            ).encode("utf-8")
            stripped = deploy.strip_patch(text).decode("utf-8")
            self.assertNotIn(MARKER, stripped)
            self.assertNotIn("ship_probe.ship_probe_tick", stripped)
            self.assertIn("    a = 1", stripped)
            self.assertIn("    b = 2", stripped)


if __name__ == "__main__":
    unittest.main()
