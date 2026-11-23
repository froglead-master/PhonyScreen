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
- `phonyscreen/winclip.py`: Win32 clipboard (text, pictures via Pillow, change counter)
- `phonyscreen/style.py`: look of the window: palette, Segoe UI fonts loaded from their files (`SysFont("segoeui")` picks Segoe UI *Light*), antialiased shapes/icons (white mask drawn 4x, smoothscaled, tinted; cached). `viewer.py` builds each overlay once per state (`cached_overlay`)
- `phonyscreen/appicon.py`: the app icon ("Mirror": iPhone + its mirrored copy on a flat squircle tile, no background gradient by the owner's choice), drawn with Pillow; ≤32 px get a simplified drawing. Single source for the window/taskbar icon, the connecting screen and `assets/phonyscreen.ico` (regenerate: `python -m phonyscreen.appicon assets/phonyscreen.ico`)
- `phonyscreen/phoneclip.py`: text from rich phone copies (HTML / web archive → text), patched into pymobiledevice3's `snapshot_text`
- `phonyscreen/discovery.py`: finds the phone on Wi-Fi by scanning the /24 for port 62078 and checking the UDID
- `phonyscreen/config.py`: `%APPDATA%\PhonyScreen\config.json` (udid, wifi_ip, window_pos, settings)
- `docs/FINDINGS.md`: feasibility research and problems solved. Read before changing streaming code.
- `.github/workflows/build.yml`: PyInstaller onedir build on windows-latest (x64 Python 3.13), smoke tests, artifact `PhonyScreen-windows`; tags `v*` publish a release

## Hard-won facts (don't relearn these)
- Video: CoreDevice DisplayService, HEVC over RTP through the RSD tunnel. Needs Developer Mode and the developer disk image mounted after each phone reboot.
- Decode in software (PyAV, slice threads). Hardware HEVC on Snapdragon shows green flashes on this stream.
- Never pause decoding waiting for a keyframe (the base class's refresh loop caused 7 s freezes). On errors: one PLI; if no IDR within 1 s, restart the stream. Watchdog restarts with back-off (5→30 s, counted from any restart), because each restart makes iOS show a "viewed remotely" banner. Every restart opens with a keyframe burst even from an asleep phone, so the back-off resets only when frames still arrive ≥10 s after the last restart (`STEADY_SECONDS`).
- Watchdog check 3 (2026-11-23): data can keep arriving while no picture comes out (packets lost during a big clipboard transfer over the Wi-Fi tunnel; a keyframe *arriving* clears `dirty_since`, and `last_feed_t` stays fresh), which froze the video for good. `deliver()` stamps `last_frame_t`; data but no picture for FROZEN_SECONDS (3 s) restarts the stream, with the same back-off, which now resets only after STEADY_SECONDS of pictures (not data). LeanDecoder hands over a frame at least every 0.1 s even when behind.
- Locked phone keeps streaming pure black frames → `frame_is_black` → blurred last frame + "Click to wake" (privacy: blur must stay unreadable).
- Touch: 0..65535 normalized, mainTouchscreen surface (257). Send samples ≥16 ms apart (`_paced_pointer`). iOS does NOT act on injected Home swipes from the bottom edge, so `_track_home_swipe` presses the Home button (Consumer 0x0C/0x40) at the end of such a swipe. Swipe up on the lock screen does work (used to reach the passcode screen after wake).
- App Switcher = Home (Consumer 0x0C/0x40) pressed twice, 150 ms apart (`_open_app_switcher`), like a physical Home button. Confirmed on iOS 27.2; no gesture surface needed.
- The gesture surface 1281 is a Generic Desktop *mouse* (usage 1/2, `_CoreDevice_suppressMousePointer`) taking 19-byte rid 0x13 pointer reports; unexplored. Other surfaces: 512 keyboard, 1026 mainScreenButtons, 1280 avpCustom (vendor page 0xFF61).
- Audio: RTP PT 101, one raw AAC-ELD frame per packet, 480 samples, 48 kHz stereo. The phone's ASC is wrong for FFmpeg; use `F8 E6 50 00`. The phone keeps playing on its own speaker unless muted; we press its Mute key while PC sound plays (see Open items).
- Phone brightness (2026-11-23, iOS 27.2): `DeviceInfoService.get_display_info()` has no level, only `backlightState` (`activeOn`/`off`). The level IS readable via the diagnostics relay (`DiagnosticsService(rsd).ioregistry(name="AppleARMBacklight")` → `IODisplayParameters.brightness` 0..65536, `rawBrightness` 0..2047; ~170 ms; Auto-Brightness moves it on its own). Setting it with HID did NOT work: Consumer 0x0C/0x70 and 0x6F (Display Brightness Decrement/Increment) via `IndigoHIDService.send_button` (the Mute path), Apple vendor keyboard 0xFF01/0x21 and 0x20, and F14 via a virtual keyboard all left the level unchanged (phone unlocked, screen on, confirmed by the owner). The video stream is unaffected by the phone's brightness (owner).
- Typing: the virtual keyboard sends US key positions (HID usages), so only ASCII maps. Non-ASCII (ç ş ğ ı İ ü ö) and AltGr symbols (@ # { } ...) go through the phone's clipboard + Cmd+V (`_type_text`; ~0.35 s; no iOS banner; chars typed during a paste are merged). Cmd+V is sent as explicit reports so a held Shift (Ç) doesn't make it Cmd+Shift+V. The clipboard's real text (`_clip_known`: last Windows push, last phone copy, or content at start) is put back after RESTORE_AFTER. Never read the phone's clipboard to "save" it right after one of our writes: the read can be stale (that leaked a typed letter to Windows once). Typed texts are never reported as phone copies (`_typed_texts`).
- Windows reports AltGr as LCtrl+RAlt (pygame mod 0x0240): detect it (printable unicode + RALT/MODE or Ctrl+Alt) or AltGr+Q (@ on Turkish Q) becomes Ctrl+Q → Cmd+Q. iOS auto-capitalizes after ". " / "! " for hardware keyboards too (phone setting, not a bug).
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
Confirmed working by the owner: scroll scale, wake → auto swipe to passcode screen, keypad, iPhone speaker mute while PC sound plays (HID Mute toggle, `_sync_phone_mute`; captured audio is pre-volume), Home via touchpad pointer drag from the bottom edge, Home via one-finger touchscreen swipe from the bottom edge (FingerTracker: lone finger from bottom 5%, up ≥10%, no pause; confirmed 2026-11-23), App Switcher via pill button, F4 and swipe-up-and-hold from the bottom edge (held still > `HOME_SWIPE_PAUSE` 0.3 s before lifting; confirmed 2026-11-23).
Not working / to verify:
Phone → Windows clipboard confirmed on 2026-11-23: plain text field, WhatsApp, Gmail (arrives within ~50 ms; log "phone copy -> Windows clipboard").
- Rich copies (Gmail/WebKit) list ~8 types in an order that changes per copy. pymobiledevice3 reads only the primary type inline (`promiseSecondary`), so `phoneclip` extracts text from inline HTML or a web archive. For any other primary type, `allow_full_pull` (monitor + `_clip_pull_loop`) pulls every representation: 0.14 s for Gmail, up to ~64 s for Notes-style copies (pasteboard only, not video). An RTF-first copy relies on that full pull; untested.
- Oddity seen only after a host SET (PC → phone): a PULL can report the new changeCount with the previous contents, or types with empty data. Phone copies were always read correctly.
- Watchdog back-off while the phone sleeps: fixed 2026-11-23 (it used to reset to 5 s on each restart's opening frames, restarting every ~13–20 s). Checked with an offline timing simulation (gaps 10→20→30 s, reset after steady video); not yet on hardware. To verify: let the phone sleep with PhonyScreen open; the log's "restarting stream: no video" lines should space out to 30 s.
- The owner can't do two-finger touchpad swipes comfortably; the pointer drag and touchscreen paths are what matter.
Ideas / not done:
- Explore surface 1281 (mouse) for real system gestures, if ever needed (Home and App Switcher already work via the Home button).
- Done 2026-11-23, confirmed by the owner: non-ASCII typing (incl. @ # { }) and image clipboard both ways. Pictures: phone → Windows as CF_DIB + "PNG" (`winclip.image_formats`, converted off the window thread); Windows → phone as PNG via `PasteboardMonitor.set_image` on focus gain and Ctrl+V (Ctrl+V waits for the picture). Windows' `GetClipboardSequenceNumber` decides when to read and marks our own writes (no echo); pictures sent are hashed so they aren't read back as phone copies or sent twice. HEIC-only copies can't be converted (no pillow-heif). Emoji from the Windows emoji panel (Win+.) arrive as TEXTINPUT only, not handled.
- Dim the iPhone screen while connected (battery): HID brightness keys don't work (see Hard-won facts). Only route left: drag Control Center's brightness slider with injected touches, checking the IORegistry level; fragile (layout-dependent, visible on the phone). Not started.
- Multi-touch on touchscreen laptops is untested on hardware.
- Release v0.1.0 (tag) only after the owner confirms stability; then a download page on froglead.com (repo froglead-master/froglead-website, Vercel). Show him before it goes live.
