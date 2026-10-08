from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from window_layout import (  # noqa: E402
    LayoutTarget,
    Rect,
    WS_CHILD,
    WS_EX_TOOLWINDOW,
    WindowInfo,
    WindowLayoutService,
    calculate_grid,
    calculate_packed_layout,
    select_main_windows,
)


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.wait_count = 0
        self.cancel_on_wait: int | None = None

    def clock(self) -> float:
        return self.value

    def wait(self, cancel: threading.Event, seconds: float) -> bool:
        self.wait_count += 1
        self.value += seconds
        if self.cancel_on_wait == self.wait_count:
            cancel.set()
        return cancel.is_set()


class FakeBackend:
    def __init__(self, *rounds: tuple[WindowInfo, ...]) -> None:
        self.rounds = rounds or ((),)
        self.round_index = 0
        self.work_area = Rect(-1920, 0, 0, 1040)
        self.enumerated_pids: list[set[int]] = []
        self.anchor_hwnds: list[int] = []
        self.placements: list[tuple[int, Rect]] = []
        self.fail_hwnds: set[int] = set()

    def enumerate_windows(self, pids: set[int]) -> tuple[WindowInfo, ...]:
        self.enumerated_pids.append(set(pids))
        result = self.rounds[min(self.round_index, len(self.rounds) - 1)]
        self.round_index += 1
        return result

    def work_area_for_window(self, hwnd: int) -> Rect:
        self.anchor_hwnds.append(hwnd)
        return self.work_area

    def place_window(self, hwnd: int, rect: Rect) -> None:
        if hwnd in self.fail_hwnds:
            raise OSError(f"cannot move {hwnd}")
        self.placements.append((hwnd, rect))


def window(hwnd: int, pid: int, width: int = 800, height: int = 600, **values: object) -> WindowInfo:
    return WindowInfo(hwnd, pid, Rect(0, 0, width, height), **values)  # type: ignore[arg-type]


class GridLayoutTests(unittest.TestCase):
    def test_grid_stays_inside_work_area_without_overlap(self) -> None:
        work_area = Rect(-1919, 17, 1, 1057)
        for count in (0, 1, 2, 3, 4, 5, 17):
            with self.subTest(count=count):
                tiles = calculate_grid(work_area, count)
                self.assertEqual(len(tiles), count)
                for tile in tiles:
                    self.assertGreater(tile.width, 0)
                    self.assertGreater(tile.height, 0)
                    self.assertGreaterEqual(tile.left, work_area.left)
                    self.assertGreaterEqual(tile.top, work_area.top)
                    self.assertLessEqual(tile.right, work_area.right)
                    self.assertLessEqual(tile.bottom, work_area.bottom)
                for index, first in enumerate(tiles):
                    for second in tiles[index + 1:]:
                        overlap = (
                            min(first.right, second.right) > max(first.left, second.left)
                            and min(first.bottom, second.bottom) > max(first.top, second.top)
                        )
                        self.assertFalse(overlap, (first, second))

    def test_two_windows_follow_monitor_orientation(self) -> None:
        self.assertEqual(
            calculate_grid(Rect(0, 0, 1920, 1080), 2),
            (Rect(0, 0, 960, 1080), Rect(960, 0, 1920, 1080)),
        )
        self.assertEqual(
            calculate_grid(Rect(0, 0, 1080, 1920), 2),
            (Rect(0, 0, 1080, 960), Rect(0, 960, 1080, 1920)),
        )

    def test_packed_layout_keeps_ratio_and_has_no_gaps_between_windows(self) -> None:
        layout = calculate_packed_layout(
            Rect(0, 0, 2560, 1400),
            ((1600, 900),) * 5,
        )
        self.assertEqual(layout, (
            Rect(0, 0, 853, 480),
            Rect(853, 0, 1706, 480),
            Rect(1706, 0, 2559, 480),
            Rect(0, 480, 853, 960),
            Rect(853, 480, 1706, 960),
        ))
        for rect in layout:
            self.assertLessEqual(abs(rect.width * 900 - rect.height * 1600), 900)

    def test_packed_layout_supports_different_ratios_and_negative_coordinates(self) -> None:
        self.assertEqual(
            calculate_packed_layout(
                Rect(-1920, 0, 0, 1080),
                ((1600, 900), (800, 600), (600, 600)),
            ),
            (
                Rect(-1920, 0, -960, 540),
                Rect(-960, 0, -240, 540),
                Rect(-1920, 540, -1380, 1080),
            ),
        )

    def test_invalid_packed_layout_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_packed_layout(Rect(0, 0, 0, 100), ((16, 9),))
        with self.assertRaises(ValueError):
            calculate_packed_layout(Rect(0, 0, 100, 100), ((0, 9),))

    def test_invalid_grid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_grid(Rect(0, 0, 100, 100), -1)
        with self.assertRaises(ValueError):
            calculate_grid(Rect(0, 0, 0, 100), 1)


class WindowSelectionTests(unittest.TestCase):
    def test_filters_non_game_windows_and_selects_largest_candidate(self) -> None:
        windows = (
            window(1, 10, 200, 100),
            window(2, 10, 900, 700, title="AICore"),
            window(3, 10, 1200, 900, visible=False),
            window(4, 10, 1200, 900, owner=99),
            window(5, 10, 1200, 900, style=WS_CHILD),
            window(6, 10, 1200, 900, ex_style=WS_EX_TOOLWINDOW),
            window(7, 10, 1200, 900, cloaked=True),
            window(8, 10, 1200, 900, class_name="ConsoleWindowClass"),
            window(9, 10, 0, 900),
            window(20, 20, 1600, 900),
        )
        selected = select_main_windows(windows, {10})
        self.assertEqual(set(selected), {10})
        self.assertEqual(selected[10].hwnd, 2)

    def test_empty_title_is_valid_and_ties_are_stable(self) -> None:
        selected = select_main_windows((
            window(30, 10, title=""),
            window(20, 10, title=""),
        ), {10})
        self.assertEqual(selected[10].hwnd, 20)


class WindowLayoutServiceTests(unittest.TestCase):
    def service(self, backend: FakeBackend, fake_time: FakeTime) -> WindowLayoutService:
        return WindowLayoutService(
            backend, clock=fake_time.clock, waiter=fake_time.wait,
        )

    def test_waits_for_stable_delayed_windows_and_preserves_target_order(self) -> None:
        splash = window(102, 10, 300, 200)
        host = window(102, 10, 1000, 700, title="Host")
        client = window(202, 20, 1000, 700, title="Client")
        backend = FakeBackend(
            (),
            (splash,),
            (host, client),
            (host, client),
            (host, client),
        )
        fake_time = FakeTime()
        result = self.service(backend, fake_time).arrange(
            (LayoutTarget(1, 10), LayoutTarget(2, 20)),
            777,
            threading.Event(),
            timeout_seconds=2,
            poll_interval_seconds=0.1,
            stable_seconds=0.2,
        )
        self.assertEqual(result.moved, (1, 2))
        self.assertEqual(result.missing, ())
        self.assertEqual(backend.enumerated_pids[0], {10, 20})
        self.assertEqual(backend.anchor_hwnds, [777])
        self.assertEqual(
            backend.placements,
            [
                (102, Rect(-1920, 0, -960, 672)),
                (202, Rect(-960, 0, 0, 672)),
            ],
        )

    def test_timeout_tiles_found_windows_and_reports_missing_targets(self) -> None:
        host = window(102, 10)
        backend = FakeBackend((host,))
        fake_time = FakeTime()
        result = self.service(backend, fake_time).arrange(
            (LayoutTarget(1, 10), LayoutTarget(2, 20)),
            777,
            threading.Event(),
            timeout_seconds=0.2,
            poll_interval_seconds=0.1,
        )
        self.assertEqual(result.moved, (1,))
        self.assertEqual(result.missing, (2,))
        self.assertEqual(
            backend.placements,
            [(102, Rect(-1920, 0, -534, 1040))],
        )

    def test_cancellation_stops_waiting_without_moving_windows(self) -> None:
        backend = FakeBackend(())
        fake_time = FakeTime()
        fake_time.cancel_on_wait = 1
        result = self.service(backend, fake_time).arrange(
            (LayoutTarget(1, 10),), 777, threading.Event(), timeout_seconds=2,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(backend.placements, [])
        self.assertEqual(backend.anchor_hwnds, [])

    def test_pre_cancelled_request_does_not_enumerate_windows(self) -> None:
        backend = FakeBackend((window(102, 10),))
        fake_time = FakeTime()
        cancel = threading.Event()
        cancel.set()
        result = self.service(backend, fake_time).arrange(
            (LayoutTarget(1, 10),), 777, cancel,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(backend.enumerated_pids, [])

    def test_one_move_failure_does_not_block_other_windows(self) -> None:
        backend = FakeBackend((window(102, 10), window(202, 20)))
        backend.fail_hwnds.add(102)
        fake_time = FakeTime()
        result = self.service(backend, fake_time).arrange(
            (LayoutTarget(1, 10), LayoutTarget(2, 20)),
            777,
            threading.Event(),
            stable_seconds=0,
        )
        self.assertEqual(result.moved, (2,))
        self.assertEqual(tuple(failure.instance_id for failure in result.failures), (1,))
        self.assertEqual(backend.placements[0][0], 202)


if __name__ == "__main__":
    unittest.main()
