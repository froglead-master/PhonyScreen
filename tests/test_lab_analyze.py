"""Gesture lab measurement path: LeanMirror's real input worker (HID stubbed) records what it sends,
and the analyzer turns touches + frame shifts into strokes, release speed, tracking and coasting."""

import asyncio
import collections
import time

from phonyscreen import gesturelab as lab
from phonyscreen import gesturelab_analyze as ga
from phonyscreen import viewer as v


def test_worker_records_touches_and_latency(tmp_path, monkeypatch):
    sent = []

    async def fake_base_pointer(self, client, mask, x, y):
        sent.append((mask, x, y))
        client.pressed = bool(mask & 1)

    monkeypatch.setattr(v.vs.VncStreamServer, "_handle_pointer", fake_base_pointer)
    m = object.__new__(v.LeanMirror)
    m.client = v.vs._VncClient(None, None)
    m._touch_last = None
    m._fb_width, m._fb_height = 420, 912
    m.input_buf = collections.deque()
    rec = lab.Recorder(tmp_path / "r.jsonl")
    monkeypatch.setattr(lab, "REC", rec)

    async def run():
        m._loop = asyncio.get_running_loop()
        m.input_evt = asyncio.Event()
        worker = asyncio.create_task(m._input_worker())
        m.post_input("pointer", 1, 200, 600)
        for i in range(1, 20):
            m.post_input("move", 1, 200, 600 - 15 * i)
            await asyncio.sleep(0.01)
        m.post_input("pointer", 0, 200, 300)
        await asyncio.sleep(0.3)
        worker.cancel()

    asyncio.run(run())
    rec.close()
    evs = ga.load(tmp_path / "r.jsonl")
    touches = [e for e in evs if e["k"] == "touch"]
    assert touches[0]["m"] == 1 and not touches[0]["was"]
    assert touches[-1]["m"] == 0 and touches[-1]["was"]
    assert len(touches) == len(sent)
    lat = ga.latency_stats(evs)
    assert lat["post_to_touch_ms"]["n"] >= 10
    assert lat["post_to_touch_ms"]["p50"] < 40  # paced at >= 16 ms per sample
    strokes = ga.strokes_from_touch(evs)
    assert len(strokes) == 1 and strokes[0]["release"][1:] == (200, 300)


def synthetic_trial(release_moving: bool) -> list[dict]:
    """A drag up 300 px in 300 ms; content follows 1:1, 60 ms late; with a moving release it coasts."""
    evs, t = [], 1000.0
    evs.append({"t": t, "k": "touch", "x": 200, "y": 600, "m": 1, "was": False, "fb": [420, 912]})
    for i in range(1, 20):
        evs.append({"t": t + 16 * i, "k": "touch", "x": 200, "y": 600 - 15 * i + (0 if release_moving or i < 19 else 0),
                    "m": 1, "was": True, "fb": [420, 912]})
    t_rel = t + 16 * 19 + (5 if release_moving else 200)
    evs.append({"t": t_rel, "k": "touch", "x": 200, "y": 315, "m": 0, "was": True, "fb": [420, 912]})
    for i in range(1, 20):  # frames: content moves with the finger, 60 ms later
        evs.append({"t": t + 16 * i + 60, "k": "frame", "tin": t + 16 * i + 60, "dx": 0.0, "dy": -15.0, "peak": 0.8})
    if release_moving:
        for j in range(1, 30):  # coasting, decaying
            evs.append({"t": t_rel + 60 + 16 * j, "k": "frame", "tin": t_rel + 60 + 16 * j, "dx": 0.0,
                        "dy": -15.0 * 0.9 ** j, "peak": 0.8})
    return sorted(evs, key=lambda e: e["t"])


def test_analyzer_release_speed_tracking_and_coast():
    moving = synthetic_trial(True)
    s = ga.strokes_from_touch(moving)
    d = ga.describe_stroke(s[0])
    assert d["release_speed"] > 500 and d["release_gap_ms"] < 10
    c = ga.content_motion(moving, s, moving[-1]["t"])
    assert abs(c["lag_ms"] - 60) < 17
    assert abs(c["strokes"][0]["during"][1] + 285) < 30  # ~1:1 with the 285 px sent
    assert c["strokes"][0]["coast"][1] < -50 and c["strokes"][0]["coast_ms"] > 200

    still = synthetic_trial(False)
    s = ga.strokes_from_touch(still)
    d = ga.describe_stroke(s[0])
    assert d["release_speed"] < 1 and d["release_gap_ms"] >= 199  # held still, then lifted
    c = ga.content_motion(still, s, still[-1]["t"] + 500)
    assert c["strokes"][0]["coast"] == (0, 0)


def test_pad_segments_lift_speed():
    evs = []
    for i in range(30):  # two fingers sliding down 1 mm per 8 ms report, then lifted while moving
        y = 10 + i
        evs.append({"t": 8.0 * i, "k": "pad", "n": 2, "c": [[0, True, 40, y], [1, True, 60, y]]})
    evs.append({"t": 240.0, "k": "pad", "n": 2, "c": [[0, False, 40, 40], [1, False, 60, 40]]})
    segs = ga.pad_segments(evs)
    assert len(segs) == 1
    assert segs[0]["fingers"] == 2 and abs(segs[0]["dy_mm"] - 29) < 0.01
    assert segs[0]["lift_speed_mm_s"] > 100  # still moving at lift


def test_recorder_paused_writes_nothing(tmp_path):
    r = lab.Recorder(tmp_path / "p.jsonl")
    r.paused = True
    r.write("touch", {"x": 1})
    r.paused = False
    r.write("touch", {"x": 2})
    r.close()
    assert [e["x"] for e in ga.load(tmp_path / "p.jsonl")] == [2]
    assert time.perf_counter() > 0
