from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import launcher  # noqa: E402
from launcher_core import (  # noqa: E402
    DEFAULT_EXECUTABLE,
    NetworkRole,
    format_command_preview,
    parse_argument_text,
)


@unittest.skipUnless(sys.platform == "win32", "Windows launcher UI")
class LauncherUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "settings.json"
        config_patch = patch.object(launcher, "CONFIG_PATH", self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        service_patch = patch.object(launcher.DebugLauncherApp, "_initialize_default_debug_service")
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = launcher.DebugLauncherApp(self.root)
        self.addCleanup(self.app._on_close)

    def test_presets_and_manual_edits_update_preview_and_saved_settings(self) -> None:
        self.app.debug_wait_var.set(False)
        self.app.argument_text_var.set('-App "My App"')
        self.app._apply_network_preset(NetworkRole.CLIENT)
        arguments = self.app.argument_text_var.get()
        self.assertIn('-MGFNetUin 10001', arguments)
        self.app.argument_text_var.set(arguments.replace('10001', '10003'))
        settings = self.app._save_settings()
        self.assertEqual(settings.network.uin, 10003)
        self.assertEqual(settings.network.port, 7000)
        self.assertEqual(settings.arguments[-2:], ('-App', 'My App'))
        self.assertEqual(self.app.preview_var.get(), format_command_preview(settings))
        self.app.argument_text_var.set('')
        self.app._load_settings()
        self.assertEqual(self.app._collect_settings().arguments, settings.arguments)
        self.assertFalse(self.app.debug_wait_var.get())
        self.app._apply_network_preset(NetworkRole.STANDALONE)
        self.assertEqual(parse_argument_text(self.app.argument_text_var.get()), ('-App', 'My App'))

    def test_legacy_config_loads_without_gameplay_arguments(self) -> None:
        self.config.write_text(json.dumps({
            'version': 1,
            'executable': DEFAULT_EXECUTABLE,
            'arguments': ['-MGFTopBattle', '-script-debug-wait-client'],
            'argument_enabled': [True, True],
            'network_role': 'standalone',
            'network_port': 19120,
        }), encoding='utf-8')
        self.app._load_settings()
        self.assertEqual(self.app.argument_text_var.get(), '')
        self.assertTrue(self.app.debug_wait_var.get())
        self.assertEqual(self.app._collect_settings().arguments, ('-script-debug-wait-client',))

    def test_invalid_input_is_shown_and_prevents_start(self) -> None:
        self.app.argument_text_var.set('-MGFNetRole Client -MGFNetUin 1')
        self.assertIn('不能为 1', self.app.preview_var.get())
        with patch.object(self.app, '_show_error') as show_error, patch.object(self.app, '_run_async') as run:
            self.app._start()
        show_error.assert_called_once()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
