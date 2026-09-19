# ELCARS — Voice Manual

*EmanueLCARS — the Enhanced Library Computer Access and Retrieval System.*
Hands-free voice control for the desktop. Everything below is spoken.

---

## Starting & stopping

```
~/elcars/.venv/bin/python ~/elcars/elcars.py     # or: ./elcars.py
```

- A small **dot** appears bottom-right after a couple of seconds (Whisper loading).
- On startup it samples the room for ~1.5 s to set its mic threshold — **stay quiet** for that moment.
- Quit with **Ctrl-C** in the terminal.

**The wake word is “Computer.”** Say *“Computer, …”* before every command — unless you’ve opened a channel (see below).

---

## The dot (what the colours mean)

| Dot | Meaning |
|-----|---------|
| **Blue** | Standby — say “Computer” |
| **Peach** | Capturing your voice (a meter blooms upward) |
| **Lilac** | Thinking |
| **Orange** | Speaking a reply |
| **Peach flash** | An action ran (executed) |
| **Salmon** | Needs confirmation, or couldn’t comply |
| **Blue breathing frame** | **Open Channel is live** (see below) |

---

## What you can say

### Apps & windows
- “Computer, **open** Firefox” · “open the terminal” · “launch the files app”
  *(apps open **floating** by default, like Super+T)*
- “Computer, **switch to** my browser” · “go to the elcars terminal”
- “Computer, **what windows do I have open?**” · “what am I looking at?”
- “Computer, **fullscreen** this” · “exit fullscreen”
- “Computer, **make it float**” · “**return it to tile**” · “toggle floating”
- “Computer, **close this window**” → it asks first; reply **“Computer, yes.”**
- “Computer, **go to workspace 3**” · “next workspace” · “previous desktop”

### Typing (dictation)
- “Computer, **type** ‘hello world’” — into whatever window is focused.
- “Computer, **write** ‘git status’ **in the terminal**” — focuses that window, then types.
- “Computer, **type** ‘…’ **in the Claude Code window**”.

### Sound & screen
- “Computer, **volume up** / **down**” · “set volume to 30” · “mute”
- “Computer, **brightness to 50**” · “dim the screen”
- “Computer, **pause** / play” · “next track” · “previous”
- “Computer, **take a screenshot**” — saved to your Pictures folder.
- “Computer, **lock the screen.**”  *(Note: unlocking needs the keyboard.)*

### Your files (movies, music, documents)
- “Computer, **play a movie**” — lists what’s in your Downloads/Videos; name one to play it.
- “Computer, **play Ted Lasso**” · “put on **the Matrix**” — opens the best match in mpv (fullscreen).
- “Computer, **play something** relaxing / scary” — it picks by vibe from what you have.
- “Computer, **open my resume**” · “open that **PDF**” · “show me the **screenshot** I took”.
- Many matches? It reads back a few and asks which — just say the one you want.

### Web & info
- “Computer, **open** youtube.com” · “go to github dot com”
- “Computer, **what time is it?**”

### Just talking
- Ask it anything — “Computer, tell me a joke”, “what’s the capital of Japan?”

---

## Open Channel (talk without “Computer”)

- “Computer, **open channel**” → the wake-lock lifts; **just talk**, no “Computer” needed. A soft blue frame glows around the dot.
- “**Close channel**”, or **45 seconds of quiet**, returns it to normal.

---

## Naming windows

When you name a window (“the terminal”, “the browser”):

1. It looks on your **current workspace** first.
2. Two of the same there? It takes the **top-left** one — say **“the other one”** to switch.
3. Be specific any time: a **title** (“the elcars terminal”, “the Claude Code window”) or a **position** (“the top-right terminal”).

Not sure what’s open? “**What windows do I have?**” lists them.

---

## Two speeds (why some replies are instant)

Simple commands (open an app, volume, workspace, screenshot, the time) run on a **fast** model — near-instant. Anything needing judgment (typing content, closing, questions, ambiguity) is automatically handed to a **smarter** model. You’ll see `lane: fast` or `lane: smart` in the terminal log.

**Safety:** the fast model is only ever given the harmless, reversible tools — it *cannot* type, close, lock, or open links; those always go to the smart model, and closing still asks you first. ELCARS has no ability to install software, delete files, or run arbitrary commands at all.

---

## If it can’t hear you

- Mic should sit around **50%** (`wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.5`). Too hot clips and blinds it.
- The startup log prints `calibrated: noise floor … → speech gate …` — the gate should be small (≈0.01–0.03), never near 1.0.
- Set `LOG_LEVEL = logging.DEBUG` in `elcars.py` to watch per-window levels.
