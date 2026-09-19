#!/usr/bin/env python3
"""EmanueLCARS (ELCARS) — Phase 1: the face (ambient dot HUD).

A single state-coloured dot in the bottom-right corner, rendered as a
Hyprland layer-shell overlay (GTK4 + gtk4-layer-shell). At rest it's just
the dot. When ELCARS is listening, a decibel meter blooms upward above
it. Near-invisible until it matters.

STANDALONE demo: a fake state cycler walks the 8 states so the look can
be signed off before the HUD is wired into the voice loop in elcars.py.

Run:  ~/elcars/.venv/bin/python ~/elcars/hud.py
"""

# gtk4-layer-shell MUST be loaded before libwayland-client, or GTK opens
# its Wayland connection first and init_for_window() silently no-ops
# (you get a plain toplevel window). Force-load it early, before gi.
import ctypes
ctypes.CDLL("libgtk4-layer-shell.so", mode=ctypes.RTLD_GLOBAL)

import logging
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib
from gi.repository import Gtk4LayerShell as LS

log = logging.getLogger("hud")

# --- Okuda palette (memory-locked) ----------------------------------------
ORANGE = "#FF9933"
PEACH  = "#FFCC99"
LILAC  = "#CC99CC"
BLUE   = "#9999FF"
SALMON = "#CC6666"

# --- the 8 states: name -> (css class, dot colour) ------------------------
STATES = {
    "standby":          ("st-standby", BLUE),
    "wake":             ("st-wake",    ORANGE),
    "capturing":        ("st-capture", PEACH),
    "processing":       ("st-process", LILAC),
    "responding":       ("st-respond", ORANGE),
    "executed":         ("st-exec",    PEACH),
    "confirm-required": ("st-confirm", SALMON),
    "unable-to-comply": ("st-unable",  SALMON),
}
STATE_CLASSES = [c for c, _ in STATES.values()]
VU_BARS = 14

# --- stylesheet — transparent; dot glows in its state colour --------------
_STATE_CSS = "\n".join(
    f".wrap.{cls} .dot {{ background:{col}; box-shadow:0 0 9px {col}; }} "
    f".wrap.{cls} .vu-on {{ background:{col}; }}"
    for cls, col in STATES.values()
)
CSS = f"""
window, .wrap {{ background:transparent; }}
.dot {{ min-width:20px; min-height:20px; border-radius:10px; background:{BLUE};
        box-shadow:0 0 9px {BLUE}; }}
.vu-bar {{ min-width:20px; min-height:5px; border-radius:2px;
           background:rgba(255,255,255,0.14); }}
.vu-on  {{ background:{PEACH}; }}
{_STATE_CSS}
"""


class Hud(Gtk.Application):
    def __init__(self, worker=None):
        super().__init__(application_id="com.elcars.hud")
        self.state = "standby"
        self.worker = worker      # callable(hud) run on a background thread

    # --- build ------------------------------------------------------------
    def do_activate(self):
        self._load_css()
        win = Gtk.ApplicationWindow(application=self)

        # layer-shell: pinned to the bottom-right corner, floating, sized
        # to its content so it stays tiny until the meter blooms
        LS.init_for_window(win)
        assert LS.is_supported(), "wlr-layer-shell not available on this compositor"
        LS.set_namespace(win, "elcars-hud")
        LS.set_layer(win, LS.Layer.TOP)
        LS.set_anchor(win, LS.Edge.RIGHT, True)
        LS.set_anchor(win, LS.Edge.BOTTOM, True)
        LS.set_margin(win, LS.Edge.RIGHT, 18)
        LS.set_margin(win, LS.Edge.BOTTOM, 18)
        LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)

        win.set_child(self._build_indicator())
        win.present()

        self.set_state("standby")
        if self.worker is not None:
            threading.Thread(target=self.worker, args=(self,),
                             daemon=True).start()

    def _build_indicator(self):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        wrap.add_css_class("wrap")
        wrap.set_halign(Gtk.Align.END)
        wrap.set_valign(Gtk.Align.END)
        self.wrap = wrap

        # decibel meter, revealed sliding upward only while listening
        vu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        vu.set_halign(Gtk.Align.END)
        self.vu_bars = []
        for _ in range(VU_BARS):
            b = Gtk.Box(); b.add_css_class("vu-bar")
            vu.append(b); self.vu_bars.append(b)

        self.meter = Gtk.Revealer()
        self.meter.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.meter.set_transition_duration(220)
        self.meter.set_child(vu)
        self.meter.set_reveal_child(False)
        wrap.append(self.meter)

        # the always-present dot
        self.dot = Gtk.Box(); self.dot.add_css_class("dot")
        self.dot.set_halign(Gtk.Align.END)
        wrap.append(self.dot)
        return wrap

    # --- state (the seam the voice loop will call) ------------------------
    def set_state(self, name):
        self.state = name
        cls, _col = STATES[name]
        for c in STATE_CLASSES:
            self.wrap.remove_css_class(c)
        self.wrap.add_css_class(cls)
        blooming = name == "capturing"
        self.meter.set_reveal_child(blooming)              # bloom while listening
        log.info("state → %-9s%s", name, "  ▮ meter" if blooming else "")

    def set_level(self, level):          # 0.0-1.0 input meter
        lit = round(max(0.0, min(1.0, level)) * VU_BARS)
        for i, bar in enumerate(self.vu_bars):
            on = i >= (VU_BARS - lit)     # bars fill from the bottom (by the dot)
            (bar.add_css_class if on else bar.remove_css_class)("vu-on")

    # --- thread-safe seam: the voice loop calls these from its thread -----
    def ui_state(self, name):
        GLib.idle_add(self.set_state, name)

    def ui_level(self, level):
        GLib.idle_add(self.set_level, level)

    def _load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _demo_worker(hud):
    """Standalone preview: cycle the states, animate the meter on capture."""
    import itertools
    import random
    import time
    for name in itertools.cycle(STATES):
        hud.ui_state(name)
        if name == "capturing":
            for _ in range(22):
                hud.ui_level(random.uniform(0.5, 1.0))
                time.sleep(0.08)
            hud.ui_level(0.0)
        else:
            time.sleep(1.6)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-5s  %(message)s",
                        datefmt="%H:%M:%S")
    Hud(worker=_demo_worker).run(None)
