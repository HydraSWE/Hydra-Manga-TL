"""GpuDiagnosticsWorker implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class GpuDiagnosticsWorker(QObject):
    """Background worker for non-blocking GPU hardware and backend diagnostics."""

    completed = Signal(object)

    def __init__(self, *, run_load_test: bool = False) -> None:
        super().__init__()
        self.run_load_test = run_load_test

    @Slot()
    def run(self) -> None:
        report = collect_gpu_diagnostics(run_load_test=self.run_load_test)
        self.completed.emit(report)


