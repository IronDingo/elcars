#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Spotify: one-time PKCE login + 'play a playlist by name'.

Stdlib only (no client secret needed — PKCE is the right flow for a local app).
The refresh token is stored in ~/elcars/.spotify.json (chmod 600); the login's
redirect is caught on 127.0.0.1:8888 and nothing listens after.

One-time link:   ~/elcars/.venv/bin/python ~/elcars/spotify.py
Then, in ELCARS: "Computer, play my <playlist> playlist".

NOTE: starting/controlling playback via the Web API requires Spotify Premium.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))   # so this runs standalone too

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("user-modify-playback-state user-read-playback-state "
          "playlist-read-private playlist-read-collaborative")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
TOKEN_FILE = Path(__file__).with_name(".spotify.json")


def _cid() -> str:
    return os.environ.get("SPOTIFY_CLIENT_ID", "")


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _save(tok: dict) -> None:
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    os.chmod(TOKEN_FILE, 0o600)


def login() -> None:
    """One-time browser login (Authorization Code + PKCE)."""
    if not _cid():
        raise SystemExit("Set SPOTIFY_CLIENT_ID in ~/elcars/.env first.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {"client_id": _cid(), "response_type": "code", "redirect_uri": REDIRECT_URI,
              "scope": SCOPES, "code_challenge_method": "S256",
              "code_challenge": challenge, "state": state}

    caught: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404); self.end_headers(); return
            q = urllib.parse.parse_qs(parsed.query)
            caught["code"] = q.get("code", [None])[0]
            caught["state"] = q.get("state", [None])[0]
            caught["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>ELCARS is linked to Spotify. You can close this tab.</h2>")

        def log_message(self, *_):   # keep the console quiet
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
    print("Opening Spotify login in your browser…")
    webbrowser.open(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")
    while not caught:
        srv.handle_request()
    if caught.get("error") or not caught.get("code") or caught.get("state") != state:
        raise SystemExit(f"Login failed: {caught.get('error') or 'no code / state mismatch'}")

    tok = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code", "code": caught["code"],
        "redirect_uri": REDIRECT_URI, "client_id": _cid(), "code_verifier": verifier})
    _save(tok)
    print("Linked! Token saved. In ELCARS: \"Computer, play my <playlist> playlist\".")


def _access_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    new = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": _cid()})
    new.setdefault("refresh_token", tok["refresh_token"])   # Spotify may not resend it
    _save(new)
    return new["access_token"]


def _api(method: str, path: str, token: str, params: dict | None = None,
         body: dict | None = None) -> tuple[dict, int]:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode()
            return (json.loads(txt) if txt else {}), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:200]}, e.code
    except urllib.error.URLError as e:
        return {"error": str(e)}, 0


def _devices(token: str) -> list[dict]:
    data, status = _api("GET", "/me/player/devices", token)
    return data.get("devices", []) if status == 200 else []


def _pick_device(devices: list[dict]) -> dict | None:
    """Prefer the already-active device, then any controllable (non-restricted) one."""
    active = next((d for d in devices if d.get("is_active")), None)
    if active and not active.get("is_restricted"):
        return active
    usable = next((d for d in devices if not d.get("is_restricted")), None)
    return usable or active or (devices[0] if devices else None)


def _launch_local_client() -> bool:
    """Start the local Spotify desktop app so it registers as a Connect device."""
    if not shutil.which("spotify"):
        return False
    try:
        if shutil.which("hyprctl"):                   # match ELCARS' float-by-default
            subprocess.Popen(["hyprctl", "dispatch", "exec", "[float] spotify"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["spotify"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _ensure_device(token: str, launch: bool = True, timeout: float = 25.0) -> dict | None:
    """Return a controllable device, launching the local client if none exists yet."""
    dev = _pick_device(_devices(token))
    if dev or not launch:
        return dev
    if not _launch_local_client():
        return None
    deadline = time.time() + timeout                  # wait for the client to register
    while time.time() < deadline:
        time.sleep(1.0)
        dev = _pick_device(_devices(token))
        if dev:
            return dev
    return None


_PLAYLISTS: list[dict] | None = None                  # per-session cache (cleared on restart)


def _all_playlists(token: str) -> list[dict]:
    """The user's whole playlist library, paged past Spotify's 50/page cap; cached."""
    global _PLAYLISTS
    if _PLAYLISTS is not None:
        return _PLAYLISTS
    out, offset = [], 0
    while True:
        data, st = _api("GET", "/me/playlists", token, params={"limit": 50, "offset": offset})
        if st != 200:
            return out                                # don't cache a failed/partial fetch
        items = data.get("items", [])
        out.extend(p for p in items if p and p.get("name"))
        offset += 50
        if offset >= data.get("total", 0) or not items:
            break
    if out:
        _PLAYLISTS = out
    return out


def _match(query: str, playlists: list[dict]) -> dict | None:
    """Substring match, best-ranked. Vague/semantic queries return None on purpose, so the
    caller hands the full catalogue to the brain to pick by meaning."""
    q = query.lower().strip()
    cands = [p for p in playlists if q in (p.get("name") or "").lower()]
    if not cands:
        return None
    def rank(p):                                      # exact name, then starts-with, then shortest
        n = (p.get("name") or "").lower()
        return (n != q, not n.startswith(q), len(n))
    return min(cands, key=rank)


def play(query: str) -> tuple[str, bool]:
    """Play one of the user's own playlists, matched by name or (via the brain) by vibe."""
    token = _access_token()
    if not token:
        return "Spotify isn't linked yet — run the one-time login first.", False
    playlists = _all_playlists(token)
    if not playlists:
        return "I couldn't reach Spotify.", False
    match = _match(query, playlists)
    if not match:
        catalog = " · ".join(p["name"] for p in playlists if p.get("name"))
        return (f"No playlist matches '{query}'. The user's playlists are: {catalog}. "
                f"Re-call spotify_play with the closest matching name, or say none fit."), False

    device = _ensure_device(token)
    if not device:
        return "I couldn't find or start a Spotify player.", False

    st = 0
    for _ in range(2):                               # a freshly-launched client may 404 once
        _, st = _api("PUT", "/me/player/play", token,
                     params={"device_id": device["id"]},
                     body={"context_uri": match["uri"]})
        if st != 404:
            break
        time.sleep(1.5)

    if st in (200, 204):
        where = "" if device.get("is_active") else f" on {device.get('name')}"
        return f"Playing {match['name']}{where}.", True
    if st == 404:
        return "I started Spotify but it isn't ready yet — try once more.", False
    if st == 403:
        return "Spotify playback control needs Premium.", False
    return "I couldn't start playback.", False


if __name__ == "__main__":
    login()
