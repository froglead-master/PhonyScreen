"""Summarize gesture lab recordings: `python phonyscreen.py gesturelab analyze [FILE ...] [--json OUT]`.

Per trial (between the Space marks; discarded trials are left out) it compares
  * the input: wheel deltas (precise or rounded?), raw touchpad contacts in mm (when did the
    fingers lift, were they still moving?), pointer / finger travel and release speed
  * what we sent: touch strokes (press .. release), their speed at release, Home presses,
    edge snaps, frame presses, coalesced moves, input -> touch latency
  * what the phone did: content displacement from the frame-shift estimates, while the
    finger was down and after it lifted (momentum / coasting), and how long it took to start
and prints per-task medians. No file -> the newest recording in %APPDATA%\\PhonyScreen\\gesturelab.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

RELEASE_WINDOW = 0.05  # s: speed at release = movement over the last 50 ms
COAST_WINDOW = 1.5  # s after release in which content movement counts as coasting
MOVE_PX = 1.0  # a frame shift above this is movement


def load(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # a line cut off when the app was killed
    return out


def split_trials(events: list[dict]) -> tuple[dict, list[dict]]:
    """(session info, trials). A trial: task, device, trial number, rating, events between its marks."""
    info: dict[str, Any] = {}
    trials: list[dict] = []
    cur: Optional[dict] = None
    for e in events:
        k = e["k"]
        if k == "session":
            info = e
        elif k == "display":
            info["display"] = e
        elif k == "pad" and "pad_size" not in info:
            info["pad_size"] = e.get("size")
        if k == "trial_start":
            cur = {"task": e["task"], "device": e["device"], "trial": e["trial"], "t0": e["t"], "events": [],
                   "rating": None, "display": info.get("display")}
        elif k == "trial_end" and cur is not None:
            cur["t1"] = e["t"]
            trials.append(cur)
            cur = None
        elif k == "rating" and trials and trials[-1]["task"] == e["task"] and trials[-1]["trial"] == e["trial"]:
            trials[-1]["rating"] = e["rating"]
        elif k == "trial_discard" and trials and trials[-1]["task"] == e["task"]:
            trials.pop()
        elif cur is not None:
            cur["events"].append(e)
    return info, trials


# -- strokes we sent -------------------------------------------------------------------------------------
def strokes_from_touch(evs: list[dict]) -> list[dict]:
    """One-finger strokes from the pointer path ("touch" records)."""
    out, cur = [], None
    for e in evs:
        if e["k"] != "touch":
            continue
        down = bool(e["m"] & 1)
        if down and not e["was"]:
            cur = {"src": "pointer", "samples": [(e["t"], e["x"], e["y"])], "fb": e.get("fb")}
        elif down and cur is not None:
            cur["samples"].append((e["t"], e["x"], e["y"]))
        elif not down and e["was"] and cur is not None:
            cur["release"] = (e["t"], e["x"], e["y"])
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def strokes_from_multi(evs: list[dict]) -> list[dict]:
    """Per-identity strokes from touchscreen / pinch ("multi" records)."""
    out: list[dict] = []
    open_: dict[int, dict] = {}
    for e in evs:
        if e["k"] != "multi":
            continue
        for ident, x, y, touching in e["c"]:
            s = open_.get(ident)
            if touching:
                if s is None:
                    s = open_[ident] = {"src": "multi", "ident": ident, "samples": [], "fb": e.get("fb")}
                s["samples"].append((e["t"], x, y))
            elif s is not None:
                s["release"] = (e["t"], x, y)
                out.append(open_.pop(ident))
    out.extend(open_.values())
    return sorted(out, key=lambda s: s["samples"][0][0] if s["samples"] else 0)


def speed_at_end(samples: list[tuple[float, float, float]], t_end: float, window: float = RELEASE_WINDOW) -> float:
    """Speed (units/s) over the last ``window`` seconds before t_end (ms timestamps)."""
    if not samples:
        return 0.0
    pts = [s for s in samples if s[0] >= t_end - window * 1000]
    before = [s for s in samples if s[0] < t_end - window * 1000]
    if before:
        pts = [before[-1], *pts]
    if len(pts) < 2:
        return 0.0
    dist = math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2])
    return dist / max(1e-3, (t_end - pts[0][0]) / 1000)


def last_move_gap(samples: list[tuple[float, float, float]], t_end: float) -> float:
    """ms between the last sample that moved and t_end."""
    last = samples[0][0] if samples else t_end
    for a, b in zip(samples, samples[1:]):
        if (a[1], a[2]) != (b[1], b[2]):
            last = b[0]
    return t_end - last


def describe_stroke(s: dict) -> dict:
    sm = s["samples"]
    if not sm:
        return {}
    rel = s.get("release")
    end = rel or sm[-1]
    w, h = s.get("fb") or (0, 0)
    t_end = end[0]
    d = {
        "start": sm[0][1:], "end": end[1:], "dx": end[1] - sm[0][1], "dy": end[2] - sm[0][2],
        "dur_ms": round(t_end - sm[0][0], 1), "n": len(sm),
        "release_speed": round(speed_at_end(sm + ([rel] if rel else []), t_end), 1),
        "release_gap_ms": round(last_move_gap(sm, t_end), 1), "released": rel is not None,
    }
    if w and h:
        x0, y0 = sm[0][1], sm[0][2]
        d["start_edge"] = [n for n, c in (("left", x0 <= 0), ("right", x0 >= w - 1), ("top", y0 <= 0),
                                          ("bottom", y0 >= h - 1)) if c]
    return d


# -- raw input --------------------------------------------------------------------------------------------
def pad_segments(evs: list[dict]) -> list[dict]:
    """Touchpad contact periods from raw reports: start, lift, max fingers, centroid track (mm)."""
    segs, cur = [], None
    for e in evs:
        if e["k"] != "pad":
            continue
        touching = [c for c in e["c"] if c[1]]
        if e.get("n") == 0 and not touching:
            continue  # later report of the same frame without fingers
        if touching:
            cx = sum(c[2] for c in touching) / len(touching)
            cy = sum(c[3] for c in touching) / len(touching)
            ids = tuple(sorted(c[0] for c in touching))
            if cur is None or e["t"] - cur["track"][-1][0] > 80:
                if cur is not None:
                    segs.append(cur)
                cur = {"t0": e["t"], "track": [], "fingers": 0}
            cur["track"].append((e["t"], cx, cy, ids))
            cur["fingers"] = max(cur["fingers"], len(touching))
            cur["lift"] = e["t"]
        elif cur is not None:
            cur["lift"] = e["t"]
            segs.append(cur)
            cur = None
    if cur is not None:
        segs.append(cur)
    for s in segs:
        tr = s["track"]
        travel_x = travel_y = path = 0.0
        for a, b in zip(tr, tr[1:]):
            if a[3] != b[3]:
                continue  # finger set changed: the centroid jumps
            travel_x += b[1] - a[1]
            travel_y += b[2] - a[2]
            path += math.hypot(b[1] - a[1], b[2] - a[2])
        steady = [(t, x, y) for t, x, y, ids in tr if ids == tr[-1][3]]
        s.update(dx_mm=round(travel_x, 1), dy_mm=round(travel_y, 1), path_mm=round(path, 1),
                 dur_ms=round(s["lift"] - s["t0"], 1),
                 lift_speed_mm_s=round(speed_at_end(steady, steady[-1][0]), 1) if steady else 0.0)
        del s["track"]
    return segs


def wheel_stats(evs: list[dict], pads: list[dict]) -> dict:
    wheels = [e for e in evs if e["k"] == "wheel" and not e.get("ctrl")]
    if not wheels:
        return {}
    contact = [(p["t0"], p["lift"]) for p in pads if p["fingers"] >= 2]

    def during_contact(t: float) -> bool:
        return any(a - 20 <= t <= b + 20 for a, b in contact)

    after = [e for e in wheels if contact and not during_contact(e["t"])]
    ints = sum(1 for e in wheels if float(e["py"]).is_integer() and float(e["px"]).is_integer())
    gaps = [b["t"] - a["t"] for a, b in zip(wheels, wheels[1:])]
    return {
        "events": len(wheels), "sum_px": round(sum(e["px"] for e in wheels), 3),
        "sum_py": round(sum(e["py"] for e in wheels), 3),
        "integer_share": round(ints / len(wheels), 2),
        "int_y_share": round(sum(1 for e in wheels if e["y"] != 0) / len(wheels), 2),
        "median_gap_ms": round(statistics.median(gaps), 1) if gaps else None,
        "flipped": any(e.get("flip") for e in wheels),
        "after_lift_events": len(after) if contact else None,
        "after_lift_py": round(sum(e["py"] for e in after), 3) if contact else None,
        "after_lift_ms": round(after[-1]["t"] - after[0]["t"], 1) if len(after) > 1 else 0.0,
    }


def pointer_stats(evs: list[dict]) -> dict:
    """Raw pointer drags (mouse button 1 down .. up): distance and release speed in window px."""
    out, cur = [], None
    for e in evs:
        if e["k"] == "mdown" and e["b"] == 1:
            cur = [(e["t"], *e["pos"])]
        elif e["k"] == "mmove" and cur is not None:
            cur.append((e["t"], *e["pos"]))
        elif e["k"] == "mup" and e["b"] == 1 and cur is not None:
            cur.append((e["t"], *e["pos"]))
            out.append({"dist": round(math.hypot(cur[-1][1] - cur[0][1], cur[-1][2] - cur[0][2]), 1),
                        "dur_ms": round(cur[-1][0] - cur[0][0], 1),
                        "release_speed": round(speed_at_end(cur, cur[-1][0]), 1),
                        "release_gap_ms": round(last_move_gap(cur[:-1], cur[-1][0]), 1)})
            cur = None
    return {"drags": out}


def finger_stats(evs: list[dict]) -> dict:
    out, open_ = [], {}
    for e in evs:
        if e["k"] == "fdown":
            open_[e["f"]] = [(e["t"], *e["pos"])]
        elif e["k"] == "fmove" and e["f"] in open_:
            open_[e["f"]].append((e["t"], *e["pos"]))
        elif e["k"] == "fup" and e["f"] in open_:
            tr = open_.pop(e["f"])
            tr.append((e["t"], *e["pos"]))
            out.append({"dist": round(math.hypot(tr[-1][1] - tr[0][1], tr[-1][2] - tr[0][2]), 1),
                        "dur_ms": round(tr[-1][0] - tr[0][0], 1),
                        "release_speed": round(speed_at_end(tr, tr[-1][0]), 1),
                        "release_gap_ms": round(last_move_gap(tr[:-1], tr[-1][0]), 1)})
    return {"fingers": out}


def latency_stats(evs: list[dict]) -> dict:
    """post (queued in the window thread, right after the raw event) -> first touch sent after it."""
    posts = {e["n"]: e for e in evs if e["k"] == "post"}
    lat, skipped = [], 0
    pending_deq = None
    for e in evs:
        if e["k"] == "deq":
            if e.get("skip"):
                skipped += 1
            elif e.get("n") in posts and posts[e["n"]]["e"][0] in ("move", "pointer", "multi"):
                pending_deq = posts[e["n"]]
        elif e["k"] in ("touch", "multi") and pending_deq is not None:
            lat.append(e["t"] - pending_deq["t"])
            pending_deq = None
    return {"post_to_touch_ms": percentiles(lat), "coalesced": skipped}


def percentiles(xs: list[float]) -> Optional[dict]:
    if not xs:
        return None
    xs = sorted(xs)
    return {"p50": round(xs[len(xs) // 2], 1), "p90": round(xs[int(len(xs) * 0.9)], 1), "max": round(xs[-1], 1),
            "n": len(xs)}


# -- what the phone did ----------------------------------------------------------------------------------
def content_motion(evs: list[dict], strokes: list[dict], t_end: float) -> dict:
    """Content displacement during each stroke and after it (coasting), shifted by the video delay."""
    frames = [e for e in evs if e["k"] == "frame" and not e.get("reset") and e.get("peak", 0) >= 0.08]
    if not frames:
        return {}
    total = (round(sum(f["dx"] for f in frames), 1), round(sum(f["dy"] for f in frames), 1))
    # onset delay: first touch move of each stroke -> first moving frame (by arrival time)
    delays = []
    for s in strokes:
        sm = s["samples"]
        first_move = next((b[0] for a, b in zip(sm, sm[1:]) if (a[1], a[2]) != (b[1], b[2])), None)
        if first_move is None:
            continue
        f = next((f for f in frames if f["tin"] > first_move and math.hypot(f["dx"], f["dy"]) > MOVE_PX), None)
        if f is not None and f["tin"] - first_move < 1000:
            delays.append(f["tin"] - first_move)
    lag = statistics.median(delays) if delays else 0.0
    per = []
    for i, s in enumerate(strokes):
        sm = s["samples"]
        if not sm:
            continue
        t_press = sm[0][0] + lag
        t_rel = (s.get("release") or sm[-1])[0] + lag
        nxt = strokes[i + 1]["samples"][0][0] + lag if i + 1 < len(strokes) and strokes[i + 1]["samples"] else t_end + lag
        t_coast_end = min(nxt, t_rel + COAST_WINDOW * 1000)
        during = [f for f in frames if t_press < f["tin"] <= t_rel]
        after = [f for f in frames if t_rel < f["tin"] <= t_coast_end]
        moving_after = [f for f in after if math.hypot(f["dx"], f["dy"]) > MOVE_PX]
        per.append({
            "during": (round(sum(f["dx"] for f in during), 1), round(sum(f["dy"] for f in during), 1)),
            "coast": (round(sum(f["dx"] for f in after), 1), round(sum(f["dy"] for f in after), 1)),
            "coast_ms": round(moving_after[-1]["tin"] - t_rel, 0) if moving_after else 0.0,
        })
    return {"total": total, "lag_ms": round(lag, 1) if delays else None, "strokes": per,
            "fps": round(len(frames) / max(1e-3, (frames[-1]["tin"] - frames[0]["tin"]) / 1000), 1) if len(frames) > 1 else None}


# -- per trial / per task ---------------------------------------------------------------------------------
def analyze_trial(tr: dict, info: dict) -> dict:
    evs = tr["events"]
    t_end = tr["t1"]
    decisions = [e for e in evs if e["k"] == "decide"]
    pads = pad_segments(evs)
    strokes = strokes_from_touch(evs) + strokes_from_multi(evs)
    strokes.sort(key=lambda s: s["samples"][0][0] if s["samples"] else 0)
    out: dict[str, Any] = {
        "task": tr["task"], "device": tr["device"], "trial": tr["trial"], "rating": tr["rating"],
        "dur_s": round((tr["t1"] - tr["t0"]) / 1000, 2),
        "decisions": dict(sorted(_count(d["what"] for d in decisions).items())),
        "home_presses": sum(1 for e in evs if e["k"] == "hid_button" and e["down"] and (e["page"], e["code"]) == (0x0C, 0x40)),
        "strokes": [describe_stroke(s) for s in strokes],
        "pad": pads,
        "wheel": wheel_stats(evs, pads),
        "used": _wheel_used(evs),
        "latency": latency_stats(evs),
        "content": content_motion(evs, strokes, t_end),
        "snaps": [{"frm": d["frm"], "to": d["to"], "moved": round(math.hypot(d["to"][0] - d["frm"][0], d["to"][1] - d["frm"][1]), 1)}
                  for d in decisions if d["what"] == "edge_snap"],
        "scroll_releases": [d.get("quiet") for d in decisions if d["what"] == "scroll_release"],
    }
    out.update(pointer_stats(evs))
    out.update(finger_stats(evs))
    return out


def _count(xs: Iterable[str]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for x in xs:
        c[x] += 1
    return dict(c)


def _wheel_used(evs: list[dict]) -> dict:
    used = [e for e in evs if e["k"] == "wheel_used"]
    if not used:
        return {}
    return {"sum_px": round(sum(e["px"] for e in used), 3), "sum_py": round(sum(e["py"] for e in used), 3),
            "step": used[-1]["step"]}


def _med(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def task_summary(rows: list[dict], info: dict) -> dict:
    """Medians over a task's trials, plus scale ratios."""
    strokes = [s for r in rows for s in r["strokes"] if s]
    pads = [p for r in rows for p in r["pad"]]
    dpi = ((info.get("display") or {}).get("dpi") or (info.get("system") or {}).get("dpi") or 96)
    px_per_mm = dpi / 25.4
    sent_px = sum(math.hypot(s["dx"], s["dy"]) for s in strokes)
    pad_mm = sum(p["path_mm"] for p in pads)
    content = [c for r in rows for c in (r["content"].get("strokes") or [])]
    during = sum(math.hypot(*c["during"]) for c in content)
    coast = sum(math.hypot(*c["coast"]) for c in content)
    s: dict[str, Any] = {
        "trials": len(rows), "ratings": [r["rating"] for r in rows],
        "strokes_per_trial": _med([len(r["strokes"]) for r in rows]),
        "stroke_release_speed_px_s": _med([x["release_speed"] for x in strokes if x.get("released")]),
        "stroke_release_gap_ms": _med([x["release_gap_ms"] for x in strokes if x.get("released")]),
        "zero_speed_releases": sum(1 for x in strokes if x.get("released") and x["release_speed"] < 1),
        "released_strokes": sum(1 for x in strokes if x.get("released")),
        "home_presses": [r["home_presses"] for r in rows],
        "decisions": _count(k for r in rows for k, n in r["decisions"].items() for _ in range(n)),
        "latency_p50_ms": _med([(r["latency"]["post_to_touch_ms"] or {}).get("p50") for r in rows]),
        "latency_p90_ms": _med([(r["latency"]["post_to_touch_ms"] or {}).get("p90") for r in rows]),
        "video_lag_ms": _med([r["content"].get("lag_ms") for r in rows]),
        "content_during_px": round(during, 1), "content_coast_px": round(coast, 1),
        "sent_px": round(sent_px, 1),
        "tracking_ratio": round(during / sent_px, 2) if sent_px and during else None,
        "edge_snaps": [sn for r in rows for sn in r["snaps"]],
    }
    if pads:
        s["pad_mm"] = round(pad_mm, 1)
        s["pad_fingers"] = _med([p["fingers"] for p in pads])
        s["pad_lift_speed_mm_s"] = _med([p["lift_speed_mm_s"] for p in pads])
        if pad_mm:
            s["sent_screen_mm_per_pad_mm"] = round(sent_px / px_per_mm / pad_mm, 2)
            if during:
                s["content_screen_mm_per_pad_mm"] = round(during / px_per_mm / pad_mm, 2)
    wheels = [r["wheel"] for r in rows if r["wheel"]]
    if wheels:
        s["wheel_events"] = sum(w["events"] for w in wheels)
        s["wheel_integer_share"] = _med([w["integer_share"] for w in wheels])
        s["wheel_after_lift_events"] = [w["after_lift_events"] for w in wheels]
        s["wheel_after_lift_ms"] = _med([w["after_lift_ms"] for w in wheels])
        s["wheel_gap_ms"] = _med([w["median_gap_ms"] for w in wheels])
        units = sum(abs(w["sum_py"]) + abs(w["sum_px"]) for w in wheels)
        if pad_mm and units:
            s["wheel_units_per_pad_mm"] = round(units / pad_mm, 3)
        s["scroll_release_quiet_s"] = _med([q for r in rows for q in r["scroll_releases"]])
    drags = [d for r in rows for d in r["drags"]]
    if drags:
        s["pointer_release_speed_px_s"] = _med([d["release_speed"] for d in drags])
        s["pointer_release_gap_ms"] = _med([d["release_gap_ms"] for d in drags])
        if pad_mm:
            s["pointer_px_per_pad_mm"] = round(sum(d["dist"] for d in drags) / pad_mm, 2)
    fingers = [d for r in rows for d in r["fingers"]]
    if fingers:
        s["finger_release_speed_px_s"] = _med([d["release_speed"] for d in fingers])
        s["finger_release_gap_ms"] = _med([d["release_gap_ms"] for d in fingers])
    return s


def calibration(rows_by_task: dict[str, list[dict]], info: dict) -> dict:
    out: dict[str, Any] = {}
    for r in rows_by_task.get("cal_ptr", []):
        mm = sum(p["path_mm"] for p in r["pad"])
        px = 0.0
        for e in r.get("_events", []):
            if e["k"] == "mmove":
                px += math.hypot(*e["rel"])
        if mm:
            out.setdefault("pointer_px_per_mm", []).append(round(px / mm, 2))
    for r in rows_by_task.get("cal_2f", []):
        mm = sum(p["path_mm"] for p in r["pad"])
        w = r["wheel"]
        if mm and w:
            out.setdefault("wheel_units_per_mm", []).append(round((abs(w["sum_py"]) + abs(w["sum_px"])) / mm, 3))
            if r["used"]:
                out.setdefault("sent_px_per_mm", []).append(round(abs(r["used"]["sum_py"]) * r["used"]["step"] / mm, 2))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="phonyscreen gesturelab analyze", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--json", type=Path, help="also write the full per-trial analysis here")
    args = ap.parse_args(argv)
    files = args.files
    if not files:
        from phonyscreen.gesturelab import lab_dir

        found = sorted(lab_dir().glob("lab-*.jsonl"))
        if not found:
            print(f"no recordings in {lab_dir()}", file=sys.stderr)
            return 1
        files = [found[-1]]
    rows_by_task: dict[str, list[dict]] = defaultdict(list)
    info: dict = {}
    order: list[str] = []
    for f in files:
        info_f, trials = split_trials(load(f))
        info = info or info_f
        for tr in trials:
            row = analyze_trial(tr, info_f)
            row["_events"] = tr["events"] if tr["task"].startswith("cal") else []
            rows_by_task[tr["task"]].append(row)
            if tr["task"] not in order:
                order.append(tr["task"])
    print(f"files: {', '.join(str(f) for f in files)}")
    print(f"system: {info.get('system')}  display: {(info.get('display') or {})}  touchpad mm: {info.get('pad_size')}")
    print(f"settings: {info.get('settings')}")
    cal = calibration(rows_by_task, info)
    if cal:
        print(f"calibration: {cal}")
    summaries = {}
    for task in order:
        if task.startswith("cal"):
            continue
        s = task_summary(rows_by_task[task], info)
        summaries[task] = s
        print(f"\n== {task} ({rows_by_task[task][0]['device']}) ratings {s['ratings']}")
        for k, val in s.items():
            if k in ("ratings",) or val in (None, [], {}):
                continue
            print(f"   {k}: {val}")
    if args.json:
        for rows in rows_by_task.values():
            for r in rows:
                r.pop("_events", None)
        args.json.write_text(json.dumps({"info": info, "calibration": cal, "tasks": summaries, "trials": rows_by_task},
                                        indent=1, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
