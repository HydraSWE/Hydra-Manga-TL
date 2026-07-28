"""Background import inspection and thumbnail decoding for the desktop UI."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QImageReader

from hydra_manga_tl.project.discovery import SUPPORTED, discover, image_path_sort_key


@dataclass
class ImportScanResult:
    sources: list[tuple[Path, str]]
    total_bytes: int
    formats: dict[str, int]
    average_width: int
    average_height: int
    unreadable: list[str] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return len(self.sources)


class ImportScanWorker(QObject):
    progress = Signal(str, int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, inputs: list[Path]) -> None:
        super().__init__()
        self.inputs = inputs

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("detecting", 0, 0, "Detecting supported images…")
            candidates = self._resolve_candidates()
            if not candidates:
                raise ValueError("No supported images were found.")

            sources: list[tuple[Path, str]] = []
            formats: Counter[str] = Counter()
            dimensions: list[tuple[int, int]] = []
            unreadable: list[str] = []
            total_bytes = 0
            total = len(candidates)
            for current, (source, relative) in enumerate(candidates, 1):
                if QThread.currentThread().isInterruptionRequested():
                    return
                self.progress.emit("metadata", current - 1, total, source.name)
                reader = QImageReader(str(source))
                reader.setAutoTransform(True)
                size = reader.size()
                if not reader.canRead() or not size.isValid():
                    unreadable.append(str(source))
                    continue
                sources.append((source, relative))
                total_bytes += source.stat().st_size
                image_format = bytes(reader.format()).decode("ascii", errors="ignore").upper()
                formats[image_format or source.suffix.lstrip(".").upper()] += 1
                dimensions.append((size.width(), size.height()))
                self.progress.emit("metadata", current, total, source.name)

            if not sources:
                raise ValueError("No readable supported images were found.")
            average_width = round(sum(width for width, _ in dimensions) / len(dimensions))
            average_height = round(sum(height for _, height in dimensions) / len(dimensions))
            self.finished.emit(ImportScanResult(
                sources=sources,
                total_bytes=total_bytes,
                formats=dict(formats),
                average_width=average_width,
                average_height=average_height,
                unreadable=unreadable,
            ))
        except (OSError, ValueError) as error:
            self.failed.emit(str(error))

    def _resolve_candidates(self) -> list[tuple[Path, str]]:
        found: list[tuple[Path, str]] = []
        for raw_entry in self.inputs:
            entry = raw_entry.resolve()
            if entry.is_file() and entry.suffix.lower() in SUPPORTED:
                found.append((entry, entry.name))
            elif entry.is_dir():
                for image in discover(entry):
                    resolved = image.resolve()
                    found.append((resolved, str(resolved.relative_to(entry))))
        unique: dict[Path, str] = {}
        for source, relative in found:
            unique.setdefault(source, relative)
        return sorted(unique.items(), key=lambda pair: image_path_sort_key(pair[0]))


class ThumbnailWorker(QObject):
    thumbnail_ready = Signal(str, object)
    progress = Signal(int, int, str)
    finished = Signal()

    def __init__(self, images: list[tuple[str, str]], size: QSize) -> None:
        super().__init__()
        self.images = images
        self.size = size

    @Slot()
    def run(self) -> None:
        total = len(self.images)
        for current, (image_id, source_path) in enumerate(self.images, 1):
            if QThread.currentThread().isInterruptionRequested():
                break
            reader = QImageReader(source_path)
            reader.setAutoTransform(True)
            source_size = reader.size()
            if source_size.isValid():
                reader.setScaledSize(source_size.scaled(self.size, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                self.thumbnail_ready.emit(image_id, image)
            self.progress.emit(current, total, Path(source_path).name)
        self.finished.emit()
