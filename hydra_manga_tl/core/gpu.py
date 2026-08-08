"""GPU hardware and native-backend diagnostics for Windows runtimes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib
import os
from pathlib import Path
import site
import subprocess
import sys
from typing import Any


@dataclass
class BackendDiagnostic:
    installed: bool = False
    gpu_ready: bool = False
    version: str = ""
    detail: str = ""
    load_test: str = "not run"
    error: str = ""


@dataclass
class GpuDiagnostic:
    hardware_detected: bool = False
    translation_gpu_ready: bool = False
    device_name: str = ""
    driver_version: str = ""
    compute_capability: str = ""
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    memory_free_mb: int = 0
    utilization_percent: int = 0
    hardware_error: str = ""
    backends: dict[str, BackendDiagnostic] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.translation_gpu_ready:
            return "Ready"
        if self.hardware_detected:
            if not self.backends:
                return "Detected"
            return "Detected — runtime attention required"
        return "No NVIDIA GPU detected"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if not self.hardware_detected:
            return self.status
        memory = (
            f"{self.memory_used_mb / 1024:.1f}/"
            f"{self.memory_total_mb / 1024:.1f} GB VRAM"
            if self.memory_total_mb
            else "VRAM unknown"
        )
        return (
            f"{self.status}: {self.device_name} · {memory} · "
            f"Driver {self.driver_version or 'unknown'}"
        )

    def detail_lines(self) -> list[str]:
        lines = [self.summary()]
        if self.compute_capability:
            lines.append(f"Compute capability: {self.compute_capability}")
        if self.memory_total_mb:
            lines.append(
                f"VRAM: {self.memory_used_mb:,} MiB used · "
                f"{self.memory_free_mb:,} MiB free · "
                f"{self.memory_total_mb:,} MiB total"
            )
        lines.append(f"GPU utilization: {self.utilization_percent}%")
        for name, backend in self.backends.items():
            state = (
                "GPU ready"
                if backend.gpu_ready
                else "installed, CPU only"
                if backend.installed
                else "not available"
            )
            detail = f" · {backend.detail}" if backend.detail else ""
            load = (
                f" · load test: {backend.load_test}"
                if backend.load_test != "not run"
                else ""
            )
            error = f" · {backend.error}" if backend.error else ""
            lines.append(
                f"{name}: {state}"
                f"{f' ({backend.version})' if backend.version else ''}"
                f"{detail}{load}{error}"
            )
        if self.hardware_error:
            lines.append(f"Hardware probe: {self.hardware_error}")
        return lines


def _parse_mib(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    return int(digits or 0)


def _parse_nvidia_smi_line(line: str) -> dict[str, Any]:
    fields = [value.strip() for value in line.strip().split(",")]
    if len(fields) < 8:
        raise ValueError("nvidia-smi returned an incomplete device record")
    return {
        "device_name": fields[0],
        "driver_version": fields[1],
        "memory_total_mb": _parse_mib(fields[2]),
        "memory_used_mb": _parse_mib(fields[3]),
        "memory_free_mb": _parse_mib(fields[4]),
        "utilization_percent": _parse_mib(fields[5]),
        "compute_capability": fields[6],
        "hardware_detected": fields[7].casefold() not in {"n/a", "none", ""},
    }


def _subprocess_startup() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def probe_nvidia_hardware() -> GpuDiagnostic:
    report = GpuDiagnostic()
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used,"
                "memory.free,utilization.gpu,compute_cap,uuid",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            **_subprocess_startup(),
        )
        if result.returncode != 0:
            report.hardware_error = (
                result.stderr.strip()
                or f"nvidia-smi exited with code {result.returncode}"
            )
            return report
        line = next(
            (value for value in result.stdout.splitlines() if value.strip()),
            "",
        )
        values = _parse_nvidia_smi_line(line)
        for name, value in values.items():
            setattr(report, name, value)
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError) as error:
        report.hardware_error = f"{type(error).__name__}: {error}"
    return report


def _probe_torch(run_load_test: bool) -> BackendDiagnostic:
    backend = BackendDiagnostic()
    try:
        torch = importlib.import_module("torch")
        backend.installed = True
        backend.version = str(getattr(torch, "__version__", ""))
        backend.gpu_ready = bool(torch.cuda.is_available())
        cuda_version = str(getattr(getattr(torch, "version", None), "cuda", "") or "")
        if backend.gpu_ready:
            backend.detail = (
                f"{torch.cuda.get_device_name(0)} · CUDA {cuda_version or 'unknown'}"
            )
            if run_load_test:
                allocation = torch.empty((1,), device="cuda")
                torch.cuda.synchronize()
                del allocation
                backend.load_test = "passed"
        else:
            backend.detail = (
                f"CUDA build {cuda_version}, but no CUDA device is available"
                if cuda_version
                else "CPU-only Torch build"
            )
            if run_load_test:
                backend.load_test = "not available"
    except Exception as error:
        backend.error = f"{type(error).__name__}: {error}"
        if run_load_test:
            backend.load_test = "failed"
    return backend


def _probe_paddle(run_load_test: bool) -> BackendDiagnostic:
    backend = BackendDiagnostic()
    try:
        paddle = importlib.import_module("paddle")
        backend.installed = True
        backend.version = str(getattr(paddle, "__version__", ""))
        backend.gpu_ready = bool(paddle.device.is_compiled_with_cuda())
        backend.detail = (
            str(paddle.device.get_device())
            if backend.gpu_ready
            else "CPU-only Paddle build (OCR remains available on CPU)"
        )
        if run_load_test:
            if backend.gpu_ready:
                previous = str(paddle.device.get_device())
                try:
                    paddle.device.set_device("gpu:0")
                    value = paddle.to_tensor([1.0])
                    _ = value.numpy()
                finally:
                    paddle.device.set_device(previous)
                backend.load_test = "passed"
            else:
                backend.load_test = "not available"
    except Exception as error:
        backend.error = f"{type(error).__name__}: {error}"
        if run_load_test:
            backend.load_test = "failed"
    return backend


def _add_nvidia_dll_directories() -> None:
    if os.name != "nt":
        return
    candidates: list[Path] = []
    try:
        candidates.extend(Path(value) for value in site.getsitepackages())
    except (AttributeError, OSError):
        pass
    candidates.append(Path(sys.executable).resolve().parent)
    for root in candidates:
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for folder in nvidia_root.glob("*"):
            bin_dir = folder / "bin"
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except (AttributeError, FileNotFoundError, OSError):
                pass
            current_path = os.environ.get("PATH", "")
            if str(bin_dir) not in current_path.split(os.pathsep):
                os.environ["PATH"] = str(bin_dir) + os.pathsep + current_path


def _probe_llama(run_load_test: bool) -> BackendDiagnostic:
    backend = BackendDiagnostic()
    try:
        _add_nvidia_dll_directories()
        llama_cpp = importlib.import_module("llama_cpp")
        backend.installed = True
        backend.version = str(getattr(llama_cpp, "__version__", ""))
        backend.gpu_ready = bool(llama_cpp.llama_supports_gpu_offload())
        backend.detail = (
            "CUDA GPU offload is supported"
            if backend.gpu_ready
            else "llama.cpp loaded without GPU offload support"
        )
        if run_load_test:
            backend.load_test = "passed"
    except Exception as error:
        backend.error = (
            "Native dependency load failed: "
            f"{type(error).__name__}: {error}"
        )
        if run_load_test:
            backend.load_test = "failed"
    return backend


def collect_gpu_diagnostics(
    *,
    run_load_test: bool = False,
) -> GpuDiagnostic:
    report = probe_nvidia_hardware()
    report.backends = {
        "Torch / Marian": _probe_torch(run_load_test),
        "llama.cpp / Qwen": _probe_llama(run_load_test),
        "Paddle OCR": _probe_paddle(run_load_test),
    }
    report.translation_gpu_ready = any(
        report.backends[name].gpu_ready
        for name in ("Torch / Marian", "llama.cpp / Qwen")
    )
    return report


def translation_gpu_state(
    engine_key: str,
    *,
    engine: Any = None,
) -> str:
    """Return an honest short state without conflating hardware and models."""
    if engine_key not in {"marian", "qwen"}:
        return "Cloud / Not Used"
    hardware = probe_nvidia_hardware()
    if not hardware.hardware_detected:
        return "CPU"
    if engine_key == "qwen":
        if engine is None:
            return "Detected / Runtime not loaded"
        model_path = Path(str(getattr(engine, "model_path", "") or ""))
        if not model_path.is_file():
            return "Detected / Model not ready"
        runtime_config = dict(getattr(engine, "runtime_config", {}) or {})
        if int(runtime_config.get("n_gpu_layers", 0) or 0) == 0:
            return "Detected / CPU configured"
        return "Idle" if bool(getattr(engine, "_loaded", False)) else "Configured"
    torch_backend = _probe_torch(False)
    return "Idle" if torch_backend.gpu_ready else "Detected / Torch CPU"
