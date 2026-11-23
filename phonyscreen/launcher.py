"""PhonyScreen launcher: one command from "phone nearby" to "phone in a window".

    phonyscreen                 connect (USB if plugged in, else finds the phone on Wi-Fi) and open the viewer
    phonyscreen setup           one-time setup over USB (Developer Mode, Wi-Fi access)
    phonyscreen --usb           force USB
    phonyscreen --wifi IP       force Wi-Fi, trying this IP first
    phonyscreen pmd3 ...        run a pymobiledevice3 command (used internally by the .exe)

Needs administrator rights (the iOS 17+ tunnel creates a virtual network adapter);
PhonyScreen asks for them itself.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Optional

from phonyscreen import __version__
from phonyscreen.config import load_config, save_config

TUNNELD_URL = "http://127.0.0.1:49151/"
WIFI_WAIT_SECONDS = 60  # keep looking for an asleep phone this long


# -- helpers -------------------------------------------------------------------
_status_cb = None  # set while the viewer window is open: progress messages show there too
CONSOLE_HIDDEN = False
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW for helper processes


def say(msg: str) -> None:
    print(f"[PhonyScreen] {msg}", flush=True)
    if _status_cb is not None:
        _status_cb(msg)


def hide_own_console() -> bool:
    """Hide the console window if PhonyScreen owns it (double-clicked, or the elevated relaunch).
    A terminal the user started PhonyScreen from is left alone."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        pids = (ctypes.c_uint32 * 8)()
        if kernel32.GetConsoleProcessList(pids, 8) > 1:  # shared with cmd/PowerShell
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        return True
    except Exception:
        return False


def show_error_box(msg: str) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "PhonyScreen", 0x10)  # MB_ICONERROR


def pmd3_cmd(*args: str) -> list[str]:
    """Command line that runs pymobiledevice3, both from source and from the frozen .exe."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "pmd3", *args]
    return [sys.executable, "-m", "pymobiledevice3", *args]


def run_pmd3(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        pmd3_cmd(*args), capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW
    )


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else True
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(argv: list[str]) -> bool:
    """Re-run this program elevated (UAC prompt). Returns True if the elevated copy was started."""
    import ctypes

    if getattr(sys, "frozen", False):
        exe, params = sys.executable, argv
    else:
        exe, params = sys.executable, [os.path.abspath(sys.argv[0]), *argv]
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(params), None, 1)
    return rc > 32


def ensure_firewall_rule() -> None:
    """Allow incoming traffic for this program. The phone sends video as incoming UDP over the
    tunnel; without a rule, Windows Firewall silently drops it (input still works, video never
    arrives). Needs admin, which PhonyScreen already has."""
    if sys.platform != "win32":
        return
    program = sys.executable
    name = "PhonyScreen"
    # If a Windows "allow this app?" prompt for this program was dismissed, Windows created
    # inbound BLOCK rules for it (typically for Public networks, which is how the tunnel
    # adapter is classified). Block beats allow, so remove those first.
    ps_program = program.replace("'", "''")
    subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-NetFirewallApplicationFilter"
            f" | Where-Object {{ $_.Program -ieq '{ps_program}' }}"
            " | Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' } | Remove-NetFirewallRule",
        ],
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    # The app used to be called PhonePane: drop its rule.
    subprocess.run(
        ["netsh", "advfirewall", "firewall", "delete", "rule", "name=PhonePane"],
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    # Replace any old allow rule for this exact program path, then add a fresh one.
    subprocess.run(
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}", f"program={program}"],
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    res = subprocess.run(
        [
            "netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
            f"program={program}", "enable=yes", "profile=any",
        ],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if res.returncode != 0:
        say(f"Couldn't add a Windows Firewall rule ({res.stdout.strip() or res.stderr.strip()}); "
            "if video doesn't appear, allow PhonyScreen in Windows Firewall.")


def usb_devices() -> list[dict]:
    """Devices usbmuxd sees over the cable."""
    try:
        out = run_pmd3("usbmux", "list", timeout=30)
        devices = json.loads(out.stdout or "[]")
    except Exception:
        return []
    return [d for d in devices if d.get("ConnectionType") == "USB"]


def remember_usb_phone(cfg: dict, device: dict) -> None:
    """Over USB: remember which phone this is and allow Wi-Fi connections (once), so later
    Wi-Fi sessions can find and verify it without any setup."""
    udid = device.get("UniqueDeviceID") or device.get("Identifier")
    changed = False
    if udid and cfg.get("udid") != udid:
        cfg["udid"], changed = udid, True
    if not cfg.get("wifi_enabled_for") == udid:
        res = run_pmd3("lockdown", "wifi-connections", "on", timeout=30)
        if res.returncode == 0:
            cfg["wifi_enabled_for"], changed = udid, True
    if changed:
        save_config(cfg)


def create_shortcuts() -> None:
    """Start menu and desktop shortcuts to the .exe (only for the packaged app)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    exe = sys.executable.replace("'", "''")
    workdir = os.path.dirname(sys.executable).replace("'", "''")
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        "foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) { "
        "Remove-Item -ErrorAction SilentlyContinue (Join-Path $d 'PhonePane.lnk'); "  # the former name
        "$l = $s.CreateShortcut((Join-Path $d 'PhonyScreen.lnk')); "
        f"$l.TargetPath = '{exe}'; $l.WorkingDirectory = '{workdir}'; $l.IconLocation = '{exe},0'; "
        "$l.Description = 'Mirror and control your iPhone'; $l.Save() }"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if res.returncode == 0:
        say("Added PhonyScreen to the Start menu and the desktop.")
    else:
        say(f"Couldn't create shortcuts: {_error_line(res.stderr)}")


def tunneld_has_device() -> bool:
    try:
        with urllib.request.urlopen(TUNNELD_URL, timeout=2) as r:
            return bool(json.loads(r.read().decode() or "{}"))
    except Exception:
        return False


def _error_line(output: str) -> str:
    """The most useful line of pymobiledevice3's log output: the last ERROR message, if any."""
    lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
    errors = [ln.split(" ERROR ", 1)[1] for ln in lines if " ERROR " in ln]
    if errors:
        return errors[-1]
    return lines[-1] if lines else "no output"


def mount_developer_image(*rsd_args: str) -> None:
    say("Mounting the developer disk image (needed after every phone restart)...")
    res = run_pmd3("mounter", "auto-mount", *rsd_args, timeout=300)
    output = res.stdout + res.stderr
    if res.returncode == 0 or "already mounted" in output.lower():
        say("Developer disk image ready.")
    else:
        say(f"Mount reported a problem (continuing anyway): {_error_line(output)}")


# -- connection modes -----------------------------------------------------------
def start_usb_tunnel() -> subprocess.Popen:
    if tunneld_has_device():
        say("Reusing the tunnel that is already running.")
        return None  # type: ignore[return-value]
    say("Starting the USB tunnel...")
    proc = subprocess.Popen(
        pmd3_cmd("remote", "tunneld", "--no-wifi", "--no-mobdev2"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    for _ in range(60):
        if tunneld_has_device():
            say("Tunnel up.")
            return proc
        if proc.poll() is not None:
            raise RuntimeError("the tunnel helper exited; is the phone unlocked and trusted?")
        time.sleep(1)
    proc.terminate()
    raise RuntimeError("no tunnel after 60 s; unlock the phone and check the cable")


def start_wifi_tunnel(ip: str) -> tuple[subprocess.Popen, str, str]:
    say(f"Opening a Wi-Fi tunnel to {ip}...")
    proc = subprocess.Popen(
        pmd3_cmd("lockdown", "start-tunnel", "--mobdev2-host", ip, "--script-mode"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    deadline = time.time() + 45
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"Wi-Fi tunnel failed: {_error_line(proc.stderr.read() if proc.stderr else '')}. "
                    "Is the phone unlocked, on the same network, and was `phonyscreen setup` run?"
                )
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            say("Tunnel up.")
            return proc, parts[0], parts[1]
    proc.terminate()
    raise RuntimeError("no Wi-Fi tunnel after 45 s")


# -- commands -----------------------------------------------------------------
def cmd_setup() -> int:
    say("One-time setup. Connect the iPhone with a USB cable, unlock it and tap Trust if asked.")
    for _ in range(30):
        devs = usb_devices()
        if devs:
            break
        time.sleep(2)
    else:
        say("No iPhone found over USB. Install Apple Devices from the Microsoft Store, then try again.")
        return 1
    d = devs[0]
    say(f"Found {d.get('DeviceName')} (iOS {d.get('ProductVersion')}).")
    if int(str(d.get("ProductVersion", "0")).split(".")[0]) < 27:
        say("PhonyScreen needs iOS 27 or later for screen streaming.")
        return 1

    run_pmd3("amfi", "reveal-developer-mode")
    say("On the iPhone: Settings > Privacy & Security > Developer Mode > On, then restart it.")
    say("After the restart, unlock it and confirm 'Turn On'.")
    input("[PhonyScreen] Press Enter when Developer Mode is on and the phone is unlocked... ")

    cfg = load_config()
    remember_usb_phone(cfg, d)
    say("Wi-Fi access enabled; PhonyScreen will find the phone on your network by itself.")
    mount_developer_image()
    create_shortcuts()
    say("Setup done. Start PhonyScreen to mirror over USB, or unplug and it connects over Wi-Fi.")
    return 0


def cmd_connect(force_usb: bool, wifi_ip: Optional[str], viewer_args: list[str]) -> int:
    """Open the viewer window right away; connecting happens in the background with progress shown
    in the window (no console needed)."""
    global _status_cb
    cfg = load_config()
    if wifi_ip:
        cfg["wifi_ip"] = wifi_ip
        save_config(cfg)
    procs: list[subprocess.Popen] = []

    def prepare(set_status) -> Optional[tuple[str, int]]:
        global _status_cb
        _status_cb = set_status
        say("Looking for your iPhone...")
        devs = usb_devices()
        if devs and not wifi_ip:
            remember_usb_phone(cfg, devs[0])
            mount_developer_image()
            p = start_usb_tunnel()
            if p is not None:
                procs.append(p)
            return None
        if force_usb:
            raise RuntimeError("No iPhone found on USB. Plug it in, unlock it and tap Trust.")
        from phonyscreen.discovery import find_phone

        # An asleep/locked iPhone often stops answering on Wi-Fi, so keep trying for a while
        # (it answers again once the screen is on) instead of failing on the first attempt.
        deadline = time.time() + WIFI_WAIT_SECONDS
        attempt = 0
        last_error = ""
        while True:
            attempt += 1
            say("Finding your iPhone on Wi-Fi..." if attempt == 1 else
                "Waiting for your iPhone on Wi-Fi. Unlock it (or plug it in with USB)...")
            if usb_devices():  # plugged in meanwhile: use the cable
                devs = usb_devices()
                if devs:
                    remember_usb_phone(cfg, devs[0])
                    mount_developer_image()
                    p = start_usb_tunnel()
                    if p is not None:
                        procs.append(p)
                    return None
            found = find_phone(cfg.get("udid"), wifi_ip or cfg.get("wifi_ip"))
            if found is not None:
                ip, udid = found
                if (cfg.get("wifi_ip"), cfg.get("udid")) != (ip, udid):
                    cfg.update(wifi_ip=ip, udid=udid)
                    save_config(cfg)
                try:
                    p, host, port = start_wifi_tunnel(ip)
                except RuntimeError as e:
                    last_error = str(e)
                else:
                    procs.append(p)
                    mount_developer_image("--rsd", host, port)
                    return host, int(port)
            if time.time() > deadline:
                raise RuntimeError(
                    "Couldn't reach your iPhone over Wi-Fi. Unlock it and check it's on the same Wi-Fi "
                    "as this PC, or plug it in with USB once so PhonyScreen can set up Wi-Fi."
                    + (f" (last error: {last_error})" if last_error else "")
                )
            time.sleep(3)

    from phonyscreen import viewer

    try:
        return viewer.main(viewer_args, prepare=prepare)
    finally:
        _status_cb = None
        for p in procs:
            with contextlib.suppress(Exception):
                p.terminate()
                p.wait(timeout=5)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["selftest"]:  # used by CI: check the packaged app can load everything it needs
        import av
        import pygame
        import sounddevice

        from phonyscreen import viewer
        from phonyscreen.audio import AacEldDecoder

        av.codec.CodecContext.create("hevc", "r")
        AacEldDecoder()
        print(f"sounddevice {sounddevice.__version__} (PortAudio {sounddevice.get_portaudio_version()[1]})")
        print(
            f"selftest ok: PhonyScreen {__version__} ({viewer.__name__}), "
            f"PyAV {av.__version__}, pygame {pygame.version.ver}"
        )
        return 0
    if argv[:1] == ["pmd3"]:  # pymobiledevice3 passthrough (the .exe calls itself this way)
        from pymobiledevice3.__main__ import main as pmd3_main

        sys.argv = ["pymobiledevice3", *argv[1:]]
        return int(pmd3_main() or 0)

    parser = argparse.ArgumentParser(prog="phonyscreen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["connect", "setup"], default="connect")
    parser.add_argument("--usb", action="store_true", help="force USB")
    parser.add_argument("--wifi", metavar="IP", help="force Wi-Fi, trying this phone IP first")
    parser.add_argument("--version", action="version", version=f"PhonyScreen {__version__}")
    args, viewer_args = parser.parse_known_args(argv)

    if sys.platform == "win32" and not is_admin():
        say("Administrator rights are needed for the tunnel; asking Windows...")
        if relaunch_as_admin(argv):
            return 0
        say("Not elevated. Right-click PhonyScreen and choose 'Run as administrator'.")
        return 1

    global CONSOLE_HIDDEN
    if args.command != "setup":
        CONSOLE_HIDDEN = hide_own_console()
    ensure_firewall_rule()
    try:
        if args.command == "setup":
            return cmd_setup()
        return cmd_connect(args.usb, args.wifi, viewer_args)
    except Exception as e:
        if CONSOLE_HIDDEN:
            show_error_box(f"PhonyScreen stopped: {e}")
        raise
    finally:
        if getattr(sys, "frozen", False) and not CONSOLE_HIDDEN and sys.stdin and sys.stdin.isatty():
            with contextlib.suppress(Exception):
                input("[PhonyScreen] Press Enter to close this window.")
