"""PipelineWorker render queue helper."""

from __future__ import annotations

from hydra_manga_tl.phase.phase3 import run as render_phase3
from hydra_manga_tl.phase.render_queue import RENDER_QUEUE
from hydra_manga_tl.translation.requests import RenderRequest


class PipelineWorkerRenderMixin:
    """Submit phase-3 render work for worker-owned render requests."""
    @staticmethod
    def _render_translation_payload(request: RenderRequest):
        return RENDER_QUEUE.submit(
            request,
            lambda result_path, output_dir, policy: render_phase3(
                result_path,
                output_dir,
                policy=policy,
                skip_title_reconstruction=(request.reason == "batch"),
            ),
        ).result()["output"]


