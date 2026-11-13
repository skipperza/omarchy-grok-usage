#!/usr/bin/python3
"""Shell restart must not hide the agents bar icon.

The stock panel discovers usage files only when its own updater exits. Deleting
grok.json on every service teardown, including an Omarchy update's shell
restart, leaves that scan with nothing to show, so the icon collapses.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import struct
import sys
import tempfile
import time
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


def _varint(value: int) -> bytes:
  out = bytearray()
  while True:
    byte = value & 0x7F
    value >>= 7
    if value:
      out.append(byte | 0x80)
    else:
      out.append(byte)
      return bytes(out)


def _proto_key(field: int, wire: int) -> bytes:
  return _varint((field << 3) | wire)


def _proto_varint_field(field: int, value: int) -> bytes:
  return _proto_key(field, 0) + _varint(value)


def _proto_float_field(field: int, value: float) -> bytes:
  return _proto_key(field, 5) + struct.pack("<f", value)


def _proto_len_field(field: int, payload: bytes) -> bytes:
  return _proto_key(field, 2) + _varint(len(payload)) + payload


def _product_field(enum: int, percent: float | None = None) -> bytes:
  body = _proto_varint_field(1, enum)
  if percent is not None:
    body += _proto_float_field(2, percent)
  return _proto_len_field(7, body)


def _config_message(*products: bytes, weekly: float | None = None) -> bytes:
  body = b""
  if weekly is not None:
    body += _proto_float_field(1, weekly)
  return body + b"".join(products)


def _grpc_frame(message: bytes, *, status: str | None = "0", wrap: bool = False) -> bytes:
  if wrap:
    message = _proto_len_field(1, message)
  frame = b"\x00" + len(message).to_bytes(4, "big") + message
  if status is None:
    return frame
  trailer = f"grpc-status:{status}\r\n".encode()
  return frame + b"\x80" + len(trailer).to_bytes(4, "big") + trailer


# Captured GetGrokCreditsConfig frame: weekly float 19, enum 2 float 19, enum 4
# with no percent field, no enum 6. No credentials.
LIVE_CREDITS_FRAME = bytes.fromhex(
  "000000005a0a580d0000984112001a00220c0898e889d60610e0c5d9d903"
  "2a0c0898ddaed60610e0c5d9d9033a07080215000098413a020804"
  "421e0802120c0898e889d60610e0c5d9d9031a0c0898ddaed60610e0c5d9d903"
  "580162006801800000000f677270632d7374617475733a300d0a"
)


class ChatVoiceLimits(unittest.TestCase):
  def test_enum_4_without_percent_is_grok_chat_at_zero(self) -> None:
    # Weekly float and a later product float must not be zipped onto Chat.
    frame = _grpc_frame(
      _config_message(_product_field(4), _product_field(2, 19.0), weekly=19.0),
      wrap=True,
    )
    limits = collect.limits_with_credits(CLI_LIMITS, frame)
    chat = [entry for entry in limits if entry["label"] == "Grok Chat"]

    self.assertEqual(len(chat), 1)
    self.assertEqual(chat[0]["title"], "Grok Chat")
    self.assertEqual(chat[0]["percent"], 0.0)
    self.assertEqual(chat[0]["resetsAt"], CLI_LIMITS[0]["resetsAt"])
    self.assertEqual(next(entry["percent"] for entry in limits if entry["label"] == "Grok Build"), 0.17)
    self.assertNotIn("Imagine", [entry["label"] for entry in limits])

  def test_live_frame_maps_omitted_chat_percent_to_zero(self) -> None:
    limits = collect.limits_with_credits(CLI_LIMITS, LIVE_CREDITS_FRAME)
    labels = [entry["label"] for entry in limits]

    self.assertEqual(labels, ["Weekly", "Grok Build", "Grok Chat", "Grok Voice"])
    self.assertEqual(next(entry["percent"] for entry in limits if entry["label"] == "Grok Chat"), 0.0)
    self.assertEqual(next(entry["percent"] for entry in limits if entry["label"] == "Grok Build"), 0.17)
    self.assertEqual(next(entry["percent"] for entry in limits if entry["label"] == "Grok Voice"), 0.0)
    self.assertEqual(
      next(entry["resetsAt"] for entry in limits if entry["label"] == "Grok Voice"),
      CLI_LIMITS[0]["resetsAt"],
    )

  def test_enum_6_with_percent_is_grok_voice(self) -> None:
    frame = _grpc_frame(_config_message(_product_field(6, 12.5)))
    limits = collect.limits_with_credits(CLI_LIMITS, frame)
    voice = [entry for entry in limits if entry["label"] == "Grok Voice"]

    self.assertEqual(len(voice), 1)
    self.assertEqual(voice[0]["title"], "Grok Voice")
    self.assertAlmostEqual(voice[0]["percent"], 0.125)
    self.assertEqual(voice[0]["resetsAt"], CLI_LIMITS[0]["resetsAt"])

  def test_failed_grpc_call_keeps_grok_build_and_does_not_invent_voice(self) -> None:
    frame = _grpc_frame(
      _config_message(_product_field(6, 40.0), _product_field(4)),
      status="14",
    )

    def fetch() -> tuple[int, bytes]:
      raise OSError("credits config unreachable")

    from_fetch = collect.limits_with_credits(CLI_LIMITS, fetch=fetch)
    from_status = collect.limits_with_credits(CLI_LIMITS, frame)

    for limits in (from_fetch, from_status):
      self.assertEqual([entry["label"] for entry in limits], ["Weekly", "Grok Build"])
      self.assertEqual(limits[1]["percent"], 0.17)
      self.assertEqual(limits[1]["title"], "Grok Build")

  def test_explicit_rest_chat_percent_is_not_replaced_with_zero(self) -> None:
    limits = CLI_LIMITS + [{
      "label": "Grok Chat",
      "title": "Grok Chat",
      "percent": 0.42,
      "resetsAt": "2026-10-11T15:43:20.993420+00:00",
    }]
    frame = _grpc_frame(_config_message(_product_field(4)), wrap=True)
    merged = collect.limits_with_credits(limits, frame)
    chat = [entry for entry in merged if entry["label"] == "Grok Chat"]

    self.assertEqual(len(chat), 1)
    self.assertEqual(chat[0]["percent"], 0.42)

  def test_rest_chat_row_without_percent_is_zero_when_grpc_fails(self) -> None:
    limits = collect.limits_with_credits(
      CLI_LIMITS,
      rest_products=[
        {"product": "GrokBuild", "usagePercent": 17},
        {"product": "GrokChat"},
      ],
      fetch=lambda: (_ for _ in ()).throw(OSError("down")),
    )

    self.assertEqual(
      [entry["label"] for entry in limits],
      ["Weekly", "Grok Build", "Grok Chat"],
    )
    self.assertEqual(limits[2]["percent"], 0.0)
    self.assertEqual(limits[2]["resetsAt"], CLI_LIMITS[0]["resetsAt"])

  def test_imagine_api_and_other_need_an_explicit_percent(self) -> None:
    omitted = collect.limits_with_credits(
      CLI_LIMITS,
      _grpc_frame(_config_message(_product_field(1), _product_field(3), _product_field(5))),
    )
    present = collect.limits_with_credits(
      CLI_LIMITS,
      _grpc_frame(_config_message(_product_field(5, 3.0))),
    )

    self.assertNotIn("API", [entry["label"] for entry in omitted])
    self.assertNotIn("Other", [entry["label"] for entry in omitted])
    self.assertNotIn("Imagine", [entry["label"] for entry in omitted])
    imagine = next(entry for entry in present if entry["label"] == "Imagine")
    self.assertAlmostEqual(imagine["percent"], 0.03)

  def test_chat_and_voice_are_inserted_ahead_of_grok_bot(self) -> None:
    limits = CLI_LIMITS + [{
      "label": "Grok Bot",
      "title": "Grok Bot",
      "percent": 0.044,
      "resetsAt": "2026-10-10T08:09:00+00:00",
    }]
    frame = _grpc_frame(_config_message(_product_field(4), _product_field(6, 8.0)), wrap=True)
    merged = collect.limits_with_credits(limits, frame)

    self.assertEqual(
      [entry["label"] for entry in merged],
      ["Weekly", "Grok Build", "Grok Chat", "Grok Voice", "Grok Bot"],
    )
    self.assertEqual(merged[2]["percent"], 0.0)
    self.assertAlmostEqual(merged[3]["percent"], 0.08)
    self.assertEqual(merged[4]["percent"], 0.044)


class UnifiedBilling(unittest.TestCase):
  def test_combined_pool_is_one_weekly_bar(self) -> None:
    # Product rows are shares of one pool (10 + 1 = 11), not separate limits.
    payload = {
      "config": {
        "currentPeriod": {
          "type": "USAGE_PERIOD_TYPE_WEEKLY",
          "end": "2026-11-01T06:38:01.689+00:00",
        },
        "creditUsagePercent": 11.0,
        "productUsage": [
          {"product": "GrokBot", "usagePercent": 10.0},
          {"product": "Api", "usagePercent": 1.0},
        ],
        "isUnifiedBillingUser": True,
      }
    }
    calls: list[int] = []

    result = collect.limits_from_payload(payload, fetch=lambda: calls.append(1) or (200, b"unused"))

    self.assertEqual(calls, [])
    self.assertTrue(result["unified"])
    self.assertEqual([entry["label"] for entry in result["limits"]], ["Weekly"])
    self.assertEqual(result["limits"][0]["title"], "Weekly 11%")
    self.assertAlmostEqual(result["limits"][0]["percent"], 0.11)
    self.assertTrue(str(result["limits"][0]["resetsAt"]).startswith("2026-11-01T06:38:01"))

  def test_unified_probe_does_not_add_grok_bot(self) -> None:
    original_probe = collect.probe_limits
    original_bot = collect.limits_with_grok_bot
    called: list[str] = []

    def probe(_token: str) -> dict:
      return {
        "ok": True,
        "unified": True,
        "tierLabel": "",
        "limits": [{
          "label": "Weekly",
          "title": "Weekly",
          "percent": 0.11,
          "resetsAt": "2026-11-01T06:38:01+00:00",
        }],
      }

    def bot(limits: list, *_args, **_kwargs) -> dict:
      called.append("bot")
      return {"limits": limits + [{"label": "Grok Bot", "title": "Grok Bot", "percent": 0.2, "resetsAt": ""}], "authHelpText": ""}

    collect.probe_limits = probe
    collect.limits_with_grok_bot = bot
    try:
      result = collect.collect_limits("token", "2099-01-01T00:00:00+00:00", True)
    finally:
      collect.probe_limits = original_probe
      collect.limits_with_grok_bot = original_bot

    self.assertEqual(called, [])
    self.assertEqual([entry["label"] for entry in result["limits"]], ["Weekly"])


class TokenStats(unittest.TestCase):
  def test_cached_input_is_not_counted_twice(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      session = Path(tmp) / "folder" / "session"
      session.mkdir(parents=True)
      usage = {
        "inputTokens": 100,
        "outputTokens": 10,
        "cachedReadTokens": 80,
        "cacheCreationTokens": 0,
        "totalTokens": 110,
        "modelUsage": {
          "grok-4.7-build": {
            "inputTokens": 100,
            "outputTokens": 10,
            "cachedReadTokens": 80,
            "cacheCreationTokens": 0,
            "totalTokens": 110,
          }
        },
      }
      line = {
        "timestamp": time.time(),
        "params": {
          "sessionId": "session",
          "update": {
            "sessionUpdate": "turn_completed",
            "prompt_id": "prompt-1",
            "usage": usage,
          },
        },
      }
      (session / "updates.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

      stats = collect.scan_sessions(Path(tmp))

    self.assertEqual(stats["todayTotalTokens"], 110)
    today = next(day for day in stats["recentDays"] if day["messageCount"] == 110)
    self.assertEqual(today["messageCount"], 110)
    bucket = stats["modelUsage"]["grok-4.7-build"]
    counted = (
      bucket["inputTokens"]
      + bucket["outputTokens"]
      + bucket["cacheReadInputTokens"]
      + bucket["cacheCreationInputTokens"]
    )
    self.assertEqual(counted, 110)

  def test_each_weekday_total_is_its_own_row(self) -> None:
    rows = collect.day_meters({
      "recentDays": [
        {"date": "2026-10-20", "messageCount": 2_487_368},
        {"date": "2026-10-21", "messageCount": 0},
        {"date": "2026-10-26", "messageCount": 4_059_756},
      ],
    })

    self.assertEqual([row["title"] for row in rows], ["Tue", "Wed", "Mon"])
    self.assertEqual([row["valueText"] for row in rows], ["2.5M", "0", "4.1M"])
    self.assertTrue(all(row["percent"] == 0 and row["resetsAt"] == "" for row in rows))

  def test_caption_names_today_and_this_week(self) -> None:
    caption = collect.usage_caption({
      "todayTotalTokens": 7_899_756,
      "recentDays": [
        {"date": "2026-10-20", "messageCount": 4_800_000},
        {"date": "2026-10-26", "messageCount": 7_899_756},
      ],
    })

    self.assertEqual(caption, "7.9M today · 12.7M this week")

  def test_caption_includes_lifetime_tokens(self) -> None:
    caption = collect.usage_caption({
      "todayTotalTokens": 7_899_756,
      "recentDays": [
        {"date": "2026-10-20", "messageCount": 4_800_000},
        {"date": "2026-10-26", "messageCount": 7_899_756},
      ],
      "modelUsage": {
        "grok-4.7-build": {
          "inputTokens": 1_000_000_000,
          "outputTokens": 200_000_000,
          "cacheReadInputTokens": 36_000_000,
          "cacheCreationInputTokens": 0,
        },
      },
    })

    self.assertEqual(caption, "7.9M today · 12.7M this week · 1.2B lifetime")

  def test_caption_names_lifetime_when_this_week_is_empty(self) -> None:
    caption = collect.usage_caption({
      "todayTotalTokens": 0,
      "recentDays": [],
      "lifetimeTotalTokens": 1_500,
    })

    self.assertEqual(caption, "1.5K lifetime")

  def test_scan_records_lifetime_token_total(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      session = Path(tmp) / "folder" / "session"
      session.mkdir(parents=True)
      usage = {
        "inputTokens": 100,
        "outputTokens": 10,
        "cachedReadTokens": 80,
        "cacheCreationTokens": 0,
        "totalTokens": 110,
      }
      line = {
        "timestamp": time.time(),
        "params": {
          "sessionId": "session",
          "update": {
            "sessionUpdate": "turn_completed",
            "prompt_id": "prompt-1",
            "usage": usage,
          },
        },
      }
      (session / "updates.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

      stats = collect.scan_sessions(Path(tmp))

    self.assertEqual(stats["lifetimeTotalTokens"], 110)

  def test_record_publishes_lifetime_when_the_scan_cache_omits_it(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      home = root / "grok"
      (home / "sessions").mkdir(parents=True)
      cache = root / "cache" / "omarchy" / "agent-usage"
      cache.mkdir(parents=True)
      (cache / "grok-sessions.json").write_text(json.dumps({
        "todayPrompts": 1,
        "todaySessions": 1,
        "todayTotalTokens": 110,
        "todayTokensByModel": {},
        "recentDays": [{"date": "2026-10-31", "messageCount": 110}],
        "modelUsage": {
          "grok-4.7-build": {
            "inputTokens": 20,
            "outputTokens": 10,
            "cacheReadInputTokens": 80,
            "cacheCreationInputTokens": 0,
          },
        },
        "totalPrompts": 1,
        "totalSessions": 1,
        "activeDays": 1,
        "activeDates": ["2026-10-31"],
      }), encoding="utf-8")
      old_home = os.environ.get("GROK_HOME")
      old_cache = os.environ.get("XDG_CACHE_HOME")
      old_hermes = os.environ.get("HERMES_HOME")
      os.environ["GROK_HOME"] = str(home)
      os.environ["XDG_CACHE_HOME"] = str(root / "cache")
      os.environ["HERMES_HOME"] = str(root / "hermes")
      try:
        record = collect.build_record(False, False, 900)
      finally:
        if old_home is None:
          os.environ.pop("GROK_HOME", None)
        else:
          os.environ["GROK_HOME"] = old_home
        if old_cache is None:
          os.environ.pop("XDG_CACHE_HOME", None)
        else:
          os.environ["XDG_CACHE_HOME"] = old_cache
        if old_hermes is None:
          os.environ.pop("HERMES_HOME", None)
        else:
          os.environ["HERMES_HOME"] = old_hermes

    self.assertEqual(record["lifetimeTotalTokens"], 110)
    self.assertEqual(record["tierLabel"], "110 today · 110 this week · 110 lifetime")

  def test_open_turn_tokens_count_before_turn_completed(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp) / "grok"
      session = home / "sessions" / "folder" / "open-session"
      session.mkdir(parents=True)
      (session / "updates.jsonl").write_text("", encoding="utf-8")
      now = time.time()
      event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "msg": "shell.turn.inference_done",
        "sid": "open-session",
        "ctx": {
          "loop_index": 1,
          "prompt_tokens": 1000,
          "cached_prompt_tokens": 800,
          "completion_tokens": 50,
        },
      }
      log_dir = home / "logs"
      log_dir.mkdir()
      (log_dir / "unified.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

      stats = collect.scan_sessions(home / "sessions")

    self.assertEqual(stats["todayTotalTokens"], 1050)
    today = next(day for day in stats["recentDays"] if day["date"] == time.strftime("%Y-%m-%d"))
    self.assertEqual(today["messageCount"], 1050)
    self.assertEqual(stats["lifetimeTotalTokens"], 1050)

  def test_finished_turn_inference_is_not_counted_twice(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp) / "grok"
      session = home / "sessions" / "folder" / "done-session"
      session.mkdir(parents=True)
      completed = time.time() - 30
      line = {
        "timestamp": completed,
        "params": {
          "sessionId": "done-session",
          "update": {
            "sessionUpdate": "turn_completed",
            "prompt_id": "prompt-1",
            "usage": {
              "inputTokens": 100,
              "outputTokens": 10,
              "cachedReadTokens": 80,
              "cacheCreationTokens": 0,
              "totalTokens": 110,
            },
          },
        },
      }
      (session / "updates.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
      event = {
        "ts": dt.datetime.fromtimestamp(completed + 0.4, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "msg": "shell.turn.inference_done",
        "sid": "done-session",
        "ctx": {
          "loop_index": 3,
          "prompt_tokens": 1000,
          "cached_prompt_tokens": 800,
          "completion_tokens": 50,
        },
      }
      log_dir = home / "logs"
      log_dir.mkdir()
      (log_dir / "unified.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

      stats = collect.scan_sessions(home / "sessions")

    self.assertEqual(stats["todayTotalTokens"], 110)
    self.assertEqual(stats["lifetimeTotalTokens"], 110)

  def test_inference_after_the_skew_window_counts_as_the_next_turn(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp) / "grok"
      session = home / "sessions" / "folder" / "done-session"
      session.mkdir(parents=True)
      completed = time.time() - 30
      line = {
        "timestamp": completed,
        "params": {
          "sessionId": "done-session",
          "update": {
            "sessionUpdate": "turn_completed",
            "prompt_id": "prompt-1",
            "usage": {
              "inputTokens": 100,
              "outputTokens": 10,
              "cachedReadTokens": 80,
              "totalTokens": 110,
            },
          },
        },
      }
      (session / "updates.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
      event = {
        "ts": dt.datetime.fromtimestamp(completed + 5, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "msg": "shell.turn.inference_done",
        "sid": "done-session",
        "ctx": {
          "loop_index": 1,
          "prompt_tokens": 1000,
          "cached_prompt_tokens": 800,
          "completion_tokens": 50,
        },
      }
      log_dir = home / "logs"
      log_dir.mkdir()
      (log_dir / "unified.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

      stats = collect.scan_sessions(home / "sessions")

    self.assertEqual(stats["todayTotalTokens"], 1160)

  def test_open_turn_is_read_from_the_end_of_a_long_log(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      home = Path(tmp) / "grok"
      session = home / "sessions" / "folder" / "open-session"
      session.mkdir(parents=True)
      (session / "updates.jsonl").write_text("", encoding="utf-8")
      event = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "msg": "shell.turn.inference_done",
        "sid": "open-session",
        "ctx": {"loop_index": 1, "prompt_tokens": 1000, "cached_prompt_tokens": 0, "completion_tokens": 50},
      })
      log_dir = home / "logs"
      log_dir.mkdir()
      (log_dir / "unified.jsonl").write_text(("pad\n" * 40) + event + "\n", encoding="utf-8")
      old_limit = collect.MAX_SESSION_FILE_BYTES
      # The cap starts inside the padding, so only a tail read still sees the event.
      collect.MAX_SESSION_FILE_BYTES = len(event) + 8
      try:
        stats = collect.scan_sessions(home / "sessions")
      finally:
        collect.MAX_SESSION_FILE_BYTES = old_limit

    self.assertEqual(stats["todayTotalTokens"], 1050)

  def test_hermes_same_day_fills_that_day_and_a_span_does_not_dump(self) -> None:
    today = collect.local_date_string()
    noon = dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    yesterday = noon - 86400
    stats = {
      "recentDays": [
        {"date": collect.date_string(dt.date.fromtimestamp(yesterday)), "messageCount": 0},
        {"date": today, "messageCount": 10},
      ],
      "todayTotalTokens": 10,
      "lifetimeTotalTokens": 10,
    }
    same_day = {"key": "default:short:grok-4.7", "total": 100, "first_seen": noon, "last_seen": noon + 60}
    spanning = {"key": "default:long:grok-4.7", "total": 5000, "first_seen": yesterday, "last_seen": noon}

    ledger = collect.add_hermes_usage(stats, [same_day, spanning], {})

    self.assertEqual(stats["recentDays"][1]["messageCount"], 110)
    self.assertEqual(stats["recentDays"][0]["messageCount"], 0)
    self.assertEqual(stats["todayTotalTokens"], 110)
    self.assertEqual(ledger["default:long:grok-4.7"], 5000)

    spanning["total"] = 5400
    ledger = collect.add_hermes_usage(stats, [same_day, spanning], ledger)

    self.assertEqual(stats["recentDays"][1]["messageCount"], 610)
    self.assertEqual(stats["todayTotalTokens"], 610)


if __name__ == "__main__":
  unittest.main()
