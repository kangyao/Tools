import os
import subprocess
import sys
from pathlib import Path

from repo_tool.profiles import ProfileStore


def test_real_entrypoint_opens_window_and_reports_invalid_config(tmp_path):
    entry = Path(__file__).resolve().parents[1] / "run.py"
    data = tmp_path / "settings"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(entry), "--smoke-test", "--data-dir", str(data)],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    store = ProfileStore(data)
    assert len(store.load().profiles[0].repositories) == 8
    store.path.write_text("{ broken", encoding="utf-8")
    result = subprocess.run([sys.executable, str(entry), "--smoke-test", "--data-dir", str(data)],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 1
    assert store.path.read_text(encoding="utf-8") == "{ broken"
