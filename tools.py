#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Phase 2: the hands (voice-triggerable desktop actions).

A registry of tools the Brain (elcars.py) can call to *act* on the desktop, not
just talk. Everything shells out to the session's own utilities (hyprctl,
pamixer, wtype, playerctl, brightnessctl, xdg-open, grim, loginctl) — ELCARS
stays a self-contained app driving the desktop it lives in, no daemon, no
system mods.

Contract:
  - Each Tool carries an Anthropic tool schema (name/description/input_schema)
    and a run(**input) -> (spoken_text, ok). ok=False → Brain shows
    'unable-to-comply' and marks the tool_result is_error.
  - confirm=True marks a hard-to-undo / outward action. Brain never runs those
    on first call — it asks the user to say "Computer, yes" first.
  - compose=True marks an INFORMATIONAL tool whose output is data for the Brain
    to speak *about*, not a ready-made spoken line. It is exempt from the
    first-hop short-circuit, so the model always gets a turn to reason over the
    data instead of reading a raw list aloud.
  - The two Open Channel tools are in MODE_TOOLS; the Brain flips its
    channel_open flag on them. deep_think is handled in the Brain too — it
    escalates the turn rather than doing anything itself.

Descriptions are prescriptive ("Use this when the user asks to …") — the model
reaches for tools conservatively, so the trigger cue earns its keep.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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

# spoken category → window-class fragments, so "the terminal" matches a kitty/
# Alacritty window (whose class isn't literally "terminal")
_CLASS_ALIASES = {
    "terminal": ["kitty", "alacritty", "foot", "ghostty", "wezterm", "konsole", "gnome-terminal"],
    "browser":  ["firefox", "chromium", "brave", "chrome", "librewolf", "zen", "vivaldi"],
    "files":    ["nautilus", "thunar", "dolphin", "nemo"],
    "editor":   ["code", "zed", "nvim", "neovim", "sublime"],
}


def _resolve_app(name: str) -> str | None:
    for cand in _APP_ALIASES.get(name.lower(), [name]):
        if shutil.which(cand):
            return cand
    return None


# --- window helpers (Hyprland knows every window by class + title) ---------
def _clients() -> tuple[list | None, str | None]:
    out, ok = _run(["hyprctl", "-j", "clients"], capture=True)
    if not ok:
        return None, "I couldn't reach the window manager."
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "I couldn't read the window list."


def _active_address() -> str:
    out, ok = _run(["hyprctl", "-j", "activewindow"], capture=True)
    if ok:
        try:
            return json.loads(out).get("address", "")
        except json.JSONDecodeError:
            pass
    return ""


def _active_workspace() -> str:
    out, ok = _run(["hyprctl", "-j", "activeworkspace"], capture=True)
    if ok:
        try:
            return str(json.loads(out).get("id", ""))
        except json.JSONDecodeError:
            pass
    return ""


_POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right",
              "top", "bottom", "left", "right"]


def _split_position(query: str) -> tuple[str | None, str]:
    """Pull a leading positional qualifier + articles off a window query.
    'the top-left terminal' -> ('top-left', 'terminal'); 'terminal' -> (None, 'terminal')."""
    q = query.lower().strip()
    for art in ("the ", "my ", "an ", "a "):
        if q.startswith(art):
            q = q[len(art):]
    q = (q.replace("top left", "top-left").replace("top right", "top-right")
          .replace("bottom left", "bottom-left").replace("bottom right", "bottom-right"))
    for p in _POSITIONS:                              # compound positions listed first
        if q == p:
            return p, ""
        if q.startswith(p + " "):
            return p, q[len(p):].strip()
    return None, q


def _matches(client: dict, needle: str) -> bool:
    if not needle:
        return True
    cls = (client.get("class") or "").lower()
    if needle in f"{cls} {(client.get('title') or '').lower()}":
        return True
    aliases = _CLASS_ALIASES.get(needle)
    return bool(aliases and any(k in cls for k in aliases))


def _pick_position(pool: list, pos: str) -> dict:
    def x(c): return (c.get("at") or [0, 0])[0]
    def y(c): return (c.get("at") or [0, 0])[1]
    return {
        "left":   lambda: min(pool, key=x),
        "right":  lambda: max(pool, key=x),
        "top":    lambda: min(pool, key=y),
        "bottom": lambda: max(pool, key=y),
        "top-left":     lambda: min(pool, key=lambda c: (y(c), x(c))),
        "top-right":    lambda: min(pool, key=lambda c: (y(c), -x(c))),
        "bottom-left":  lambda: max(pool, key=lambda c: (y(c), -x(c))),
        "bottom-right": lambda: max(pool, key=lambda c: (y(c), x(c))),
    }.get(pos, lambda: pool[0])()


def _find_window(clients: list, query: str) -> dict | None:
    """Find a window: honour a positional qualifier, prefer the active workspace,
    default to the top-left-most match (the user can then say 'the other one')."""
    pos, needle = _split_position(query)
    matches = [c for c in clients if _matches(c, needle)]
    if not matches:
        return None
    aws = _active_workspace()
    pool = [c for c in matches if str((c.get("workspace") or {}).get("id")) == aws] or matches
    if pos and len(pool) > 1:
        return _pick_position(pool, pos)
    return min(pool, key=lambda c: ((c.get("at") or [0, 0])[1], (c.get("at") or [0, 0])[0]))


# --- the actions ----------------------------------------------------------
def launch_app(name: str) -> tuple[str, bool]:
    exe = _resolve_app(name)
    if not exe:
        return f"I can't find {name}.", False
    _, ok = _run(["hyprctl", "dispatch", "exec", f"[float] {exe}"])   # float by default (like Super+T)
    return (f"Opening {name}.", True) if ok else (f"I couldn't open {name}.", False)


def focus_app(name: str) -> tuple[str, bool]:
    clients, err = _clients()
    if err:
        return err, False
    match = _find_window(clients, name)
    if not match:
        return f"{name} doesn't seem to be open.", False
    _, ok = _run(["hyprctl", "dispatch", "focuswindow", f"address:{match.get('address')}"])
    return (f"Switching to {name}.", True) if ok else ("I couldn't switch.", False)


def list_windows() -> tuple[str, bool]:
    clients, err = _clients()
    if err:
        return err, False
    if not clients:
        return "No windows are open right now.", True
    active = _active_address()
    parts = []
    for c in clients:
        cls = (c.get("class") or "").strip()
        title = (c.get("title") or "").strip()
        ws = (c.get("workspace") or {}).get("name", "?")
        label = cls or "window"
        if title and title.lower() != cls.lower():
            label = f"{cls} — {title}" if cls else title
        if len(label) > 60:
            label = label[:60] + "…"
        focused = " (focused)" if active and c.get("address") == active else ""
        parts.append(f"{label} [workspace {ws}]{focused}")
    return "Open windows: " + "; ".join(parts) + ".", True


def type_in_window(window: str, text: str) -> tuple[str, bool]:
    if not text:
        return "There's nothing to type.", False
    clients, err = _clients()
    if err:
        return err, False
    match = _find_window(clients, window)
    if not match:
        return f"I don't see a {window} window open.", False
    _run(["hyprctl", "dispatch", "focuswindow", f"address:{match.get('address')}"])
    time.sleep(0.15)                                 # let focus settle before typing
    _, ok = _run(["wtype", "--", text])
    return (f"Typed into {window}.", True) if ok else ("I focused it but couldn't type.", False)


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


# --- window & session control ---------------------------------------------
def fullscreen() -> tuple[str, bool]:
    _, ok = _run(["hyprctl", "dispatch", "fullscreen"])          # toggles active window
    return ("Fullscreen.", True) if ok else ("I couldn't do that.", False)


def switch_workspace(target: str) -> tuple[str, bool]:
    arg = {"next": "e+1", "previous": "e-1", "prev": "e-1"}.get(str(target).lower(), str(target))
    _, ok = _run(["hyprctl", "dispatch", "workspace", arg])
    return (f"Workspace {target}.", True) if ok else ("I couldn't switch workspace.", False)


def screenshot() -> tuple[str, bool]:
    dest = Path(os.environ.get("XDG_PICTURES_DIR") or (Path.home() / "Pictures"))
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    _, ok = _run(["grim", str(path)])
    return (f"Screenshot saved to {dest.name}.", True) if ok else ("I couldn't take a screenshot.", False)


def lock_screen() -> tuple[str, bool]:
    _, ok = _run(["loginctl", "lock-session"])
    return ("Locking the screen.", True) if ok else ("I couldn't lock the screen.", False)


def window_layout(mode: str) -> tuple[str, bool]:
    """Float or tile the focused window (Super+T = togglefloating)."""
    out, ok = _run(["hyprctl", "-j", "activewindow"], capture=True)
    floating = False
    if ok:
        try:
            floating = bool(json.loads(out).get("floating"))
        except json.JSONDecodeError:
            pass
    m = mode.lower().strip()
    if m in ("toggle", "switch"):
        want = not floating
    elif m in ("float", "floating"):
        want = True
    elif m in ("tile", "tiled", "tiling", "untile", "dock"):
        want = False
    else:
        return f"I can't set the layout to {mode}.", False
    if want == floating:
        return f"It's already {'floating' if want else 'tiled'}.", True
    _, ok = _run(["hyprctl", "dispatch", "togglefloating", "active"])
    return (f"{'Floating' if want else 'Tiled'}.", True) if ok else ("I couldn't change the layout.", False)


def spotify_play(query: str) -> tuple[str, bool]:
    import spotify                                    # lazy — only imported when used
    return spotify.play(query)


def open_file(query: str = "", category: str = "any") -> tuple[str, bool]:
    import files                                      # lazy — only imported when used
    return files.open_file(query, category)


def find_places(query: str = "", area: str = "") -> tuple[str, bool]:
    import places                                     # lazy — only imported when used
    return places.find(query, area)


# --- Open Channel: conversation mode (the wake-lock lifts) -----------------
# Handled specially by the Brain (they flip its channel_open flag); the
# functions just supply the spoken confirmation.
def open_channel() -> tuple[str, bool]:
    return "Channel open. I'm listening — go ahead.", True


def close_channel() -> tuple[str, bool]:
    return "Channel closed.", True


MODE_TOOLS = {"open_channel", "close_channel"}


# --- deep_think: escalate the turn to the deep model ----------------------
# Intercepted by the Brain, which discards the shallow turn unspoken and re-runs
# it on DEEP_MODEL. The body never executes in normal operation; it exists so
# the registry stays complete and a stray call degrades to a shrug, not a crash.
def deep_think() -> tuple[str, bool]:
    return "Thinking about that properly.", True


DEEP_TOOL = "deep_think"


# --- registry -------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., tuple[str, bool]]
    confirm: bool = False
    compose: bool = False       # output is DATA for the brain to speak about, not a spoken line


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


TOOLS: list[Tool] = [
    Tool("launch_app",
         "Open or launch a desktop APPLICATION by name. Use when the user asks to open, "
         "launch or start a program ('open Firefox', 'launch the terminal', 'open the "
         "file manager'). NOT for the user's own movies, music, documents or photos — "
         "those are files, handled elsewhere; don't launch an app for them.",
         {"type": "object",
          "properties": {"name": _str("App name, e.g. 'firefox', 'terminal', 'files'.")},
          "required": ["name"]},
         launch_app),
    Tool("focus_app",
         "Switch focus to an already-open application or window by name. Use for "
         "'switch to …', 'go to my browser', 'bring up …'. Prefers the current "
         "workspace; accepts positions like 'top-left terminal'.",
         {"type": "object",
          "properties": {"name": _str("App/window name, optionally with a position, e.g. 'terminal', 'top-right kitty'.")},
          "required": ["name"]},
         focus_app),
    Tool("list_windows",
         "List the currently open windows — app name, title, workspace, and which is "
         "focused. Use when the user asks what's open / what windows or apps they have, "
         "or to find the right window before typing into one. You CAN see the open "
         "windows this way — you are not blind to the desktop (you just can't read the "
         "pixel content of a window).",
         {"type": "object", "properties": {}},
         list_windows),
    Tool("type_in_window",
         "Type text into a specific open window by name — focuses it first, then "
         "types. Use for 'write X in the terminal', 'type Y in the Claude Code window'. "
         "The window name may include a position ('top-left terminal'); it prefers the "
         "current workspace. If unsure which window, call list_windows first.",
         {"type": "object",
          "properties": {
              "window": _str("Which window — app/title fragment, optionally a position, e.g. 'terminal', 'claude', 'top-left kitty'."),
              "text": _str("The exact text to type.")},
          "required": ["window", "text"]},
         type_in_window),
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
         "Type text into whatever window is currently focused (voice dictation). Use "
         "when the user asks you to type/write/enter text with no particular window in "
         "mind — for a named window use type_in_window instead.",
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
    Tool("fullscreen",
         "Toggle fullscreen on the focused window. Use for 'fullscreen this', "
         "'make it fullscreen', 'exit fullscreen'.",
         {"type": "object", "properties": {}},
         fullscreen),
    Tool("switch_workspace",
         "Switch to another workspace / virtual desktop. Use for 'go to workspace 3', "
         "'next workspace', 'previous desktop'.",
         {"type": "object",
          "properties": {"target": _str("Workspace number, or 'next' / 'previous'.")},
          "required": ["target"]},
         switch_workspace),
    Tool("screenshot",
         "Take a screenshot of the whole screen (saved to the Pictures folder). Use "
         "for 'take a screenshot', 'grab the screen', 'capture this'.",
         {"type": "object", "properties": {}},
         screenshot),
    Tool("lock_screen",
         "Lock the screen. Use for 'lock the screen', 'lock it', 'secure the computer'.",
         {"type": "object", "properties": {}},
         lock_screen),
    Tool("window_layout",
         "Float or tile the focused window (like Super+T). Use for 'make it float', "
         "'float this', 'return it to tile', 'tile this', 'toggle floating'.",
         {"type": "object",
          "properties": {"mode": {"type": "string", "enum": ["float", "tile", "toggle"]}},
          "required": ["mode"]},
         window_layout),
    Tool("spotify_play",
         "Play one of the USER'S OWN Spotify playlists — by name or by vibe. Use for 'play "
         "my <name> playlist', 'put on <name>', 'play something calming', 'play 432hz', "
         "'play rain sounds'. If nothing matches it returns the user's playlist list — then "
         "re-call with the closest one. Launches Spotify if needed (needs Premium).",
         {"type": "object",
          "properties": {"query": _str("Playlist name to search for, e.g. 'Discover Weekly', 'chill'.")},
          "required": ["query"]},
         spotify_play),
    Tool("open_file",
         "Find and open one of the user's OWN files — play a movie or show, put on some "
         "music, open a PDF, document, or image — searched by name or vibe across their "
         "Downloads, Videos, Music, Documents, Pictures and Desktop. Use for 'play a "
         "movie', 'play the film I downloaded', 'put on <title>', 'open my resume', 'open "
         "that PDF'. Give a category when the kind is clear. Leave the query empty when "
         "they didn't name a title (e.g. just 'play a movie'). If nothing clearly matches "
         "it returns a short newest-first list of filenames — pick the best and re-call "
         "with that name, or ask which one they mean. Video and audio open in mpv; the "
         "rest in their default app.",
         {"type": "object",
          "properties": {
              "query": _str("Title, keyword or vibe, e.g. 'the matrix', 'something scary', "
                            "'my resume'. Leave empty if they didn't name one."),
              "category": {"type": "string",
                           "enum": ["video", "audio", "document", "image", "any"],
                           "description": "Filter by kind when known; defaults to any."}}},
         open_file),
    Tool("find_places",
         "Find real local places — restaurants, cafés, bars, dessert spots — for planning: "
         "'where's good pasta in Dokki', 'find 5 nice restaurants in Zamalek', 'somewhere for "
         "coffee', 'plan an evening out'. Returns a grounded list of places that genuinely "
         "exist (OpenStreetMap) for YOU to rank and shape into an answer or itinerary using "
         "your own knowledge of which are well-regarded. Falls back to the user's "
         "configured home area when none is named. It has NO star ratings and NO reliable "
         "'open now' — don't claim either, and never invent a place or a rating.",
         {"type": "object",
          "properties": {
              "query": _str("Kind of place / cuisine / vibe, e.g. 'pasta', 'sushi', "
                            "'coffee', 'romantic dinner'. Leave empty for any."),
              "area": _str("Neighbourhood or city, e.g. 'Zamalek', 'Kreuzberg, Berlin'. "
                           "Leave empty for the user's home area.")}},
         find_places, compose=True),
    Tool("open_channel",
         "Enter Open Channel mode — stay listening so the user can talk without "
         "saying 'Computer' each time. Use when they ask to open a channel, start a "
         "conversation, chat, or 'keep listening'.",
         {"type": "object", "properties": {}},
         open_channel),
    Tool("close_channel",
         "Leave Open Channel mode and go back to requiring 'Computer'. Use when they "
         "ask to close the channel, stop listening, end the conversation, or say "
         "'that's all'.",
         {"type": "object", "properties": {}},
         close_channel),
    Tool("deep_think",
         "Hand THIS question to a slower, more capable model. Call it alone, as your "
         "first and only action, when the request genuinely needs careful thought: "
         "multi-step reasoning, real analysis, composing something substantial, a hard "
         "technical or philosophical question, or the user explicitly asking you to "
         "think hard or take your time. Do NOT call it for desktop commands, small "
         "talk, or anything you can already answer well — it costs the user several "
         "seconds of silence, so it has to earn them.",
         {"type": "object", "properties": {}},
         deep_think),
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
