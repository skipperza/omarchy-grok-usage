#!/usr/bin/python3
"""Collect Grok CLI usage into one Omarchy agents-panel JSON record.

Omarchy's stock updater only ships Claude, Codex, and Fireworks collectors.
This writes the same record contract for Grok from:

- ~/.grok/sessions/**/updates.jsonl  (per-turn token usage, once a turn finishes)
- ~/.grok/logs/unified.jsonl  (tokens already spent on a turn that is still open)
- ~/.hermes/state.db and each profile state.db (Grok tokens spent in Hermes, by the day the session ran)
- https://cli-chat-proxy.grok.com/v1/billing?format=credits  (one combined weekly pool, or the older per-product pools)
- https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig  (Grok Chat and Grok Voice on a split plan)
- Grok Bot's Cursor sand session (its own meter, only on a split plan)

The agents panel discovers ~/.local/state/omarchy/agents/usage/*.json only
when its own updater exits. A grok.json that appears after that scan stays
invisible, and with no other agent producing numbers the bar icon hides.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable

AGENT_ID = "grok"
AGENT_NAME = "Grok"
PLUGIN_CONFIG_ID = "io.github.dougfour.grok-usage"
AUTH_HELP = "Run `grok login` to restore weekly usage limits."
BOT_AUTH_HELP = "Sign in to Grok Bot to show its weekly limit."
BOT_UNREACHABLE_HELP = "Couldn't reach Grok Bot. Other Grok limits are still shown."
BILLING_ENDPOINT = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
CREDITS_CONFIG_ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
# Empty grpc-web data frame. Cloudflare 1010s urllib's default User-Agent.
CREDITS_CONFIG_BODY = b"\x00\x00\x00\x00\x00"
BROWSER_USER_AGENT = (
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CURSOR_BACKEND = "https://api2.cursor.sh"
SAND_USAGE_RPC = CURSOR_BACKEND + "/aiserver.v1.DashboardService/GetSandUsageStatus"
CURSOR_TOKEN_URL = CURSOR_BACKEND + "/oauth/token"
# Public Cursor installed-app OAuth client id, not a user secret.
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
# Public xAI CLI OAuth token endpoint. The client id lives in auth.json.
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
# Renew before the 5-minute poll can paint "Sign-in expired".
REFRESH_SKEW_SECONDS = 600
USAGE_HOSTS = frozenset({"cli-chat-proxy.grok.com", "api2.cursor.sh", "grok.com", "auth.x.ai"})
CHAT_LABEL = "Grok Chat"
VOICE_LABEL = "Grok Voice"
# GetGrokCreditsConfig product enum. Do not remap.
_CREDITS_ENUM_LABEL = {
  1: "API",
  2: "Grok Build",
  3: "Other",
  4: CHAT_LABEL,
  5: "Imagine",
  6: VOICE_LABEL,
}
PROBE_MIN_INTERVAL_SECONDS = 15
MAX_AUTH_FILE_BYTES = 256 * 1024
MAX_CACHE_FILE_BYTES = 1024 * 1024
MAX_SESSION_FILE_BYTES = 32 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 256 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_TOKEN_CHARS = 16 * 1024
# The last inference_done of a finished turn is logged just after that turn's
# timestamp. Keep this window or the same tokens are counted twice.
OPEN_TURN_SKEW_SECONDS = 2
INFERENCE_DONE_MSG = "shell.turn.inference_done"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def expand_path(value: str) -> Path:
  return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def grok_home() -> Path:
  return expand_path(os.environ.get("GROK_HOME") or "~/.grok")


def cache_root() -> Path:
  root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy" / "agent-usage"
  root.mkdir(parents=True, exist_ok=True)
  return root


def usage_dir() -> Path:
  state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
  path = state / "omarchy" / "agents" / "usage"
  path.mkdir(parents=True, exist_ok=True)
  return path


def date_string(value: dt.date) -> str:
  return value.strftime("%Y-%m-%d")


def recent_date_strings() -> list[str]:
  today = dt.datetime.now().date()
  return [date_string(today - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def local_date_string() -> str:
  return date_string(dt.datetime.now().date())


def unix_timestamp(value: Any) -> float | None:
  """Unix seconds from an epoch number, millisecond epoch, or ISO-8601 string."""
  if value is None:
    return None
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return None
    if text.endswith("Z"):
      text = text[:-1] + "+00:00"
    try:
      parsed = dt.datetime.fromisoformat(text)
    except ValueError:
      return None
    if parsed.tzinfo is None:
      parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()
  try:
    seconds = float(value)
  except (TypeError, ValueError):
    return None
  if seconds != seconds:
    return None
  if seconds > 10_000_000_000:
    seconds /= 1000.0
  return seconds


def local_date_from_timestamp(value: Any) -> str:
  seconds = unix_timestamp(value)
  if seconds is None:
    return local_date_string()
  try:
    return date_string(dt.datetime.fromtimestamp(seconds).date())
  except Exception:
    return local_date_string()


def read_text_bounded(path: Path, max_bytes: int) -> str | None:
  """Read a regular file, keeping the tail when it is larger than max_bytes.

  Open turns are at the end of the log. A cap that kept only the start would
  drop the tokens the panel is missing.
  """
  flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
  try:
    fd = os.open(path, flags)
  except OSError:
    return None
  skipped_prefix = False
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
      return None
    skipped_prefix = info.st_size > max_bytes
    if skipped_prefix:
      os.lseek(fd, info.st_size - max_bytes, os.SEEK_SET)
    data = os.read(fd, max_bytes)
  except OSError:
    return None
  finally:
    try:
      os.close(fd)
    except OSError:
      pass
  text = data.decode("utf-8", errors="replace")
  if skipped_prefix:
    newline = text.find("\n")
    text = "" if newline < 0 else text[newline + 1 :]
  return text


def number(value: Any) -> int:
  try:
    n = float(value or 0)
    return round(n) if n == n else 0
  except Exception:
    return 0


def empty_bucket() -> dict[str, int]:
  return {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
  }


def empty_stats() -> dict[str, Any]:
  recent = [{"date": day, "messageCount": 0} for day in recent_date_strings()]
  return {
    "todayPrompts": 0,
    "todaySessions": 0,
    "todayTotalTokens": 0,
    "todayTokensByModel": {},
    "recentDays": recent,
    "modelUsage": {},
    "totalPrompts": 0,
    "totalSessions": 0,
    "activeDays": 0,
    "activeDates": [],
    "lifetimeTotalTokens": 0,
  }


def open_regular_file(path: Path, max_bytes: int) -> int | None:
  """Open path only if the fd is a regular file within max_bytes.

  Type and size are taken from fstat on the descriptor after open, with
  O_NOFOLLOW so a same-user symlink or fifo swap cannot block the collector
  or redirect the read.
  """
  flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
  try:
    fd = os.open(path, flags)
  except OSError:
    return None
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > max_bytes:
      os.close(fd)
      return None
    return fd
  except OSError:
    os.close(fd)
    return None


def read_regular_file(path: Path, max_bytes: int) -> bytes | None:
  fd = open_regular_file(path, max_bytes)
  if fd is None:
    return None
  try:
    data = os.read(fd, max_bytes + 1)
  except OSError:
    os.close(fd)
    return None
  os.close(fd)
  if len(data) > max_bytes:
    return None
  return data


def read_limited_http_body(response: BinaryIO, max_bytes: int) -> bytes:
  length = None
  headers = getattr(response, "headers", None)
  if headers is not None:
    raw = headers.get("Content-Length")
    if raw not in (None, ""):
      try:
        length = int(raw)
      except (TypeError, ValueError):
        length = None
  if length is not None and (length < 0 or length > max_bytes):
    raise ValueError("HTTP body exceeds size limit")

  chunks: list[bytes] = []
  total = 0
  while True:
    chunk = response.read(min(65536, max_bytes - total + 1))
    if not chunk:
      break
    total += len(chunk)
    if total > max_bytes:
      raise ValueError("HTTP body exceeds size limit")
    chunks.append(chunk)
  return b"".join(chunks)


def lock_regular_file(path: Path) -> int | None:
  """Create or open a lock file without following a symlink, then flock it.

  Path.open('w') truncates after a same-user symlink swap. O_NOFOLLOW plus
  fstat on the fd keeps that from pointing at another file we own.
  """
  flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
  try:
    fd = os.open(path, flags, 0o600)
  except OSError:
    return None
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
      os.close(fd)
      return None
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd
  except OSError:
    os.close(fd)
    return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
  tmp = Path(tmp_name)
  try:
    with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
      handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
  except BaseException:
    tmp.unlink(missing_ok=True)
    raise


def read_fresh_json(path: Path, max_age_seconds: float) -> dict[str, Any] | None:
  if max_age_seconds <= 0:
    return None
  fd = open_regular_file(path, MAX_CACHE_FILE_BYTES)
  if fd is None:
    return None
  try:
    info = os.fstat(fd)
    if time.time() - info.st_mtime > max_age_seconds:
      return None
    raw = os.read(fd, MAX_CACHE_FILE_BYTES + 1)
  except OSError:
    return None
  finally:
    os.close(fd)
  if len(raw) > MAX_CACHE_FILE_BYTES:
    return None
  try:
    data = json.loads(raw.decode("utf-8"))
  except Exception:
    return None
  return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- local scan


def scan_sessions(sessions_dir: Path) -> dict[str, Any]:
  today = local_date_string()
  recent_dates = recent_date_strings()
  recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}

  seen: set[str] = set()
  sessions: set[str] = set()
  known_sessions: set[str] = set()
  last_completed: dict[str, float] = {}
  active_days: set[str] = set()
  today_sessions: set[str] = set()
  today_tokens: dict[str, int] = {}
  usage_by_model: dict[str, dict[str, int]] = {}
  prompts = 0
  today_prompt_count = 0
  today_token_total = 0
  lifetime_total = 0

  files = sessions_dir.glob("*/*/updates.jsonl") if sessions_dir.is_dir() else []
  for path in files:
    session_id = path.parent.name
    fd = open_regular_file(path, MAX_SESSION_FILE_BYTES)
    if fd is None:
      continue
    known_sessions.add(session_id)
    try:
      with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
        fd = -1
        total_read = 0
        for line in handle:
          total_read += len(line)
          if total_read > MAX_SESSION_FILE_BYTES:
            break
          if len(line) > MAX_JSONL_LINE_BYTES:
            continue
          if "turn_completed" not in line or '"usage"' not in line:
            continue
          try:
            entry = json.loads(line)
          except Exception:
            continue

          params = entry.get("params") if isinstance(entry, dict) else None
          update = params.get("update") if isinstance(params, dict) else None
          if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
            continue
          usage = update.get("usage")
          if not isinstance(usage, dict):
            continue

          prompt_id = str(update.get("prompt_id") or "")
          sid = str(params.get("sessionId") or session_id)
          known_sessions.add(sid)
          completed_at = unix_timestamp(entry.get("timestamp"))
          if completed_at is not None:
            for completed_id in (sid, session_id):
              previous = last_completed.get(completed_id)
              if previous is None or completed_at > previous:
                last_completed[completed_id] = completed_at
          key = sid + ":" + (prompt_id or str(entry.get("timestamp") or ""))
          if key in seen:
            continue
          seen.add(key)

          input_tokens, output_tokens, cache_read, cache_write = accounted_tokens(usage)
          total = input_tokens + output_tokens + cache_read + cache_write
          if total <= 0:
            continue

          meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
          day = local_date_from_timestamp(meta.get("agentTimestampMs") or entry.get("timestamp"))
          models = usage.get("modelUsage") if isinstance(usage.get("modelUsage"), dict) else {}
          model = next(iter(models), None) or "grok"
          model = str(model).rstrip("/").split("/")[-1] or "grok"

          sessions.add(sid)
          active_days.add(day)
          prompts += 1
          lifetime_total += total
          bucket = usage_by_model.setdefault(model, empty_bucket())
          bucket["inputTokens"] += input_tokens
          bucket["outputTokens"] += output_tokens
          bucket["cacheReadInputTokens"] += cache_read
          bucket["cacheCreationInputTokens"] += cache_write
          if day in recent:
            recent[day]["messageCount"] += total
          if day == today:
            today_prompt_count += 1
            today_sessions.add(sid)
            today_token_total += total
            today_tokens[model] = today_tokens.get(model, 0) + total
    except OSError:
      continue
    finally:
      if fd >= 0:
        try:
          os.close(fd)
        except OSError:
          pass

  if sessions_dir.name == "sessions":
    log_text = read_text_bounded(sessions_dir.parent / "logs" / "unified.jsonl", MAX_SESSION_FILE_BYTES)
  else:
    log_text = None
  if log_text:
    seen_inference: set[tuple[Any, ...]] = set()
    for line in log_text.splitlines():
      if INFERENCE_DONE_MSG not in line or len(line) > MAX_JSONL_LINE_BYTES:
        continue
      try:
        entry = json.loads(line)
      except Exception:
        continue
      if not isinstance(entry, dict) or entry.get("msg") != INFERENCE_DONE_MSG:
        continue
      sid = str(entry.get("sid") or "")
      if sid not in known_sessions:
        continue
      when = unix_timestamp(entry.get("ts"))
      if when is None:
        continue
      cutoff = last_completed.get(sid)
      if cutoff is not None and when <= cutoff + OPEN_TURN_SKEW_SECONDS:
        continue
      ctx = entry.get("ctx") if isinstance(entry.get("ctx"), dict) else {}
      prompt = number(ctx.get("prompt_tokens"))
      completion = number(ctx.get("completion_tokens"))
      cached = number(ctx.get("cached_prompt_tokens"))
      if cached > prompt:
        cached = prompt
      total = prompt + completion
      if total <= 0:
        continue
      key = (sid, when, ctx.get("loop_index"), prompt, completion)
      if key in seen_inference:
        continue
      seen_inference.add(key)

      day = local_date_from_timestamp(when)
      model = "grok"
      sessions.add(sid)
      active_days.add(day)
      lifetime_total += total
      bucket = usage_by_model.setdefault(model, empty_bucket())
      bucket["inputTokens"] += prompt - cached
      bucket["outputTokens"] += completion
      bucket["cacheReadInputTokens"] += cached
      if day in recent:
        recent[day]["messageCount"] += total
      if day == today:
        today_sessions.add(sid)
        today_token_total += total
        today_tokens[model] = today_tokens.get(model, 0) + total

  if lifetime_total <= 0:
    return empty_stats()
  return {
    "todayPrompts": today_prompt_count,
    "todaySessions": len(today_sessions),
    "todayTotalTokens": today_token_total,
    "todayTokensByModel": today_tokens,
    "recentDays": [recent[day] for day in recent_dates],
    "modelUsage": usage_by_model,
    "totalPrompts": prompts,
    "totalSessions": len(sessions),
    "activeDays": len(active_days),
    "activeDates": sorted(active_days),
    "lifetimeTotalTokens": lifetime_total,
  }


def cached_scan(sessions_dir: Path, max_age_seconds: float) -> dict[str, Any]:
  cache_file = cache_root() / "grok-sessions.json"
  lock_file = cache_root() / "grok-sessions.lock"
  cached = read_fresh_json(cache_file, max_age_seconds)
  if cached is not None and "totalPrompts" in cached:
    return cached
  lock_fd = lock_regular_file(lock_file)
  if lock_fd is None:
    return scan_sessions(sessions_dir)
  try:
    cached = read_fresh_json(cache_file, max_age_seconds)
    if cached is not None and "totalPrompts" in cached:
      return cached
    stats = scan_sessions(sessions_dir)
    write_json(cache_file, stats)
    return stats
  finally:
    try:
      fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
      pass
    os.close(lock_fd)


# ------------------------------------------------------------------- limits


def load_auth(auth_path: Path) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
  raw = read_regular_file(auth_path, MAX_AUTH_FILE_BYTES)
  if raw is None:
    return None
  try:
    data = json.loads(raw.decode("utf-8"))
  except Exception:
    return None
  if not isinstance(data, dict):
    return None
  for key, entry in data.items():
    if not isinstance(entry, dict):
      continue
    token = str(entry.get("key") or "").strip()
    if token:
      return data, str(key), entry
  return None


def access_token(auth_path: Path) -> tuple[str, str]:
  loaded = load_auth(auth_path)
  if loaded is None:
    return "", ""
  _data, _key, entry = loaded
  return str(entry.get("key") or "").strip(), str(entry.get("expires_at") or "")


def token_due(expires_at: str, skew_seconds: int = REFRESH_SKEW_SECONDS) -> bool:
  """True when the access token is missing an expiry, expired, or inside the skew."""
  if not expires_at:
    return True
  try:
    parsed = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
  except Exception:
    return True
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=dt.timezone.utc)
  return parsed <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=skew_seconds)


def refresh_access(client_id: str, refresh_token: str) -> dict[str, Any] | None:
  """Exchange a saved refresh token. Returns new fields, never logs them."""
  client = _header_safe(client_id)
  refresh = _header_safe(refresh_token)
  if client is None or refresh is None:
    return None
  body = urllib.parse.urlencode({
    "grant_type": "refresh_token",
    "refresh_token": refresh,
    "client_id": client,
  }).encode("utf-8")
  try:
    status, payload = usage_request(
      XAI_TOKEN_URL,
      method="POST",
      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
      data=body,
    )
  except Exception:
    return None
  if status != 200:
    return None
  try:
    parsed = json.loads(payload.decode("utf-8"))
  except Exception:
    return None
  if not isinstance(parsed, dict):
    return None
  access = _header_safe(parsed.get("access_token"))
  if access is None:
    return None
  try:
    expires_in = int(parsed.get("expires_in") or 0)
  except (TypeError, ValueError):
    expires_in = 0
  if expires_in <= 0:
    return None
  rotated = parsed.get("refresh_token")
  return {
    "key": access,
    "expires_in": expires_in,
    "refresh_token": rotated if isinstance(rotated, str) and _header_safe(rotated) else None,
  }


def write_auth(auth_path: Path, data: dict[str, Any]) -> None:
  auth_path.parent.mkdir(parents=True, exist_ok=True)
  handle_fd, tmp_name = tempfile.mkstemp(dir=auth_path.parent, prefix=auth_path.name + ".", suffix=".tmp")
  tmp = Path(tmp_name)
  try:
    with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
      json.dump(data, handle, indent=2)
      handle.write("\n")
    tmp.chmod(0o600)
    tmp.replace(auth_path)
  except BaseException:
    tmp.unlink(missing_ok=True)
    raise


def ensure_fresh_auth(auth_path: Path) -> tuple[str, str]:
  """Renew an expired Grok CLI login from its saved refresh token.

  A failed refresh leaves the file alone so the panel can still say sign-in
  expired instead of pretending a dead token works.
  """
  loaded = load_auth(auth_path)
  if loaded is None:
    return "", ""
  _data, _key, entry = loaded
  token = str(entry.get("key") or "").strip()
  expires = str(entry.get("expires_at") or "")
  if token and not token_due(expires):
    return token, expires

  lock_fd = lock_regular_file(auth_path.parent / "auth.lock")
  if lock_fd is None:
    return token, expires
  try:
    loaded = load_auth(auth_path)
    if loaded is None:
      return "", ""
    data, key, entry = loaded
    token = str(entry.get("key") or "").strip()
    expires = str(entry.get("expires_at") or "")
    if token and not token_due(expires):
      return token, expires
    client_id = str(entry.get("oidc_client_id") or "")
    refresh = str(entry.get("refresh_token") or "")
    renewed = refresh_access(client_id, refresh)
    if renewed is None:
      return token, expires
    expires_at = (
      dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=renewed["expires_in"])
    ).isoformat().replace("+00:00", "Z")
    updated = dict(entry)
    updated["key"] = renewed["key"]
    updated["expires_at"] = expires_at
    if renewed["refresh_token"]:
      updated["refresh_token"] = renewed["refresh_token"]
    data[key] = updated
    write_auth(auth_path, data)
    return renewed["key"], expires_at
  finally:
    try:
      fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError:
      pass
    os.close(lock_fd)


def token_expired(expires_at: str) -> bool:
  if not expires_at:
    return False
  try:
    parsed = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
  except Exception:
    return False
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=dt.timezone.utc)
  return parsed <= dt.datetime.now(dt.timezone.utc)


def accounted_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int]:
  """Uncached input, output, cache read, cache write.

  Grok's inputTokens already includes cached reads: totalTokens is input plus
  output. Counting the cache again doubles the day and week totals the panel
  shows. A total that already adds the cache on top is left as reported.
  """
  raw_input = number(usage.get("inputTokens"))
  output = number(usage.get("outputTokens"))
  cached = number(usage.get("cachedReadTokens") or usage.get("cacheReadInputTokens"))
  created = number(usage.get("cacheCreationTokens") or usage.get("cachedWriteTokens"))
  reported = number(usage.get("totalTokens")) if usage.get("totalTokens") is not None else None
  if reported is not None and reported == raw_input + output + cached + created:
    return raw_input, output, cached, created
  if cached > 0 and raw_input >= cached:
    return raw_input - cached, output, cached, created
  return raw_input, output, cached, created


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def percent_text(fraction: float) -> str:
  """The whole-number percent the panel otherwise hides until hover."""
  points = fraction * 100
  whole = round(points)
  if whole == 0 and points > 0:
    return f"{points:.1f}%"
  return f"{int(whole)}%"


def with_visible_percent(entry: dict[str, Any]) -> dict[str, Any]:
  copied = dict(entry)
  base = str(copied.get("label") or "Weekly").strip()
  if base.endswith("%"):
    base = base.rsplit(" ", 1)[0].strip() or "Weekly"
  try:
    fraction = float(copied.get("percent"))
  except (TypeError, ValueError):
    return copied
  copied["label"] = base
  copied["title"] = f"{base} {percent_text(fraction)}"
  return copied


def weekday_name(iso_date: str) -> str:
  try:
    parsed = dt.date.fromisoformat(str(iso_date))
  except ValueError:
    return ""
  return _WEEKDAYS[parsed.weekday()]


def day_meters(stats: dict[str, Any]) -> list[dict[str, Any]]:
  """One row per recent day. The weekday stays on the left; the token total is valueText.

  The Agents panel draws valueText on the right of the meter. The meter stays
  empty. A filled day bar would look like another allowance percent, and
  anything at 90% lights the icon as if the subscription were running out.
  """
  days = stats.get("recentDays")
  if not isinstance(days, list):
    return []
  rows: list[dict[str, Any]] = []
  for day in days:
    if not isinstance(day, dict):
      continue
    name = weekday_name(str(day.get("date") or ""))
    if not name:
      continue
    total = compact_count(number(day.get("messageCount")))
    rows.append({
      "label": name,
      "title": name,
      "valueText": total,
      "percent": 0,
      "resetsAt": "",
    })
  return rows


def compact_count(value: int) -> str:
  n = max(0, int(value))
  if n >= 1_000_000_000:
    return f"{n / 1_000_000_000:.1f}B"
  if n >= 1_000_000:
    return f"{n / 1_000_000:.1f}M"
  if n >= 1_000:
    return f"{n / 1_000:.1f}K"
  return str(n)


def lifetime_tokens(stats: dict[str, Any]) -> int:
  """All tokens still on disk. Cached input is already split out of the buckets."""
  if "lifetimeTotalTokens" in stats:
    return number(stats.get("lifetimeTotalTokens"))
  models = stats.get("modelUsage")
  if not isinstance(models, dict):
    return 0
  total = 0
  for bucket in models.values():
    if not isinstance(bucket, dict):
      continue
    total += (
      number(bucket.get("inputTokens"))
      + number(bucket.get("outputTokens"))
      + number(bucket.get("cacheReadInputTokens"))
      + number(bucket.get("cacheCreationInputTokens"))
    )
  return total


def usage_caption(stats: dict[str, Any]) -> str:
  """Today, this week, and lifetime, for the line beside the Grok name."""
  today = number(stats.get("todayTotalTokens"))
  week = 0
  days = stats.get("recentDays")
  if isinstance(days, list):
    for day in days:
      if isinstance(day, dict):
        week += number(day.get("messageCount"))
  parts: list[str] = []
  if today > 0 or week > 0:
    parts.append(f"{compact_count(today)} today · {compact_count(week)} this week")
  lifetime = lifetime_tokens(stats)
  if lifetime > 0:
    parts.append(f"{compact_count(lifetime)} lifetime")
  return " · ".join(parts)


def tier_from_settings(home: Path) -> str:
  raw = read_regular_file(home / "settings_cache.json", MAX_AUTH_FILE_BYTES)
  if raw is None:
    return ""
  try:
    cached = json.loads(raw.decode("utf-8"))
    payload = json.loads(cached.get("payload") or "{}") if isinstance(cached, dict) else {}
  except Exception:
    return ""
  settings = payload.get("settings") if isinstance(payload, dict) else None
  if not isinstance(settings, dict):
    return ""
  return str(settings.get("subscription_tier_display") or "").strip()


def normalize_percent(value: Any) -> float:
  try:
    n = float(value)
  except (TypeError, ValueError):
    return -1.0
  if n != n or n < 0:
    return -1.0
  # Grok billing reports 40.0 for forty percent.
  return min(1.0, n / 100.0)


def product_label(raw: Any) -> str:
  text = str(raw or "").strip()
  if not text:
    return "Grok"
  spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
  spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
  return spaced.strip() or text


def normalize_reset_at(value: Any) -> str:
  raw = str(value or "").strip()
  if not raw:
    return ""
  try:
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.isoformat()
  except Exception:
    return raw


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
  """Fail closed on 3xx so a bearer or refresh body cannot leave the original host."""

  def http_error_302(self, req, fp, code, msg, headers):
    raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

  http_error_301 = http_error_302
  http_error_303 = http_error_302
  http_error_307 = http_error_302
  http_error_308 = http_error_302


_USAGE_OPENER = urllib.request.build_opener(_RefuseRedirect)


def _allowed_usage_url(url: str) -> bool:
  parts = urllib.parse.urlsplit(url)
  host = (parts.hostname or "").lower()
  if parts.scheme.lower() != "https" or parts.username or parts.password:
    return False
  return host in USAGE_HOSTS


def _header_safe(value: Any) -> str | None:
  if not isinstance(value, str):
    return None
  if not value or len(value) > MAX_TOKEN_CHARS:
    return None
  if any(ch in value for ch in ("\r", "\n", "\x00")):
    return None
  return value


def usage_request(
  url: str,
  *,
  method: str = "GET",
  headers: dict[str, str] | None = None,
  data: bytes | None = None,
  timeout: float = 10,
) -> tuple[int, bytes]:
  if not _allowed_usage_url(url):
    raise ValueError("usage request host is not allowed")
  request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
  with _USAGE_OPENER.open(request, timeout=timeout) as response:
    return int(getattr(response, "status", 200)), read_limited_http_body(response, MAX_HTTP_BODY_BYTES)


def probe_limits(token: str) -> dict[str, Any]:
  safe = _header_safe(token)
  if safe is None:
    return {"ok": False, "helpText": "Grok sign-in is no longer valid. Run `grok login`. Local Grok stats are still shown."}
  try:
    _status, body = usage_request(
      BILLING_ENDPOINT,
      headers={
        "Authorization": "Bearer " + safe,
        "Accept": "application/json",
      },
    )
    payload = json.loads(body.decode("utf-8", errors="replace"))
  except urllib.error.HTTPError as error:
    try:
      error.read(MAX_HTTP_BODY_BYTES)
    except Exception:
      pass
    if error.code in (401, 403):
      return {"ok": False, "helpText": "Grok sign-in is no longer valid. Run `grok login`. Local Grok stats are still shown."}
    return {"ok": False, "helpText": f"Grok billing returned status {error.code}. Local Grok stats are still shown."}
  except Exception:
    return {
      "ok": False,
      "transport": True,
      "helpText": "Couldn't reach Grok billing. Retrying shortly. Local Grok stats are still shown.",
    }

  parsed = limits_from_payload(payload, fetch=lambda: fetch_credits_config(safe))
  if not parsed.get("ok"):
    return {"ok": False, "helpText": parsed.get("helpText") or "Grok billing returned no usage. Local Grok stats are still shown."}
  return {
    "ok": True,
    "limits": parsed["limits"],
    "tierLabel": parsed.get("tierLabel") or "",
    "unified": parsed.get("unified") is True,
  }


def _window_label(period: dict[str, Any]) -> str:
  kind = str(period.get("type") or "")
  return "Weekly" if "WEEK" in kind.upper() else "Monthly"


def _unified_window(config: dict[str, Any]) -> dict[str, Any]:
  """The one combined pool. Product rows are shares of it, not extra meters."""
  period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
  label = _window_label(period)
  resets_at = normalize_reset_at(period.get("end") or config.get("billingPeriodEnd"))
  percent = normalize_percent(config.get("creditUsagePercent") or 0)
  if percent < 0:
    percent = 0.0
  return with_visible_percent({
    "label": label,
    "title": label,
    "percent": percent,
    "resetsAt": resets_at,
  })


def limits_from_payload(
  payload: Any,
  *,
  fetch: Callable[[], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
  """Limits from a billing JSON body. A combined plan is one window."""
  config = payload.get("config") if isinstance(payload, dict) else None
  if not isinstance(config, dict):
    return {
      "ok": False,
      "unified": False,
      "limits": [],
      "tierLabel": "",
      "helpText": "Grok billing returned no usage. Local Grok stats are still shown.",
    }

  tier = str(config.get("subscriptionTier") or (payload.get("subscriptionTier") if isinstance(payload, dict) else "") or "").strip()
  if config.get("isUnifiedBillingUser") is True:
    return {"ok": True, "unified": True, "limits": [_unified_window(config)], "tierLabel": tier}

  period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
  resets_at = normalize_reset_at(period.get("end") or config.get("billingPeriodEnd"))
  limits: list[dict[str, Any]] = []

  weekly = normalize_percent(config.get("creditUsagePercent"))
  if weekly >= 0:
    limits.append({"label": "Weekly", "title": "Weekly", "percent": weekly, "resetsAt": resets_at})

  product_rows = config.get("productUsage")
  if not isinstance(product_rows, list):
    product_rows = []
  for entry in product_rows:
    if not isinstance(entry, dict):
      continue
    percent = normalize_percent(entry.get("usagePercent"))
    if percent < 0:
      continue
    name = product_label(entry.get("product"))
    limits.append({"label": name, "title": name, "percent": percent, "resetsAt": resets_at})

  try:
    limits = limits_with_credits(
      limits,
      resets_at=resets_at,
      rest_products=product_rows,
      fetch=fetch,
    )
  except Exception:
    pass

  if not limits:
    return {
      "ok": False,
      "unified": False,
      "limits": [],
      "tierLabel": tier,
      "helpText": "Grok billing returned no limits. Local Grok stats are still shown.",
    }
  return {"ok": True, "unified": False, "limits": limits, "tierLabel": tier}


def fetch_credits_config(token: str) -> tuple[int, bytes]:
  """POST the empty GetGrokCreditsConfig frame. Never follows a redirect."""
  safe = _header_safe(token)
  if safe is None:
    return 401, b""
  try:
    return usage_request(
      CREDITS_CONFIG_ENDPOINT,
      method="POST",
      headers={
        "Content-Type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "Authorization": "Bearer " + safe,
        "x-xai-token-auth": "xai-grok-cli",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/?_s=usage",
        "User-Agent": BROWSER_USER_AGENT,
      },
      data=CREDITS_CONFIG_BODY,
      timeout=15,
    )
  except urllib.error.HTTPError as error:
    try:
      read_limited_http_body(error, MAX_HTTP_BODY_BYTES)
    except Exception:
      pass
    return int(error.code), b""
  except (urllib.error.URLError, TimeoutError, OSError, ValueError):
    return -1, b""


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
  value = 0
  shift = 0
  for _ in range(10):
    if index >= len(buf):
      raise ValueError("truncated varint")
    byte = buf[index]
    index += 1
    value |= (byte & 0x7F) << shift
    if not byte & 0x80:
      return value, index
    shift += 7
  raise ValueError("varint too long")


def _iter_proto_fields(buf: bytes):
  index = 0
  size = len(buf)
  while index < size:
    tag, index = _read_varint(buf, index)
    field, wire = tag >> 3, tag & 7
    if field <= 0:
      raise ValueError("invalid protobuf field")
    if wire == 0:
      value, index = _read_varint(buf, index)
      yield field, wire, value
    elif wire == 1:
      if index + 8 > size:
        raise ValueError("truncated fixed64")
      yield field, wire, struct.unpack_from("<d", buf, index)[0]
      index += 8
    elif wire == 5:
      if index + 4 > size:
        raise ValueError("truncated fixed32")
      yield field, wire, struct.unpack_from("<f", buf, index)[0]
      index += 4
    elif wire == 2:
      length, index = _read_varint(buf, index)
      if length < 0 or index + length > size:
        raise ValueError("truncated bytes")
      yield field, wire, buf[index:index + length]
      index += length
    else:
      raise ValueError("unsupported protobuf wire type")


def _decode_credit_product(buf: bytes) -> dict[str, Any] | None:
  """One field-7 product. A missing field 2 is omitted, not paired with another float."""
  enum = None
  percent = None
  for field, wire, value in _iter_proto_fields(buf):
    if field == 1 and wire == 0 and enum is None:
      enum = int(value)
    elif field == 2 and wire in (1, 5) and percent is None:
      number = float(value)
      if number == number and number >= 0:
        percent = number
  if enum is None:
    return None
  return {"enum": enum, "percent": percent}


def _products_in_message(buf: bytes, depth: int = 0) -> list[dict[str, Any]] | None:
  """Field-7 products, unwrapping a single length-delimited config wrapper."""
  if depth > 3:
    return None
  products: list[dict[str, Any]] = []
  found = False
  nested: list[bytes] = []
  for field, wire, value in _iter_proto_fields(buf):
    if field == 7 and wire == 2 and isinstance(value, bytes):
      found = True
      entry = _decode_credit_product(value)
      if entry is not None:
        products.append(entry)
    elif field == 1 and wire == 2 and isinstance(value, bytes):
      nested.append(value)
  if found:
    return products
  for child in nested:
    inner = _products_in_message(child, depth + 1)
    if inner is not None:
      return inner
  return None


def _grpc_web_products(body: bytes) -> list[dict[str, Any]] | None:
  """Products from a successful grpc-web body, or None when the call failed."""
  frames: list[tuple[int, bytes]] = []
  index = 0
  size = len(body)
  while index + 5 <= size:
    flag = body[index]
    length = int.from_bytes(body[index + 1:index + 5], "big")
    index += 5
    if length < 0 or index + length > size:
      return None
    frames.append((flag, body[index:index + length]))
    index += length
  if index != size or not frames:
    return None

  status = None
  message = None
  for flag, chunk in frames:
    if flag & 0x80:
      text = chunk.decode("utf-8", "replace")
      for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.lower().startswith("grpc-status:"):
          raw = line.split(":", 1)[1].strip()
          try:
            status = int(raw)
          except ValueError:
            return None
    elif flag & 0x01:
      return None
    elif message is None:
      message = chunk
  if status not in (None, 0) or message is None:
    return None
  try:
    products = _products_in_message(message)
  except ValueError:
    return None
  return [] if products is None else products


def _rest_usage(products: Any, label: str) -> tuple[bool, float | None]:
  """(listed, raw percent). An omitted usagePercent is listed with None."""
  if not isinstance(products, list):
    return False, None
  for entry in products:
    if not isinstance(entry, dict) or product_label(entry.get("product")) != label:
      continue
    raw = entry.get("usagePercent")
    if raw is None or normalize_percent(raw) < 0:
      return True, None
    return True, float(raw)
  return False, None


def _explicit_fraction(raw: float | None) -> float | None:
  if raw is None:
    return None
  percent = normalize_percent(raw)
  return percent if percent >= 0 else None


def _insert_meter(limits: list[dict[str, Any]], entry: dict[str, Any]) -> None:
  for index, item in enumerate(limits):
    if item.get("label") == "Grok Bot":
      limits.insert(index, entry)
      return
  limits.append(entry)


def _has_label(limits: list[dict[str, Any]], label: str) -> bool:
  return any(item.get("label") == label for item in limits)


def _first_product(products: list[dict[str, Any]] | None, enum: int) -> dict[str, Any] | None:
  if not products:
    return None
  for entry in products:
    if entry.get("enum") == enum:
      return entry
  return None


def limits_with_credits(
  limits: list[dict[str, Any]],
  body: bytes | None = None,
  *,
  resets_at: str = "",
  rest_products: list[Any] | None = None,
  fetch: Callable[[], tuple[int, bytes]] | None = None,
) -> list[dict[str, Any]]:
  """Add Grok Chat and Grok Voice. A failed credits-config call leaves the other meters."""
  if fetch is not None:
    try:
      status, raw = fetch()
    except Exception:
      status, raw = -1, b""
    body = raw if status == 200 and isinstance(raw, (bytes, bytearray)) and raw else None

  kept = [dict(entry) for entry in limits if isinstance(entry, dict)]
  if not resets_at:
    for entry in kept:
      if entry.get("label") == "Weekly" and entry.get("resetsAt"):
        resets_at = str(entry["resetsAt"])
        break
  resets_at = str(resets_at or "")
  products = _grpc_web_products(body) if isinstance(body, (bytes, bytearray)) and body else None

  if products is not None:
    for enum in (1, 2, 3, 5):
      label = _CREDITS_ENUM_LABEL[enum]
      item = _first_product(products, enum)
      fraction = _explicit_fraction(item.get("percent") if item else None)
      if fraction is None or _has_label(kept, label):
        continue
      _insert_meter(kept, {"label": label, "title": label, "percent": fraction, "resetsAt": resets_at})

  if not _has_label(kept, CHAT_LABEL):
    listed, explicit = _rest_usage(rest_products, CHAT_LABEL)
    grpc_chat = _first_product(products, 4)
    fraction = None
    if explicit is not None:
      fraction = _explicit_fraction(explicit)
    elif grpc_chat is not None and grpc_chat.get("percent") is not None:
      fraction = _explicit_fraction(grpc_chat.get("percent"))
    if fraction is None and (listed or grpc_chat is not None):
      fraction = 0.0
    if fraction is not None:
      _insert_meter(kept, {"label": CHAT_LABEL, "title": CHAT_LABEL, "percent": fraction, "resetsAt": resets_at})

  # Unused voice is omitted from a successful product mix. A failed call must not invent the meter.
  if products is not None and not _has_label(kept, VOICE_LABEL):
    grpc_voice = _first_product(products, 6)
    fraction = _explicit_fraction(grpc_voice.get("percent") if grpc_voice else None)
    if fraction is None:
      fraction = 0.0
    _insert_meter(kept, {"label": VOICE_LABEL, "title": VOICE_LABEL, "percent": fraction, "resetsAt": resets_at})

  return kept


# --------------------------------------------------------------- Grok Bot
#
# Grok Bot's weekly allowance is not in the CLI billing productUsage list.
# The desktop app stores a Cursor sand session in sand-secrets.json, encrypted
# with Chromium OSCrypt (v10). The keyring item is "Grok Bot"; "peanuts" is
# Chromium's public Linux fallback, not a user secret.


_SBOX = bytes([
  0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
  0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
  0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
  0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
  0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
  0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
  0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
  0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
  0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
  0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
  0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
  0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
  0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
  0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
  0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
  0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
])
_INV_SBOX = bytes([_SBOX.index(i) for i in range(256)])
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
  return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF


def _mul(a: int, b: int) -> int:
  out = 0
  for _ in range(8):
    if b & 1:
      out ^= a
    hi = a & 0x80
    a = (a << 1) & 0xFF
    if hi:
      a ^= 0x1B
    b >>= 1
  return out


def _expand_key(key: bytes) -> list[int]:
  expanded = list(key)
  for i in range(4, 44):
    word = expanded[(i - 1) * 4:(i - 1) * 4 + 4]
    if i % 4 == 0:
      word = [_SBOX[word[1]] ^ _RCON[i // 4], _SBOX[word[2]], _SBOX[word[3]], _SBOX[word[0]]]
    prev = expanded[(i - 4) * 4:(i - 4) * 4 + 4]
    expanded.extend(left ^ right for left, right in zip(prev, word))
  return expanded


def _add_round_key(state: list[int], key: list[int], round_n: int) -> None:
  off = round_n * 16
  for i in range(16):
    state[i] ^= key[off + i]


def _inv_shift_rows(state: list[int]) -> None:
  state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
  state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
  state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]


def _inv_sub_bytes(state: list[int]) -> None:
  for i in range(16):
    state[i] = _INV_SBOX[state[i]]


def _inv_mix_columns(state: list[int]) -> None:
  for column in range(4):
    i = column * 4
    a, b, d, e = state[i], state[i + 1], state[i + 2], state[i + 3]
    state[i] = _mul(a, 14) ^ _mul(b, 11) ^ _mul(d, 13) ^ _mul(e, 9)
    state[i + 1] = _mul(a, 9) ^ _mul(b, 14) ^ _mul(d, 11) ^ _mul(e, 13)
    state[i + 2] = _mul(a, 13) ^ _mul(b, 9) ^ _mul(d, 14) ^ _mul(e, 11)
    state[i + 3] = _mul(a, 11) ^ _mul(b, 13) ^ _mul(d, 9) ^ _mul(e, 14)


def _decrypt_block(block: bytes, key: list[int]) -> bytes:
  state = list(block)
  _add_round_key(state, key, 10)
  for round_n in range(9, 0, -1):
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, key, round_n)
    _inv_mix_columns(state)
  _inv_shift_rows(state)
  _inv_sub_bytes(state)
  _add_round_key(state, key, 0)
  return bytes(state)


def aes128_cbc_decrypt(key: bytes, data: bytes, iv: bytes) -> bytes | None:
  if len(key) != 16 or len(iv) != 16 or len(data) < 16 or len(data) % 16:
    return None
  expanded = _expand_key(key)
  prev = iv
  out = bytearray()
  for offset in range(0, len(data), 16):
    block = data[offset:offset + 16]
    plain = bytes(left ^ right for left, right in zip(_decrypt_block(block, expanded), prev))
    out.extend(plain)
    prev = block
  if not out:
    return None
  pad = out[-1]
  if pad < 1 or pad > 16 or out[-pad:] != bytes([pad]) * pad:
    return None
  return bytes(out[:-pad])


def decrypt_chromium_v10(blob_b64: str, password: str) -> bytes | None:
  try:
    raw = base64.b64decode(blob_b64)
  except (ValueError, TypeError):
    return None
  if not raw.startswith(b"v10") or len(raw) <= 19:
    return None
  ciphertext = raw[3:]
  iv = b" " * 16
  for iterations in (1, 1003):
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", iterations, dklen=16)
    plain = aes128_cbc_decrypt(key, ciphertext, iv)
    if plain:
      return plain
  return None


def jwt_from_bytes(data: bytes) -> str | None:
  match = JWT_RE.search(data.decode("utf-8", "replace"))
  return match.group(0) if match else None


def jwt_claims(token: str) -> dict[str, Any]:
  try:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
  except (IndexError, ValueError, json.JSONDecodeError):
    return {}
  return claims if isinstance(claims, dict) else {}


def jwt_expired(token: str, skew_seconds: int = 60) -> bool:
  try:
    return float(jwt_claims(token).get("exp")) <= time.time() + skew_seconds
  except (TypeError, ValueError):
    return False


def libsecret_password(application: str) -> str | None:
  try:
    proc = subprocess.run(
      ["secret-tool", "lookup", "application", application],
      capture_output=True,
      text=True,
      timeout=3,
    )
  except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
    return None
  if proc.returncode != 0:
    return None
  secret = (proc.stdout or "").strip()
  return secret or None


def grok_bot_passwords() -> list[str]:
  passwords: list[str] = []
  stored = libsecret_password("Grok Bot")
  if stored:
    passwords.append(stored)
  passwords.append("peanuts")
  return passwords


def decrypt_grok_bot_secret(blob_b64: str, passwords: list[str]) -> str | None:
  seen: set[str] = set()
  for password in passwords:
    if not password or password in seen:
      continue
    seen.add(password)
    plain = decrypt_chromium_v10(blob_b64, password)
    if not plain:
      continue
    token = jwt_from_bytes(plain)
    if token:
      return token
  return None


def decrypt_secret_text(blob_b64: str, passwords: list[str]) -> str | None:
  """Plaintext of a sand secret. Used for refresh tokens that are not JWTs."""
  seen: set[str] = set()
  for password in passwords:
    if not password or password in seen:
      continue
    seen.add(password)
    plain = decrypt_chromium_v10(blob_b64, password)
    if not plain:
      continue
    text = plain.decode("utf-8", "replace").strip().strip("\x00").strip()
    jwt = jwt_from_bytes(plain)
    if jwt:
      return jwt
    safe = _header_safe(text)
    if safe:
      return safe
  return None


def grok_bot_secrets_path() -> Path:
  override = os.environ.get("GROK_BOT_SECRETS")
  if override:
    return expand_path(override)
  config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
  return config / "Grok Bot" / "sand-secrets.json"


def grok_bot_secret_blobs(path: Path) -> tuple[str, str]:
  raw = read_regular_file(path, MAX_AUTH_FILE_BYTES)
  if raw is None:
    return "", ""
  try:
    data = json.loads(raw.decode("utf-8"))
  except (UnicodeError, json.JSONDecodeError):
    return "", ""
  if not isinstance(data, dict):
    return "", ""
  accounts = data.get("cursor-accounts")
  if isinstance(accounts, str):
    try:
      accounts = json.loads(accounts)
    except json.JSONDecodeError:
      return "", ""
  if not isinstance(accounts, dict):
    return "", ""
  active = accounts.get("active")
  buckets = accounts.get("accounts")
  if not isinstance(active, str) or not isinstance(buckets, dict):
    return "", ""
  entry = buckets.get(active)
  if not isinstance(entry, dict):
    return "", ""
  access = entry.get("cursor-access-token")
  refresh = entry.get("cursor-refresh-token")
  return (
    access if isinstance(access, str) else "",
    refresh if isinstance(refresh, str) else "",
  )


def cursor_checksum(machine_id: str, now_ms: int | None = None) -> str:
  if now_ms is None:
    now_ms = int(time.time() * 1000)
  ks = now_ms // 1_000_000
  buf = bytearray([
    (ks >> 40) & 255,
    (ks >> 32) & 255,
    (ks >> 24) & 255,
    (ks >> 16) & 255,
    (ks >> 8) & 255,
    ks & 255,
  ])
  prev = 165
  for i, byte in enumerate(buf):
    buf[i] = ((byte ^ prev) + (i % 256)) & 255
    prev = buf[i]
  return base64.urlsafe_b64encode(bytes(buf)).decode("ascii").rstrip("=") + machine_id


def cursor_refresh_access(refresh_token: str) -> str | None:
  safe = _header_safe(refresh_token)
  if safe is None:
    return None
  body = json.dumps({
    "client_id": CURSOR_CLIENT_ID,
    "grant_type": "refresh_token",
    "refresh_token": safe,
  }).encode("utf-8")
  try:
    status, raw = usage_request(
      CURSOR_TOKEN_URL,
      method="POST",
      headers={"Content-Type": "application/json", "Accept": "application/json"},
      data=body,
      timeout=15,
    )
  except (urllib.error.URLError, TimeoutError, OSError, ValueError):
    return None
  if status != 200 or not raw:
    return None
  try:
    payload = json.loads(raw.decode("utf-8", errors="replace"))
  except json.JSONDecodeError:
    return None
  if not isinstance(payload, dict):
    return None
  access = payload.get("access_token")
  if not isinstance(access, str):
    access = payload.get("accessToken")
  return _header_safe(access)


def grok_bot_access_token(path: Path, passwords: list[str]) -> str | None:
  access_blob, refresh_blob = grok_bot_secret_blobs(path)
  if not access_blob:
    return None
  access = _header_safe(decrypt_grok_bot_secret(access_blob, passwords))
  if access is None:
    return None
  if not jwt_expired(access):
    return access
  refresh = decrypt_secret_text(refresh_blob, passwords) if refresh_blob else None
  rotated = cursor_refresh_access(refresh) if refresh else None
  if rotated and not jwt_expired(rotated, skew_seconds=0):
    return rotated
  if jwt_expired(access, skew_seconds=0):
    return None
  return access


def fetch_sand_usage(token: str) -> tuple[int, dict[str, Any]]:
  safe = _header_safe(token)
  if safe is None:
    return 401, {}
  headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": "Bearer " + safe,
    "Connect-Protocol-Version": "1",
    "x-cursor-checksum": cursor_checksum(str(uuid.uuid4())),
    "x-cursor-client-type": "sand",
    "x-cursor-client-version": "0.1.0",
    "x-sand-box-namespace": "prod",
    "x-ghost-mode": "true",
    "x-request-id": str(uuid.uuid4()),
  }
  try:
    status, body = usage_request(SAND_USAGE_RPC, method="POST", headers=headers, data=b"{}", timeout=15)
  except urllib.error.HTTPError as error:
    try:
      read_limited_http_body(error, MAX_HTTP_BODY_BYTES)
    except Exception:
      pass
    return int(error.code), {}
  except (urllib.error.URLError, TimeoutError, OSError, ValueError):
    return -1, {}
  if not body:
    return status, {}
  try:
    payload = json.loads(body.decode("utf-8", errors="replace"))
  except json.JSONDecodeError:
    return status, {}
  return status, payload if isinstance(payload, dict) else {}


def reset_instant(value: Any) -> str:
  if value is None or value == "":
    return ""
  if isinstance(value, (int, float)):
    timestamp = float(value)
    if timestamp > 10_000_000_000:
      timestamp /= 1000.0
    try:
      return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
      return ""
  raw = str(value).strip()
  if raw.isdigit():
    return reset_instant(int(raw))
  return normalize_reset_at(raw)


def sand_usage_limit(payload: Any) -> dict[str, Any] | None:
  """One Grok Bot meter, or None when the payload omits a usage percent."""
  if not isinstance(payload, dict):
    return None
  raw_percent = payload.get("usagePercent")
  if raw_percent is None:
    raw_percent = payload.get("usage_percent")
  if raw_percent is None:
    return None
  percent = normalize_percent(raw_percent)
  if percent < 0:
    return None
  raw_reset = payload.get("nextResetTimestampUtc")
  if raw_reset is None:
    raw_reset = payload.get("next_reset_timestamp_utc")
  return {
    "label": "Grok Bot",
    "title": "Grok Bot",
    "percent": percent,
    "resetsAt": reset_instant(raw_reset),
  }


def _cli_limits(limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
  kept: list[dict[str, Any]] = []
  for entry in limits:
    if not isinstance(entry, dict) or entry.get("label") == "Grok Bot":
      continue
    kept.append(dict(entry))
  return kept


def limits_with_grok_bot(
  limits: list[dict[str, Any]],
  secrets_path: Path | None = None,
  *,
  passwords: list[str] | None = None,
  fetch: Callable[[str], tuple[int, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
  """Append a Grok Bot meter. A missing sand session leaves the CLI meters in place."""
  kept = _cli_limits(limits)
  path = grok_bot_secrets_path() if secrets_path is None else secrets_path
  key_passwords = grok_bot_passwords() if passwords is None else passwords
  token = grok_bot_access_token(path, key_passwords)
  if not token:
    return {"limits": kept, "authHelpText": BOT_AUTH_HELP}
  status, payload = (fetch or fetch_sand_usage)(token)
  if status in (401, 403):
    return {"limits": kept, "authHelpText": BOT_AUTH_HELP}
  if status != 200:
    return {"limits": kept, "authHelpText": BOT_UNREACHABLE_HELP}
  entry = sand_usage_limit(payload)
  if entry is None:
    return {"limits": kept, "authHelpText": ""}
  return {"limits": kept + [entry], "authHelpText": ""}


def limit_window_open(entry: dict[str, Any], now: dt.datetime) -> bool:
  raw = str(entry.get("resetsAt") or "")
  if raw == "":
    return True
  try:
    resets_at = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
  except Exception:
    return True
  if resets_at.tzinfo is None:
    resets_at = resets_at.replace(tzinfo=dt.timezone.utc)
  return resets_at > now


def usable_cached_limits(cached: dict[str, Any]) -> list[dict[str, Any]]:
  entries = cached.get("limits")
  if not isinstance(entries, list):
    return []
  now = dt.datetime.now(dt.timezone.utc)
  return [entry for entry in entries if isinstance(entry, dict) and limit_window_open(entry, now)]


def collect_limits(token: str, expires_at: str, force: bool) -> dict[str, Any]:
  result = {"limits": [], "usageStatusText": "", "authHelpText": AUTH_HELP, "tierLabel": ""}
  probe_cache = cache_root() / "grok-limits.json"
  cached = read_fresh_json(probe_cache, float("inf")) or {}
  fallback = usable_cached_limits(cached)
  if isinstance(cached.get("tierLabel"), str):
    result["tierLabel"] = cached["tierLabel"]

  if token == "":
    result["limits"] = fallback
    result["usageStatusText"] = "Waiting for auth"
    return result
  if token_expired(expires_at):
    result["limits"] = fallback
    result["usageStatusText"] = "Sign-in expired"
    result["authHelpText"] = (
      "Grok's saved sign-in expired"
      + (" — showing the last known limits." if fallback else ".")
      + " Start Grok, or run `grok login`, to refresh it."
    )
    return result

  fetched_at = number(cached.get("fetchedAtMs")) / 1000
  # Caches written before the combined-pool flag still list product rows as
  # separate meters. Re-probe those instead of painting the old bars.
  if fallback and "unified" in cached and not force and time.time() - fetched_at < PROBE_MIN_INTERVAL_SECONDS:
    result["limits"] = fallback
    result["usageStatusText"] = ""
    result["authHelpText"] = ""
    return result

  probe = probe_limits(token)
  if probe["ok"]:
    # A combined subscription has one pool. Product rows, voice, and Grok Bot
    # are shares of that pool, so they must not become more bars.
    if probe.get("unified"):
      merged_limits = probe["limits"]
      auth_help = ""
    else:
      merged = limits_with_grok_bot(probe["limits"])
      merged_limits = merged["limits"]
      auth_help = merged["authHelpText"]
    result["limits"] = merged_limits
    result["tierLabel"] = str(probe.get("tierLabel") or result["tierLabel"])
    write_json(probe_cache, {
      "fetchedAtMs": round(time.time() * 1000),
      "limits": merged_limits,
      "tierLabel": result["tierLabel"],
      "unified": probe.get("unified") is True,
    })
    result["usageStatusText"] = ""
    result["authHelpText"] = auth_help
    return result

  if probe.get("transport"):
    result["retryAdvised"] = True
  if fallback:
    result["limits"] = fallback
    result["usageStatusText"] = ""
    result["authHelpText"] = "" if probe.get("transport") else str(probe.get("helpText") or "")
  else:
    result["usageStatusText"] = "Grok limits unavailable"
    result["authHelpText"] = probe["helpText"]
  return result


# -------------------------------------------------------------------- record


def hermes_state_dbs() -> list[tuple[str, Path]]:
  """Default Hermes home plus each profile. Missing databases are skipped."""
  home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
  found: list[tuple[str, Path]] = []
  default = home / "state.db"
  if default.is_file():
    found.append(("default", default))
  profiles = home / "profiles"
  if profiles.is_dir():
    for path in sorted(profiles.glob("*/state.db")):
      if path.is_file():
        found.append((path.parent.name, path))
  return found


def hermes_row_total(inp: Any, out: Any, cache_read: Any, cache_write: Any) -> int:
  """Input, output, and cache. Reasoning is already inside output on this install."""
  return number(inp) + number(out) + number(cache_read) + number(cache_write)


def read_hermes_usage_rows(databases: list[tuple[str, Path]] | None = None) -> list[dict[str, Any]]:
  """One row per Hermes session and model. A locked or unreadable database is skipped."""
  rows: list[dict[str, Any]] = []
  for name, path in databases if databases is not None else hermes_state_dbs():
    try:
      con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
      continue
    try:
      fetched = con.execute(
        """
        SELECT session_id, model, billing_provider, task, first_seen, last_seen,
               input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        FROM session_model_usage
        """
      ).fetchall()
    except sqlite3.Error:
      continue
    finally:
      con.close()
    for session_id, model, provider, task, first_seen, last_seen, inp, out, cache_read, cache_write in fetched:
      total = hermes_row_total(inp, out, cache_read, cache_write)
      if total <= 0 or not session_id:
        continue
      rows.append({
        "key": f"{name}:{session_id}:{model}:{provider}:{task}",
        "total": total,
        "first_seen": first_seen,
        "last_seen": last_seen,
      })
  return rows


def load_hermes_ledger(path: Path | None = None) -> dict[str, int]:
  target = cache_root() / "hermes-usage-ledger.json" if path is None else path
  raw = read_regular_file(target, MAX_CACHE_FILE_BYTES)
  if raw is None:
    return {}
  try:
    data = json.loads(raw.decode("utf-8"))
  except Exception:
    return {}
  totals = data.get("totals") if isinstance(data, dict) else None
  if not isinstance(totals, dict):
    return {}
  return {str(key): number(value) for key, value in totals.items()}


def save_hermes_ledger(totals: dict[str, int], path: Path | None = None) -> None:
  target = cache_root() / "hermes-usage-ledger.json" if path is None else path
  write_json(target, {"totals": totals})


def add_hermes_usage(stats: dict[str, Any], rows: list[dict[str, Any]], ledger: dict[str, int]) -> dict[str, int]:
  """Add Hermes tokens onto the day they were used.

  A session that starts and ends on one day belongs on that day. A session that
  spans days is remembered in the ledger: only tokens added since the last scan
  land on the latest day. The first sight of a spanning session is not dumped
  onto one bar. The published record is rebuilt from the Grok scan each time, so
  a same-day total is added in full, not as a delta.
  """
  days = stats.get("recentDays")
  if not isinstance(days, list):
    return dict(ledger)
  recent = {str(day.get("date")): day for day in days if isinstance(day, dict)}
  today = local_date_string()
  updated = dict(ledger)
  hermes_lifetime = 0
  for row in rows:
    total = number(row.get("total"))
    if total <= 0:
      continue
    hermes_lifetime += total
    key = str(row.get("key") or "")
    first_day = local_date_from_timestamp(row.get("first_seen"))
    last_day = local_date_from_timestamp(row.get("last_seen"))
    previous = ledger.get(key)
    if first_day == last_day:
      day = recent.get(last_day)
      if day is not None:
        day["messageCount"] = number(day.get("messageCount")) + total
      if last_day == today:
        stats["todayTotalTokens"] = number(stats.get("todayTotalTokens")) + total
    elif previous is not None:
      delta = max(0, total - number(previous))
      day_key = last_day if last_day in recent else today
      day = recent.get(day_key)
      if delta and day is not None:
        day["messageCount"] = number(day.get("messageCount")) + delta
      if delta and day_key == today:
        stats["todayTotalTokens"] = number(stats.get("todayTotalTokens")) + delta
    if key:
      updated[key] = total
  stats["lifetimeTotalTokens"] = number(stats.get("lifetimeTotalTokens")) + hermes_lifetime
  return updated


def build_record(force: bool, limits_only: bool, cache_seconds: float) -> dict[str, Any]:
  home = grok_home()
  scan_age = 0 if force else (900 if limits_only else cache_seconds)
  stats = dict(cached_scan(home / "sessions", scan_age))
  stats["lifetimeTotalTokens"] = lifetime_tokens(stats)
  ledger = load_hermes_ledger()
  ledger = add_hermes_usage(stats, read_hermes_usage_rows(), ledger)
  save_hermes_ledger(ledger)
  token, expires_at = ensure_fresh_auth(home / "auth.json")
  limits = collect_limits(token, expires_at, force)

  record = {
    "schemaVersion": 1,
    "id": AGENT_ID,
    "name": AGENT_NAME,
    "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    "ready": number(stats.get("totalPrompts")) > 0 or len(limits["limits"]) > 0,
    "hasLocalStats": number(stats.get("totalPrompts")) > 0,
    "tierLabel": usage_caption(stats) or limits.get("tierLabel") or tier_from_settings(home),
    "usageStatusText": limits["usageStatusText"],
    "authHelpText": limits["authHelpText"],
    "limits": [with_visible_percent(entry) if isinstance(entry, dict) and entry.get("resetsAt") else entry for entry in limits["limits"]] + day_meters(stats),
  }
  if limits.get("retryAdvised"):
    record["retryAdvised"] = True
  record.update(stats)
  return record


def clear_record() -> None:
  dest = usage_dir() / "grok.json"
  dest.unlink(missing_ok=True)


def shell_config_path() -> Path:
  config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
  return config_home / "omarchy" / "shell.json"


def plugin_enabled(config_path: Path | None = None) -> bool:
  """True when shell.json still loads this plugin.

  Unreadable config fails open: a shell restart must not delete grok.json
  just because the file could not be read while the process was exiting.
  """
  path = shell_config_path() if config_path is None else config_path
  try:
    raw = path.read_text(encoding="utf-8")
  except OSError:
    return True
  try:
    config = json.loads(raw)
  except json.JSONDecodeError:
    return True
  if not isinstance(config, dict):
    return True

  disabled = config.get("disabledPlugins")
  if isinstance(disabled, list) and PLUGIN_CONFIG_ID in disabled:
    return False

  def listed(entry: Any) -> bool:
    return entry == PLUGIN_CONFIG_ID or (
      isinstance(entry, dict) and entry.get("id") == PLUGIN_CONFIG_ID
    )

  plugins = config.get("plugins")
  if isinstance(plugins, list) and any(listed(entry) for entry in plugins):
    return True

  bar = config.get("bar")
  layout = bar.get("layout") if isinstance(bar, dict) else None
  if isinstance(layout, dict):
    for section in ("left", "center", "right"):
      entries = layout.get(section)
      if isinstance(entries, list) and any(listed(entry) for entry in entries):
        return True
  return False


def clear_if_disabled(config_path: Path | None = None, dest: Path | None = None) -> bool:
  """Drop grok.json only after the plugin itself has been turned off.

  Omarchy update restarts the shell, which destroys this service even though
  shell.json still enables it. Deleting the record on that path hides the
  agents icon until the stock panel's next scan.
  """
  if plugin_enabled(config_path):
    return False
  target = usage_dir() / "grok.json" if dest is None else dest
  target.unlink(missing_ok=True)
  return True


def publish_record(record: dict[str, Any], dest: Path, nudge: Callable[..., None]) -> bool:
  """Write grok.json. Ask the panel to rescan only when the file is new.

  An update of an existing file is already watched. A rescan on every write
  would loop: refresh rewrites the stock records, which triggers another collect.
  """
  existed = dest.is_file()
  write_json(dest, record)
  if existed:
    return False
  nudge("rescan")
  return True


def request_panel_rescan(*_ignored: object) -> None:
  try:
    subprocess.Popen(
      ["omarchy-shell", "-q", "omarchy.agents", "refresh"],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      start_new_session=True,
    )
  except OSError:
    return


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--force", action="store_true", help="rescan sessions and re-probe billing, ignoring caches")
  parser.add_argument("--limits-only", action="store_true", help="reuse any recent session scan; only billing must be fresh")
  parser.add_argument("--cache-seconds", type=float, default=20)
  parser.add_argument("--write", action="store_true", help="write ~/.local/state/omarchy/agents/usage/grok.json")
  parser.add_argument("--clear", action="store_true", help="remove grok.json so the agents panel drops the Grok tab")
  parser.add_argument(
    "--clear-if-disabled",
    action="store_true",
    help="remove grok.json only when this plugin is no longer enabled",
  )
  args = parser.parse_args()

  if args.clear and args.clear_if_disabled:
    parser.error("--clear and --clear-if-disabled are mutually exclusive")

  if args.clear:
    clear_record()
    return 0

  if args.clear_if_disabled:
    clear_if_disabled()
    return 0

  record = build_record(args.force, args.limits_only, args.cache_seconds)
  if args.write:
    publish_record(record, usage_dir() / "grok.json", request_panel_rescan)
  else:
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
