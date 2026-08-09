"""TranslationTestWorker implementation."""

from __future__ import annotations

from .common import *  # noqa: F401,F403


class TranslationTestWorker(QObject):
    completed = Signal(bool, str)

    def __init__(
        self,
        *,
        qwen_model_path: str | None,
        preferred_engine: str,
        fallback_engine: str | None,
        qwen_model_name: str,
        provider_models: dict[str, str],
        provider_base_urls: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.qwen_model_path = qwen_model_path
        self.preferred_engine = preferred_engine
        self.fallback_engine = fallback_engine
        self.qwen_model_name = qwen_model_name
        self.provider_models = provider_models
        self.provider_base_urls = provider_base_urls or {}
        self._cancel_requested = False
        self._manager = None

    def cancel(self) -> None:
        self._cancel_requested = True
        manager = self._manager
        if manager is not None:
            for engine in tuple(getattr(manager, "engines", {}).values()):
                cancel = getattr(engine, "cancel", None)
                if callable(cancel):
                    cancel()

    def _cancelled(self) -> bool:
        return self._cancel_requested or QThread.currentThread().isInterruptionRequested()

    @Slot()
    def run(self) -> None:
        from hydra_manga_tl.translation.engines import PageDialogue, TranslationEngineManager

        manager = TranslationEngineManager(
            glossary={},
            qwen_model_path=self.qwen_model_path,
            preferred_engine=self.preferred_engine,
            fallback_engine=self.fallback_engine,
            qwen_model_name=self.qwen_model_name,
            provider_models=self.provider_models,
            provider_base_urls=self.provider_base_urls,
            allow_local_fallback_for_cloud=True,
            translation_memory_enabled=False,
        )
        self._manager = manager
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て！"}],
        )
        started = time.perf_counter()
        try:
            if self._cancelled():
                return
            manager.load()
            if self._cancelled():
                return
            result = manager.translate_page(page)
            if self._cancelled():
                return
            sample = str(result.translations[0].get("text", "")) if result.translations else ""
            reported_engine = getattr(manager, "last_engine_id", "")
            engine_key = (
                reported_engine
                if isinstance(reported_engine, str) and reported_engine
                else self.preferred_engine
            )
            engine = manager.engines.get(engine_key)
            self.completed.emit(
                True,
                self._diagnostic_message(
                    engine_key,
                    engine,
                    elapsed=time.perf_counter() - started,
                    sample=sample,
                ),
            )
        except Exception as error:
            if self._cancelled():
                return
            engine = manager.engines.get(self.preferred_engine)
            self.completed.emit(
                False,
                self._diagnostic_message(
                    self.preferred_engine,
                    engine,
                    elapsed=time.perf_counter() - started,
                    error=error,
                ),
            )
        finally:
            manager.unload()
            self._manager = None

    def _diagnostic_message(
        self,
        engine_key: str,
        engine,
        *,
        elapsed: float,
        sample: str = "",
        error: Exception | None = None,
    ) -> str:
        backend = "llama.cpp" if engine_key == "qwen" else (
            "Transformers / MarianMT" if engine_key == "marian" else engine_key
        )
        model_path = self.qwen_model_path or "Not configured"
        device = "Unknown"
        offload = "Not applicable"
        if engine_key == "qwen":
            runtime_config = dict(getattr(engine, "runtime_config", {}) or {})
            gpu_layers = int(runtime_config.get("n_gpu_layers", 0) or 0)
            offload = (
                "CPU only"
                if gpu_layers == 0
                else "Automatic GPU offload"
                if gpu_layers < 0
                else f"{gpu_layers} GPU layer(s)"
            )
            device = "CPU" if gpu_layers == 0 else "GPU offload requested"
            model_path = str(getattr(engine, "model_path", "") or model_path)
        elif engine_key == "marian":
            model_path = "Helsinki-NLP model cache"
            try:
                import torch
                if torch.cuda.is_available():
                    device = torch.cuda.get_device_name(0)
                else:
                    device = "CPU"
            except (ImportError, RuntimeError):
                device = "Unavailable"

        lines = [
            f"Requested engine: {self.preferred_engine}",
            f"Engine used: {engine_key}",
            f"Backend: {backend}",
            f"Device: {device}",
            f"Model path: {model_path}",
            f"Configured offload: {offload}",
            f"Native load: {'Passed' if error is None else 'Failed'}",
            f"Elapsed: {elapsed:.2f}s",
        ]
        gpu_report = collect_gpu_diagnostics(run_load_test=False)
        lines.extend([
            f"GPU hardware: {gpu_report.device_name or gpu_report.status}",
            (
                f"VRAM: {gpu_report.memory_used_mb:,} MiB used / "
                f"{gpu_report.memory_total_mb:,} MiB"
                if gpu_report.memory_total_mb
                else "VRAM: unavailable"
            ),
            f"Driver: {gpu_report.driver_version or 'unavailable'}",
        ])
        for name, diagnostic in gpu_report.backends.items():
            lines.append(
                f"{name}: "
                f"{'GPU ready' if diagnostic.gpu_ready else diagnostic.detail or 'unavailable'}"
                f"{f' — {diagnostic.error}' if diagnostic.error else ''}"
            )
        if error is None:
            lines.append(f"Sample result: {sample or '[empty translation]'}")
        else:
            lines.append(manual_translation_error(error))
            if engine_key == "qwen":
                lines.append(
                    "Action: verify the GGUF path, llama/CUDA DLL availability, "
                    "and try CPU offload if the GPU runtime cannot load."
                )
            elif engine_key == "marian":
                lines.append(
                    "Action: verify the local model cache and Torch device installation."
                )
        return "\n".join(lines)


