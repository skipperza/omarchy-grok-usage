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


CLI_LIMITS = [
  {
    "label": "Weekly",
    "title": "Weekly",
    "percent": 0.17,
    "resetsAt": "2026-10-11T15:43:20.993420+00:00",
  },
  {
    "label": "Grok Build",
    "title": "Grok Build",
    "percent": 0.17,
    "resetsAt": "2026-10-11T15:43:20.993420+00:00",
  },
]

# Chromium v10 blob of a fake JWT, AES-128-CBC, password "peanuts".
PEANUTS_ACCESS_BLOB = "djEw2i+kX0+bTlgL15+Rrl1dRUkyKdd5/0PY8y9ryA719PNL63x564svagiRmZkfDl+pLDWa235mxCDZt8HI6cGVkg=="
FIXTURE_ACCESS_TOKEN = "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTl9.c2ln"

SAND_PAYLOAD = {
  "usagePercent": 23.5,
  "nextResetTimestampUtc": "2026-10-12T07:00:00.000Z",
  "grokPlanLabel": "SuperGrok",
  "hasAvailableUsage": True,
}


def write_sand_secrets(path: Path, access_blob: str) -> None:
  payload = {
    "cursor-accounts": json.dumps({
      "active": "acct",
      "accounts": {"acct": {"cursor-access-token": access_blob}},
    }),
  }
  path.write_text(json.dumps(payload))


class GrokBotLimit(unittest.TestCase):
  def test_sand_usage_payload_becomes_grok_bot_limit(self) -> None:
    entry = collect.sand_usage_limit(SAND_PAYLOAD)

    self.assertIsNotNone(entry)
    assert entry is not None
    self.assertEqual(entry["label"], "Grok Bot")
    self.assertEqual(entry["title"], "Grok Bot")
    self.assertAlmostEqual(entry["percent"], 0.235)
    self.assertTrue(str(entry["resetsAt"]).startswith("2026-10-12T07:00:00"))

  def test_omitted_usage_percent_does_not_invent_a_meter(self) -> None:
    self.assertIsNone(collect.sand_usage_limit({
      "hasAvailableUsage": True,
      "grokPlanLabel": "SuperGrok",
      "nextResetTimestampUtc": "2026-10-12T07:00:00.000Z",
    }))
    self.assertIsNone(collect.sand_usage_limit({"usagePercent": None, "hasAvailableUsage": False}))

  def test_zero_usage_percent_is_kept(self) -> None:
    entry = collect.sand_usage_limit({
      "usagePercent": 0,
      "nextResetTimestampUtc": "2026-10-12T07:00:00Z",
    })

    self.assertIsNotNone(entry)
    assert entry is not None
    self.assertEqual(entry["percent"], 0.0)
    self.assertEqual(entry["label"], "Grok Bot")

  def test_missing_secret_keeps_grok_build(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = collect.limits_with_grok_bot(
        CLI_LIMITS,
        Path(tmp) / "sand-secrets.json",
        passwords=["peanuts"],
        fetch=lambda _token: (_ for _ in ()).throw(AssertionError("fetch must not run")),
      )

    self.assertEqual([entry["label"] for entry in result["limits"]], ["Weekly", "Grok Build"])
    self.assertEqual(result["limits"][1]["percent"], 0.17)
    self.assertEqual(result["authHelpText"], "Sign in to Grok Bot to show its weekly limit.")

  def test_undecryptable_secret_keeps_grok_build(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "sand-secrets.json"
      write_sand_secrets(path, "bm90LWEtdjEwLWJsb2I")
      result = collect.limits_with_grok_bot(
        CLI_LIMITS,
        path,
        passwords=["peanuts"],
        fetch=lambda _token: (_ for _ in ()).throw(AssertionError("fetch must not run")),
      )

    self.assertEqual([entry["label"] for entry in result["limits"]], ["Weekly", "Grok Build"])
    self.assertEqual(result["limits"][1]["title"], "Grok Build")
    self.assertEqual(result["authHelpText"], "Sign in to Grok Bot to show its weekly limit.")

  def test_fixture_payload_is_merged_beside_grok_build(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "sand-secrets.json"
      write_sand_secrets(path, PEANUTS_ACCESS_BLOB)
      seen: list[str] = []

      def fetch(token: str) -> tuple[int, dict]:
        seen.append(token)
        return 200, SAND_PAYLOAD

      result = collect.limits_with_grok_bot(
        CLI_LIMITS,
        path,
        passwords=["peanuts"],
        fetch=fetch,
      )

    self.assertEqual(seen, [FIXTURE_ACCESS_TOKEN])
    self.assertEqual(
      [entry["label"] for entry in result["limits"]],
      ["Weekly", "Grok Build", "Grok Bot"],
    )
    self.assertAlmostEqual(result["limits"][2]["percent"], 0.235)
    self.assertTrue(str(result["limits"][2]["resetsAt"]).startswith("2026-10-12T07:00:00"))
    self.assertEqual(result["authHelpText"], "")

  def test_success_without_percent_does_not_drop_grok_build(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "sand-secrets.json"
      write_sand_secrets(path, PEANUTS_ACCESS_BLOB)
      result = collect.limits_with_grok_bot(
        CLI_LIMITS,
        path,
        passwords=["peanuts"],
        fetch=lambda _token: (200, {"hasAvailableUsage": True, "grokPlanLabel": "SuperGrok"}),
      )

    self.assertEqual([entry["label"] for entry in result["limits"]], ["Weekly", "Grok Build"])
    self.assertEqual(result["authHelpText"], "")


if __name__ == "__main__":
  unittest.main()
