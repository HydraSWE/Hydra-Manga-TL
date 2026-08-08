"""Child-process host for the native Qwen GGUF runtime."""

from __future__ import annotations

import traceback
from dataclasses import asdict
from multiprocessing.connection import Connection
from typing import Any


def _send_message(connection: Connection, message: dict[str, Any]) -> bool:
    try:
        connection.send(message)
        return True
    except (BrokenPipeError, EOFError, OSError):
        return False


def _serialize_error(error: BaseException) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
    }


def run_qwen_worker(connection: Connection, config: dict[str, Any]) -> None:
    """Run one Qwen engine behind a pipe until the parent shuts it down."""
    from hydra_manga_tl.translation.engines.base import PageDialogue
    from hydra_manga_tl.translation.engines.qwen_engine import QwenGGUFEngine

    engine = QwenGGUFEngine(
        model_path=config.get("model_path"),
        glossary=dict(config.get("glossary") or {}),
        model_name=str(config.get("model_name") or "Qwen3-4B-Instruct-2507"),
        runtime_config=dict(config.get("runtime_config") or {}),
    )
    try:
        if not _send_message(connection, {"event": "state", "state": "LOADING_MODEL"}):
            return
        engine.load()
        if not _send_message(connection, {"event": "state", "state": "READY", "engine_id": engine.engine_id}):
            return
        while True:
            try:
                message = connection.recv()
            except (BrokenPipeError, EOFError, OSError):
                break
            command = str(message.get("command") or "")
            request_id = str(message.get("request_id") or "")
            if command == "shutdown":
                break
            if command == "ping":
                if not _send_message(connection, {
                    "event": "response",
                    "request_id": request_id,
                    "ok": True,
                    "state": "READY",
                    "engine_id": engine.engine_id,
                }):
                    break
                continue
            if command == "translate_page":
                payload = dict(message.get("page") or {})
                page = PageDialogue(
                    source_language=str(payload.get("source_language") or ""),
                    target_language=str(payload.get("target_language") or ""),
                    dialogue=list(payload.get("dialogue") or []),
                    page_context=payload.get("page_context"),
                )
                result = engine.translate_page(page)
                if not _send_message(connection, {
                    "event": "response",
                    "request_id": request_id,
                    "ok": True,
                    "translation": asdict(result),
                }):
                    break
                continue
            if not _send_message(connection, {
                "event": "response",
                "request_id": request_id,
                "ok": False,
                "error": {
                    "type": "ValueError",
                    "message": f"Unknown Qwen worker command: {command}",
                    "traceback": "",
                },
            }):
                break
    except Exception as error:
        try:
            _send_message(connection, {
                "event": "error",
                "ok": False,
                "error": _serialize_error(error),
            })
        except Exception:
            pass
    finally:
        try:
            engine.unload()
        except BaseException:
            pass
        try:
            connection.close()
        except BaseException:
            pass
