#!/usr/bin/env python3
"""
Stage 6 verification helper
Requires: pip install requests websocket-client
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import urlparse

import requests
import websocket

BASE_URL = os.environ.get("LOGIC_BASE_URL", "http://127.0.0.1:4317")
ROOT_TOKEN = os.environ.get("LOGIC_TOKEN", "dev-token-123")
REQUEST_TIMEOUT = int(os.environ.get("LOGIC_REQUEST_TIMEOUT", "30"))
PARSED = urlparse(BASE_URL)


def call(method: str, path: str, token: str, body=None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    resp = requests.request(method, f"{BASE_URL}{path}", json=body, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def main():
    print("=== Stage 6 verification (Python) ===")
    requested_scopes = ["file:read", "diag:read", "logs:view", "test:run"]
    issued = call("POST", "/v1/token", ROOT_TOKEN, {"ttl": 900, "scopes": requested_scopes})
    short_token = issued["token"]
    print("1) Token response:\n", json.dumps(issued, indent=2, ensure_ascii=False))

    diag = call("GET", "/v1/diagnostics?path=core", short_token)
    print("\n2) Diagnostics:\n", json.dumps(diag, indent=2, ensure_ascii=False))

    logs = call("GET", "/v1/logs?limit=20", short_token)
    print("\n3) Logs:\n", json.dumps(logs, indent=2, ensure_ascii=False))

    run_payload = {"clientId": "stage6"}
    run = call("POST", "/v1/runTests", short_token, run_payload)
    run_id = run.get("runId")
    task_id = run.get("taskId")
    print("\n4) runTests:\n", json.dumps(run, indent=2, ensure_ascii=False))

    audit = call("GET", "/v1/audit/latest?limit=5", short_token)
    print("\n5) Audit tail:\n", json.dumps(audit, indent=2, ensure_ascii=False))

    ws_url = f"ws://{PARSED.hostname}:{PARSED.port or 80}/ws?clientId=stage6&token={short_token}"
    ws = websocket.create_connection(ws_url, timeout=60)
    start = time.time()
    observed_run_id = run_id
    try:
        while time.time() - start < 60:
            raw = ws.recv()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype in ("log", "test.progress", "test.complete", "test.error"):
                print(event)
            if event.get("runId") and not observed_run_id:
                observed_run_id = event["runId"]
            if etype in ("test.complete", "test.error"):
                if not observed_run_id or event.get("runId") == observed_run_id:
                    break
        else:
            raise RuntimeError(f"Timed out waiting for test completion (taskId={task_id})")
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Stage 6 verification failed:", exc, file=sys.stderr)
        sys.exit(1)
