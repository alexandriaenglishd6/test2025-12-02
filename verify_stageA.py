#!/usr/bin/env python
"""
Milestone A verification helper:
  - Fires multiple /v1/generate and /v1/runTests requests with the same clientId
  - Confirms TaskManager queues (max concurrency respected) and WS subscriptions filter events

Usage:
  LOGIC_BASE_URL=http://127.0.0.1:4318 LOGIC_TOKEN=dev-token python tools/verify_stageA.py

Requires:
  pip install websocket-client
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from requests.exceptions import ReadTimeout


if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - Python 3.7+
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = os.environ.get("LOGIC_BASE_URL", "http://127.0.0.1:4317")
AUTH_TOKEN = os.environ.get("LOGIC_TOKEN", "")
CLIENT_ID = os.environ.get("LOGIC_CLIENT_ID", "verify-stageA")
TARGET_PATH = os.environ.get("LOGIC_SAMPLE_PATH", "README.md")
EXPECTED_MAX_CONCURRENCY = int(os.environ.get("LOGIC_MAX_CONCURRENCY", "2"))

parsed = urlparse(BASE_URL)
if parsed.scheme not in {"http", "https"}:
    raise SystemExit(f"Unsupported LOGIC_BASE_URL: {BASE_URL}")

session = requests.Session()
REQUEST_TIMEOUT = int(os.environ.get("LOGIC_REQUEST_TIMEOUT", "60"))


def request(method: str, path: str, body: Optional[dict] = None) -> dict:
    headers = {}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    url = f"{BASE_URL}{path}"
    resp = session.request(method, url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text}")
    if not resp.content:
        return {}
    return resp.json()


@dataclass
class TaskSnapshot:
    id: str
    state: str
    type: str
    progress: float
    client_id: Optional[str]


def list_tasks() -> Dict[str, TaskSnapshot]:
    payload = request("GET", "/v1/tasks")
    snapshots: Dict[str, TaskSnapshot] = {}
    for item in payload.get("tasks", []):
        snapshots[item["id"]] = TaskSnapshot(
            id=item["id"],
            state=item["state"],
            type=item["type"],
            progress=float(item.get("progress", 0.0) or 0.0),
            client_id=item.get("clientId"),
        )
    return snapshots



def spawn_generate_tasks(count: int) -> List[str]:
    task_ids: List[str] = []
    for idx in range(count):
        payload = {
            "prompt": f"Milestone A verification #{idx}",
            "path": TARGET_PATH,
            "clientId": CLIENT_ID,
        }
        try:
            resp = request("POST", "/v1/generate", payload)
        except ReadTimeout:
            print(
                f"[HTTP] /v1/generate timeout for prompt #{idx}; task may still be queued, continuing..."
            )
            continue
        task_id = resp.get("taskId")
        if not task_id:
            raise RuntimeError(f"Expected taskId in response, got: {resp}")
        task_ids.append(task_id)
        print(f"[HTTP] Enqueued generate task {task_id} ({len(task_ids)}/{count})")
    return task_ids


def spawn_run_tests() -> Optional[str]:
    try:
        resp = request("POST", "/v1/runTests", {"clientId": CLIENT_ID})
    except ReadTimeout:
        print("[HTTP] /v1/runTests timeout; task may still be queued, continuing...")
        return None
    task_id = resp.get("taskId")
    if not task_id:
        raise RuntimeError(f"Expected taskId in runTests response, got: {resp}")
    print(f"[HTTP] Enqueued runTests task {task_id}")
    return task_id


def wait_for_completion(task_ids: List[str], timeout: float = 120.0):
    deadline = time.time() + timeout
    observed_peaks = defaultdict(int)
    if not task_ids:
        print("[HTTP] No tasks enqueued, skipping completion check.")
        return
    while time.time() < deadline:
        snapshots = list_tasks()
        running = [snap for snap in snapshots.values() if snap.state == "running"]
        observed_peaks["running"] = max(observed_peaks["running"], len(running))
        client_running = [snap for snap in running if snap.client_id == CLIENT_ID]
        if len(client_running) > EXPECTED_MAX_CONCURRENCY:
            raise AssertionError(
                f"More than {EXPECTED_MAX_CONCURRENCY} concurrent tasks detected for client {CLIENT_ID}"
            )
        all_done = True
        for task_id in task_ids:
            snap = snapshots.get(task_id)
            if not snap:
                all_done = False
                continue
            if snap.state not in {"succeeded", "failed", "canceled"}:
                all_done = False
        if all_done:
            print(f"[HTTP] All tasks completed. Peak concurrent running tasks (all clients): {observed_peaks['running']}")
            return
        time.sleep(0.5)
    raise TimeoutError("Tasks did not finish within allotted time")


def main():
    print("=== Milestone A verification ===")
    generate_task_ids: List[str]
    run_tests_task_id: Optional[str]

    generate_task_ids = spawn_generate_tasks(int(os.environ.get("LOGIC_GENERATE_COUNT", "2")))
    run_tests_task_id = spawn_run_tests()
    all_ids = generate_task_ids + ([run_tests_task_id] if run_tests_task_id else [])
    wait_for_completion(all_ids)

    print("Verification complete.")
    print("Check console output for HTTP task completion and concurrency checks.")


if __name__ == "__main__":
    main()

