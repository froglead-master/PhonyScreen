# Feasibility findings (November 2026)

Hands-on test: Windows-on-Arm laptop (Snapdragon), iPhone 17 on iOS 27.2, pymobiledevice3 11.61.1.

## What worked

| Area | Result |
|---|---|
| Video | Phone sends HEVC Main, 1216 × 2656 (2624 visible), up to 60 fps, over the iOS 17+ tunnel. Recorded streams decode with zero errors in FFmpeg. |
| Input | Touch, keyboard and hardware buttons through the phone's HID services, no iOS app. |
| Lock screen | Streams while locked. iOS blanks the passcode keypad in capture; taps on its positions still work, so the laptop can unlock the phone. |
| USB | Smooth, felt instant; the Windows side added 12–18 ms. |
| Wi-Fi | Smooth over home Wi-Fi via `lockdown start-tunnel --mobdev2-host <phone IP>`. |

## Problems found, and the fixes in PhonyScreen

1. **Hardware decoding fails on Snapdragon.** The Windows/Qualcomm HEVC decoder (used by Chrome, Edge and Windows Media Player) shows green flashes and frozen frames on this stream, even though the stream itself is clean. **Fix:** software decoding with FFmpeg/PyAV, slice-threaded. Decode took 4–9 ms per frame under x64 emulation.
2. **Keyframe requests are mostly ignored.** pymobiledevice3's VNC recovery loop periodically pauses decoding and sends an RTCP PLI, then waits for an IDR frame. The phone rarely answers, so video froze for about 6 s at a time. **Fix:** never pause decoding. On a decode problem, send one PLI; if no keyframe arrives within 1 s, restart the stream, which always starts with an IDR and takes about 0.1–0.2 s.
3. **The stream stops when the phone sleeps or locks** and doesn't come back by itself. **Fix:** a watchdog restarts the stream after 6 s without video.
4. **`serve-vnc` crashes on Windows** (`os.dup()` on a socket handle). Not used by PhonyScreen, which has its own viewer.
5. **The userspace tunnel drops packets.** Use the native Wintun tunnel (`remote tunneld`) or a lockdown tunnel.
6. **Wi-Fi discovery (Bonjour) found nothing** on the test network, even with the firewall configured. Apple Devices' "Show this iPhone when on Wi-Fi" checkbox did not actually set `EnableWifiConnections`; `pymobiledevice3 lockdown wifi-connections on` did. Connecting by IP works.
7. **Windows Firewall blocked the packaged .exe.** A dismissed "allow this app?" prompt creates inbound Block rules for Public networks, and the tunnel adapter counts as Public, so the keyframe bursts were dropped (input still worked). **Fix:** the launcher removes Block rules for its own executable and adds an Allow rule.
8. **iOS 26 and earlier:** the stream request is accepted but no frames arrive.

## Ideas not yet tested

- Multi-touch (pinch, two-finger gestures) from Windows touchscreens
- Audio (AAC-ELD; needs a non-macOS decoder)
- Drawing a passcode keypad overlay at the blanked keypad's positions
- Android support by bundling scrcpy
