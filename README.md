# LanPad

Use an Android phone as a real touchpad for a Linux PC, over USB or Wi-Fi.

The phone runs a small app that turns its screen pitch black, keeps it on, and streams
multitouch frames to a daemon on the PC. The daemon replays them through a uinput
virtual touchpad, so libinput (sway, GNOME, KDE, …) treats the phone like a laptop
touchpad: pointer acceleration, tap-to-click, tap-and-drag, two-finger scroll, pinch and
three/four-finger swipes. Your compositor's touchpad settings apply to it as usual.

## Requirements

- PC: Linux, Python 3.10+, [python-evdev](https://pypi.org/project/evdev/), write access
  to `/dev/uinput` (logind grants it to the active seat user on most distros), `adb`
  (`android-tools`), a JDK to build the app.
- Phone: Android 8+, USB debugging enabled.

## Setup

```sh
./lanpad.py pair              # builds the app, installs it over USB, shares a random key
./lanpad.py install-service   # systemd user service, starts with the graphical session
```

`pair` downloads the Android build tools into `.sdk/` on first run (no Android Studio needed).
Hold the phone in landscape with its top on the left.

- **USB**: works whenever the phone is plugged in. The daemon keeps `adb reverse` set up
  across reconnects and opens the app when the phone appears.
- **Wi-Fi**: works when the cable is out; the app finds the PC by broadcast. If a firewall
  is active, allow the port once, e.g. `sudo ufw allow in on wlan0 to any port 47474 proto udp`.

Logs: `journalctl --user -u lanpad.service -f`

## Options

`--diagonal INCHES` (serve / install-service): the phone's screen size, so libinput knows the
pad's physical size for acceleration and gesture distances (default 5.0).
`--no-usb`: Wi-Fi only. `--port`: UDP/TCP port (default 47474).

## How it works

- `android/`: a single-activity app built with `android/build.sh` (javac → d8 → aapt2 →
  apksigner, no Gradle). Every finger-down gets a fresh track id; frames are repeated
  every 40 ms while fingers rest so the PC can tell a resting finger from packet loss.
- `lanpad.py`: authenticates packets (HMAC-SHA256, truncated, with per-session sequence
  numbers against replay), converts frames to evdev multitouch protocol B, and smooths
  each finger with a [1€ filter](https://gery.casiez.net/1euro/). Cheap touch panels
  report coarse, "sticky" positions; without smoothing a slow drag arrives as stalls plus
  jumps, which libinput accelerates like flicks and precision is lost.

Wire format (big endian): `"LPD1" | type:u8 | body | mac[16]`, see `MainActivity.java`.
Over USB each packet is prefixed with a u16 length.
