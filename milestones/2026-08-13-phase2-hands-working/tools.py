#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Phase 2: the hands (voice-triggerable desktop actions).

A registry of tools the Brain (elcars.py) can call to *act* on the desktop, not
just talk. Everything shells out to the session's own utilities (hyprctl,
pamixer, wtype, playerctl, brightnessctl, xdg-open) — ELCARS stays a
self-contained app driving the desktop it lives in, no daemon, no system mods.

Contract:
  - Each Tool carries an Anthropic tool schema (name/description/input_schema)
    and a run(**input) -> (spoken_text, ok). ok=False → Brain shows
    'unable-to-comply' and marks the tool_result is_error.
  - confirm=True marks a hard-to-undo / outward action. Brain never runs those
    on first call — it asks the user to say "Computer, yes" first, then runs it
    on the confirming turn (see Brain._pending in elcars.py).

Descriptions are prescriptive ("Use this when the user asks to …") — Sonnet 4.6
reaches for tools conservatively, so the trigger cue earns its keep.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


def _run(cmd: list[str], *, capture: bool = False) -> tuple[str, bool]:
    """Run a command; never raise. Returns (output-or-error, ok)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except FileNotFoundError:
        return f"{cmd[0]} isn't installed.", False
    except subprocess.TimeoutExpired:
        return f"{cmd[0]} timed out.", False
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "that didn't work").strip(), False
    return (proc.stdout.strip() if capture else ""), True


# friendly aliases → candidate executables, first one present wins (dynamic,
# never hardcode the user's specific browser/terminal)
_APP_ALIASES = {
    "terminal": ["alacritty", "foot", "kitty", "ghostty"],
    "files":    ["nautilus", "thunar", "dolphin"],
    "browser":  ["firefox", "chromium", "brave"],
    "editor":   ["code", "zed", "nvim"],
}


def _resolve_app(name: str) -> str | None:
    for cand in _APP_ALIASES.get(name.lower(), [name]):
        if shutil.which(cand):
            return cand
    return None


# --- the actions ----------------------------------------------------------
def launch_app(name: str) -> tuple[str, bool]:
    exe = _resolve_app(name)
    if not exe:
        return f"I can't find {name}.", False
    _, ok = _run(["hyprctl", "dispatch", "exec", exe])
    return (f"Opening {name}.", True) if ok else (f"I couldn't open {name}.", False)


def focus_app(name: str) -> tuple[str, bool]:
    out, ok = _run(["hyprctl", "-j", "clients"], capture=True)
    if not ok:
        return "I couldn't reach the window manager.", False
    try:
        clients = json.loads(out)
    except json.JSONDecodeError:
        return "I couldn't read the window list.", False
    needle = name.lower()
    for c in clients:
        if needle in f"{c.get('class', '')} {c.get('title', '')}".lower():
            _, ok = _run(["hyprctl", "dispatch", "focuswindow", f"address:{c.get('address')}"])
            return (f"Switching to {name}.", True) if ok else ("I couldn't switch.", False)
    return f"{name} doesn't seem to be open.", False


def close_active_window() -> tuple[str, bool]:
    _, ok = _run(["hyprctl", "dispatch", "killactive"])
    return ("Closed.", True) if ok else ("I couldn't close it.", False)


def volume(action: str, percent: int | None = None) -> tuple[str, bool]:
    if action == "set":
        if percent is None:
            return "Tell me what level to set.", False
        pct = max(0, min(100, int(percent)))
        _, ok = _run(["pamixer", "--set-volume", str(pct)])
        return (f"Volume {pct} percent.", True) if ok else ("Couldn't set the volume.", False)
    verbs = {"up": ["-i", "5"], "down": ["-d", "5"], "mute": ["-t"]}
    if action not in verbs:
        return f"I don't know how to {action} the volume.", False
    _, ok = _run(["pamixer", *verbs[action]])
    said = {"up": "Louder.", "down": "Quieter.", "mute": "Toggled mute."}[action]
    return (said, True) if ok else ("Couldn't change the volume.", False)


def media_control(action: str) -> tuple[str, bool]:
    verb = {"play_pause": "play-pause", "next": "next",
            "previous": "previous", "stop": "stop"}.get(action)
    if not verb:
        return f"I can't {action} playback.", False
    _, ok = _run(["playerctl", verb])
    return ("Okay.", True) if ok else ("Nothing seems to be playing.", False)


def set_brightness(percent: int) -> tuple[str, bool]:
    pct = max(1, min(100, int(percent)))
    _, ok = _run(["brightnessctl", "set", f"{pct}%"])
    return (f"Brightness {pct} percent.", True) if ok else ("Couldn't set brightness.", False)


def type_text(text: str) -> tuple[str, bool]:
    if not text:
        return "There's nothing to type.", False
    _, ok = _run(["wtype", "--", text])
    return ("Typed.", True) if ok else ("I couldn't type that.", False)


def open_url(url: str) -> tuple[str, bool]:
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    _, ok = _run(["xdg-open", u])
    return (f"Opening {u}.", True) if ok else ("I couldn't open that link.", False)


def get_context() -> tuple[str, bool]:
    """The brain has no clock/sensors of its own — this answers 'what time is it'."""
    now = datetime.now().strftime("%A %-d %B, %H:%M")
    vol, ok = _run(["pamixer", "--get-volume"], capture=True)
    return f"Time is {now}. Volume {vol + ' percent' if ok else 'unknown'}.", True


# --- registry -------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., tuple[str, bool]]
    confirm: bool = False


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS: list[Tool] = [
    Tool("launch_app",
         "Open or launch a desktop application. Use when the user asks to open, "
         "launch or start an app ('open Firefox', 'launch the terminal').",
         {"type": "object",
          "properties": {"name": _str("App name, e.g. 'firefox', 'terminal', 'files'.")},
          "required": ["name"]},
         launch_app),
    Tool("focus_app",
         "Switch focus to an already-open application by name. Use for 'switch to "
         "…', 'go to my browser', 'bring up …'.",
         {"type": "object",
          "properties": {"name": _str("App or window name to focus.")},
          "required": ["name"]},
         focus_app),
    Tool("close_active_window",
         "Close the currently focused window. Use for 'close this', 'quit this window'.",
         {"type": "object", "properties": {}},
         close_active_window, confirm=True),
    Tool("volume",
         "Control system audio volume. Use for 'volume up/down', 'set volume to 30', "
         "'mute'.",
         {"type": "object",
          "properties": {
              "action": {"type": "string", "enum": ["set", "up", "down", "mute"]},
              "percent": {"type": "integer",
                          "description": "0-100, required only when action is 'set'."}},
          "required": ["action"]},
         volume),
    Tool("media_control",
         "Control media playback (any player). Use for 'pause', 'play', 'next track', "
         "'skip', 'previous'.",
         {"type": "object",
          "properties": {"action": {"type": "string",
                         "enum": ["play_pause", "next", "previous", "stop"]}},
          "required": ["action"]},
         media_control),
    Tool("set_brightness",
         "Set screen brightness. Use for 'brightness to 50', 'dim/brighten the screen'.",
         {"type": "object",
          "properties": {"percent": {"type": "integer", "description": "1-100."}},
          "required": ["percent"]},
         set_brightness),
    Tool("type_text",
         "Type text into whatever field is focused (voice dictation). Use when the "
         "user asks you to type, write, or enter text somewhere.",
         {"type": "object",
          "properties": {"text": _str("The exact text to type.")},
          "required": ["text"]},
         type_text),
    Tool("open_url",
         "Open a web address in the default browser. Use for 'open youtube.com', "
         "'go to <site>'.",
         {"type": "object",
          "properties": {"url": _str("The URL or domain to open.")},
          "required": ["url"]},
         open_url),
    Tool("get_context",
         "Get the current time and volume. Use when the user asks the time or the "
         "current volume level — you have no clock of your own.",
         {"type": "object", "properties": {}},
         get_context),
]

REGISTRY = {t.name: t for t in TOOLS}


def anthropic_tools() -> list[dict]:
    return [{"name": t.name, "description": t.description,
             "input_schema": t.input_schema} for t in TOOLS]


def execute(name: str, args: dict) -> tuple[str, bool]:
    tool = REGISTRY.get(name)
    if tool is None:
        return f"I don't have a way to {name}.", False
    try:
        return tool.run(**(args or {}))
    except Exception as e:            # never let a tool crash the voice loop
        return f"{name} failed: {e}", False
