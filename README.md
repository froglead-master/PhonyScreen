# PhonyScreen

**Mirror and control your iPhone from a Windows PC: no app on the phone, over USB or Wi-Fi.**

PhonyScreen shows your iPhone's screen in a window on Windows and lets you use it with your mouse, keyboard or touchscreen. Nothing is installed on the iPhone. It uses the screen-streaming service built into iOS 27 (the one Xcode's device mirroring uses), via [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

> **Status: early preview (v0.1).** Tested on one Windows-on-Arm laptop (Snapdragon) with an iPhone 17 on iOS 27.2, over USB and home Wi-Fi. Expect rough edges.
>
> PhonyScreen is not affiliated with or endorsed by Apple. iPhone and iOS are trademarks of Apple Inc.

## What works

- Live screen at up to 60 fps, decoded in software (about 12–18 ms added on the Windows side in testing)
- Tap, swipe and type from Windows; Home, App Switcher and Lock buttons
- USB or Wi-Fi (Wi-Fi finds the phone on your network by itself)
- Phone sound plays on the PC's speakers (PhonyScreen mutes the iPhone's own speaker meanwhile and unmutes it when you close PhonyScreen)
- Shared clipboard: copy text on the phone, paste it in Windows, and the other way round
- Locked phone: wake it and enter your passcode from the laptop with the built-in keypad (iOS hides its own number pad from screen capture)
- Video restarts on its own if the stream stops

## Requirements

- Windows 10/11 (x64, or Windows on Arm via built-in emulation)
- iPhone on **iOS 27 or later**
- [Apple Devices](https://apps.microsoft.com/) from the Microsoft Store (provides the iPhone USB driver)
- Administrator rights (the iOS tunnel creates a virtual network adapter; PhonyScreen asks for them, and also adds a Windows Firewall rule so the phone's video stream can reach it)

## Quick start (.exe)

1. Download `PhonyScreen-windows.zip` from [Releases](../../releases) and unzip it.
   > Windows SmartScreen may say "Windows protected your PC" because PhonyScreen isn't code-signed. Click **More info → Run anyway**. The source code is all here if you'd rather build it yourself.
2. **First time only:** plug the iPhone in by USB and run `PhonyScreen.exe setup`. It walks you through turning on **Developer Mode** (Settings → Privacy & Security, needs a phone restart), allows Wi-Fi connections, and adds PhonyScreen to the Start menu and desktop.
3. **After that:** start PhonyScreen. It uses USB if the phone is plugged in; otherwise it finds the phone on your Wi-Fi by itself (no IP address needed).

Controls:
- **click / drag** = tap / swipe. Drags that start at the screen edge work like iPhone edge gestures: swipe up from the bottom for Home (PhonyScreen presses Home for you at the end of that swipe, since iOS doesn't act on it by itself), swipe up from the bottom and hold still a moment before letting go for the App Switcher, down from the top right for Control Center, in from the left for Back.
- **mouse wheel / touchpad** scrolls the phone while the pointer is over it; a two-finger swipe with the pointer on the phone's bottom edge goes Home; **touchpad pinch** (or Ctrl + wheel) pinch-zooms
- **touchscreen laptops**: use your fingers directly, including two-finger pinch
- **the bar above the phone**: drag to move the window; buttons for Home, App Switcher, rotate, passcode pad, settings, minimize, close
- **settings** (gear button): phone size, scroll speed, pinch speed, sound on this PC, mute iPhone speaker, shared clipboard, stats. Changes are remembered.
- **locked phone**: PhonyScreen shows a blurred background with a lock; click to wake it. PhonyScreen swipes past Face ID to the passcode screen, so you can type the code on the keypad or the keyboard (**Show passcode screen** brings it back if iOS timed out)
- **Ctrl + / Ctrl − / Ctrl 0** = bigger / smaller / default size (same as the Phone size setting; the window position is remembered too)
- type normally (US-ASCII characters for now). Other **Ctrl** shortcuts go to the phone as **Cmd** shortcuts: Ctrl+C / Ctrl+V / Ctrl+A / Ctrl+Z work in phone text fields
- **F1** or right-click = Home · **F2** = Lock/wake · **F3** = stats · **F4** = App Switcher · **Esc** = quit

PhonyScreen opens its own window and shows connection progress there; no console window stays open. If something misbehaves, the log is at `%APPDATA%\PhonyScreen\phonyscreen.log`.

## Running from source

```
python -m pip install -r requirements.txt
python phonyscreen.py setup     # first time, phone on USB
python phonyscreen.py           # every time after
python phonyscreen.py --wifi 192.168.1.23   # force Wi-Fi to a given IP
```

Use 64-bit (x64) Python 3.13. On Windows on Arm, some dependencies have no ARM64 builds yet; the x64 Python runs fine under emulation.

## Known limitations

- iOS 27+ only. On iOS 26 the phone accepts the stream request but never sends video.
- Wi-Fi: PhonyScreen finds the phone by scanning your local network for it (Bonjour discovery was unreliable), so the phone must be on the same network as the PC, and has to have been connected by USB once.
- The developer disk image must be re-mounted after every phone restart (PhonyScreen does this automatically).
- Landscape: PhonyScreen rotates when the phone reports a rotation; if it doesn't, use the rotate button.
- To keep sound off the phone's speaker, PhonyScreen presses the phone's Mute key. If PhonyScreen is closed unexpectedly the phone can stay muted; press a volume button on the phone to bring the sound back.
- The shared clipboard handles text only, not images or files.
- Typing non-ASCII characters (for example ç, ş, ğ) isn't supported yet.
- Built on an undocumented Apple service. An iOS update could change or break it at any time.

## How it works

See [docs/FINDINGS.md](docs/FINDINGS.md) for what was tested and why the viewer is built the way it is. In short: pymobiledevice3 opens an iOS 17+ tunnel (USB or Wi-Fi) and starts the phone's HEVC screen stream. PhonyScreen decodes it with FFmpeg (PyAV) in software, scales it inside the decoder, and draws only the newest frame. Touch and keys go back through the phone's HID services.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)), as required by pymobiledevice3. Contributions welcome.
