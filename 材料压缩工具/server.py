"""Loopback-only UI for the offline document compressor."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import unquote, urlparse
import webbrowser
import zipfile

from processor import MAX_BYTES, MAX_PAGES, import_document, load_page, compose, compress, make_groups

ROOT = Path(__file__).resolve().parent
API_VERSION = 2


def page_info(key, page):
    return {'id': key, 'name': page.name, 'width': page.width, 'height': page.height,
            'sourceId': page.source_id, 'sourceName': page.source_name,
            'pageNumber': page.page_number, 'sourceTotal': page.source_total,
            'thumbnail': 'data:image/jpeg;base64,' + base64.b64encode(page.thumbnail).decode('ascii')}


def safe_stem(name):
    basename = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    stem = Path(basename).stem
    return safe_name(stem)


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name))[:120].strip(' .') or '报名材料'
    if name.split('.', 1)[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1, 10)], *[f'LPT{i}' for i in range(1, 10)]}:
        name = '_' + name
    return name


def unique_jpg_name(stem, used):
    name = stem + '.jpg'
    suffix = 2
    while name.casefold() in used:
        name = f'{stem} ({suffix}).jpg'
        suffix += 1
    used.add(name.casefold())
    return name


class ToolServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.pages = {}
        self.outputs = {}
        self.last_saved = None
        self.lock = threading.RLock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_bytes(self, body, mime='application/json; charset=utf-8', status=200, filename=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        if filename:
            from urllib.parse import quote
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode('utf-8'), status=status)

    def allowed(self, private=False):
        expected = f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.json({'error': '仅允许从本机地址访问。'}, 403)
            return False
        if private and not secrets.compare_digest(self.headers.get('X-Tool-Token', ''), self.server.token):
            self.json({'error': '页面会话已过期，请刷新工具页面。'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{expected}':
            self.json({'error': '访问来源不正确。'}, 403)
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        if path in ('/', '/app.js', '/style.css'):
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
            data = (ROOT / name).read_bytes()
            if path == '/':
                data = data.replace(b'__TOOL_TOKEN__', self.server.token.encode('ascii'))
            self.send_bytes(data, mime)
            return
        if path == '/favicon.ico':
            self.send_bytes(b'', 'image/x-icon', 204)
            return
        if not self.allowed(private=True):
            return
        with self.server.lock:
            if path == '/api/state':
                self.json({'version': API_VERSION,
                           'pages': [page_info(key, page) for key, page in self.server.pages.items()]})
            elif path == '/api/zip':
                if not self.server.outputs:
                    self.json({'error': '请先生成结果。'}, 400)
                    return
                buffer = BytesIO()
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
                    for output in self.server.outputs.values():
                        archive.writestr(output['name'], output['data'])
                self.send_bytes(buffer.getvalue(), 'application/zip', filename='报名材料_JPG.zip')
            elif path.startswith('/api/output/'):
                output = self.server.outputs.get(path.rsplit('/', 1)[-1])
                if not output:
                    self.json({'error': '结果已失效，请重新转换。'}, 404)
                    return
                self.send_bytes(output['data'], 'image/jpeg', filename=output['name'])
            else:
                self.json({'error': '地址不存在。'}, 404)

    def do_POST(self):
        if not self.allowed(private=True):
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = 80 * 1024 * 1024 if path == '/api/import' else 65536
            if length <= 0 or length > limit:
                self.json({'error': '单个文件需小于 80MB；请勿导入空文件。'}, 413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('文件未完整传入，请重新导入。')
            with self.server.lock:
                if path == '/api/import':
                    name = unquote(self.headers.get('X-File-Name', '材料'))[:180]
                    pages = import_document(body, name, MAX_PAGES - len(self.server.pages))
                    result = []
                    for page in pages:
                        key = secrets.token_hex(12)
                        self.server.pages[key] = page
                        result.append(page_info(key, page))
                    self.json({'pages': result})
                elif path == '/api/remove':
                    payload = json.loads(body)
                    self.server.pages.pop(payload['id'], None)
                    self.json({'ok': True})
                elif path == '/api/clear':
                    self.server.pages.clear()
                    self.server.outputs.clear()
                    self.json({'ok': True})
                elif path == '/api/shutdown':
                    self.json({'ok': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif path == '/api/save':
                    if not self.server.outputs:
                        raise ValueError('请先生成结果。')
                    folder = ROOT / '导出结果' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2))
                    folder.mkdir(parents=True, exist_ok=False)
                    for output in self.server.outputs.values():
                        (folder / output['name']).write_bytes(output['data'])
                    self.server.last_saved = folder
                    self.json({'path': str(folder), 'count': len(self.server.outputs)})
                elif path == '/api/open-folder':
                    if self.server.last_saved is None or not self.server.last_saved.is_dir():
                        raise ValueError('请先保存 JPG 到本机文件夹。')
                    os.startfile(str(self.server.last_saved))
                    self.json({'ok': True})
                elif path == '/api/convert':
                    payload = json.loads(body)
                    entries = payload.get('pages', [])
                    if not entries or len(entries) > MAX_PAGES:
                        raise ValueError('请选择 1 至 30 页材料。')
                    if len({entry['id'] for entry in entries}) != len(entries):
                        raise ValueError('页面列表存在重复，请刷新后重试。')
                    for entry in entries:
                        if entry['id'] not in self.server.pages:
                            raise ValueError('导入页面已失效，请重新导入。')
                    mode = payload.get('mode', 'files')
                    entries = [{**entry, 'source_id': self.server.pages[entry['id']].source_id} for entry in entries]
                    groups = make_groups(entries, mode)
                    source_count = len({entry['source_id'] for entry in entries})
                    first_page = self.server.pages[entries[0]['id']]
                    merge_name = str(payload.get('name', '')).strip()
                    name = safe_name(merge_name) if merge_name else (safe_stem(first_page.source_name) if source_count == 1 else '合并材料')
                    outputs = {}
                    result = []
                    used_names = set()
                    for number, group in enumerate(groups, 1):
                        images = [load_page(self.server.pages[entry['id']], int(entry.get('rotation', 0))) for entry in group]
                        merged = compose(images, payload.get('layout', 'vertical'))
                        data, info = compress(merged, bool(payload.get('grayscale', False)))
                        if len(data) >= MAX_BYTES:
                            raise ValueError('生成结果超出大小限制，请重新转换。')
                        key = secrets.token_hex(12)
                        source_page = self.server.pages[group[0]['id']]
                        if mode == 'files':
                            stem = safe_stem(source_page.source_name)
                        elif mode == 'pages':
                            stem = safe_stem(source_page.source_name)
                            if source_page.source_total > 1:
                                stem += f'_第{source_page.page_number}页'
                        else:
                            stem = name if len(groups) == 1 else f'{name}_{number:02d}'
                        filename = unique_jpg_name(stem, used_names)
                        outputs[key] = {'name': filename, 'data': data}
                        # Approximate per-page width after the final resizing, for a useful readability hint.
                        columns = 2 if payload.get('layout') == 'grid' and len(group) > 1 else 1
                        page_width = info['width'] / columns
                        result.append({'id': key, 'name': filename, **info, 'pages': len(group),
                                       'sourceId': source_page.source_id,
                                       'warning': page_width < 1050 or len(group) > 3})
                    self.server.outputs = outputs
                    self.json({'outputs': result, 'limit': MAX_BYTES, 'mode': mode, 'sourceCount': source_count})
                else:
                    self.json({'error': '地址不存在。'}, 404)
        except (ValueError, KeyError, TypeError) as error:
            self.json({'error': str(error) or '输入无效，请重试。'}, 400)
        except Exception:
            self.json({'error': '无法处理此文件。请确认文件未损坏、格式为 PDF / JPG / PNG，或减少合并页数后重试。'}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ToolServer(('127.0.0.1', args.port))
    url = f'http://127.0.0.1:{server.server_port}'
    print(url, flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.pages.clear()
        server.outputs.clear()


if __name__ == '__main__':
    main()
