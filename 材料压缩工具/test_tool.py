"""Regression checks for byte limits, rendering, ordering and the local HTTP API."""
from io import BytesIO
import json
from pathlib import Path
import random
import threading
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

import fitz
from PIL import Image, ImageDraw, ImageFont

from processor import MAX_BYTES, import_document, load_page, compose, compress, make_groups
from server import ToolServer


def image_bytes(image, kind='PNG', **kwargs):
    buffer = BytesIO()
    image.save(buffer, kind, **kwargs)
    return buffer.getvalue()


def demo_pdf():
    document = fitz.open()
    for index in range(3):
        page = document.new_page(width=595, height=842)
        page.insert_text((45, 60), f'SAMPLE MATERIAL - PAGE {index + 1}', fontsize=20)
        for line in range(32):
            page.insert_text((45, 110 + line * 18), f'{line + 1:02d}  Sample record   2026-10-24   Test only   PASS', fontsize=11)
        page.draw_rect(fitz.Rect(380, 680, 520, 770), color=(0.8, 0.1, 0.1), width=3)
        page.insert_text((400, 730), 'SAMPLE', fontsize=18, color=(0.8, 0.1, 0.1))
    data = document.tobytes()
    document.close()
    return data


class ProcessorTests(unittest.TestCase):
    def test_pdf_all_pages_and_rotation(self):
        pages = import_document(demo_pdf(), 'demo.pdf')
        self.assertEqual(len(pages), 3)
        image = load_page(pages[0], 90)
        self.assertGreater(image.width, image.height)
        for layout in ('vertical', 'grid'):
            merged = compose([load_page(page) for page in pages], layout)
            data, meta = compress(merged)
            self.assertLess(len(data), MAX_BYTES)
            with Image.open(BytesIO(data)) as result:
                self.assertEqual(result.format, 'JPEG')
                self.assertEqual(result.size, (meta['width'], meta['height']))

    def test_noisy_large_jpeg_is_strictly_below_limit(self):
        data = random.Random(42).randbytes(2400 * 3200 * 3)
        image = Image.frombytes('RGB', (2400, 3200), data)
        for gray in (False, True):
            output, meta = compress(image, gray)
            self.assertLess(len(output), 199000)
            self.assertTrue(meta['resized'])
            Image.open(BytesIO(output)).verify()

    def test_transparency_and_exif_orientation(self):
        image = Image.new('RGBA', (800, 1000), (0, 0, 0, 0))
        page = import_document(image_bytes(image), 'transparent.png')[0]
        self.assertEqual(load_page(page).getpixel((0, 0)), (255, 255, 255))
        image = Image.new('RGB', (600, 900), 'white')
        exif = Image.Exif(); exif[274] = 6
        page = import_document(image_bytes(image, 'JPEG', exif=exif), 'rotated.jpg')[0]
        self.assertEqual((page.width, page.height), (900, 600))

    def test_groups_order_and_page_limit(self):
        items = list(range(5))
        self.assertEqual(make_groups(items, 'two'), [[0, 1, 2], [3, 4]])
        self.assertEqual(make_groups(items, 'pages'), [[0], [1], [2], [3], [4]])
        with self.assertRaises(ValueError):
            import_document(demo_pdf(), 'too-many.pdf', remaining=2)

    def test_encrypted_pdf_rejected(self):
        document = fitz.open(stream=demo_pdf(), filetype='pdf')
        encrypted = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='secret', owner_pw='owner')
        document.close()
        with self.assertRaisesRegex(ValueError, '密码'):
            import_document(encrypted, 'locked.pdf')


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ToolServer(('127.0.0.1', 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self, path, body=None, headers=None):
        return urlopen(Request(self.base + path, data=body,
                              headers={'X-Tool-Token': self.server.token, **(headers or {})}), timeout=60)

    def setUp(self):
        json.load(self.request('/api/clear', b'{}'))

    def upload(self, data, name):
        return json.load(self.request('/api/import', data, {'X-File-Name': quote(name)}))['pages']

    def convert(self, pages, **options):
        payload = {'pages': [{'id': page['id'], 'rotation': 0} for page in pages], **options}
        return json.load(self.request('/api/convert', json.dumps(payload).encode()))

    def test_batch_one_output_per_source_and_original_names(self):
        pdf_pages = self.upload(demo_pdf(), '学籍报告.pdf')
        jpeg_pages = self.upload(image_bytes(Image.new('RGB', (600, 800), 'white'), 'JPEG'), '毕业证.JPG')
        png_pages = self.upload(image_bytes(Image.new('RGB', (800, 600), 'white')), '成绩单.png')
        self.assertEqual(len({page['sourceId'] for page in pdf_pages}), 1)
        self.assertEqual(pdf_pages[2]['pageNumber'], 3)
        state = json.load(self.request('/api/state'))
        self.assertEqual(state['version'], 2)
        self.assertEqual(state['pages'][0]['sourceName'], '学籍报告.pdf')
        # Interleaving page order must not mix documents or split one PDF into outputs.
        entries = [pdf_pages[0], *jpeg_pages, pdf_pages[1], *png_pages, pdf_pages[2]]
        result = self.convert(entries, name='此旧统一名称应被忽略')
        outputs = result['outputs']
        expected = ['学籍报告.jpg', '毕业证.jpg', '成绩单.jpg']
        self.assertEqual(result['sourceCount'], 3)
        self.assertEqual([output['name'] for output in outputs], expected)
        self.assertEqual([output['pages'] for output in outputs], [3, 1, 1])
        self.assertTrue(all(output['bytes'] < 199000 for output in outputs))
        with zipfile.ZipFile(BytesIO(self.request('/api/zip').read())) as archive:
            self.assertEqual(archive.namelist(), expected)
        with tempfile.TemporaryDirectory() as folder, patch('server.ROOT', Path(folder)):
            saved = json.load(self.request('/api/save', b'{}'))
            files = list(Path(saved['path']).glob('*.jpg'))
            self.assertEqual(sorted(file.name for file in files), sorted(expected))
            self.assertTrue(all(file.stat().st_size < 199000 for file in files))
        subset = self.convert([pdf_pages[1], *png_pages], mode='files')
        self.assertEqual([output['name'] for output in subset['outputs']], ['学籍报告.jpg', '成绩单.jpg'])
        self.assertEqual(len(subset['outputs']), 2)

    def test_batch_identical_names_are_unique_and_not_grouped_together(self):
        data = image_bytes(Image.new('RGB', (80, 100), 'white'))
        pages = []
        for name in ['证明.pdf.png', '证明.pdf.JPG', '证明.pdf.png', '证明.pdf (2).png', 'CON.png']:
            pages.extend(self.upload(data, name))
        result = self.convert(pages, mode='files')
        names = [output['name'] for output in result['outputs']]
        self.assertEqual(len(names), 5)
        self.assertEqual(len({name.casefold() for name in names}), 5)
        self.assertEqual(names[0], '证明.pdf.jpg')
        self.assertEqual(names[1], '证明.pdf (2).jpg')
        self.assertEqual(names[-1], '_CON.jpg')
        with zipfile.ZipFile(BytesIO(self.request('/api/zip').read())) as archive:
            self.assertEqual(len(archive.namelist()), 5)

    def test_page_mode_uses_source_names_and_actual_page_numbers(self):
        pdf_pages = self.upload(demo_pdf(), '报告.pdf')
        png_pages = self.upload(image_bytes(Image.new('RGB', (100, 80), 'white')), '证书.png')
        result = self.convert([pdf_pages[2], *png_pages, pdf_pages[0]], mode='pages', name='不应覆盖原名')
        self.assertEqual([output['name'] for output in result['outputs']], ['报告_第3页.jpg', '证书.jpg', '报告_第1页.jpg'])

    def test_api_end_to_end(self):
        response = self.request('/api/import', demo_pdf(), {'X-File-Name':quote('示例.pdf')})
        pages = json.load(response)['pages']
        self.assertEqual(len(pages), 3)
        restored = json.load(self.request('/api/state'))['pages']
        self.assertEqual(len(restored), 3)
        for mode, count in [('one', 1), ('two', 2), ('pages', 3)]:
            payload = {'pages': [{'id':page['id'], 'rotation':0} for page in reversed(pages)],
                       'mode':mode, 'layout':'grid', 'name':'示例/材料'}
            result = json.load(self.request('/api/convert', json.dumps(payload).encode()))
            self.assertEqual(len(result['outputs']), count)
            for output in result['outputs']:
                data = self.request('/api/output/' + output['id']).read()
                self.assertEqual(len(data), output['bytes'])
                self.assertLess(len(data), 199000)
                self.assertNotIn('/', output['name'])
                Image.open(BytesIO(data)).verify()
            with zipfile.ZipFile(BytesIO(self.request('/api/zip').read())) as archive:
                self.assertEqual(len(archive.namelist()), count)
                self.assertIsNone(archive.testzip())
        with tempfile.TemporaryDirectory() as folder, patch('server.ROOT', Path(folder)):
            saved = json.load(self.request('/api/save', b'{}'))
            files = list(Path(saved['path']).glob('*.jpg'))
            self.assertEqual(len(files), 3)
            self.assertTrue(all(file.stat().st_size < 199000 for file in files))
        json.load(self.request('/api/clear', b'{}'))
        self.assertEqual(len(json.load(self.request('/api/state'))['pages']), 0)

    def test_private_api_and_foreign_host_rejected(self):
        for headers, path in [({}, '/api/state'), ({'Host':'foreign.example'}, '/')]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.base + path, headers=headers), timeout=5)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/clear', b'{}', {'Origin':'https://foreign.example'})
        self.assertEqual(error.exception.code, 403)


def make_demo_files():
    target = Path(__file__).resolve().parent.parent / 'tmp' / '材料工具测试'
    target.mkdir(parents=True, exist_ok=True)
    (target / '示例材料_3页.pdf').write_bytes(demo_pdf())
    image = Image.new('RGBA', (1600, 2100), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 44)
    small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 26)
    draw.text((100, 100), '报名材料转换测试（虚构示例）', font=font, fill='#243c32')
    for line in range(32):
        draw.text((100, 230 + line * 42), f'第 {line + 1:02d} 行：用于检查压缩后文字是否清晰、内容是否完整。', font=small, fill='#283c32')
    draw.ellipse((1070, 1630, 1420, 1980), outline='#bd3535', width=8)
    draw.text((1130, 1770), '测试印章', font=font, fill='#bd3535')
    image.save(target / '透明背景_测试材料.png')
    from processor import rgb_image
    rgb_image(image).save(target / '测试材料.jpg', quality=98)
    print(target)


if __name__ == '__main__':
    import sys
    if '--demo' in sys.argv:
        make_demo_files()
    else:
        unittest.main(verbosity=2)
