"""PhonyScreen viewer: shows and controls an iPhone (iOS 27+) in a Windows window.

Reuses pymobiledevice3's own iOS 27 screen stream (keyframe recovery,
touch / keyboard / button HID) and replaces only the display side:

  * HEVC decoded in software with libavcodec (PyAV), slice-threaded
  * frames scaled to window size INSIDE the decoder (cheap), and only the
    newest frame is converted -- stale frames are skipped, so lag can't pile up
  * drawn straight into a window (no browser, no VNC hop)
  * watchdog restarts the video stream when it stops (e.g. phone locked)

Usually started by the PhonyScreen launcher, which sets up the tunnel first.
Standalone: needs `pymobiledevice3 remote tunneld` running, or --rsd HOST PORT.

Controls (window focused):
  click / drag ........ tap / swipe (up from the bottom edge: Home; up and hold: App Switcher)
  type ................ keyboard input (ç ş ğ, @ # { } ...: pasted through the phone's clipboard)
  F1 or right-click ... Home          F2 or middle-click ... Lock / wake
  F3 .................. toggle stats   F4 .................... App Switcher
  Esc ................. quit viewer
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Any, Callable, Optional

import av
import pygame

from pymobiledevice3.remote.core_device import hid_service as hs
from pymobiledevice3.remote.core_device import vnc_server as vs
from pymobiledevice3.remote.core_device.hevc_rps import parse_sps, remove_emulation_prevention
from pymobiledevice3.remote.core_device.hid_service import TouchscreenContact
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.tunneld.api import get_tunneld_devices

from phonyscreen import appicon, phoneclip, style

logger = logging.getLogger("phonyscreen.viewer")

STALL_SECONDS = 6.0  # no video for this long -> restart the stream
RESTART_BACKOFF = 5.0  # min gap between "no video" restart attempts
STEADY_SECONDS = 10.0  # video still arriving this long after a restart -> back-off resets
FROZEN_SECONDS = 3.0  # video data still arriving but no picture this long -> restart the stream
TOUCH_INTERVAL = 0.016  # min gap between drag samples sent to the phone (finger-like cadence)
HOME_SWIPE_EDGE = 0.05  # a drag starting in the bottom 5% of the screen ...
HOME_SWIPE_DISTANCE = 0.10  # ... and going up at least 10% of it is a Home swipe
HOME_SWIPE_PAUSE = 0.3  # ... lifted within this of the last move: Home; held still longer first: App Switcher
HOME_BUTTON = vs._BTN_HOME
APP_SWITCHER_GAP = 0.15  # Home pressed twice this far apart opens the App Switcher (confirmed on iOS 27.2)
RESTORE_AFTER = 1.5  # after typing ç/ş/ğ through the phone's clipboard, put its old text back this much later
KEYFRAME_WAIT = 1.0  # after a decode problem, wait this long for a keyframe before restarting
NEWFRAME = pygame.USEREVENT + 1

# pygame key -> X11 keysym for keys without a printable character.
SPECIAL_KEYSYMS = {
    pygame.K_BACKSPACE: 0xFF08, pygame.K_TAB: 0xFF09, pygame.K_RETURN: 0xFF0D,
    pygame.K_KP_ENTER: 0xFF8D, pygame.K_LEFT: 0xFF51, pygame.K_UP: 0xFF52,
    pygame.K_RIGHT: 0xFF53, pygame.K_DOWN: 0xFF54, pygame.K_DELETE: 0xFFFF,
    pygame.K_HOME: 0xFF50, pygame.K_END: 0xFF57, pygame.K_PAGEUP: 0xFF55,
    pygame.K_PAGEDOWN: 0xFF56,
}
MODIFIER_KEYSYMS = {
    pygame.K_LSHIFT: 0xFFE1, pygame.K_RSHIFT: 0xFFE2,
}
CMD_KEYSYM = 0xFFEB  # Super_L -> the iPhone's Command key
MODIFIER_KEYS = {pygame.K_LCTRL, pygame.K_RCTRL, pygame.K_LALT, pygame.K_RALT, pygame.K_LSHIFT, pygame.K_RSHIFT, pygame.K_MODE}


def _has_key_nal(annexb: bytes) -> bool:
    """True if the Annex-B access unit contains an IDR/CRA picture."""
    i = annexb.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(annexb):
        if (annexb[i + 3] >> 1) & 0x3F in (19, 20, 21):
            return True
        i = annexb.find(b"\x00\x00\x01", i + 3)
    return False


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.decode_ms: deque[float] = deque(maxlen=120)
        self.convert_ms: deque[float] = deque(maxlen=120)
        self.pipeline_ms: deque[float] = deque(maxlen=120)  # packet in -> on screen
        self.shown: deque[float] = deque(maxlen=240)  # display timestamps
        self.decoded = 0
        self.skipped = 0
        self.restarts = 0

    @staticmethod
    def avg(d: deque[float]) -> float:
        return sum(d) / len(d) if d else 0.0


class LeanDecoder:
    """Drop-in for pymobiledevice3's HevcToBgraTranscoder (feed/close/width/height)."""

    def __init__(self, server: "LeanMirror", vps: bytes, sps: bytes, pps: bytes) -> None:
        st = parse_sps(remove_emulation_prevention(sps[2:]))
        self.width = st.pic_width_in_luma_samples
        self.height = st.pic_height_in_luma_samples
        self._server = server
        self.out_w, self.out_h, self.view_w, self.view_h = server.plan_scale(self.width, self.height)
        self._codec: Any = av.codec.CodecContext.create("hevc", "r")
        # The stream uses wavefront (WPP) coding, so slice threading parallelises
        # each frame without the extra frames of delay that frame threading adds.
        self._codec.thread_type = "SLICE"
        self._codec.thread_count = max(2, min(8, os.cpu_count() or 2))
        with contextlib.suppress(Exception):
            list(self._codec.decode(av.Packet(b"".join(b"\x00\x00\x00\x01" + n for n in (vps, sps, pps)))))
        self._q: queue.Queue[Optional[tuple[bytes, float]]] = queue.Queue()
        self._last_shown = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lean-hevc", daemon=True)
        self._thread.start()

    def feed(self, annexb: bytes) -> None:
        now = time.perf_counter()
        srv = self._server
        if now - srv.last_feed_t > 1.0:
            srv.feed_since = now  # data resumed after a pause: "no picture" counts from here
        srv.last_feed_t = now
        if srv.dirty_since and _has_key_nal(annexb):
            srv.dirty_since = 0.0  # a full frame arrived: references are clean again
        self._q.put((annexb, now))

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        stats = self._server.stats
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                break
            annexb, t_in = item
            t0 = time.perf_counter()
            try:
                frames = list(self._codec.decode(av.Packet(annexb)))
            except Exception as e:
                logger.debug("decode rejected AU: %s", e)
                self._server._on_decode_error_from_worker()
                continue
            t1 = time.perf_counter()
            with stats.lock:
                stats.decode_ms.append((t1 - t0) * 1000)
                stats.decoded += len(frames)
            if not frames:
                self._server._on_decode_error_from_worker()
                continue
            # Every AU must be decoded (later frames reference it), but only the
            # newest one needs converting for display -- yet show one at least every
            # 0.1 s, or a decoder that's behind never shows anything.
            if not self._q.empty() and t1 - self._last_shown < 0.1:
                with stats.lock:
                    stats.skipped += 1
                continue
            self._last_shown = t1
            frame = frames[-1]
            rgb = frame.reformat(width=self.out_w, height=self.out_h, format="rgb24", interpolation="FAST_BILINEAR")
            plane = rgb.planes[0]
            data = bytes(plane)
            row, pitch, surf_w = self.out_w * 3, plane.line_size, self.out_w
            if pitch != row:  # rows padded for alignment
                if pitch % 3 == 0:
                    surf_w = pitch // 3  # padding becomes extra columns right of the visible area
                else:
                    data = b"".join(data[i * pitch : i * pitch + row] for i in range(self.out_h))
            with stats.lock:
                stats.convert_ms.append((time.perf_counter() - t1) * 1000)
            self._server.deliver(data, surf_w, self.out_h, self.view_w, self.view_h, t_in)


class LeanMirror(vs.VncStreamServer):
    """pymobiledevice3's stream server with the VNC side replaced by a local window."""

    restart_t = 0.0  # when the stream was last restarted (any reason)

    def __init__(self, rsd: Any, target_height: int) -> None:
        phoneclip.install()  # Gmail/browser copies often carry only HTML inline: read their text from it
        super().__init__(rsd, decoder="av", share_clipboard=True)  # creates the pasteboard monitor
        if self._clipboard_monitor is not None:
            # A rich copy whose primary type isn't text/HTML: pull every representation. Can take up
            # to ~64 s for a Notes copy (pasteboard only, not video); otherwise it never arrives.
            self._clipboard_monitor._allow_full_pull = True
        self.target_height = target_height
        self.last_frame_t = time.perf_counter()  # last picture handed to the window
        self.feed_since = self.last_frame_t  # when video data last started flowing after a pause
        self.backlight: Optional[str] = None  # the phone's screen state, once known (see _apply_display)
        self.stats = Stats()
        self.last_feed_t = time.perf_counter()
        self.slot: Optional[tuple[bytes, int, int, int, int, float, int]] = None
        self.slot_lock = threading.Lock()
        self.newframe_pending = threading.Event()
        self.status = "Starting video..."
        # Set when the decoder may hold broken references (decode error, lost
        # packets); cleared by the next full frame. Too long -> restart stream.
        self.dirty_since = 0.0
        self.last_pli_t = 0.0
        self.frame_seq = 0
        self.input_buf: deque[tuple[Any, ...]] = deque()
        self.input_evt: Optional[asyncio.Event] = None
        self.stop_event: Optional[asyncio.Event] = None
        # (width, height, rotation) of the picture as shown in the window; touch coordinates are
        # mapped back to the phone through it. Set by the window thread; None = unrotated frame.
        self.display_map: Optional[tuple[int, int, int]] = None
        # The base server only runs keyframe recovery while a client is attached.
        self.client = vs._VncClient(None, None)  # type: ignore[arg-type]
        self._touch_last: Optional[tuple[int, int, float]] = None  # last drag sample (x, y, time)
        self.phone_clip: Optional[tuple[int, str]] = None  # (sequence, text) last copied on the phone
        self.phone_image: Optional[tuple[int, bytes]] = None  # (sequence, PNG/JPEG...) picture copied on the phone
        self._pushed_images: set[str] = set()  # hashes of pictures sent from Windows (not phone copies)
        self.player: Optional[Any] = None  # phonyscreen.audio.AudioPlayer, when sound is on
        self.sound_wanted = True
        self.mute_phone_wanted = True  # mute the phone's own speaker while its sound plays here
        self._phone_muted = False
        self._clip_pull_evt: Optional[asyncio.Event] = None
        self._clip_change: Any = None  # pasteboard state last seen by pull_phone_clipboard
        self._audio_tasks: list[asyncio.Task[None]] = []
        self._clip_seq = 0
        # Typing what the virtual keyboard can't (ç, ş, ğ, @ via AltGr, ...) goes through the phone's clipboard:
        self._typed_texts: set[str] = set()  # texts put there to type them: never a copy on the phone
        # The phone clipboard's real text as far as we know (from Windows, a phone copy, or at start);
        # put back after typing. Not read back before typing: a read right after one of our writes
        # can return stale contents.
        self._clip_known: Optional[str] = None
        self._clip_restore_task: Optional[asyncio.Task[None]] = None

    # -- decode side ---------------------------------------------------------
    def plan_scale(self, w: int, h: int) -> tuple[int, int, int, int]:
        """Scale so the visible part of the frame (minus encoder padding) fits the window height."""
        cw, ch = vs.content_size(w, h, self._display_size)
        s = min(1.0, self.target_height / ch)
        # Width a multiple of 64 px -> RGB rows need no alignment padding.
        out_w = min(w // 64 * 64 or w, max(64, round(w * s / 64) * 64))
        s = out_w / w  # keep the aspect ratio exact
        out_h = max(2, int(h * s) // 2 * 2)
        view_w = min(out_w, round(cw * s))
        view_h = min(out_h, round(ch * s))
        return out_w, out_h, view_w, view_h

    def _new_transcoder(self, vps: bytes, sps: bytes, pps: bytes) -> Any:
        return LeanDecoder(self, vps, sps, pps)

    def _apply_display(self, display: dict[str, Any]) -> bool:
        """Also note the screen's backlight: "activeOn", "activeDimmed" (idle, before auto-lock),
        "inactiveOn" (always-on lock screen) or "off". The display monitor sees every change within
        ~1 s; the window's locked view follows it."""
        light = display.get("backlightState", self.backlight)
        if light != self.backlight:
            logger.info("phone screen: %s", light)
            self.backlight = light
        return super()._apply_display(display)

    def _fire_decoder_refresh(self, now: float, *, reason: str) -> None:
        """Replaces the base behaviour, which PAUSES decoding until the phone answers a
        keyframe request -- this phone rarely answers, so video froze. Here decoding
        continues; we ask once for a keyframe and let the watchdog restart the stream
        (which always yields a fresh keyframe) if none arrives within KEYFRAME_WAIT."""
        t = time.perf_counter()
        if not self.dirty_since:
            self.dirty_since = t
            logger.debug("references may be broken (%s) - requesting keyframe", reason)
        if t - self.last_pli_t > 0.5:
            self.last_pli_t = t
            task = asyncio.create_task(self._send_rtcp_pli())
            self._pli_tasks.add(task)
            task.add_done_callback(self._pli_tasks.discard)

    def deliver(self, data: bytes, w: int, h: int, vw: int, vh: int, t_in: float) -> None:
        self.last_frame_t = time.perf_counter()  # the watchdog's "picture still moving" signal
        with self.slot_lock:
            self.frame_seq += 1
            self.slot = (data, w, h, vw, vh, t_in, self.frame_seq)
        loop = self._loop
        if loop is not None:
            # Keeps the base class's keyframe-recovery bookkeeping and pointer
            # mapping (it maps clicks against _fb_width/_fb_height = view size).
            fb_w, fb_h, rot = self.display_map or (vw, vh, 0)
            loop.call_soon_threadsafe(self._broadcast_frame, data, fb_w, fb_h, rot)
        if not self.newframe_pending.is_set():
            self.newframe_pending.set()
            with contextlib.suppress(Exception):
                pygame.event.post(pygame.event.Event(NEWFRAME))

    # -- input side ------------------------------------------------------------
    def post_input(self, *event: Any) -> None:
        loop = self._loop
        if loop is not None and self.input_evt is not None:
            loop.call_soon_threadsafe(self._push_input, event)

    def _push_input(self, event: tuple[Any, ...]) -> None:
        self.input_buf.append(event)
        assert self.input_evt is not None
        self.input_evt.set()

    async def _input_worker(self) -> None:
        assert self.input_evt is not None
        buf = self.input_buf
        while True:
            if not buf:
                self.input_evt.clear()
                await self.input_evt.wait()
                continue
            ev = buf.popleft()
            # Coalesce drag moves: skip a move only when the next queued event is a
            # newer move, so the last position before a release is always sent.
            if ev[0] == "move" and buf and buf[0][0] == "move":
                continue
            # Same for multi-finger moves: skip if the next report has the same fingers, all still down.
            if (
                ev[0] == "multi"
                and buf
                and buf[0][0] == "multi"
                and all(c[3] for c in ev[1])
                and all(c[3] for c in buf[0][1])
                and [c[0] for c in ev[1]] == [c[0] for c in buf[0][1]]
            ):
                continue
            try:
                if ev[0] in ("move", "pointer"):
                    await self._paced_pointer(ev[1], ev[2], ev[3])
                elif ev[0] == "key":
                    await self._handle_key(self.client, ev[1], ev[2])
                elif ev[0] == "multi":
                    await self._send_multi(ev[1])
                elif ev[0] == "phone_mute_sync":
                    await self._sync_phone_mute()
                elif ev[0] == "swipe_up":
                    await self._swipe_up_from_bottom(ev[1])
                elif ev[0] == "app_switcher":
                    await self._open_app_switcher()
                elif ev[0] == "type_text":
                    text = ev[1]
                    while buf and buf[0][0] == "type_text":  # typed during the last paste: one paste
                        text += buf.popleft()[1]
                    await self._type_text(text)
                elif ev[0] == "clip_to_phone":
                    if self._clipboard_monitor is not None:
                        await self._clipboard_monitor.set_text(ev[1])
                        self._clip_known = ev[1]
                        await self._clipboard_set_done()  # a Ctrl+V right behind must paste this
                elif ev[0] == "clip_image_to_phone":
                    if self._clipboard_monitor is not None:
                        self._pushed_images.add(hashlib.sha1(ev[1]).hexdigest())
                        await self._clipboard_monitor.set_image(ev[1], "public.png")
                        self._clip_known = None  # typing ç/ş can't put a picture back
                        await self._clipboard_set_done()
                elif ev[0] == "button":
                    page, code = ev[1]
                    await self._send_hid_button(page, code, down=True)
                    await asyncio.sleep(0.05)
                    await self._send_hid_button(page, code, down=False)
            except Exception:
                logger.exception("input event failed: %r", ev[0])

    async def _paced_pointer(self, mask: int, x: int, y: int) -> None:
        """Send a pointer sample so drags look like a finger to iOS: samples at least ~16 ms
        apart, and big jumps filled in with intermediate points. pymobiledevice3's own swipe
        synthesis does the same (10 samples / ~200 ms); faster bursts are misread as flicks,
        which is why the Home swipe from the bottom edge didn't register."""
        last = self._touch_last
        if last is not None and (mask & 1 or self.client.pressed):
            lx, ly, lt = last
            step = max(4.0, self._fb_height * 0.02)
            n = min(12, int(max(abs(x - lx), abs(y - ly)) / step))
            for i in range(1, n):
                await self._wait_until(lt + TOUCH_INTERVAL)
                xi, yi = round(lx + (x - lx) * i / n), round(ly + (y - ly) * i / n)
                await self._handle_pointer(self.client, mask | 1, xi, yi)
                lt = time.perf_counter()
            await self._wait_until(lt + TOUCH_INTERVAL)
        was_pressed = self.client.pressed
        await self._handle_pointer(self.client, mask, x, y)
        self._touch_last = (x, y, time.perf_counter()) if mask & 1 else None
        await self._track_home_swipe(mask, x, y, was_pressed)

    async def _swipe_up_from_bottom(self, delay: float) -> None:
        """A finger-paced swipe up from the bottom edge (opens the passcode screen from the lock screen)."""
        if delay:
            await asyncio.sleep(delay)
        w, h = self._fb_width, self._fb_height
        if w <= 0 or h <= 0:
            return
        x, steps = w // 2, 12
        for i in range(steps + 1):
            y = round((h - 1) - (h * 0.45) * i / steps)
            await self._handle_pointer(self.client, 1, x, y)
            await asyncio.sleep(TOUCH_INTERVAL)
        await self._handle_pointer(self.client, 0, x, y)
        self._touch_last = None
        await asyncio.sleep(0.5)  # let the passcode field appear before digits arrive

    async def _open_app_switcher(self) -> None:
        """Home pressed twice quickly: iOS opens the App Switcher, as with a physical Home button."""
        for i in range(2):
            if i:
                await asyncio.sleep(APP_SWITCHER_GAP)
            await self._send_hid_button(*HOME_BUTTON, down=True)
            await asyncio.sleep(0.05)
            await self._send_hid_button(*HOME_BUTTON, down=False)

    async def _track_home_swipe(self, mask: int, x: int, y: int, was_pressed: bool) -> None:
        """iOS doesn't act on the Home swipe from these injected touches, so recognize it here:
        a drag that starts at the bottom edge and goes up quickly also presses Home on release;
        one that holds still before letting go opens the App Switcher (iOS's own gesture)."""
        h = max(1, self._fb_height)
        now = time.perf_counter()
        if mask & 1 and not was_pressed:
            self._swipe = [y / h, y / h, now, now, y]  # start v, min v, start t, last move t, last y
            return
        sw = getattr(self, "_swipe", None)
        if sw is None:
            return
        if mask & 1:
            if abs(y - sw[4]) >= 2:
                sw[3], sw[4] = now, y
            sw[1] = min(sw[1], y / h)
            return
        self._swipe = None
        start_v, end_v = sw[0], y / h
        if not (was_pressed and start_v >= 1.0 - HOME_SWIPE_EDGE and start_v - end_v >= HOME_SWIPE_DISTANCE):
            return
        if now - sw[3] > HOME_SWIPE_PAUSE:
            logger.info("bottom-edge swipe up and hold -> App Switcher")
            await asyncio.sleep(0.12)
            await self._open_app_switcher()
        elif now - sw[2] <= 1.5:
            logger.info("bottom-edge swipe up -> Home")
            await asyncio.sleep(0.12)
            await self._send_hid_button(*HOME_BUTTON, down=True)
            await asyncio.sleep(0.05)
            await self._send_hid_button(*HOME_BUTTON, down=False)

    @staticmethod
    async def _wait_until(t: float) -> None:
        delay = t - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _send_multi(self, contacts: list[tuple[int, int, int, bool]]) -> None:
        """contacts: (identity 0..4, x, y, touching) in window-picture pixels."""
        if self._fb_width <= 0 or self._fb_height <= 0:
            return
        await self._ensure_hid()
        assert self._uhs is not None and self._touch_service_id is not None
        out = []
        for ident, x, y, touching in contacts[:5]:
            u, v = vs.framebuffer_to_display(x, y, self._fb_width, self._fb_height, self._fb_rotation)
            out.append(
                TouchscreenContact(ident, max(0, min(65535, int(u * 65535))), max(0, min(65535, int(v * 65535))), touching)
            )
        await self._uhs.send_multitouch(out, service_id=self._touch_service_id)

    async def _start_lean_audio(self) -> None:
        """Ask the phone for its system audio (paired with the video session, as Xcode does),
        decode it with FFmpeg and play it on the PC."""
        from phonyscreen.audio import AacEldDecoder, AudioPlayer

        player = AudioPlayer()
        if not player.start():
            return
        svc = vs.DisplayService(self._rsd)
        await svc.connect()
        transport, receiver_ip = vs.open_media_receiver(svc, (4 * 1024 * 1024, 1024 * 1024))
        answer = await svc.start_audio_stream(
            receiver_ip=receiver_ip,
            receiver_port=transport.port,
            sender_ip=self._sender_ip,
            client_session_id=self._shared_session_id,
        )
        cfg = answer["connection"].get("streamConfig", {})
        source_port = int(cfg.get("SourcePort", 0))
        self._audio_local_ssrc = int(cfg.get("RemoteSSRC", 0))
        self._audio_remote_ssrc = int(cfg.get("LocalSSRC", 0))
        self._audio_rtcp_dest = (self._sender_ip, source_port) if source_port else None
        self._audio_transport, self._audio_svc, self.player = transport, svc, player
        decoder = AacEldDecoder()
        self._audio_tasks.append(asyncio.create_task(self._lean_audio_recv(transport, decoder, player)))
        if self._audio_rtcp_dest and self._audio_local_ssrc and self._audio_remote_ssrc:
            # receiver reports keep the phone from ending the audio session after ~20 s
            self._audio_tasks.append(asyncio.create_task(self._audio_rtcp_send_loop(transport)))
        logger.info("phone audio on")

    async def _lean_audio_recv(self, transport: Any, decoder: Any, player: Any) -> None:
        errors = 0
        while True:
            try:
                data = await transport.recv(65535)
            except (OSError, asyncio.CancelledError):
                return
            if len(data) < 12 or 64 <= (data[1] & 0x7F) <= 95:  # too short / RTCP
                continue
            seq = int.from_bytes(data[2:4], "big")
            cur = self._audio_rtp_highest_seq
            cycles, last16 = (cur >> 16) & 0xFFFF, cur & 0xFFFF
            if seq < last16 and last16 - seq > 0x8000:
                cycles = (cycles + 1) & 0xFFFF
            ext = (cycles << 16) | seq
            if cur == 0 or ((ext - cur) & 0xFFFFFFFF) < 0x80000000:
                self._audio_rtp_highest_seq = ext
            off = 12 + 4 * (data[0] & 0x0F)
            if data[0] & 0x10:
                off += 4 + 4 * int.from_bytes(data[off + 2 : off + 4], "big")
            try:
                player.push(decoder.decode(data[off:]))
                errors = 0
            except Exception as e:
                errors += 1
                if errors in (1, 50):
                    logger.debug("audio frame dropped: %s", e)

    async def _stop_lean_audio(self) -> None:
        for t in self._audio_tasks:
            t.cancel()
        for t in self._audio_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._audio_tasks.clear()
        for closer in (getattr(self._audio_transport, "close", None),):
            with contextlib.suppress(Exception):
                if closer:
                    closer()
        if self._audio_svc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._audio_svc.close(), timeout=2.0)
        if self.player is not None:
            self.player.close()
        self._audio_transport = self._audio_svc = self.player = None

    def set_sound(self, on: bool) -> None:
        self.sound_wanted = on
        if self.player is not None:
            self.player.set_enabled(on)
        self.post_input("phone_mute_sync")

    def set_mute_phone(self, on: bool) -> None:
        self.mute_phone_wanted = on
        self.post_input("phone_mute_sync")

    async def _sync_phone_mute(self) -> None:
        """Mute is a toggle on iOS: press it to mute while PC sound plays, press again to give the
        phone its sound back (its volume level is untouched). Captured audio isn't affected."""
        want = bool(self.mute_phone_wanted and self.sound_wanted and self.player is not None and self.player.ok)
        if want != self._phone_muted:
            await self._send_hid_button(*vs._BTN_MUTE, down=True)
            await asyncio.sleep(0.05)
            await self._send_hid_button(*vs._BTN_MUTE, down=False)
            self._phone_muted = want
            logger.info("iPhone speaker %s", "muted" if want else "unmuted")

    def pull_phone_clipboard(self, delay: float = 0.0) -> None:
        """Ask for a fresh look at the phone's clipboard (thread-safe), e.g. when PhonyScreen loses
        focus: the user may have copied something on the phone and is about to paste in Windows."""
        loop, evt = self._loop, self._clip_pull_evt
        if loop is not None and evt is not None:
            loop.call_soon_threadsafe(lambda: loop.call_later(delay, evt.set))

    async def _clip_pull_loop(self) -> None:
        from pymobiledevice3.remote.core_device import pasteboard_service as pb

        assert self._clip_pull_evt is not None
        first = True
        while True:
            if not first:
                await self._clip_pull_evt.wait()
            self._clip_pull_evt.clear()
            try:
                # allow_full_pull: see __init__; read_pasteboard bounds that pull at 90 s itself
                snapshot, content = await asyncio.wait_for(pb.read_pasteboard(self._rsd, allow_full_pull=True), 95.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info("couldn't read the phone clipboard: %s: %s", type(e).__name__, e)
                continue
            change = pb._change_id(snapshot)
            if first:  # what the phone holds at start isn't a new copy
                first, self._clip_change = False, change
                self._clip_known = self._clip_known or content.text
                continue
            if change == self._clip_change:
                continue
            self._clip_change = change
            self._on_device_clipboard(content)

    async def _type_text(self, text: str) -> None:
        """Type text the virtual keyboard can't (it sends US key positions, so no ç, ş, ğ, ı, emoji,
        and AltGr symbols depend on the phone's layout): put it on the phone's clipboard and press
        Cmd+V. The clipboard's real text comes back once typing pauses (RESTORE_AFTER)."""
        if self._clip_restore_task is not None and not self._clip_restore_task.done():
            self._clip_restore_task.cancel()  # still typing: restore after the last paste
        try:
            self._typed_texts.add(text)
            if len(self._typed_texts) > 500:
                self._typed_texts = {text}
            await self._set_phone_clipboard(text)
            await self._ensure_keyboard()
            assert self._uhs is not None and self._kb_service_id is not None
            held = set(self.client.pressed_keys)  # e.g. Shift for Ç: Cmd+Shift+V would be another command
            for keys in ({hs.KEY_LEFT_GUI}, {hs.KEY_LEFT_GUI, hs.KEY_V}, {hs.KEY_LEFT_GUI}):
                await self._uhs.send_keyboard(self._kb_service_id, keys)
                await asyncio.sleep(0.03)
            await self._uhs.send_keyboard(self._kb_service_id, held)
            logger.info("typed %d character(s) through the phone clipboard", len(text))
        finally:
            self._clip_restore_task = asyncio.create_task(self._restore_phone_clipboard())

    async def _set_phone_clipboard(self, text: str) -> None:
        """Put text on the phone's clipboard and wait until it's there (the monitor doesn't report
        its own changes as copies on the phone)."""
        monitor = self._clipboard_monitor
        if monitor is not None:
            await monitor.set_text(text)  # the monitor recognises its own changes
            await self._clipboard_set_done()
        else:
            from pymobiledevice3.remote.core_device import pasteboard_service as pb

            async with pb.PasteboardService(self._rsd) as service:
                await asyncio.wait_for(service.set_text(text), 3.0)

    async def _clipboard_set_done(self, timeout: float = 3.0) -> None:
        """Wait for the monitor's pending SETs: their SET_REPLY means the phone's clipboard holds it."""
        pending = list(self._clipboard_monitor._set_tasks) if self._clipboard_monitor is not None else []
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _restore_phone_clipboard(self) -> None:
        await asyncio.sleep(RESTORE_AFTER)
        if self._clip_known is not None:  # unknown (e.g. a picture): the typed text stays
            with contextlib.suppress(Exception):
                await self._set_phone_clipboard(self._clip_known)

    def _on_device_clipboard(self, content: Any) -> None:
        """Called by the pasteboard monitor when something is copied on the phone."""
        text = getattr(content, "text", None)
        if text in self._typed_texts:
            return  # put there to type it (possibly read back stale), not a copy on the phone
        image = getattr(content, "image", None)
        if image and not text:
            if hashlib.sha1(image).hexdigest() in self._pushed_images:
                return  # the picture Windows sent, read back
            logger.info("phone clipboard changed (picture, %d KB, %s)", len(image) // 1024, getattr(content, "image_uti", "?"))
            self._clip_known = None
            self._clip_seq += 1
            self.phone_image = (self._clip_seq, image)
            return
        logger.info("phone clipboard changed (%s)", f"{len(text)} chars of text" if text else "no text")
        if text:
            self._clip_known = text
        if text:
            self._clip_seq += 1
            self.phone_clip = (self._clip_seq, text)

    def request_restart(self, why: str, delay: float = 0.0) -> None:
        """Restart the video stream from any thread (e.g. shortly after waking the phone)."""
        loop = self._loop
        if loop is None:
            return

        def fire() -> None:
            asyncio.ensure_future(self._restart_stream(why))

        loop.call_soon_threadsafe(lambda: loop.call_later(delay, fire))

    # -- stream watchdog -----------------------------------------------------
    async def _restart_stream(self, why: str) -> None:
        logger.info("restarting stream: %s", why)
        self.restart_t = time.perf_counter()
        try:
            await self._switch_video()
            self.stats.restarts += 1
        except Exception as e:
            logger.warning("stream restart failed: %s", e)
        self.dirty_since = 0.0

    async def _watchdog(self) -> None:
        backoff = RESTART_BACKOFF
        while True:
            await asyncio.sleep(0.1)
            now = time.perf_counter()
            # 1) Broken references and the phone ignored our keyframe request.
            if self.dirty_since and now - self.dirty_since > KEYFRAME_WAIT:
                await self._restart_stream(f"no keyframe {now - self.dirty_since:.1f}s after a decode problem")
                continue
            # 2) No video at all (phone asleep, stream died). Each restart makes iOS post a
            #    "viewed remotely" notification, so back off while nothing comes through;
            #    clicking "wake" restarts right away (request_restart). Every restart
            #    opens with a keyframe burst even from an asleep phone, so the back-off
            #    resets only once frames still arrive STEADY_SECONDS after the restart.
            idle = now - self.last_feed_t
            # 3) Data keeps coming but no picture comes out (e.g. packets lost while a big
            #    clipboard picture crossed the tunnel, and decoding never resynced). Check 1
            #    misses it once a keyframe *arrives*, check 2 because data arrives.
            #    Counted from when data resumed: after a normal pause the first new picture
            #    takes a moment, and the picture before the pause is old by then.
            frozen = now - max(self.last_frame_t, self.feed_since)
            if idle < 1.0 and frozen > FROZEN_SECONDS and now - self.restart_t >= max(FROZEN_SECONDS, backoff):
                await self._restart_stream(f"video data but no picture for {frozen:.1f}s")
                backoff = min(backoff * 2, 30.0)
                continue
            if idle < STALL_SECONDS:
                self.status = ""
                if self.last_frame_t - self.restart_t >= STEADY_SECONDS:  # pictures, not just data
                    backoff = RESTART_BACKOFF
                continue
            if now - self.restart_t >= backoff:  # counted from any restart, "wake" included
                await self._restart_stream(f"no video for {idle:.1f}s (next try in {min(backoff * 2, 30):.0f}s)")
                backoff = min(backoff * 2, 30.0)

    # -- lifecycle ---------------------------------------------------------------
    async def run(self) -> None:
        vs.require_video_stream_support(self._rsd)
        await vs.require_service(self._rsd, vs.DisplayService.SERVICE_NAME)
        self._loop = asyncio.get_running_loop()
        self.input_evt = asyncio.Event()
        self.stop_event = asyncio.Event()
        try:
            async with vs.DeviceInfoService(self._rsd) as info:
                display = vs.select_display(await info.get_display_info(), None)
            self._apply_display(display)
        except Exception:
            logger.warning("couldn't query displays; using display %d", self._display_id)
        await self._start_video()
        self._clients.add(self.client)
        if self.sound_wanted:
            try:
                await self._start_lean_audio()
            except Exception as e:  # sound is a nice-to-have; never block mirroring on it
                logger.warning("phone audio unavailable: %s", e)
        self._clip_pull_evt = asyncio.Event()
        self._audio_tasks.append(asyncio.create_task(self._clip_pull_loop()))
        if self.sound_wanted and self.player is not None:
            with contextlib.suppress(Exception):
                await self._sync_phone_mute()
        if self._clipboard_monitor is not None:
            try:
                await self._clipboard_monitor.start()
            except Exception as e:  # clipboard sync is a nice-to-have; never block mirroring on it
                logger.warning("clipboard sync unavailable: %s", e)
                self._clipboard_monitor = None
        # Note: the base class's _decoder_refresh_loop is deliberately NOT started --
        # its periodic "refresh" pauses decoding and waits for a keyframe the phone
        # rarely sends (that was the 7-second freeze).
        tasks = [
            asyncio.create_task(self._display_monitor_loop()),
            asyncio.create_task(self._input_worker()),
            asyncio.create_task(self._watchdog()),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if self._clipboard_monitor is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._clipboard_monitor.stop(), timeout=3.0)
            if self._phone_muted:  # give the phone its sound back
                self.mute_phone_wanted = False
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._sync_phone_mute(), 2.0)
            await self._stop_lean_audio()
            await self._stop_hid()
            async with self._video_lock:
                await self._stop_video()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(vs.DisplayService.stop_all_streams(self._rsd), timeout=6.0)

    def request_stop(self) -> None:
        if self._loop is not None and self.stop_event is not None:
            self._loop.call_soon_threadsafe(self.stop_event.set)


def run_backend(
    box: dict[str, Any],
    target_height: int,
    ready: threading.Event,
    rsd_addr: Optional[tuple[str, int]],
    prepare: Optional[Callable[[Callable[[str], None]], Optional[tuple[str, int]]]] = None,
) -> None:
    def set_status(text: str) -> None:
        box["status"] = text
        with contextlib.suppress(Exception):
            pygame.event.post(pygame.event.Event(NEWFRAME))

    async def main() -> None:
        addr = rsd_addr
        if prepare is not None:
            # Launcher work (mount, tunnel) runs here so its progress shows in the window.
            addr = await asyncio.to_thread(prepare, set_status) or addr
        set_status("Starting video...")
        if addr is not None:
            # A tunnel started by hand, e.g. `lockdown start-tunnel --mobdev2-host <phone IP>` (Wi-Fi).
            rsd = RemoteServiceDiscoveryService(addr)
            await rsd.connect()
        else:
            rsds = await get_tunneld_devices()
            if not rsds:
                raise RuntimeError(
                    "tunneld has no device. Is `pymobiledevice3 remote tunneld` running and the iPhone reachable?"
                )
            rsd = rsds[0]
            for extra in rsds[1:]:
                with contextlib.suppress(Exception):
                    await extra.close()
        mirror = LeanMirror(rsd, target_height)
        with contextlib.suppress(Exception):
            from phonyscreen.config import load_config

            saved = load_config().get("settings") or {}
            mirror.sound_wanted = bool(saved.get("sound", True))
            mirror.mute_phone_wanted = bool(saved.get("mute_phone", True))
        box["mirror"] = mirror
        ready.set()
        try:
            await mirror.run()
        finally:
            with contextlib.suppress(Exception):
                await rsd.close()

    try:
        asyncio.run(main())
    except Exception as e:
        box["error"] = e
        set_status(f"Couldn't connect: {e}")
        logger.exception("backend stopped")
    finally:
        ready.set()
        with contextlib.suppress(Exception):
            pygame.event.post(pygame.event.Event(NEWFRAME))


# -- window chrome -----------------------------------------------------------------
BAR_H = 30  # floating pill bar above the phone
BAR_GAP = 6
CORNER_FRACTION = 0.135  # iPhone screen corner radius as a fraction of its short side
EDGE_MARGIN = 0.03  # touches starting this close to an edge snap to it (fraction of size)
# The locked-phone view follows the phone's own backlight state when it reports one (LeanMirror.backlight).
# The stream pauses for a few seconds now and then while the phone is in use, so missing video alone
# isn't "locked":
WAIT_SECONDS = 10.0  # screen on but no video this long -> "Waiting for your iPhone"
IDLE_SECONDS = 5.0  # backlight unknown: no video this long -> locked-phone view
BLACK_SECONDS = 2.0  # backlight unknown: only pure-black frames this long -> screen off -> locked-phone view
DARK_LEVEL = 14  # average byte value below which a frame counts as "screen off"
PILL_LEFT = ("home", "switcher", "rotate", "keypad", "settings")
DEFAULT_SETTINGS = {"size": 0.70, "scroll": 0.45, "pinch_speed": 0.5, "sound": True, "mute_phone": True, "clipboard": True, "stats": False}
SETTING_ROWS = (  # (key, label, kind, min, max, step)
    ("size", "Phone size", "pct", 0.35, 0.95, 0.05),
    ("scroll", "Scroll speed", "pct", 0.0, 1.0, 0.05),
    ("pinch_speed", "Pinch speed", "pct", 0.1, 2.0, 0.1),
    ("sound", "Sound on this PC", "bool", 0, 0, 0),
    ("mute_phone", "Mute iPhone speaker", "bool", 0, 0, 0),
    ("clipboard", "Share clipboard", "bool", 0, 0, 0),
    ("stats", "Show stats", "bool", 0, 0, 0),
)
PILL_RIGHT = ("minimize", "close")
KEYPAD_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "del")


def phone_origin_y() -> int:
    return BAR_H + BAR_GAP


def pill_rect(win_w: int) -> pygame.Rect:
    pw = min(win_w, max(270, int(win_w * 0.6)))
    return pygame.Rect((win_w - pw) // 2, 0, pw, BAR_H)


def pill_buttons(win_w: int) -> dict[str, pygame.Rect]:
    pr = pill_rect(win_w)
    out: dict[str, pygame.Rect] = {}
    for i, name in enumerate(PILL_LEFT):
        out[name] = pygame.Rect(pr.left + 6 + i * BAR_H, 0, BAR_H, BAR_H)
    for i, name in enumerate(reversed(PILL_RIGHT)):
        out[name] = pygame.Rect(pr.right - 6 - (i + 1) * BAR_H, 0, BAR_H, BAR_H)
    return out



def snap_to_edges(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    """iOS starts system gestures (Home, Control Center, back) only from the very edge."""
    mx = max(6, round(w * EDGE_MARGIN))
    my = max(6, round(h * EDGE_MARGIN * 0.6))
    if x < mx:
        x = 0
    elif x > w - 1 - mx:
        x = w - 1
    if y < my:
        y = 0
    elif y > h - 1 - my:
        y = h - 1
    return x, y


def frame_is_dark(data: bytes) -> bool:
    sample = data[:: max(1, len(data) // 600)]
    return (sum(sample) / max(1, len(sample))) < DARK_LEVEL


def frame_is_black(data: bytes) -> bool:
    """Screen off: a locked iPhone keeps streaming, but every pixel is (almost) pure black.
    Strict on purpose: dark mode, dark wallpapers and the lock screen are never this black."""
    sample = data[:: max(1, len(data) // 4000)]
    return max(sample, default=0) <= 24 and (sum(sample) / max(1, len(sample))) < 3


PASSCODE_IDLE = 8.0  # seconds after which iOS has probably left the passcode screen
KEYPAD_HEADER = 80  # hint line + "Show passcode screen" button above the digits


def keypad_layout(w: int, h: int) -> tuple[pygame.Rect, dict[str, pygame.Rect]]:
    """Number pad over the lower part of the phone picture (w x h), in picture coordinates."""
    panel = pygame.Rect(int(w * 0.08), int(h * 0.32), int(w * 0.84), int(h * 0.60))
    head = KEYPAD_HEADER
    cols, rows = 3, 4
    cw, rh = panel.width / cols, (panel.height - head) / rows
    size = int(min(cw, rh) * 0.78)
    keys: dict[str, pygame.Rect] = {}
    for i, label in enumerate(KEYPAD_KEYS):
        if not label:
            continue
        cx = panel.left + cw * (i % cols + 0.5)
        cy = panel.top + head + rh * (i // cols + 0.5)
        keys[label] = pygame.Rect(int(cx - size / 2), int(cy - size / 2), size, size)
    bw = int(panel.width * 0.8)
    keys["show"] = pygame.Rect(panel.centerx - bw // 2, panel.top + 36, bw, 34)
    return panel, keys


def _settings_metrics(h: int) -> tuple[int, int, int]:
    """(row height, header height, footer height): roomy in portrait, compact in a short window."""
    n = len(SETTING_ROWS)
    if 60 + 54 * n + 70 + 16 <= h:
        return 54, 60, 70
    return max(30, min(54, (h - 16 - 44 - 52) // n)), 44, 52


def settings_layout(w: int, h: int) -> tuple[pygame.Rect, dict[str, pygame.Rect]]:
    """Settings panel in phone-picture coordinates: panel rect and hit rects ('<key>-'/'<key>+'/'<key>', 'done')."""
    row_h, head, foot = _settings_metrics(h)
    ph = head + row_h * len(SETTING_ROWS) + foot
    pw = min(int(w * 0.86), 520)
    panel = pygame.Rect((w - pw) // 2, max(8, (h - ph) // 2), pw, ph)
    b = min(38, row_h - 4)  # +/- button size
    hits: dict[str, pygame.Rect] = {}
    for i, (key, _label, kind, *_rest) in enumerate(SETTING_ROWS):
        cy = panel.top + head + i * row_h + row_h // 2
        if kind == "pct":
            hits[key + "-"] = pygame.Rect(panel.right - 150, cy - b // 2, b, b)
            hits[key + "+"] = pygame.Rect(panel.right - 14 - b, cy - b // 2, b, b)
        else:
            th = min(34, row_h - 6)
            hits[key] = pygame.Rect(panel.right - 100, cy - th // 2, 2 * th, th)
    dh = min(40, foot - 12)
    hits["done"] = pygame.Rect(panel.centerx - 60, panel.bottom - foot + (foot - dh) // 2, 120, dh)
    return panel, hits


SCROLL_MIN, SCROLL_MAX = 0.10, 0.50  # what 0% and 100% on the Scroll speed row mean


def scroll_factor(settings: dict) -> float:
    return SCROLL_MIN + (SCROLL_MAX - SCROLL_MIN) * float(settings["scroll"])


def load_settings(saved: Optional[dict]) -> dict:
    saved = dict(saved or {})
    if "scroll" not in saved and "scroll_speed" in saved:  # older builds stored the raw factor
        old = float(saved.pop("scroll_speed"))
        saved["scroll"] = round(min(1.0, max(0.0, (old - SCROLL_MIN) / (SCROLL_MAX - SCROLL_MIN))) / 0.05) * 0.05
    saved.pop("scroll_speed", None)
    return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}


def apply_setting(settings: dict, hit: str) -> bool:
    """Change a setting for a click on ``hit``; True if something changed."""
    for key, _label, kind, lo, hi, step in SETTING_ROWS:
        if kind == "pct" and hit in (key + "-", key + "+"):
            delta = step if hit.endswith("+") else -step
            settings[key] = round(min(hi, max(lo, settings[key] + delta)), 2)
            return True
        if kind == "bool" and hit == key:
            settings[key] = not settings[key]
            return True
    return False


_OVERLAYS: dict[tuple, pygame.Surface] = {}


def cached_overlay(key: tuple, w: int, h: int, build: Callable[[pygame.Surface], None]) -> pygame.Surface:
    """A transparent w x h surface drawn by ``build`` once per ``key`` (overlays are redrawn every
    video frame; building them takes a few ms, blitting a ready one almost nothing)."""
    surf = _OVERLAYS.get(key)
    if surf is None:
        if len(_OVERLAYS) > 32:
            _OVERLAYS.clear()
        surf = pygame.Surface((max(1, w), max(1, h)), pygame.SRCALPHA)
        build(surf)
        _OVERLAYS[key] = surf
    return surf


def text_size(h: int) -> int:
    """Body text size for a phone picture h px tall."""
    return max(14, h // 62)


def draw_settings(screen: pygame.Surface, y0: int, w: int, h: int, settings: dict) -> None:
    key = ("settings", w, h, tuple(sorted(settings.items())))
    screen.blit(cached_overlay(key, w, h, lambda s: _build_settings(s, w, h, settings)), (0, y0))


def _build_settings(s: pygame.Surface, w: int, h: int, settings: dict) -> None:
    panel, hits = settings_layout(w, h)
    row_h, head, _foot = _settings_metrics(h)
    ts = text_size(h)
    s.fill((0, 0, 0, 130))
    s.blit(style.round_rect(panel.w, panel.h, 20, style.PANEL), panel.topleft)
    s.blit(style.round_rect(panel.w, panel.h, 20, style.PANEL_EDGE, 1), panel.topleft)
    title = style.text("Settings", ts + 4, style.TEXT, "semibold")
    s.blit(title, (panel.left + 20, panel.top + (head - title.get_height()) // 2))
    for i, (key, label, kind, *_rest) in enumerate(SETTING_ROWS):
        top = panel.top + head + i * row_h
        cy = top + row_h // 2
        if i:
            pygame.draw.line(s, style.HAIRLINE, (panel.left + 20, top), (panel.right - 20, top))
        img = style.text(label, ts, style.TEXT)
        s.blit(img, (panel.left + 20, cy - img.get_height() // 2))
        if kind == "pct":
            for sign, name in (("-", "minus"), ("+", "plus")):
                r = hits[key + sign]
                style.blit_center(s, style.circle(r.width, style.CONTROL), r.center)
                style.blit_center(s, style.icon(name, int(r.width * 0.5), style.TEXT), r.center)
            mid = (hits[key + "-"].right + hits[key + "+"].left) // 2
            style.blit_center(s, style.text(f"{round(settings[key] * 100)}%", ts, style.MUTED), (mid, cy))
        else:
            r = hits[key]
            on = bool(settings[key])
            s.blit(style.round_rect(r.w, r.h, r.h / 2, style.GREEN if on else style.CONTROL_OFF), r.topleft)
            kd = r.h - 6
            kx = r.right - 3 - kd / 2 if on else r.left + 3 + kd / 2
            style.blit_center(s, style.circle(kd + 2, (0, 0, 0, 70)), (kx, cy + 1))  # knob shadow
            style.blit_center(s, style.circle(kd, (255, 255, 255)), (kx, cy))
    d = hits["done"]
    s.blit(style.round_rect(d.w, d.h, d.h / 2, style.ACCENT), d.topleft)
    style.blit_center(s, style.text("Done", ts, (255, 255, 255), "semibold"), d.center)


class ScrollGesture:
    """Turns mouse-wheel / touchpad scrolling into a one-finger drag on the phone."""

    RELEASE_AFTER = 0.12
    HOME_ZONE = 0.06  # scrolling that starts with the pointer this close to the bottom = Home swipe

    def __init__(self) -> None:
        self.active = False
        self.pos = (0.0, 0.0)
        self.last = 0.0
        self.home: Optional[list] = None  # [dx so far, dy so far, Home sent] while a Home swipe is in progress
        self.home_quiet_until = 0.0  # swallow touchpad momentum after a Home swipe (no double Home)

    def feed(self, start: tuple[int, int], dx: float, dy: float, w: int, h: int, now: float) -> list[tuple]:
        """Returns input events: ('pointer'|'move', mask, x, y), or ('button', HOME) for a Home swipe."""
        # stay well clear of iOS's edge-gesture zones (Home, Notification/Control Center, Back)
        mx, my = max(14, round(w * 0.05)), max(14, round(h * 0.05))
        events: list[tuple] = []
        if not self.active and now < self.home_quiet_until:
            return []
        if not self.active and start[1] >= h * (1 - self.HOME_ZONE) and abs(dy) >= abs(dx):
            # two fingers swiping with the pointer on the bottom edge: like swiping up from the
            # phone's bottom edge. Whichever way Windows maps the scroll direction, it means Home.
            self.active, self.home = True, [0.0, 0.0, False]
        if self.home is not None:
            self.last = now
            if self.home[2]:
                self.home_quiet_until = now + 1.0
            self.home[0] += dx
            self.home[1] += dy
            if not self.home[2] and abs(self.home[1]) >= h * 0.08 and abs(self.home[1]) > abs(self.home[0]):
                self.home[2] = True
                self.home_quiet_until = now + 1.0
                logger.info("touchpad swipe from the bottom edge -> Home")
                return [("button", HOME_BUTTON)]
            return []  # no touches while it's a Home swipe
        if not self.active:
            x = min(max(start[0], mx), w - 1 - mx)
            y = min(max(start[1], my), h - 1 - my)
            self.pos, self.active = (float(x), float(y)), True
            events.append(("pointer", 1, x, y))
        nx, ny = self.pos[0] + dx, self.pos[1] + dy
        if not (mx <= nx <= w - 1 - mx and my <= ny <= h - 1 - my):
            # reached the edge of the screen: lift, and the next tick starts a new drag
            events.append(("pointer", 0, int(self.pos[0]), int(self.pos[1])))
            self.active = False
            return events
        self.pos, self.last = (nx, ny), now
        events.append(("move", 1, int(nx), int(ny)))
        return events

    def tick(self, now: float) -> list[tuple]:
        if self.active and now - self.last > self.RELEASE_AFTER:
            self.active = False
            if self.home is not None:
                self.home = None
                return []
            return [("pointer", 0, int(self.pos[0]), int(self.pos[1]))]
        return []


class PinchGesture:
    """Ctrl+wheel (what a touchpad pinch sends on Windows) -> two fingers pinching on the phone."""

    RELEASE_AFTER = 0.15

    def __init__(self) -> None:
        self.active = False
        self.center = (0, 0)
        self.dist = 0.0
        self.last = 0.0
        self.limits = (20.0, 200.0)

    def _contacts(self, touching: bool) -> list[tuple[int, int, int, bool]]:
        cx, cy = self.center
        half = self.dist / 2
        return [(0, int(cx - half), cy, touching), (1, int(cx + half), cy, touching)]

    def feed(self, center: tuple[int, int], amount: float, w: int, h: int, now: float, speed: float = 1.0) -> list[tuple]:
        short = min(w, h)
        amount *= speed
        if not self.active:
            self.limits = (24.0, short * 0.8)
            self.dist = short * 0.25
            half = self.limits[1] / 2 + 2
            self.center = (int(min(max(center[0], half), w - 1 - half)), int(min(max(center[1], 30), h - 31)))
            self.active = True
        self.dist = min(max(self.dist * (1.12 ** amount), self.limits[0]), self.limits[1])
        self.last = now
        return [("multi", self._contacts(True))]

    def tick(self, now: float) -> list[tuple]:
        if self.active and now - self.last > self.RELEASE_AFTER:
            self.active = False
            return [("multi", self._contacts(False))]
        return []


class FingerTracker:
    """Touchscreen fingers -> stable contact identities 0..4 in picture coordinates."""

    def __init__(self) -> None:
        self.contacts: dict[int, tuple[int, int, int]] = {}  # finger_id -> (identity, x, y)
        # lone finger that started on the bottom edge: [finger, start y, picture h, start t, last move t, last y]
        self.solo: Optional[list] = None
        self.home_swipe = False  # set by up() when that finger swiped up: the caller presses Home
        self.app_switcher = False  # set by up() when it swiped up and held still: the caller opens the App Switcher

    def down(self, finger: int, x: int, y: int, w: int, h: int) -> Optional[list[tuple[int, int, int, bool]]]:
        used = {c[0] for c in self.contacts.values()}
        free = [i for i in range(5) if i not in used]
        if not free:
            return None
        if not self.contacts:  # a lone finger near the edge starts an edge gesture
            x, y = snap_to_edges(x, y, w, h)
            now = time.perf_counter()
            self.solo = [finger, y, max(1, h), now, now, y] if y >= h * (1 - HOME_SWIPE_EDGE) else None
        else:
            self.solo = None  # a second finger: not a Home swipe
        self.contacts[finger] = (free[0], x, y)
        return self.report()

    def move(self, finger: int, x: int, y: int) -> Optional[list[tuple[int, int, int, bool]]]:
        if finger not in self.contacts:
            return None
        ident = self.contacts[finger][0]
        self.contacts[finger] = (ident, x, y)
        if self.solo and self.solo[0] == finger and abs(y - self.solo[5]) >= 2:
            self.solo[4], self.solo[5] = time.perf_counter(), y
        return self.report()

    def up(self, finger: int) -> Optional[list[tuple[int, int, int, bool]]]:
        c = self.contacts.pop(finger, None)
        if c is None:
            return None
        sw, now = self.solo, time.perf_counter()
        if sw and sw[0] == finger:
            self.solo = None
            # same rule as the mouse: up at least 10% of the screen; held still before lifting =
            # App Switcher, otherwise (quickly, no pause) = Home
            if (sw[1] - c[2]) / sw[2] >= HOME_SWIPE_DISTANCE:
                if now - sw[4] > HOME_SWIPE_PAUSE:
                    self.app_switcher = True
                elif now - sw[3] <= 1.5:
                    self.home_swipe = True
        return self.report() + [(c[0], c[1], c[2], False)]

    def report(self) -> list[tuple[int, int, int, bool]]:
        return [(i, x, y, True) for i, x, y in self.contacts.values()]


def apply_window_shape(win_w: int, phone_w: int, phone_h: int) -> None:
    """Rounded, iPhone-shaped window plus the floating pill (Windows only)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        hwnd = pygame.display.get_wm_info()["window"]
        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        y0 = phone_origin_y()
        d = max(8, int(min(phone_w, phone_h) * CORNER_FRACTION * 2))
        phone = gdi32.CreateRoundRectRgn(0, y0, phone_w + 1, y0 + phone_h + 1, d, d)
        pr = pill_rect(win_w)
        pill = gdi32.CreateRoundRectRgn(pr.left, pr.top, pr.right + 1, pr.bottom + 1, BAR_H, BAR_H)
        gdi32.CombineRgn(phone, phone, pill, 2)  # RGN_OR
        gdi32.DeleteObject(pill)
        user32.SetWindowRgn(hwnd, phone, True)  # Windows now owns the region


def start_window_drag() -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        hwnd = pygame.display.get_wm_info()["window"]
        ctypes.windll.user32.ReleaseCapture()
        ctypes.windll.user32.SendMessageW(hwnd, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION


def window_position() -> Optional[tuple[int, int]]:
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        import ctypes
        import ctypes.wintypes

        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(pygame.display.get_wm_info()["window"], ctypes.byref(rect))
        return rect.left, rect.top
    return None


def position_on_screen(x: int, y: int) -> bool:
    if sys.platform != "win32":
        return True
    with contextlib.suppress(Exception):
        import ctypes

        gsm = ctypes.windll.user32.GetSystemMetrics
        vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)  # virtual screen (all monitors)
        return vx <= x <= vx + vw - 100 and vy <= y <= vy + vh - 100
    return False


PILL_TIPS = {
    "home": "Home  ·  F1", "switcher": "App Switcher  ·  F4", "rotate": "Rotate", "keypad": "Passcode keypad",
    "settings": "Settings", "minimize": "Minimize", "close": "Close",
}


def draw_pill(screen: pygame.Surface, win_w: int, hover: Optional[str], keypad_on: bool, settings_on: bool = False) -> None:
    pr = pill_rect(win_w)
    key = ("pill", win_w, hover, keypad_on, settings_on)
    screen.blit(cached_overlay(key, pr.w, pr.h, lambda s: _build_pill(s, win_w, hover, keypad_on, settings_on)), pr.topleft)
    tip = PILL_TIPS.get(hover or "")
    if tip:  # over the top of the phone picture, below the Dynamic Island (~0.1 of the width deep)
        img = style.toast(tip, 13, bg=(18, 18, 20, 235))
        x = min(max(8, pill_buttons(win_w)[hover].centerx - img.get_width() // 2), win_w - img.get_width() - 8)  # type: ignore[index]
        screen.blit(img, (x, phone_origin_y() + int(win_w * 0.12)))


def _build_pill(s: pygame.Surface, win_w: int, hover: Optional[str], keypad_on: bool, settings_on: bool) -> None:
    pr = pill_rect(win_w)
    s.blit(style.round_rect(pr.w, pr.h, pr.h / 2, style.PILL), (0, 0))
    btns = pill_buttons(win_w)
    for name, r in btns.items():
        center = (r.centerx - pr.left, r.centery)
        active = (name == "keypad" and keypad_on) or (name == "settings" and settings_on)
        bg = (style.CLOSE_HOVER if name == "close" else style.PILL_HOVER) if hover == name else style.PILL_ACTIVE if active else None
        if bg:
            style.blit_center(s, style.circle(BAR_H - 6, bg), center)
        style.blit_center(s, style.icon(name, 18, style.TEXT), center)
    gx = (btns[PILL_LEFT[-1]].right + btns["minimize"].left) // 2 - pr.left  # grip: drag elsewhere on the pill to move
    for i in (-1, 0, 1):
        style.blit_center(s, style.circle(4, (112, 112, 118)), (gx + i * 7, pr.h / 2))


KEYPAD_LETTERS = {"2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ"}


def draw_keypad(screen: pygame.Surface, y0: int, w: int, h: int) -> None:
    screen.blit(cached_overlay(("keypad", w, h), w, h, lambda s: _build_keypad(s, w, h)), (0, y0))


def _build_keypad(s: pygame.Surface, w: int, h: int) -> None:
    panel, keys = keypad_layout(w, h)
    ts = text_size(h)
    s.fill((0, 0, 0, 150))
    style.blit_center(s, style.text("Enter Passcode", ts + 4, style.TEXT, "semibold"), (panel.centerx, panel.top + 16))
    for label, r in keys.items():
        if label == "show":  # iOS may time the passcode screen out; this brings it back
            s.blit(style.round_rect(r.w, r.h, r.h / 2, (255, 255, 255, 46)), r.topleft)
            style.blit_center(s, style.text("Show passcode screen", ts, style.TEXT, "semibold"), r.center)
        elif label == "del":
            style.blit_center(s, style.icon("backspace", int(r.width * 0.42), style.TEXT), r.center)
        else:
            style.blit_center(s, style.circle(r.width, (255, 255, 255, 46)), r.center)
            letters = KEYPAD_LETTERS.get(label)
            digit = style.text(label, int(r.width * 0.40), style.TEXT)
            if letters:
                style.blit_center(s, digit, (r.centerx, r.centery - r.height * 0.09))
                style.blit_center(s, style.text(letters, max(9, int(r.width * 0.115)), style.TEXT, "semibold"),
                                  (r.centerx, r.centery + r.height * 0.24))
            else:
                style.blit_center(s, digit, r.center)
    style.blit_center(s, style.text("You can also type it on your keyboard", max(12, ts - 1), (200, 200, 208)),
                      (panel.centerx, min(h - 30, panel.bottom + 20)))


def privacy_blur(picture: pygame.Surface) -> pygame.Surface:
    """A heavily blurred, darkened copy: colours only, nothing readable (no messages on show)."""
    w, h = picture.get_size()
    tiny = pygame.transform.smoothscale(picture, (max(2, w // 96), max(2, h // 96)))  # ~6 x 13 colour blocks
    soft = pygame.transform.smoothscale(tiny, (max(2, w // 24), max(2, h // 24)))  # spread them out
    out = pygame.transform.smoothscale(soft, (w, h))
    shade = pygame.Surface((w, h), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 120))
    out.blit(shade, (0, 0))
    return out


def draw_locked(screen: pygame.Surface, y0: int, w: int, h: int, title: str) -> None:
    screen.blit(cached_overlay(("locked", w, h, title), w, h, lambda s: _build_locked(s, w, h, title)), (0, y0))


def _build_locked(s: pygame.Surface, w: int, h: int, title: str) -> None:
    cx, cy = w // 2, int(h * 0.44)
    d = max(76, min(w, h) // 4)
    style.blit_center(s, style.circle(d, (255, 255, 255, 38)), (cx, cy))  # frosted disc
    style.blit_center(s, style.round_rect(d, d, d / 2, (255, 255, 255, 90), 1.5), (cx, cy))
    style.blit_center(s, style.icon("phone" if title.startswith("Waiting") else "lock", int(d * 0.46), style.TEXT), (cx, cy))
    head = style.text(title, max(20, h // 36), style.TEXT, "semibold")
    y = cy + d // 2 + 22
    style.blit_center(s, head, (cx, y + head.get_height() // 2))
    sub = style.text("Click to wake", text_size(h) + 1, (222, 222, 230))
    style.blit_center(s, sub, (cx, y + head.get_height() + 6 + sub.get_height() // 2))


def draw_connecting(screen: pygame.Surface, y0: int, w: int, h: int, status: str, failed: bool, t: float, body: bool = True) -> None:
    """Shown until the first video frame: app icon and name, then progress (spinner) or the error."""
    screen.blit(style.vertical_gradient(w, h, (36, 36, 40), (14, 14, 16)), (0, y0))
    if not body:
        return
    cx, cy = w // 2, y0 + int(h * 0.36)
    size = max(64, w // 6)
    style.blit_center(screen, app_icon(size), (cx, cy))
    name = style.text("PhonyScreen", max(22, h // 32), style.TEXT, "semibold")
    y = cy + size // 2 + 16
    screen.blit(name, (cx - name.get_width() // 2, y))
    y += name.get_height() + 30
    ts = text_size(h)
    if failed:
        style.blit_center(screen, style.icon("warning", 30, style.WARN), (cx, y + 15))
        y += 42
        head = style.text("Couldn't connect", ts + 2, style.TEXT, "semibold")
        screen.blit(head, (cx - head.get_width() // 2, y))
        y += head.get_height() + 6
        status = status.removeprefix("Couldn't connect: ")
    else:
        style.blit_center(screen, style.spinner(24, int(t * 12) % 12, style.MUTED), (cx, y + 12))
        y += 40
    for line in style.wrap(status, ts, w - 72):
        img = style.text(line, ts, style.MUTED)
        screen.blit(img, (cx - img.get_width() // 2, y))
        y += img.get_height() + 2


def main(
    argv: Optional[list[str]] = None,
    prepare: Optional[Callable[[Callable[[str], None]], Optional[tuple[str, int]]]] = None,
) -> int:
    """Open the viewer. ``prepare(set_status)`` (from the launcher) runs first in the background and
    may return an ``(host, port)`` tunnel address; its progress and errors show in the window."""
    from phonyscreen.config import load_config, update_config

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--height", type=int, default=0, help="phone height in pixels (default: remembered, or ~85%% of screen)")
    parser.add_argument(
        "--rsd", nargs=2, metavar=("HOST", "PORT"),
        help="use this tunnel instead of tunneld (the --rsd HOST PORT printed by `lockdown start-tunnel`)",
    )
    parser.add_argument("--stats", action="store_true", help="show the stats overlay at start (F3 toggles)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    with contextlib.suppress(Exception):  # a log file to look at when something misbehaves
        from logging.handlers import RotatingFileHandler

        from phonyscreen.config import CONFIG_PATH

        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(CONFIG_PATH.parent / "phonyscreen.log", maxBytes=2_000_000, backupCount=1, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("pymobiledevice3").setLevel(logging.INFO if args.verbose else logging.WARNING)
    if args.verbose:  # keyframe requests, decoder rebuilds, RPS gaps, ingress stats
        logging.getLogger("pymobiledevice3.remote.core_device.vnc_server").setLevel(logging.DEBUG)

    if sys.platform == "win32":  # real pixels, not Windows-scaled blur
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)

    cfg = load_config()
    pos = cfg.get("window_pos")
    if isinstance(pos, list) and len(pos) == 2 and position_on_screen(int(pos[0]), int(pos[1])):
        os.environ["SDL_VIDEO_WINDOW_POS"] = f"{int(pos[0])},{int(pos[1])}"
    os.environ.setdefault("SDL_TOUCH_MOUSE_EVENTS", "0")  # touchscreen handled as real fingers

    pygame.init()
    y0 = phone_origin_y()
    screen_h = pygame.display.Info().current_h
    max_h = max(400, screen_h - y0 - 40)
    settings = load_settings(cfg.get("settings"))
    target_h = args.height or int((screen_h - y0) * float(settings["size"]))
    target_h = min(max(target_h, 400), max_h)
    placeholder = (int(target_h * 0.46), target_h)
    win_size = (placeholder[0], y0 + placeholder[1])
    screen = pygame.display.set_mode(win_size, pygame.NOFRAME)
    pygame.display.set_caption("PhonyScreen")
    with contextlib.suppress(Exception):
        pygame.display.set_icon(app_icon(64))
    apply_window_shape(win_size[0], *placeholder)

    box: dict[str, Any] = {"status": "Connecting..."}
    ready = threading.Event()
    rsd_addr = (args.rsd[0], int(args.rsd[1])) if args.rsd else None
    backend = threading.Thread(
        target=run_backend, args=(box, target_h, ready, rsd_addr, prepare), daemon=True, name="backend"
    )
    backend.start()

    show_stats = args.stats or bool(settings["stats"])
    settings_on = False
    last_clip_seq = 0
    last_image_seq = 0
    last_clip_seqno = -1  # Windows' clipboard counter when we last read or wrote it
    last_synced_clip: Optional[str] = None
    last_seq = -1
    got_picture = False  # until the first frame, the connecting screen shows (with a spinner)
    bright: Optional[tuple] = None  # last frame with the screen on, shown while locked
    disp = placeholder  # size of the phone picture as shown
    rot_manual = 0
    rot_shown = 0
    pressed_keysyms: dict[int, int] = {}
    mouse_mask = 0
    touch_down = False
    hover: Optional[str] = None
    keypad_on = False
    keypad_last = 0.0
    keypad_digits = 0
    resize_at = 0.0
    scroll, pinch, fingers = ScrollGesture(), PinchGesture(), FingerTracker()

    def to_fb(pos: tuple[int, int], snap: bool = False) -> tuple[int, int]:
        x = max(0, min(disp[0] - 1, pos[0]))
        y = max(0, min(disp[1] - 1, pos[1] - y0))
        if snap:
            x, y = snap_to_edges(x, y, disp[0], disp[1])
        return x, y

    def pill_hit(pos: tuple[int, int]) -> Optional[str]:
        if pos[1] >= BAR_H or not pill_rect(win_size[0]).collidepoint(pos):
            return None
        for name, r in pill_buttons(win_size[0]).items():
            if r.collidepoint(pos):
                return name
        return "drag"

    def keypad_hit(pos: tuple[int, int]) -> Optional[str]:
        if not keypad_on:
            return None
        panel, keys = keypad_layout(*disp)
        p = (pos[0], pos[1] - y0)
        for label, r in keys.items():
            if r.collidepoint(p):
                return label
        return "panel" if panel.collidepoint(p) else None

    passcode_swipe_t = 0.0  # when we last swiped up to the passcode screen
    black_since: Optional[float] = None  # when the stream started sending only black frames
    blur_cache: dict[str, Any] = {}
    stream_stopped = False  # no video for a while (wake() restarts it)
    screen_off = False  # the phone says its screen is off (or only the always-on lock screen shows)

    def is_idle(m: Optional[LeanMirror]) -> bool:
        """Show the locked-phone view? Screen off per the phone, or (if it doesn't say) the video
        stopped or only sends black frames."""
        nonlocal stream_stopped, screen_off
        if m is None or "error" in box:
            return False
        quiet = time.perf_counter() - m.last_feed_t
        if m.backlight is not None:
            # "activeOn" / "activeDimmed" (idle dimming before auto-lock): in use. "inactiveOn" (always-on
            # lock screen) / "off": asleep.
            screen_off = not m.backlight.startswith("active")
            stream_stopped = quiet > (IDLE_SECONDS if screen_off else WAIT_SECONDS)
            return screen_off or stream_stopped
        screen_off = False
        stream_stopped = quiet > IDLE_SECONDS
        return stream_stopped or (black_since is not None and time.perf_counter() - black_since > BLACK_SECONDS)

    running = True
    while running:
        busy = scroll.active or pinch.active or resize_at or (box.get("mirror") and box["mirror"].phone_clip
                                                                and box["mirror"].phone_clip[0] != last_clip_seq)
        busy = busy or (not got_picture and "error" not in box)  # keep the spinner turning
        events = [pygame.event.wait(30 if busy else 250)]
        events += pygame.event.get()
        now = time.perf_counter()
        redraw = False
        mirror: Optional[LeanMirror] = box.get("mirror")
        idle = is_idle(mirror)

        def press_key(keysym: int) -> None:
            mirror.post_input("key", True, keysym)  # type: ignore[union-attr]
            mirror.post_input("key", False, keysym)  # type: ignore[union-attr]

        def wake() -> None:
            nonlocal keypad_on, keypad_last, keypad_digits
            nonlocal passcode_swipe_t
            mirror.post_input("button", vs._BTN_HOME)  # type: ignore[union-attr]
            if stream_stopped:  # video ended while asleep: start it again once the screen is on
                mirror.request_restart("wake", delay=1.0)  # type: ignore[union-attr]
            # Face ID waits on the lock screen; swipe up so the passcode screen is ready for the keypad
            # (if Face ID unlocks first, the same swipe just opens the Home Screen)
            mirror.post_input("swipe_up", 0.9)  # type: ignore[union-attr]
            passcode_swipe_t = now
            keypad_on, keypad_last, keypad_digits = True, now, 0

        def keypad_press(label: str) -> None:
            nonlocal keypad_last, keypad_digits, passcode_swipe_t
            if label == "show" or (
                label.isdigit() and keypad_digits == 0 and now - max(passcode_swipe_t, keypad_last) > PASSCODE_IDLE
            ):
                # iOS drops back to the Face ID lock screen after a few idle seconds
                mirror.post_input("swipe_up", 0.0)  # type: ignore[union-attr]
                passcode_swipe_t = now
            keypad_last = now
            if label == "del":
                press_key(0xFF08)
                keypad_digits = max(0, keypad_digits - 1)
            elif label.isdigit():
                press_key(ord(label))
                keypad_digits += 1

        def push_windows_clipboard(then_paste: bool = False) -> bool:
            """Windows -> phone, so a paste on the phone gets what was copied on the PC. A picture is
            converted on another thread (big ones take a second); with ``then_paste`` that thread
            presses Cmd+V once the picture is on the phone, and this returns True."""
            nonlocal last_synced_clip, last_clip_seqno
            if not settings["clipboard"] or mirror is None:
                return False
            from phonyscreen import winclip

            seqno = winclip.sequence_number()
            if seqno == last_clip_seqno:
                return False  # unchanged since we last looked, or what we put there from the phone
            last_clip_seqno = seqno
            text = winclip.get_text()
            if text:
                if text != last_synced_clip:
                    last_synced_clip = text
                    mirror.post_input("clip_to_phone", text)
                return False
            if not winclip.has_image():
                return False
            target = mirror

            def send_picture() -> None:
                png = winclip.get_image_png()
                digest = hashlib.sha1(png).hexdigest() if png else ""
                if png and digest != box.get("last_picture_sent"):  # Snipping Tool updates the clipboard twice
                    box["last_picture_sent"] = digest
                    logger.info("Windows picture -> phone clipboard (%d KB)", len(png) // 1024)
                    target.post_input("clip_image_to_phone", png)
                if then_paste:
                    for e in (("key", True, CMD_KEYSYM), ("key", True, pygame.K_v), ("key", False, pygame.K_v),
                              ("key", False, CMD_KEYSYM)):
                        target.post_input(*e)

            threading.Thread(target=send_picture, daemon=True, name="clipboard-picture").start()
            return then_paste

        def settings_click(pos: tuple[int, int]) -> bool:
            """Handle a click while the settings panel is open; True if it was consumed."""
            nonlocal settings_on, show_stats, target_h, resize_at
            panel, hits = settings_layout(*disp)
            p = (pos[0], pos[1] - y0)
            for hit, r in hits.items():
                if r.collidepoint(p):
                    if hit == "done":
                        settings_on = False
                    elif apply_setting(settings, hit):
                        show_stats = bool(settings["stats"])
                        if hit.startswith("size"):
                            target_h = min(max(int((screen_h - y0) * settings["size"]), 400), max_h)
                            resize_at = now + 0.35
                        if hit == "sound" and mirror is not None:
                            mirror.set_sound(bool(settings["sound"]))
                        if hit == "mute_phone" and mirror is not None:
                            mirror.set_mute_phone(bool(settings["mute_phone"]))
                        update_config(settings=settings)
                    return True
            if not panel.collidepoint(p):
                settings_on = False
            return True

        def pill_action(hit: str) -> None:
            nonlocal running, rot_manual, keypad_on, keypad_last, keypad_digits, settings_on
            if hit == "close":
                running = False
            elif hit == "minimize":
                pygame.display.iconify()
            elif hit == "rotate":
                rot_manual = (rot_manual + 90) % 360
            elif hit == "keypad":
                keypad_on, keypad_last, keypad_digits = not keypad_on, now, 0
            elif hit == "settings":
                settings_on = not settings_on
            elif hit == "home" and mirror is not None:
                mirror.post_input("button", vs._BTN_HOME)
            elif hit == "switcher" and mirror is not None:
                mirror.post_input("app_switcher")
            elif hit == "drag":
                start_window_drag()

        for ev in events:
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == NEWFRAME:
                redraw = True
            elif ev.type == getattr(pygame, "WINDOWFOCUSLOST", -1):
                if mirror is not None and settings["clipboard"]:
                    mirror.pull_phone_clipboard()
            elif ev.type == getattr(pygame, "WINDOWFOCUSGAINED", -1):
                push_windows_clipboard()
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION) and getattr(
                ev, "touch", False
            ):
                continue  # synthesized from the touchscreen; handled as FINGER events
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and pill_hit(ev.pos):
                pill_action(pill_hit(ev.pos))  # type: ignore[arg-type]
                redraw = True
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 2, 3) and ev.pos[1] >= y0:
                if settings_on:
                    settings_click(ev.pos)
                    redraw = True
                    continue
                if mirror is None:
                    continue
                k = keypad_hit(ev.pos) if ev.button == 1 else None
                if k:
                    if k != "panel":
                        keypad_press(k)
                    redraw = True
                    continue
                if idle and ev.button == 1:
                    wake()
                    redraw = True
                    continue
                if keypad_on and ev.button == 1:
                    keypad_on = False  # tapping elsewhere dismisses the pad
                mouse_mask |= {1: 1, 2: 2, 3: 4}[ev.button]
                touch_down = touch_down or ev.button == 1
                mirror.post_input("pointer", mouse_mask, *to_fb(ev.pos, snap=ev.button == 1))
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button in (1, 2, 3):
                bit = {1: 1, 2: 2, 3: 4}[ev.button]
                if mirror is None or not mouse_mask & bit:
                    continue
                mouse_mask &= ~bit
                if ev.button == 1:
                    touch_down = False
                mirror.post_input("pointer", mouse_mask, *to_fb(ev.pos))
            elif ev.type == pygame.MOUSEMOTION:
                if touch_down and mirror is not None:
                    mirror.post_input("move", mouse_mask, *to_fb(ev.pos))
                new_hover = pill_hit(ev.pos)
                if new_hover != hover:
                    hover, redraw = new_hover, True
            elif ev.type == pygame.MOUSEWHEEL:
                mpos = pygame.mouse.get_pos()
                if mirror is None or touch_down or mpos[1] < y0 or idle or settings_on:
                    continue
                sign = -1 if getattr(ev, "flipped", False) else 1
                py = float(getattr(ev, "precise_y", ev.y)) * sign
                px = float(getattr(ev, "precise_x", ev.x)) * sign
                if pygame.key.get_mods() & pygame.KMOD_CTRL:  # touchpad pinch arrives as Ctrl+wheel
                    for e in pinch.feed(to_fb(mpos), py, disp[0], disp[1], now, speed=settings["pinch_speed"]):
                        mirror.post_input(*e)
                else:
                    step = disp[1] * 0.06 * scroll_factor(settings)
                    for e in scroll.feed(to_fb(mpos), -px * step, py * step, disp[0], disp[1], now):
                        mirror.post_input(*e)
            elif ev.type in (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP):
                wx, wy = int(ev.x * win_size[0]), int(ev.y * win_size[1])
                if mirror is None:
                    continue
                if ev.type == pygame.FINGERDOWN:
                    if pill_hit((wx, wy)):
                        pill_action(pill_hit((wx, wy)))  # type: ignore[arg-type]
                        redraw = True
                        continue
                    if settings_on:
                        settings_click((wx, wy))
                        redraw = True
                        continue
                    k = keypad_hit((wx, wy))
                    if k:
                        if k != "panel":
                            keypad_press(k)
                        redraw = True
                        continue
                    if idle:
                        wake()
                        redraw = True
                        continue
                    if wy < y0:
                        continue
                    report = fingers.down(ev.finger_id, *to_fb((wx, wy)), disp[0], disp[1])
                elif ev.type == pygame.FINGERMOTION:
                    report = fingers.move(ev.finger_id, *to_fb((wx, wy)))
                else:
                    report = fingers.up(ev.finger_id)
                if report is not None:
                    mirror.post_input("multi", report)
                if fingers.home_swipe:  # iOS ignores the injected edge swipe, so press Home for it
                    fingers.home_swipe = False
                    logger.info("touchscreen swipe from the bottom edge -> Home")
                    mirror.post_input("button", HOME_BUTTON)
                if fingers.app_switcher:
                    fingers.app_switcher = False
                    logger.info("touchscreen swipe from the bottom edge and hold -> App Switcher")
                    mirror.post_input("app_switcher")
            elif ev.type == pygame.KEYDOWN:
                # Windows reports AltGr as Ctrl+Alt: AltGr+Q (@ on Turkish Q) is a character, not Ctrl+Q
                altgr = bool(ev.unicode) and ev.unicode.isprintable() and bool(
                    ev.mod & (pygame.KMOD_RALT | pygame.KMOD_MODE)
                    or (ev.mod & pygame.KMOD_CTRL and ev.mod & pygame.KMOD_ALT)
                )
                ctrl = bool(ev.mod & pygame.KMOD_CTRL) and not altgr
                if ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_ALT | pygame.KMOD_MODE) and ev.key not in MODIFIER_KEYS:
                    logger.debug("key %s mod 0x%04X altgr=%s", pygame.key.name(ev.key), ev.mod, altgr)  # not plain typing
                if ev.key == pygame.K_ESCAPE:
                    if settings_on:
                        settings_on, redraw = False, True
                    elif keypad_on:
                        keypad_on, redraw = False, True
                    else:
                        running = False
                elif ctrl and ev.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS, pygame.K_MINUS,
                                         pygame.K_KP_MINUS, pygame.K_0, pygame.K_KP0):
                    if ev.key in (pygame.K_0, pygame.K_KP0):
                        settings["size"] = DEFAULT_SETTINGS["size"]
                    else:
                        grow = ev.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS)
                        apply_setting(settings, "size+" if grow else "size-")
                    target_h = min(max(int((screen_h - y0) * settings["size"]), 400), max_h)
                    update_config(settings=settings)
                    resize_at = now + 0.35  # apply once the key presses settle
                elif mirror is None:
                    continue
                elif ev.key == pygame.K_F1:
                    mirror.post_input("button", vs._BTN_HOME)
                elif ev.key == pygame.K_F2:
                    mirror.post_input("button", vs._BTN_LOCK)
                elif ev.key == pygame.K_F4 and not ev.mod & pygame.KMOD_ALT:  # Alt+F4 stays "close"
                    mirror.post_input("app_switcher")
                elif ev.key == pygame.K_F3:
                    show_stats = settings["stats"] = not show_stats
                    update_config(settings=settings)
                    redraw = True
                else:
                    if ev.key in MODIFIER_KEYSYMS:
                        keysym = MODIFIER_KEYSYMS[ev.key]
                    elif ev.key in SPECIAL_KEYSYMS:
                        keysym = SPECIAL_KEYSYMS[ev.key]
                    elif altgr:  # @ # $ { } [ ] \ | ~ €: where they sit depends on the phone's layout
                        mirror.post_input("type_text", ev.unicode)
                        keysym = 0
                    elif not ctrl and ev.unicode and 0x20 <= ord(ev.unicode) <= 0x7E:
                        keysym = ord(ev.unicode)
                    elif not ctrl and ev.unicode and ev.unicode.isprintable():
                        mirror.post_input("type_text", ev.unicode)  # ç ş ğ ı İ ü ö ...: pasted on the phone
                        keysym = 0
                    elif ctrl and 0x20 <= ev.key <= 0x7E:
                        # Ctrl+C / V / X / A / Z ... -> Command+key, the iPhone's shortcuts
                        deferred = ev.key == pygame.K_v and push_windows_clipboard(then_paste=True)
                        if not deferred:  # (a picture pastes once it's on the phone)
                            for e in (("key", True, CMD_KEYSYM), ("key", True, ev.key), ("key", False, ev.key),
                                      ("key", False, CMD_KEYSYM)):
                                mirror.post_input(*e)
                        if ev.key in (pygame.K_c, pygame.K_x) and settings["clipboard"]:
                            mirror.pull_phone_clipboard(0.5)  # the copy lands on the phone, then comes here
                        keysym = 0
                    else:
                        keysym = 0
                    if keysym:
                        pressed_keysyms[ev.key] = keysym
                        mirror.post_input("key", True, keysym)
            elif ev.type == pygame.KEYUP:
                keysym = pressed_keysyms.pop(ev.key, 0)
                if keysym and mirror is not None:
                    mirror.post_input("key", False, keysym)

        if mirror is not None:
            for e in scroll.tick(now) + pinch.tick(now):
                mirror.post_input(*e)
            clip = mirror.phone_clip
            if clip is not None and clip[0] != last_clip_seq:
                last_clip_seq = clip[0]
                if settings["clipboard"] and clip[1] != last_synced_clip:
                    from phonyscreen import winclip

                    last_synced_clip = clip[1]
                    ok = winclip.set_text(clip[1], pygame.display.get_wm_info().get("window"))
                    last_clip_seqno = winclip.sequence_number()  # ours: don't send it back
                    logger.info("phone copy -> Windows clipboard (%d chars)%s", len(clip[1]), "" if ok else " FAILED")
            phone_pic = mirror.phone_image
            if phone_pic is not None and phone_pic[0] != last_image_seq:
                last_image_seq = phone_pic[0]
                if settings["clipboard"]:
                    from phonyscreen import winclip

                    def convert(data: bytes = phone_pic[1]) -> None:  # big photos take a second: not here
                        box["clip_formats"] = (winclip.image_formats(data), len(data))
                        with contextlib.suppress(Exception):
                            pygame.event.post(pygame.event.Event(NEWFRAME))

                    threading.Thread(target=convert, daemon=True, name="clipboard-picture").start()
            converted = box.pop("clip_formats", None)
            if converted is not None:
                from phonyscreen import winclip

                formats, size = converted
                ok = bool(formats) and winclip.set_formats(formats, pygame.display.get_wm_info().get("window"))
                last_clip_seqno = winclip.sequence_number()
                logger.info("phone picture -> Windows clipboard (%d KB)%s", size // 1024, "" if ok else " FAILED")
        if resize_at and now >= resize_at and mirror is not None:
            resize_at = 0.0
            mirror.target_height = target_h
            mirror.request_restart("resize")
        if keypad_on and keypad_digits >= 4 and now - keypad_last > 2.0:
            keypad_on, redraw = False, True  # passcode entered; get out of the way

        rot = ((getattr(mirror, "_rotation", 0) if mirror is not None else 0) + rot_manual) % 360
        if rot != rot_shown:
            redraw = True
        status = mirror.status if mirror is not None and "error" not in box else box.get("status", "")
        if idle:
            status = ""  # the locked view explains it
        if not (redraw or status or idle != box.get("_idle_drawn")):
            continue
        box["_idle_drawn"] = idle
        if mirror is not None:
            mirror.newframe_pending.clear()
        slot = None
        if mirror is not None:
            with mirror.slot_lock:
                slot = mirror.slot
        if slot is not None and slot[6] != last_seq:
            last_seq = slot[6]
            if not frame_is_dark(slot[0]):
                bright = slot
            if frame_is_black(slot[0]):
                black_since = black_since or now
            else:
                black_since = None
            with mirror.stats.lock:  # type: ignore[union-attr]
                mirror.stats.shown.append(now)  # type: ignore[union-attr]
                mirror.stats.pipeline_ms.append((now - slot[5]) * 1000)  # type: ignore[union-attr]
        show = bright if (idle and bright is not None) else slot
        picture = None
        if show is not None:
            data, w, h, vw, vh = show[:5]
            picture = pygame.image.frombuffer(data, (w, h), "RGB").subsurface(pygame.Rect(0, 0, vw, vh))
            if rot:
                picture = pygame.transform.rotate(picture, -rot)  # clockwise
            new_disp = picture.get_size()
            if new_disp != disp or rot != rot_shown:
                disp, rot_shown = new_disp, rot
                signed = -90 if rot == 270 else rot
                mirror.display_map = (disp[0], disp[1], signed)  # type: ignore[union-attr]
                mirror._fb_width, mirror._fb_height, mirror._fb_rotation = disp[0], disp[1], signed  # type: ignore[union-attr]
                new_size = (disp[0], y0 + disp[1])
                if new_size != win_size:
                    win_size = new_size
                    screen = pygame.display.set_mode(win_size, pygame.NOFRAME)
                apply_window_shape(win_size[0], *disp)
        screen.fill((0, 0, 0))
        if picture is not None and idle:
            key = (show[6], disp, rot_shown)  # type: ignore[index]
            if blur_cache.get("key") != key:
                blur_cache.update(key=key, surf=privacy_blur(picture))
            screen.blit(blur_cache["surf"], (0, y0))
        elif picture is not None:
            screen.blit(picture, (0, y0))
        else:  # no video yet: app name and progress, or why it couldn't connect
            draw_connecting(screen, y0, win_size[0], win_size[1] - y0, status, "error" in box, now, body=not idle)
        got_picture = got_picture or picture is not None
        draw_pill(screen, win_size[0], hover, keypad_on, settings_on)
        if idle and not keypad_on:
            title = "Waiting for your iPhone" if stream_stopped and not screen_off else "iPhone is locked"
            draw_locked(screen, y0, disp[0], disp[1], title)
        if keypad_on and picture is not None:
            draw_keypad(screen, y0, disp[0], disp[1])
        if settings_on:
            draw_settings(screen, y0, disp[0], disp[1], settings)

        lines: list[str] = []
        if status and picture is not None:  # without a picture, the connecting screen shows it
            lines.extend(style.wrap(status, max(13, disp[1] // 66), win_size[0] - 64))
        if show_stats and mirror is not None:
            st = mirror.stats
            with st.lock:
                fps = sum(1 for t in st.shown if now - t <= 2.0) / 2.0  # frames shown in the last 2 s
                lines.append(f"{fps:4.1f} fps | in->screen {Stats.avg(st.pipeline_ms):4.0f} ms")
                lines.append(
                    f"decode {Stats.avg(st.decode_ms):4.1f} ms | convert {Stats.avg(st.convert_ms):4.1f} ms"
                    f" | restarts {st.restarts}"
                )
        y = y0 + max(44, int(disp[1] * 0.065))  # below the rounded corners and the camera cut-out
        for line in lines:
            img = style.toast(line, max(13, disp[1] // 66))
            screen.blit(img, ((win_size[0] - img.get_width()) // 2, y))
            y += img.get_height() + 6
        pygame.display.flip()

    wp = window_position()
    with contextlib.suppress(Exception):
        update_config(**({"window_pos": list(wp)} if wp else {}))
    mirror = box.get("mirror")
    if mirror is not None:
        mirror.request_stop()
    backend.join(timeout=8.0)  # let the backend release the device's media session
    pygame.quit()
    return 0


@lru_cache(maxsize=8)
def app_icon(size: int) -> pygame.Surface:
    """The app icon (drawn by phonyscreen/appicon.py, like the exe's .ico) as a pygame surface."""
    img = appicon.render(size)
    return pygame.image.frombytes(img.tobytes(), img.size, "RGBA")


if __name__ == "__main__":
    sys.exit(main())
