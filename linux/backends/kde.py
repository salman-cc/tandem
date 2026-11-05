# SPDX-License-Identifier: GPL-3.0-only
#
# kde.py - KDE Plasma (KWin, Wayland) backend.
#
# Author: Muhammed Salman
#
# Virtual monitor + capture: xdg-desktop-portal's ScreenCast portal with
# the Virtual source type (portal version 4+), which KDE implements in
# xdg-desktop-portal-kde (Plasma 6). The portal shows a one-time consent
# dialog, creates the virtual output through KWin and hands over a
# PipeWire node; GStreamer turns the node into H.264 the same way as on
# GNOME:
#
#     portal ScreenCast (Virtual) -> PipeWire node
#         -> pipewiresrc -> VA-API / x264 / openh264
#         -> raw Annex-B H.264 on a pipe -> tandemd's parser
#
# Why the portal and not KWin's zkde_screencast_unstable_v1 protocol
# directly: KWin blacklists that protocol for every client whose
# executable doesn't match a .desktop file declaring
# X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1 (KWin's
# wayland_server.cpp allowInterface), so a plain python3 process never
# even sees the global. The portal is the sanctioned path and needs no
# privileged protocol.
#
# Consent: the first run shows KWin's screen sharing dialog; the session
# is persisted (persist_mode=until-revoked), so later runs reuse the
# restore data and skip the dialog. The portal session dies with this
# process, so a stopped or killed daemon leaves no monitor behind.
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

import glob
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import uinput_touch

from . import gnome

try:
    from gi.repository import Gio, GLib
except Exception:              # reported by missing()
    Gio = GLib = None

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

SOURCE_VIRTUAL = 4             # ScreenCast source type: virtual monitor
CURSOR_EMBEDDED = 2            # draw the pointer into the frames
PERSIST_UNTIL_REVOKED = 2

PORTAL_CALL_TIMEOUT_MS = 15000
DIALOG_TIMEOUT_S = 240         # waiting for the user at the consent dialog

STATE_DIR = os.path.expanduser("~/.config/tandem")
RESTORE_FILE = os.path.join(STATE_DIR, "kde-portal-restore")

# The output we ask the portal for is named "Tandem"; KWin prefixes the
# connector name with "Virtual-". The reliable discovery is the
# before/after diff in find_connector (portal-created names vary).
OUTPUT_NAME = "Tandem"
CONNECTOR_PREFIX = "Virtual-"

# How long to wait for KScreen to list the new output after the stream
# starts.
PLACE_TIMEOUT = 15

# Session and request object paths must be unique on the portal's bus.
_serial = itertools.count(1)


def log(*a):
    try:                       # never raise on a closed terminal / TUI pipe
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def is_kde_wayland():
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "KDE" in desk and bool(os.environ.get("WAYLAND_DISPLAY"))


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def kscreen_outputs():
    """KScreen's outputs as dicts, or [] when kscreen-doctor can't say."""
    try:
        r = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode != 0:
            log(f"kscreen-doctor -j failed: {(r.stderr or r.stdout).strip()}")
            return []
        return json.loads(r.stdout).get("outputs", [])
    except Exception as e:
        log(f"kscreen-doctor -j failed: {e}")
        return []


def find_connector(outputs=None, pre=None):
    """Our virtual output's KScreen name, or None.

    Fast path: the name we asked for ("Virtual-Tandem"). The portal may
    name its output differently, so otherwise take the enabled output that
    appeared since `pre` (the snapshot taken before setup).
    """
    if outputs is None:
        outputs = kscreen_outputs()
    names = [o.get("name", "") for o in outputs if o.get("enabled", True)]
    exact = CONNECTOR_PREFIX + OUTPUT_NAME
    if exact in names:
        return exact
    named = next((n for n in names if OUTPUT_NAME in n), None)
    if named:
        return named
    if pre is not None:
        new = [n for n in names if n not in pre]
        if new:
            return new[0]
    return None


def _is_primary(o):
    """Plasma 6's kscreen-doctor JSON has no "primary" flag: the primary
    output is the one with priority 1 (every other output is 0). Plasma 5
    used "primary": true. Accept both."""
    if o.get("primary"):
        return True
    return o.get("priority", 0) == 1


def primary_monitor_desc():
    """One-line description of the primary display, for the TUI."""
    for o in kscreen_outputs():
        if _is_primary(o):
            size = o.get("size", {})
            return (f"{o.get('name', '?')}  "
                    f"{size.get('width', '?')}x{size.get('height', '?')}"
                    f"  scale {o.get('scale', 1):g}")
    return ""


def _logical_rect(o):
    """(x, y, w, h) of a KScreen output in logical pixels.

    KScreen 6 (Plasma 6) works in logical coordinates throughout: "pos"
    and "size" are already logical, so no division by scale."""
    pos = o.get("pos", {})
    size = o.get("size", {})
    return (pos.get("x", 0), pos.get("y", 0),
            size.get("width", 0), size.get("height", 0))


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
        anchor = next((o for o in others if _is_primary(o)), None) or \
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


def _load_restore_data():
    """A saved portal restore token (opaque (suv) variant), or None."""
    try:
        with open(RESTORE_FILE) as f:
            text = f.read().strip()
        if not text:
            return None
        return GLib.Variant.parse(GLib.VariantType("(suv)"), text,
                                  None, None)
    except Exception:
        return None


def _save_restore_data(variant):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(RESTORE_FILE, "w") as f:
            f.write(variant.print_(True))
    except Exception:
        pass


def _v(x):
    """Unbox a GLib.Variant from an a{sv} dict; pass scalars through."""
    return x.unpack() if isinstance(x, GLib.Variant) else x


class PortalVirtualMonitor:
    """An org.freedesktop.portal.ScreenCast virtual-monitor session.

    `node` is its PipeWire node id (and `serial` its PipeWire object
    serial when the portal reports one); close() ends the sharing and the
    virtual monitor goes with it.
    """

    def __init__(self):
        self.bus = _bus()
        self.session = None
        self.node = None
        self.serial = None
        self._open()

    def _request(self, method, extra_args, timeout_s=DIALOG_TIMEOUT_S):
        """One portal request/response round trip.

        The request object path is predicted from our unique bus name plus
        a fresh token (that's the spec's scheme), so the Response signal
        subscription is in place before the call goes out.
        """
        token = f"tandem{os.getpid()}_{next(_serial)}"
        sender = self.bus.get_unique_name().lstrip(":").replace(".", "_")
        handle = f"{PORTAL_PATH}/request/{sender}/{token}"
        opts = dict(extra_args)
        opts["handle_token"] = GLib.Variant("s", token)
        got = {}
        loop = GLib.MainLoop()

        def on_response(conn, sender_name, path, iface, signal, params):
            got["code"], got["results"] = params.unpack()
            loop.quit()

        sub = self.bus.signal_subscribe(
            PORTAL_BUS, REQUEST_IFACE, "Response", handle, None,
            Gio.DBusSignalFlags.NONE, on_response)
        try:
            call_args = tuple(a if m != "options" else opts
                              for m, a in method["args"])
            self.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, SCREENCAST_IFACE, method["name"],
                GLib.Variant(method["sig"], call_args),
                GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                PORTAL_CALL_TIMEOUT_MS, None)

            def on_timeout():
                got["timeout"] = True
                loop.quit()
                return False

            GLib.timeout_add(timeout_s * 1000, on_timeout)
            loop.run()
        finally:
            self.bus.signal_unsubscribe(sub)
        if "timeout" in got:
            raise RuntimeError(
                f"no answer from the screen sharing dialog within "
                f"{timeout_s}s; run tandem again and approve the dialog")
        if got.get("code") != 0:
            raise RuntimeError(
                f"portal {method['name']} was refused or cancelled "
                f"(response code {got.get('code')})")
        return got["results"]

    def _open(self):
        version, sources = self._portal_version()
        log(f"[kde] xdg-desktop-portal ScreenCast version {version} "
            f"(source types {sources})")
        if version < 4 or not (sources & SOURCE_VIRTUAL):
            raise RuntimeError(
                "this xdg-desktop-portal cannot create virtual monitors "
                "(needs ScreenCast portal version 4 with the Virtual "
                "source; KDE Plasma 6 provides it via "
                "xdg-desktop-portal-kde)")
        session_token = f"tandem{os.getpid()}_{next(_serial)}"
        results = self._request({
            "name": "CreateSession", "sig": "(a{sv})",
            "args": [("options", None)],
        }, {"session_handle_token": GLib.Variant("s", session_token)})
        self.session = _v(results.get("session_handle"))
        if not self.session:
            raise RuntimeError("the portal returned no session handle")

        opts = {
            "types": GLib.Variant("u", SOURCE_VIRTUAL),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        restore = _load_restore_data()
        if restore is not None:
            opts["restore_data"] = restore
        self._request({
            "name": "SelectSources", "sig": "(oa{sv})",
            "args": [("session_handle", self.session), ("options", None)],
        }, opts)
        log("[kde] asked the portal for a virtual monitor; approve the "
            "screen sharing dialog if it appears (once, it is remembered)")
        results = self._request({
            "name": "Start", "sig": "(osa{sv})",
            "args": [("session_handle", self.session), ("", ""),
                     ("options", None)],
        }, {})
        streams = _v(results.get("streams")) or []
        if not streams:
            self.close()
            raise RuntimeError(
                "the portal gave no PipeWire stream for the virtual "
                "monitor")
        self.node, props = streams[0][0], streams[0][1]
        self.serial = _v(props.get("pipewire-serial")) if props else None
        if self.serial is not None and not isinstance(self.serial, int):
            self.serial = None
        rd = results.get("restore_data")
        if rd is not None:
            _save_restore_data(rd)

    def _portal_version(self):
        props = self.bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties",
            "GetAll", GLib.Variant("(s)", (SCREENCAST_IFACE,)),
            GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE,
            PORTAL_CALL_TIMEOUT_MS, None).unpack()[0]
        return _v(props.get("version") or 0), \
            _v(props.get("AvailableSourceTypes") or 0)

    def close(self):
        if self.session:
            try:
                self.bus.call_sync(
                    PORTAL_BUS, self.session, SESSION_IFACE, "Close",
                    None, None, Gio.DBusCallFlags.NONE, 3000, None)
            except Exception:
                pass               # already gone
            self.session = None


class KdeBackend:
    name = "kde"

    def __init__(self, args):
        self.a = args
        self.portal = None            # PortalVirtualMonitor
        self.node = None              # PipeWire node id
        self.serial = None            # PipeWire object serial (portal v6+)
        self.proc = None
        self.connector = None
        self._pre_outputs = None      # KScreen names before the output
        self._place_lock = threading.Lock()

    @staticmethod
    def missing():
        miss = [t for t in ("gst-launch-1.0", "gst-inspect-1.0")
                if not shutil.which(t)]
        if Gio is None:
            miss.append("python3-gobject")
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
    # virtual output + PipeWire node (xdg-desktop-portal ScreenCast)
    # ------------------------------------------------------------------ #

    def setup(self):
        """Create the virtual monitor through the portal; remember its
        PipeWire node and the KScreen snapshot needed to find it later."""
        if Gio is None:
            raise RuntimeError(
                "python3-gobject is not installed; on Fedora install it "
                "with: sudo dnf install python3-gobject")
        self._pre_outputs = {o.get("name", "") for o in kscreen_outputs()}
        self.portal = PortalVirtualMonitor()
        self.node = self.portal.node
        self.serial = self.portal.serial
        log(f"[kde] the portal is sharing the virtual monitor; PipeWire "
            f"node {self.node} (object serial {self.serial})")

    def create_touch_surface(self):
        """A uinput touchscreen mapped onto the virtual monitor."""
        problem = uinput_touch.uinput_access_problem()
        if problem:
            raise RuntimeError(
                problem + " (a one-time udev setup is needed; "
                "press t in the live view)")
        screen = uinput_touch.VirtualTouchscreen(self.a.width, self.a.height)
        connector = self.connector or find_connector(pre=self._pre_outputs)
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
        cmd = self._build_pipeline(self.node, self.serial,
                                   gnome.bitrate_kbps(a.bitrate),
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
    def _pipewiresrc(node, serial):
        """pipewiresrc for our stream: by the PipeWire object serial when
        the portal reported one (portal v6+; node ids can be reused),
        else by node id."""
        if serial is not None:
            src = f"pipewiresrc target-object={serial} do-timestamp=true"
        else:
            src = f"pipewiresrc path={node} do-timestamp=true"
        if gnome.has_prop("pipewiresrc", "keepalive-time"):
            src += " keepalive-time=1000"
        return src

    @classmethod
    def _build_pipeline(cls, node, serial, kbps, gop, enc, fd=3, fps=60):
        """gst-launch argv: PipeWire stream -> H.264 Annex-B access units.

        No width/height caps: the portal fixes the stream's pixel size
        when the virtual output is created, so forcing caps would break
        scaled outputs. The format is negotiated with the node.
        """
        desc = (f"{cls._pipewiresrc(node, serial)} ! video/x-raw "
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
            connector = find_connector(outputs, pre=self._pre_outputs)
            if connector:
                if connector != self.connector:
                    self.connector = connector
                error = self._place(self.a.side, self.a.scale)
                if error is None:
                    if settle_until is None:
                        settle_until = time.monotonic() + 3
            time.sleep(0.5)

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
        """Close the portal session: the sharing ends and KWin removes
        the virtual monitor."""
        if self.portal is not None:
            self.portal.close()
            self.portal = None
        log("closed the portal session (virtual monitor removed)")
