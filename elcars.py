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

One brain: Sonnet handles every turn with the full toolset. The old Haiku
reflex tier was removed — it cost a discarded round-trip on anything
conversational and bought ~0.8 s on trivial commands. Menial verbs belong in a
local, model-free reflex (reflex.py); DEEP_MODEL is reserved for hard
reasoning.

Run:  ~/elcars/.venv/bin/python ~/elcars/elcars.py
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np
import anthropic
import sounddevice as sd
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from piper import PiperVoice

from hud import Hud   # the ambient dot HUD (view + set_state/set_level/set_channel seam)
import tools as toolkit   # the hands: voice-triggerable desktop actions (Phase 2)

load_dotenv(Path(__file__).with_name(".env"))   # loads ~/elcars/.env if present

# --- config ---------------------------------------------------------------
SAMPLE_RATE = 16000
LISTEN_WINDOW = 5.0                        # wait this long for speech to START, then re-loop
MAX_UTTERANCE = 12.0                       # hard cap on a single utterance
SILENCE_TAIL = 0.8                         # stop this long after the speaker goes quiet
PRE_ROLL = 0.5                             # audio kept from before the first blooming block
WAKE_WORD = "computer"
MIC_GAIN = 8.0                             # RMS -> 0..1 VU *display* scaling only (visual)
SPEECH_RATIO = 3.0                         # detect: speech must exceed room noise × this …
NOISE_FLOOR_MIN = 0.010                    # … and clear this raw-RMS floor (dead silence can't trigger)
MODEL = "claude-sonnet-5"                  # the brain: everything the reflex can't do
DEEP_MODEL = "claude-opus-5"               # deep lane: hard reasoning, on request or on deep_think
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
    "You can find real local places to eat or spend an evening — restaurants, cafés, "
    "bars — with find_places. It grounds you in places that genuinely exist on the map; "
    "you turn that into a recommendation or itinerary from your own knowledge of the "
    "city. There are no star ratings and no reliable opening hours, so never claim them, "
    "never invent a place, and be honest when you don't know one.\n"
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
_voice = PiperVoice.load(PIPER_VOICE)   # once at import (~1.4 s), not per utterance


# --- the brain seam (Sonnet by default, Opus on the deep lane) ------------
class Brain:
    """One brain: MODEL answers every turn with the full toolset. Set .deep to
    route the next turn to DEEP_MODEL instead. reply() → (text, hud_state)."""

    def __init__(self, on_status=lambda s: None):
        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
        self.history = []
        self.on_status = on_status            # push HUD states live as tools fire
        self._tools = toolkit.anthropic_tools()
        self._pending = None                  # (name, input-key) awaiting spoken confirmation
        self.channel_open = False             # Open Channel: wake-lock lifted while True
        self.deep = False                     # route the next turn to DEEP_MODEL

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
            text, state, history, pending, channel = self._attempt(
                prompt, DEEP_MODEL if self.deep else MODEL, SYSTEM, self._tools)
            self.history, self._pending, self.channel_open = history, pending, channel
            return text, state
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                log.error("brain unreachable: API credit balance exhausted")
                return ("My account is out of credits. Local commands still work.",
                        "unable-to-comply")
            log.warning("brain rejected the request: %s", e)
            return "My brain rejected that request.", "unable-to-comply"
        except anthropic.RateLimitError:
            return "I'm being rate limited. Try again in a moment.", "unable-to-comply"
        except anthropic.APIConnectionError:
            return "I can't reach my brain — no network.", "unable-to-comply"
        except Exception as e:                # never let a model hiccup crash the voice loop
            log.warning("brain error: %s", e)
            return "Sorry — I had a problem reaching my brain.", "unable-to-comply"

    def _attempt(self, prompt, model, system, tools):
        """Run the model's tool loop on a TRIAL copy of history, returning
        (text, state, history, pending, channel) and leaving self.* untouched
        until reply() commits it."""
        history = self.history + [{"role": "user", "content": prompt}]
        just_confirmed = self._pending
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
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": out, "is_error": not ok})
                    if spec and spec.compose:
                        # informational tool (e.g. find_places): hand the data back for
                        # the model to speak ABOUT — don't treat it as an executed action
                        # or short-circuit on its raw output
                        self.on_status("processing")
                    else:
                        did_exec = did_exec or ok
                        did_fail = did_fail or not ok
                        self.on_status("executed" if ok else "unable-to-comply")
                        if ok:
                            short_text = out      # last successful tool's spoken line
            history.append({"role": "user", "content": results})

            # short-circuit: one clean successful action on the first hop → speak the
            # tool's own words and skip the extra round-trip back to the model
            if hop == 0 and len(blocks) == 1 and not deferred and did_exec:
                break

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


def record(hud: Hud, gate: float) -> tuple[np.ndarray, float, bool]:
    """Record until the speaker STOPS, instead of burning a fixed window.

    Waits up to LISTEN_WINDOW for speech to begin (raw RMS crossing `gate`); if
    nothing is heard it returns silence so the caller re-loops and re-checks its
    timers. Once speech blooms it keeps recording until SILENCE_TAIL of quiet,
    capped at MAX_UTTERANCE. Only PRE_ROLL of the lead-in is kept, so Whisper
    sees the utterance rather than the wait.

    Detection stays in raw-RMS space, decoupled from the VU display and its 1.0
    cap. Returns the audio, the window's peak raw RMS, and whether any real
    sound was heard.
    """
    block = int(SAMPLE_RATE * 0.05)                      # 50 ms
    tail_blocks, pre_blocks = int(SILENCE_TAIL / 0.05), int(PRE_ROLL / 0.05)
    frames, peak, bloomed, quiet, bloom_at = [], 0.0, False, 0, 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=block) as stream:
        while True:
            indata, _ = stream.read(block)
            frames.append(indata.copy())
            rms = _rms(indata)                           # detect in raw-RMS space
            peak = max(peak, rms)
            hud.ui_level(_meter_level(indata))           # display is separate
            elapsed = len(frames) * 0.05

            if not bloomed:
                if rms >= gate:                          # first real sound → bloom
                    bloomed, bloom_at = True, len(frames) - 1
                    hud.ui_state("capturing")
                elif elapsed >= LISTEN_WINDOW:
                    break                                # nothing said — let the loop re-check
            else:
                quiet = quiet + 1 if rms < gate else 0
                if quiet >= tail_blocks or elapsed - bloom_at * 0.05 >= MAX_UTTERANCE:
                    break

    hud.ui_level(0.0)
    if bloomed:
        frames = frames[max(0, bloom_at - pre_blocks):]  # drop the dead wait
    audio = np.concatenate(frames).flatten() if frames else np.zeros(0, "float32")
    log.debug("window: %.1fs  peak rms %.4f  gate %.4f  → %s",
              len(audio) / SAMPLE_RATE, peak, gate, "HEARD" if bloomed else "silence")
    return audio, peak, bloomed


def transcribe(audio: np.ndarray) -> str:
    # vad_filter makes silence come back as "" — we use that as the quiet signal
    segments, _ = whisper.transcribe(audio, vad_filter=True)
    return "".join(s.text for s in segments).strip()


def say(text: str) -> None:
    """Speak `text` through the resident Piper voice.

    Re-spawning the piper binary cost ~1.5 s of process startup before a single
    sample was produced — more than the synthesis itself. The voice is loaded
    once at import and chunks are written straight to the output stream as they
    arrive.
    """
    stream = None
    try:
        for ch in _voice.synthesize(text):
            if stream is None:
                stream = sd.OutputStream(samplerate=ch.sample_rate,
                                         channels=ch.sample_channels,
                                         dtype="float32")
                stream.start()
            stream.write(ch.audio_float_array)
    finally:
        if stream is not None:
            stream.stop()      # drains buffered audio; abort() would clip the tail
            stream.close()


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
        audio, peak, sound = record(hud, gate)

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
