from __future__ import annotations

import codecs
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def redact(text: str) -> str:
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text, flags=re.I)
    return re.sub(r"([?&](?:access_token|private_token|token|password|secret)=)[^&\s]+",
                  r"\1[redacted]", text, flags=re.I)


@dataclass
class ProcessResult:
    returncode: int
    output: str
    duration: float
    cancelled: bool = False
    timed_out: bool = False


class ProcessRunner:
    def __init__(self, output: Callable[[str], None] | None = None,
                 cancel: threading.Event | None = None):
        self.output = output or (lambda line: None)
        self.cancel = cancel or threading.Event()

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def run(self, program: str, arguments: list[str], cwd: Path | None = None,
            timeout: float | None = None, stream: bool = True,
            environment: dict[str, str | None] | None = None,
            on_terminate: Callable[[], None] | None = None) -> ProcessResult:
        if self.cancel.is_set():
            return ProcessResult(-1, "操作已取消", 0, cancelled=True)
        started = time.monotonic()
        env = os.environ.copy()
        for key, value in (environment or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        # A launcher may itself run inside another repository. Scope always comes from cwd.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_PREFIX", "GIT_IMPLICIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
            env.pop(key, None)
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", PYTHONIOENCODING="utf-8")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen([str(program), *map(str, arguments)], cwd=cwd, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, **options)
        except OSError as error:
            return ProcessResult(-1, str(error), time.monotonic() - started)
        chunks: queue.Queue[bytes | None] = queue.Queue()

        def read() -> None:
            try:
                while data := process.stdout.read1(4096):
                    chunks.put(data)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output = ""
        pending = ""
        ended = False
        cancelled = False
        timed_out = False
        exit_seen: float | None = None
        termination_requested = False
        while not ended or process.poll() is None:
            cancelled = self.cancel.is_set()
            timed_out = timeout is not None and time.monotonic() - started > timeout
            if (cancelled or timed_out) and not termination_requested:
                termination_requested = True
                if on_terminate and process.poll() is None:
                    try:
                        on_terminate()
                    except Exception as error:
                        self.output("停止当前会话失败：" + redact(str(error)))
                self._terminate(process)
            try:
                data = chunks.get(timeout=0.05)
            except queue.Empty:
                data = b""
            if data is None:
                ended = True
                text = decoder.decode(b"", final=True)
            else:
                text = decoder.decode(data)
            output = (output + text)[-4_000_000:]
            pending += text
            lines = re.split(r"[\r\n]", pending)
            pending = lines.pop()
            if stream:
                for line in lines:
                    if line:
                        self.output(redact(line))
            if process.poll() is not None:
                exit_seen = exit_seen or time.monotonic()
                if time.monotonic() - exit_seen > 2:
                    break
        if pending and stream:
            self.output(redact(pending))
        reader.join(timeout=0.2)
        if not reader.is_alive():
            process.stdout.close()
        return ProcessResult(process.wait(), output, time.monotonic() - started, cancelled, timed_out)
