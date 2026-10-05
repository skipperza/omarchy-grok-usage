#!/usr/bin/python3
"""Shell restart must not hide the agents bar icon.

The stock panel discovers usage files only when its own updater exits. Deleting
grok.json on every service teardown, including an Omarchy update's shell
restart, leaves that scan with nothing to show, so the icon collapses.
"""

from __future__ import annotations

import json
import struct
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


if __name__ == "__main__":
  unittest.main()
