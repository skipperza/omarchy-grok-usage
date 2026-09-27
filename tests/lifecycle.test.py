#!/usr/bin/python3
"""Shell restart must not hide the agents bar icon.

The stock panel discovers usage files only when its own updater exits. Deleting
grok.json on every service teardown, including an Omarchy update's shell
restart, leaves that scan with nothing to show, so the icon collapses.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collect  # noqa: E402


PLUGIN_ID = "io.github.dougfour.grok-usage"


def write_config(path: Path, plugins: list, disabled: list | None = None) -> None:
  payload = {"plugins": plugins}
  if disabled is not None:
    payload["disabledPlugins"] = disabled
  path.write_text(json.dumps(payload))


class ClearOnTeardown(unittest.TestCase):
  def test_enabled_plugin_keeps_grok_record(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "shell.json"
      record = root / "grok.json"
      write_config(config, [{"id": PLUGIN_ID}])
      record.write_text('{"id":"grok"}\n')

      removed = collect.clear_if_disabled(config, record)

      self.assertFalse(removed)
      self.assertEqual(record.read_text(), '{"id":"grok"}\n')

  def test_disabled_plugin_removes_grok_record(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "shell.json"
      record = root / "grok.json"
      write_config(config, [], [PLUGIN_ID])
      record.write_text('{"id":"grok"}\n')

      removed = collect.clear_if_disabled(config, record)

      self.assertTrue(removed)
      self.assertFalse(record.exists())

  def test_disabled_flag_removes_record_even_if_still_listed(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      config = root / "shell.json"
      record = root / "grok.json"
      write_config(config, [{"id": PLUGIN_ID}], [PLUGIN_ID])
      record.write_text('{"id":"grok"}\n')

      removed = collect.clear_if_disabled(config, record)

      self.assertTrue(removed)
      self.assertFalse(record.exists())

  def test_unreadable_config_keeps_grok_record(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      record = root / "grok.json"
      record.write_text('{"id":"grok"}\n')

      removed = collect.clear_if_disabled(root / "missing.json", record)

      self.assertFalse(removed)
      self.assertTrue(record.exists())


class PanelDiscovery(unittest.TestCase):
  def test_new_record_asks_the_panel_to_rescan(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "grok.json"
      nudged = []

      created = collect.publish_record({"id": "grok"}, dest, nudged.append)

      self.assertTrue(created)
      self.assertEqual(json.loads(dest.read_text())["id"], "grok")
      self.assertEqual(nudged, ["rescan"])

  def test_existing_record_does_not_ask_for_another_rescan(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "grok.json"
      dest.write_text('{"id":"old"}\n')
      nudged = []

      created = collect.publish_record({"id": "grok"}, dest, nudged.append)

      self.assertFalse(created)
      self.assertEqual(json.loads(dest.read_text())["id"], "grok")
      self.assertEqual(nudged, [])


if __name__ == "__main__":
  unittest.main()
