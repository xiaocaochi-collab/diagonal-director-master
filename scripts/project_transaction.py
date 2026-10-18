#!/usr/bin/env python3
"""Recoverable multi-file commits for DIAGONAL director projects.

The creative facts remain in the Markdown project files. This helper stores only
technical transaction state, hashes, staging paths, and backup metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safe_names import sanitize_filename, unique_path
from asset_library import MARKER, project_lock, route_target, record_routes, sync_transaction

STATE_NAME = ".storyboard_project_state.json"
TRANSACTION_DIR = ".storyboard_transactions"
BACKUP_DIR = ".storyboard_backups"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    if os.name == 'nt':
        return  # Windows does not expose directory fsync via os.open.
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _load_state(project_dir: Path) -> dict[str, Any]:
    path = project_dir / STATE_NAME
    if not path.exists():
        return {"schema_version": 1, "active_transaction": None, "files": {}, "last_transaction": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise ValueError("unsupported project state schema")
    return state


def _resolve_project_dir(value: str | Path) -> Path:
    project_dir = Path(value).expanduser().resolve()
    if not project_dir.is_dir():
        raise ValueError("project directory must already exist")
    return project_dir


def _prepare_entries(project_dir: Path, writes: list[dict[str, Any]], txid: str) -> list[dict[str, Any]]:
    project_dir = Path(project_dir).resolve()
    if not writes:
        raise ValueError("transaction requires at least one write")
    stage_dir = project_dir / TRANSACTION_DIR / txid
    backup_dir = project_dir / BACKUP_DIR / txid
    stage_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    used_targets: set[str] = set()

    for index, item in enumerate(writes, start=1):
        source = Path(item["source"]).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"source is not a file: {source}")
        requested = str(item["target"])
        safe_name = route_target(project_dir, requested, item.get('scene'))
        canonical_name = sanitize_filename(requested)
        replace_existing = bool(item.get("replace_existing", False))
        target = project_dir / safe_name
        routes_path = project_dir / '.diagonal_paths.json'
        if routes_path.exists():
            routes = json.loads(routes_path.read_text(encoding='utf-8'))
            if (requested in routes or canonical_name in routes) and not target.exists():
                raise RuntimeError('previously registered file is missing; review the deletion before writing: ' + requested)
        if target.is_symlink():
            raise ValueError('symlink targets are not supported')
        for parent in target.parents:
            if parent == project_dir:
                break
            if parent.is_symlink():
                raise ValueError('symlink target directories are not supported')
        target.resolve().relative_to(project_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = item.get('expected_sha256')
        if replace_existing and target.exists() and (project_dir / MARKER).exists() and not expected:
            raise ValueError('managed overwrite requires expected_sha256 from the file you read')
        if expected and (not target.is_file() or _sha256(target) != expected):
            raise RuntimeError('file changed since it was read: ' + safe_name)
        if target.exists() and not replace_existing:
            singleton_names = {
                'storyboard_完整版.md', 'storyboard_global_analysis.md',
                'storyboard_scene_cards.md', 'storyboard_continuity.md',
                'storyboard_locations.md', 'storyboard_stats.md',
                'storyboard_emotion_curve.png',
            }
            if (project_dir / MARKER).exists() and (
                canonical_name.startswith('storyboard_第') or canonical_name in singleton_names
                or re.match(r'第.+场提示词\.md$', canonical_name)
            ):
                raise RuntimeError('formal file already exists; review it and use expected_sha256 to update')
            target = unique_path(target.parent, target.name)
        relative = str(target.relative_to(project_dir)).replace(os.sep, '/')
        if relative in used_targets:
            raise ValueError(f"duplicate transaction target: {relative}")
        used_targets.add(relative)

        staged = stage_dir / f"{index:04d}.staged"
        shutil.copy2(source, staged)
        desired_hash = _sha256(staged)
        original_exists = target.exists()
        backup = backup_dir / f"{index:04d}.backup"
        original_hash = None
        if original_exists:
            shutil.copy2(target, backup)
            original_hash = _sha256(backup)
            if expected and original_hash != expected:
                raise RuntimeError('file changed while preparing transaction: ' + relative)

        entries.append({
            "target": relative,
            "requested_target": requested,
            "staged": str(staged.relative_to(project_dir)),
            "backup": str(backup.relative_to(project_dir)) if original_exists else None,
            "original_exists": original_exists,
            "original_sha256": original_hash,
            "desired_sha256": desired_hash,
            "committed": False,
            "asset_metadata": {**item.get('asset_metadata', {}), 'scene': item.get('scene', '')},
        })
    _fsync_directory(stage_dir)
    _fsync_directory(backup_dir)
    return entries


def _finish(project_dir: Path, state: dict[str, Any], status: str) -> dict[str, Any]:
    transaction = state["active_transaction"]
    if status == "committed":
        for entry in transaction["entries"]:
            target = project_dir / entry["target"]
            if not target.is_file() or _sha256(target) != entry["desired_sha256"]:
                raise RuntimeError(f"committed file verification failed: {entry['target']}")
    for entry in transaction["entries"]:
        target = project_dir / entry["target"]
        if target.exists():
            state["files"][entry["target"]] = {"sha256": _sha256(target), "updated_at": _now()}
        else:
            state["files"].pop(entry["target"], None)
    transaction["status"] = status
    transaction["finished_at"] = _now()
    state["last_transaction"] = transaction
    state["active_transaction"] = None
    if status == 'committed' and (project_dir / MARKER).exists():
        state['asset_sync'] = {'status': 'pending', 'transaction': transaction}
    _atomic_json(project_dir / STATE_NAME, state)
    state = _sync_assets(project_dir, state)
    stage_dir = project_dir / TRANSACTION_DIR / transaction["id"]
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    return state


def _sync_assets(project_dir, state):
    pending = state.get('asset_sync', {})
    if pending.get('status') != 'pending':
        return state
    record_routes(project_dir, pending['transaction']['entries'])
    revisions = sync_transaction(project_dir, pending['transaction'])
    state['asset_sync'] = {'status': 'complete', 'revisions': revisions}
    _atomic_json(project_dir / STATE_NAME, state)
    return state


def _check_current_files(project_dir, entries):
    project_dir = Path(project_dir).resolve()
    for entry in entries:
        target = project_dir / entry['target']
        if target.is_symlink():
            raise RuntimeError('refusing symlink target')
        target.resolve().relative_to(project_dir)
        if target.exists() and _sha256(target) not in {entry['desired_sha256'], entry.get('original_sha256')}:
            raise RuntimeError('refusing to overwrite user-modified file during recovery: ' + entry['target'])
        if not target.exists() and entry['original_exists']:
            raise RuntimeError('previously existing target was removed: ' + entry['target'])


def _roll_forward(project_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    transaction = state["active_transaction"]
    _check_current_files(project_dir, transaction['entries'])
    for entry in transaction["entries"]:
        target = project_dir / entry["target"]
        if target.is_file() and _sha256(target) == entry["desired_sha256"]:
            entry["committed"] = True
            _atomic_json(project_dir / STATE_NAME, state)
            continue
        staged = project_dir / entry["staged"]
        if not staged.is_file() or _sha256(staged) != entry["desired_sha256"]:
            raise RuntimeError(f"cannot roll forward {entry['target']}: valid staged file is missing")
        os.replace(staged, target)
        _fsync_directory(target.parent)
        entry["committed"] = True
        _atomic_json(project_dir / STATE_NAME, state)
    return _finish(project_dir, state, "committed")


def _rollback(project_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    transaction = state["active_transaction"]
    _check_current_files(project_dir, transaction['entries'])
    for entry in reversed(transaction["entries"]):
        target = project_dir / entry["target"]
        if target.exists():
            current_hash = _sha256(target)
            known_hashes = {entry["desired_sha256"], entry.get("original_sha256")}
            if current_hash not in known_hashes:
                raise RuntimeError(
                    f"refusing to overwrite user-modified file during rollback: {entry['target']}"
                )
        if entry["original_exists"]:
            backup = project_dir / entry["backup"]
            if not backup.is_file() or _sha256(backup) != entry["original_sha256"]:
                raise RuntimeError(f"cannot restore {entry['target']}: valid backup is missing")
            restore_copy = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
            shutil.copy2(backup, restore_copy)
            os.replace(restore_copy, target)
        elif target.exists():
            target.unlink()
        _fsync_directory(project_dir)
    return _finish(project_dir, state, "rolled_back")


def _apply_transaction(project_dir: str | Path, writes: list[dict[str, Any]]) -> dict[str, Any]:
    project = _resolve_project_dir(project_dir)
    state = _load_state(project)
    if state.get("active_transaction"):
        raise RuntimeError("unfinished transaction exists; run recover before starting another write")
    state = _sync_assets(project, state)
    txid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    try:
        entries = _prepare_entries(project, writes, txid)
    except Exception:
        shutil.rmtree(project / TRANSACTION_DIR / txid, ignore_errors=True)
        shutil.rmtree(project / BACKUP_DIR / txid, ignore_errors=True)
        raise
    state["active_transaction"] = {
        "id": txid,
        "status": "prepared",
        "created_at": _now(),
        "entries": entries,
    }
    _atomic_json(project / STATE_NAME, state)
    return _roll_forward(project, state)


def _recover_transaction(project_dir: str | Path, mode: str = "auto") -> dict[str, Any]:
    project = _resolve_project_dir(project_dir)
    state = _load_state(project)
    transaction = state.get("active_transaction")
    if not transaction:
        return _sync_assets(project, state)
    if mode == "rollback":
        return _rollback(project, state)
    if mode == "roll-forward":
        return _roll_forward(project, state)

    can_roll_forward = True
    for entry in transaction["entries"]:
        target = project / entry["target"]
        target_matches = target.is_file() and _sha256(target) == entry["desired_sha256"]
        staged = project / entry["staged"]
        stage_matches = staged.is_file() and _sha256(staged) == entry["desired_sha256"]
        if not target_matches and not stage_matches:
            can_roll_forward = False
            break
    return _roll_forward(project, state) if can_roll_forward else _rollback(project, state)


def apply_transaction(project_dir, writes):
    project = _resolve_project_dir(project_dir)
    with project_lock(project):
        return _apply_transaction(project, writes)


def recover_transaction(project_dir, mode='auto'):
    project = _resolve_project_dir(project_dir)
    with project_lock(project):
        return _recover_transaction(project, mode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply or recover a director-project file transaction.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--project-dir", required=True)
    apply_parser.add_argument("--spec", required=True, type=Path, help="JSON with a writes array")
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--project-dir", required=True)
    recover_parser.add_argument("--mode", choices=("auto", "roll-forward", "rollback"), default="auto")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project-dir", required=True)
    args = parser.parse_args()

    if args.command == "apply":
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        result = apply_transaction(args.project_dir, spec.get("writes", []))
    elif args.command == "recover":
        result = recover_transaction(args.project_dir, args.mode)
    else:
        result = _load_state(_resolve_project_dir(args.project_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
