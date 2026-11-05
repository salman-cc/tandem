# SPDX-License-Identifier: GPL-3.0-only
#
# kde.py - KDE Plasma (KWin, Wayland) backend.
#
# Author: Muhammed Salman
#
# Virtual monitor + capture: KWin's zkde_screencast_unstable_v1 Wayland
# protocol creates a virtual output and hands over a PipeWire node, exactly
# the mechanism krfb-virtualmonitor uses. GStreamer turns the node into
# H.264 the same way as on GNOME:
#
#     zkde_screencast_unstable_v1.stream_virtual_output(name, w, h, scale)
#         -> PipeWire node -> pipewiresrc -> VA-API / x264 / openh264
#         -> raw Annex-B H.264 on a pipe -> tandemd's parser
#
# The virtual output lives exactly as long as the stream: KWin removes it
# when the stream is closed (and when our Wayland connection dies), so a
# stopped or killed daemon leaves no monitor behind.
#
# Placement: kscreen-doctor (ships with Plasma) puts the output on the
# requested side of the primary monitor and sets its scale. Only our own
# output is ever touched.
#
# Touch input: the shared uinput touchscreen (uinput_touch.py), mapped onto
# the virtual output through KWin's per-device D-Bus property
# (org.kde.KWin.InputDevice "outputName" on /org/kde/KWin/InputDevice/<sys>),
# the same mechanism System Settings -> Touch Screen uses. KWin resolves the
# output's uuid and persists the mapping to kcminputrc itself.
#
# Caveat: zkde_screencast_unstable_v1's own description warns that it is a
# KDE implementation detail which may change without a version bump. It is
# what KDE's own krfb uses, and it has been stable since Plasma 5.24, but a
# future KWin could still break it.
#
# pywayland is imported lazily so the other backends never need it.

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import uinput_touch

from . import gnome

# The output's connector name is "Virtual-" + the name we ask for (KWin's
# DRM backend prefixes it), and KScreen matches kscreen-doctor commands by
# that name.
OUTPUT_NAME = "Tandem"
CONNECTOR_PREFIX = "Virtual-"

CURSOR_EMBEDDED = 2              # zkde pointer mode: draw the cursor in

# How long to wait for KWin to announce the PipeWire node, and for the
# output to appear in KScreen after the pipeline negotiates.
SETUP_TIMEOUT = 10
PLACE_TIMEOUT = 15


def log(*a):
    try:                       # never raise on a closed terminal / TUI pipe
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def is_kde_wayland():
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "KDE" in desk and bool(os.environ.get("WAYLAND_DISPLAY"))


def pywayland_reason():
    """Why the pywayland import fails here, or None if it works."""
    if importlib.util.find_spec("pywayland") is None:
        return "pywayland is not installed"
    return None


def kscreen_outputs():
    """KScreen's outputs as dicts, or [] when kscreen-doctor can't say."""
    try:
        r = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode != 0:
            return []
        return json.loads(r.stdout).get("outputs", [])
    except Exception:
        return []


def find_connector(outputs=None):
    """Our virtual output's KScreen name ("Virtual-Tandem"), or None."""
    if outputs is None:
        outputs = kscreen_outputs()
    names = [o.get("name", "") for o in outputs if o.get("enabled", True)]
    exact = CONNECTOR_PREFIX + OUTPUT_NAME
    if exact in names:
        return exact
    return next((n for n in names if OUTPUT_NAME in n), None)


def primary_monitor_desc():
    """One-line description of the primary display, for the TUI."""
    for o in kscreen_outputs():
        if o.get("primary"):
            size = o.get("size", {})
            return (f"{o.get('name', '?')}  "
                    f"{size.get('width', '?')}x{size.get('height', '?')}"
                    f"  scale {o.get('scale', 1):g}")
    return ""


def _logical_rect(o):
    """(x, y, w, h) of a KScreen output in logical pixels."""
    pos = o.get("pos", {})
    size = o.get("size", {})
    scale = o.get("scale", 1) or 1
    return (pos.get("x", 0), pos.get("y", 0),
            round(size.get("width", 0) / scale),
            round(size.get("height", 0) / scale))


def place_connector(connector, side, scale):
    """Put `connector` on `side` of the primary monitor, at `scale`.

    kscreen-doctor applies a whole output configuration at once; we change
    only our own output's scale and position. Every other monitor is left
    exactly as it is. Returns True if a change was sent.
    """
    outputs = kscreen_outputs()
    ext = next((o for o in outputs if o.get("name") == connector), None)
    others = [o for o in outputs
              if o.get("name") != connector and o.get("enabled", True)]
    if ext is None:
        raise RuntimeError(f"{connector} is not in KScreen's output list")

    changed = False
    if scale and abs((ext.get("scale") or 1) - scale) > 0.001:
        _kscreen_doctor(f"output.{connector}.scale", f"{scale:g}")
        ext["scale"] = scale
        changed = True

    if others:
        anchor = next((o for o in others if o.get("primary")), None) or \
            min(others, key=lambda o: (o.get("pos", {}).get("x", 0),
                                       o.get("pos", {}).get("y", 0)))
        spot = gnome.place_beside(_logical_rect(anchor),
                                  [_logical_rect(o) for o in others],
                                  _logical_rect(ext)[2:], side)
        pos = ext.get("pos", {})
        if (pos.get("x"), pos.get("y")) != spot:
            _kscreen_doctor(f"output.{connector}.position",
                            f"{spot[0]},{spot[1]}")
            changed = True
    return changed


def _kscreen_doctor(key, value):
    """One kscreen-doctor setting ("output.NAME.key", "value")."""
    r = subprocess.run(["kscreen-doctor", key, value],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(f"kscreen-doctor {key} {value}: "
                           f"{(r.stderr or r.stdout).strip()}")


def map_touch_device(device_name, connector):
    """Bind the uinput touchscreen to our output via KWin's D-Bus property.

    KWin registers every libinput device at
    /org/kde/KWin/InputDevice/<sysName>; writing its outputName property
    makes KWin resolve the output's uuid and persist the mapping itself
    (kcminputrc), the same thing System Settings -> Touch Screen does.
    busctl ships with systemd, so no extra dependency.
    """
    sysname = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for path in glob.glob("/sys/class/input/event*/device/name"):
            try:
                with open(path) as f:
                    if f.read().strip() == device_name:
                        sysname = path.split("/")[3]
                        break
            except OSError:
                continue
        if sysname:
            break
        time.sleep(0.1)
    if not sysname:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["busctl", "--user", "set-property", "org.kde.KWin",
             f"/org/kde/KWin/InputDevice/{sysname}",
             "org.kde.KWin.InputDevice", "outputName", "s", connector],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True
        time.sleep(0.25)         # KWin has not registered the device yet
    return False


class KdeBackend:
    name = "kde"

    def __init__(self, args):
        self.a = args
        self.display = None           # pywayland Display
        self.manager = None           # zkde_screencast_unstable_v1 proxy
        self.stream = None            # zkde_screencast_stream_unstable_v1
        self.node = None
        self.proc = None
        self.connector = None
        self._place_lock = threading.Lock()

    @staticmethod
    def missing():
        miss = [t for t in ("gst-launch-1.0", "gst-inspect-1.0")
                if not shutil.which(t)]
        if pywayland_reason():
            miss.append("pywayland")
        if not shutil.which("kscreen-doctor"):
            miss.append("kscreen-doctor")
        if shutil.which("gst-inspect-1.0"):
            for el in ("pipewiresrc", "h264parse", "videoconvert"):
                if not gnome.gst_inspect(el):
                    miss.append(f"gstreamer:{el}")
            if not gnome.pick_encoder():
                miss.append("gstreamer:h264-encoder")
        return miss

    # ------------------------------------------------------------------ #
    # virtual output + PipeWire node (KWin zkde screencast protocol)
    # ------------------------------------------------------------------ #

    def setup(self):
        """Create the virtual output and get its PipeWire node id."""
        from pywayland.client import Display
        from protocols.zkde_screencast import ZkdeScreencastUnstableV1

        self.display = Display()
        self.display.connect()
        try:
            self._setup_stream()
        except Exception:
            self.teardown()
            raise

    def _setup_stream(self):
        registry = self.display.get_registry()
        found = {}

        def on_global(reg, name, interface, version):
            if interface == ZkdeScreencastUnstableV1.name:
                found["global"] = (name, version)

        registry.dispatcher["global"] = on_global
        self.display.roundtrip()
        if "global" not in found:
            raise RuntimeError(
                "KWin does not offer zkde_screencast_unstable_v1; "
                "a KDE Plasma (Wayland) session is required")
        gname, gver = found["global"]
        if gver < 2:
            raise RuntimeError(
                f"KWin's screencast protocol is version {gver}; virtual "
                "outputs need version 2 (Plasma 5.24+)")
        self.manager = registry.bind(
            gname, ZkdeScreencastUnstableV1, min(gver, 6))

        got = {}

        def on_created(stream, node):
            got["node"] = node

        def on_failed(stream, error):
            got["error"] = error

        a = self.a
        scale = a.scale or 1.0
        if gver >= 4:
            stream = self.manager.stream_virtual_output_with_description(
                OUTPUT_NAME, "Tandem second screen (phone)",
                a.width, a.height, scale, CURSOR_EMBEDDED)
        else:
            stream = self.manager.stream_virtual_output(
                OUTPUT_NAME, a.width, a.height, scale, CURSOR_EMBEDDED)
        stream.dispatcher["created"] = on_created
        stream.dispatcher["failed"] = on_failed
        self.stream = stream

        deadline = time.monotonic() + SETUP_TIMEOUT
        while not got and time.monotonic() < deadline:
            self.display.flush()
            self.display.dispatch(block=False)
            time.sleep(0.01)
        if "error" in got:
            raise RuntimeError(f"KWin refused the virtual output: "
                               f"{got['error']}")
        if "node" not in got:
            raise RuntimeError("KWin never announced the PipeWire stream")
        self.node = got["node"]
        log(f"kwin virtual monitor stream on pipewire node {self.node}")

    def create_touch_surface(self):
        """A uinput touchscreen mapped onto the virtual monitor."""
        problem = uinput_touch.uinput_access_problem()
        if problem:
            raise RuntimeError(
                problem + " (a one-time udev setup is needed; "
                "press t in the live view)")
        screen = uinput_touch.VirtualTouchscreen(self.a.width, self.a.height)
        connector = self.connector or find_connector()
        if not connector:
            log("warning: virtual output not in KScreen yet; "
                "touch is not mapped to it")
        elif map_touch_device(uinput_touch.DEVICE_NAME, connector):
            log(f"touch input: {uinput_touch.DEVICE_NAME} -> {connector}")
        else:
            log("warning: could not map the touch device onto "
                f"{connector}; map it in System Settings -> Touch Screen")
        return screen

    # ------------------------------------------------------------------ #
    # capture (GStreamer on the PipeWire node, as on GNOME)
    # ------------------------------------------------------------------ #

    def start_capture(self):
        """Start the encoder on our PipeWire node; returns its H.264 stream.

        Safe to call again after stop_capture() within one session: the
        virtual output belongs to setup()/teardown(), not to this.
        """
        a = self.a
        if a.codec != "h264":
            log("note: KDE backend encodes h264 only; ignoring codec "
                f"'{a.codec}'")
        enc = gnome.pick_encoder(getattr(a, "encoder", None))
        if not enc:
            raise RuntimeError("no usable GStreamer H.264 encoder found")
        self.stop_capture()                 # never two encoders on one node
        rfd, wfd = os.pipe()
        cmd = self._build_pipeline(self.node, gnome.bitrate_kbps(a.bitrate),
                                   a.gop, enc, fd=wfd,
                                   fps=getattr(a, "fps", 60) or 60)
        log(f"encoder {enc}; capture cmd: " + " ".join(cmd))
        try:
            # Own session: a Ctrl-C or hangup on the launching terminal
            # must not kill the encoder behind the daemon's back.
            self.proc = subprocess.Popen(cmd, stdout=wfd,
                                         stderr=subprocess.PIPE,
                                         start_new_session=True)
        except Exception:
            os.close(rfd)
            raise
        finally:
            os.close(wfd)          # our copy; EOF arrives when gst exits
        proc = self.proc
        threading.Thread(target=self._gst_log, args=(proc,),
                         daemon=True).start()
        threading.Thread(target=self._place_when_ready, args=(proc,),
                         daemon=True).start()
        return os.fdopen(rfd, "rb", buffering=0)

    @staticmethod
    def _build_pipeline(node, kbps, gop, enc, fd=3, fps=60):
        """gst-launch argv: PipeWire node -> H.264 Annex-B access units.

        No width/height caps: KWin fixes the stream's pixel size when the
        virtual output is created (logical size x scale), so forcing caps
        would break scaled outputs. The format is negotiated with the node.
        """
        desc = (f"{gnome.pipewiresrc(node)} ! video/x-raw "
                f"! {gnome.LEAKY_QUEUE} "
                f"! {gnome.encoder_chain(enc, kbps, gop, fps)} "
                f"! h264parse config-interval=-1 "
                f"! video/x-h264,stream-format=byte-stream,alignment=au "
                f"! fdsink fd={fd} sync=false")
        return gnome.gst_launch(desc)

    @staticmethod
    def _gst_log(proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").rstrip()
            if line:
                log("gst: " + line)

    def _place_when_ready(self, proc):
        """Wait for KScreen to add the output, then place it.

        Keeps watching for a few seconds after that: on a capture restart
        KWin may recreate the output at its default spot. place_connector
        sends nothing when the monitor is already in place, so re-checking
        is free of flicker.
        """
        deadline = time.monotonic() + PLACE_TIMEOUT
        settle_until = None
        error = None
        while time.monotonic() < deadline and proc.poll() is None:
            if settle_until and time.monotonic() > settle_until:
                return
            outputs = kscreen_outputs()
            connector = find_connector(outputs)
            if connector:
                if connector != self.connector:
                    self.connector = connector
                    log(f"virtual monitor appeared as {connector}")
                error = self._place(self.a.side, self.a.scale)
                if error is None:
                    settle_until = settle_until or time.monotonic() + 5
            time.sleep(0.25)
        if settle_until is None and proc.poll() is None:
            log("warning: could not place virtual monitor "
                f"({error or 'it never appeared in KScreen'})")

    def apply_live(self, side, scale, bus=None):
        if not self.connector:
            return False
        error = self._place(side, scale)
        if error:
            log(f"warning: could not place virtual monitor ({error})")
        return error is None

    def _place(self, side, scale):
        """place_connector under the lock; returns None or the error."""
        try:
            with self._place_lock:
                changed = place_connector(self.connector, side,
                                          scale or self.a.scale)
        except Exception as e:
            return e
        if changed:
            log(f"placed {self.connector} side={side} "
                f"scale={scale or self.a.scale}")
        return None

    def stop_capture(self):
        """Stop the encoder only (the virtual monitor stays). Idempotent."""
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def teardown(self):
        """Close the stream: KWin removes the monitor and rehomes windows."""
        if self.stream is not None:
            try:
                self.stream.close()     # destructor; output goes with it
                if self.display:
                    self.display.roundtrip()
            except Exception:
                pass
            self.stream = None
        if self.manager is not None:
            try:
                self.manager.destroy()
            except Exception:
                pass
            self.manager = None
        if self.display is not None:
            try:
                self.display.disconnect()
            except Exception:
                pass
            self.display = None
        log("closed kwin screencast stream (virtual monitor removed)")
