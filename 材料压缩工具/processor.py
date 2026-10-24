"""Offline document conversion. Source files are never modified or saved."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import warnings
from uuid import uuid4

import fitz
from PIL import Image, ImageOps

MAX_BYTES = 199_000  # Strictly below both 200,000 and 204,800 bytes.
MAX_PAGES = 30
SOURCE_EDGE = 2600
Image.MAX_IMAGE_PIXELS = 60_000_000


@dataclass
class Page:
    name: str
    data: bytes
    thumbnail: bytes
    width: int
    height: int
    source_name: str = ''
    source_id: str = ''
    page_number: int = 1
    source_total: int = 1


def rgb_image(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
        rgba = image.convert('RGBA')
        background = Image.new('RGB', rgba.size, 'white')
        background.paste(rgba, mask=rgba.getchannel('A'))
        return background
    return image.convert('RGB')


def make_page(image: Image.Image, name: str, **source) -> Page:
    image = rgb_image(image)
    image.thumbnail((SOURCE_EDGE, SOURCE_EDGE), Image.Resampling.LANCZOS)
    data = BytesIO()
    image.save(data, 'PNG')
    thumb = image.copy()
    thumb.thumbnail((240, 300), Image.Resampling.LANCZOS)
    thumbnail = BytesIO()
    thumb.save(thumbnail, 'JPEG', quality=78)
    return Page(name, data.getvalue(), thumbnail.getvalue(), *image.size, **source)


def import_document(data: bytes, name: str, remaining: int = MAX_PAGES) -> list[Page]:
    if remaining < 1:
        raise ValueError(f'一次最多导入 {MAX_PAGES} 页，请先移除部分页面。')
    source_id = uuid4().hex
    if data[:5] == b'%PDF-':
        with fitz.open(stream=data, filetype='pdf') as document:
            if document.needs_pass:
                raise ValueError('此 PDF 有密码保护，请先解除密码后导入。')
            if len(document) < 1:
                raise ValueError('PDF 没有可转换的页面。')
            if len(document) > remaining:
                raise ValueError(f'此文件有 {len(document)} 页，当前还可导入 {remaining} 页。')
            pages = []
            for index, page in enumerate(document):
                edge = max(page.rect.width, page.rect.height)
                if edge <= 0:
                    raise ValueError(f'第 {index + 1} 页尺寸异常。')
                scale = min(SOURCE_EDGE / edge, 3)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                image = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
                pages.append(make_page(image, f'{name} · 第 {index + 1} 页', source_name=name,
                                       source_id=source_id, page_number=index + 1, source_total=len(document)))
            return pages
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format not in ('JPEG', 'PNG'):
                raise ValueError('请选择 PDF、JPG / JPEG 或 PNG 文件。')
            return [make_page(image, name, source_name=name, source_id=source_id)]


def load_page(page: Page, rotation: int = 0) -> Image.Image:
    with Image.open(BytesIO(page.data)) as image:
        result = image.convert('RGB')
    if rotation % 90:
        raise ValueError('旋转角度必须是 90 度的倍数。')
    if rotation % 360:
        result = result.rotate(-rotation, expand=True)
    return result


def compose(images: list[Image.Image], layout: str) -> Image.Image:
    if not images:
        raise ValueError('请至少选择一页材料。')
    if len(images) == 1:
        return images[0].copy()
    if layout not in ('vertical', 'grid'):
        raise ValueError('未知的合并布局。')
    columns = 1 if layout == 'vertical' else 2
    cell_width = min(1800, max(image.width for image in images))
    gap = 16
    rows = []
    for offset in range(0, len(images), columns):
        row = []
        for image in images[offset:offset + columns]:
            height = max(1, round(image.height * cell_width / image.width))
            row.append(image.resize((cell_width, height), Image.Resampling.LANCZOS))
        rows.append(row)
    heights = [max(image.height for image in row) for row in rows]
    full_width = cell_width * columns + gap * (columns - 1)
    full_height = sum(heights) + gap * (len(rows) - 1)
    # Limit working allocation before creating a potentially very tall canvas.
    scale = min(1.0, math.sqrt(14_000_000 / (full_width * full_height)), 16000 / full_height)
    canvas = Image.new('RGB', (max(1, round(full_width * scale)), max(1, round(full_height * scale))), 'white')
    y = 0
    for row, row_height in zip(rows, heights):
        for column, image in enumerate(row):
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            canvas.paste(image.resize(size, Image.Resampling.LANCZOS), (round(column * (cell_width + gap) * scale), round(y * scale)))
        y += row_height + gap
    return canvas


def encode_jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, 'JPEG', quality=quality, optimize=True, progressive=True, subsampling=2)
    return buffer.getvalue()


def compress(image: Image.Image, grayscale: bool = False) -> tuple[bytes, dict]:
    image = image.convert('L' if grayscale else 'RGB')
    original = image.size
    image.thumbnail((3200, 9000), Image.Resampling.LANCZOS)
    # Prefer readable resolution at a moderate quality, then find the best quality
    # that fits. A real byte check, rather than an estimated size, gates every result.
    for _ in range(45):
        trial = encode_jpeg(image, 60)
        if len(trial) < MAX_BYTES:
            low, high = 60, 95
            best, best_quality = trial, 60
            while low <= high:
                quality = (low + high) // 2
                candidate = encode_jpeg(image, quality)
                if len(candidate) < MAX_BYTES:
                    best, best_quality, low = candidate, quality, quality + 1
                else:
                    high = quality - 1
            assert len(best) < 200_000
            return best, {'width': image.width, 'height': image.height, 'quality': best_quality,
                          'resized': image.size != original, 'bytes': len(best)}
        factor = max(0.65, min(0.9, math.sqrt(MAX_BYTES / len(trial)) * 0.96))
        size = (max(1, int(image.width * factor)), max(1, int(image.height * factor)))
        if size == image.size:
            break
        image = image.resize(size, Image.Resampling.LANCZOS)
    raise ValueError('无法在指定大小内生成图片，请减少本次合并的页数。')


def make_groups(items: list, mode: str) -> list[list]:
    if mode == 'files':
        groups = {}
        for item in items:
            groups.setdefault(item['source_id'], []).append(item)
        return list(groups.values())
    if mode == 'pages':
        return [[item] for item in items]
    if mode == 'one':
        return [items]
    if mode == 'two':
        midpoint = (len(items) + 1) // 2
        return [group for group in (items[:midpoint], items[midpoint:]) if group]
    raise ValueError('未知的输出方式。')
