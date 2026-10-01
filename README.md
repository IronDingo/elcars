# ELCARS

**E**nhanced **L**ibrary **C**omputer **A**ccess and **R**etrieval **S**ystem — a voice-first
desktop assistant for Hyprland. You say *"Computer, open the terminal"* and the terminal opens.

It was built for one user who cannot comfortably use a keyboard. That is the whole design
brief, and it explains nearly everything below: why there are no keybindings, why every
reply is spoken rather than printed, why the interface is a single dot in the corner of the
screen, and why a tool that can't be undone by saying the opposite of what you just said
has to ask permission first.

```
you:    Computer, what windows do I have open?
elcars: Alacritty — elcars, Firefox — OpenStreetMap, and Spotify. Firefox is focused.

you:    Computer, write git status in the elcars terminal
elcars: Typed into the elcars terminal.

you:    Computer, play something relaxing
elcars: Playing Calm morning.

you:    Computer, close this window
elcars: That will close the focused Firefox window. Say "Computer, yes" to confirm.
you:    Computer, yes
elcars: Closed.
```

Roughly four seconds from the moment you stop speaking to the moment it starts. The
section on [latency](#latency) explains where every one of those seconds goes, because
getting there took measuring rather than guessing.

---

## Table of contents

- [What it is, and what it isn't](#what-it-is-and-what-it-isnt)
- [Requirements](#requirements) — **read this first**
- [Install](#install)
- [Running it](#running-it)
- [The dot](#the-dot)
- [What you can say](#what-you-can-say)
- [The tools](#the-tools)
- [How it works](#how-it-works)
- [Latency](#latency)
- [What it cannot do](#what-it-cannot-do)
- [Known limitations](#known-limitations)
- [Privacy](#privacy)
- [Credits and licence](#credits-and-licence)

---

## What it is, and what it isn't

**It is an application.** It does not patch your compositor, install a daemon, drop files
into `/etc`, or ask to be a systemd unit. It runs as one Python process that drives the
desktop it happens to be living in, through the same command-line tools you would use by
hand. Kill it and the machine is exactly as it was.

**It is a complete loop, not a demo.** Ears, voice, a face, and twenty-two hands that
actually reach the desktop: windows, workspaces, volume, brightness, media, dictation,
screenshots, the screen lock, your own film and music library, your Spotify playlists, and
real restaurants on a real map.

**It is not portable, and does not aspire to be.** It speaks fluent Hyprland and nothing
else. See [Requirements](#requirements), which are unusually load-bearing.

**It is not a smart speaker.** Nothing is always-on in the cloud; nothing is recorded to
disk; there is no account, no companion app, and no wake-word model phoning home. What
leaves the machine is one transcribed sentence per command, and only when you have said
"Computer" first. See [Privacy](#privacy).

---

## Requirements

> **This runs on Hyprland, on Wayland, with PipeWire. It will not work on GNOME, KDE, X11,
> macOS, Windows, or WSL, and the failure will not be graceful.**

Not a soft preference. The window tools shell out to `hyprctl` and parse its JSON; the HUD
is a `wlr-layer-shell` overlay; typing goes through `wtype`, which is Wayland-only. On
anything else ELCARS will start, calibrate its microphone, cheerfully transcribe you, and
then fail at every single thing you ask it to do.

| | |
|---|---|
| **Compositor** | Hyprland (developed against 0.56) |
| **Audio** | PipeWire, with a working input device |
| **Python** | 3.14 (3.11+ should be fine) |
| **Brain** | An Anthropic API key, with credit on it |
| **Hardware** | A CPU. Whisper `base.en` at int8 runs comfortably without a GPU |

**System packages.** These are not pip-installable — they talk to hardware and to the
compositor. Arch names; translate as needed.

```
gtk4  gtk4-layer-shell  python-gobject      # the HUD overlay
portaudio                                   # what sounddevice talks to
pamixer  brightnessctl  grim  wtype         # volume, brightness, screenshots, typing
playerctl                                   # media transport (pause / next / previous)
xdg-utils                                   # opening files and links in your own apps
mpv  mpv-mpris                              # plays your films and music
```

`hyprctl` and `loginctl` you already have. Anything in that list which is missing degrades
honestly rather than silently: the tool returns *"playerctl isn't installed"*, the dot turns
salmon, and ELCARS says so out loud. It will not pretend to have done something.

---

## Install

```bash
git clone https://github.com/IronDingo/elcars.git ~/elcars
cd ~/elcars

# The venv must be able to see the system GTK — PyGObject is not pip-installable here.
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

# The voice: 61 MB, deliberately not in the repo.
curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

cp .env.example .env    # then put your Anthropic key in it
```

The Whisper model downloads itself on first run and caches.

**One gotcha worth knowing in advance**, because it is invisible when it bites: if the HUD
appears as an ordinary draggable window instead of a corner overlay, something has moved
the `ctypes.CDLL("libgtk4-layer-shell.so")` call in `hud.py` below the `import gi` line.
GTK opens its Wayland connection on import, and after that the layer-shell initialiser
quietly does nothing at all. Load order is the whole fix. There is an
`assert LS.is_supported()` standing guard over it.

### Optional: Spotify

```bash
.venv/bin/python spotify.py    # one-time browser login
```

Authorization Code with PKCE, so there is no client secret to leak — put the client ID in
`.env`. Register the redirect URI as exactly `http://127.0.0.1:8888/callback`; Spotify
rejects `localhost`, which costs everyone who meets it about twenty minutes. The refresh
token lands in `.spotify.json` at mode 600, and nothing listens on that port afterwards.

Reading your playlists is free. *Playing* them requires Premium — a business decision, and
Spotify's to make. Without it you get a clean `403` and an honest sentence about it.

### Optional: local places

Set `ELCARS_HOME_AREA` (and `ELCARS_HOME_CITY`) in `.env` and ELCARS will default to your
own neighbourhood when you ask it to find somewhere for dinner. Leave them blank and it
will simply ask which area you mean, which is the correct behaviour for software that does
not know where you live.

---

## Running it

```bash
~/elcars/.venv/bin/python ~/elcars/elcars.py
```

A dot appears in the bottom-right corner after two or three seconds, while Whisper loads.
Then ELCARS spends about a second and a half listening to your empty room to work out what
silence sounds like here — **stay quiet for that moment**, because it is measuring the floor
you will have to speak above. `Ctrl-C` quits.

Say **"Computer"** before every command. One sentence, one action, back to standby — there
is no session to be in or forget you are in.

**If it can't hear you**, the startup line tells you almost everything:

```
calibrated: noise floor 0.0043 → speech gate 0.0130 (raw rms)
```

The gate should land somewhere around 0.01–0.03. If it comes out near 1.0, your microphone
is clipping: PipeWire input wants to sit near 50%, not 100%.

```bash
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.5
```

This is hard-won. An Intel SOF digital microphone at full volume clips past 1.0, and an
earlier version of the calibration routine took the *loudest* thing it heard in a second and
a half — which, reliably, was the Enter key that had just launched the program. The gate
came out at 1.48, above the ceiling of the meter that was supposed to cross it, and nothing
could ever trigger. It looked exactly like a dead microphone. It was arithmetic. The
routine now takes the mean, and detection runs in raw RMS space where there is no ceiling
to hide behind.

Set `LOG_LEVEL = logging.DEBUG` in `elcars.py` to watch the per-window levels directly.

---

## The dot

Most of the time it is a dot. This is the point: an interface for someone who cannot reach
the keyboard should not demand to be looked at, and a dot in the corner can be read from
across a room without reading anything.

| Dot | State | Meaning |
|---|---|---|
| **Blue** | `standby` | Waiting. Say "Computer" |
| **Peach**, with a meter | `capturing` | Hearing you; the bars show your level |
| **Lilac** | `processing` | Thinking |
| **Orange** | `responding` | Speaking |
| **Peach flash** | `executed` | Something actually happened |
| **Salmon** | `confirm-required` | Waiting for you to say "Computer, yes" |
| **Salmon** | `unable-to-comply` | It couldn't. It will tell you why |
| **Blue breathing frame** | — | [Open Channel](#open-channel) is live |

Colours are the Okuda palette — `#FF9933 #FFCC99 #CC99CC #9999FF #CC6666` — because the
thing is called ELCARS and one may as well commit.

The meter blooms upward out of the dot while you speak and collapses when you stop, driven
by the real microphone level. It is also the single most useful diagnostic in the system:
if the bars don't move, the problem is upstream of everything else.

```bash
.venv/bin/python hud.py    # the HUD alone, cycling its states, no microphone involved
```

### Open Channel

Saying "Computer" before every sentence is correct for commands and tiresome for
conversation. So: *"Computer, open channel."* The wake-lock lifts, a soft blue frame
breathes around the dot, and you can simply talk. *"Close channel"* — or forty-five seconds
of quiet — puts the lock back.

The frame exists because a microphone that is listening without a password should be
visibly, continuously obvious about it.

---

## What you can say

[**MANUAL.md**](MANUAL.md) is the full spoken reference. In brief:

**Windows and apps** — open Firefox · switch to my browser · what windows do I have open ·
fullscreen this · make it float · return it to tile · close this window *(asks first)* · go
to workspace 3 · next workspace

**Dictation** — type "hello world" · write "git status" in the terminal · type this in the
Claude Code window

**Sound and screen** — volume up · set volume to 30 · mute · brightness to 50 · pause ·
next track · take a screenshot · lock the screen

**Your own things** — play a movie · play Ted Lasso · play something relaxing · open my
resume · show me that screenshot · play my chill playlist · play 432hz

**The world** — open youtube.com · what time is it · where's good pasta nearby · find five
restaurants in Zamalek · plan an evening out

**Thinking** — think hard about this one · take your time · and anything else you'd ask a
reasonably well-read colleague

When several of your files match what you asked for, it reads back a few and asks which one
you meant. When you name a window and there are two of them, it takes the top-left and
waits to be told *"the other one."*

---

## The tools

Twenty-two. Each one carries its own schema and its own spoken result, and each returns
`(what to say, whether it worked)` — so a failure is a sentence, never a traceback.

| | |
|---|---|
| **Windows** | `launch_app` `focus_app` `list_windows` `fullscreen` `window_layout` `switch_workspace` `close_active_window`† |
| **Input** | `type_text` `type_in_window` |
| **System** | `volume` `set_brightness` `media_control` `screenshot` `lock_screen` `get_context` |
| **Content** | `open_file` `spotify_play` `open_url` |
| **World** | `find_places`‡ |
| **Meta** | `open_channel` `close_channel` `deep_think` |

† asks for spoken confirmation before it runs  ‡ returns data for the brain to reason over,
not a line to read aloud

`launch_app` opens things floating by default, which is what you want when your hands are
not available to tidy a tiling layout afterwards. It resolves friendly names — "terminal",
"browser", "files", "editor" — against whatever is actually installed, via `shutil.which`
over a candidate list, so nothing is pinned to one person's choice of terminal.

`list_windows` is worth singling out: ELCARS can enumerate every open window by class,
title and workspace. It is **not** blind to your desktop. What it cannot do is read the
pixels *inside* a window — there is no vision and no OCR — and the system prompt is explicit
about the difference in both directions, because a voice assistant that under-claims is
nearly as annoying as one that over-claims.

---

## How it works

```
                        ┌──────────────────────────────────────┐
   "Computer, …"  ──▶   │  record()      stops 0.8 s after you │
                        │                do, caps at 12 s      │
                        ├──────────────────────────────────────┤
                        │  Whisper       base.en, int8, CPU    │
                        │                0.08 s. Never the     │
                        │                bottleneck            │
                        ├──────────────────────────────────────┤
                        │  wake gate     the word "computer",  │
                        │                stripped off          │
                        ├──────────────────────────────────────┤
                        │  Brain         Sonnet 5 + 22 tools,  │
       the dot    ◀──── │                tool loop, history    │
    reflects each       ├──────────────────────────────────────┤
    stage live          │  tools.py      hyprctl, pamixer,     │
                        │                wtype, grim, mpv …    │
                        ├──────────────────────────────────────┤
                        │  Piper         resident voice,        │
                        │                streamed to the card  │
                        └──────────────────────────────────────┘
```

Four files, three satellites, no framework:

| | |
|---|---|
| `elcars.py` | The loop and the `Brain`. Runs on a background thread |
| `hud.py` | The dot. GTK4 layer-shell overlay; owns the main thread |
| `tools.py` | The hands. A frozen registry of twenty-two actions |
| `files.py` | The eyes on disk — your films, music, documents |
| `spotify.py` | PKCE login and playlist-by-name |
| `places.py` | The eyes on the map — OpenStreetMap, no key, no card |
| `MANUAL.md` | What to say, for someone who will never read this file |

GTK insists on the main thread, so the HUD owns it and the voice loop runs as a daemon
thread, reaching the interface through `GLib.idle_add`. Three one-line methods —
`ui_state`, `ui_level`, `ui_channel` — are the entire seam between them. The HUD knows
nothing about Whisper and the loop knows nothing about GTK.

### The wake word is not a wake word

There is no trained wake model. ELCARS records in windows, transcribes, and acts if the
word "computer" appears in the transcript. This is honest about what it is: a transcript
gate, not a neural trigger, and so Whisper is always running rather than sleeping until
called.

This was a deliberate choice twice over. openWakeWord was installed, considered, and left
on the shelf, because the gate works, costs nothing, and adds no model to train or ship —
and because the thing it would genuinely improve is idle CPU, which was never the complaint.
The sentence that *was* the complaint — a clipped final word — turned out to be a fixed
five-second recording window, and got fixed by endpointing on silence instead.

### One lane, two gears

Every turn starts on Sonnet, with all twenty-two tools. When a question genuinely needs
thinking about, the model calls `deep_think`; the half-finished turn is thrown away
*unspoken*, and the whole thing re-runs on Opus. You never hear two answers to one
question, and the deep lane costs nothing on the hundred ordinary commands that never ask
for it.

The discard is clean because `_attempt` always works on a trial copy of the history and
only `reply()` commits it. `deep_think` is checked for across the whole hop *before* any
tool executes, so a discarded turn can never leave a half-run side effect behind.

There used to be a third gear: a Haiku lane for menial commands, with the dangerous tools
structurally withheld from it. It was removed, and the reason is instructive. It saved
about 0.8 s on "volume up" and cost a wasted 1.4 s round-trip on everything conversational,
because anything it couldn't handle had to be thrown away and asked again. Menial verbs do
not need a language model at all — "play a movie" is fuzzy string matching over a file
index, not natural-language understanding, and a small model would only be a slow wrapper
around the search. That lane belongs in local, deterministic, free, offline code. It is
not built yet.

### The confirm gate is not a session

Closing a window is the one action in the registry you cannot undo by saying the opposite
of what you just said. So `close_active_window` never runs on first call. The Brain stashes
a one-turn `(tool name, arguments)` pending key, the dot turns salmon, and the model is
instructed to say what is about to happen and ask for *"Computer, yes."* Only a matching
re-call on the very next turn executes it.

What makes this safe rather than merely polite: while a confirmation is pending, every
confirm-gated tool is **removed from the tool list** for that turn. The model cannot
satisfy its own confirmation, because for one turn the capability does not exist. And there
is no listening state, no awaiting-mode, no timer — the next utterance is an ordinary
"Computer, …" like every other.

### The data-back contract

`files.py`, `spotify.py` and `places.py` share one shape, and it is the most reusable idea
in the codebase.

A clean match acts immediately: *"play the Matrix"* finds the file and opens it, with no
model round-trip at all. A **vague** query deliberately **misses** — and hands back a short,
newest-first list of real candidates for the brain to choose from by meaning, which it then
re-calls by exact name. So *"play something relaxing"* works across hundreds of Spotify playlists
without a single embedding, index, or vector store: the language model is the semantic
matcher, which is the one thing it is unambiguously better at than a `for` loop.

The constraint that shapes it is tokens. The whole library is never poured into the
context — fifty filenames on a miss, nothing on a hit. `files.py` walks six XDG folders
fresh on every call, four levels deep, hidden and junk directories pruned, capped at six
thousand files, with no cache at all, so a film that finished downloading ninety seconds
ago is simply there.

`find_places` extends it with a `compose` flag, which marks a tool whose output is *data to
speak about* rather than a line to read out. Compose tools are exempt from the first-hop
short-circuit, so the model always gets another turn to think — otherwise ELCARS would
cheerfully read thirty restaurant names aloud in a row.

### Grounded places, honest taste

`find_places` geocodes an area with Nominatim, asks Overpass for the real food and drink
venues within about 1600 m, and hands back up to thirty of them — names, kinds, cuisines,
and opening hours where somebody has bothered to tag them. Every name is a place that
genuinely exists.

OpenStreetMap has no star ratings and no reliable "open now", so ELCARS claims neither. The
*judgement* — which of these is any good — comes from the model's own knowledge, and the
tool description, the system prompt and the tool's own returned text all independently
instruct it to vouch only for places it actually recognises and never to invent one. The
division is deliberate: **the map supplies the facts, the model supplies the taste, and
neither is allowed to do the other's job.**

Why not a real ratings API? Because every one of them wants a credit card. Google Places
asked for a $30 refundable prepayment, Foursquare's ratings are a paid tier, and
TripAdvisor requires a visual bubble-logo attribution that a voice interface cannot
physically honour. Ratings are commercial data everywhere, and OSM is free, keyless and
cardless. If that calculus ever changes, the shape of `find_places` does not have to.

Public Overpass instances have a relationship with load best described as fraught. An
earlier version tried three mirrors one after another with generous timeouts, which stacked
into a 73-second silence — unusable in a voice interface, where four seconds already feels
long. It now fires at all three simultaneously and takes whichever answers first, under a
hard cap.

---

## Latency

The gap between "stop talking" and "hear the first word" was 11.2 seconds. It is now about
4.3. Getting there meant instrumenting the whole path rather than trusting intuition about
it — the initial hunch was the model handoff, which turned out to be about a third of the
problem.

| | before | after | what it was |
|---|---|---|---|
| Recording | 5.00 s fixed | ~3.1 s | A hard window. Speech ran into the edge and got clipped; silence at the end was just waited through. Now it ends 0.8 s after you stop |
| Whisper | 0.08 s | 0.08 s | **Free. Never the bottleneck — stop suspecting it** |
| Fast lane | 1.40 s | — | Discarded on every handoff. Deleted |
| The brain | 4.81 s | 2.96 s | Sonnet 4.6 → Sonnet 5, same question. The model upgrade alone was 1.9 s |
| Piper | 1.54 s | 0.09 s | Re-spawning the binary cost ~1.5 s of process startup before a single sample — more than the synthesis. The voice is now loaded once and streamed |

*Measured end-to-end on the development machine, on a two-second question, 2026-08-21.*

The Piper result is the one worth remembering. A voice assistant that spawns a process per
sentence spends most of its life in `execve`, and the fix was moving one line out of a
function and into module scope. `stream.stop()` rather than `abort()`, incidentally —
`abort()` clips the last syllable off every sentence, which sounds like a model problem and
is not.

Still on the list: the system prompt plus twenty-two tool schemas come to roughly 3,400
static input tokens on every single call. That is a textbook cache prefix and would cut
per-turn cost by about three quarters. It is purely a cost play — at this size, prompt
caching showed no measurable latency benefit.

---

## What it cannot do

Worth stating plainly, because the registry *is* the boundary.

There is no shell tool. No package manager, no `rm`, no arbitrary command execution, no
network calls beyond the three services named here. ELCARS cannot install software or delete
a file, because nothing in `tools.py` can, and no phrasing of a request conjures a tool that
does not exist.

Two honest caveats on that:

- It can **type into whatever window is focused**. If that window is a terminal, then it is
  a terminal, with everything that implies. This is the price of dictation being the central
  accessibility feature, and it is paid knowingly.
- Everything goes through `subprocess` with an **argument list, never a shell string**, so
  spoken text cannot become shell syntax. Every command run to completion is under an
  eight-second timeout and cannot raise — a missing binary is a sentence, not a stack
  trace. The few that launch something and let go (mpv, Spotify) are detached on purpose.

---

## Known limitations

Current, real, and not hidden in a footnote.

**Locking the screen is a one-way door.** `lock_screen` works; unlocking needs a typed
password. For a voice-first user that is genuine friction with no clean answer, and it is
flagged rather than solved.

**It cannot see inside windows.** No vision, no OCR. It knows that a browser window is open
and what its title is; it has no idea what is drawn in it. "Play the second result" is
therefore out of reach.

**Opening a page is not the same as using one.** `open_url` opens a link. Asking for a
nature documentary gets you a browser with a search in it and nothing more — there is no
search-and-play tool yet, and that gap is the most obvious missing hand.

**History can bleed across a long gap.** The conversation history holds thirty messages and
clears only on restart. Leave the machine for an hour, come back with something unrelated,
and a half-finished intention from before can occasionally resurface and get acted on. It
wants an idle expiry.

**Spotify is playlists only**, by deliberate choice, and playback needs Premium.

**`find_places` plans, it does not navigate.** No directions, no "take me there", no
ratings — see above on why the ratings are absent and will stay absent.

**The brain needs credit.** When an Anthropic balance empties, every single lane fails
identically, which is a convincing impression of a broken program. ELCARS now says *"my
account is out of credits"* out loud instead of apologising vaguely. Check billing before
reading code.

---

## Privacy

What leaves the machine, and nothing else does:

- **Your transcribed command** → Anthropic, once per turn, after you have said "Computer".
- **An area name** → OpenStreetMap, only when you ask it to find somewhere.
- **A playlist name** → Spotify, only when you ask for one.

Speech recognition and speech synthesis both run **locally**: Whisper and Piper never touch
the network. Audio is never written to disk — it lives in a NumPy array for a few seconds
and is overwritten by the next utterance. There is no telemetry, no analytics, and no
account.

The two files that hold secrets, `.env` and `.spotify.json`, are in `.gitignore` and were
never committed. The Spotify token is mode 600. The 61 MB voice model is not in the repo
because repositories are not a content delivery network.

---

## Credits and licence

Place data from [OpenStreetMap](https://www.openstreetmap.org/copyright) —
**© OpenStreetMap contributors**, [ODbL](https://opendatacommons.org/licenses/odbl/) — via
the public [Nominatim](https://nominatim.org/) and [Overpass](https://overpass-api.de/)
services, used within their rate limits and with a real User-Agent.

Standing on: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
[Piper](https://github.com/rhasspy/piper) and the Lessac voice ·
[gtk4-layer-shell](https://github.com/wmww/gtk4-layer-shell) ·
[Hyprland](https://hyprland.org/) · the [Anthropic API](https://docs.anthropic.com/).

ELCARS itself is [MIT licensed](LICENSE).

The LCARS name, the Okuda palette and the general demeanour are affectionate borrowings
from a television programme owned by Paramount. This is a personal accessibility tool and
no affiliation is claimed or implied.
