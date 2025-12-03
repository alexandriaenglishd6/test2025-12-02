"""Generate service contracts and implementation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from logic_server.models import GenerateRequest, GenerateResponse, Patch
from logic_server.task_manager import TaskRecord


ResolvePromptResult = Tuple[str, Optional[str], Optional[str], Optional[str]]


@dataclass
class GenerateServiceDeps:
    """Dependencies required by generate service."""

    resolve_prompt_and_context: Callable[[GenerateRequest], ResolvePromptResult]
    build_mock_completion: Callable[[str, Optional[str]], str]
    build_append_patch: Callable[[Optional[str], str], Optional[Patch]]
    broadcast_log: Callable[..., Any]
    task_manager: Optional[Any]
    ws_manager: Any
    workspace_root: Path


logger = logging.getLogger("logic_api")


async def process_generate(
    deps: GenerateServiceDeps, req: GenerateRequest, client_id: str, task: Optional[TaskRecord] = None
) -> GenerateResponse:
    """Handle generate request and optionally update task state."""
    prompt, context, file_path, current_content = deps.resolve_prompt_and_context(req)
    logger.info("[generate] prompt resolved path=%s file_path=%s", req.path, file_path)
    request_id = f"req_{int(time.time() * 1000)}"

    if task and deps.task_manager:
        await deps.task_manager.update_progress(
            task.id,
            0.1,
            extra={"requestId": request_id, "clientId": client_id, "path": req.path},
        )

    logger.info("[generate] start requestId=%s path=%s client=%s", request_id, req.path, client_id)
    await deps.broadcast_log("info", "generation started", requestId=request_id, clientId=client_id, path=req.path)
    logger.info(
        "[generate] start requestId=%s path=%s has_task=%s file_path=%s",
        request_id,
        req.path,
        task is not None,
        file_path,
    )

    mock_text = deps.build_mock_completion(prompt, context)
    logger.info("[generate] completion built requestId=%s len=%s", request_id, len(mock_text))
    patch = None
    if file_path is not None:
        patch = deps.build_append_patch(current_content, mock_text)
    logger.info(
        "[generate] patch built requestId=%s has_patch=%s",
        request_id,
        patch is not None,
    )

    # 统一使用字典形式，以规避不同 Patch 实例带来的验证兼容问题
    patch_payload = patch.model_dump() if patch else None

    response = GenerateResponse(requestId=request_id, result=mock_text, patch=patch_payload)

    logger.info(
        "[generate] done requestId=%s has_patch=%s task=%s path=%s",
        request_id,
        patch is not None,
        task is not None,
        req.path,
    )

    if task:
        task.result = response.dict()
        if deps.task_manager:
            await deps.task_manager.update_progress(
                task.id,
                0.6,
                extra={"requestId": request_id, "clientId": client_id, "path": req.path},
            )
            await deps.task_manager.update_progress(
                task.id,
                1.0,
                extra={"requestId": request_id, "clientId": client_id, "path": req.path},
            )
        else:
            task.progress = 1.0

        # 发送 generation.complete 事件（兼容原有行为）
        event_path = req.path
        if not event_path and file_path:
            try:
                event_path = str(Path(file_path).relative_to(deps.workspace_root))
            except ValueError:
                event_path = str(file_path)
        event_payload = {
            "type": "generation.complete",
            "requestId": request_id,
            "path": event_path,
            "result": response.result,
            "patch": patch_payload,
            "clientId": client_id,
        }
        try:
            await deps.ws_manager.send(client_id, event_payload)
        except Exception:
            pass
        await deps.ws_manager.broadcast(event_payload)
        logger.info(
            "[generate] complete broadcast: requestId=%s path=%s clientId=%s has_patch=%s",
            request_id,
            event_path,
            client_id,
            event_payload["patch"] is not None,
        )

        await deps.broadcast_log("info", "generation completed", requestId=request_id, clientId=client_id, path=req.path)
    logger.info("[generate] completed requestId=%s", request_id)

    return response


__all__ = [
    "GenerateServiceDeps",
    "ResolvePromptResult",
    "process_generate",
    "GenerateRequest",
    "GenerateResponse",
    "Patch",
    "TaskRecord",
]
