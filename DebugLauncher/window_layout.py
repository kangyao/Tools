from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from fractions import Fraction
import math
import os
import threading
import time
from typing import Callable, Collection, Protocol, Sequence


WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
CONSOLE_WINDOW_CLASSES = frozenset((
    "consolewindowclass",
    "cascadia_hosting_window_class",
))


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)


@dataclass(frozen=True)
class LayoutTarget:
    instance_id: int
    pid: int


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    rect: Rect
    visible: bool = True
    owner: int = 0
    style: int = 0
    ex_style: int = 0
    class_name: str = ""
    title: str = ""
    cloaked: bool = False


@dataclass(frozen=True)
class LayoutFailure:
    instance_id: int
    message: str


@dataclass(frozen=True)
class LayoutResult:
    moved: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    failures: tuple[LayoutFailure, ...] = ()
    cancelled: bool = False


class WindowBackend(Protocol):
    def enumerate_windows(self, pids: Collection[int]) -> tuple[WindowInfo, ...]: ...

    def work_area_for_window(self, hwnd: int) -> Rect: ...

    def place_window(self, hwnd: int, rect: Rect) -> None: ...


def _grid_shape(work_area: Rect, count: int) -> tuple[int, int]:
    if count <= 0:
        raise ValueError("窗口数量必须是正整数。")
    if work_area.width <= 0 or work_area.height <= 0:
        raise ValueError("显示器工作区必须具有正宽度和正高度。")
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    if work_area.height > work_area.width:
        columns, rows = rows, columns
    return columns, rows


def calculate_grid(work_area: Rect, count: int) -> tuple[Rect, ...]:
    """Split a monitor work area into stable, row-major tiles."""

    if count < 0:
        raise ValueError("窗口数量不能为负数。")
    if count == 0:
        return ()
    columns, rows = _grid_shape(work_area, count)

    tiles: list[Rect] = []
    for index in range(count):
        row, column = divmod(index, columns)
        left = work_area.left + work_area.width * column // columns
        right = work_area.left + work_area.width * (column + 1) // columns
        top = work_area.top + work_area.height * row // rows
        bottom = work_area.top + work_area.height * (row + 1) // rows
        tiles.append(Rect(left, top, right, bottom))
    return tuple(tiles)


def calculate_packed_layout(
    work_area: Rect, source_sizes: Sequence[tuple[int, int]],
) -> tuple[Rect, ...]:
    """Pack aspect-preserving windows tightly from the work area's top-left corner."""

    if not source_sizes:
        return ()
    if any(width <= 0 or height <= 0 for width, height in source_sizes):
        raise ValueError("原窗口必须具有正宽度和正高度。")
    columns, rows = _grid_shape(work_area, len(source_sizes))
    ratios = tuple(Fraction(width, height) for width, height in source_sizes)
    row_ratios = (
        sum(ratios[index:index + columns], Fraction())
        for index in range(0, len(ratios), columns)
    )
    widest_row_ratio = max(row_ratios)
    height_from_width = (
        work_area.width * widest_row_ratio.denominator // widest_row_ratio.numerator
    )
    row_height = min(work_area.height // rows, height_from_width)
    if row_height <= 0:
        raise ValueError("显示器工作区太小，无法排列窗口。")

    layout: list[Rect] = []
    for index, (source_width, source_height) in enumerate(source_sizes):
        row, column = divmod(index, columns)
        if column == 0:
            left = work_area.left
        else:
            left = layout[-1].right
        width = max(1, row_height * source_width // source_height)
        top = work_area.top + row * row_height
        rect = Rect(left, top, left + width, top + row_height)
        if rect.right > work_area.right or rect.bottom > work_area.bottom:
            raise ValueError("显示器工作区太小，无法排列窗口。")
        layout.append(rect)
    return tuple(layout)


def is_main_window_candidate(window: WindowInfo, pids: Collection[int]) -> bool:
    return (
        window.pid in pids
        and window.visible
        and window.owner == 0
        and not window.style & WS_CHILD
        and not window.ex_style & WS_EX_TOOLWINDOW
        and not window.cloaked
        and window.rect.area > 0
        and window.class_name.casefold() not in CONSOLE_WINDOW_CLASSES
    )


def select_main_windows(
    windows: Sequence[WindowInfo], pids: Collection[int],
) -> dict[int, WindowInfo]:
    """Pick one stable main-window candidate for each managed process."""

    selected: dict[int, WindowInfo] = {}
    for window in windows:
        if not is_main_window_candidate(window, pids):
            continue
        current = selected.get(window.pid)
        score = (window.rect.area, bool(window.title), -window.hwnd)
        if current is None:
            selected[window.pid] = window
            continue
        current_score = (current.rect.area, bool(current.title), -current.hwnd)
        if score > current_score:
            selected[window.pid] = window
    return selected


class WindowLayoutService:
    def __init__(
        self,
        backend: WindowBackend | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        waiter: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self.backend = backend or Win32WindowBackend()
        self._clock = clock
        self._waiter = waiter or (lambda event, seconds: event.wait(seconds))

    def arrange(
        self,
        targets: Sequence[LayoutTarget],
        anchor_hwnd: int,
        cancel: threading.Event,
        *,
        timeout_seconds: float = 12.0,
        poll_interval_seconds: float = 0.1,
        stable_seconds: float = 0.4,
    ) -> LayoutResult:
        """Wait for managed top-level windows, then tile every window found."""

        ordered = tuple(targets)
        if not ordered:
            return LayoutResult()
        if timeout_seconds < 0 or poll_interval_seconds <= 0 or stable_seconds < 0:
            raise ValueError("窗口等待时间配置无效。")
        if cancel.is_set():
            return LayoutResult(cancelled=True)

        pids = {target.pid for target in ordered}
        deadline = self._clock() + timeout_seconds
        fingerprint: tuple[tuple[int, int | None, Rect | None], ...] | None = None
        stable_since = self._clock()
        selected: dict[int, WindowInfo] = {}

        while True:
            if cancel.is_set():
                return LayoutResult(cancelled=True)
            selected = select_main_windows(self.backend.enumerate_windows(pids), pids)
            current = tuple(
                (
                    target.pid,
                    selected[target.pid].hwnd if target.pid in selected else None,
                    selected[target.pid].rect if target.pid in selected else None,
                )
                for target in ordered
            )
            now = self._clock()
            if current != fingerprint:
                fingerprint = current
                stable_since = now
            all_found = all(target.pid in selected for target in ordered)
            if all_found and now - stable_since >= stable_seconds:
                break
            if now >= deadline:
                break
            wait_seconds = min(poll_interval_seconds, max(0.0, deadline - now))
            if self._waiter(cancel, wait_seconds):
                return LayoutResult(cancelled=True)

        found = tuple(target for target in ordered if target.pid in selected)
        missing = tuple(target.instance_id for target in ordered if target.pid not in selected)
        if not found:
            return LayoutResult(missing=missing)
        if cancel.is_set():
            return LayoutResult(missing=missing, cancelled=True)

        work_area = self.backend.work_area_for_window(anchor_hwnd)
        layout = calculate_packed_layout(
            work_area,
            tuple(
                (selected[target.pid].rect.width, selected[target.pid].rect.height)
                for target in found
            ),
        )
        moved: list[int] = []
        failures: list[LayoutFailure] = []
        for target, rect in zip(found, layout):
            if cancel.is_set():
                return LayoutResult(
                    moved=tuple(moved), missing=missing,
                    failures=tuple(failures), cancelled=True,
                )
            window = selected[target.pid]
            try:
                self.backend.place_window(window.hwnd, rect)
            except Exception as exc:
                failures.append(LayoutFailure(target.instance_id, str(exc)))
            else:
                moved.append(target.instance_id)
        return LayoutResult(tuple(moved), missing, tuple(failures))


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_WNDENUMPROC_TYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDENUMPROC = _WNDENUMPROC_TYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class Win32WindowBackend:
    GW_OWNER = 4
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    MONITOR_DEFAULTTONEAREST = 2
    SW_RESTORE = 9
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_NOOWNERZORDER = 0x0200
    SWP_ASYNCWINDOWPOS = 0x4000
    DWMWA_CLOAKED = 14

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("窗口平铺仅支持 Windows。")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = self._load_dwmapi()
        self._configure_functions()

    def _load_dwmapi(self) -> object | None:
        try:
            return ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:
            return None

    def _configure_functions(self) -> None:
        user32 = self.user32
        user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        self._get_window_long = getattr(user32, "GetWindowLongPtrW", None)
        if self._get_window_long is None:
            self._get_window_long = user32.GetWindowLongW
        self._get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_window_long.restype = ctypes.c_ssize_t
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        if self.dwmapi is not None:
            self.dwmapi.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ]
            self.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    @staticmethod
    def _rect(value: _RECT) -> Rect:
        return Rect(int(value.left), int(value.top), int(value.right), int(value.bottom))

    @staticmethod
    def _last_error(prefix: str) -> OSError:
        code = ctypes.get_last_error()
        detail = ctypes.FormatError(code).strip() if code else "未知错误"
        return OSError(f"{prefix}（Windows 错误 {code}：{detail}）")

    def _class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        length = self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length else ""

    def _title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value[:copied] if copied else ""

    def _is_cloaked(self, hwnd: int) -> bool:
        if self.dwmapi is None:
            return False
        value = wintypes.DWORD()
        result = self.dwmapi.DwmGetWindowAttribute(
            hwnd, self.DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value),
        )
        return result == 0 and bool(value.value)

    def enumerate_windows(self, pids: Collection[int]) -> tuple[WindowInfo, ...]:
        wanted = set(pids)
        windows: list[WindowInfo] = []

        @_WNDENUMPROC
        def callback(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) not in wanted:
                return True
            rect = _RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            windows.append(WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid.value),
                rect=self._rect(rect),
                visible=bool(self.user32.IsWindowVisible(hwnd)),
                owner=int(self.user32.GetWindow(hwnd, self.GW_OWNER) or 0),
                style=int(self._get_window_long(hwnd, self.GWL_STYLE)),
                ex_style=int(self._get_window_long(hwnd, self.GWL_EXSTYLE)),
                class_name=self._class_name(hwnd),
                title=self._title(hwnd),
                cloaked=self._is_cloaked(hwnd),
            ))
            return True

        ctypes.set_last_error(0)
        if not self.user32.EnumWindows(callback, 0):
            raise self._last_error("枚举 App 窗口失败")
        return tuple(windows)

    def work_area_for_window(self, hwnd: int) -> Rect:
        monitor = self.user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            raise self._last_error("查找启动器所在显示器失败")
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise self._last_error("读取显示器工作区失败")
        return self._rect(info.rcWork)

    def place_window(self, hwnd: int, rect: Rect) -> None:
        if self.user32.IsIconic(hwnd) or self.user32.IsZoomed(hwnd):
            self.user32.ShowWindowAsync(hwnd, self.SW_RESTORE)
        flags = (
            self.SWP_NOZORDER | self.SWP_NOACTIVATE
            | self.SWP_NOOWNERZORDER | self.SWP_ASYNCWINDOWPOS
        )
        if not self.user32.SetWindowPos(
            hwnd, 0, rect.left, rect.top, rect.width, rect.height, flags,
        ):
            raise self._last_error("设置 App 窗口位置失败")
