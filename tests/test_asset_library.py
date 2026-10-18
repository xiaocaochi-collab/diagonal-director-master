import concurrent.futures
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asset_library import Library, CATEGORIES, digest, resolve_file, route_target
import project_transaction as tx
from asset_server import make_server


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.lib = Library(self.base / 'library')
        self.project = self.base / 'film'
        self.lib.attach(self.project, '测试影片')
        self.source = self.base / 'source.md'
        self.source.write_text('第一版', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, **extra):
        return tx.apply_transaction(self.project, [{'target': 'storyboard_第1场_客厅.md',
            'source': str(self.source), 'scene': '第01场_客厅', **extra}])

    def test_project_and_scene_folder_routing(self):
        self.write()
        target = self.project / '正式分镜/第01场_客厅/storyboard_第1场_客厅.md'
        self.assertEqual(target.read_text(), '第一版')
        self.assertEqual(route_target(self.project, target.name), target.relative_to(self.project).as_posix())
        self.assertEqual(len(self.lib.records()), 1)
        self.assertTrue(all((self.project / name).is_dir() for name in CATEGORIES))

    def test_memory_is_hidden_and_not_in_catalog(self):
        tx.apply_transaction(self.project, [{'target': 'storyboard_continuity.md', 'source': str(self.source)}])
        self.assertTrue((self.project / '.diagonal_memory/storyboard_continuity.md').is_file())
        self.assertEqual(self.lib.records(), [])

    def test_prompt_requires_scene_and_shares_scene_folder(self):
        self.assertRaises(ValueError, route_target, self.project, '第1场提示词.md')
        self.assertEqual(route_target(self.project, '第1场提示词.md', '第01场_客厅'), '提示词/第01场_客厅/第1场提示词.md')

    def test_overwrite_keeps_history_and_checks_base(self):
        self.write()
        self.assertRaisesRegex(RuntimeError, 'formal file already exists', self.write)
        target = self.project / route_target(self.project, 'storyboard_第1场_客厅.md')
        old = digest(target)
        self.source.write_text('第二版', encoding='utf-8')
        self.write(replace_existing=True, expected_sha256=old)
        records = self.lib.records(history=True)
        self.assertEqual(len(records), 2)
        self.assertEqual({Path(r['snapshot_path']).read_text() for r in records}, {'第一版', '第二版'})
        self.assertRaises(RuntimeError, self.write, replace_existing=True, expected_sha256=old)
        self.assertEqual(target.read_text(), '第二版')

    def test_missing_formal_file_is_not_silently_recreated(self):
        self.write()
        target = self.project / route_target(self.project, 'storyboard_第1场_客厅.md')
        target.unlink()
        with self.assertRaisesRegex(RuntimeError, 'previously registered file is missing'):
            self.write()
        self.assertFalse(target.exists())

    def test_global_fact_source_is_not_duplicated(self):
        writes = [{'target': 'storyboard_global_analysis.md', 'source': str(self.source)}]
        tx.apply_transaction(self.project, writes)
        self.source.write_text('第二版', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'formal file already exists'):
            tx.apply_transaction(self.project, writes)
        self.assertFalse((self.project / '其他成果/全片/storyboard_global_analysis-02.md').exists())

    def test_sanitized_logical_name_keeps_web_move_mapping(self):
        logical = 'location_客/厅_main_view_prompt.md'
        result = tx.apply_transaction(self.project, [{
            'target': logical, 'source': str(self.source), 'scene': '跨场共用_客厅',
        }])
        original = result['last_transaction']['entries'][0]['target']
        moved = '提示词/跨场共用_客厅/改名后.md'
        (self.project / original).rename(self.project / moved)
        from asset_server import update_paths
        update_paths(self.lib, self.project, original, moved)
        self.assertEqual(route_target(self.project, logical, '跨场共用_客厅'), moved)
        old_hash = digest(self.project / moved)
        self.source.write_text('移动后修订', encoding='utf-8')
        tx.apply_transaction(self.project, [{
            'target': logical, 'source': str(self.source), 'scene': '跨场共用_客厅',
            'replace_existing': True, 'expected_sha256': old_hash,
        }])
        self.assertEqual((self.project / moved).read_text(encoding='utf-8'), '移动后修订')
        self.assertFalse((self.project / original).exists())

    def test_recorded_path_cannot_escape_project(self):
        (self.project / '.diagonal_paths.json').write_text(
            json.dumps({'storyboard_第1场_客厅.md': '../../outside.md'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            route_target(self.project, 'storyboard_第1场_客厅.md')
        (self.project / '.diagonal_paths.json').write_text(
            json.dumps({'storyboard_第1场_客厅.md': '正式分镜/.hidden/file.md'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            route_target(self.project, 'storyboard_第1场_客厅.md')

    def test_browser_deletion_does_not_authorize_regeneration(self):
        self.write()
        from types import SimpleNamespace
        from asset_server import Handler
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(library=self.lib)
        replies = []
        handler.json = lambda value, status=200: replies.append((status, value))
        handler.post('/api/delete', {
            'project': self.lib.project(self.project)['id'],
            'path': '正式分镜/第01场_客厅/storyboard_第1场_客厅.md',
        })
        self.assertTrue(replies[0][1]['ok'])
        with self.assertRaisesRegex(RuntimeError, 'previously registered file is missing'):
            self.write()

    def test_recovery_preserves_manual_edit_even_with_valid_staging(self):
        self.write()
        target = self.project / route_target(self.project, 'storyboard_第1场_客厅.md')
        entries = tx._prepare_entries(self.project, [{'target': target.name, 'source': str(self.source),
            'replace_existing': True, 'expected_sha256': digest(target)}], 'interrupted')
        state = tx._load_state(self.project)
        state['active_transaction'] = {'id': 'interrupted', 'entries': entries, 'status': 'prepared'}
        tx._atomic_json(self.project / tx.STATE_NAME, state)
        target.write_text('人工修改', encoding='utf-8')
        self.assertRaisesRegex(RuntimeError, 'user-modified', tx.recover_transaction, self.project)
        self.assertEqual(target.read_text(), '人工修改')

    def test_pending_catalog_sync_can_recover_idempotently(self):
        with patch.object(tx, 'sync_transaction', side_effect=RuntimeError('catalog unavailable')):
            self.assertRaises(RuntimeError, self.write)
        self.assertEqual(tx._load_state(self.project)['asset_sync']['status'], 'pending')
        tx.recover_transaction(self.project)
        tx.recover_transaction(self.project)
        self.assertEqual(len(self.lib.records()), 1)

    def test_commit_does_not_claim_success_if_saved_content_differs(self):
        entries = tx._prepare_entries(self.project, [{'target': 'storyboard_第1场_客厅.md',
            'source': str(self.source), 'scene': '第01场_客厅'}], 'unverified')
        state = tx._load_state(self.project)
        state['active_transaction'] = {'id': 'unverified', 'entries': entries, 'status': 'prepared'}
        target = self.project / entries[0]['target']
        target.write_text('意外变化', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
            tx._finish(self.project, state, 'committed')
        self.assertEqual(state['active_transaction']['status'], 'prepared')
        self.assertFalse((self.project / tx.STATE_NAME).exists())

    def test_concurrent_writers_do_not_lose_different_files(self):
        def writer(n):
            source = self.base / f'source{n}.md'; source.write_text(str(n))
            tx.apply_transaction(self.project, [{'target': f'第{n}场提示词.md', 'source': str(source), 'scene': f'第{n}场'}])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer, range(8)))
        self.assertEqual(len(self.lib.records()), 8)
        self.assertEqual(len(json.loads((self.project / '.diagonal_paths.json').read_text())), 8)

    def test_paths_cannot_escape_or_reach_memory(self):
        for value in ('../outside', '/outside', '.diagonal_memory/test', '人物资产/../secret', 'C:/x'):
            with self.assertRaises(ValueError): resolve_file(self.project, value, True)
        (self.project / '人物资产/link').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError): resolve_file(self.project, '人物资产/link/source.md', True)

    def test_existing_catalog_migrates_to_project_trash_schema(self):
        legacy = self.base / 'legacy-library'; legacy.mkdir()
        with sqlite3.connect(legacy / 'catalog.sqlite3') as c:
            c.execute('CREATE TABLE projects (id TEXT PRIMARY KEY,title TEXT NOT NULL,path TEXT NOT NULL UNIQUE,created TEXT NOT NULL)')
            c.execute('PRAGMA user_version=1')
        upgraded = Library(legacy)
        self.assertEqual(upgraded.projects(), [])
        self.assertEqual(sqlite3.connect(upgraded.db).execute('PRAGMA user_version').fetchone()[0], 2)

    def test_managed_project_delete_and_restore_keeps_files(self):
        managed = self.lib.attach(self.lib.root / 'projects' / 'film', '可恢复影片')
        source = Path(managed['path'])
        (source / '人物资产' / 'photo.png').write_bytes(b'important')
        result = self.lib.delete_project(managed['id'])
        self.assertTrue(result['files_moved'])
        self.assertFalse(source.exists())
        self.assertNotIn(managed['id'], {x['id'] for x in self.lib.projects()})
        self.assertRaises(ValueError, self.lib.by_id, managed['id'])
        self.assertEqual(len(self.lib.trashed_projects()), 1)
        restored = self.lib.restore_project(managed['id'])
        self.assertEqual(restored['path'], str(source))
        self.assertEqual((source / '人物资产' / 'photo.png').read_bytes(), b'important')

    def test_external_project_removal_preserves_original_directory(self):
        result = self.lib.delete_project(self.lib.project(self.project)['id'])
        self.assertFalse(result['files_moved'])
        self.assertTrue(self.project.is_dir())
        self.assertEqual(self.lib.projects(), [])
        self.lib.restore_project(self.lib.trashed_projects()[0]['id'])
        self.assertEqual(len(self.lib.projects()), 1)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.server = make_server(self.root / 'lib')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.project = self.server.library.attach(self.root / 'project', '网页测试')
        self.pid = self.project['id']
        self.project_path = Path(self.project['path'])

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, method, route, body=None, auth=True, headers=None):
        h = {'X-Diagonal-Token': self.server.token} if auth else {}
        if isinstance(body, dict): body = json.dumps(body).encode(); h['Content-Type'] = 'application/json'
        if headers: h.update(headers)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, route, body, h)
        r = conn.getresponse(); data = r.read(); result = (r.status, dict(r.getheaders()), data); conn.close()
        return result

    def get(self, endpoint, **query):
        return self.request('GET', endpoint + '?' + urlencode({'project': self.pid, **query}))

    def post(self, endpoint, **data):
        return self.request('POST', endpoint, {'project': self.pid, **data})

    def upload(self, name='照片.png', data=b'png-test', path='人物资产'):
        return self.request('PUT', '/api/upload?' + urlencode({'project': self.pid, 'path': path, 'name': name}), data)

    def test_home_and_categories(self):
        status, _, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('导演资产库'.encode(), body)
        self.assertNotIn(b'__TOKEN__', body)
        status, _, body = self.get('/api/list', path='')
        self.assertEqual({x['name'] for x in json.loads(body)['items']}, set(CATEGORIES))

    def test_logo_images_are_served(self):
        for name in ('logo-white.png', 'logo-black.png'):
            status, headers, body = self.request('GET', '/assets/' + name)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertTrue(body.startswith(b'\x89PNG'))

    def test_project_delete_and_restore_api(self):
        status, _, body = self.request('POST', '/api/create-project', {'title':'可删除项目'})
        self.assertEqual(status, 200)
        created = json.loads(body)
        original = Path(created['path'])
        (original / '场景资产' / '现场.mov').write_bytes(b'film')
        status, _, _ = self.request('POST', '/api/delete-project', {'project':created['id']})
        self.assertEqual(status, 200)
        self.assertFalse(original.exists())
        self.assertEqual(self.get('/api/list', path='')[0], 200)
        self.assertEqual(self.request('GET', '/api/list?' + urlencode({'project':created['id'], 'path':''}))[0], 400)
        status, _, body = self.request('GET', '/api/project-trash')
        self.assertEqual(status, 200)
        self.assertEqual([p['id'] for p in json.loads(body)['projects']], [created['id']])
        status, _, body = self.request('POST', '/api/restore-project', {'project':created['id']})
        self.assertEqual(status, 200)
        self.assertEqual(Path(json.loads(body)['path']), original)
        self.assertEqual((original / '场景资产' / '现场.mov').read_bytes(), b'film')

    def test_upload_collision_and_listing(self):
        self.assertEqual(self.upload()[0], 200)
        self.assertEqual(self.upload()[0], 200)
        _, _, body = self.get('/api/list', path='人物资产')
        self.assertEqual({x['name'] for x in json.loads(body)['items']}, {'照片.png', '照片-02.png'})

    def test_mkdir_move_search_and_recycle_restore(self):
        self.post('/api/mkdir', path='人物资产', name='林雪')
        self.upload()
        self.assertEqual(self.post('/api/move', path='人物资产/照片.png', destination='人物资产/林雪/正面.png')[0], 200)
        _, _, body = self.get('/api/list', path='人物资产', q='正面')
        self.assertEqual(len(json.loads(body)['items']), 1)
        self.assertEqual(self.post('/api/delete', path='人物资产/林雪')[0], 200)
        _, _, body = self.get('/api/trash')
        item = json.loads(body)['items'][0]
        self.assertEqual(self.post('/api/restore', id=item['id'])[0], 200)
        self.assertTrue((self.project_path / '人物资产/林雪/正面.png').exists())

    def test_text_edit_conflict_and_original_snapshot(self):
        self.upload('提示.md', '原文'.encode(), '提示词')
        _, _, body = self.get('/api/text', path='提示词/提示.md')
        hashed = json.loads(body)['hash']
        self.assertEqual(self.post('/api/save', path='提示词/提示.md', text='修改', hash=hashed)[0], 200)
        self.assertEqual(self.post('/api/save', path='提示词/提示.md', text='过期写入', hash=hashed)[0], 409)
        self.assertEqual((self.project_path / '提示词/提示.md').read_text(), '修改')
        self.assertIn('原文', [Path(r['snapshot_path']).read_text() for r in self.server.library.records(history=True)])
        _, _, body = self.get('/api/history', path='提示词/提示.md')
        self.assertEqual(len(json.loads(body)['items']), 2)
        oldest = json.loads(body)['items'][-1]['id']
        status, headers, body = self.get('/api/history-file', path='提示词/提示.md', revision=oldest)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), '原文')
        self.assertIn('attachment', headers['Content-Disposition'])

    def test_video_range_requests(self):
        self.upload('test.mp4', b'0123456789', '场景资产')
        route = '/api/file?' + urlencode({'project': self.pid, 'path': '场景资产/test.mp4'})
        status, headers, data = self.request('GET', route, headers={'Range': 'bytes=2-5'})
        self.assertEqual(status, 206); self.assertEqual(data, b'2345'); self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(self.request('GET', route, headers={'Range': 'bytes=-3'})[2], b'789')
        self.assertEqual(self.request('GET', route, headers={'Range': 'bytes=99-'})[0], 416)

    def test_hidden_files_traversal_and_unsafe_upload_are_blocked(self):
        for value in ('.diagonal_project.json', '../source', '人物资产/../../secret'):
            self.assertEqual(self.get('/api/file', path=value)[0], 400)
        self.assertEqual(self.upload('../escape')[0], 400)
        self.assertEqual(self.post('/api/delete', path='人物资产')[0], 400)

    def test_csrf_and_host_checks(self):
        data = {'title': '不应创建'}
        self.assertEqual(self.request('POST', '/api/create-project', data, auth=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/create-project', data, headers={'Origin':'https://example.com'})[0], 403)
        self.assertEqual(self.request('GET', '/api/projects', headers={'Host':'example.com'})[0], 403)

    def test_active_content_downloads_instead_of_executing(self):
        self.upload('example.html', b'<script>alert(1)</script>', '其他成果')
        status, headers, _ = self.get('/api/file', path='其他成果/example.html')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertIn('attachment', headers['Content-Disposition'])

    def test_web_move_keeps_skill_logical_path(self):
        source = self.root / 'src.md'; source.write_text('镜头正文')
        tx.apply_transaction(self.project_path, [{'source': str(source), 'target': '第1场提示词.md', 'scene': '第01场_客厅'}])
        old = '提示词/第01场_客厅/第1场提示词.md'
        new = '提示词/第01场_客厅/修改后的名称.md'
        self.assertEqual(self.post('/api/move', path=old, destination=new)[0], 200)
        self.assertEqual(route_target(self.project_path, '第1场提示词.md'), new)
        source.write_text('新的提示词')
        tx.apply_transaction(self.project_path, [{'source': str(source), 'target': '第1场提示词.md',
            'scene': '第01场_客厅', 'replace_existing': True,
            'expected_sha256': digest(self.project_path / new)}])
        self.assertEqual((self.project_path / new).read_text(), '新的提示词')
        self.assertEqual(len(self.server.library.records(history=True)), 2)

    def test_skill_final_files_are_visible_in_same_project_folders(self):
        storyboard = self.root / 'shot.md'; storyboard.write_text('正式分镜内容', encoding='utf-8')
        prompt = self.root / 'prompt.md'; prompt.write_text('正式提示词内容', encoding='utf-8')
        tx.apply_transaction(self.project_path, [
            {'source': str(storyboard), 'target': 'storyboard_第1场_客厅.md', 'scene': '第01场_客厅'},
            {'source': str(prompt), 'target': '第1场提示词.md', 'scene': '第01场_客厅'},
            {'source': str(storyboard), 'target': 'storyboard_continuity.md'},
        ])
        for category, filename, content in (
            ('正式分镜', 'storyboard_第1场_客厅.md', '正式分镜内容'),
            ('提示词', '第1场提示词.md', '正式提示词内容'),
        ):
            path = f'{category}/第01场_客厅'
            status, _, body = self.get('/api/list', path=path)
            self.assertEqual(status, 200)
            self.assertIn(filename, [item['name'] for item in json.loads(body)['items']])
            status, _, body = self.get('/api/text', path=f'{path}/{filename}')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['text'], content)
        status, _, body = self.get('/api/list', path='')
        self.assertNotIn('.diagonal_memory', [item['name'] for item in json.loads(body)['items']])


if __name__ == '__main__': unittest.main()
