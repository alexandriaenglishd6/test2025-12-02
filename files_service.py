from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Optional, Tuple

from fastapi import HTTPException

from logic_server.models import Edit, GenerateRequest, Patch


def safe_join(workspace_root: Path, rel_path: str) -> Path:
    rel_norm = rel_path.replace("\\", os.sep).replace("/", os.sep)
    candidate = (workspace_root / rel_norm).resolve()
    if not str(candidate).startswith(str(workspace_root)):
        raise HTTPException(status_code=400, detail="INVALID_PATH: outside workspace")
    return candidate


def hash_bytes(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def read_file(path: Path) -> Tuple[str, float, str]:
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="file is not utf-8 encoded") from exc
    mtime_ms = int(path.stat().st_mtime * 1000)
    file_hash = hash_text(content)
    return content, mtime_ms, file_hash


def resolve_prompt_and_context(workspace_root: Path, req: GenerateRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    file_path: Optional[Path] = None
    current_content = ""
    if req.path:
        file_path = safe_join(workspace_root, req.path)
        if file_path.is_dir():
            raise HTTPException(status_code=400, detail="path points to a directory")
        try:
            current_content, _, _ = read_file(file_path)
        except HTTPException as exc:
            if exc.status_code == 404:
                current_content = ""
            else:
                raise
    context = req.context or current_content
    return prompt, context, file_path, current_content


def build_mock_completion(prompt: str, context: str) -> str:
    context_hint = ""
    if context.strip():
        last_line = context.strip().splitlines()[-1]
        context_hint = f"# Context hint: {last_line[:60]}...\n"
    return context_hint + f"# Mock completion based on prompt: {prompt[:80]}...\n"


def build_append_patch(existing_content: str, addition: str) -> Optional[Patch]:
    if addition is None:
        return None
    insert_pos = len(existing_content)
    return Patch(edits=[Edit(range=(insert_pos, insert_pos), newText=addition)])


def apply_patch(content: str, patch: Patch) -> str:
    edits = sorted(patch.edits, key=lambda e: e.range[0], reverse=True)
    text = content
    for edit in edits:
        start, end = edit.range
        if start < 0 or end < start or end > len(text):
            raise HTTPException(status_code=400, detail=f"invalid range {edit.range}")
        text = text[:start] + edit.newText + text[end:]
    return text


def build_diff(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def apply_file_update(
    workspace_root: Path, path: str, expected_hash: Optional[str], patch: Patch
) -> Tuple[Path, str, int, int]:
    file_path = safe_join(workspace_root, path)
    content, _, current_hash = read_file(file_path)
    new_content = apply_patch(content, patch)
    if expected_hash and expected_hash != current_hash:
        diff_text = build_diff(content, new_content, path)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HASH_MISMATCH",
                "message": "content changed",
                "currentHash": current_hash,
                "diff": diff_text,
            },
        )
    file_path.write_text(new_content, encoding="utf-8")
    new_hash = hash_text(new_content)
    mtime_ms = int(file_path.stat().st_mtime * 1000)
    return file_path, new_hash, mtime_ms, len(patch.edits)
