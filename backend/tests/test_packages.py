import io
import json
import zipfile

import pytest

from app.packages import extract, menu_path, safe_path
from app.security import phone_number, sign, verify


@pytest.mark.parametrize('path', ['../escape.html', '/root.html', 'a/../../b', 'C:/file', 'a\\b', 'a//b', './index.html', 'NUL.html', 'dir./index.html'])
def test_unsafe_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_manifest_and_assets(tmp_path):
    source = tmp_path / 'page.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'title': '报表', 'entry': 'web/start.html', 'menu': ['工作', '报表']}))
        archive.writestr('web/start.html', '<html><title>ignored</title><link href="style.css"></html>')
        archive.writestr('web/style.css', 'body{color:green}')
    info = extract(source, 'page.zip', tmp_path / 'out', {})
    assert info['entry'] == 'web/start.html'
    assert info['menus'] == ['工作', '报表']
    assert info['title'] == '报表' and info['file_count'] == 3


def test_htm_file_and_zip_entry(tmp_path):
    source = tmp_path / 'document.htm'
    source.write_text('<html><title>HTM report</title></html>', encoding='utf-8')
    info = extract(source, 'DOCUMENT.HTM', tmp_path / 'single', {})
    assert info['title'] == 'HTM report' and info['entry'] == 'index.html'
    with zipfile.ZipFile(tmp_path / 'package.zip', 'w') as archive:
        archive.writestr('site/start.htm', '<html><title>HTM entry</title></html>')
        archive.writestr('manifest.json', json.dumps({'entry': 'site/start.htm'}))
    info = extract(tmp_path / 'package.zip', 'package.zip', tmp_path / 'package', {})
    assert info['entry'] == 'site/start.htm' and info['title'] == 'HTM entry'


def test_zip_traversal_rejected(tmp_path):
    source = tmp_path / 'bad.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('../escape.html', 'bad')
    with pytest.raises(ValueError):
        extract(source, 'bad.zip', tmp_path / 'out', {})
    assert not (tmp_path / 'escape.html').exists()


def test_menu_depth_and_signed_access():
    assert menu_path([]) == ['未分类']
    assert phone_number('+86 138-0000-0001') == '13800000001'
    with pytest.raises(ValueError):
        menu_path(['1', '2', '3', '4'])
    value = sign({'tenant': 'sample'})
    assert verify(value)['tenant'] == 'sample'
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        verify(value + 'tampered')
    with pytest.raises(HTTPException):
        verify(sign({}, seconds=-1))
