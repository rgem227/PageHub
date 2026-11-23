import json
import re
import stat
import zipfile
from html import unescape
from pathlib import Path, PurePosixPath

from .config import settings


def safe_path(value: str) -> str:
    if not value or '\\' in value or ':' in value or '\x00' in value or value.startswith('/'):
        raise ValueError('文件路径不安全')
    path = PurePosixPath(value)
    if '..' in path.parts or any(p in ('', '.') for p in value.rstrip('/').split('/')):
        raise ValueError('文件路径不安全')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if any(part.endswith((' ', '.')) or part.split('.')[0].upper() in reserved for part in path.parts):
        raise ValueError('文件路径包含不受支持的系统名称')
    return str(path)


def menu_path(parts) -> list[str]:
    if not isinstance(parts, list) or len(parts) > 3:
        raise ValueError('菜单路径最多三级')
    if any(not isinstance(p, str) or not p.strip() or len(p.strip()) > 60 for p in parts):
        raise ValueError('菜单名称需为 1 至 60 个字符')
    return [p.strip() for p in parts] or ['未分类']


def extract(source: Path, filename: str, target: Path, options: dict):
    target.mkdir(parents=True, exist_ok=True)
    manifest = {}
    if filename.lower().endswith(('.html', '.htm')):
        (target / 'index.html').write_bytes(source.read_bytes())
    elif filename.lower().endswith('.zip'):
        size = 0
        names = set()
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > settings.max_zip_files:
                raise ValueError('ZIP 文件数量超出上限')
            for item in archive.infolist():
                name = safe_path(item.filename)
                if name.casefold() in names:
                    raise ValueError('ZIP 包含重复路径')
                names.add(name.casefold())
                if item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('ZIP 不允许加密或符号链接')
                if item.is_dir():
                    continue
                size += item.file_size
                if size > settings.max_expanded_mb * 1024**2:
                    raise ValueError('ZIP 解压总大小超出上限')
                if item.file_size > max(1024 * 1024, item.compress_size * 200):
                    raise ValueError('ZIP 压缩比例异常')
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                actual = 0
                with archive.open(item) as src, destination.open('wb') as dst:
                    while chunk := src.read(65536):
                        actual += len(chunk)
                        if actual > item.file_size or actual > settings.max_expanded_mb * 1024**2:
                            raise ValueError('解压内容超出声明大小')
                        dst.write(chunk)
        if (target / 'manifest.json').exists():
            if (target / 'manifest.json').stat().st_size > 65536:
                raise ValueError('manifest.json 过大')
            manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8-sig'))
            if not isinstance(manifest, dict):
                raise ValueError('manifest.json 必须是对象')
    else:
        raise ValueError('仅支持 HTML、HTM 和 ZIP 文件')
    entry = safe_path(options.get('entry_file') or manifest.get('entry') or 'index.html')
    if not entry.lower().endswith(('.html', '.htm')) or not (target / entry).is_file():
        raise ValueError('未找到 HTML 入口，请提供入口文件路径')
    html = (target / entry).read_text(encoding='utf-8-sig', errors='replace')
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    title = options.get('title') or manifest.get('title') or (unescape(match.group(1)).strip() if match else Path(filename).stem)
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError('页面标题需为 1 至 200 个字符')
    menus = menu_path(options.get('menu_path') or manifest.get('menu') or [])
    files = [p for p in target.rglob('*') if p.is_file()]
    return {'entry': entry, 'title': title.strip(), 'menus': menus,
            'size': sum(p.stat().st_size for p in files), 'file_count': len(files)}
