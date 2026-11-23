# PhonyScreen: notes for Claude Code

Open-source (GPL-3.0) Windows app that mirrors and controls an unmodified iPhone (iOS 27+), with no app on the phone. Owner: Cem (Froglead). Repo: froglead-master/PhonyScreen. Local folder: C:\FrogleadApps\PhonyScreen.

Formerly named PhonePane (old repo froglead-master/PhonePane, renamed 2026-11-23 and restarted with fresh history). On first run PhonyScreen copies `%APPDATA%\PhonePane\config.json`, and removes the old "PhonePane" firewall rule and shortcuts.

## Product requirements (from the owner)
- One-time setup on Windows and iPhone, then seamless: start it and it connects (USB or Wi-Fi, no IP typing).
- Must work wirelessly. Latency must not feel clunky.
- Must handle a locked phone gracefully (wake + passcode from the PC).
- Polished, macOS-like window: rounded, no console window.
- Not code-signed on purpose; README explains the SmartScreen "More info → Run anyway" step.

## Layout
- `phonyscreen.py`: entry point → `phonyscreen.launcher.main`
- `phonyscreen/launcher.py`: subcommands (`setup`, connect (default), `--usb`, `--wifi IP`, `selftest`, `pmd3 ...` passthrough to pymobiledevice3), admin relaunch, firewall rule (removes Windows' auto-created *Block* rules), USB/Wi-Fi tunnels, developer disk image mount, Start-menu shortcuts
- `phonyscreen/viewer.py`: everything else. `LeanMirror` (subclass of pymobiledevice3's `VncStreamServer`) runs the stream on an asyncio thread; `main()` is the pygame window loop (rounded window via SetWindowRgn, pill bar, keypad, settings panel, gestures, locked view)
- `phonyscreen/audio.py`: AAC-ELD decode (PyAV) + jitter buffer + sounddevice output
- `phonyscreen/winclip.py`: Win32 clipboard (text)
- `phonyscreen/discovery.py`: finds the phone on Wi-Fi by scanning the /24 for port 62078 and checking the UDID
- `phonyscreen/config.py`: `%APPDATA%\PhonyScreen\config.json` (udid, wifi_ip, window_pos, settings)
- `docs/FINDINGS.md`: feasibility research and problems solved. Read before changing streaming code.
- `.github/workflows/build.yml`: PyInstaller onedir build on windows-latest (x64 Python 3.13), smoke tests, artifact `PhonyScreen-windows`; tags `v*` publish a release

## Hard-won facts (don't relearn these)
- Video: CoreDevice DisplayService, HEVC over RTP through the RSD tunnel. Needs Developer Mode and the developer disk image mounted after each phone reboot.
- Decode in software (PyAV, slice threads). Hardware HEVC on Snapdragon shows green flashes on this stream.
- Never pause decoding waiting for a keyframe (the base class's refresh loop caused 7 s freezes). On errors: one PLI; if no IDR within 1 s, restart the stream. Watchdog restarts with back-off (5→30 s), because each restart makes iOS show a "viewed remotely" banner.
- Locked phone keeps streaming pure black frames → `frame_is_black` → blurred last frame + "Click to wake" (privacy: blur must stay unreadable).
- Touch: 0..65535 normalized, mainTouchscreen surface (257). Send samples ≥16 ms apart (`_paced_pointer`). iOS does NOT act on injected Home swipes from the bottom edge, so `_track_home_swipe` presses the Home button (Consumer 0x0C/0x40) at the end of such a swipe. Swipe up on the lock screen does work (used to reach the passcode screen after wake).
- The gesture surface 1281 (19-byte rid 0x13 pointer reports) is unexplored. It might be the route to real system gestures (Home, App Switcher).
- Audio: RTP PT 101, one raw AAC-ELD frame per packet, 480 samples, 48 kHz stereo. The phone's ASC is wrong for FFmpeg; use `F8 E6 50 00`. The phone keeps playing on its own speaker unless muted; we press its Mute key while PC sound plays (see Open items).
- Windows clipboard: open it with our HWND or SetClipboardData fails after EmptyClipboard.
- Dismissed Windows Firewall prompts create Block rules for the exe (tunnel adapter counts as Public network); launcher deletes them.
- Wi-Fi: Bonjour was unreliable → subnet scan. Tunnel via `lockdown start-tunnel --mobdev2-host IP`.
- ARM64 Python can't build some deps (cryptography, lzfse). Use x64 Python 3.13 even on Windows on Arm.

## Developing on this PC
- `py -3.13 -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt` (x64 Python).
- Run: `.venv\Scripts\python phonyscreen.py -v` from an **Administrator** terminal (the tunnel creates a network adapter). `python phonyscreen.py setup` once per phone.
- Logs go to the terminal with `-v`. Lint: `python -m pyflakes phonyscreen`.
- pymobiledevice3 CLI is available for experiments: `python phonyscreen.py pmd3 developer core-device universal-hid-service --help` (needs a tunnel; `pmd3 remote tunneld` in another admin terminal).
- Keep pymobiledevice3 pinned (11.61.1); the app subclasses its internals.

## Open items (as of 2026-11-23)
Confirmed working by the owner: scroll scale, wake → auto swipe to passcode screen, keypad, iPhone speaker mute while PC sound plays (HID Mute toggle, `_sync_phone_mute`; captured audio is pre-volume), Home via touchpad pointer drag from the bottom edge.
Not working / to verify:
- **Phone → Windows clipboard still fails** (Windows → phone works). Tried: HWND owner for SetClipboardData, pulling the pasteboard on focus loss and 0.5 s after Ctrl+C/X (`_clip_pull_loop`), plus pymobiledevice3's PasteboardMonitor notifications. Next step: read `%APPDATA%\PhonyScreen\phonyscreen.log` for "phone clipboard changed", "phone copy -> Windows clipboard", "couldn't read the phone clipboard", then test each half in isolation (`pmd3` pasteboard read; `winclip.set_text` alone).
- Touchscreen Home swipe: added in FingerTracker (lone finger from bottom 5%, up ≥10%, no pause) after the owner reported it didn't work on the touchscreen; untested on hardware.
- The owner can't do two-finger touchpad swipes comfortably; the pointer drag and touchscreen paths are what matter.
Ideas / not done:
- App Switcher gesture; explore surface 1281 for real system gestures.
- Non-ASCII typing (ç, ş, ğ); image clipboard.
- Multi-touch on touchscreen laptops is untested on hardware.
- Release v0.1.0 (tag) only after the owner confirms stability; then a download page on froglead.com (repo froglead-master/froglead-website, Vercel). Show him before it goes live.
