import threading
import traceback
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..build import BuildService
from ..changes import ChangesService
from ..process import redact
from ..service import RepoService


class JobThread(QThread):
    event = Signal(str, object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, data_dir: Path, profile, operation: str, selected: list[str], extra=None, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.profile = deepcopy(profile)
        self.operation = operation
        self.selected = list(selected)
        self.extra = deepcopy(extra)
        self.stop_requested = threading.Event()
        self.cancel_requested = threading.Event()

    def run(self):
        try:
            if self.operation in {"build", "build-check"}:
                service = BuildService(self.data_dir, self.event.emit, self.stop_requested, self.cancel_requested)
                self.completed.emit(service.run(self.profile, validate_only=self.operation == "build-check"))
                return
            if self.operation in {"changes", "file-diff", "discard"}:
                changes = ChangesService(self.data_dir, self.event.emit, self.cancel_requested)
                key = self.selected[0]
                if self.operation == "changes":
                    result = changes.scan(self.profile, key)
                elif self.operation == "file-diff":
                    result = changes.diff(self.profile, key, self.extra)
                else:
                    result = changes.discard(self.profile, key, self.extra["snapshot"], self.extra["paths"])
                self.completed.emit(result)
                return
            service = RepoService(self.data_dir, self.event.emit, self.stop_requested, self.cancel_requested)
            if self.operation == "sync":
                result = service.sync(self.profile, self.selected)
            elif self.operation == "setup":
                result = service.setup(self.profile)
            elif self.operation == "branches":
                result = service.branch_list(self.extra)
            else:
                result = service.check(self.profile, self.selected, remote=self.operation == "check")
            self.completed.emit(result)
        except Exception:
            self.failed.emit(redact(traceback.format_exc()))
