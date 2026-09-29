#!/usr/bin/env python3
"""LanPad: use an Android phone as a real touchpad for this PC, over USB or the LAN.

The phone runs the LanPad app (android/): a pitch-black, always-on, full-screen
touch surface that streams multitouch frames over USB (adb reverse) or Wi-Fi (UDP).
This daemon authenticates them (HMAC-SHA256 with a key shared at pairing) and replays
them through a uinput virtual touchpad, so libinput/sway treat the phone exactly like
a laptop touchpad: acceleration, tap-to-click, two-finger scroll, gestures.

  lanpad.py pair             build+install the app via adb (USB) and share the key
  lanpad.py serve            run the daemon (what the systemd service runs)
  lanpad.py install-service  run the daemon automatically in every graphical session
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import math
import os
import re
import secrets
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e

HERE = Path(__file__).resolve().parent
APK = HERE / "lanpad.apk"
APP_PACKAGE = "dev.lanpad"
APP_ACTIVITY = f"{APP_PACKAGE}/.MainActivity"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lanpad"
KEY_FILE = CONFIG_DIR / "key"
UNIT_NAME = "lanpad.service"
DEFAULT_PORT = 47474

DEVICE_NAME = "LanPad"
MAX_SLOTS = 5
TOOL_KEYS = [e.BTN_TOOL_FINGER, e.BTN_TOOL_DOUBLETAP, e.BTN_TOOL_TRIPLETAP,
             e.BTN_TOOL_QUADTAP, e.BTN_TOOL_QUINTTAP]

# Wire protocol; must match android/src/dev/lanpad/MainActivity.java.
MAGIC = b"LPD1"
HELLO, WELCOME, FRAME, RESYNC = 1, 2, 3, 4
MAC_LEN = 16
# The app repeats the touch state every 40 ms while fingers are down; silence
# longer than this means packets are being lost, so lift the fingers.
TOUCH_TIMEOUT = 0.25
MAX_SESSIONS = 4


# ---------------------------------------------------------------- touchpad

class _UInput(UInput):
    def _find_device(self, fd):
        # python-evdev opens the new /dev/input node for `.device`, which we never use;
        # without `input` group access that retries for 2 s and blocks the daemon.
        return None


class OneEuro2D:
    """1€ filter (Casiez et al., CHI 2012) on a 2-D point: the cutoff frequency rises with
    speed, so slow movements are smoothed heavily and fast ones pass with little lag.

    Needed because the phone's panel reports whole ~0.12 mm units and holds still until the
    finger has moved a unit or more; a slow drag arrives as runs of zero motion followed by
    single-frame jumps, which libinput reads as fast flicks and accelerates."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.pos: tuple[float, float] | None = None
        self.vel = (0.0, 0.0)
        self.t = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        return 1.0 / (1.0 + 1.0 / (2 * math.pi * cutoff * dt))

    def __call__(self, x: float, y: float, t: float) -> tuple[float, float]:
        if self.pos is None:
            self.pos, self.t = (x, y), t
            return self.pos
        dt = max(t - self.t, 1e-3)
        self.t = t
        px, py = self.pos
        ad = self._alpha(self.d_cutoff, dt)
        vx = self.vel[0] + ad * ((x - px) / dt - self.vel[0])
        vy = self.vel[1] + ad * ((y - py) / dt - self.vel[1])
        self.vel = (vx, vy)
        a = self._alpha(self.min_cutoff + self.beta * math.hypot(vx, vy), dt)
        self.pos = (px + a * (x - px), py + a * (y - py))
        return self.pos


class SpikeGate:
    """Rejects short single-axis glitches. The phone's panel occasionally reports one axis
    hundreds of pixels off for 1-2 frames while the other axis tracks normally; replayed
    as motion, that teleports the cursor. A finger moves continuously within one track, so
    a sample far from the velocity prediction is held back (the prediction is used instead)
    for up to SPIKE_MAX_FRAMES frames; if it persists longer it is accepted as real."""

    def __init__(self) -> None:
        self.good: float | None = None
        self.t = 0.0
        self.vel = 0.0
        self.suspect = 0

    def __call__(self, v: float, t: float) -> float:
        if self.good is None:
            self.good, self.t = v, t
            return v
        dt = max(t - self.t, 1e-3)
        expected = self.good + self.vel * dt
        if abs(v - expected) > SPIKE_PX + 0.5 * abs(self.vel * dt) and self.suspect < SPIKE_MAX_FRAMES:
            self.suspect += 1
            return expected
        self.suspect = 0
        self.vel = 0.5 * self.vel + 0.5 * (v - self.good) / dt
        self.good, self.t = v, t
        return v


class FingerFilter:
    """Per-finger input cleanup: glitch rejection per axis, then 1€ smoothing."""

    def __init__(self) -> None:
        self.gx, self.gy = SpikeGate(), SpikeGate()
        self.smooth = OneEuro2D(SMOOTH_MIN_CUTOFF, SMOOTH_BETA)

    def __call__(self, x: int, y: int, t: float) -> tuple[float, float]:
        return self.smooth(self.gx(x, t), self.gy(y, t), t)


# Output units per phone pixel, so smoothed sub-pixel positions survive rounding.
SUBUNITS = 8
# 1€ tuning, in phone pixels (~0.12 mm): 1 Hz cutoff when still, +0.02 Hz per px/s of speed.
SMOOTH_MIN_CUTOFF = 1.0
SMOOTH_BETA = 0.02
# Glitch rejection: deviation from the predicted position (px, ~7 mm) beyond which a
# sample is suspect, and how many consecutive suspect frames are held back at most.
SPIKE_PX = 60
SPIKE_MAX_FRAMES = 2


class Touchpad:
    """uinput multitouch (protocol B) touchpad fed with whole frames of {track id: (x, y)}."""

    def __init__(self, width: int, height: int, diagonal_in: float):
        self.size = (width, height)
        mm_per_px = diagonal_in * 25.4 / math.hypot(width, height)
        res = max(1, round(SUBUNITS / mm_per_px))
        xmax, ymax = (width - 1) * SUBUNITS, (height - 1) * SUBUNITS
        caps = {
            e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_TOUCH, *TOOL_KEYS],
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, 0, xmax, 0, 0, res)),
                (e.ABS_Y, AbsInfo(0, 0, ymax, 0, 0, res)),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, MAX_SLOTS - 1, 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, AbsInfo(0, 0, xmax, 0, 0, res)),
                (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, ymax, 0, 0, res)),
            ],
        }
        self.ui = _UInput(caps, name=DEVICE_NAME, vendor=0x1bbb, product=0x7470, version=1,
                          bustype=e.BUS_VIRTUAL, input_props=[e.INPUT_PROP_POINTER])
        self.slots: list[tuple[int, int, int] | None] = [None] * MAX_SLOTS  # (track, out x, out y)
        self.filters: dict[int, FingerFilter] = {}
        self.cur_slot = 0
        self.next_tid = 0
        self.ntouch = 0

    def frame(self, contacts: dict[int, tuple[int, int]], now: float) -> None:
        ui = self.ui

        def select(si: int) -> None:
            if self.cur_slot != si:
                ui.write(e.EV_ABS, e.ABS_MT_SLOT, si)
                self.cur_slot = si

        for si, s in enumerate(self.slots):
            if s is not None and s[0] not in contacts:
                select(si)
                ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
                self.slots[si] = None
                self.filters.pop(s[0], None)
        slot_of = {s[0]: si for si, s in enumerate(self.slots) if s is not None}
        for track, (x, y) in contacts.items():
            si = slot_of.get(track)
            if si is None:
                free = [i for i, s in enumerate(self.slots) if s is None]
                if not free:
                    continue
                si = free[0]
                select(si)
                ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, self.next_tid)
                self.next_tid = (self.next_tid + 1) & 0xFFFF
                self.filters[track] = FingerFilter()
            fx, fy = self.filters[track](x, y, now)
            ox, oy = round(fx * SUBUNITS), round(fy * SUBUNITS)
            if self.slots[si] is not None and self.slots[si][1:] == (ox, oy):
                continue
            select(si)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, ox)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, oy)
            self.slots[si] = (track, ox, oy)

        active = [s for s in self.slots if s is not None]
        n = len(active)
        if active:
            ui.write(e.EV_ABS, e.ABS_X, active[0][1])
            ui.write(e.EV_ABS, e.ABS_Y, active[0][2])
        if n != self.ntouch:
            if (n > 0) != (self.ntouch > 0):
                ui.write(e.EV_KEY, e.BTN_TOUCH, int(n > 0))
            if self.ntouch:
                ui.write(e.EV_KEY, TOOL_KEYS[min(self.ntouch, len(TOOL_KEYS)) - 1], 0)
            if n:
                ui.write(e.EV_KEY, TOOL_KEYS[min(n, len(TOOL_KEYS)) - 1], 1)
            self.ntouch = n
        ui.syn()

    def close(self) -> None:
        self.frame({}, time.monotonic())
        self.ui.close()


# ---------------------------------------------------------------- daemon

def load_key() -> bytes:
    try:
        return bytes.fromhex(KEY_FILE.read_text().strip())
    except FileNotFoundError:
        sys.exit(f"no pairing key at {KEY_FILE}; run: {sys.argv[0]} pair")


class Server:
    """Accepts the app's packets over Wi-Fi (UDP, any interface) and over USB (TCP on
    loopback, reached by the phone through `adb reverse`; TCP frames are u16-length prefixed)."""

    def __init__(self, key: bytes, port: int, diagonal: float):
        self.key = key
        self.diagonal = diagonal
        self.sel = selectors.DefaultSelector()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", port))
        self.sel.register(self.udp, selectors.EVENT_READ, "udp")
        self.listener = socket.create_server(("127.0.0.1", port))
        self.sel.register(self.listener, selectors.EVENT_READ, "listen")
        self.tcp_bufs: dict[socket.socket, bytearray] = {}
        self.sessions: dict[bytes, list[int]] = {}  # session -> [last seq]; insertion-ordered
        self.pad: Touchpad | None = None
        self.last_frame = 0.0
        self.peer: str | None = None
        self.last_packet = 0.0  # any authenticated packet, heartbeats included

    def mac(self, data: bytes) -> bytes:
        return hmac.new(self.key, data, hashlib.sha256).digest()[:MAC_LEN]

    def seal(self, ptype: int, body: bytes) -> bytes:
        pkt = MAGIC + bytes([ptype]) + body
        return pkt + self.mac(pkt)

    def serve(self) -> None:
        while True:
            for key, _ in self.sel.select(timeout=0.05):
                now = time.monotonic()
                if key.data == "udp":
                    data, addr = self.udp.recvfrom(1024)
                    self.handle(data, f"Wi-Fi {addr[0]}", lambda p, a=addr: self.udp.sendto(p, a), now)
                elif key.data == "listen":
                    conn, _ = self.listener.accept()
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.setblocking(False)
                    self.tcp_bufs[conn] = bytearray()
                    self.sel.register(conn, selectors.EVENT_READ, "tcp")
                else:
                    self.read_tcp(key.fileobj, now)
            if self.pad and self.pad.ntouch and time.monotonic() - self.last_frame > TOUCH_TIMEOUT:
                self.pad.frame({}, time.monotonic())

    def read_tcp(self, conn: socket.socket, now: float) -> None:
        try:
            chunk = conn.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self.sel.unregister(conn)
            del self.tcp_bufs[conn]
            conn.close()
            return
        buf = self.tcp_bufs[conn]
        buf += chunk

        def send(p: bytes) -> None:
            try:
                conn.sendall(struct.pack(">H", len(p)) + p)
            except OSError:
                pass

        while len(buf) >= 2:
            n = struct.unpack_from(">H", buf)[0]
            if len(buf) < 2 + n:
                break
            pkt = bytes(buf[2:2 + n])
            del buf[:2 + n]
            self.handle(pkt, "USB", send, now)

    def handle(self, data: bytes, via: str, reply, now: float) -> None:
        if len(data) < 5 + MAC_LEN or data[:4] != MAGIC:
            return
        body, tag = data[:-MAC_LEN], data[-MAC_LEN:]
        if not hmac.compare_digest(self.mac(body), tag):
            return
        self.last_packet = now
        ptype, body = body[4], body[5:]
        if ptype == HELLO and len(body) == 20:
            nonce, sess, w, h = struct.unpack(">8s8sHH", body)
            if sess not in self.sessions:
                sess = secrets.token_bytes(8)
                self.sessions[sess] = [-1]
                while len(self.sessions) > MAX_SESSIONS:
                    del self.sessions[next(iter(self.sessions))]
            if self.pad is None or self.pad.size != (w, h):
                if self.pad:
                    self.pad.close()
                self.pad = Touchpad(w, h, self.diagonal)
                print(f"touchpad '{DEVICE_NAME}' ready: {w}x{h}")
            if self.peer != via:
                self.peer = via
                print(f"phone connected via {via}")
            reply(self.seal(WELCOME, nonce + sess))
        elif ptype == FRAME and len(body) >= 13:
            sess, seq, n = struct.unpack(">8sIB", body[:13])
            st = self.sessions.get(sess)
            if st is None:
                reply(self.seal(RESYNC, sess))
                return
            if seq <= st[0] or len(body) != 13 + 5 * n or self.pad is None:
                return  # replayed / reordered / malformed
            st[0] = seq
            contacts = {tid: (x, y) for tid, x, y in struct.iter_unpack(">BHH", body[13:])}
            self.last_frame = now
            self.pad.frame(contacts, now)

    def close(self) -> None:
        if self.pad:
            self.pad.close()
        for conn in self.tcp_bufs:
            conn.close()
        self.listener.close()
        self.udp.close()


def usb_link_loop(adb: str, port: int, idle_for) -> None:
    """For every USB-attached phone: keep `adb reverse tcp:PORT` set (the mapping is lost on
    each reconnect), start the app when it isn't running, and wake the phone if it went to
    sleep while LanPad was in front (a grip easily presses the power button; an app cannot
    block that). Leaving the app (Home/Back) before turning the screen off opts out."""
    linked: set[str] = set()
    last_wake: dict[str, float] = {}

    def run(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    while True:
        try:
            out = run(adb, "devices").stdout
            serials = {ln.split()[0] for ln in out.splitlines()[1:] if ln.endswith("\tdevice")}
            for serial in serials:
                a = (adb, "-s", serial)
                if f"tcp:{port}" not in run(*a, "reverse", "--list").stdout:
                    if run(*a, "reverse", f"tcp:{port}", f"tcp:{port}").returncode != 0:
                        continue
                    if serial not in linked:
                        print(f"USB: linked {serial}")
                        linked.add(serial)
                    if not run(*a, "shell", "pidof", APP_PACKAGE).stdout.strip():
                        run(*a, "shell", "am", "start", "-n", APP_ACTIVITY)
                # The app heartbeats every 2 s while connected; silence means it is not running.
                if idle_for() > 3 and time.monotonic() - last_wake.get(serial, 0.0) > 5:
                    awake = "Awake" in run(*a, "shell", "dumpsys power | grep mWakefulness=").stdout
                    if not awake and APP_PACKAGE in run(*a, "shell", "dumpsys window | grep mFocusedApp=").stdout:
                        run(*a, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
                        last_wake[serial] = time.monotonic()
                        print(f"USB: {serial} went to sleep with LanPad in front; woke it")
        except (subprocess.SubprocessError, OSError) as ex:
            print(f"USB: adb error: {ex}")
        time.sleep(1)


def cmd_serve(args) -> None:
    sys.stdout.reconfigure(line_buffering=True)
    server = Server(load_key(), args.port, args.diagonal)

    def on_signal(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    adb = find_adb(required=False) if not args.no_usb else None
    if adb:
        threading.Thread(target=usb_link_loop, daemon=True,
                         args=(adb, args.port, lambda: time.monotonic() - server.last_packet)).start()
    print(f"listening: Wi-Fi udp/{args.port}" + (", USB via adb reverse" if adb else "")
          + "; open LanPad on the phone")
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


# ---------------------------------------------------------------- pairing

def find_adb(required: bool = True) -> str | None:
    for cand in (os.environ.get("ADB"), shutil.which("adb"), str(HERE / "platform-tools" / "adb")):
        if cand and os.access(cand, os.X_OK):
            return cand
    if required:
        sys.exit("adb not found: install android-tools, or set ADB=/path/to/adb")
    return None


def cmd_pair(args) -> None:
    adb = [find_adb()] + (["-s", args.serial] if args.serial else [])
    state = subprocess.run(adb + ["get-state"], capture_output=True, text=True)
    if state.stdout.strip() != "device":
        sys.exit(f"phone not ready over adb: {(state.stderr or state.stdout).strip()} "
                 "(plug in USB, enable USB debugging, accept the prompt)")
    if not APK.exists() or args.rebuild:
        subprocess.run([str(HERE / "android" / "build.sh")], check=True)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.touch(mode=0o600)
        KEY_FILE.write_text(secrets.token_hex(32) + "\n")
    key_hex = KEY_FILE.read_text().strip()

    # The PC address the phone should try first; broadcast discovery covers IP changes.
    wlan = subprocess.run(adb + ["shell", "ip -4 addr show wlan0"], capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", wlan)
    host, iface = None, "wlan0"
    if m:
        route = subprocess.run(["ip", "-4", "route", "get", m.group(1)], capture_output=True, text=True).stdout
        src, dev = re.search(r"\bsrc (\S+)", route), re.search(r"\bdev (\S+)", route)
        host = src.group(1) if src else None
        iface = dev.group(1) if dev else iface
    else:
        print("warning: phone is not on Wi-Fi; connect it to the same network as this PC")

    print("installing app...")
    subprocess.run(adb + ["install", "-r", str(APK)], check=True, stdout=subprocess.DEVNULL)
    start = ["shell", "am", "start", "-n", APP_ACTIVITY, "--es", "key", key_hex, "--ei", "port", str(args.port)]
    if host:
        start += ["--es", "host", host]
    subprocess.run(adb + start, check=True, stdout=subprocess.DEVNULL)
    print(f"paired (PC {host or 'found via discovery'}, port {args.port}).\n"
          f"USB: works whenever the phone is plugged in and the daemon runs.")
    if shutil.which("ufw"):
        print(f"Wi-Fi: ufw is installed; if it is active, allow the port once:\n"
              f"  sudo ufw allow in on {iface} to any port {args.port} proto udp")


# ---------------------------------------------------------------- service

def cmd_install_service(args) -> None:
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_NAME).write_text(f"""[Unit]
Description=LanPad daemon (phone touchscreen as a touchpad over USB/LAN)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={sys.executable} {Path(__file__).resolve()} --port {args.port} serve --diagonal {args.diagonal}{" --no-usb" if args.no_usb else ""}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT_NAME], check=True)
    print(f"installed and started {UNIT_NAME}; logs: journalctl --user -u {UNIT_NAME} -f")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default {DEFAULT_PORT})")
    sub = ap.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="run the daemon (default)")
    p_pair = sub.add_parser("pair", help="install the app on the USB-connected phone and pair it")
    p_pair.add_argument("-s", "--serial", help="adb serial if several phones are connected")
    p_pair.add_argument("--rebuild", action="store_true", help="rebuild the APK first")
    p_svc = sub.add_parser("install-service", help="start the daemon with every graphical session")
    for p in (p_serve, p_svc):
        p.add_argument("--diagonal", type=float, default=5.0,
                       help="phone screen diagonal in inches; sets the physical size libinput uses "
                            "for acceleration and gestures (default 5.0)")
        p.add_argument("--no-usb", action="store_true",
                       help="Wi-Fi only: don't manage `adb reverse` for USB-connected phones")
    argv = sys.argv[1:]
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args([*argv, "serve"])
    {"serve": cmd_serve, "pair": cmd_pair, "install-service": cmd_install_service}[args.cmd](args)


if __name__ == "__main__":
    main()
