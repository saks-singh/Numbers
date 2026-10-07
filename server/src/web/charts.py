"""Shaping numbers for the templates. No database, no Flask.

Everything here is a pure function over rows that `queries` already cast to
float, which is what makes the whole presentation layer testable without a
Postgres or a browser.

Two rules carried over from the skill's own report:

  * A delta smaller than DELTA_THRESHOLD is rendered as an em dash, not as
    a number. Boot-time run-to-run variance is a few hundred milliseconds,
    so showing every +0.08 s would light the page up every night and train
    people to ignore the colour that matters.
  * Numbers are formatted to exactly three decimals, the precision
    bootbench actually measures. Trailing-zero trimming would make a column
    ragged and unscannable.
"""

from __future__ import annotations

DELTA_THRESHOLD = 0.5

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

# The columns the dashboard shows, in boot order. The sub-components exist
# in the database too, but a six-column table is readable and a sixteen-
# column one is not -- the run page links to the skill's own report for the
# full breakdown.
TABLE_METRICS = (
    ("nhlos_s", "NHLOS"),
    ("kernel_s", "Kernel"),
    ("initramfs_s", "Initramfs"),
    ("sysinit_svc_s", "Sysinit svc"),
    ("total_sysinit_s", "Sysinit"),
    ("total_multiuser_s", "Multi-user"),
)

PRIMARY_METRIC = "total_multiuser_s"


def seconds(value, places=3) -> str:
    """A number as the dashboard shows it, or an em dash for absent."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def duration(start, end) -> str:
    """Wall-clock between two timestamps, as `42m 11s`."""
    if start is None or end is None:
        return "—"
    try:
        total = int((end - start).total_seconds())
    except (TypeError, AttributeError):
        return "—"
    if total < 0:
        return "—"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def delta(current, previous) -> dict:
    """The signed change, and whether it is worth colouring.

    `direction` is "" below the threshold, which is what the template keys
    its colour off -- so the "don't shout about noise" rule lives in exactly
    one place.
    """
    out = {"value": None, "text": "—", "direction": "", "significant": False}
    if current is None or previous is None:
        return out
    try:
        diff = float(current) - float(previous)
    except (TypeError, ValueError):
        return out

    out["value"] = diff
    if abs(diff) < DELTA_THRESHOLD:
        return out

    out["significant"] = True
    out["direction"] = "slower" if diff > 0 else "faster"
    out["text"] = f"{diff:+.3f}"
    return out


def series(rows, key=PRIMARY_METRIC) -> dict:
    """A chart series from `queries.trend` rows, oldest first.

    Rows whose metric is null are kept as nulls rather than dropped: a gap
    in the line is the truth about a run that failed to parse, whereas
    silently joining across it draws a trend that never happened.
    """
    points = []
    for row in rows:
        value = row.get(key)
        points.append({
            "build": row.get("build_number"),
            "run_id": row.get("run_id"),
            "value": None if value is None else round(float(value), 3),
            "status": row.get("status"),
            "label": str(row.get("build_number")
                         if row.get("build_number") is not None
                         else row.get("run_id")),
        })

    values = [p["value"] for p in points if p["value"] is not None]
    return {
        "key": key,
        "points": points,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "count": len(points),
    }


def plot(series_data, *, width=1040, height=240, pad_left=52, pad_right=12,
         pad_top=14, pad_bottom=28) -> dict:
    """Pixel geometry for one server-rendered SVG line chart.

    The chart is computed here and emitted as static SVG rather than handed
    to a charting library in the browser. Three reasons, in order of weight:
    a lab server with no internet cannot load a CDN and this repo has no
    `static/` directory to vendor 200 KB into; a chart that is already in
    the HTML renders with JavaScript disabled and prints correctly; and the
    arithmetic is then plain Python with a unit test, which is the same
    standard the rest of this codebase is held to.

    Segments are straight lines (no smoothing) on purpose: an interpolated
    curve between two nightly builds draws boot times that were never
    measured.

    Returns empty geometry rather than raising when there is nothing to
    plot, so the template's only job is to check `points`.
    """
    points = [p for p in series_data.get("points", []) if p["value"] is not None]
    geometry = {
        "width": width, "height": height, "polyline": "", "points": [],
        "yticks": [], "xticks": [],
        "plot": {"x": pad_left, "y": pad_top,
                 "w": width - pad_left - pad_right,
                 "h": height - pad_top - pad_bottom},
    }
    if not points:
        return geometry

    values = [p["value"] for p in points]
    low, high = min(values), max(values)
    if high - low < 0.5:
        # Pad a flat series so a quiet month does not render as a line
        # wobbling across the full height of the panel.
        mid = (high + low) / 2
        low, high = mid - 0.5, mid + 0.5
    else:
        margin = (high - low) * 0.12
        low, high = low - margin, high + margin

    area = geometry["plot"]
    span = high - low
    step = area["w"] / max(len(points) - 1, 1)

    placed = []
    for index, point in enumerate(points):
        x = area["x"] + (index * step if len(points) > 1
                         else area["w"] / 2)
        y = area["y"] + area["h"] * (1 - (point["value"] - low) / span)
        placed.append({**point, "x": round(x, 2), "y": round(y, 2)})

    geometry["points"] = placed
    geometry["polyline"] = " ".join(f"{p['x']},{p['y']}" for p in placed)

    ticks = 4
    for index in range(ticks + 1):
        value = low + span * index / ticks
        y = area["y"] + area["h"] * (1 - index / ticks)
        geometry["yticks"].append({"y": round(y, 2),
                                   "label": f"{value:.1f}"})

    # At most eight x labels; a nightly chart is 60 builds wide and every
    # label would be unreadable.
    stride = max(1, len(placed) // 8)
    geometry["xticks"] = [
        {"x": p["x"], "label": p["label"]}
        for index, p in enumerate(placed)
        if index % stride == 0 or index == len(placed) - 1
    ]
    return geometry


def sparkline(values, width=12) -> str:
    """The last `width` values as block characters.

    Scaled to its own min/max, so it shows shape and not magnitude -- which
    is all a 12-character glyph can honestly convey. The number beside it
    carries the magnitude.
    """
    numbers = [float(v) for v in values if v is not None]
    numbers = numbers[-width:]
    if len(numbers) < 2:
        return ""
    low, high = min(numbers), max(numbers)
    span = high - low
    if span <= 0:
        return SPARK_BLOCKS[0] * len(numbers)
    out = []
    last = len(SPARK_BLOCKS) - 1
    for value in numbers:
        index = int(round((value - low) / span * last))
        out.append(SPARK_BLOCKS[min(max(index, 0), last)])
    return "".join(out)


def status_class(status) -> str:
    """Maps a run status onto one of four flat colours.

    `partial` is amber rather than green on purpose: a device recording five
    boots out of six is the failure mode this system exists to catch, and
    rendering it as success would hide it.
    """
    return {
        "success": "ok",
        "partial": "warn",
        "failed": "bad",
        "timeout": "bad",
        "unreachable": "bad",
        "cancelled": "muted",
        "queued": "muted",
        "running": "busy",
    }.get(status, "muted")


def action_class(action) -> str:
    return {
        "enqueued": "ok",
        "skipped": "muted",
        "error": "warn",
        "blocked": "bad",
    }.get(action, "muted")


def summarize_device(row, history) -> dict:
    """One index-page card: the newest run plus its delta and sparkline.

    `history` is oldest-first trend rows for the same device, so the delta
    compares the two newest *comparable* boots rather than whatever two runs
    happen to be adjacent -- a failed run in between must not look like a
    regression.
    """
    values = [r.get(PRIMARY_METRIC) for r in history]
    current = values[-1] if values else row.get(PRIMARY_METRIC)
    previous = values[-2] if len(values) > 1 else None
    previous_build = history[-2].get("build_number") if len(history) > 1 else None

    return {
        "device_id": row.get("device_id"),
        "target": row.get("target"),
        "agent_url": row.get("agent_url"),
        "agent_host": _host(row.get("agent_url")),
        "enabled": row.get("enabled", True),
        "notes": row.get("notes") or "",
        "run_id": row.get("run_id"),
        "status": row.get("status"),
        "status_class": status_class(row.get("status")),
        "build_number": row.get("build_number"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "duration": duration(row.get("started_at"), row.get("finished_at")),
        "failure_stage": row.get("failure_stage"),
        "boots_recorded": row.get("boots_recorded"),
        "boots_expected": row.get("boots_expected"),
        "reflashed": row.get("reflashed"),
        "value": current,
        "value_text": seconds(current),
        "delta": delta(current, previous),
        "previous_build": previous_build,
        "sparkline": sparkline(values),
    }


def _host(url) -> str:
    if not url:
        return ""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]
