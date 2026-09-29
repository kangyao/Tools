from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from .process import redact
from .profiles import default_data_dir
from .ui.window import MainWindow


def configure_appearance(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#f3f5f8"),
        (QPalette.ColorRole.WindowText, "#182536"),
        (QPalette.ColorRole.Base, "#ffffff"),
        (QPalette.ColorRole.AlternateBase, "#f5f7fa"),
        (QPalette.ColorRole.Text, "#182536"),
        (QPalette.ColorRole.Button, "#ffffff"),
        (QPalette.ColorRole.ButtonText, "#182536"),
        (QPalette.ColorRole.Highlight, "#2563eb"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#98a1ae"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#98a1ae"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QPushButton { padding: 7px 12px; border: 1px solid #d5dce5; border-radius: 5px; }
        QPushButton:hover { background: #edf3ff; border-color: #9ebbf9; }
        QPushButton:disabled { background: #f4f5f7; }
        QPushButton#primary { background: #2563eb; color: white; border-color: #2563eb; }
        QPushButton#primary:disabled { background: #b4c9f3; border-color: #b4c9f3; }
        QLineEdit, QComboBox { padding: 5px; border: 1px solid #d5dce5; border-radius: 4px; }
        QGroupBox { font-weight: bold; margin-top: 10px; padding-top: 15px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; }
        QHeaderView::section { padding: 8px 5px; background: #eaf0f6; border: none; }
        QTreeWidget::item { padding-top: 8px; padding-bottom: 8px; }
        QTabBar::tab { padding: 8px 20px; }
        QPlainTextEdit { border: 1px solid #d5dce5; border-radius: 4px; }
        QSplitter::handle { background: #e2e7ef; }
    """)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MiniGame 仓库管理器")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(),
                        help="配置、日志和运行锁所在目录")
    parser.add_argument("--no-scan", action="store_true", help="启动时不检查本地仓库")
    parser.add_argument("--smoke-test", action="store_true", help="仅启动窗口并自动退出，不运行 Git")
    parser.add_argument("--screenshot", type=Path, help="配合 --smoke-test 保存窗口截图")
    args = parser.parse_args(argv)
    if args.screenshot and not args.smoke_test:
        parser.error("--screenshot 需要同时传 --smoke-test")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("MiniGameRepoTool")
    app.setOrganizationName("MiniGameTools")
    configure_appearance(app)
    try:
        window = MainWindow(args.data_dir.resolve(), auto_scan=not (args.no_scan or args.smoke_test))
    except (OSError, ValueError) as error:
        message = redact(str(error)) + "\n\n原配置会保留。可检查 profiles.json 或备份 profiles.json.bak。"
        if args.smoke_test:
            print(message, file=sys.stderr)
        else:
            QMessageBox.critical(None, "启动失败", message)
        return 1

    def exception_hook(kind, value, trace):
        message = redact("".join(traceback.format_exception(kind, value, trace)))
        args.data_dir.mkdir(parents=True, exist_ok=True)
        with (args.data_dir / "application-errors.log").open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")
        if args.smoke_test:
            print(message, file=sys.stderr)
            app.exit(1)
        else:
            QMessageBox.critical(window, "操作失败", message)

    sys.excepthook = exception_hook
    window.show()
    if args.smoke_test:
        def finish():
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot)):
                    raise OSError("截图保存失败")
            window.close()
            app.quit()
        QTimer.singleShot(500, finish)
    return app.exec()
