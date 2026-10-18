#!/usr/bin/env python3
"""Local-only folder browser and upload server. Python standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from asset_library import (Library, CATEGORIES, atomic_text, digest, now,
                           project_lock, resolve_file)
from safe_names import sanitize_component, unique_path

TEXT_EXT = {'.md', '.txt', '.csv', '.json', '.srt', '.vtt'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.avif'}
VIDEO_EXT = {'.mp4', '.webm', '.mov', '.m4v'}
AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}
MAX_TEXT = 4 * 1024 * 1024
MAX_UPLOAD = 4 * 1024 * 1024 * 1024


def build_id():
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for path in (Path(__file__), root / 'scripts' / 'asset_library.py',
                 root / 'assets' / 'library.html', root / 'assets' / 'logo-white.png',
                 root / 'assets' / 'logo-black.png'):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def file_type(path):
    if path.is_dir(): return 'folder'
    ext = path.suffix.lower()
    if ext in IMAGE_EXT: return 'image'
    if ext in VIDEO_EXT: return 'video'
    if ext in AUDIO_EXT: return 'audio'
    if ext in TEXT_EXT: return 'text'
    if ext == '.pdf': return 'pdf'
    return 'file'


def name_component(name):
    if not name or name.startswith('.') or sanitize_component(name, 120) != name:
        raise ValueError('名称不能为空，也不能包含路径或特殊字符')
    return name


def writable(project):
    state = project / '.storyboard_project_state.json'
    if state.exists():
        data = json.loads(state.read_text(encoding='utf-8'))
        if data.get('active_transaction') or data.get('asset_sync', {}).get('status') == 'pending':
            raise RuntimeError('该项目有待完成的写入，请让 Codex 恢复项目后重试')


def event(project, action, **fields):
    path = project / '.diagonal_user_changes.jsonl'
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'time': now(), 'action': action, **fields}, ensure_ascii=False) + '\n')


def update_paths(lib, project, old, new):
    path = project / '.diagonal_paths.json'
    if path.exists():
        routes = json.loads(path.read_text(encoding='utf-8'))
        for key, value in list(routes.items()):
            if value == old or value.startswith(old + '/'):
                routes[key] = new + value[len(old):]
        atomic_text(path, json.dumps(routes, ensure_ascii=False, indent=2))
    with lib.connect() as c:
        record = lib.project(project)
        rows = c.execute('SELECT r.id,r.source FROM revisions r JOIN assets a ON a.id=r.asset WHERE a.project=?', (record['id'],)).fetchall()
        src, dst = str(project / old), str(project / new)
        for row in rows:
            if row['source'] == src or row['source'].startswith(src + os.sep):
                c.execute('UPDATE revisions SET source=? WHERE id=?', (dst + row['source'][len(src):], row['id']))


class Handler(BaseHTTPRequestHandler):
    server_version = 'DIAGONAL/1.0'

    def log_message(self, fmt, *args):
        # No file names, prompt contents, tokens or query strings in routine logs.
        pass

    @property
    def lib(self): return self.server.library

    def guard(self, mutation=False):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            raise PermissionError('只接受本机地址')
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + expected:
            raise PermissionError('跨站请求已拒绝')
        if mutation and not secrets.compare_digest(self.headers.get('X-Diagonal-Token', ''), self.server.token):
            raise PermissionError('页面已过期，请刷新后重试')

    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length < 0 or length > MAX_TEXT:
            raise ValueError('请求内容过大')
        return json.loads(self.rfile.read(length) or b'{}')

    def project(self, value): return Path(self.lib.by_id(value)['path'])

    def run(self, method):
        try:
            self.guard(method != 'GET')
            split = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
            if method == 'GET': self.get(split.path, q)
            elif method == 'PUT': self.upload(split.path, q)
            else: self.post(split.path, self.body())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except PermissionError as e: self.json({'error': str(e)}, 403)
        except FileNotFoundError: self.json({'error': '文件不存在，请刷新目录'}, 404)
        except (ValueError, KeyError, TypeError) as e: self.json({'error': str(e)}, 400)
        except RuntimeError as e: self.json({'error': str(e)}, 409)
        except Exception as e: self.json({'error': f'操作未完成：{type(e).__name__}: {e}'}, 500)

    def do_GET(self): self.run('GET')
    def do_POST(self): self.run('POST')
    def do_PUT(self): self.run('PUT')

    def get(self, route, q):
        if route in ('/assets/logo-white.png', '/assets/logo-black.png'):
            filename = 'logo-white.png' if route.endswith('white.png') else 'logo-black.png'
            body = (Path(__file__).resolve().parents[1] / 'assets' / filename).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if route == '/':
            template = Path(__file__).resolve().parents[1] / 'assets' / 'library.html'
            body = template.read_text(encoding='utf-8').replace('__TOKEN__', self.server.token).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if route == '/api/ping':
            self.json({'app': 'DIAGONAL', 'instance': self.server.instance,
                       'build': self.server.build}); return
        if route == '/api/projects':
            self.json({'projects': self.lib.projects(), 'root': str(self.lib.root)}); return
        if route == '/api/project-trash':
            self.json({'projects': self.lib.trashed_projects()}); return
        project = self.project(q['project'])
        if route == '/api/trash':
            records = []
            for p in (project / '.diagonal_trash').glob('*/manifest.json'):
                data = json.loads(p.read_text(encoding='utf-8'))
                if (p.parent / 'content').exists(): records.append(data)
            self.json({'items': sorted(records, key=lambda x: x['time'], reverse=True)}); return
        relative = q.get('path', '')
        if route == '/api/list':
            if not relative:
                candidates = [project / name for name in CATEGORIES]
            else:
                directory = resolve_file(project, relative, True)
                if not directory.is_dir(): raise ValueError('这不是文件夹')
                candidates = list(directory.iterdir())
            query = q.get('q', '').casefold()
            if query:
                roots = [project / c for c in CATEGORIES] if not relative else [directory]
                candidates = []
                for root in roots:
                    for current, dirs, files in os.walk(root, followlinks=False):
                        dirs[:] = [x for x in dirs if not x.startswith('.') and not (Path(current)/x).is_symlink()]
                        for name in dirs + files:
                            if query in name.casefold(): candidates.append(Path(current) / name)
                        if len(candidates) > 2000: break
                    if len(candidates) > 2000: break
            items = []
            for path in candidates[:2000]:
                if path.name.startswith('.') or path.is_symlink() or not path.exists(): continue
                stat = path.stat()
                items.append({'name': path.name, 'path': path.relative_to(project).as_posix(),
                              'type': file_type(path), 'size': stat.st_size if path.is_file() else 0,
                              'modified': stat.st_mtime})
            if not relative and not query:
                items.sort(key=lambda x: CATEGORIES.index(x['name']))
            else:
                items.sort(key=lambda x: (x['type'] != 'folder', x['name']))
            self.json({'items': items, 'path': relative, 'truncated': len(candidates) > 2000}); return
        if route == '/api/history':
            target = resolve_file(project, relative, True)
            history = [r for r in self.lib.records(history=True) if r['source'] == str(target)]
            self.json({'items': [{'id': r['id'], 'version': r['version'], 'created': r['created'],
                                  'status': r['status'], 'kind': r['kind']} for r in history]}); return
        if route == '/api/history-file':
            target = resolve_file(project, relative, True)
            revision = q.get('revision', '')
            match = next((r for r in self.lib.records(history=True)
                          if r['id'] == revision and r['source'] == str(target)), None)
            if not match: raise FileNotFoundError()
            self.send_file(Path(match['snapshot_path']), True, target.name); return
        path = resolve_file(project, relative, True)
        if route == '/api/text':
            if path.suffix.lower() not in TEXT_EXT or path.stat().st_size > MAX_TEXT:
                raise ValueError('此文件请下载后查看')
            data = path.read_bytes()
            import hashlib
            self.json({'text': data.decode('utf-8-sig'), 'hash': hashlib.sha256(data).hexdigest()}); return
        if route == '/api/file':
            self.send_file(path, q.get('download') == '1'); return
        raise ValueError('未知接口')

    def send_file(self, path, download=False, display_name=None):
        if not path.is_file(): raise FileNotFoundError()
        size = path.stat().st_size
        start, end, partial = 0, size - 1, False
        requested = self.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not any(match.groups()): raise ValueError('无效播放范围')
            a, b = match.groups()
            if a: start, end = int(a), min(int(b), size-1) if b else size-1
            else: start, end = max(0, size-int(b)), size-1
            if start > end or start >= size:
                self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
            partial = True
        kind = file_type(path)
        inline = kind in ('image','video','audio') and not download
        mime = mimetypes.guess_type(path.name)[0] if inline else 'application/octet-stream'
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', mime or 'application/octet-stream')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Accept-Ranges', 'bytes')
        if not inline:
            from urllib.parse import quote
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(display_name or path.name))
        if partial: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(max(0, end-start+1)))
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start); remaining = end-start+1
            while remaining > 0:
                block = f.read(min(1024*1024, remaining))
                if not block: break
                self.wfile.write(block); remaining -= len(block)

    def upload(self, route, q):
        if route != '/api/upload': raise ValueError('未知接口')
        project = self.project(q['project'])
        directory = resolve_file(project, q['path'], True)
        if not directory.is_dir(): raise ValueError('请先进入上传目标文件夹')
        name = name_component(q['name'])
        length = int(self.headers.get('Content-Length', -1))
        if length < 0 or length > MAX_UPLOAD: raise ValueError('单个文件上限为 4 GB')
        stage = project / '.diagonal_uploads'; stage.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=stage)
        try:
            with os.fdopen(fd, 'wb') as f:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(1024*1024, remaining))
                    if not block: raise ValueError('上传中断')
                    f.write(block); remaining -= len(block)
                f.flush(); os.fsync(f.fileno())
            with project_lock(project):
                writable(project)
                directory = resolve_file(project, q['path'], True)
                if not directory.is_dir(): raise ValueError('目标文件夹已移动，请刷新后重新上传')
                target = unique_path(directory, name)
                os.replace(temporary, target)
                event(project, 'upload', path=target.relative_to(project).as_posix())
            self.json({'path': target.relative_to(project).as_posix(), 'name': target.name})
        finally:
            if os.path.exists(temporary): os.unlink(temporary)

    def post(self, route, data):
        if route == '/api/create-project':
            title = name_component(data['title'])
            path = self.lib.root / 'projects' / (sanitize_component(title, 50) + '-' + uuid.uuid4().hex[:8])
            self.json(self.lib.attach(path, title)); return
        if route == '/api/delete-project':
            self.json(self.lib.delete_project(data['project'])); return
        if route == '/api/restore-project':
            self.json(self.lib.restore_project(data['project'])); return
        project = self.project(data['project'])
        with project_lock(project):
            writable(project)
            if route == '/api/mkdir':
                parent = resolve_file(project, data['path'], True)
                target = parent / name_component(data['name'])
                target.mkdir(exist_ok=False)
                self.json({'path': target.relative_to(project).as_posix()}); return
            if route == '/api/restore':
                item_id = data['id']
                if not re.fullmatch(r'[a-f0-9]{32}', item_id): raise ValueError('无效记录')
                item = project / '.diagonal_trash' / item_id
                info = json.loads((item / 'manifest.json').read_text(encoding='utf-8'))
                target = resolve_file(project, info['path'], True)
                if target.exists(): raise RuntimeError('原位置已有同名内容，请先移动它再恢复')
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(item / 'content', target)
                event(project, 'restore', path=info['path'])
                self.json({'ok': True}); return
            relative = data['path']
            path = resolve_file(project, relative, True)
            if route == '/api/save':
                if path.suffix.lower() not in TEXT_EXT or len(data['text'].encode('utf-8')) > MAX_TEXT:
                    raise ValueError('不支持编辑此文件')
                if digest(path) != data['hash']: raise RuntimeError('文件已被其他会话修改，请重新打开后再编辑')
                routes_path = project / '.diagonal_paths.json'
                routes = json.loads(routes_path.read_text(encoding='utf-8')) if routes_path.exists() else {}
                key = next((logical for logical, current in routes.items() if current == relative), relative)
                self.lib.register(project, [{'source': str(path), 'key': key}])
                atomic_text(path, data['text'])
                self.lib.register(project, [{'source': str(path), 'key': key, 'status': 'needs_review', 'origin': 'local-editor'}])
                event(project, 'edit', path=relative)
                self.json({'hash': digest(path)}); return
            if relative in CATEGORIES: raise ValueError('项目的六个分类文件夹需要保留')
            if route == '/api/move':
                destination = resolve_file(project, data['destination'], True)
                name_component(destination.name)
                if destination.exists(): raise RuntimeError('目标位置已存在同名内容')
                if not destination.parent.is_dir(): raise ValueError('目标文件夹不存在')
                if path.is_dir() and path in destination.parents: raise ValueError('不能移动到自身内部')
                os.replace(path, destination)
                update_paths(self.lib, project, relative, data['destination'])
                event(project, 'move', path=relative, destination=data['destination'])
                self.json({'ok': True}); return
            if route == '/api/delete':
                item_id = uuid.uuid4().hex
                folder = project / '.diagonal_trash' / item_id; folder.mkdir(parents=True)
                atomic_text(folder / 'manifest.json', json.dumps({'id': item_id, 'path': relative, 'time': now()}, ensure_ascii=False))
                os.replace(path, folder / 'content')
                event(project, 'delete', path=relative)
                self.json({'ok': True}); return
            raise ValueError('未知接口')


def make_server(root=None, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.library = Library(root)
    server.token = secrets.token_urlsafe(32)
    server.instance = uuid.uuid4().hex
    server.build = build_id()
    return server


def existing_server(lib):
    state = lib.root / '.server.json'
    if not state.exists(): return None
    try:
        data = json.loads(state.read_text(encoding='utf-8'))
        parsed = urlsplit(data['url'])
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1': return None
        with urlopen(data['url'] + '/api/ping', timeout=1) as response:
            ping = json.load(response)
        return (data, ping) if ping.get('instance') == data['instance'] else None
    except Exception:
        return None


def running(lib):
    existing = existing_server(lib)
    return existing[0]['url'] if existing and existing[1].get('build') == build_id() else None


def start(lib):
    with project_lock(lib.root):
        url = running(lib)
        if url: return url
        existing = existing_server(lib)
        if existing and existing[1].get('build') != build_id():
            pid = existing[0].get('pid')
            if isinstance(pid, int) and pid > 0 and pid != os.getpid():
                try: os.kill(pid, signal.SIGTERM)
                except OSError: pass
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session': True}
        with (lib.root / '.server.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--root', str(lib.root), 'serve'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        for _ in range(80):
            time.sleep(.1)
            url = running(lib)
            if url: return url
        raise RuntimeError('服务启动失败，请检查资产库中的 .server.log')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path)
    p.add_argument('command', choices=('start','serve'), nargs='?', default='start')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--open', action='store_true')
    args = p.parse_args(); lib = Library(args.root)
    if args.command == 'start':
        url = start(lib)
        print(json.dumps({'url': url, 'library': str(lib.root)}, ensure_ascii=False), flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
    else:
        server = make_server(lib.root, args.port)
        url = f'http://127.0.0.1:{server.server_port}'
        atomic_text(lib.root / '.server.json', json.dumps({'url': url, 'instance': server.instance,
                                                           'build': server.build, 'pid': os.getpid()}))
        print(url, flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()


if __name__ == '__main__': main()
