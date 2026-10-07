"""The operator's `claude` CLI as a contained, publisher-side drafting model (ruling D2, 2026-10-06).

This mirrors SmartBrain_3000's claudecli containment, trimmed to one stateless completion:
- an empty-toolset agent on argv — the mechanism that actually blocks Bash, files and the web;
- `--setting-sources ""` (no CLAUDE.md or settings reach the model), `--strict-mcp-config`,
  `--no-session-persistence`;
- argv carries nothing content-derived (argv is world-readable); instructions and data go over stdin, and a
  data line that imitates the stdin headings is quoted so it cannot pose as instructions;
- a private 0700 working directory removed afterwards; an environment without ANTHROPIC_* keys (billing stays
  on the operator's own login) and with the CLI's telemetry switched off; the whole process group killed at
  the deadline.
Used by `answers-draft` only. The user's machine never runs this; every draft records `is_local: false`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

MODELS = ("sonnet", "opus", "haiku")  # aliases: stable across CLI releases, unlike dated ids
DEFAULT_MODEL = "sonnet"
TIMEOUT_S = 300.0
MAX_OUTPUT_LINES = 40000
_MIN_MAJOR = 2
_AGENT = "library-drafter"
_AGENT_PROMPT = ("You draft structured data for the SmartBrain Library. The user message starts with "
                 "'## System instructions' — follow them exactly; '## Data' holds the material to work from.")
_BIN_CANDIDATES = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


class CliError(Exception):
    """The CLI is unavailable, timed out, or returned an error result."""


def binary_path() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for c in _BIN_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def available() -> tuple[bool, str]:
    """(ok, why): a CLI of major version >= 2 that is signed in."""
    path = binary_path()
    if not path:
        return False, "claude CLI not installed"
    try:
        ver = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
        major = int(re.match(r"(\d+)\.", ver).group(1))  # type: ignore[union-attr]
        if major < _MIN_MAJOR:
            return False, f"claude CLI {ver} is too old (need {_MIN_MAJOR}.x)"
        auth = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=15).stdout
        if not json.loads(auth or "{}").get("loggedIn"):
            return False, "claude CLI is not signed in"
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired) as e:
        return False, f"claude CLI probe failed: {type(e).__name__}"
    return True, ver


def neutralize(text: str) -> str:
    """Quote lines that look like the stdin headings so data can never pose as instructions."""
    return "\n".join(("> " + ln) if _HEADING.match(ln) else ln for ln in text.splitlines())


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SMARTBRAIN_") and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def command(path: str, model: str) -> list[str]:
    assert model in MODELS, f"model must be one of {MODELS}"
    agents = json.dumps({_AGENT: {"description": "SmartBrain Library answers drafter", "prompt": _AGENT_PROMPT, "tools": []}})
    return [path, "-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--model", model, "--agents", agents, "--agent", _AGENT]


def parse_stream(lines: list[str]) -> tuple[str, dict]:
    """(reply text, meta) from the CLI's NDJSON events; raises CliError on an error result or no result."""
    parts: list[str] = []
    meta: dict = {}
    noise: list[str] = []
    done = False
    for ln in lines[:MAX_OUTPUT_LINES]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except ValueError:
            noise.append(ln[:200])
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            meta["model"] = ev.get("model")
        elif kind == "stream_event":
            inner = ev.get("event") or {}
            delta = inner.get("delta") or {}
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                parts.append(str(delta.get("text") or ""))
        elif kind == "result":
            done = True
            if ev.get("is_error"):
                raise CliError(f"claude CLI error: {str(ev.get('result') or '')[:300]}")
            meta["usage"] = ev.get("usage")
            meta["cost_usd"] = ev.get("total_cost_usd")
            if not parts and isinstance(ev.get("result"), str):
                parts.append(ev["result"])
    if not done:
        raise CliError("claude CLI ended without a result: " + " | ".join(noise[-5:]))
    return "".join(parts), meta


def complete(instructions: str, data: str, *, model: str = DEFAULT_MODEL, timeout: float = TIMEOUT_S) -> tuple[str, dict]:
    """One contained completion. Returns (reply text, meta{model, usage, cost_usd})."""
    assert isinstance(instructions, str) and isinstance(data, str), "args required"
    path = binary_path()
    if not path:
        raise CliError("claude CLI not installed")
    stdin_text = "## System instructions\n" + instructions.strip() + "\n\n## Data\n" + neutralize(data) + "\n"
    cwd = tempfile.mkdtemp(prefix="sb-library-draft-")
    os.chmod(cwd, 0o700)
    try:
        proc = subprocess.Popen(command(path, model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=cwd, env=_env(), text=True, start_new_session=True)
        killed = threading.Event()

        def _kill() -> None:
            killed.set()
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            out, _ = proc.communicate(stdin_text)
        finally:
            timer.cancel()
        if killed.is_set():
            raise CliError(f"claude CLI timed out after {int(timeout)} s")
        text, meta = parse_stream(out.splitlines())
        meta.setdefault("model", model)
        return text, meta
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
