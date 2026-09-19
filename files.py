#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Files: find and open the user's own files by name or vibe.

"The eyes on disk." A lean, stdlib-only finder over the user's own folders
(Downloads, Videos, Music, Documents, Pictures, Desktop). Token-frugal by
construction — like spotify.py, a clean name match opens straight away; a vague
query MISSES on purpose and hands a short, newest-first candidate list back to
the brain, which picks by meaning and re-calls the exact filename. The whole
tree is never dumped into the model.

Video and audio open in mpv (one lean binary — the FOSS-native player);
everything else opens in its default app via xdg-open.

No cache — the tree is walked fresh each call so a just-downloaded film shows up
at once. The walk is bounded (fixed roots, depth cap, hidden/junk dirs pruned,
a hard file cap) so it stays quick.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# category -> file extensions (lowercase, no leading dot)
_EXTS = {
    "video": {"mkv", "mp4", "avi", "mov", "webm", "m4v", "mpg", "mpeg", "wmv", "flv"},
    "audio": {"mp3", "flac", "wav", "m4a", "ogg", "opus", "aac"},
    "document": {"pdf", "epub", "mobi", "azw3", "djvu", "cbz", "cbr",
                 "doc", "docx", "odt", "rtf", "txt", "md"},
    "image": {"jpg", "jpeg", "png", "gif", "webp", "tiff", "bmp", "svg", "heic"},
}
_ALL_EXTS = set().union(*_EXTS.values())
_MEDIA_EXTS = _EXTS["video"] | _EXTS["audio"]

# directory names never worth walking into (hidden dirs are pruned separately)
_SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "trash",
              "$RECYCLE.BIN", "System Volume Information"}

_MAX_DEPTH = 4        # roots are shallow; a film sits at most a folder or two deep
_MAX_FILES = 6000     # hard bound on how many candidates we'll ever consider
_LIST_CAP = 50        # names handed back to the brain on a miss (newest first)

# tokens that mark where a release name stops being the human title
_NOISE = re.compile(
    r"\b(19|20)\d{2}\b|\b(480p|540p|576p|720p|1080p|1440p|2160p|4k|8k|"
    r"web ?dl|webrip|web|bluray|bdrip|brrip|dvdrip|hdtv|hdrip|remux|"
    r"x264|x265|h ?264|h ?265|hevc|avc|xvid|divx|10bit|"
    r"aac\d?|ac3|dts|ddp?5|dd5|hdr|sdr|proper|repack)\b",
    re.IGNORECASE)


def _roots() -> list[Path]:
    """The user's own content folders (honours XDG overrides), existing ones only."""
    home = Path.home()
    wanted = {
        "Downloads": "XDG_DOWNLOAD_DIR", "Videos": "XDG_VIDEOS_DIR",
        "Music": "XDG_MUSIC_DIR", "Documents": "XDG_DOCUMENTS_DIR",
        "Pictures": "XDG_PICTURES_DIR", "Desktop": "XDG_DESKTOP_DIR",
    }
    roots: list[Path] = []
    for default, env in wanted.items():
        p = Path(os.environ.get(env) or (home / default))
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


def _norm(s: str) -> str:
    """Fold a filename or query for matching: lowercase, separators -> spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[._\-/\[\]()]+", " ", s.lower())).strip()


def _pretty(name: str) -> str:
    """A clean, speakable title from a release-style filename (for TTS only)."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = re.sub(r"\[.*?\]", " ", stem)                 # drop [EZTVx.to], [YTS...]
    words = re.sub(r"[._\-]+", " ", stem)
    m = _NOISE.search(words)                             # cut at the first release tag
    if m and m.start() > 0:
        words = words[:m.start()]
    return re.sub(r"\s+", " ", words).strip() or name


def _exts_for(category: str) -> set[str] | None:
    cat = (category or "any").lower()
    if cat in ("any", "all", "file", "files", ""):
        return None                                      # any openable file
    return _EXTS.get(cat)


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _scan(category: str) -> list[Path]:
    """All files of the category across the roots, newest first, bounded."""
    allow = _exts_for(category) or _ALL_EXTS
    found: list[tuple[float, Path]] = []
    for root in _roots():
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if len(Path(dirpath).parts) - base >= _MAX_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in filenames:
                if name.startswith("."):
                    continue
                e = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if e not in allow:
                    continue
                p = Path(dirpath) / name
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    continue
            if len(found) >= _MAX_FILES:
                break
        if len(found) >= _MAX_FILES:
            break
    found.sort(key=lambda mp: mp[0], reverse=True)       # newest first
    return [p for _, p in found]


def _match(query: str, files: list[Path]) -> Path | None:
    """Best substring match on the normalised name; None if nothing contains the
    query — the caller then hands the list to the brain to choose by meaning.
    `files` is newest-first and min() is stable, so ties resolve to the newest."""
    q = _norm(query)
    if not q:
        return None
    cands = [p for p in files if q in _norm(p.name)]
    if not cands:
        return None
    def rank(p: Path):
        n = _norm(p.name)
        return (n != q, not n.startswith(q))             # exact, then prefix
    return min(cands, key=rank)


def _launch(path: Path) -> bool:
    e = _ext(path)
    if e in _MEDIA_EXTS and shutil.which("mpv"):
        cmd = ["mpv", "--", str(path)]
        if e in _EXTS["video"]:
            cmd.insert(1, "--fullscreen")
    else:
        cmd = ["xdg-open", str(path)]
    try:
        subprocess.Popen(cmd, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def open_file(query: str = "", category: str = "any") -> tuple[str, bool]:
    """Find and open a file by name or vibe. Mirrors spotify.play's contract:
    a clean hit opens; a vague / no-match query hands the candidate list back to
    the brain (ok=False) so it can pick by meaning and re-call the exact name."""
    files = _scan(category)
    kind = (category or "any").lower()
    label = kind if kind in _EXTS else "openable"
    if not files:
        return f"I couldn't find any {label} files in your folders.", True

    q = (query or "").strip()
    if not q and len(files) == 1:
        match: Path | None = files[0]
    elif not q:
        match = None
    else:
        match = _match(q, files)

    if match is None:
        names = " · ".join(p.name for p in files[:_LIST_CAP])
        extra = "" if len(files) <= _LIST_CAP else f" (and {len(files) - _LIST_CAP} more)"
        lead = (f"Nothing clearly matches '{query}'." if q
                else f"There are {len(files)} {label} files.")
        return (f"{lead} Newest first: {names}{extra}. Re-call open_file with the "
                f"closest filename, or ask the user which one they mean.", False)

    if not _launch(match):
        return f"I found {_pretty(match.name)} but couldn't open it.", False
    verb = "Playing" if _ext(match) in _MEDIA_EXTS else "Opening"
    return f"{verb} {_pretty(match.name)}.", True
