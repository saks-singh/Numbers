"""Presentation arithmetic: deltas, series, SVG geometry, sparklines.

Pure functions, so these are the cheapest tests in the suite and they cover
the part of the dashboard most likely to lie quietly. Three properties are
load-bearing rather than cosmetic:

  * a sub-threshold delta renders as an em dash with no direction, because
    a page that flags every +0.08 s teaches people to ignore the colour
    that matters;
  * a null metric stays a null in the series -- a gap in the line is the
    truth about a run that failed to parse, and joining across it draws a
    trend that never happened;
  * `summarize_device` compares the two newest *comparable* boots from the
    trend, not two adjacent runs, so a failed run in between cannot look
    like a regression.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.web import charts  # noqa: E402


def trend_rows(pairs, status="success"):
    """Oldest-first trend rows: `pairs` is [(build_number, value), ...]."""
    return [{"build_number": build, "run_id": 100 + index,
             "status": status, "total_multiuser_s": value}
            for index, (build, value) in enumerate(pairs)]


class TestSeconds(unittest.TestCase):
    def test_three_decimals_always(self):
        self.assertEqual(charts.seconds(12.4), "12.400")
        self.assertEqual(charts.seconds(12), "12.000")
        self.assertEqual(charts.seconds("12.4815"), "12.482")

    def test_absent_and_garbage_are_dashes(self):
        for value in (None, "", "n/a", object()):
            with self.subTest(value=value):
                self.assertEqual(charts.seconds(value), "—")


class TestDuration(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 6, 1, 4, tzinfo=timezone.utc)

    def test_minutes_and_seconds(self):
        end = self.start + timedelta(minutes=42, seconds=11)
        self.assertEqual(charts.duration(self.start, end), "42m 11s")

    def test_hours(self):
        end = self.start + timedelta(hours=1, minutes=7)
        self.assertEqual(charts.duration(self.start, end), "1h 07m")

    def test_seconds_only(self):
        end = self.start + timedelta(seconds=9)
        self.assertEqual(charts.duration(self.start, end), "9s")

    def test_unfinished_run_is_a_dash(self):
        self.assertEqual(charts.duration(self.start, None), "—")
        self.assertEqual(charts.duration(None, None), "—")

    def test_negative_is_a_dash(self):
        """Clock skew between the bench host and the coordinator must not
        render as a negative duration."""
        end = self.start - timedelta(minutes=1)
        self.assertEqual(charts.duration(self.start, end), "—")


class TestDelta(unittest.TestCase):
    def test_noise_is_not_shouted_about(self):
        out = charts.delta(12.48, 12.40)
        self.assertEqual(out["text"], "—")
        self.assertEqual(out["direction"], "")
        self.assertFalse(out["significant"])
        # The value is still there for anyone who wants it; only the
        # rendering is suppressed.
        self.assertAlmostEqual(out["value"], 0.08, places=3)

    def test_threshold_is_exclusive(self):
        just_under = charts.delta(12.0 + charts.DELTA_THRESHOLD - 0.001, 12.0)
        self.assertFalse(just_under["significant"])
        at = charts.delta(12.0 + charts.DELTA_THRESHOLD, 12.0)
        self.assertTrue(at["significant"])

    def test_signed_text_and_direction(self):
        slower = charts.delta(13.0, 12.0)
        self.assertEqual(slower["text"], "+1.000")
        self.assertEqual(slower["direction"], "slower")

        faster = charts.delta(12.0, 13.0)
        self.assertEqual(faster["text"], "-1.000")
        self.assertEqual(faster["direction"], "faster")

    def test_no_previous_build_is_not_an_improvement(self):
        """A device's first run has nothing to compare against. Rendering
        that as a delta of zero, or as green, would be a lie."""
        out = charts.delta(12.0, None)
        self.assertIsNone(out["value"])
        self.assertEqual(out["text"], "—")
        self.assertFalse(out["significant"])


class TestSeries(unittest.TestCase):
    def test_nulls_are_kept_as_gaps(self):
        rows = trend_rows([(2465, 12.1), (2468, None), (2471, 12.5)])
        out = charts.series(rows)
        self.assertEqual([p["value"] for p in out["points"]],
                         [12.1, None, 12.5])
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["min"], 12.1)
        self.assertEqual(out["max"], 12.5)

    def test_all_null_series_has_no_bounds(self):
        out = charts.series(trend_rows([(2465, None), (2468, None)]))
        self.assertIsNone(out["min"])
        self.assertIsNone(out["max"])
        self.assertEqual(out["count"], 2)

    def test_label_falls_back_to_run_id(self):
        """A branch build has no build number (BUILD_NAME_RE only matches
        master nightlies), and an unlabelled point is unclickable."""
        rows = [{"build_number": None, "run_id": 77, "status": "success",
                 "total_multiuser_s": 12.0}]
        self.assertEqual(charts.series(rows)["points"][0]["label"], "77")

    def test_alternate_metric(self):
        rows = [{"build_number": 2471, "run_id": 1, "status": "success",
                 "nhlos_s": 2.104, "total_multiuser_s": 12.481}]
        out = charts.series(rows, "nhlos_s")
        self.assertEqual(out["key"], "nhlos_s")
        self.assertEqual(out["points"][0]["value"], 2.104)

    def test_empty(self):
        out = charts.series([])
        self.assertEqual(out["points"], [])
        self.assertEqual(out["count"], 0)


class TestPlot(unittest.TestCase):
    def test_empty_series_returns_empty_geometry(self):
        """The template's only check is `points`, so this must not raise."""
        geometry = charts.plot(charts.series([]))
        self.assertEqual(geometry["points"], [])
        self.assertEqual(geometry["polyline"], "")
        self.assertEqual(geometry["yticks"], [])

    def test_points_stay_inside_the_plot_area(self):
        rows = trend_rows([(2460 + i, 10 + i * 0.4) for i in range(10)])
        geometry = charts.plot(charts.series(rows))
        area = geometry["plot"]
        self.assertEqual(len(geometry["points"]), 10)
        for point in geometry["points"]:
            self.assertGreaterEqual(point["x"], area["x"] - 0.01)
            self.assertLessEqual(point["x"], area["x"] + area["w"] + 0.01)
            self.assertGreaterEqual(point["y"], area["y"] - 0.01)
            self.assertLessEqual(point["y"], area["y"] + area["h"] + 0.01)

    def test_higher_is_drawn_lower(self):
        """SVG y grows downward. Getting this backwards would draw every
        regression as an improvement."""
        rows = trend_rows([(2465, 10.0), (2468, 14.0)])
        points = charts.plot(charts.series(rows))["points"]
        self.assertLess(points[1]["y"], points[0]["y"])

    def test_x_advances_left_to_right_oldest_first(self):
        rows = trend_rows([(2460 + i, 12.0 + i) for i in range(5)])
        xs = [p["x"] for p in charts.plot(charts.series(rows))["points"]]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(len(set(xs)), 5)

    def test_nulls_are_omitted_from_the_geometry(self):
        rows = trend_rows([(2465, 12.1), (2468, None), (2471, 12.5)])
        geometry = charts.plot(charts.series(rows))
        self.assertEqual(len(geometry["points"]), 2)
        self.assertEqual(len(geometry["polyline"].split()), 2)

    def test_single_point_is_centred_not_crashed(self):
        geometry = charts.plot(charts.series(trend_rows([(2471, 12.4)])))
        area = geometry["plot"]
        self.assertEqual(len(geometry["points"]), 1)
        self.assertAlmostEqual(geometry["points"][0]["x"],
                               area["x"] + area["w"] / 2, places=1)

    def test_flat_series_is_padded(self):
        """Six identical builds must render as a flat line, not as noise
        amplified across the full height of the panel."""
        rows = trend_rows([(2460 + i, 12.0) for i in range(6)])
        geometry = charts.plot(charts.series(rows))
        ys = {p["y"] for p in geometry["points"]}
        self.assertEqual(len(ys), 1)
        area = geometry["plot"]
        self.assertAlmostEqual(ys.pop(), area["y"] + area["h"] / 2, places=1)

    def test_x_labels_are_thinned_but_keep_the_newest(self):
        rows = trend_rows([(2400 + i, 12.0 + (i % 3)) for i in range(60)])
        geometry = charts.plot(charts.series(rows))
        self.assertLessEqual(len(geometry["xticks"]), 10)
        self.assertEqual(geometry["xticks"][-1]["label"], "2459")

    def test_yticks_span_the_axis(self):
        rows = trend_rows([(2465, 10.0), (2471, 14.0)])
        geometry = charts.plot(charts.series(rows))
        area = geometry["plot"]
        ys = [t["y"] for t in geometry["yticks"]]
        self.assertEqual(len(ys), 5)
        self.assertAlmostEqual(min(ys), area["y"], places=1)
        self.assertAlmostEqual(max(ys), area["y"] + area["h"], places=1)


class TestSparkline(unittest.TestCase):
    def test_shape_not_magnitude(self):
        spark = charts.sparkline([10, 11, 12, 13])
        self.assertEqual(len(spark), 4)
        self.assertEqual(spark[0], charts.SPARK_BLOCKS[0])
        self.assertEqual(spark[-1], charts.SPARK_BLOCKS[-1])
        # Scaled to its own range, so a +1 s climb and a +100 s climb draw
        # the same glyph. The number beside it carries the magnitude.
        self.assertEqual(spark, charts.sparkline([10, 110, 210, 310]))

    def test_flat_is_flat(self):
        self.assertEqual(charts.sparkline([12, 12, 12]),
                         charts.SPARK_BLOCKS[0] * 3)

    def test_too_few_points_renders_nothing(self):
        self.assertEqual(charts.sparkline([]), "")
        self.assertEqual(charts.sparkline([12.0]), "")
        self.assertEqual(charts.sparkline([None, None]), "")

    def test_truncated_to_the_newest(self):
        spark = charts.sparkline(list(range(40)), width=12)
        self.assertEqual(len(spark), 12)

    def test_nulls_are_dropped(self):
        self.assertEqual(len(charts.sparkline([10, None, 12, None, 14])), 3)


class TestStatusClasses(unittest.TestCase):
    def test_partial_is_amber_not_green(self):
        """A device recording five boots out of six is the failure mode this
        system exists to catch."""
        self.assertEqual(charts.status_class("partial"), "warn")
        self.assertNotEqual(charts.status_class("partial"),
                            charts.status_class("success"))

    def test_known_statuses(self):
        cases = {"success": "ok", "failed": "bad", "timeout": "bad",
                 "unreachable": "bad", "cancelled": "muted",
                 "queued": "muted", "running": "busy"}
        for status, klass in cases.items():
            with self.subTest(status=status):
                self.assertEqual(charts.status_class(status), klass)

    def test_unknown_and_none_are_muted(self):
        self.assertEqual(charts.status_class(None), "muted")
        self.assertEqual(charts.status_class("invented"), "muted")

    def test_blocked_decision_is_the_loudest(self):
        self.assertEqual(charts.action_class("blocked"), "bad")
        self.assertEqual(charts.action_class("enqueued"), "ok")
        self.assertEqual(charts.action_class("skipped"), "muted")
        self.assertEqual(charts.action_class(None), "muted")


class TestSummarizeDevice(unittest.TestCase):
    def setUp(self):
        self.row = {
            "device_id": "iq-9075-evk-01", "target": "iq-9075-evk",
            "agent_url": "http://bench-win-01:8765", "status": "success",
            "build_number": 2471, "run_id": 9,
            "started_at": datetime(2026, 10, 6, 1, 4, tzinfo=timezone.utc),
            "finished_at": datetime(2026, 10, 6, 1, 46, 11,
                                    tzinfo=timezone.utc),
            "total_multiuser_s": 12.481, "boots_recorded": 6,
            "boots_expected": 6, "reflashed": True,
        }

    def test_delta_comes_from_the_trend_not_adjacent_runs(self):
        history = trend_rows([(2462, 11.0), (2465, 11.9), (2471, 12.481)])
        card = charts.summarize_device(self.row, history)
        self.assertEqual(card["value_text"], "12.481")
        self.assertEqual(card["previous_build"], 2465)
        self.assertAlmostEqual(card["delta"]["value"], 0.581, places=3)
        self.assertEqual(card["delta"]["direction"], "slower")

    def test_first_ever_run_has_no_delta(self):
        card = charts.summarize_device(self.row, trend_rows([(2471, 12.481)]))
        self.assertEqual(card["delta"]["text"], "—")
        self.assertIsNone(card["previous_build"])
        self.assertEqual(card["sparkline"], "")

    def test_no_history_falls_back_to_the_row(self):
        card = charts.summarize_device(self.row, [])
        self.assertEqual(card["value_text"], "12.481")
        self.assertEqual(card["delta"]["text"], "—")

    def test_agent_host_is_stripped_for_display(self):
        card = charts.summarize_device(self.row, [])
        self.assertEqual(card["agent_host"], "bench-win-01:8765")

    def test_duration_and_status_class(self):
        card = charts.summarize_device(self.row, [])
        self.assertEqual(card["duration"], "42m 11s")
        self.assertEqual(card["status_class"], "ok")

    def test_device_with_no_runs_at_all(self):
        """The index page renders a card for a device in devices.yaml that
        the database has never seen; it must not raise."""
        card = charts.summarize_device({"device_id": "new-01"}, [])
        self.assertEqual(card["value_text"], "—")
        self.assertEqual(card["status_class"], "muted")
        self.assertEqual(card["duration"], "—")
        self.assertEqual(card["agent_host"], "")


if __name__ == "__main__":
    unittest.main()
