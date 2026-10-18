#!/usr/bin/env python3
"""Portable per-user DIAGONAL catalog, immutable revisions and local browser."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

MARKER = '.diagonal_project.json'
STATUSES = ('draft', 'recorded', 'confirmed', 'final', 'needs_review', 'archived')
CATEGORIES = ('正式分镜', '提示词', '人物资产', '场景资产', '道具资产', '其他成果')
MEMORY_FILES = {'storyboard_scene_cards.md', 'storyboard_continuity.md', 'storyboard_locations.md'}


def now():
    return datetime.now(timezone.utc).isoformat()


def default_root():
    return Path(os.environ.get('DIAGONAL_ASSET_HOME') or
                Path.home() / 'Documents' / 'Codex' / 'DIAGONAL资产库').expanduser().resolve()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def project_lock(project):
    """OS releases the lock on process exit, including abnormal termination."""
    import time
    path = Path(project) / '.diagonal_write.lock'
    with path.open('a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0'); f.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('project busy; retry after the other writer completes')
                time.sleep(.1)
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Library:
    def __init__(self, root=None):
        self.root = Path(root).expanduser().resolve() if root else default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'objects').mkdir(exist_ok=True)
        self.db = self.root / 'catalog.sqlite3'
        with self.connect() as c:
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError('unsupported asset library schema')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                created TEXT NOT NULL, deleted_at TEXT, original_path TEXT);
              CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
                key TEXT NOT NULL, UNIQUE(project,key));
              CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY, asset TEXT NOT NULL REFERENCES assets(id),
                version INTEGER NOT NULL, hash TEXT NOT NULL, object TEXT NOT NULL,
                title TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                scene TEXT NOT NULL, source TEXT NOT NULL, origin TEXT NOT NULL,
                parents TEXT NOT NULL, created TEXT NOT NULL,
                UNIQUE(asset,version));
            ''')
            if version == 1:
                c.execute('ALTER TABLE projects ADD COLUMN deleted_at TEXT')
                c.execute('ALTER TABLE projects ADD COLUMN original_path TEXT')
            c.execute('PRAGMA user_version=2')

    def connect(self):
        c = sqlite3.connect(self.db, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    def attach(self, project, title=None):
        project = Path(project).expanduser().resolve()
        project.mkdir(parents=True, exist_ok=True)
        with project_lock(project):
            marker = project / MARKER
            if marker.exists():
                old = json.loads(marker.read_text(encoding='utf-8'))
                if Path(old['library']).resolve() != self.root:
                    raise ValueError('project belongs to another library; do not silently rebind')
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM projects WHERE path=?', (str(project),)).fetchone()
                if row:
                    if row['deleted_at']:
                        raise ValueError('项目在回收站中，请先恢复')
                    record = dict(row)
                else:
                    record = dict(id='PRJ-' + uuid.uuid4().hex[:16], title=title or project.name,
                                  path=str(project), created=now())
                    c.execute('INSERT INTO projects (id,title,path,created) VALUES (:id,:title,:path,:created)', record)
                if marker.exists() and old['project_id'] != record['id']:
                    raise ValueError('copied/moved project marker detected; use a clean import directory')
            atomic_text(marker, json.dumps({'schema_version': 1, 'library': str(self.root),
                        'project_id': record['id']}, ensure_ascii=False, indent=2))
            for category in CATEGORIES:
                (project / category).mkdir(exist_ok=True)
        return record

    def projects(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY created DESC')]

    def trashed_projects(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM projects WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC')]

    def delete_project(self, project_id):
        record = self.by_id(project_id)
        project = Path(record['path'])
        managed = project.parent == (self.root / 'projects').resolve()
        destination = self.root / '.project_trash' / record['id'] if managed else project
        with project_lock(project):
            state = project / '.storyboard_project_state.json'
            if state.exists():
                data = json.loads(state.read_text(encoding='utf-8'))
                if data.get('active_transaction') or data.get('asset_sync', {}).get('status') == 'pending':
                    raise RuntimeError('该项目有待完成的写入，请让 Codex 恢复项目后重试')
            moved = False
            try:
                with self.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if managed:
                        destination.parent.mkdir(exist_ok=True)
                        if destination.exists(): raise RuntimeError('项目回收位置已被占用')
                        os.replace(project, destination)
                        moved = True
                    updated = c.execute('UPDATE projects SET path=?,original_path=?,deleted_at=? WHERE id=? AND deleted_at IS NULL',
                                        (str(destination), str(project), now(), project_id))
                    if updated.rowcount != 1: raise RuntimeError('项目状态已变化，请刷新后重试')
            except Exception:
                if moved and destination.exists() and not project.exists():
                    os.replace(destination, project)
                raise
        return {'ok': True, 'files_moved': managed}

    def restore_project(self, project_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM projects WHERE id=? AND deleted_at IS NOT NULL', (project_id,)).fetchone()
        if not row: raise ValueError('项目不在回收站')
        source, original = Path(row['path']), Path(row['original_path'])
        if not source.is_dir(): raise FileNotFoundError('回收站中的项目目录不存在')
        managed = source != original
        with project_lock(source):
            moved = False
            try:
                with self.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if managed:
                        if original.exists(): raise RuntimeError('原位置已有文件，无法恢复项目')
                        original.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(source, original)
                        moved = True
                    updated = c.execute('UPDATE projects SET path=?,original_path=NULL,deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL',
                                        (str(original), project_id))
                    if updated.rowcount != 1: raise RuntimeError('项目状态已变化，请刷新后重试')
            except Exception:
                if moved and original.exists() and not source.exists():
                    os.replace(original, source)
                raise
        return self.by_id(project_id)

    def by_id(self, project_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM projects WHERE id=? AND deleted_at IS NULL', (project_id,)).fetchone()
        if not row:
            raise ValueError('项目不存在')
        return dict(row)

    def project(self, project):
        project = Path(project).expanduser().resolve()
        marker = json.loads((project / MARKER).read_text(encoding='utf-8'))
        if Path(marker['library']).resolve() != self.root:
            raise ValueError('wrong asset library')
        with self.connect() as c:
            row = c.execute('SELECT * FROM projects WHERE id=? AND path=? AND deleted_at IS NULL',
                            (marker['project_id'], str(project))).fetchone()
        if not row:
            raise ValueError('unregistered or moved project; restore its original binding first')
        return dict(row)

    def snapshot(self, source, expected=None):
        source = Path(source).expanduser().resolve()
        suffix = source.suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
            suffix = '.bin'
        fd, name = tempfile.mkstemp(prefix='.capture-', dir=self.root / 'objects')
        os.close(fd)
        try:
            shutil.copyfile(source, name)
            hashed = digest(name)
            if expected and hashed != expected:
                raise RuntimeError('source changed since the expected revision: ' + str(source))
            target = self.root / 'objects' / hashed[:2] / (hashed + suffix)
            target.parent.mkdir(exist_ok=True)
            if target.exists():
                if digest(target) != hashed:
                    raise RuntimeError('stored snapshot is corrupt: ' + str(target))
            else:
                with open(name, 'rb') as f:
                    os.fsync(f.fileno())
                os.replace(name, target)
            return hashed, str(target.relative_to(self.root))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def register(self, project, entries):
        record = self.project(project)
        prepared = []
        for e in entries:
            source = Path(e['source']).expanduser().resolve()
            status = e.get('status', 'recorded')
            if status not in STATUSES:
                raise ValueError('invalid status: ' + status)
            parents = e.get('parents', [])
            if not isinstance(parents, list) or not all(isinstance(x, str) for x in parents):
                raise ValueError('parents must contain exact revision IDs')
            hashed, obj = self.snapshot(source, e.get('expected_sha256'))
            prepared.append(dict(key=e.get('key') or str(source), hash=hashed, object=obj,
                title=e.get('title') or source.name, kind=e.get('kind') or infer_kind(source.name),
                status=status, scene=str(e.get('scene', '')), source=str(source),
                origin=e.get('origin', ''), parents=json.dumps(sorted(set(parents)))))
        results = []
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in prepared:
                for parent in json.loads(item['parents']):
                    if not c.execute('SELECT 1 FROM revisions WHERE id=?', (parent,)).fetchone():
                        raise ValueError('unknown source revision: ' + parent)
                asset = c.execute('SELECT id FROM assets WHERE project=? AND key=?',
                                  (record['id'], item['key'])).fetchone()
                aid = asset['id'] if asset else 'AST-' + uuid.uuid4().hex
                if not asset:
                    c.execute('INSERT INTO assets VALUES (?,?,?)', (aid, record['id'], item['key']))
                last = c.execute('SELECT * FROM revisions WHERE asset=? ORDER BY version DESC LIMIT 1',
                                 (aid,)).fetchone()
                fields = ('hash','title','kind','status','scene','parents')
                if last and all(last[k] == item[k] for k in fields):
                    results.append(dict(last)); continue
                revision = {k: item[k] for k in ('hash','object','title','kind','status','scene','source','origin','parents')}
                revision.update(id='REV-' + uuid.uuid4().hex, asset=aid,
                                version=last['version'] + 1 if last else 1, created=now())
                c.execute('''INSERT INTO revisions VALUES
                    (:id,:asset,:version,:hash,:object,:title,:kind,:status,:scene,:source,:origin,:parents,:created)''', revision)
                results.append(revision)
        return results

    def records(self, query='', project_id=None, history=False):
        with self.connect() as c:
            rows = [dict(r) for r in c.execute('''SELECT r.*, a.project, a.key,
                p.title AS project_title FROM revisions r JOIN assets a ON a.id=r.asset
                JOIN projects p ON p.id=a.project WHERE p.deleted_at IS NULL ORDER BY r.created DESC''')]
        latest = {}
        for row in rows:
            latest.setdefault(row['asset'], row['id'])
        by_id = {r['id']: r for r in rows}
        result = []
        for r in rows:
            r['latest'] = latest[r['asset']] == r['id']
            if not history and not r['latest']:
                continue
            if project_id and r['project'] != project_id:
                continue
            if query.casefold() not in ' '.join(str(r[k]) for k in ('title','kind','scene','project_title','status','key')).casefold():
                continue
            r['stale_sources'] = [x for x in json.loads(r['parents'])
                                  if x not in by_id or latest[by_id[x]['asset']] != x]
            r['snapshot_path'] = str(self.root / r['object'])
            r['source_exists'] = Path(r['source']).is_file()
            result.append(r)
        return result

    def verify(self):
        problems = []
        for r in self.records(history=True):
            p = Path(r['snapshot_path'])
            if not p.is_file() or digest(p) != r['hash']:
                problems.append({'revision': r['id'], 'problem': 'snapshot missing or damaged'})
            if r['latest'] and not r['key'].startswith('preview:'):
                src = Path(r['source'])
                if not src.is_file():
                    problems.append({'revision': r['id'], 'problem': 'working file missing; snapshot retained'})
                elif digest(src) != r['hash']:
                    problems.append({'revision': r['id'], 'problem': 'working file changed; needs registration'})
        return problems

def infer_kind(name):
    if name.startswith('storyboard_第'): return '分镜'
    if name.startswith('location_'): return 'MJ提示词'
    if re.match(r'第.+场提示词', name): return 'Seedance提示词'
    for token, kind in [('global_analysis','全片分析'),('scene_cards','场次工作卡'),
                        ('continuity','连续性'),('locations','场地档案'),('stats','统计'),('emotion_curve','情绪曲线')]:
        if token in name: return kind
    ext = Path(name).suffix.lower()
    if ext in ('.png','.jpg','.jpeg','.webp','.gif'): return '图片'
    if ext in ('.mp4','.mov','.webm'): return '视频'
    if ext in ('.mp3','.wav','.m4a'): return '音频'
    return '文档'


def library_for(project):
    marker = json.loads((Path(project) / MARKER).read_text(encoding='utf-8'))
    return Library(marker['library'])


def checked_relative(value, visible=False):
    """Never interpret user names as absolute, hidden, drive or parent paths."""
    from pathlib import PurePosixPath
    if not isinstance(value, str) or '\\' in value or ':' in value or '\x00' in value:
        raise ValueError('无效路径')
    p = PurePosixPath(value)
    if p.is_absolute() or any(x.startswith('.') or x in ('', '..') for x in p.parts):
        raise ValueError('无效路径')
    if visible and (not p.parts or p.parts[0] not in CATEGORIES):
        raise ValueError('只能访问项目中的内容文件夹')
    return p


def resolve_file(project, relative, visible=False):
    p = checked_relative(relative, visible)
    base = Path(project).resolve()
    target = base.joinpath(*p.parts)
    # Reject even symlinks whose destinations happen to be inside the project.
    current = base
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('不支持符号链接')
    target.resolve().relative_to(base)
    return target


def route_target(project, filename, scene=None):
    """Logical canonical names resolve to one physical file, never a mirror."""
    from safe_names import sanitize_filename, sanitize_component
    from pathlib import PurePosixPath
    requested = str(filename)
    filename = sanitize_filename(requested)
    if not (Path(project) / MARKER).exists():
        return filename
    # Reuse a previously established path, including moves made in the website.
    mapping = Path(project) / '.diagonal_paths.json'
    if mapping.exists():
        routes = json.loads(mapping.read_text(encoding='utf-8'))
        for key in (requested, filename):
            if key not in routes:
                continue
            relative = routes[key]
            if not isinstance(relative, str) or '\\' in relative or ':' in relative or '\x00' in relative:
                raise ValueError('invalid recorded project path')
            parts = PurePosixPath(relative).parts
            if (not parts or PurePosixPath(relative).is_absolute()
                    or any(part.startswith('.') for part in parts[1:])
                    or not (parts[0] in CATEGORIES or
                            (parts[0] == '.diagonal_memory' and len(parts) == 2
                             and parts[1] in MEMORY_FILES))):
                raise ValueError('invalid recorded project path')
            target = Path(project).resolve().joinpath(*parts)
            target.resolve().relative_to(Path(project).resolve())
            return relative
    if filename in MEMORY_FILES:
        return '.diagonal_memory/' + filename
    match = re.match(r'storyboard_(第.+?场)(?:_(.*))?\.md$', filename)
    if match:
        folder = scene or (match[1] + ('_' + match[2] if match[2] else ''))
        return '正式分镜/' + sanitize_component(folder) + '/' + filename
    if filename == 'storyboard_完整版.md':
        return '正式分镜/全片/' + filename
    if filename.startswith('location_') or re.match(r'第.+场提示词', filename):
        if not scene:
            raise ValueError('提示词写入须提供 scene（如第01场_客厅、跨场共用_客厅）')
        return '提示词/' + sanitize_component(scene) + '/' + filename
    return '其他成果/' + sanitize_component(scene or '全片') + '/' + filename


def record_routes(project, entries):
    if not (Path(project) / MARKER).exists():
        return
    path = Path(project) / '.diagonal_paths.json'
    routes = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for e in entries:
        routes[e['requested_target']] = e['target']
    atomic_text(path, json.dumps(routes, ensure_ascii=False, indent=2))


def sync_transaction(project, transaction):
    lib = library_for(project)
    entries = []
    for e in transaction['entries']:
        if e['target'].startswith('.'):
            continue
        metadata = e.get('asset_metadata', {})
        entries.append({**metadata, 'source': str(Path(project) / e['target']),
                        'key': e['requested_target'], 'expected_sha256': e['desired_sha256'],
                        'origin': metadata.get('origin') or transaction['id']})
    result = lib.register(project, entries)
    return [r['id'] for r in result]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, help='default: per-user local DIAGONAL library')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    create = sub.add_parser('create'); create.add_argument('--title', required=True)
    attach = sub.add_parser('attach'); attach.add_argument('--project', type=Path, required=True); attach.add_argument('--title')
    register = sub.add_parser('register'); register.add_argument('--project', type=Path, required=True)
    register.add_argument('--manifest', type=Path, required=True, help='JSON {assets:[...]}')
    listing = sub.add_parser('list'); listing.add_argument('--query', default=''); listing.add_argument('--project-id'); listing.add_argument('--history', action='store_true')
    sub.add_parser('verify'); sub.add_parser('projects')
    resolve = sub.add_parser('resolve'); resolve.add_argument('--project', type=Path, required=True)
    resolve.add_argument('--name', required=True); resolve.add_argument('--scene')
    args = p.parse_args()
    if args.command == 'register' and args.root is None:
        lib = library_for(args.project)
    else:
        lib = Library(args.root)
    if args.command == 'create':
        from safe_names import sanitize_component
        directory = lib.root / 'projects' / (sanitize_component(args.title, 50) + '-' + uuid.uuid4().hex[:8])
        result = lib.attach(directory, args.title)
    elif args.command == 'attach':
        result = lib.attach(args.project, args.title)
    elif args.command == 'register':
        result = lib.register(args.project, json.loads(args.manifest.read_text(encoding='utf-8'))['assets'])
    elif args.command == 'list':
        result = lib.records(args.query, args.project_id, args.history)
    elif args.command == 'projects':
        result = lib.projects()
    elif args.command == 'resolve':
        result = {'path': str(args.project.resolve() / route_target(args.project, args.name, args.scene))}
    elif args.command == 'verify':
        result = lib.verify()
    else:
        result = {'library': str(lib.root)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == 'verify' and result:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
