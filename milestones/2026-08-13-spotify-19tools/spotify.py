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


def play(query: str) -> tuple[str, bool]:
    """Find a saved/followed playlist matching `query`, start it on the active device."""
    token = _access_token()
    if not token:
        return "Spotify isn't linked yet — run the one-time login first.", False
    data, status = _api("GET", "/me/playlists", token, params={"limit": 50})
    if status == 401:
        return "Spotify needs re-linking.", False
    if status != 200:
        return "I couldn't reach Spotify.", False
    playlists = data.get("items", [])
    q = query.lower().strip()
    match = next((p for p in playlists if q in (p.get("name") or "").lower()), None)
    if not match:                                    # fall back to any word overlap
        words = [w for w in q.split() if len(w) > 2]
        match = next((p for p in playlists
                      if any(w in (p.get("name") or "").lower() for w in words)), None)
    if not match:
        return f"I couldn't find a playlist called {query}.", False
    _, st = _api("PUT", "/me/player/play", token, body={"context_uri": match["uri"]})
    if st in (200, 204):
        return f"Playing {match['name']}.", True
    if st == 404:
        return "Open Spotify first — I don't see an active player.", False
    if st == 403:
        return "Spotify playback control needs Premium.", False
    return "I couldn't start playback.", False


if __name__ == "__main__":
    login()
