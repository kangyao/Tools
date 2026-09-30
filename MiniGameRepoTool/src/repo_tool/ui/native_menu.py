from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QWidget

from ..windows_shell_menu import client_to_screen, show_folder_context_menu


def show_repository_menu(path: Path, owner: QWidget, point: QPoint) -> bool:
    """Point is relative to the owning Qt window, in Qt logical pixels."""
    if not path.is_dir():
        raise OSError("仓库目录不存在")
    handle = int(owner.winId())
    scale = owner.devicePixelRatioF()
    x, y = client_to_screen(handle, round(point.x() * scale), round(point.y() * scale))
    return show_folder_context_menu(str(path), handle, x, y)
