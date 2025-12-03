#!/usr/bin/env python3
"""
Stage 7 verification helper
Checks diagnostics filtering, batch apply (partial success), Prometheus metrics,
and runTests markers/paths support.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
import websocket

BASE_URL = os.environ.get("LOGIC_BASE_URL", "http://127.0.0.1:4317")
ROOT_TOKEN = os.environ.get("LOGIC_TOKEN", "dev-token-123")
PARSED = urlparse(BASE_URL)

REQUIRED_SCOPES = [
    "file:read",
    "file:write",
    "diag:read",
    "logs:view",
    "test:run",
    "tasks:manage",
]

TEST_FILE = Path("tools/verify_stage7_target.txt")
SECOND_TEST_FILE = Path("tools/verify_stage7_target_2.txt")


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def call_api(method: str, path: str, token: str, json_body: Optional[dict] = None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    resp = requests.request(method, f"{BASE_URL}{path}", json=json_body, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} failed: {resp.status_code} {resp.text}")
    if resp.content:
        return resp.json()
    return {}


def issue_short_token() -> str:
    body = {"ttl": 900, "scopes": REQUIRED_SCOPES, "subject": "verify-stage7"}
    data = call_api("POST", "/v1/token", ROOT_TOKEN, body)
    print("short-lived token issued:", json.dumps({"ttl": data["ttl"], "scopes": data["scopes"]}, ensure_ascii=False))
    return data["token"]


def ensure_file(path: Path, content: str) -> str:
    original = None
    if path.exists():
        original = path.read_text(encoding="utf-8")
    path.write_text(content, encoding="utf-8")
    return original


def revert_file(path: Path, original: Optional[str]):
    if original is None:
        if path.exists():
            path.unlink()
    else:
        path.write_text(original, encoding="utf-8")


def build_patch(old_text: str, new_text: str) -> dict:
    return {"edits": [{"range": [0, len(old_text)], "newText": new_text}]}


def verify_diagnostics(token: str):
    print("\n[1] Diagnostics filtering")
    resp = call_api("GET", "/v1/diagnostics?path=core&path=server.py&severity=error", token)
    assert isinstance(resp, dict)
    print("diagnostics items:", len(resp.get("items", [])))


def verify_batch_apply(token: str):
    print("\n[2] Batch apply with partial success")
    original_a = ensure_file(TEST_FILE, "Stage7 baseline\n")
    original_b = ensure_file(SECOND_TEST_FILE, "Another file\n")
    try:
        hash_a = _hash_file(TEST_FILE)
        hash_b = _hash_file(SECOND_TEST_FILE)
        batch_body = {
            "items": [
                {
                    "path": str(TEST_FILE),
                    "expectedHash": hash_a,
                    "patch": build_patch("Stage7 baseline\n", "Stage7 updated\n"),
                    "message": "update file A",
                },
                {
                    "path": str(SECOND_TEST_FILE),
                    "expectedHash": "sha256:deadbeef",
                    "patch": build_patch("Another file\n", "Should fail\n"),
                    "message": "intentional mismatch",
                },
            ]
        }
        resp = requests.post(
            f"{BASE_URL}/v1/apply/batch",
            json=batch_body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code not in (200, 207):
            raise RuntimeError(f"/v1/apply/batch failed: {resp.status_code} {resp.text}")
        data = resp.json()
        print("batch response:", json.dumps(data, indent=2, ensure_ascii=False))
        success_count = sum(1 for item in data.get("results", []) if item.get("success"))
        fail_count = sum(1 for item in data.get("results", []) if not item.get("success"))
        assert success_count == 1 and fail_count == 1, "expected one success and one failure"
    finally:
        revert_file(TEST_FILE, original_a)
        revert_file(SECOND_TEST_FILE, original_b)


def verify_metrics(token: str):
    print("\n[3] Prometheus metrics")
    resp = requests.get(f"{BASE_URL}/metrics", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"/metrics unavailable: {resp.status_code}")
    body = resp.text
    if "logic_http_requests_total" not in body:
        raise RuntimeError("metrics output missing logic_http_requests_total")
    print("metrics sample:", "\n".join(body.splitlines()[:5]))


def verify_run_tests(token: str):
    print("\n[4] runTests with markers/paths")
    body = {
        "clientId": "stage7",
        "suite": "default",
        "markers": ["not slow"],
        "paths": ["tests"],
    }
    resp = call_api("POST", "/v1/runTests", token, body)
    run_id = resp.get("runId") or resp.get("taskId")
    print("runTests response:", resp)
    ws_url = f"ws://{PARSED.hostname}:{PARSED.port or 80}/ws?clientId=stage7&token={token}"
    ws = websocket.create_connection(ws_url, timeout=60)
    try:
        start = time.time()
        while time.time() - start < 60:
            event_raw = ws.recv()
            if not event_raw:
                continue
            try:
                event = json.loads(event_raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype in {"test.progress", "test.complete", "test.error"}:
                print(event)
            if etype in {"test.complete", "test.error"}:
                break
    finally:
        ws.close()


def main():
    print("=== Stage 7 verification (Python) ===")
    token = issue_short_token()
    verify_diagnostics(token)
    verify_batch_apply(token)
    verify_metrics(token)
    verify_run_tests(token)
    print("\nStage 7 verification complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:
        print("Stage 7 verification failed:", exc, file=sys.stderr)
        sys.exit(1)
