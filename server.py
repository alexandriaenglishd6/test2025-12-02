"""
FastAPI logic API skeleton for VS Code (logic) + Cursor (UI) collaboration.
Implements Stage 1 (health) and Stage 2 (file read + mock generation) plus
foundational plumbing for later stages (WebSocket, patch apply, diagnostics/test stubs).
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import subprocess
import threading
import json
import logging
import logging.handlers
import os
import time
import uuid
import shlex
import fnmatch
import ipaddress
from functools import partial
from collections import defaultdict, deque
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union, Set

from dotenv import load_dotenv
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from config.settings import settings as app_settings
from logic_server.audit import AuditLogger
from logic_server.auth_utils import (
    AuthContext,
    extract_bearer_token,
    make_auth_context_resolver,
    make_ensure_file_write_scope,
    make_require_scopes,
    make_ws_auth_checker,
    normalize_scopes,
)
from logic_server.bootstrap import build_app, create_lifespan, include_routers
from logic_server.history import HistoryStore
from logic_server.metrics import create_metrics_registry
from logic_server.middleware import (
    add_ip_allowlist_middleware,
    add_metrics_middleware,
    add_rate_limit_middleware,
)
from logic_server.models import (
    TaskRecord,
    TaskState,
    TaskType,
    Edit,
    Patch,
    GenerateOptions,
    GenerateRequest,
    GenerateResponse,
    ApplyRequest,
    ApplyBatchItem,
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchResponse,
    DiagnosticItem,
    DiagnosticsResponse,
    RunTestsRequest,
    CreateTaskRequest,
    TaskStatus,
    TaskStatusResponse,
    TaskListResponse,
    TaskEnqueuedResponse,
    RunTestsResponse,
    SubscriptionRequest,
    SubscriptionResponse,
    IssueTokenRequest,
    IssueTokenResponse,
    TokenInfoResponse,
    RevokeTokenResponse,
    TokenListResponse,
    LogsResponse,
    AuditLogEntry,
    AuditLogResponse,
    HistoryTaskItem,
    HistoryTaskResponse,
    HistoryTestItem,
    HistoryTestResponse,
    MetricsSummaryStats,
    MetricsSummaryResponse,
)
from logic_server.utils import (
    build_duration_stats,
    normalize_error_detail,
    normalize_scope_path_part,
    parse_iso_timestamp,
    resolve_report_dir,
)
from logic_server.routers.diagnostics import DiagnosticsDeps, create_diagnostics_router
from logic_server.routers.token_routes import TokenRouteDeps, create_token_router
from logic_server.routers.file_routes import FileRouteDeps, create_file_router
from logic_server.routers.history import HistoryRouteDeps, create_history_router
from logic_server.routers.tasks import TaskRouteDeps, create_tasks_router
from logic_server.routers.ws import WsRouteDeps, create_ws_router
from logic_server.services import files as file_services
from logic_server.services.generate import GenerateServiceDeps, process_generate
from logic_server.services.tests import TestServiceDeps, run_tests_pipeline
from logic_server.task_manager import TaskManager, TaskManagerMetricsHooks
from logic_server.tokens import TokenStore

load_dotenv()

if os.name == "nt":  # Use Proactor loop so subprocess APIs work on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

APP_VERSION = "1.0.0"
WORKSPACE_ROOT = app_settings.workspace_root
if not WORKSPACE_ROOT.is_absolute():
    WORKSPACE_ROOT = (Path(os.getcwd()) / WORKSPACE_ROOT).resolve()


def _resolve_workspace_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (WORKSPACE_ROOT / path).resolve()



DIAGNOSTICS_PATHS = [
    part.strip() for part in app_settings.diagnostics_paths.split(",") if part.strip()
]
DIAGNOSTICS_MAX_FILES = app_settings.diagnostics_max_files
TEST_COMMAND = (app_settings.test_command or "").strip()
TOKEN_TTL_SECONDS = app_settings.token_ttl_seconds
LOG_FILE = _resolve_workspace_path(app_settings.log_file)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_MAX_BYTES = app_settings.log_max_bytes
LOG_BACKUPS = app_settings.log_backups
TASK_MANAGER_ENABLED = app_settings.task_manager_enabled
MAX_CONCURRENCY = app_settings.max_concurrency
LOGIC_DB_PATH = _resolve_workspace_path(app_settings.logic_db_path)
AUDIT_LOG_PATH = _resolve_workspace_path(app_settings.audit_log_path)
SETTINGS_DEFAULT_SCOPES = app_settings.default_token_scopes or ["file:read", "diag:read"]
AUTH_TOKEN = app_settings.auth_token
RATE_LIMIT_ENABLED = getattr(app_settings, "rate_limit_enabled", False)
RATE_LIMIT_WINDOW_SECONDS = getattr(app_settings, "rate_limit_window_seconds", 60)
RATE_LIMIT_TOKEN_MAX = getattr(app_settings, "rate_limit_token_max", 120)
RATE_LIMIT_IP_MAX = getattr(app_settings, "rate_limit_ip_max", 600)
ALLOWED_IPS_RAW = getattr(app_settings, "allowed_ips", "") or ""
HISTORY_RETENTION_DAYS = getattr(app_settings, "history_retention_days", 90)
HISTORY_RETENTION_SECONDS = HISTORY_RETENTION_DAYS * 24 * 60 * 60
METRICS_MAX_WINDOW_DAYS = HISTORY_RETENTION_DAYS
METRICS_MAX_WINDOW_SECONDS = HISTORY_RETENTION_SECONDS

logger = logging.getLogger("logic_api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

task_manager: Optional[TaskManager] = None

def _ensure_task_manager() -> TaskManager:
    if not task_manager:
        raise HTTPException(status_code=400, detail="Task manager is disabled")
    return task_manager



class SlidingWindowLimiter:
    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self._hits: Dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> Optional[float]:
        if self.max_hits <= 0:
            return None
        now = time.time()
        cutoff = now - self.window_seconds
        async with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_hits:
                retry_after = max(0.0, self.window_seconds - (now - dq[0]))
                return retry_after
            dq.append(now)
        return None


class RateLimiter:
    def __init__(self, enabled: bool, window_seconds: int, token_limit: int, ip_limit: int):
        self.enabled = enabled
        self.window_seconds = window_seconds
        self.token_limiter = SlidingWindowLimiter(token_limit, window_seconds) if token_limit else None
        self.ip_limiter = SlidingWindowLimiter(ip_limit, window_seconds) if ip_limit else None

    async def enforce(self, token: Optional[str], ip: Optional[str], path: str):
        if not self.enabled:
            return
        if self.token_limiter and token:
            retry = await self.token_limiter.check(token)
            if retry is not None:
                audit_logger.log("rate_limit", token, path, "failed", {"scope": "token", "retryAfter": retry})
                raise HTTPException(
                    status_code=429,
                    detail={"code": "RATE_LIMIT", "scope": "token", "retryAfter": int(retry) + 1},
                )
        if self.ip_limiter and ip:
            retry = await self.ip_limiter.check(ip)
            if retry is not None:
                audit_logger.log("rate_limit", ip, path, "failed", {"scope": "ip", "retryAfter": retry})
                raise HTTPException(
                    status_code=429,
                    detail={"code": "RATE_LIMIT", "scope": "ip", "retryAfter": int(retry) + 1},
                )


ALL_SCOPES: Set[str] = {
    "file:read",
    "file:write",
    "diag:read",
    "diag:write",
    "test:run",
    "logs:view",
    "tasks:manage",
}
DEFAULT_TOKEN_SCOPES = [scope for scope in SETTINGS_DEFAULT_SCOPES if scope in ALL_SCOPES] or ["file:read", "diag:read"]

audit_logger = AuditLogger(AUDIT_LOG_PATH)
token_store = TokenStore(LOGIC_DB_PATH)
rate_limiter = RateLimiter(RATE_LIMIT_ENABLED, RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_TOKEN_MAX, RATE_LIMIT_IP_MAX)
history_store = HistoryStore(LOGIC_DB_PATH, HISTORY_RETENTION_SECONDS)
auth_context_resolver = make_auth_context_resolver(
    bootstrap_token=AUTH_TOKEN,
    token_store=token_store,
    all_scopes=ALL_SCOPES,
)
require_scopes = make_require_scopes(auth_context_resolver)
_ensure_file_write_scope = make_ensure_file_write_scope(WORKSPACE_ROOT, audit_logger)
_require_ws_auth = make_ws_auth_checker(auth_context_resolver)
_normalize_scopes = partial(normalize_scopes, default_scopes=DEFAULT_TOKEN_SCOPES, all_scopes=ALL_SCOPES)
ALLOWED_IP_NETWORKS: List[ipaddress._BaseNetwork] = []
if ALLOWED_IPS_RAW:
    for part in [p.strip() for p in ALLOWED_IPS_RAW.split(",") if p.strip()]:
        try:
            ALLOWED_IP_NETWORKS.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("invalid ALLOWED_IPS entry skipped: %s", part)
    logger.info("ip allowlist enabled: %d entries", len(ALLOWED_IP_NETWORKS))
else:
    logger.info("ip allowlist disabled; set ALLOWED_IPS to enable")

# Warn loudly if no bootstrap token is configured; callers must provision tokens beforehand.
if not AUTH_TOKEN:
    logger.warning("AUTH_TOKEN is not set; API requires issued tokens from storage to authenticate")

# metrics config must be set before metrics definitions
METRICS_ENABLED = app_settings.metrics_enabled

metrics_registry = create_metrics_registry(METRICS_ENABLED)
HTTP_REQUEST_COUNTER = metrics_registry.http_requests_total
HTTP_REQUEST_LATENCY = metrics_registry.http_request_duration_seconds
HTTP_ERROR_COUNTER = metrics_registry.http_errors_total
TASKS_RUNNING_GAUGE = metrics_registry.tasks_running_gauge
TASKS_QUEUED_GAUGE = metrics_registry.tasks_queued_gauge
TASKS_FAILED_COUNTER = metrics_registry.tasks_failed_total
TASK_DURATION_HISTOGRAM = metrics_registry.task_duration_histogram
WS_CONNECTIONS_GAUGE = metrics_registry.ws_connections_gauge


def _task_to_status(record: TaskRecord) -> TaskStatus:
    return TaskStatus(
        id=record.id,
        type=record.type,
        priority=record.priority,
        clientId=record.client_id,
        state=record.state,
        progress=record.progress,
        createdAt=record.created_at,
        startedAt=record.started_at,
        finishedAt=record.finished_at,
        result=record.result,
        error=record.error,
    )

def _observe_task_duration(task: TaskRecord):
    if not TASK_DURATION_HISTOGRAM:
        return
    start = task.started_at or task.created_at
    end = task.finished_at or time.time()
    if start is None or end is None:
        return
    duration = max(0.0, end - start)
    task_type = task.type.value if isinstance(task.type, TaskType) else str(task.type)
    task_state = task.state.value if isinstance(task.state, TaskState) else str(task.state)
    TASK_DURATION_HISTOGRAM.labels(task_type, task_state).observe(duration)


def _build_task_manager_metrics_hooks() -> Optional[TaskManagerMetricsHooks]:
    if not any([TASKS_QUEUED_GAUGE, TASKS_RUNNING_GAUGE, TASKS_FAILED_COUNTER]):
        return None
    return TaskManagerMetricsHooks(
        set_queue_size=(lambda value: TASKS_QUEUED_GAUGE.set(value)) if TASKS_QUEUED_GAUGE else None,
        inc_running=(lambda task_type: TASKS_RUNNING_GAUGE.labels(task_type).inc()) if TASKS_RUNNING_GAUGE else None,
        dec_running=(lambda task_type: TASKS_RUNNING_GAUGE.labels(task_type).dec()) if TASKS_RUNNING_GAUGE else None,
        inc_failed=(lambda task_type: TASKS_FAILED_COUNTER.labels(task_type).inc()) if TASKS_FAILED_COUNTER else None,
    )

async def _on_startup():
    if task_manager:
        _register_task_handlers()
        await task_manager.start()

async def _on_shutdown():
    if task_manager:
        await task_manager.stop()

lifespan = create_lifespan(on_startup=_on_startup, on_shutdown=_on_shutdown)
app = build_app(APP_VERSION, lifespan)

# Middlewares
add_ip_allowlist_middleware(app, ALLOWED_IP_NETWORKS, logger)
add_rate_limit_middleware(app, rate_limiter if RATE_LIMIT_ENABLED else None, logger, extract_bearer_token)
add_metrics_middleware(app, HTTP_REQUEST_COUNTER if METRICS_ENABLED else None, HTTP_REQUEST_LATENCY if METRICS_ENABLED else None, HTTP_ERROR_COUNTER if METRICS_ENABLED else None)

@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = normalize_error_detail(exc.detail, fallback_code=f"HTTP_{exc.status_code}")
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    if HTTP_ERROR_COUNTER:
        HTTP_ERROR_COUNTER.labels(route_path, request.method, str(exc.status_code)).inc()
    logger.warning(
        "http error handled %s %s %s -> %s",
        request.method,
        route_path,
        exc.status_code,
        detail.get("code"),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})

@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    detail = normalize_error_detail("validation error", fallback_code="VALIDATION_ERROR")
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    if HTTP_ERROR_COUNTER:
        HTTP_ERROR_COUNTER.labels(route_path, request.method, "422").inc()
    logger.warning("validation error %s %s -> %s", request.method, route_path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": detail})

def _parse_iso_timestamp(value: Optional[str], *, field: str) -> Optional[float]:
    return parse_iso_timestamp(value, field=field)


_safe_join = partial(file_services.safe_join, WORKSPACE_ROOT)
_read_file = file_services.read_file


TEST_REPORT_DIR = resolve_report_dir(WORKSPACE_ROOT, app_settings.test_report_dir)


def _tail_log_lines(limit: int) -> List[str]:
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()
    return lines[-limit:]


_resolve_prompt_and_context = partial(file_services.resolve_prompt_and_context, WORKSPACE_ROOT)
_build_mock_completion = file_services.build_mock_completion
_build_append_patch = file_services.build_append_patch


def _iter_diagnostic_files(limit: int) -> List[Path]:
    files: List[Path] = []
    if not DIAGNOSTICS_PATHS:
        return files
    for entry in DIAGNOSTICS_PATHS:
        try:
            target = _safe_join(entry)
        except HTTPException:
            continue
        if not target.exists():
            continue
        if target.is_file():
            if target.suffix == ".py":
                files.append(target)
        else:
            for path in target.rglob("*.py"):
                files.append(path)
                if len(files) >= limit:
                    break
        if len(files) >= limit:
            break
    return files[:limit]


def _line_offset(text: str, lineno: Optional[int], column: Optional[int]) -> Tuple[int, int]:
    if not text:
        return 0, 0
    lines = text.splitlines(keepends=True)
    if lineno is None or lineno < 1:
        lineno = 1
    line_index = min(max(lineno - 1, 0), len(lines) - 1)
    line_start = sum(len(lines[i]) for i in range(line_index))
    col = max((column or 1) - 1, 0)
    start = min(line_start + col, len(text))
    line_len = len(lines[line_index]) if lines else 0
    end = min(line_start + line_len, len(text))
    return start, end


def _matches_path_filters(rel_path: str, filters: List[str]) -> bool:
    if not filters:
        return True
    normalized_rel = rel_path.replace("\\", "/").lower()
    for raw in filters:
        pattern = raw.replace("\\", "/").lower()
        if any(ch in pattern for ch in "*?[]"):
            if fnmatch.fnmatch(normalized_rel, pattern):
                return True
        else:
            if normalized_rel.startswith(pattern):
                return True
    return False


def _collect_diagnostics(filter_paths: Optional[List[str]] = None) -> List["DiagnosticItem"]:
    items: List[DiagnosticItem] = []
    paths = _iter_diagnostic_files(DIAGNOSTICS_MAX_FILES)
    normalized_filters: Optional[List[str]] = None
    if filter_paths:
        normalized_filters = [path.replace("\\", "/").lower() for path in filter_paths if path]
    for file_path in paths:
        rel_path = str(file_path.relative_to(WORKSPACE_ROOT))
        if normalized_filters and not _matches_path_filters(rel_path, normalized_filters):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            items.append(
                DiagnosticItem(
                    file=rel_path,
                    range=(0, 0),
                    severity="warning",
                    message="File is not UTF-8 encoded; diagnostics skipped",
                    code="ENCODING",
                ).model_dump()
            )
            continue
        try:
            compile(content, rel_path, "exec")
        except SyntaxError as exc:
            start, end = _line_offset(content, exc.lineno, exc.offset)
            items.append(
                DiagnosticItem(
                    file=rel_path,
                    range=(start, end),
                    severity="error",
                    message=f"SyntaxError: {exc.msg}",
                    code="PY_SYNTAX_ERROR",
                ).model_dump()
            )
            continue
        # Simple style check: trailing whitespace
        for idx, line in enumerate(content.splitlines()):
            if line.rstrip() != line:
                start, end = _line_offset(content, idx + 1, len(line) + 1)
                items.append(
                    DiagnosticItem(
                        file=rel_path,
                        range=(start, end),
                        severity="info",
                        message="Trailing whitespace",
                        code="STYLE_TRIM",
                    ).model_dump()
                )
                break
    return items


# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------


class WSManager:
    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}
        self._subscriptions: Dict[str, List[str]] = {}

    async def connect(self, client_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[client_id] = ws
        logger.info(f"[WSManager] 客户端 {client_id} 已连接 WebSocket")
        print(f"[WSManager] 客户端 {client_id} 已连接 WebSocket")
        # 默认订阅：接收自身 clientId 的事件 + 基础 log
        self.update_subscription(client_id, [f"clientId:{client_id}", "type:log"])
        logger.info(f"[WSManager] 客户端 {client_id} 默认订阅已设置: clientId:{client_id}, type:log")

    def disconnect(self, client_id: str):
        if client_id in self._connections:
            logger.info(f"[WSManager] 客户端 {client_id} 断开 WebSocket 连接")
            print(f"[WSManager] 客户端 {client_id} 断开 WebSocket 连接")
        self._connections.pop(client_id, None)
        self._subscriptions.pop(client_id, None)

    async def send(self, client_id: str, payload: Dict):
        ws = self._connections.get(client_id)
        if not ws:
            logger.warning(f"[WSManager] 客户端 {client_id} 未连接，无法发送事件: {payload.get('type', 'unknown')}")
            logger.debug(f"[WSManager] 当前连接的客户端: {list(self._connections.keys())}")
            return
        logger.debug(f"[WSManager] 向客户端 {client_id} 发送事件: {payload.get('type', 'unknown')}")
        await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def broadcast(self, payload: Dict):
        event_type = payload.get("type", "unknown")
        connected_clients = list(self._connections.keys())
        logger.info(f"[WSManager] broadcast event: type={event_type} to clients={connected_clients}, payload_keys={list(payload.keys())}")
        print(f"[WSManager] broadcast event: type={event_type} to clients={connected_clients}, payload_keys={list(payload.keys())}")
        if not connected_clients:
            logger.warning(f"[WSManager] 没有客户端连接，无法广播事件: {event_type}")
            print(f"[WSManager] 没有客户端连接，无法广播事件: {event_type}")
            return
        for client_id in connected_clients:
            topics = self._subscriptions.get(client_id, [])
            should_deliver = self._should_deliver(client_id, payload)
            logger.info(f"[WSManager] check client {client_id}: should_deliver={should_deliver}, topics={topics}, payload_keys={list(payload.keys())}")
            print(f"[WSManager] check client {client_id}: should_deliver={should_deliver}, topics={topics}, payload_keys={list(payload.keys())}")
            if not should_deliver:
                logger.debug(f"[WSManager] 跳过客户端 {client_id}（订阅过滤）")
                continue
            try:
                logger.info(f"[WSManager] delivering {event_type} to {client_id}")
                print(f"[WSManager] delivering {event_type} to {client_id}")
                await self.send(client_id, payload)
            except Exception as e:
                logger.warning(f"[WSManager] 向客户端 {client_id} 广播事件失败: {e}")
                print(f"[WSManager] 向客户端 {client_id} 广播事件失败: {e}")

    def update_subscription(self, client_id: str, topics: List[str]):
        if topics:
            # 去除空字符串并保持顺序
            filtered = []
            seen = set()
            for topic in topics:
                topic = topic.strip()
                if not topic or topic in seen:
                    continue
                filtered.append(topic)
                seen.add(topic)
            if filtered:
                # 合并订阅：保留现有订阅，添加新订阅
                existing = self._subscriptions.get(client_id, [])
                merged = list(existing)
                for topic in filtered:
                    if topic not in merged:
                        merged.append(topic)
                self._subscriptions[client_id] = merged
                logger.info(f"[WSManager] 更新订阅: clientId={client_id}, 现有={existing}, 新增={filtered}, 合并后={merged}")
                print(f"[WSManager] 更新订阅: clientId={client_id}, 现有={existing}, 新增={filtered}, 合并后={merged}")
            else:
                self._subscriptions.pop(client_id, None)
        else:
            self._subscriptions.pop(client_id, None)

    def _should_deliver(self, client_id: str, payload: Dict) -> bool:
        topics = self._subscriptions.get(client_id)
        if not topics:
            logger.info(f"[WSManager] 客户端 {client_id} 没有订阅，默认允许所有事件")
            print(f"[WSManager] 客户端 {client_id} 没有订阅，默认允许所有事件")
            return True
        event_type = payload.get("type", "")
        logger.info(f"[WSManager] 检查是否应该向客户端 {client_id} 发送事件 {event_type}，订阅主题: {topics}")
        print(f"[WSManager] 检查是否应该向客户端 {client_id} 发送事件 {event_type}，订阅主题: {topics}")
        for topic in topics:
            if topic == "all":
                logger.info(f"[WSManager] 客户端 {client_id} 订阅了 'all'，允许发送")
                print(f"[WSManager] 客户端 {client_id} 订阅了 'all'，允许发送")
                return True
            if ":" not in topic:
                logger.debug(f"[WSManager] 跳过无效订阅主题（无冒号）: {topic}")
                continue
            key, pattern = topic.split(":", 1)
            value = payload.get(key)
            if value is None:
                logger.debug(f"[WSManager] 事件中没有字段 '{key}'，跳过主题: {topic}")
                continue
            if fnmatch.fnmatch(str(value), pattern):
                logger.info(f"[WSManager] 客户端 {client_id} 的订阅主题 '{topic}' 匹配事件: {key}={value}, pattern={pattern}")
                print(f"[WSManager] 客户端 {client_id} 的订阅主题 '{topic}' 匹配事件: {key}={value}, pattern={pattern}")
                return True
            else:
                logger.debug(f"[WSManager] 订阅主题 '{topic}' 不匹配: {key}={value}, pattern={pattern}")
        logger.info(f"[WSManager] 客户端 {client_id} 的订阅主题都不匹配事件 {event_type}")
        print(f"[WSManager] 客户端 {client_id} 的订阅主题都不匹配事件 {event_type}")
        return False


ws_manager = WSManager()


WS_LOG_LEVELS = {"debug", "info", "warning", "error"}
_LEVEL_TO_LOGGING = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


async def _broadcast_log(level: str, message: str, **extra):
    if level not in WS_LOG_LEVELS:
        level = "info"
    payload = {"type": "log", "level": level, "message": message}
    payload.update(extra)
    logger.log(_LEVEL_TO_LOGGING.get(level, logging.INFO), "%s | extra=%s", message, extra or {})
    await ws_manager.broadcast(payload)


async def _broadcast_task_event(event_type: str, task: TaskRecord, extra: Optional[Dict[str, Any]] = None):
    payload = {
        "type": event_type,
        "taskId": task.id,
        "clientId": task.client_id,
        "task": task.to_dict(),
    }
    if extra:
        payload.update(extra)
    await ws_manager.broadcast(payload)
    if history_store and event_type in {"task.enqueued", "task.started", "task.completed", "task.failed", "task.canceled"}:
        await asyncio.to_thread(history_store.upsert_task, task)


task_manager_metrics = _build_task_manager_metrics_hooks()
if TASK_MANAGER_ENABLED:
    task_manager = TaskManager(
        MAX_CONCURRENCY,
        event_sink=_broadcast_task_event,
        duration_observer=_observe_task_duration,
        metrics=task_manager_metrics,
    )
else:
    task_manager = None


async def _record_test_history(
    run_id: str,
    client_id: Optional[str],
    state: str,
    suite: Optional[str],
    markers: Optional[List[str]],
    paths: Optional[List[str]],
    created_at: float,
    finished_at: Optional[float],
    summary: Optional[Dict[str, Any]],
    log_path: Optional[str],
):
    if not history_store:
        return
    await asyncio.to_thread(
        history_store.upsert_test,
        run_id=run_id,
        client_id=client_id,
        state=state,
        suite=suite,
        markers=markers,
        paths=paths,
        created_at=created_at,
        finished_at=finished_at,
        summary=summary,
        log_path=log_path,
    )


@app.get("/v1/health")
def health():
    return {
        "status": "ok",
        "pid": os.getpid(),
        "version": APP_VERSION,
        "codex_connected": False,
        "ws": "/ws",
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workspaceRoot": str(WORKSPACE_ROOT),
        "metricsEnabled": METRICS_ENABLED,
    }


@app.get("/metrics")
def metrics_endpoint():
    if not METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="metrics disabled")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/generate", response_model=Union[GenerateResponse, TaskEnqueuedResponse])
async def generate(req: GenerateRequest, auth: AuthContext = Depends(require_scopes(["file:read"]))):
    client_id = req.clientId or "default"
    logger.info(f"[server] /v1/generate 被调用: path={req.path}, client_id={client_id}, task_manager={task_manager is not None}")
    print(f"[server] /v1/generate 被调用: path={req.path}, client_id={client_id}, task_manager={task_manager is not None}")
    if task_manager:
        logger.info(f"[server] 创建生成任务: client_id={client_id}")
        record = await task_manager.enqueue(
            TaskType.GENERATE,
            {"request": req.dict(), "clientId": client_id},
            client_id=client_id,
        )
        logger.info(f"[server] 任务已创建: taskId={record.id}, state={record.state}")
        return TaskEnqueuedResponse(taskId=record.id, state=record.state, type=record.type, clientId=client_id)
    logger.info(f"[server] task_manager 未启用，直接处理生成请求")
    return await process_generate(generate_service, req, client_id, task=None)


async def _handle_generate_task(task: TaskRecord):
    payload = task.payload or {}
    req_dict = payload.get("request", {})
    req_data = GenerateRequest(**req_dict)
    client_id = payload.get("clientId") or req_data.clientId or "default"
    logger.info(f"[server] _handle_generate_task: path={req_data.path}, client_id={client_id}, req_dict_keys={list(req_dict.keys())}")
    print(f"[server] _handle_generate_task: path={req_data.path}, client_id={client_id}, req_dict_keys={list(req_dict.keys())}")
    await process_generate(generate_service, req_data, client_id, task)


def _build_test_command(req: RunTestsRequest) -> str:
    command = TEST_COMMAND
    if req.suite and req.suite != "default":
        command += f" -k {shlex.quote(req.suite)}"
    if req.markers:
        for marker in req.markers:
            if marker:
                command += f" -m {shlex.quote(marker)}"
    if req.paths:
        paths = " ".join(shlex.quote(path) for path in req.paths if path)
        if paths:
            command += f" {paths}"
    return command.strip()


# Service singletons (generate / tests)
generate_service = GenerateServiceDeps(
    resolve_prompt_and_context=_resolve_prompt_and_context,
    build_mock_completion=_build_mock_completion,
    build_append_patch=_build_append_patch,
    broadcast_log=_broadcast_log,
    task_manager=task_manager,
    ws_manager=ws_manager,
    workspace_root=WORKSPACE_ROOT,
)

test_service = TestServiceDeps(
    build_test_command=_build_test_command,
    record_test_history=_record_test_history,
    broadcast_log=_broadcast_log,
    audit_logger=audit_logger,
    ws_manager=ws_manager,
    task_manager=task_manager,
    workspace_root=WORKSPACE_ROOT,
    test_report_dir=TEST_REPORT_DIR,
    test_command=TEST_COMMAND,
)


# TODO(slim-down): 将 _run_tests_pipeline 拆到 logic_server/services/tests.py，由入口装配依赖并调用
_run_tests_pipeline = partial(run_tests_pipeline, test_service)


async def _handle_run_tests_task(task: TaskRecord):
    payload = task.payload or {}
    req_data = RunTestsRequest(**payload.get("request", {}))
    client_id = payload.get("clientId") or req_data.clientId or "default"
    subject = payload.get("subject") or "task-runner"
    await _run_tests_pipeline(req_data, client_id, subject, task=task, await_completion=True)


def _register_task_handlers():
    if not task_manager:
        return
    task_manager.register_handler(TaskType.GENERATE, _handle_generate_task)
    task_manager.register_handler(TaskType.RUN_TESTS, _handle_run_tests_task)


_apply_file_update = partial(file_services.apply_file_update, WORKSPACE_ROOT)


# Routers
diagnostics_router = create_diagnostics_router(
    DiagnosticsDeps(
        require_scopes=require_scopes,
        audit_logger=audit_logger,
        collect_diagnostics=_collect_diagnostics,
        tail_log_lines=_tail_log_lines,
        parse_iso_timestamp=lambda value: _parse_iso_timestamp(value, field="timestamp") if value else None,
        log_file=str(LOG_FILE),
    )
)

token_router = create_token_router(
    TokenRouteDeps(
        require_scopes=require_scopes,
        token_store=token_store,
        audit_logger=audit_logger,
        normalize_scopes=_normalize_scopes,
        token_ttl_seconds=TOKEN_TTL_SECONDS,
    )
)

file_router = create_file_router(
    FileRouteDeps(
        require_scopes=require_scopes,
        ensure_file_write_scope=_ensure_file_write_scope,
        apply_file_update=_apply_file_update,
        safe_join=_safe_join,
        workspace_root=WORKSPACE_ROOT,
        ws_manager=ws_manager,
        broadcast_log=_broadcast_log,
        audit_logger=audit_logger,
        read_file=_read_file,
    )
)

history_router = create_history_router(
    HistoryRouteDeps(
        history_store=history_store,
        audit_logger=audit_logger,
        require_scopes=require_scopes,
        metrics_max_window_seconds=METRICS_MAX_WINDOW_SECONDS,
        metrics_max_window_days=METRICS_MAX_WINDOW_DAYS,
    )
)

tasks_router = create_tasks_router(
    TaskRouteDeps(
        require_scopes=require_scopes,
        ensure_task_manager=_ensure_task_manager,
        task_manager=task_manager,
        ws_manager=ws_manager,
        broadcast_log=_broadcast_log,
        run_tests_pipeline=_run_tests_pipeline,
        resolve_prompt_and_context=_resolve_prompt_and_context,
        build_mock_completion=_build_mock_completion,
        build_append_patch=_build_append_patch,
        task_to_status=_task_to_status,
        TaskType=TaskType,
        TaskStatusResponse=TaskStatusResponse,
        TaskListResponse=TaskListResponse,
        TaskEnqueuedResponse=TaskEnqueuedResponse,
        CreateTaskRequest=CreateTaskRequest,
        RunTestsRequest=RunTestsRequest,
        GenerateRequest=GenerateRequest,
        SubscriptionRequest=SubscriptionRequest,
        SubscriptionResponse=SubscriptionResponse,
        AuthContext=AuthContext,
    )
)
ws_router = create_ws_router(
    WsRouteDeps(
        require_ws_auth=_require_ws_auth,
        ws_manager=ws_manager,
        connections_gauge=WS_CONNECTIONS_GAUGE,
        logger=logger,
    )
)
include_routers(app, [diagnostics_router, token_router, file_router, history_router, tasks_router, ws_router])
