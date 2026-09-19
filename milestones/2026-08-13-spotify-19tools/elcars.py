#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Phase 0–2: the voice loop that can act.

Say "Computer …" → transcribe (Whisper) → think & act (Claude + tools) →
speak (Piper), with the ambient dot HUD (hud.py) reflecting state and
blooming a live mic-level meter while listening.

How it works — one clean shot each time:
  - Say "Computer …" for every command. It transcribes, thinks, (optionally
    acts through its tools,) speaks, then returns to standby.
  - It keeps a short conversation history, so a clarifying exchange still works.
  - It can act via tools.py (apps, windows, volume, media, typing, links, …).
    Hard-to-undo actions (e.g. closing a window) ask for a spoken "Computer, yes".
  - Open Channel: "Computer, open channel" lifts the wake-lock so you can talk
    freely; "close channel" (or 45 s of quiet) re-locks it. The HUD frame glows.

Tiered brain: a fast reflex (Haiku) handles simple, unambiguous one-shot
commands; for anything needing judgment it calls hand_off and a smart model
(Sonnet/Opus) takes over with the full toolset. Safety is structural — the fast
lane is only handed the menial, reversible tools.

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
FAST_MODEL = "claude-haiku-4-5"            # fast lane: menial one-shot commands
SMART_MODEL = "claude-sonnet-4-6"          # smart lane: anything needing judgment (Opus for hardest)
MAX_HISTORY = 30                           # messages kept for context (a tool turn adds 3-4)
MAX_TOOL_HOPS = 5                          # cap the tool loop so it can't spin
CHANNEL_TIMEOUT = 45                       # Open Channel re-locks after this many quiet seconds
PIPER_VOICE = str(Path(__file__).with_name("en_US-lessac-medium.onnx"))
SYSTEM = (
    "You are ELCARS — the Enhanced Library Computer Access and Retrieval System, "
    "the ship's computer, operated hands-free by voice. When asked your name or "
    "what you are, you are ELCARS.\n"
    "You can ACT on this computer through your tools: open and switch apps, "
    "fullscreen, float or tile, and switch workspaces, screenshot, lock the screen, control volume, "
    "brightness and media, type text into the focused field or a named window, open "
    "links, and read the clock. When the user asks you to DO something you have a tool "
    "for, call it — don't explain how they could do it themselves; they operate by "
    "voice and want it done.\n"
    "You CAN see the open windows by app and title (list_windows) and type into a "
    "named one (type_in_window) — you are not blind to the desktop. You just can't "
    "read what's drawn inside a window (no vision or OCR), so don't claim you can.\n"
    "Equally, don't UNDER-sell yourself: when you have a tool for something you can "
    "do it — say so plainly and just do it, don't disclaim or apologise for an ability "
    "you have. You can play Spotify playlists by name with spotify_play (once the user "
    "has linked their account).\n"
    "Some tools need confirmation. When a tool result says CONFIRM_REQUIRED, say in "
    "one short sentence what will happen and ask them to say 'Computer, yes'.\n"
    "If the user wants to keep talking without saying 'Computer' each time, call "
    "open_channel; call close_channel when they're done. In Open Channel you'll hear "
    "everything they say, so only act on what's clearly addressed to you.\n"
    "Replies are spoken aloud: keep them short, natural and plain — no markdown, "
    "lists, code, or emoji. Confirm actions briefly, e.g. 'Opening Firefox.'"
)
FAST_SYSTEM = (
    "You are ELCARS's fast reflex. You handle ONLY simple, unambiguous, single-step "
    "desktop commands using your tools (open/focus/list apps and windows, volume, "
    "brightness, media, workspace, fullscreen, screenshot, the time, and opening or "
    "closing the listening channel). Do exactly one obvious action, then give a very "
    "short spoken confirmation.\n"
    "For ANYTHING else — questions, conversation, ambiguity, more than one step, "
    "typing text, closing or deleting things, or anything you are not fully certain "
    "maps to one of your tools — call hand_off and nothing else. When in doubt, "
    "hand_off. Never guess.\n"
    "Spoken replies: short and plain, no markdown or lists."
)
HAND_OFF = {
    "name": "hand_off",
    "description": (
        "Hand this request up to the senior assistant. Call this for ANYTHING that is "
        "not a single, obvious, unambiguous action from your tools: questions, "
        "conversation, ambiguity, multiple steps, typing text, closing things, or "
        "anything you are not fully sure about. When in doubt, hand_off. Do nothing else."
    ),
    "input_schema": {"type": "object", "properties": {}},
}
LOG_LEVEL = logging.INFO                    # DEBUG for detail, WARNING to quiet the HUD

log = logging.getLogger("voice")
whisper = WhisperModel("base.en", device="cpu", compute_type="int8")


# --- the brain seam (tiered Claude: fast reflex → smart on hand_off) ------
class Brain:
    """Tiered brain: Haiku handles menial one-shot commands; on hand_off (or
    anything it can't cleanly act on) a smart model takes over with the full
    toolset. reply() → (text, hud_state)."""

    def __init__(self, on_status=lambda s: None):
        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
        self.history = []
        self.on_status = on_status            # push HUD states live as tools fire
        all_tools = toolkit.anthropic_tools()
        self._smart_tools = all_tools
        self._fast_tools = [t for t in all_tools
                            if toolkit.REGISTRY[t["name"]].fast] + [HAND_OFF]
        self._pending = None                  # (name, input-key) awaiting spoken confirmation
        self.channel_open = False             # Open Channel: wake-lock lifted while True

    @staticmethod
    def _key(inp):
        return tuple(sorted((inp or {}).items()))

    @staticmethod
    def _is_confirm(schema):
        spec = toolkit.REGISTRY.get(schema["name"])
        return bool(spec and spec.confirm)

    @staticmethod
    def _trimmed(history):
        """Bound context; never start the window mid tool-exchange (orphan
        tool_result with no matching tool_use is a 400)."""
        if len(history) <= MAX_HISTORY:
            return history
        history = history[-MAX_HISTORY:]
        while history and not (history[0]["role"] == "user"
                               and isinstance(history[0]["content"], str)):
            history = history[1:]
        return history

    def reply(self, prompt: str) -> tuple[str, str]:
        try:
            # Fast lane first — unless a confirmation is pending, which needs the
            # smart model + full context to resolve safely.
            outcome = None
            if self._pending is None:
                outcome = self._attempt(prompt, FAST_MODEL, FAST_SYSTEM,
                                        self._fast_tools, allow_handoff=True)
            if outcome is None:
                log.info("lane: smart%s", " (fast handed off)" if self._pending is None else "")
                outcome = self._attempt(prompt, SMART_MODEL, SYSTEM,
                                        self._smart_tools, allow_handoff=False)
            else:
                log.info("lane: fast")
            text, state, history, pending, channel = outcome
            self.history, self._pending, self.channel_open = history, pending, channel
            return text, state
        except Exception as e:                # never let a model hiccup crash the voice loop
            log.warning("brain error: %s", e)
            return "Sorry — I had a problem reaching my brain.", "unable-to-comply"

    def _attempt(self, prompt, model, system, tools, allow_handoff):
        """Run one model's tool loop on a TRIAL copy of history. Returns
        (text, state, history, pending, channel), or None if it hands off /
        takes no action (fast lane only) — leaving self.* untouched."""
        history = self.history + [{"role": "user", "content": prompt}]
        just_confirmed = self._pending        # only relevant in the smart lane
        pending = None
        channel = self.channel_open
        did_exec = did_fail = deferred = False
        short_text = None
        resp = None

        for hop in range(MAX_TOOL_HOPS):
            tool_set = (tools if not deferred else
                        [t for t in tools if not self._is_confirm(t)])
            resp = self.client.messages.create(
                model=model, max_tokens=1024, system=system,
                thinking={"type": "disabled"}, tools=tool_set, messages=history)
            history.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break

            blocks = [b for b in resp.content if b.type == "tool_use"]
            if allow_handoff and any(b.name == "hand_off" for b in blocks):
                if not did_exec:
                    return None                   # clean escalation — discard this attempt
                blocks = [b for b in blocks if b.name != "hand_off"]   # already acted; drop it

            results = []
            for b in blocks:
                spec = toolkit.REGISTRY.get(b.name)
                if spec is not None and spec.confirm and \
                        (b.name, self._key(b.input)) != just_confirmed:
                    pending = (b.name, self._key(b.input))
                    deferred = True
                    self.on_status("confirm-required")
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                        "content": "CONFIRM_REQUIRED: this action needs spoken "
                                   "confirmation. In one short sentence tell the user "
                                   "exactly what will happen, then ask them to say "
                                   "'Computer, yes'. Do not call the tool again now."})
                else:
                    if b.name in toolkit.MODE_TOOLS:
                        channel = (b.name == "open_channel")
                    out, ok = toolkit.execute(b.name, b.input)
                    did_exec = did_exec or ok
                    did_fail = did_fail or not ok
                    self.on_status("executed" if ok else "unable-to-comply")
                    if ok:
                        short_text = out          # last successful tool's spoken line
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": out, "is_error": not ok})
            history.append({"role": "user", "content": results})

            # short-circuit: one clean successful action on the first hop → speak the
            # tool's own words and skip the extra round-trip back to the model
            if hop == 0 and len(blocks) == 1 and not deferred and did_exec:
                break

        # fast lane only "handles" a turn if it actually acted; otherwise (it just
        # talked, or its one action failed) let the smart lane answer properly
        if allow_handoff and not did_exec:
            return None

        text = short_text if (short_text is not None and did_exec and not deferred) else \
            "".join(b.text for b in resp.content if b.type == "text").strip()
        state = ("confirm-required" if deferred else
                 "unable-to-comply" if did_fail and not did_exec else
                 "executed" if did_exec else "responding")
        return text or "Done.", state, self._trimmed(history), pending, channel


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
