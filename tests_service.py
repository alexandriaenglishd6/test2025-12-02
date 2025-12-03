"""Test execution service contracts and implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import asyncio
import logging
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from fastapi import HTTPException
from logic_server.models import RunTestsRequest, RunTestsResponse
from logic_server.task_manager import TaskRecord


@dataclass
class TestServiceDeps:
    """Dependencies required by test run service."""

    build_test_command: Callable[[RunTestsRequest], str]
    record_test_history: Callable[..., Any]
    broadcast_log: Callable[..., Any]
    audit_logger: Any
    ws_manager: Any
    task_manager: Optional[Any]
    workspace_root: Path
    test_report_dir: Path
    test_command: str


logger = logging.getLogger(__name__)


async def run_tests_pipeline(
    deps: TestServiceDeps,
    req: RunTestsRequest,
    client_id: str,
    subject: str,
    task: Optional[TaskRecord] = None,
    await_completion: bool = False,
    run_id: Optional[str] = None,
) -> RunTestsResponse:
    """Execute tests, stream progress, and record history."""
    if not deps.test_command:
        raise HTTPException(status_code=400, detail="TEST_COMMAND is not configured")

    run_id = run_id or f"test_{int(time.time() * 1000)}"
    command = deps.build_test_command(req)
    log_path = deps.test_report_dir / f"{run_id}.log"
    log_rel = str(log_path.relative_to(deps.workspace_root))
    response = RunTestsResponse(runId=run_id, status="started", log=log_rel)
    audit_subject = subject or "logic-client"
    created_at = time.time()

    await deps.record_test_history(
        run_id=run_id,
        client_id=client_id,
        state="running",
        suite=req.suite,
        markers=req.markers,
        paths=req.paths,
        created_at=created_at,
        finished_at=None,
        summary=None,
        log_path=log_rel,
    )

    if task and deps.task_manager:
        await deps.task_manager.update_progress(
            task.id,
            0.05,
            extra={"runId": run_id, "clientId": client_id},
        )

    await deps.broadcast_log("info", "test run started", runId=run_id, command=command, clientId=client_id)

    async def _stream_process():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        proc_holder: dict[str, Optional[subprocess.Popen]] = {"process": None}

        def _launch():
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(deps.workspace_root),
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
                proc_holder["process"] = process
            except Exception as exc:  # pragma: no cover - platform dependent
                asyncio.run_coroutine_threadsafe(queue.put(("spawn_error", str(exc))), loop)
                return

            with log_path.open("w", encoding="utf-8") as log_file:
                if process.stdout:
                    for raw_line in process.stdout:
                        log_file.write(raw_line)
                        log_file.flush()
                        asyncio.run_coroutine_threadsafe(
                            queue.put(("line", raw_line.rstrip("\r\n"))),
                            loop,
                        )
            exit_code = process.wait()
            proc_holder["process"] = None
            asyncio.run_coroutine_threadsafe(queue.put(("exit", exit_code)), loop)

        threading.Thread(target=_launch, daemon=True).start()

        passed = failed = 0
        step = 0
        start_time = time.time()
        exit_code = -1

        try:
            while True:
                kind, payload = await queue.get()
                if kind == "line":
                    text = payload
                    step += 1
                    normalized = text.lower()
                    if normalized.strip().endswith("passed"):
                        passed += 1
                    elif normalized.strip().endswith("failed") or "failed" in normalized:
                        failed += 1
                    await deps.ws_manager.broadcast(
                        {
                            "type": "test.progress",
                            "runId": run_id,
                            "clientId": client_id,
                            "step": step,
                            "passed": passed,
                            "failed": failed,
                            "line": text,
                        }
                    )
                    if task and deps.task_manager:
                        progress_value = min(0.95, 0.05 + (step / 100.0))
                        await deps.task_manager.update_progress(
                            task.id,
                            progress_value,
                            extra={"runId": run_id, "clientId": client_id, "step": step},
                        )
                elif kind == "spawn_error":
                    exit_code = 1
                    finished_at = time.time()
                    await deps.broadcast_log(
                        "error",
                        f"Test command failed to start: {payload}",
                        runId=run_id,
                        clientId=client_id,
                    )
                    await deps.ws_manager.broadcast(
                        {
                            "type": "test.error",
                            "runId": run_id,
                            "clientId": client_id,
                            "summary": {
                                "passed": passed,
                                "failed": failed or 1,
                                "durationMs": 0,
                                "logPath": log_rel,
                                "exitCode": exit_code,
                                "suite": req.suite,
                                "error": payload,
                            },
                        }
                    )
                    deps.audit_logger.log(
                        "runTests",
                        audit_subject,
                        run_id,
                        "failed",
                        {"suite": req.suite, "command": command, "error": payload},
                    )
                    await deps.record_test_history(
                        run_id=run_id,
                        client_id=client_id,
                        state="failed",
                        suite=req.suite,
                        markers=req.markers,
                        paths=req.paths,
                        created_at=created_at,
                        finished_at=finished_at,
                        summary={
                            "passed": passed,
                            "failed": failed or 1,
                            "durationMs": 0,
                            "logPath": log_rel,
                            "exitCode": exit_code,
                            "suite": req.suite,
                            "error": payload,
                        },
                        log_path=log_rel,
                    )
                    if task:
                        task.error = {"code": "TEST_START_ERROR", "message": payload}
                        raise RuntimeError(payload)
                    return
                elif kind == "exit":
                    exit_code = int(payload)
                    break
        except asyncio.CancelledError:  # pragma: no cover
            process = proc_holder.get("process")
            if process and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
            raise

        duration_ms = int((time.time() - start_time) * 1000)
        summary = {
            "passed": passed,
            "failed": failed,
            "durationMs": duration_ms,
            "logPath": log_rel,
            "exitCode": exit_code,
            "suite": req.suite,
        }
        event_type = "test.complete" if exit_code == 0 else "test.error"
        await deps.ws_manager.broadcast(
            {"type": event_type, "runId": run_id, "clientId": client_id, "summary": summary}
        )
        level = "info" if exit_code == 0 else "warning"
        await deps.broadcast_log(level, "test run finished", runId=run_id, clientId=client_id, summary=summary)
        deps.audit_logger.log(
            "runTests",
            audit_subject,
            run_id,
            "succeeded" if exit_code == 0 else "failed",
            {"suite": req.suite, "command": command, "logPath": log_rel},
        )
        await deps.record_test_history(
            run_id=run_id,
            client_id=client_id,
            state="succeeded" if exit_code == 0 else "failed",
            suite=req.suite,
            markers=req.markers,
            paths=req.paths,
            created_at=created_at,
            finished_at=time.time(),
            summary=summary,
            log_path=log_rel,
        )
        if task:
            task.result = {"runId": run_id, "summary": summary, "log": log_rel}
            if deps.task_manager:
                await deps.task_manager.update_progress(
                    task.id,
                    1.0,
                    extra={"runId": run_id, "clientId": client_id},
                )
            if exit_code != 0:
                task.error = {"code": "TEST_FAILED", "message": "test command exited with errors", "summary": summary}
                raise RuntimeError("test command exited with errors")

    if await_completion:
        await _stream_process()
    else:
        asyncio.create_task(_stream_process())
    return response


__all__ = [
    "TestServiceDeps",
    "run_tests_pipeline",
    "RunTestsRequest",
    "RunTestsResponse",
    "TaskRecord",
]
