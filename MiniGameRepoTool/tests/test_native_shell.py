import ctypes
import os

import pytest

from repo_tool import windows_shell_menu as shell


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell integration")
def test_native_shell_populates_menu_for_unicode_directory_without_invoking_commands(tmp_path, monkeypatch):
    target = tmp_path / "中文 文件夹"
    target.mkdir()
    item_counts = []

    def inspect_menu(context, directory, owner, x, y):
        # Query the actual Shell/installed extensions, but never display or invoke a command.
        menu = shell.user32.CreatePopupMenu()
        assert menu
        try:
            query = shell._com_method(context, 3, shell.HRESULT, shell.HMENU,
                                      shell.UINT, shell.UINT, shell.UINT, shell.UINT)
            shell._raise_if_failed(query(context, menu, 0, 1, 0x7FFF, 0), "QueryContextMenu")
            count_items = shell.user32.GetMenuItemCount
            count_items.argtypes = [shell.HMENU]
            count_items.restype = ctypes.c_int
            item_counts.append(count_items(menu))
            assert directory == str(target)
            return False
        finally:
            shell.user32.DestroyMenu(menu)

    monkeypatch.setattr(shell, "_show_menu", inspect_menu)
    assert shell.show_folder_context_menu(str(target), 0, 0, 0) is False
    assert len(item_counts) == 1 and item_counts[0] > 0
