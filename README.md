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
  across reconnects and opens the app when the phone appears. If the phone goes to sleep
  while LanPad is in front (a grip easily presses the power button), the daemon wakes it
  and LanPad reappears over the lock screen; to turn the phone off on purpose, leave the
  app (Home/Back) first.
- **Wi-Fi**: works when the cable is out; the app finds the PC by broadcast. If a firewall
  is active, allow the port once, e.g. `sudo ufw allow in on wlan0 to any port 47474 proto udp`.

Logs: `journalctl --user -u lanpad.service -f`

### Android SDK pieces

The SDK is not part of this repository. `android/build.sh` (run by `pair`) needs only two
packages from Google's repository and fetches them into `.sdk/` (about 150 MB unpacked):

- `https://dl.google.com/android/repository/build-tools_r37_linux.zip` → `.sdk/android-37.0/`
  (`aapt`, `aapt2`, `d8`, `zipalign`, `apksigner`)
- `https://dl.google.com/android/repository/platform-30_r03.zip` → `.sdk/android-11/android.jar`

To use build-tools and a platform jar you already have, set `ANDROID_BUILD_TOOLS=<dir>` and
`ANDROID_JAR=<path>/android.jar`; then nothing is downloaded. The APK is signed with the
standard Android debug key (`~/.android/debug.keystore`, created if missing).

## Following kbfollow between machines

If a [kbfollow](https://github.com/seros-cimenstein/lan-tools/tree/main/kbfollow) sender
(formerly called lanmouse) runs on this PC, LanPad follows its keyboard-driven target (read
from `~/.config/kbfollow-indicator/config.json`: `status_url`, `token`):

- target `local`: the phone is this PC's native touchpad, as above;
- target is a receiver (`laptop`, `desktop`, …): the phone drives a relative mouse,
  `LanPad pointer`, which kbfollow grabs and forwards over its existing connection. The sender's
  `mouse_match` must list it, e.g. `["Logitech M215", "LanPad pointer"]`.

kbfollow only carries mouse events, so on a receiver LanPad interprets gestures itself, with
libinput's conventions: move, tap = left click, two-finger tap = right, three-finger tap =
middle, double-tap, tap-and-drag, two-finger (natural) scrolling. Pinch and three/four-finger
swipes stay local-only.

## Options

`--diagonal INCHES` (serve / install-service): the phone's screen size, so libinput knows the
pad's physical size for acceleration and gesture distances (default 5.0).
`--remote-gain` (default 0.5) and `--[no-]remote-natural-scroll`: pointer speed and scroll
direction on kbfollow receivers. `--kbfollow PATH` / `--no-kbfollow`: kbfollow integration.
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

## Status and limitations

- Developed with a single Android phone and a sway desktop; other phones, panels and
  compositors are untested.
- Linux only (uinput, libinput). The phone app is sideloaded with a debug key.
- Packets are authenticated, not encrypted. Not security-audited.

## License

MIT, see [LICENSE](LICENSE).
