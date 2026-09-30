"""Use Windows Explorer native context menus and default item actions.

The implementation intentionally uses only ctypes so Git shell extensions and
other menu handlers registered in Explorer are available without pywin32.
Adapted from the user's Git Fleet tool; kept here so this app is self-contained.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes


if os.name == "nt":
    HRESULT = ctypes.c_long
    ULONG = ctypes.c_ulong
    UINT = wintypes.UINT
    UINT_PTR = ctypes.c_size_t
    DWORD_PTR = ctypes.c_size_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t
    HWND = wintypes.HWND
    HMENU = wintypes.HANDLE
    SUBCLASSPROC = ctypes.WINFUNCTYPE(
        LRESULT, HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR
    )

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class CMINVOKECOMMANDINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.DWORD),
            ("hwnd", HWND),
            ("lpVerb", ctypes.c_void_p),
            ("lpParameters", ctypes.c_char_p),
            ("lpDirectory", ctypes.c_char_p),
            ("nShow", ctypes.c_int),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class CMINVOKECOMMANDINFOEX(ctypes.Structure):
        _fields_ = CMINVOKECOMMANDINFO._fields_ + [
            ("lpTitle", ctypes.c_char_p),
            ("lpVerbW", ctypes.c_void_p),
            ("lpParametersW", ctypes.c_wchar_p),
            ("lpDirectoryW", ctypes.c_wchar_p),
            ("lpTitleW", ctypes.c_wchar_p),
            ("ptInvoke", POINT),
        ]

    def _guid(value: str) -> GUID:
        raw = uuid.UUID(value).bytes_le
        return GUID.from_buffer_copy(raw)

    IID_ISHELLFOLDER = _guid("000214E6-0000-0000-C000-000000000046")
    IID_ICONTEXTMENU = _guid("000214E4-0000-0000-C000-000000000046")
    IID_ICONTEXTMENU2 = _guid("000214F4-0000-0000-C000-000000000046")
    IID_ICONTEXTMENU3 = _guid("BCFCE0A0-EC17-11D0-8D10-00A0C90F2719")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)

    shell32.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    shell32.SHParseDisplayName.restype = HRESULT
    shell32.SHBindToObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHBindToObject.restype = HRESULT
    shell32.SHBindToParent.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHBindToParent.restype = HRESULT
    shell32.ShellExecuteW.argtypes = [
        HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    user32.CreatePopupMenu.restype = HMENU
    user32.DestroyMenu.argtypes = [HMENU]
    user32.TrackPopupMenu.argtypes = [HMENU, UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = UINT
    user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    comctl32.SetWindowSubclass.argtypes = [HWND, SUBCLASSPROC, UINT_PTR, DWORD_PTR]
    comctl32.SetWindowSubclass.restype = wintypes.BOOL
    comctl32.RemoveWindowSubclass.argtypes = [HWND, SUBCLASSPROC, UINT_PTR]
    comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
    comctl32.DefSubclassProc.argtypes = [HWND, UINT, WPARAM, LPARAM]
    comctl32.DefSubclassProc.restype = LRESULT


def _failed(result: int) -> bool:
    return result < 0


def _raise_if_failed(result: int, operation: str) -> None:
    if _failed(result):
        raise OSError(f"{operation} failed (HRESULT 0x{result & 0xFFFFFFFF:08X})")


def _com_method(instance: ctypes.c_void_p, index: int, restype: object, *argtypes: object):
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])
    return function


def _release(instance: ctypes.c_void_p | None) -> None:
    if instance and instance.value:
        _com_method(instance, 2, ULONG)(instance)


def _query_interface(instance: ctypes.c_void_p, iid: GUID) -> ctypes.c_void_p:
    result = ctypes.c_void_p()
    query_interface = _com_method(
        instance, 0, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    if _failed(query_interface(instance, ctypes.byref(iid), ctypes.byref(result))):
        return ctypes.c_void_p()
    return result


def _folder_background_menu(folder: ctypes.c_void_p, owner: int) -> ctypes.c_void_p:
    """Request the folder's own menu: this is Explorer's blank-area menu."""
    context_menu = ctypes.c_void_p()
    create_view_object = _com_method(
        folder, 8, HRESULT, HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    result = create_view_object(folder, HWND(owner), ctypes.byref(IID_ICONTEXTMENU), ctypes.byref(context_menu))
    _raise_if_failed(result, "CreateViewObject")
    return context_menu


def _folder_item_menu(pidl: ctypes.c_void_p, owner: int) -> tuple[ctypes.c_void_p, ctypes.c_void_p]:
    """Fallback when a folder does not expose a background context menu."""
    parent = ctypes.c_void_p()
    child = ctypes.c_void_p()
    result = shell32.SHBindToParent(
        pidl, ctypes.byref(IID_ISHELLFOLDER), ctypes.byref(parent), ctypes.byref(child)
    )
    _raise_if_failed(result, "SHBindToParent")
    context_menu = ctypes.c_void_p()
    children = (ctypes.c_void_p * 1)(child.value)
    reserved = UINT(0)
    get_ui_object = _com_method(
        parent,
        10,
        HRESULT,
        HWND,
        UINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(GUID),
        ctypes.POINTER(UINT),
        ctypes.POINTER(ctypes.c_void_p),
    )
    result = get_ui_object(
        parent,
        HWND(owner),
        1,
        children,
        ctypes.byref(IID_ICONTEXTMENU),
        ctypes.byref(reserved),
        ctypes.byref(context_menu),
    )
    if _failed(result):
        _release(parent)
        _raise_if_failed(result, "GetUIObjectOf")
    return context_menu, parent


def _invoke_menu_command(
    context_menu: ctypes.c_void_p,
    directory: str,
    owner: int,
    command: int,
    first_command: int,
) -> None:
    invoke = CMINVOKECOMMANDINFOEX()
    invoke.cbSize = ctypes.sizeof(invoke)
    invoke.fMask = 0x00004000  # CMIC_MASK_UNICODE
    invoke.hwnd = HWND(owner)
    verb_offset = command - first_command
    invoke.lpVerb = ctypes.c_void_p(verb_offset)
    invoke.lpVerbW = ctypes.c_void_p(verb_offset)
    invoke.lpDirectory = directory.encode("mbcs", errors="replace")
    invoke.lpDirectoryW = directory
    invoke.nShow = 1
    invoke_command = _com_method(context_menu, 4, HRESULT, ctypes.c_void_p)
    _raise_if_failed(invoke_command(context_menu, ctypes.byref(invoke)), "InvokeCommand")


def _show_menu(context_menu: ctypes.c_void_p, directory: str, owner: int, x: int, y: int) -> bool:
    menu = user32.CreatePopupMenu()
    if not menu:
        raise OSError(ctypes.get_last_error(), "CreatePopupMenu failed")
    context_menu3 = _query_interface(context_menu, IID_ICONTEXTMENU3)
    context_menu2 = ctypes.c_void_p()
    if not context_menu3.value:
        context_menu2 = _query_interface(context_menu, IID_ICONTEXTMENU2)
    subclass_id = UINT_PTR(id(context_menu))
    subclass_installed = False

    @SUBCLASSPROC
    def menu_subclass_proc(
        window: HWND,
        message: UINT,
        wparam: WPARAM,
        lparam: LPARAM,
        _subclass_id: UINT_PTR,
        _reference_data: DWORD_PTR,
    ) -> int:
        try:
            if context_menu3.value and message in {0x0117, 0x002B, 0x002C, 0x0120}:
                menu_result = LRESULT()
                handle_menu_message = _com_method(
                    context_menu3, 7, HRESULT, UINT, WPARAM, LPARAM, ctypes.POINTER(LRESULT)
                )
                result = handle_menu_message(
                    context_menu3, message, wparam, lparam, ctypes.byref(menu_result)
                )
                if not _failed(result):
                    return menu_result.value
            elif context_menu2.value and message in {0x0117, 0x002B, 0x002C}:
                handle_menu_message = _com_method(
                    context_menu2, 6, HRESULT, UINT, WPARAM, LPARAM
                )
                if not _failed(handle_menu_message(context_menu2, message, wparam, lparam)):
                    return 0
        except Exception:
            pass
        return comctl32.DefSubclassProc(window, message, wparam, lparam)

    try:
        first_command = 1
        query_context_menu = _com_method(context_menu, 3, HRESULT, HMENU, UINT, UINT, UINT, UINT)
        result = query_context_menu(context_menu, menu, 0, first_command, 0x7FFF, 0)
        _raise_if_failed(result, "QueryContextMenu")
        if context_menu3.value or context_menu2.value:
            if not comctl32.SetWindowSubclass(
                HWND(owner), menu_subclass_proc, subclass_id, DWORD_PTR(0)
            ):
                raise OSError(ctypes.get_last_error(), "SetWindowSubclass failed")
            subclass_installed = True
        command = user32.TrackPopupMenu(menu, 0x0102, x, y, 0, HWND(owner), None)
        if command:
            _invoke_menu_command(context_menu, directory, owner, command, first_command)
        return bool(command)
    finally:
        if subclass_installed:
            comctl32.RemoveWindowSubclass(HWND(owner), menu_subclass_proc, subclass_id)
        _release(context_menu3)
        _release(context_menu2)
        user32.DestroyMenu(menu)


def show_folder_context_menu(path: str, owner: int, x: int, y: int) -> bool:
    """Show Explorer's native background menu for *path* at screen point x/y."""
    if os.name != "nt":
        raise OSError("The native Explorer context menu is available only on Windows.")
    initialization = ole32.CoInitialize(None)
    _raise_if_failed(initialization, "CoInitialize")
    initialized = True
    pidl = ctypes.c_void_p()
    folder = ctypes.c_void_p()
    context_menu = ctypes.c_void_p()
    fallback_parent = ctypes.c_void_p()
    try:
        _raise_if_failed(shell32.SHParseDisplayName(path, None, ctypes.byref(pidl), 0, None), "SHParseDisplayName")
        result = shell32.SHBindToObject(
            None, pidl, None, ctypes.byref(IID_ISHELLFOLDER), ctypes.byref(folder)
        )
        _raise_if_failed(result, "SHBindToObject")
        try:
            context_menu = _folder_background_menu(folder, owner)
        except OSError:
            context_menu, fallback_parent = _folder_item_menu(pidl, owner)
        return _show_menu(context_menu, path, owner, x, y)
    finally:
        _release(context_menu)
        _release(fallback_parent)
        _release(folder)
        if pidl.value:
            ole32.CoTaskMemFree(pidl)
        if initialized:
            ole32.CoUninitialize()

def client_to_screen(owner: int, x: int, y: int) -> tuple[int, int]:
    """Convert native client pixels to screen pixels, including mixed-DPI monitors."""
    if os.name != "nt":
        raise OSError("原生右键菜单仅支持 Windows")
    point = POINT(x, y)
    if not user32.ClientToScreen(HWND(owner), ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "ClientToScreen failed")
    return point.x, point.y


def show_item_context_menu(path: str, owner: int, x: int, y: int) -> None:
    """Show Explorer's native item menu for *path* at screen point x/y."""
    if os.name != "nt":
        raise OSError("The native Explorer context menu is available only on Windows.")
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    initialized = ole32.CoInitialize(None) >= 0
    pidl = ctypes.c_void_p()
    context_menu = ctypes.c_void_p()
    parent = ctypes.c_void_p()
    try:
        _raise_if_failed(
            shell32.SHParseDisplayName(target, None, ctypes.byref(pidl), 0, None),
            "SHParseDisplayName",
        )
        context_menu, parent = _folder_item_menu(pidl, owner)
        _show_menu(context_menu, directory, owner, x, y)
    finally:
        _release(context_menu)
        _release(parent)
        if pidl.value:
            ole32.CoTaskMemFree(pidl)
        if initialized:
            ole32.CoUninitialize()


def open_item_default(path: str, owner: int = 0) -> None:
    """Open *path* with Explorer's default action and its parent as cwd."""
    if os.name != "nt":
        raise OSError("The default Explorer action is available only on Windows.")
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    initialized = ole32.CoInitialize(None) >= 0
    try:
        result = int(shell32.ShellExecuteW(HWND(owner), None, target, None, directory, 1) or 0)
        if result <= 32:
            raise OSError(result, f"ShellExecuteW failed for {target}")
    finally:
        if initialized:
            ole32.CoUninitialize()
