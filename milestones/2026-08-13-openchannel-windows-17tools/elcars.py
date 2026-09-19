#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Phase 0–2: the voice loop that can act.

Say "Computer …" → transcribe (Whisper) → think & act (Claude + tools) →
speak (Piper), with the ambient dot HUD (hud.py) reflecting state and
blooming a live mic-level meter while listening.

How it works — one clean shot each time:
  - Say "Computer …" for every command. It transcribes, thinks, (optionally
    acts through its tools,) speaks, then returns to standby. No session
    that "stays on" — unless you open one deliberately (below).
  - It keeps a short conversation history, so a clarifying exchange still
    works: "Computer, give me a poem" → "Classical or modern?" →
    "Computer, anything" answers in context.
  - It can act: open/switch apps, fullscreen, workspaces, screenshot, lock,
    volume, brightness, media, type text, open links (tools.py). Hard-to-undo
    actions (e.g. closing a window) ask for a spoken "Computer, yes" first.
  - Open Channel: "Computer, open channel" lifts the wake-lock so you can talk
    freely; "close channel" (or 45 s of quiet) re-locks it. The HUD frame
    glows while it's open.

The HUD's GTK loop owns the main thread; this voice loop runs on a
background thread and pushes state via hud.ui_state / hud.ui_level / hud.ui_channel.

Run:  ~/elcars/.venv/bin/python ~/elcars/elcars.py
"""

import logging
import subprocess
import time
from pathlib import Path

import numpy as np
import anthropic
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv
from faster_whisper import WhisperModel

from hud import Hud   # the ambient dot HUD (view + set_state/set_level/set_channel seam)
import tools as toolkit   # the hands: voice-triggerable desktop actions (Phase 2)

load_dotenv(Path(__file__).with_name(".env"))   # loads ~/elcars/.env if present

# --- config ---------------------------------------------------------------
SAMPLE_RATE = 16000
RECORD_SECONDS = 5
WAKE_WORD = "computer"
MIC_GAIN = 8.0                             # RMS -> 0..1 VU *display* scaling only (visual)
SPEECH_RATIO = 3.0                         # detect: speech must exceed room noise × this …
NOISE_FLOOR_MIN = 0.010                    # … and clear this raw-RMS floor (dead silence can't trigger)
MODEL = "claude-sonnet-4-6"                # brain. Haiku=fast/cheap, Opus 4.8=max
MAX_HISTORY = 30                           # messages kept for context (a tool turn adds 3-4)
MAX_TOOL_HOPS = 5                          # cap the tool loop so it can't spin
CHANNEL_TIMEOUT = 45                       # Open Channel re-locks after this many quiet seconds
PIPER_VOICE = str(Path(__file__).with_name("en_US-lessac-medium.onnx"))
SYSTEM = (
    "You are ELCARS — the Enhanced Library Computer Access and Retrieval System, "
    "the ship's computer, operated hands-free by voice. When asked your name or "
    "what you are, you are ELCARS.\n"
    "You can ACT on this computer through your tools: open and switch apps, "
    "fullscreen and switch workspaces, screenshot, lock the screen, control volume, "
    "brightness and media, type text into the focused field, open links, and read "
    "the clock. When the user asks you to DO something you have a tool for, call it — "
    "don't explain how they could do it themselves; they operate by voice and want "
    "it done.\n"
    "You CAN see the open windows by app and title (list_windows) and type into a "
    "named one (type_in_window) — you are not blind to the desktop. You just can't "
    "read what's drawn inside a window (no vision or OCR), so don't claim you can.\n"
    "Some tools need confirmation. When a tool result says CONFIRM_REQUIRED, say in "
    "one short sentence what will happen and ask them to say 'Computer, yes'.\n"
    "If the user wants to keep talking without saying 'Computer' each time, call "
    "open_channel; call close_channel when they're done. In Open Channel you'll hear "
    "everything they say, so only act on what's clearly addressed to you.\n"
    "Replies are spoken aloud: keep them short, natural and plain — no markdown, "
    "lists, code, or emoji. Confirm actions briefly, e.g. 'Opening Firefox.'"
)
LOG_LEVEL = logging.INFO                    # DEBUG for detail, WARNING to quiet the HUD

log = logging.getLogger("voice")
whisper = WhisperModel("base.en", device="cpu", compute_type="int8")


# --- the brain seam (Claude + tools; swap for a local model later) --------
class Brain:
    """Claude with conversation memory + tool-calling. reply() → (text, hud_state)."""

    def __init__(self, on_status=lambda s: None):
        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
        self.history = []
        self.on_status = on_status            # push HUD states live as tools fire
        self.tools = toolkit.anthropic_tools()
        self._pending = None                  # (name, input-key) awaiting spoken confirmation
        self.channel_open = False             # Open Channel: wake-lock lifted while True

    @staticmethod
    def _key(inp):
        return tuple(sorted((inp or {}).items()))

    def reply(self, prompt: str) -> tuple[str, str]:
        just_confirmed = self._pending        # valid for exactly this one turn
        self._pending = None
        self.history.append({"role": "user", "content": prompt})

        did_exec = did_fail = deferred = False
        resp = None
        for _ in range(MAX_TOOL_HOPS):
            # once we've deferred a confirm this turn, hide confirm tools so the
            # model can't re-issue it in-turn — it must ask the user out loud
            tool_set = (self.tools if not deferred else
                        [t for t in self.tools
                         if not toolkit.REGISTRY[t["name"]].confirm])
            resp = self.client.messages.create(
                model=MODEL, max_tokens=1024, system=SYSTEM,
                thinking={"type": "disabled"}, tools=tool_set, messages=self.history)
            self.history.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break

            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                spec = toolkit.REGISTRY.get(b.name)
                if spec is not None and spec.confirm and \
                        (b.name, self._key(b.input)) != just_confirmed:
                    self._pending = (b.name, self._key(b.input))
                    deferred = True
                    self.on_status("confirm-required")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                        "content": "CONFIRM_REQUIRED: this action needs spoken "
                                   "confirmation. In one short sentence tell the user "
                                   "exactly what will happen, then ask them to say "
                                   "'Computer, yes'. Do not call the tool again now."})
                else:
                    if b.name in toolkit.MODE_TOOLS:      # Open Channel enter/exit
                        self.channel_open = (b.name == "open_channel")
                    text, ok = toolkit.execute(b.name, b.input)
                    did_exec = did_exec or ok
                    did_fail = did_fail or not ok
                    self.on_status("executed" if ok else "unable-to-comply")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": text, "is_error": not ok})
            self.history.append({"role": "user", "content": results})

        reply_text = "".join(b.text for b in resp.content if b.type == "text").strip()
        self._trim()
        state = ("confirm-required" if deferred else
                 "unable-to-comply" if did_fail and not did_exec else
                 "executed" if did_exec else "responding")
        return reply_text or "Done.", state

    def _trim(self):
        """Keep context bounded, and never let the window start mid tool-exchange
        (an orphan tool_result with no matching tool_use is a 400)."""
        if len(self.history) <= MAX_HISTORY:
            return
        self.history = self.history[-MAX_HISTORY:]
        while self.history and not (self.history[0]["role"] == "user"
                                    and isinstance(self.history[0]["content"], str)):
            self.history.pop(0)


# --- ears + voice ---------------------------------------------------------
def _rms(indata) -> float:
    """Raw RMS of a block (uncapped) — the basis for speech detection."""
    return float(np.sqrt(np.mean(np.square(indata))))


def _meter_level(indata) -> float:
    """RMS scaled to the 0..1 VU range — display only."""
    return min(1.0, _rms(indata) * MIC_GAIN)


def calibrate(hud: Hud, seconds: float = 1.5) -> float:
    """Measure the room's average noise floor (raw RMS) → a speech gate.

    Uses the MEAN of the blocks, not the peak: a single transient (the Enter
    key that launched us, a cough) must not set the floor. The gate is a
    multiple of that floor with an absolute minimum, and lives in raw-RMS
    space — so it can never land above what real speech produces.
    """
    hud.ui_state("processing")                     # brief 'booting' cue on the dot
    levels = []

    def on_block(indata, _frames, _time, _status):
        levels.append(_rms(indata))

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=int(SAMPLE_RATE * 0.05), callback=on_block):
        sd.sleep(int(seconds * 1000))
    noise = float(np.mean(levels)) if levels else 0.0
    gate = max(NOISE_FLOOR_MIN, noise * SPEECH_RATIO)
    log.info("calibrated: noise floor %.4f → speech gate %.4f (raw rms)", noise, gate)
    return gate


def record(hud: Hud, seconds: int, gate: float) -> tuple[np.ndarray, float, bool]:
    """Record `seconds` of mono audio, streaming the live level to the HUD.

    Detection is in raw-RMS space (decoupled from the VU display and its 1.0
    cap): the meter blooms (state → capturing) the first time a block's raw
    RMS crosses `gate`. A window that never crosses is dropped without running
    Whisper. Returns the audio, the window's peak raw RMS, and whether any
    real sound was heard.
    """
    frames = []
    peak = 0.0
    bloomed = False

    def on_block(indata, _frames, _time, _status):
        nonlocal peak, bloomed
        frames.append(indata.copy())
        rms = _rms(indata)                             # detect in raw-RMS space
        peak = max(peak, rms)
        if not bloomed and rms >= gate:                # first real sound → bloom
            bloomed = True
            hud.ui_state("capturing")
        hud.ui_level(_meter_level(indata))             # display is separate

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=int(SAMPLE_RATE * 0.05), callback=on_block):
        sd.sleep(int(seconds * 1000))
    hud.ui_level(0.0)
    audio = np.concatenate(frames).flatten() if frames else np.zeros(0, "float32")
    log.debug("window: peak rms %.4f  gate %.4f  → %s",
              peak, gate, "HEARD" if bloomed else "silence")
    return audio, peak, bloomed


def transcribe(audio: np.ndarray) -> str:
    # vad_filter makes silence come back as "" — we use that as the quiet signal
    segments, _ = whisper.transcribe(audio, vad_filter=True)
    return "".join(s.text for s in segments).strip()


def say(text: str) -> None:
    subprocess.run(["piper", "-m", PIPER_VOICE, "-f", "/tmp/elcars_out.wav"],
                   input=text, text=True, check=True)
    audio, sr = sf.read("/tmp/elcars_out.wav", dtype="float32")
    sd.play(audio, sr)
    sd.wait()


def _strip_wake(text: str) -> str:
    """Drop a leading 'Computer' and trailing punctuation from the utterance."""
    i = text.lower().find(WAKE_WORD)
    if i == -1:
        return text.strip()
    return text[i + len(WAKE_WORD):].lstrip(" ,.!?:;-").strip()


# --- loop (runs on a background thread; drives the HUD) -------------------
def voice_loop(hud: Hud) -> None:
    brain = Brain(on_status=hud.ui_state)   # Brain drives the dot as tools fire
    gate = calibrate(hud)    # bloom threshold fitted to this mic/room
    last_active = time.monotonic()
    log.info('ELCARS ready — say "Computer …". Ctrl-C to quit.')

    while True:
        hud.ui_channel(brain.channel_open)              # frame glows while Open Channel
        hud.ui_state("standby")
        audio, peak, sound = record(hud, RECORD_SECONDS, gate)

        # Open Channel auto-close: re-lock after a quiet spell so it can't hang open
        if brain.channel_open and time.monotonic() - last_active > CHANNEL_TIMEOUT:
            brain.channel_open = False
            hud.ui_channel(False)
            log.info("open channel timed out — re-locking")
            hud.ui_state("responding")
            say("Channel closed.")
            last_active = time.monotonic()
            continue

        if not sound:                                   # silent window — skip Whisper
            continue

        hud.ui_state("processing")
        heard = transcribe(audio)
        # Open Channel: any speech is addressed to ELCARS. Otherwise require the wake word.
        if not (brain.channel_open or WAKE_WORD in heard.lower()):
            if heard:
                log.info('ignored (no wake word): "%s"', heard)
            continue

        command = _strip_wake(heard)                    # drops a courtesy "Computer" if present
        if not command:
            log.debug('nothing after wake: "%s"', heard)
            continue

        log.info("you%s: %s", " (open)" if brain.channel_open else "", command)
        reply_text, state = brain.reply(command)
        log.info("elcars[%s]: %s", state, reply_text)
        hud.ui_channel(brain.channel_open)              # reflect a just-opened/closed channel now
        hud.ui_state(state)
        say(reply_text)
        last_active = time.monotonic()


if __name__ == "__main__":
    logging.basicConfig(level=LOG_LEVEL,
                        format="%(asctime)s  %(name)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    try:
        Hud(worker=voice_loop).run(None)
    except KeyboardInterrupt:
        pass
