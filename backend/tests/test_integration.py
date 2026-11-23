import io
import base64
import hashlib
import json
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.content import app as content_app
from app.config import settings
from app.db import pool, transaction
from app.main import app
from app.security import digest, passwords
from app.worker import process_one, process_deletion, index_manifest

NAMESPACE = 'pytest_' + uuid.uuid4().hex


@pytest.fixture(scope='module')
def clients():
    ids = []
    with TestClient(app, base_url='http://127.0.0.1', client=(NAMESPACE, 50000)) as main:
        yield main, ids
        with transaction(admin=True) as conn:
            conn.execute('DELETE FROM rate_limits WHERE key LIKE %s', ('%' + NAMESPACE + '%',))
            for tenant_id in ids:
                conn.execute('UPDATE pages SET current_version=NULL WHERE tenant_id=%s', (tenant_id,))
                conn.execute('UPDATE pages SET purge_id=NULL WHERE tenant_id=%s', (tenant_id,))
                conn.execute('UPDATE page_versions SET deletion_id=NULL WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM file_deletions WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM account_shares WHERE tenant_id=%s OR recipient_tenant_id=%s', (tenant_id, tenant_id))
                conn.execute('DELETE FROM page_shares WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM oauth_authorization_codes WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM oauth_grants WHERE tenant_id=%s', (tenant_id,))
                for table in ['bookmarks', 'bookmark_categories', 'page_versions', 'pages']:
                    conn.execute(f'DELETE FROM {table} WHERE tenant_id=%s', (tenant_id,))
                conn.execute('UPDATE menus SET name=id::text,parent_id=NULL WHERE tenant_id=%s', (tenant_id,))
                for table in ['menus', 'uploads', 'mcp_credentials', 'audit_logs']:
                    conn.execute(f'DELETE FROM {table} WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE tenant_id=%s)', (tenant_id,))
                conn.execute('DELETE FROM users WHERE tenant_id=%s', (tenant_id,))
                conn.execute('DELETE FROM tenants WHERE id=%s', (tenant_id,))
                storage.remove_prefix(f'tenants/{tenant_id}/')
                storage.remove_prefix(f'uploads/{tenant_id}/')
            conn.execute('DELETE FROM oauth_clients WHERE client_name=%s', (NAMESPACE + '_oauth',))


def account(clients, admin=False):
    main, ids = clients
    suffix = uuid.uuid4().hex[:10]
    with transaction(admin=True) as conn:
        tenant = conn.execute('INSERT INTO tenants(name) VALUES (%s) RETURNING id', ('test_' + suffix,)).fetchone()['id']
        user = conn.execute('INSERT INTO users(tenant_id,username,password_hash,role) VALUES (%s,%s,%s,%s) RETURNING id',
                            (tenant, 'test_' + suffix, passwords.hash('StrongTestPassword!'), 'superadmin' if admin else 'user')).fetchone()['id']
        ids.append(tenant)
    c = TestClient(app, base_url='http://127.0.0.1', client=(NAMESPACE + '_' + suffix, 50000))
    result = c.post('/api/auth/login', json={'account': 'test_' + suffix, 'password': 'StrongTestPassword!'})
    assert result.status_code == 200, result.text
    return c, tenant, user


def publish(client, data=b'<html><title>Test</title><h1>Hello</h1></html>', options=None, filename='test.html'):
    result = client.post('/api/uploads', files={'file': (filename, data)}, data=options or {'menu_path': '["工作","报表","月报"]'})
    assert result.status_code == 202, result.text
    upload_id = result.json()['id']
    for _ in range(30):
        process_one()
        status = client.get('/api/uploads/' + upload_id).json()
        if status['status'] not in ('queued', 'processing'):
            return status
        time.sleep(.1)
    raise AssertionError('worker 没有处理上传任务')


def finish_deletions(client, params=None):
    for _ in range(100):
        if all(job['status'] not in ('queued', 'processing') for job in client.get('/api/files/deletions', params=params).json()['items']):
            return
        process_deletion()
    raise AssertionError('worker 没有处理清理任务')


def test_account_share_is_recipient_only_and_revocable(clients):
    owner, _, _ = account(clients)
    recipient, _, _ = account(clients)
    other, _, _ = account(clients)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('site/start.html', '<html><title>内部分享</title><img src="assets/pixel.txt"></html>')
        package.writestr('site/assets/pixel.txt', 'private asset')
    published = publish(owner, archive.getvalue(), {'entry_file': 'site/start.html', 'menu_path': '["分享测试"]'}, filename='share.zip')['result']
    menu_id = owner.get('/api/menus').json()[0]['id']

    created = owner.post(f'/api/menus/{menu_id}/account-shares', json={
        'page_id': published['page_id'], 'recipient_user_id': recipient.get('/api/auth/me').json()['id'], 'days': None,
    })
    assert created.status_code == 200, created.text
    share_id = created.json()['id']
    assert recipient.get('/api/shares/received').json()[0]['expires_at'] is None
    preview = recipient.get(f'/api/shares/received/{share_id}/preview')
    assert preview.status_code == 200, preview.text
    entry_url = preview.json()['url']
    assert entry_url.endswith('/site/start.html')
    assert recipient.get(entry_url).text.find('内部分享') >= 0
    asset_url = entry_url.rsplit('/', 1)[0] + '/assets/pixel.txt'
    assert recipient.get(asset_url).text == 'private asset'
    assert other.get(entry_url).status_code == 403

    assert owner.delete(f'/api/shares/accounts/{share_id}').status_code == 200
    assert recipient.get(entry_url).status_code == 403
    assert owner.get('/api/shares').json()[0]['status'] == '已撤销'
    assert owner.delete(f'/api/shares/account/{share_id}/record').status_code == 200
    assert owner.get('/api/shares').json() == []

    second = owner.post(f'/api/menus/{menu_id}/account-shares', json={
        'page_id': published['page_id'], 'recipient_user_id': recipient.get('/api/auth/me').json()['id'], 'days': 7,
    }).json()
    assert recipient.delete(f"/api/shares/received/{second['id']}").status_code == 200
    assert recipient.get(f"/api/shares/received/{second['id']}/preview").status_code == 403


def test_share_recipient_can_be_found_by_full_phone(clients):
    sender, _, _ = account(clients)
    recipient, _, recipient_id = account(clients)
    with transaction(admin=True) as conn:
        conn.execute("UPDATE users SET phone='13812345678' WHERE id=%s", (recipient_id,))

    for number in ('13812345678', '+86 13812345678'):
        response = sender.post('/api/share-recipients', json={'q': number})
        assert response.status_code == 200, response.text
        assert [row['id'] for row in response.json()] == [str(recipient_id)]
        assert 'phone' not in response.json()[0]
    assert sender.post('/api/share-recipients', json={'q': '1381234'}).json() == []
    assert recipient.post('/api/share-recipients', json={'q': '13812345678'}).json() == []


@pytest.mark.parametrize('share_type', ['link', 'account'])
def test_deleting_active_share_only_removes_the_share(clients, share_type):
    owner, _, _ = account(clients)
    recipient, _, recipient_id = account(clients)
    page_id = publish(owner, options={'menu_path': '["Share deletion"]'})['result']['page_id']
    menu_id = owner.get('/api/menus').json()[0]['id']
    endpoint = f'/api/menus/{menu_id}/'
    body = {'page_id': page_id}
    if share_type == 'account':
        created = owner.post(endpoint + 'account-shares', json={**body, 'recipient_user_id': str(recipient_id)})
    else:
        created = owner.post(endpoint + 'shares', json=body)
    assert created.status_code == 200, created.text
    share = created.json()
    share_id = share['id']
    delete_url = f'/api/shares/{share_type}/{share_id}/record'
    content = TestClient(content_app, base_url='http://127.0.0.1')
    if share_type == 'account':
        preview_url = recipient.get(f'/api/shares/received/{share_id}/preview').json()['url']
        viewer = recipient
    else:
        preview_url, viewer = share['url'], content
    assert recipient.delete(delete_url).status_code == 404
    assert viewer.get(preview_url).status_code == 200
    assert owner.delete(delete_url).status_code == 200
    assert owner.get('/api/shares').json() == []
    assert recipient.get('/api/shares/received').json() == []
    assert owner.get(endpoint + 'shares').json()['shares'] == []
    assert viewer.get(preview_url).status_code == 403
    assert content.get(owner.get(f'/api/pages/{page_id}/preview').json()['url']).status_code == 200


def test_file_inventory_versions_permissions_and_physical_deletion(clients):
    a, tenant, _ = account(clients)
    b, _, _ = account(clients)
    admin, _, _ = account(clients, admin=True)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('index.html', '<html><title>文件测试</title></html>')
        package.writestr('assets/images/logo.txt', 'image-data')
        package.writestr('assets/css/site.css', 'body{}')
    first = publish(a, archive.getvalue(), filename='files.zip')['result']
    page_id, old_id = first['page_id'], first['version_id']
    old_capability = a.get(f'/api/pages/{page_id}/preview').json()['url']
    second = publish(a, b'<html><title>New version</title></html>', {'page_id': page_id})['result']
    current_id = second['version_id']
    inventory = a.get('/api/files').json()
    assert inventory['total'] == 2
    assert {v['version_no'] for v in inventory['items']} == {1, 2}
    assert all(v['binding'] == 'attached' for v in inventory['items'])
    manifest = a.get(f'/api/files/versions/{old_id}/entries').json()
    assert manifest['ready'] and manifest['total'] == 3
    folders = {f['path']: f['size_bytes'] for f in manifest['folders']}
    assert folders['assets'] == len(b'image-data') + len(b'body{}')
    assert folders['assets/images'] == len(b'image-data')
    assert a.get(f'/api/files/versions/{old_id}/entries', params={'path': 'assets', 'q': 'site', 'size': 1}).json()['total'] == 1
    total = first['size_bytes'] + second['size_bytes']
    assert a.get('/api/files/summary').json()['used_bytes'] == total
    assert sum(f['direct_bytes'] for f in a.get('/api/files/folders').json()) == total
    folder_rows = {f['id']: f for f in a.get('/api/files/folders').json()}
    root = folder_rows[inventory['items'][0]['menu_id']]
    while root['parent_id']:
        root = folder_rows[root['parent_id']]
    assert a.get('/api/files', params={'menu': root['id']}).json()['total'] == 2
    assert b.get('/api/files').json()['total'] == 0
    assert b.get('/api/files', params={'tenant': str(tenant)}).status_code == 403
    assert b.get('/api/files', params={'platform': True}).status_code == 403
    assert b.get(f'/api/files/versions/{old_id}/entries').status_code == 404
    assert b.post('/api/files/versions/delete', json={'ids': [old_id]}).status_code == 404
    assert admin.get('/api/files', params={'platform': True, 'tenant': str(tenant)}).json()['total'] == 2
    admin_preview = admin.get(f'/api/files/versions/{old_id}/preview', params={'platform': True, 'tenant': str(tenant)})
    assert admin_preview.status_code == 200, admin_preview.text
    assert admin_preview.json()['expires_in'] == 300
    admin_preview_url = admin_preview.json()['url']
    assert admin_preview_url.endswith('/index.html')
    assert a.get(f'/api/files/versions/{old_id}/preview', params={'platform': True, 'tenant': str(tenant)}).status_code == 403
    credential = admin.post('/api/credentials', json={'name': 'files-admin', 'readonly': False}).json()['token']
    assert admin.get('/api/files', params={'platform': True}, headers={'Authorization': 'Bearer ' + credential}).status_code == 403
    assert admin.get(f'/api/files/versions/{old_id}/preview', params={'platform': True}, headers={'Authorization': 'Bearer ' + credential}).status_code == 403
    assert a.post('/api/files/versions/delete', json={'ids': [old_id, current_id]}).status_code == 409
    assert a.get('/api/files/deletions').json()['total'] == 0
    content = TestClient(content_app, base_url='http://127.0.0.1')
    preview_path = urlsplit(admin_preview_url).path
    assert '文件测试' in content.get(preview_path).text
    assert content.get(preview_path.rsplit('/', 1)[0] + '/assets/css/site.css').text == 'body{}'
    accepted = a.post('/api/files/versions/delete', json={'ids': [old_id]})
    assert accepted.status_code == 202
    assert content.get(preview_path).status_code == 403
    assert a.get('/api/workspace').json()['storage']['used_bytes'] == total
    assert a.post(f'/api/pages/{page_id}/rollback/{old_id}').status_code == 404
    assert a.post(f'/api/files/pages/{page_id}/delete').status_code == 409
    assert content.get(old_capability).status_code == 403
    finish_deletions(a)
    assert content.get(preview_path).status_code == 403
    assert a.get('/api/workspace').json()['storage']['used_bytes'] == second['size_bytes']
    deleted = next(v for v in a.get('/api/files').json()['items'] if v['version_id'] == old_id)
    assert deleted['file_state'] == 'deleted' and deleted['size_bytes'] == 0
    assert a.get('/api/upload-list').json()['total'] == 2
    assert not storage.list_files(f'tenants/{tenant}/pages/{page_id}/versions/{old_id}/')
    assert storage.list_files(f'tenants/{tenant}/pages/{page_id}/versions/{current_id}/')
    assert a.post(f'/api/pages/{page_id}/rollback/{old_id}').status_code == 404
    third = publish(a, options={'page_id': page_id})['result']
    assert next(v for v in a.get('/api/files').json()['items'] if v['version_id'] == third['version_id'])['version_no'] == 3


def test_uploaded_sources_can_be_downloaded_individually_and_in_batches(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    one = publish(a, b'<html>one</html>', {'menu_path': '["下载测试","页面一"]'}, filename='report.html')
    two = publish(a, b'<html>two</html>', {'menu_path': '["下载测试","页面二"]'}, filename='report.html')
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as zipped:
        zipped.writestr('index.html', '<html>packaged</html>')
        zipped.writestr('assets/data.txt', 'asset')
    three = publish(a, package.getvalue(), {'menu_path': '["下载测试","页面三"]'}, filename='package.zip')
    upload_ids = [one['id'], two['id'], three['id']]
    single = a.get(f"/api/files/uploads/{upload_ids[0]}/download")
    assert single.status_code == 200 and single.content == b'<html>one</html>'
    assert 'report.html' in single.headers['content-disposition']
    archive = a.get('/api/files/download', params=[('ids', upload_id) for upload_id in upload_ids])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == ['report.html', 'report_2.html', 'package.zip']
        assert zipped.read('report.html') == b'<html>one</html>'
        assert zipped.read('report_2.html') == b'<html>two</html>'
        with zipfile.ZipFile(io.BytesIO(zipped.read('package.zip'))) as package_zip:
            assert package_zip.namelist() == ['assets/data.txt', 'index.html']
            assert package_zip.read('assets/data.txt') == b'asset'
    assert b.get(f"/api/files/uploads/{upload_ids[0]}/download").status_code == 404
    assert b.get('/api/files/download', params=[('ids', upload_ids[0])]).status_code == 404
    assert a.get('/api/files/download/check', params=[('ids', upload_ids[0])]).json()['count'] == 1


def test_bookmark_export_downloads_filtered_tenant_csv(clients):
    import csv
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    a.post('/api/bookmarks', json={'title': '清单一', 'url': 'https://example.com/one', 'category_name': '清单分类'})
    a.post('/api/bookmarks', json={'title': '清单二', 'url': 'https://example.com/two', 'category_name': '其他分类'})
    b.post('/api/bookmarks', json={'title': '其他租户', 'url': 'https://example.com/foreign'})
    category = next(row['id'] for row in a.get('/api/bookmark-categories').json() if row['name'] == '清单分类')
    response = a.get('/api/bookmarks/export', params={'category_id': category})
    assert response.status_code == 200 and 'attachment;' in response.headers['content-disposition']
    rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert rows[0] == ['名称', '网址', '分类', '备注', '打开方式', '收藏时间']
    assert len(rows) == 2 and rows[1][0] == '清单一' and rows[1][2] == '清单分类'


def test_file_cleanup_failure_retry_page_purge_and_quota(clients, monkeypatch):
    a, tenant, user_id = account(clients)
    first = publish(a)['result']
    page_id, old_id = first['page_id'], first['version_id']
    second = publish(a, options={'page_id': page_id})['result']
    before = a.get('/api/workspace').json()['storage']['used_bytes']
    original_remove = storage.remove_prefix

    def broken_remove(prefix, **kwargs):
        original_remove(prefix, **kwargs)
        raise OSError('模拟部分文件删除后中断')

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'remove_prefix', broken_remove)
        assert a.post('/api/files/versions/delete', json={'ids': [old_id]}).status_code == 202
        finish_deletions(a)
    job = a.get('/api/files/deletions').json()['items'][0]
    assert job['status'] == 'failed'
    assert a.get('/api/workspace').json()['storage']['used_bytes'] == before
    assert a.post('/api/files/deletions/' + job['id'] + '/retry').status_code == 200
    finish_deletions(a)
    assert a.get('/api/workspace').json()['storage']['used_bytes'] == second['size_bytes']
    assert a.delete('/api/pages/' + page_id).status_code == 200
    assert a.get('/api/files', params={'state': 'withdrawn'}).json()['total'] == 1
    menu_id = next(v for v in a.get('/api/files').json()['items'] if v['is_current'])['menu_id']
    result = a.post('/api/files/pages/' + page_id + '/delete')
    assert result.status_code == 202
    assert a.post('/api/files/pages/' + page_id + '/delete').json()['id'] == result.json()['id']
    assert a.post(f'/api/pages/{page_id}/rollback/{second["version_id"]}').status_code == 404
    assert a.patch('/api/pages/' + page_id, json={'title': '复活', 'menu_id': menu_id}).status_code == 404
    finish_deletions(a)
    assert a.get('/api/workspace').json()['storage']['used_bytes'] == 0
    assert a.get('/api/files/summary').json()['used_bytes'] == 0
    assert a.get('/api/files', params={'state': 'deleted'}).json()['total'] == 2
    assert a.get('/api/upload-list').json()['total'] == 2
    assert any(menu['id'] == menu_id for menu in a.get('/api/menus').json())
    assert a.delete('/api/menus/' + menu_id).status_code == 200
    with transaction(admin=True) as conn:
        assert conn.execute('SELECT count(*) AS n FROM audit_logs WHERE user_id=%s AND action=%s', (user_id, 'files.delete.complete')).fetchone()['n'] == 2
    assert not storage.list_files(f'tenants/{tenant}/pages/{page_id}/versions/{second["version_id"]}/')


def test_file_manifest_backfill_and_expired_cleanup_claim(clients, monkeypatch):
    a, tenant, _ = account(clients)
    first = publish(a)['result']
    old_id, page_id = first['version_id'], first['page_id']
    publish(a, options={'page_id': page_id})
    with transaction(tenant) as conn:
        conn.execute("UPDATE page_versions SET file_manifest=NULL,created_at='1970-01-01' WHERE id=%s", (old_id,))
    assert not a.get(f'/api/files/versions/{old_id}/entries').json()['ready']
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'list_files', lambda prefix: (_ for _ in ()).throw(OSError('扫描失败')))
        index_manifest()
    assert a.get(f'/api/files/versions/{old_id}/entries').json()['error']
    assert a.post(f'/api/files/versions/{old_id}/rescan').status_code == 200
    for _ in range(100):
        index_manifest()
        if a.get(f'/api/files/versions/{old_id}/entries').json()['ready']:
            break
    assert a.get(f'/api/files/versions/{old_id}/entries').json()['total'] == 1
    job = a.post('/api/files/versions/delete', json={'ids': [old_id]}).json()['jobs'][0]
    with transaction(tenant) as conn:
        conn.execute("UPDATE file_deletions SET status='processing',claimed_at=now()-interval '6 minutes' WHERE id=%s", (job['id'],))
    finish_deletions(a)
    assert a.get('/api/files/deletions').json()['items'][0]['status'] == 'success'


def test_platform_cleanup_snapshot_guard_and_original_uploader_quota(clients):
    a, tenant, uploader = account(clients)
    admin, new_tenant, _ = account(clients, admin=True)
    first = publish(a)['result']
    second = publish(a, options={'page_id': first['page_id']})['result']
    params = {'platform': True, 'tenant': str(tenant)}
    path = '/api/files/pages/' + first['page_id']
    assert admin.get(path + '/impact', params=params).json()['versions'] == 2
    assert admin.post(path + '/delete', params=params, json={'expected_version_ids': [first['version_id']]}).status_code == 409
    assert a.get('/api/files/deletions').json()['total'] == 0
    assert admin.post('/api/files/versions/' + first['version_id'] + '/activate', params=params).status_code == 200
    assert a.get('/api/pages/' + first['page_id']).json()['current_version'] == first['version_id']
    assert admin.patch('/api/admin/users/' + str(uploader), json={'tenant_id': str(new_tenant), 'active': True}).status_code == 200
    assert admin.post('/api/files/versions/delete', params=params, json={'ids': [second['version_id']]}).status_code == 202
    finish_deletions(admin, params)
    owner = admin.get('/api/admin/records/users', params={'entity': str(uploader)}).json()['items'][0]
    assert owner['storage_bytes'] == first['size_bytes']
    impact = admin.get(path + '/impact', params=params).json()
    assert admin.post(path + '/delete', params=params, json={'expected_version_ids': impact['version_ids']}).status_code == 202
    finish_deletions(admin, params)
    owner = admin.get('/api/admin/records/users', params={'entity': str(uploader)}).json()['items'][0]
    assert owner['storage_bytes'] == 0
    assert admin.get('/api/files/summary', params=params).json()['used_bytes'] == 0


def test_cleanup_rejects_storage_prefix_outside_version(clients):
    a, tenant, _ = account(clients)
    first = publish(a)['result']
    publish(a, options={'page_id': first['page_id']})
    prefix = f'tenants/{tenant}/pages/{first["page_id"]}/versions/{first["version_id"]}/'
    with transaction(tenant) as conn:
        conn.execute('UPDATE page_versions SET storage_prefix=%s WHERE id=%s', ('unsafe/another-folder/', first['version_id']))
    job = a.post('/api/files/versions/delete', json={'ids': [first['version_id']]}).json()['jobs'][0]
    finish_deletions(a)
    assert a.get('/api/files/deletions').json()['items'][0]['status'] == 'failed'
    assert storage.list_files(prefix)
    with transaction(tenant) as conn:
        conn.execute('UPDATE page_versions SET storage_prefix=%s WHERE id=%s', (prefix, first['version_id']))
    assert a.post('/api/files/deletions/' + job['id'] + '/retry').status_code == 200
    finish_deletions(a)
    assert not storage.list_files(prefix)


def test_catalogue_search_visibility_pagination_and_personal_view(clients):
    a, tenant, _ = account(clients)
    b, _, _ = account(clients)
    result = publish(a, b'<html><title>Search Needle</title></html>')
    assert result['status'] == 'success'
    found = a.get('/api/catalogue', params={'q': 'Needle', 'size': 1}).json()
    assert found['total'] == 1 and len(found['items']) == 1
    assert found['items'][0]['matches'][0]['id'] == result['result']['page_id']
    published = a.get('/api/pages/' + result['result']['page_id']).json()['created_at']
    assert found['items'][0]['published_at'] == published
    assert b.get('/api/catalogue').json()['total'] == 0
    assert b.get('/api/page-list').json()['total'] == 0
    assert b.get('/api/upload-list').json()['total'] == 0
    assert a.get('/api/upload-list', params={'status': 'success'}).json()['total'] == 1
    assert a.get('/api/menu-list').json()['total'] == 3
    assert b.get('/api/menu-list').json()['total'] == 0
    roots = a.get('/api/menus').json()
    root = next(m for m in roots if m['parent_id'] is None)
    assert a.get('/api/page-list', params={'menu': root['id']}).json()['total'] == 1
    assert a.patch('/api/menus/' + root['id'], json={'name': root['name'], 'visible': False}).status_code == 200
    assert a.get('/api/catalogue', params={'q': 'Needle'}).json()['total'] == 0
    a.post('/api/menus', json={'path': ['100%完成']})
    a.post('/api/menus', json={'path': ['100分完成']})
    assert a.get('/api/catalogue', params={'q': '100%'}).json()['total'] == 1
    assert a.patch('/api/preferences', json={'directory_view': 'list'}).status_code == 200
    assert a.get('/api/auth/me').json()['directory_view'] == 'list'
    assert b.get('/api/auth/me').json()['directory_view'] == 'compact'
    assert a.get('/api/catalogue', params={'size': 101}).status_code == 422
    assert a.get('/api/catalogue', params={'sort': 'invalid'}).status_code == 422
    with transaction(tenant) as conn:
        with conn.cursor() as cursor:
            cursor.executemany('INSERT INTO menus(tenant_id,name) VALUES (%s,%s)', [(tenant, f'容量验证{i:04d}') for i in range(5000)])
    last = a.get('/api/catalogue', params={'q': '容量验证', 'page': 105, 'size': 48}).json()
    assert last['total'] == 5000 and len(last['items']) == 8 and last['page'] == 105
    first = a.get('/api/catalogue', params={'q': '容量验证', 'size': 48}).json()
    assert not {m['id'] for m in first['items']} & {m['id'] for m in last['items']}
    assert a.get('/api/catalogue', params={'q': '容量验证4999'}).json()['total'] == 1
    assert a.get('/api/catalogue', params={'q': '容量验证', 'page': 999}).json()['page'] == 105
    assert a.get('/api/menu-list', params={'q': '容量验证', 'page': 100}).json()['total'] == 5000


def test_console_lists_filter_details_and_authorization(clients):
    admin, tenant, user = account(clients, admin=True)
    ordinary, other_tenant, _ = account(clients)
    published = publish(admin)
    assert published['status'] == 'success'
    users = admin.get('/api/admin/records/users', params={'tenant': tenant, 'size': 1}).json()
    assert users['total'] == 1 and users['items'][0]['id'] == str(user)
    assert 'password_hash' not in users['items'][0]
    name = users['items'][0]['username']
    assert admin.get('/api/admin/records/users', params={'q': name}).json()['total'] == 1
    jobs = admin.get('/api/admin/records/uploads', params={'entity': user, 'status': 'success'}).json()
    assert jobs['total'] == 1 and jobs['items'][0]['status'] == 'success'
    assert admin.get('/api/admin/records/tenants', params={'entity': other_tenant}).json()['total'] == 1
    assert admin.get('/api/admin/records/audit', params={'tenant': tenant, 'action': 'login'}).json()['total'] == 1
    assert admin.get('/api/admin/records/audit', params={'after': '2020-01-01T00:00:00', 'before': '2021-01-01T00:00:00Z'}).json()['total'] == 0
    assert admin.get('/api/admin/records/audit', params={'after': '2021-01-01T00:00:00', 'before': '2020-01-01T00:00:00Z'}).status_code == 422
    assert admin.get('/api/admin/overview').json()['pages'] >= 1
    assert admin.get('/api/admin/records/users', params={'status': 'failed'}).status_code == 422
    for path in ('overview', 'records/users', 'records/tenants', 'records/uploads', 'records/audit', 'tenant-options'):
        assert ordinary.get('/api/admin/' + path).status_code == 403
    token = admin.post('/api/credentials', json={'name': 'console-rejected', 'readonly': True}).json()['token']
    assert admin.get('/api/admin/overview', headers={'Authorization': 'Bearer ' + token}).status_code == 403


def test_user_activity_heartbeat_filters_and_bulk_deactivation(clients):
    admin, _, _ = account(clients, admin=True)
    online, _, online_id = account(clients)
    active_week, _, week_id = account(clients)
    inactive_month, _, month_id = account(clients)
    inactive_old, _, old_id = account(clients)
    never_active, _, never_id = account(clients)
    created_credential = inactive_month.post('/api/credentials', json={'name': 'bulk-disable'})
    assert created_credential.status_code == 200, created_credential.text
    credential_token = created_credential.json()['token']
    with transaction(admin=True) as conn:
        conn.execute("UPDATE users SET last_activity_at=now()-interval '10 days' WHERE id=%s", (online_id,))
        conn.execute("UPDATE sessions SET last_seen_at=now()-interval '10 minutes' WHERE user_id=%s", (online_id,))
        conn.execute("UPDATE users SET last_activity_at=now()-interval '3 days' WHERE id=%s", (week_id,))
        conn.execute("UPDATE users SET last_activity_at=now()-interval '10 days' WHERE id=%s", (month_id,))
        conn.execute("UPDATE users SET last_activity_at=now()-interval '31 days' WHERE id=%s", (old_id,))
        conn.execute('UPDATE users SET last_activity_at=NULL WHERE id=%s', (never_id,))
        conn.execute('DELETE FROM sessions WHERE user_id=ANY(%s)', ([week_id, month_id, old_id, never_id],))

    assert online.post('/api/auth/heartbeat').status_code == 200
    params = {'entity': str(online_id), 'activity': 'online'}
    assert admin.get('/api/admin/records/users', params=params).json()['total'] == 1
    assert admin.get('/api/admin/records/users', params={'entity': str(week_id), 'activity': 'active_7d'}).json()['total'] == 1
    assert admin.get('/api/admin/records/users', params={'entity': str(month_id), 'activity': 'inactive_8_30d'}).json()['total'] == 1
    old_row = admin.get('/api/admin/records/users', params={'entity': str(old_id), 'activity': 'inactive_30d'}).json()['items'][0]
    assert old_row['online_sessions'] == 0 and old_row['last_activity_at']
    never_row = admin.get('/api/admin/records/users', params={'entity': str(never_id), 'activity': 'never'}).json()['items'][0]
    assert never_row['last_activity_at'] is None
    assert admin.get('/api/admin/records/users', params={'activity': 'online'}).status_code == 200
    assert admin.get('/api/admin/records/uploads', params={'activity': 'online'}).status_code == 422
    assert online.get('/api/admin/user-activity').status_code == 403

    response = admin.post('/api/admin/users/deactivate', json={'user_ids': [str(month_id)]})
    assert response.status_code == 200 and response.json()['count'] == 1
    assert inactive_month.get('/api/workspace').status_code == 401
    assert rpc(inactive_month, credential_token, 'tools/list').status_code == 401
    assert admin.post('/api/admin/users/deactivate', json={'user_ids': [str(month_id)]}).status_code == 409
    assert admin.post('/api/admin/users/deactivate', json={'user_ids': [str(old_id), str(old_id)]}).status_code == 422


def test_registration_phone_unique_and_ready(clients):
    main, ids = clients
    phone = '139' + str(uuid.uuid4().int)[:8]
    name = 'test_' + uuid.uuid4().hex[:10]
    sms = main.post('/api/auth/sms', json={'phone': phone})
    assert sms.status_code == 200, sms.text
    code = sms.json()['development_code']
    missing_name = main.post('/api/auth/register', json={'username': name, 'phone': phone, 'code': code, 'password': 'StrongTestPassword!'})
    assert missing_name.status_code == 422
    blank_name = main.post('/api/auth/register', json={'username': name, 'full_name': '   ', 'phone': phone, 'code': code, 'password': 'StrongTestPassword!'})
    assert blank_name.status_code == 422
    result = main.post('/api/auth/register', json={'username': name, 'full_name': '测试用户', 'phone': phone, 'code': code, 'password': 'StrongTestPassword!'})
    assert result.status_code == 200, result.text
    assert result.json()['full_name'] == '测试用户'
    ids.append(result.json()['tenant_id'])
    assert main.get('/api/workspace').status_code == 200
    with transaction() as conn:
        conn.execute("INSERT INTO sms_codes(phone,code_hash,expires_at) VALUES (%s,%s,now()+interval '1 minute')", (phone, digest('123456')))
    duplicate = main.post('/api/auth/register', json={'username': name + '2', 'full_name': '重复用户', 'phone': '+86' + phone, 'code': '123456', 'password': 'StrongTestPassword!'})
    assert duplicate.status_code == 409
    invalid = main.post('/api/auth/register', json={'username': name + '3', 'full_name': '   ', 'phone': '', 'code': '123456', 'password': 'StrongTestPassword!'})
    assert invalid.status_code == 422


def test_tenant_rls_versions_and_content(clients):
    a, tenant_a, _ = account(clients)
    b, tenant_b, _ = account(clients)
    status = publish(a)
    assert status['status'] == 'success', status
    page_id = status['result']['page_id']
    assert len(a.get('/api/menus').json()) == 3
    assert b.get('/api/pages').json() == []
    assert b.get('/api/pages/' + page_id).status_code == 404
    with transaction(tenant_b) as conn:
        assert not conn.execute('SELECT id FROM pages WHERE id=%s', (page_id,)).fetchone()
        with pytest.raises(Exception):
            with conn.transaction():
                conn.execute('INSERT INTO menus(tenant_id,name) VALUES (%s,%s)', (tenant_a, 'forbidden'))
    with transaction() as conn:
        assert conn.execute('SELECT count(*) AS n FROM pages').fetchone()['n'] == 0
        assert not conn.execute('SELECT rolbypassrls,rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()['rolsuper']
    url = a.get('/api/pages/' + page_id + '/preview').json()['url']
    content = TestClient(content_app)
    response = content.get(url)
    assert response.status_code == 200 and '<h1>Hello</h1>' in response.text
    assert 'sandbox allow-scripts' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']
    second = publish(a, b'<html><title>Updated</title><h1>Second</h1></html>', {'page_id': page_id, 'menu_path': '["工作","报表","月报"]'})
    assert second['status'] == 'success', second
    assert len(a.get(f'/api/pages/{page_id}/versions').json()) == 2
    assert len(a.get('/api/menus').json()) == 3
    assert a.post(f'/api/pages/{page_id}/rollback/{status["result"]["version_id"]}').status_code == 200
    assert b.post(f'/api/pages/{page_id}/rollback/{status["result"]["version_id"]}').status_code == 404
    assert a.delete('/api/pages/' + page_id).status_code == 200
    assert content.get(url).status_code == 403
    assert a.get('/api/pages').json() == []


def test_zip_failure_and_quota(clients):
    a, tenant, _ = account(clients)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as z:
        z.writestr('../escape.html', 'bad')
    status = publish(a, raw.getvalue(), filename='bad.zip')
    assert status['status'] == 'failed'
    assert a.get('/api/pages').json() == []
    deep = a.post('/api/menus', json={'path': ['1', '2', '3', '4']})
    assert deep.status_code == 422
    with transaction(admin=True) as conn:
        conn.execute('UPDATE tenants SET quota_bytes=10 WHERE id=%s', (tenant,))
    result = a.post('/api/uploads', files={'file': ('test.html', b'<html>exceeds quota</html>')})
    assert result.status_code == 413


def test_pages_require_leaf_menus_and_menu_list_shows_entry_paths(clients):
    a, _, _ = account(clients)
    assert a.post('/api/menus', json={'path': ['已有分类', '已有子菜单']}).status_code == 200
    parent_upload = publish(a, options={'menu_path': '["已有分类"]'})
    assert parent_upload['status'] == 'failed'
    assert '末级菜单' in parent_upload['error']
    assert a.get('/api/pages').json() == []

    attached = publish(a, options={'menu_path': '["页面分类"]'})
    assert attached['status'] == 'success', attached
    page_id = attached['result']['page_id']
    page_menu_id = next(row['id'] for row in a.get('/api/menus').json() if row['name'] == '页面分类')
    duplicate = publish(a, options={'menu_path': '["页面分类"]'})
    assert duplicate['status'] == 'failed' and '已挂接页面' in duplicate['error']
    assert '新的菜单名称' in duplicate['error'] and '后台菜单管理' in duplicate['error']
    created_child = a.post('/api/menus', json={'path': ['页面分类', '新子菜单']})
    assert created_child.status_code == 422 and '已挂接页面' in created_child.text
    parent_id = next(row['id'] for row in a.get('/api/menus').json() if row['name'] == '已有分类')
    assert a.patch('/api/pages/' + page_id, json={'title': 'Test', 'menu_id': parent_id}).status_code == 422
    assert a.patch('/api/pages/' + page_id, json={'title': '标题已更新', 'menu_id': page_menu_id}).status_code == 200
    another = publish(a, options={'menu_path': '["另一个空闲末级"]'})
    assert another['status'] == 'success', another
    assert a.patch('/api/pages/' + another['result']['page_id'], json={
        'title': 'Test', 'menu_id': page_menu_id,
    }).status_code == 422

    menus = a.get('/api/menus').json()
    assert next(row for row in menus if row['name'] == '页面分类')['can_attach_page'] is False
    assert next(row for row in menus if row['name'] == '另一个空闲末级')['can_attach_page'] is False
    assert next(row for row in menus if row['name'] == '已有分类')['has_children'] is True
    assert next(row for row in menus if row['name'] == '已有分类')['depth'] == 1
    assert next(row for row in menus if row['name'] == '已有分类')['can_create_child'] is True
    assert next(row for row in menus if row['name'] == '页面分类')['can_create_child'] is False
    menu = next(row for row in a.get('/api/menu-list').json()['items'] if row['name'] == '页面分类')
    assert menu['attached_files'] == [{'title': '标题已更新', 'path': 'index.html'}]
    workspace_menu = next(row for row in a.get('/api/menus').json() if row['id'] == page_menu_id)
    assert workspace_menu['attached_files'] == [{'title': '标题已更新', 'path': 'index.html'}]


def test_moving_page_under_hidden_menu_requires_confirmation(clients):
    a, _, _ = account(clients)
    published = publish(a, '<html><title>隐藏目标测试</title></html>'.encode(),
                        {'menu_path': '["原位置"]'})['result']
    assert a.post('/api/menus', json={'path': ['隐藏目录', '父菜单', '子菜单']}).status_code == 200
    menus = {row['name']: row for row in a.get('/api/menus').json()}
    assert a.patch('/api/menus/' + menus['隐藏目录']['id'], json={'visible': False}).status_code == 200
    refused = a.patch('/api/pages/' + published['page_id'], json={
        'title': '隐藏目标测试', 'parent_menu_id': menus['父菜单']['id'],
    })
    assert refused.status_code == 409 and 'confirm_hidden=true' in refused.text
    assert a.get('/api/pages/' + published['page_id']).json()['menu_id'] == menus['原位置']['id']
    confirmed = a.patch('/api/pages/' + published['page_id'], json={
        'title': '隐藏目标测试', 'parent_menu_id': menus['父菜单']['id'], 'confirm_hidden': True,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert a.get('/api/pages/' + published['page_id']).json()['menu_id'] == menus['原位置']['id']
    assert next(row for row in a.get('/api/menus').json() if row['id'] == menus['原位置']['id'])['parent_id'] == menus['父菜单']['id']


def test_page_edit_moves_attached_menu_under_parent_or_binds_free_leaf(clients):
    a, _, _ = account(clients)
    page = publish(a, '<html><title>原页面</title></html>'.encode(),
                   {'menu_path': '["原位置"]'})['result']
    assert a.post('/api/menus', json={'path': ['目标根', '目标分组', '已有三级页面']}).status_code == 200
    occupied_child = publish(a, '<html><title>子页面</title></html>'.encode(),
                             {'menu_path': '["目标根","目标分组","已有三级页面"]'})['result']
    occupied_parent_page = publish(a, '<html><title>已占用节点</title></html>'.encode(),
                                   {'menu_path': '["已占用父菜单"]'})['result']
    assert a.post('/api/menus', json={'path': ['空闲末级']}).status_code == 200
    menus = {row['name']: row for row in a.get('/api/menus').json()}
    page_id = page['page_id']
    original_leaf_id = a.get('/api/pages/' + page_id).json()['menu_id']
    parent_id = menus['目标分组']['id']
    occupied_parent_id = menus['已占用父菜单']['id']
    free_leaf_id = menus['空闲末级']['id']

    unchanged = a.patch('/api/pages/' + page_id, json={'title': '标题已更新'})
    assert unchanged.status_code == 200, unchanged.text
    assert a.get('/api/pages/' + page_id).json()['menu_id'] == original_leaf_id
    blocked_parent = a.patch('/api/pages/' + page_id, json={
        'title': '标题已更新', 'parent_menu_id': occupied_parent_id,
    })
    assert blocked_parent.status_code == 409
    assert a.get('/api/pages/' + occupied_parent_page['page_id']).json()['menu_id'] == occupied_parent_id

    moved_under_parent = a.patch('/api/pages/' + page_id, json={
        'title': '标题已更新', 'parent_menu_id': parent_id,
    })
    assert moved_under_parent.status_code == 200, moved_under_parent.text
    assert a.get('/api/pages/' + page_id).json()['menu_id'] == original_leaf_id
    updated_leaf = next(row for row in a.get('/api/menus').json() if row['id'] == original_leaf_id)
    assert updated_leaf['parent_id'] == parent_id
    leaf_entry = next(row for row in a.get('/api/catalogue').json()['items'] if row['id'] == original_leaf_id)
    assert leaf_entry['path'] == '目标根 / 目标分组 / 原位置'
    assert next(row for row in a.get('/api/pages/' + occupied_child['page_id'] + '/versions').json())['version_no'] == 1

    moved_to_free_leaf = a.patch('/api/pages/' + page_id, json={
        'title': '标题已更新', 'menu_id': free_leaf_id,
    })
    assert moved_to_free_leaf.status_code == 200, moved_to_free_leaf.text
    assert a.get('/api/pages/' + page_id).json()['menu_id'] == free_leaf_id
    assert next(row for row in a.get('/api/menus').json() if row['id'] == original_leaf_id)['parent_id'] == parent_id


def test_menu_drag_swap_root_and_move_subtree_atomically(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    for path in [['来源', '报表', '页面A'], ['来源', '报表', '页面B'], ['目标', '已有分组', '已有页面']]:
        assert a.post('/api/menus', json={'path': path}).status_code == 200
    page_a = publish(a, '<html><title>页面A</title></html>'.encode(),
                     {'menu_path': '["来源","报表","页面A"]'})['result']['page_id']
    page_b = publish(a, '<html><title>页面B</title></html>'.encode(),
                     {'menu_path': '["来源","报表","页面B"]'})['result']['page_id']
    target_page = publish(a, '<html><title>已有页面</title></html>'.encode(),
                          {'menu_path': '["目标","已有分组","已有页面"]'})['result']['page_id']
    before = {row['name']: row for row in a.get('/api/menus').json()}
    source_root = before['来源']['id']
    target_root = before['目标']['id']
    branch = before['报表']['id']
    leaf_a = before['页面A']['id']
    leaf_b = before['页面B']['id']
    existing_branch = before['已有分组']['id']
    existing_leaf = before['已有页面']['id']
    flat_positions = {row['id']: row['flat_position'] for row in before.values()}
    roots_before = [row['id'] for row in a.get('/api/menus').json() if row['parent_id'] is None]

    swapped = a.post('/api/menus/' + source_root + '/move', json={'target_id': target_root, 'operation': 'swap'})
    assert swapped.status_code == 200, swapped.text
    roots_after = [row['id'] for row in a.get('/api/menus').json() if row['parent_id'] is None]
    assert roots_after == roots_before[::-1]
    assert {row['id']: row['flat_position'] for row in a.get('/api/menus').json()} == flat_positions
    rows = {row['id']: row for row in a.get('/api/menus').json()}
    assert rows[branch]['parent_id'] == source_root
    assert rows[leaf_a]['parent_id'] == branch and rows[leaf_b]['parent_id'] == branch

    moved = a.post('/api/menus/' + branch + '/move', json={'target_id': target_root, 'operation': 'into'})
    assert moved.status_code == 200, moved.text
    rows = {row['id']: row for row in a.get('/api/menus').json()}
    assert rows[branch]['parent_id'] == target_root
    assert rows[leaf_a]['parent_id'] == branch and rows[leaf_b]['parent_id'] == branch
    assert a.get('/api/pages/' + page_a).json()['menu_id'] == leaf_a
    assert a.get('/api/pages/' + page_b).json()['menu_id'] == leaf_b
    assert next(row for row in a.get('/api/catalogue').json()['items'] if row['id'] == leaf_a)['path'] == '目标 / 报表 / 页面A'

    too_deep = a.post('/api/menus/' + branch + '/move', json={'target_id': existing_branch, 'operation': 'into'})
    assert too_deep.status_code == 422 and '超过三级' in too_deep.text
    cycle = a.post('/api/menus/' + target_root + '/move', json={'target_id': branch, 'operation': 'into'})
    assert cycle.status_code == 422 and '自身或子菜单' in cycle.text
    occupied = a.post('/api/menus/' + source_root + '/move', json={'target_id': existing_leaf, 'operation': 'into'})
    assert occupied.status_code == 409 and '已直接挂接页面' in occupied.text
    assert a.get('/api/pages/' + target_page).json()['menu_id'] == existing_leaf
    assert b.post('/api/menus/' + branch + '/move', json={'target_id': target_root, 'operation': 'into'}).status_code == 404


def rpc(client, credential, method, params=None):
    return client.post('/mcp/', headers={'Authorization': 'Bearer ' + credential, 'Accept': 'application/json, text/event-stream'},
                       json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def test_mcp_real_protocol_and_scopes(clients):
    a, _, _ = account(clients)
    token = a.post('/api/credentials', json={'name': 'test'}).json()['token']
    init = rpc(a, token, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
    assert init.status_code == 200, init.text
    tools = rpc(a, token, 'tools/list')
    assert tools.status_code == 200, tools.text
    assert 'upload_zip' in [t['name'] for t in tools.json()['result']['tools']]
    upload_tools = {tool['name']: tool['description'] for tool in tools.json()['result']['tools'] if tool['name'] in ('upload_html', 'upload_zip')}
    assert all('末级菜单' in description and 'list_menus' in description for description in upload_tools.values())
    assert all('新菜单名称' in description and '后台菜单管理' in description for description in upload_tools.values())
    assert all('can_create_child=true' in description and 'menu_path_auto_extended' in description for description in upload_tools.values())
    result = rpc(a, token, 'tools/call', {'name': 'create_menu', 'arguments': {'menu_path': ['MCP', '自动菜单']}})
    assert result.status_code == 200, result.text
    assert not result.json()['result'].get('isError'), result.text
    assert len(a.get('/api/menus').json()) == 2
    readonly = a.post('/api/credentials', json={'name': 'read', 'readonly': True}).json()['token']
    allowed = rpc(a, readonly, 'tools/call', {'name': 'list_menus', 'arguments': {}})
    assert not allowed.json()['result'].get('isError'), allowed.text
    blocked = rpc(a, readonly, 'tools/call', {'name': 'create_menu', 'arguments': {'menu_path': ['blocked']}})
    assert blocked.json()['result']['isError']
    unauthenticated = a.post('/mcp/', json={})
    assert unauthenticated.status_code == 401
    assert 'resource_metadata=' in unauthenticated.headers['WWW-Authenticate']


def register_oauth_client(client):
    redirect_uri = 'http://127.0.0.1:8765/oauth/callback'
    result = client.post('/api/oauth/register', json={
        'client_name': NAMESPACE + '_oauth', 'redirect_uris': [redirect_uri],
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
    })
    assert result.status_code == 201, result.text
    return result.json(), redirect_uri


def complete_oauth_authorization(client, client_id, redirect_uri, lifetime, scope=None):
    verifier = 'A' * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = {
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri,
        'state': 'pagehub-test-state', 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'resource': settings.mcp_url,
    }
    if scope is not None:
        params['scope'] = scope
    start = client.get('/api/oauth/authorize', params=params, follow_redirects=False)
    assert start.status_code == 303 and 'oauth_request=' in start.headers['location']
    details = client.get('/api/oauth/authorize/details', params=params)
    assert details.status_code == 200, details.text
    expected_scopes = scope.split() if scope is not None else ['read', 'write']
    assert details.json()['scopes'] == expected_scopes
    approval = client.post('/api/oauth/authorize', json={**params, 'expires_in_days': lifetime})
    assert approval.status_code == 200, approval.text
    callback = urlsplit(approval.json()['redirect_uri'])
    from urllib.parse import parse_qs
    callback_params = parse_qs(callback.query)
    assert callback_params['state'] == [params['state']]
    return callback_params['code'][0], verifier


def test_mcp_credential_expirations_and_permanent_token(clients):
    client, tenant, _ = account(clients)
    default = client.post('/api/credentials', json={'name': 'default expiry'}).json()
    assert 89 <= (date.fromisoformat(default['expires_at'][:10]) - date.today()).days <= 91
    for days in (7, 30, 90, 180, 365):
        created = client.post('/api/credentials', json={'name': f'{days} days', 'expires_in_days': days})
        assert created.status_code == 200 and created.json()['expires_at']
    permanent = client.post('/api/credentials', json={'name': 'permanent', 'expires_in_days': None})
    assert permanent.status_code == 200 and permanent.json()['expires_at'] is None
    assert client.post('/api/credentials', json={'name': 'bad expiry', 'expires_in_days': 10}).status_code == 422
    assert rpc(client, permanent.json()['token'], 'tools/list').status_code == 200
    with transaction(tenant) as conn:
        conn.execute('UPDATE mcp_credentials SET expires_at=now()-interval \'1 second\' WHERE token_hash=%s',
                     (digest(permanent.json()['token']),))
    expired = rpc(client, permanent.json()['token'], 'tools/list')
    assert expired.status_code == 401 and 'WWW-Authenticate' in expired.headers


def test_oauth_pkce_refresh_rotation_and_replay_revokes_grant(clients):
    client, _, _ = account(clients)
    metadata = client.get('/.well-known/oauth-authorization-server')
    protected = client.get('/.well-known/oauth-protected-resource/mcp')
    assert metadata.status_code == 200 and metadata.json()['code_challenge_methods_supported'] == ['S256']
    assert metadata.json()['token_endpoint_auth_methods_supported'] == ['none']
    assert protected.status_code == 200 and protected.json()['resource'] == settings.mcp_url.rstrip('/')
    external = client.post('/api/oauth/register', headers={'Origin': 'https://client.example'}, json={
        'client_name': NAMESPACE + '_oauth', 'redirect_uris': ['http://evil.example/callback'],
    })
    assert external.status_code == 400 and external.json()['error'] == 'invalid_redirect_uri'
    assert external.headers['access-control-allow-origin'] == '*'
    preflight = client.options('/api/oauth/token', headers={
        'Origin': 'https://client.example', 'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type',
    })
    assert preflight.status_code == 204 and preflight.headers['access-control-allow-origin'] == '*'
    registered, redirect_uri = register_oauth_client(client)
    code, verifier = complete_oauth_authorization(client, registered['client_id'], redirect_uri, 7)
    token_result = client.post('/api/oauth/token', data={
        'grant_type': 'authorization_code', 'client_id': registered['client_id'], 'code': code,
        'redirect_uri': redirect_uri, 'code_verifier': verifier, 'resource': settings.mcp_url,
    })
    assert token_result.status_code == 200, token_result.text
    first = token_result.json()
    assert first['expires_in'] == 3600 and first['refresh_token'] and first['authorization_expires_at']
    assert first['scope'] == 'read write'
    assert rpc(client, first['access_token'], 'tools/list').status_code == 200
    assert client.get('/api/workspace', headers={'Authorization': 'Bearer ' + first['access_token']}).status_code == 401
    grant = client.get('/api/oauth/grants').json()[0]
    assert grant['client_name'] == registered['client_name'] and grant['status'] == 'active'
    rotated = client.post('/api/oauth/token', data={'grant_type': 'refresh_token', 'client_id': registered['client_id'],
                                                     'refresh_token': first['refresh_token']})
    assert rotated.status_code == 200 and rotated.json()['refresh_token'] != first['refresh_token']
    replay = client.post('/api/oauth/token', data={'grant_type': 'refresh_token', 'client_id': registered['client_id'],
                                                    'refresh_token': first['refresh_token']})
    assert replay.status_code == 400 and replay.json()['error'] == 'invalid_grant'
    assert rpc(client, rotated.json()['access_token'], 'tools/list').status_code == 401
    assert client.get('/api/oauth/grants').json()[0]['status'] == 'revoked'


def test_oauth_permanent_grant_can_be_revoked_and_isolated(clients):
    client, _, _ = account(clients)
    other, _, _ = account(clients)
    registered, redirect_uri = register_oauth_client(client)
    code, verifier = complete_oauth_authorization(client, registered['client_id'], redirect_uri, None, scope='read')
    exchanged = client.post('/api/oauth/token', data={
        'grant_type': 'authorization_code', 'client_id': registered['client_id'], 'code': code,
        'redirect_uri': redirect_uri, 'code_verifier': verifier,
    })
    assert exchanged.status_code == 200 and exchanged.json()['authorization_expires_at'] is None
    token_data = exchanged.json()
    readable = rpc(client, token_data['access_token'], 'tools/call', {'name': 'list_menus', 'arguments': {}})
    writable = rpc(client, token_data['access_token'], 'tools/call', {
        'name': 'create_menu', 'arguments': {'menu_path': ['read-only-blocked']},
    })
    assert not readable.json()['result'].get('isError')
    assert writable.json()['result']['isError']
    grants = client.get('/api/oauth/grants').json()
    assert len(grants) == 1 and grants[0]['expires_at'] is None and grants[0]['scopes'] == ['read']
    grant_id = grants[0]['id']
    assert other.get('/api/oauth/grants').json() == []
    assert other.delete('/api/oauth/grants/' + grant_id).status_code == 404
    revoked = client.delete('/api/oauth/grants/' + grant_id)
    assert revoked.status_code == 200
    assert rpc(client, token_data['access_token'], 'tools/list').status_code == 401


def test_page_card_order_is_tenant_scoped_and_persistent(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    page_ids = [publish(a, data=f'<html><title>顺序{i}</title></html>'.encode(),
                        options={'menu_path': json.dumps(['页面排序', f'页面{i}'])})['result']['page_id'] for i in range(3)]
    assert [p['id'] for p in a.get('/api/page-list', params={'size': 50}).json()['items']][:3] == list(reversed(page_ids))
    assert a.post('/api/pages/' + page_ids[0] + '/move', json={'target_id': page_ids[2]}).status_code == 200
    ordered = [p['id'] for p in a.get('/api/page-list', params={'size': 50}).json()['items']]
    assert ordered[:3] == [page_ids[0], page_ids[1], page_ids[2]]
    assert b.post('/api/pages/' + page_ids[0] + '/move', json={'target_id': page_ids[2]}).status_code == 404


def test_flat_workspace_menu_order_is_tenant_scoped_and_persistent(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    first = a.post('/api/menus', json={'path': ['平铺甲']}).json()['id']
    second = a.post('/api/menus', json={'path': ['平铺乙']}).json()['id']
    third = a.post('/api/menus', json={'path': ['平铺丙']}).json()['id']
    assert [item['id'] for item in a.get('/api/catalogue').json()['items']] == [first, second, third]
    assert a.post('/api/menus/' + third + '/flat-move', json={'target_id': first}).status_code == 200
    assert [item['id'] for item in a.get('/api/catalogue').json()['items']] == [third, second, first]
    assert b.post('/api/menus/' + first + '/flat-move', json={'target_id': third}).status_code == 404


def test_bookmark_card_order_stays_inside_category(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    ids = [a.post('/api/bookmarks', json={'title': title, 'url': f'https://example.com/{title}', 'category_name': '排序'}).json()['id']
           for title in ['A', 'B', 'C']]
    listed = a.get('/api/bookmarks').json()['items']
    assert [item['id'] for item in listed] == ids
    assert all(item['kind'] == 'bookmark' for item in listed)
    assert a.post('/api/bookmarks/' + ids[2] + '/move', json={'target_id': ids[0]}).status_code == 200
    assert [item['id'] for item in a.get('/api/bookmarks').json()['items']] == [ids[2], ids[1], ids[0]]
    foreign = b.post('/api/bookmarks', json={'title': 'foreign', 'url': 'https://example.com/foreign'}).json()
    assert a.post('/api/bookmarks/' + ids[0] + '/move', json={'target_id': foreign['id']}).status_code == 404


def test_credential_delete_removes_record_and_invalidates_token(clients):
    a, tenant, _ = account(clients)
    b, _, _ = account(clients)
    created = a.post('/api/credentials', json={'name': 'delete me'}).json()
    credential_id = created['id']
    secret = created['token']
    assert b.delete('/api/credentials/' + credential_id).status_code == 404
    assert a.delete('/api/credentials/' + credential_id).status_code == 200
    assert credential_id not in [str(item['id']) for item in a.get('/api/credentials').json()]
    assert rpc(a, secret, 'tools/list').status_code == 401
    with transaction(tenant) as conn:
        assert conn.execute("SELECT count(*) n FROM mcp_credentials WHERE id=%s", (credential_id,)).fetchone()['n'] == 0
        assert conn.execute("SELECT count(*) n FROM audit_logs WHERE action='credential.delete' AND detail->>'id'=%s", (credential_id,)).fetchone()['n'] == 1


def test_bookmark_crud_validation_duplicate_and_quota(clients):
    a, tenant, _ = account(clients)
    before = a.get('/api/workspace').json()['storage']
    payload = {'title': ' 文档入口 ', 'url': 'https://example.com', 'menu_path': ['办公', '知识', '文档'], 'description': '中文备注'}
    first = a.post('/api/bookmarks', json=payload)
    assert first.status_code == 201, first.text
    item = first.json()
    assert item['title'] == '文档入口' and item['url'] == 'https://example.com/' and item['open_mode'] == 'external'
    assert '?bookmark=' + item['id'] in item['access_url']
    assert len(a.get('/api/menus').json()) == 0
    assert item['category_name'] == '文档'
    assert a.post('/api/bookmarks', json=payload).status_code == 409
    duplicate = a.post('/api/bookmarks', json={**payload, 'allow_duplicate': True})
    assert duplicate.status_code == 201
    assert a.patch('/api/bookmarks/' + item['id'], json={**payload, 'description': '保留网址只维护备注'}).status_code == 200
    changed = a.patch('/api/bookmarks/' + item['id'], json={'title': '新名称', 'url': 'https://example.com/docs', 'category_id': item['category_id'], 'open_mode': 'embedded'})
    assert changed.status_code == 200 and changed.json()['open_mode'] == 'embedded'
    assert a.delete('/api/bookmark-categories/' + item['category_id']).status_code == 409
    assert a.get('/api/workspace').json()['storage'] == before
    for bad in ['javascript:alert(1)', 'data:text/html,hello', 'file:///c:/test', 'https://user:secret@example.com', 'https://example.com\\@other.com', 'https://example.com/\nfoo']:
        assert a.post('/api/bookmarks', json={**payload, 'url': bad}).status_code == 422, bad
    for extra in [{'title': ' '}, {'menu_path': ['1', '2', '3', '4']}, {'menu_path': [' ']}, {'open_mode': 'auto'}, {'url': 'https://example.com/' + 'a' * 2050}]:
        assert a.post('/api/bookmarks', json={**payload, **extra}).status_code == 422
    assert a.post('/api/bookmarks', json={**payload, 'category_name': '另一个分类'}).status_code == 422
    assert a.post('/api/bookmarks/batch-delete', json={'ids': [item['id'], duplicate.json()['id']]}).json()['deleted'] == 2
    assert a.get('/api/bookmarks/' + item['id']).status_code == 404
    with transaction(tenant) as conn:
        assert conn.execute("SELECT count(*) n FROM audit_logs WHERE action LIKE 'bookmark.%'").fetchone()['n'] == 5


def test_bookmark_categories_rls_and_page_menu_independence(clients):
    a, tenant, _ = account(clients)
    b, other, _ = account(clients)
    data = {'title': 'Favorite Needle', 'url': 'https://example.com/tools', 'category_name': '业务工具'}
    own = a.post('/api/bookmarks', json=data).json()
    foreign = b.post('/api/bookmarks', json=data).json()
    assert b.get('/api/bookmarks/' + own['id']).status_code == 404
    assert b.patch('/api/bookmarks/' + own['id'], json=data).status_code == 404
    assert a.post('/api/bookmark-categories', json={'name': '跨租户分类'}).status_code == 201
    assert a.post('/api/bookmarks/batch-delete', json={'ids': [own['id'], foreign['id']]}).status_code == 404
    assert a.get('/api/bookmarks/' + own['id']).status_code == 200
    assert a.post('/api/bookmarks/batch-move', json={'ids': [own['id']], 'category_id': foreign['category_id']}).status_code == 404
    with transaction(other) as conn:
        assert not conn.execute('SELECT id FROM bookmarks WHERE id=%s', (own['id'],)).fetchone()
    assert a.get('/api/bookmark-categories').json()[0]['name'] == '业务工具'
    assert not a.get('/api/bookmark-categories').json()[0].get('parent_id')
    assert a.get('/api/menus').json() == []
    empty = a.post('/api/bookmark-categories', json={'name': '空分类'}).json()
    assert empty['id'] not in [c['id'] for c in a.get('/api/bookmark-categories').json()]
    assert empty['id'] in [c['id'] for c in a.get('/api/bookmark-categories?include_empty=true').json()]
    publish(a, options={'menu_path': json.dumps(['演示中心', '报告'])})
    assert a.get('/api/menus').json()
    assert [c['name'] for c in a.get('/api/bookmark-categories').json()] == ['业务工具']
    assert a.get('/api/bookmarks').json()['total'] == 1
    target = a.post('/api/bookmark-categories', json={'name': '收藏新分类'}).json()['id']
    assert a.post('/api/bookmarks/batch-move', json={'ids': [own['id'], foreign['id']], 'category_id': target}).status_code == 404
    assert a.get('/api/bookmarks/' + own['id']).json()['category_id'] == own['category_id']
    assert a.post('/api/bookmarks/batch-move', json={'ids': [own['id']], 'category_id': target}).json()['moved'] == 1
    assert a.get('/api/bookmarks/' + own['id']).json()['category_id'] == target


def test_bookmark_mcp_tools_pagination_and_scopes(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    credential = a.post('/api/credentials', json={'name': 'bookmarks'}).json()['token']
    readonly = a.post('/api/credentials', json={'name': 'read bookmarks', 'readonly': True}).json()['token']
    def call(name, arguments, secret=credential):
        return rpc(a, secret, 'tools/call', {'name': name, 'arguments': arguments}).json()['result']
    result = call('create_bookmark', {'title': 'MCP 网站', 'url': 'https://example.com', 'menu_path': ['MCP', '收藏', '文档']})
    assert not result.get('isError'), result
    item = json.loads(result['content'][0]['text'])
    assert a.get('/api/menus').json() == []
    assert item['category_name'] == '文档'
    changed = call('update_bookmark', {'bookmark_id': item['id'], 'description': '100% 标记', 'open_mode': 'embedded'})
    assert not changed.get('isError'), changed
    assert a.get('/api/bookmarks/' + item['id']).json()['description'] == '100% 标记'
    for name, args in [('create_bookmark', {'title': 'blocked', 'url': 'https://example.com'}), ('update_bookmark', {'bookmark_id': item['id'], 'title': 'blocked'}), ('delete_bookmark', {'bookmark_id': item['id']})]:
        assert call(name, args, readonly)['isError']
    assert not call('get_bookmark', {'bookmark_id': item['id']}, readonly).get('isError')
    assert a.post('/api/bookmarks/batch-delete', headers={'Authorization': 'Bearer ' + readonly}, json={'ids': [item['id']]}).status_code == 403
    found = call('search_bookmarks', {'query': '100%', 'page_size': 1}, readonly)
    assert json.loads(found['content'][0]['text'])['total'] == 1
    assert b.get('/api/bookmarks', params={'q': 'MCP 网站'}).json()['total'] == 0
    for args in [{'page_size': 101}, {'page': 0}, {'query': 'a' * 201}]:
        assert call('search_bookmarks', args)['isError']
    for i in range(3):
        assert a.post('/api/bookmarks', json={'title': f'分页{i}', 'url': f'https://example.com/{i}'}).status_code == 201
    second = a.get('/api/bookmarks', params={'q': '分页', 'size': 2, 'page': 2}).json()
    assert second['total'] == 3 and len(second['items']) == 1
    assert a.get('/api/bookmarks', params={'size': 101}).status_code == 422
    foreign = b.post('/api/bookmarks', json={'title': '别人的收藏', 'url': 'https://example.com'}).json()
    assert call('get_bookmark', {'bookmark_id': foreign['id']})['isError']
    assert call('delete_bookmark', {'bookmark_id': foreign['id']})['isError']
    assert not call('delete_bookmark', {'bookmark_id': item['id']}).get('isError')


def test_mcp_zip_upload_and_result(clients):
    a, _, _ = account(clients)
    credential = a.post('/api/credentials', json={'name': 'upload'}).json()['token']
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('index.html', '<html><title>MCP zip</title></html>')
        archive.writestr('manifest.json', json.dumps({'menu': ['MCP ZIP', '自动发布']}))
    response = rpc(a, credential, 'tools/call', {'name': 'upload_zip', 'arguments': {'zip_base64': base64.b64encode(raw.getvalue()).decode()}})
    body = response.json()['result']
    assert not body.get('isError'), response.text
    result = json.loads(body['content'][0]['text'])
    status = None
    for _ in range(30):
        process_one()
        status = a.get('/api/uploads/' + result['id']).json()
        if status['status'] not in ('queued', 'processing'):
            break
        time.sleep(.1)
    assert status['status'] == 'success', status
    assert status['result']['menu_path'] == ['MCP ZIP', '自动发布']
    menu_result = rpc(a, credential, 'tools/call', {'name': 'list_menus'}).json()['result']
    menus = [json.loads(item['text']) for item in menu_result['content']]
    parent = next(menu for menu in menus if menu['name'] == 'MCP ZIP')
    occupied = next(menu for menu in menus if menu['name'] == '自动发布')
    assert parent['has_children'] and not parent['can_attach_page']
    assert not occupied['has_children'] and not occupied['can_attach_page']

    blocked = rpc(a, credential, 'tools/call', {'name': 'upload_html', 'arguments': {
        'html': '<html><title>占用菜单测试</title></html>', 'menu_path': ['MCP ZIP', '自动发布'],
    }}).json()['result']
    blocked_job = json.loads(blocked['content'][0]['text'])
    for _ in range(30):
        process_one()
        failed = a.get('/api/uploads/' + blocked_job['id']).json()
        if failed['status'] not in ('queued', 'processing'):
            break
    status = rpc(a, credential, 'tools/call', {'name': 'get_upload_status', 'arguments': {'upload_id': blocked_job['id']}}).json()['result']
    details = json.loads(status['content'][0]['text'])
    assert details['status'] == 'failed'
    assert '新的菜单名称' in details['error'] and '后台菜单管理' in details['error']


def test_mcp_upload_auto_creates_child_beneath_parent(clients):
    a, _, _ = account(clients)
    credential = a.post('/api/credentials', json={'name': 'auto menu'}).json()['token']
    parent_path = ['自动菜单演示', '页面分类']
    seed = publish(a, '<html><title>已有三级页面</title></html>'.encode(),
                   {'menu_path': json.dumps([*parent_path, '已有三级菜单'])})
    assert seed['status'] == 'success', seed

    menus = a.get('/api/menus').json()
    parent = next(row for row in menus if row['name'] == parent_path[-1])
    assert parent['depth'] == 2 and parent['has_children']
    assert not parent['can_attach_page'] and parent['can_create_child']

    def upload_and_wait(tool, arguments):
        response = rpc(a, credential, 'tools/call', {'name': tool, 'arguments': arguments})
        body = response.json()['result']
        assert not body.get('isError'), response.text
        job = json.loads(body['content'][0]['text'])
        for _ in range(30):
            process_one()
            status = a.get('/api/uploads/' + job['id']).json()
            if status['status'] not in ('queued', 'processing'):
                return status
            time.sleep(.1)
        raise AssertionError('worker 没有处理 MCP 上传任务')

    first = upload_and_wait('upload_html', {
        'html': '<html><title>自动创建的周报</title></html>',
        'menu_path': parent_path,
    })
    assert first['status'] == 'success', first
    assert first['result']['menu_path'] == [*parent_path, '自动创建的周报']
    assert first['result']['menu_path_auto_extended'] is True

    listed = rpc(a, credential, 'tools/call', {'name': 'list_menus'}).json()['result']
    listed = [json.loads(item['text']) for item in listed['content']]
    attached = next(row for row in listed if row['name'] == '自动创建的周报')
    assert attached['parent_id'] == parent['id'] and attached['depth'] == 3
    assert not attached['can_attach_page'] and not attached['can_create_child']

    collision = upload_and_wait('upload_html', {
        'html': '<html><title>自动创建的周报</title></html>',
        'menu_path': parent_path,
    })
    assert collision['status'] == 'success', collision
    assert collision['result']['menu_path'] == [*parent_path, '自动创建的周报（2）']

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('index.html', '<html><title>ZIP季度汇报</title></html>')
    zipped = upload_and_wait('upload_zip', {
        'zip_base64': base64.b64encode(raw.getvalue()).decode(),
        'menu_path': parent_path,
    })
    assert zipped['status'] == 'success', zipped
    assert zipped['result']['menu_path'] == [*parent_path, 'ZIP季度汇报']
    assert zipped['result']['menu_path_auto_extended'] is True


def test_htm_upload_and_mcp_paged_search_filename_isolation(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    first = publish(a, b'<html><title>HTM upload</title></html>', filename='report.htm')
    assert first['status'] == 'success'
    credential = a.post('/api/credentials', json={'name': 'htm upload'}).json()['token']
    response = rpc(a, credential, 'tools/call', {'name': 'upload_html', 'arguments': {
        'html': '<html><title>MCP HTM</title></html>', 'filename': 'mcp-report.htm', 'menu_path': ['MCP HTM', '报告'],
    }})
    result = response.json()['result']
    assert not result.get('isError'), result
    job = json.loads(result['content'][0]['text'])
    for _ in range(30):
        process_one()
        status = a.get('/api/uploads/' + job['id']).json()
        if status['status'] not in ('queued', 'processing'):
            break
    assert status['status'] == 'success' and status['filename'] == 'mcp-report.htm'
    readonly = a.post('/api/credentials', json={'name': 'search', 'readonly': True}).json()['token']
    body = rpc(a, readonly, 'tools/call', {'name': 'search_pages', 'arguments': {'query': 'HTM', 'page_size': 1}}).json()['result']
    result = json.loads(body['content'][0]['text'])
    assert result['total'] == 2 and len(result['items']) == 1
    assert rpc(a, readonly, 'tools/call', {'name': 'search_pages', 'arguments': {'page_size': 101}}).json()['result']['isError']
    assert rpc(a, readonly, 'tools/call', {'name': 'upload_html', 'arguments': {'html': '<html></html>'}}).json()['result']['isError']
    other = b.post('/api/credentials', json={'name': 'other tenant'}).json()['token']
    body = rpc(b, other, 'tools/call', {'name': 'search_pages', 'arguments': {'query': 'HTM'}}).json()['result']
    assert json.loads(body['content'][0]['text'])['total'] == 0
    assert rpc(b, other, 'tools/call', {'name': 'get_upload_status', 'arguments': {'upload_id': job['id']}}).json()['result']['isError']


def test_forced_password_change_and_credential_revocation(clients):
    a, tenant, user_id = account(clients)
    credential = a.post('/api/credentials', json={'name': 'old'}).json()['token']
    with transaction(admin=True) as conn:
        conn.execute('UPDATE users SET must_change_password=true WHERE id=%s', (user_id,))
    assert a.get('/api/workspace').status_code == 403
    assert a.get('/api/auth/me').json()['must_change_password']
    result = a.post('/api/auth/password', json={'old_password': 'StrongTestPassword!', 'new_password': 'ChangedTestPassword!'})
    assert result.status_code == 200, result.text
    assert a.get('/api/workspace').status_code == 200
    assert rpc(a, credential, 'tools/list').status_code == 401


def test_menu_move_rename_order_and_bounds(clients):
    a, tenant, _ = account(clients)
    b, _, _ = account(clients)
    assert a.post('/api/menus', json={'path': ['原分类', '报表', '月报']}).status_code == 200
    target = a.post('/api/menus', json={'path': ['目标分类']}).json()['id']
    all_menus = {m['name']: m for m in a.get('/api/menus').json()}
    root, branch, leaf = (all_menus[name]['id'] for name in ['原分类', '报表', '月报'])
    status = publish(a, options={'menu_path': '["原分类","报表","月报"]'})
    assert status['status'] == 'success'
    page_id = status['result']['page_id']
    moved = a.patch('/api/menus/' + branch, json={'name': '新报表', 'parent_id': target, 'position': -5, 'visible': True})
    assert moved.status_code == 200, moved.text
    rows = a.get('/api/menus').json()
    updated = next(m for m in rows if m['id'] == branch)
    assert updated['name'] == '新报表' and updated['parent_id'] == target and updated['position'] == -5
    assert a.get('/api/pages/' + page_id).json()['menu_id'] == leaf
    assert next(m for m in rows if m['id'] == leaf)['parent_id'] == branch
    cycle = a.patch('/api/menus/' + target, json={'name': '目标分类', 'parent_id': leaf})
    assert cycle.status_code == 422 and '自身或子菜单' in cycle.text
    too_deep = a.patch('/api/menus/' + branch, json={'name': '新报表', 'parent_id': leaf})
    assert too_deep.status_code == 422
    other_branch = a.post('/api/menus', json={'path': ['独立分类', '第二级']}).json()['id']
    too_deep = a.patch('/api/menus/' + branch, json={'name': '新报表', 'parent_id': other_branch})
    assert too_deep.status_code == 422 and '超过三级' in too_deep.text
    foreign_parent = b.post('/api/menus', json={'path': ['其他租户']}).json()['id']
    assert a.patch('/api/menus/' + root, json={'name': '原分类', 'parent_id': foreign_parent}).status_code == 404
    assert b.patch('/api/menus/' + root, json={'name': '非法修改'}).status_code == 404
    assert a.patch('/api/menus/' + branch, json={'name': '一级报表', 'parent_id': None}).status_code == 200
    # 旧调用方未提供 parent_id 时，保留现有归属。
    assert a.patch('/api/menus/' + leaf, json={'name': '改名后的月报', 'position': 7}).status_code == 200
    assert next(m for m in a.get('/api/menus').json() if m['id'] == leaf)['parent_id'] == branch
    assert a.patch('/api/menus/' + leaf, json={'name': '改名后的月报', 'position': 2147483648}).status_code == 422
    duplicate = a.post('/api/menus', json={'path': ['另一个根菜单']}).json()['id']
    assert a.patch('/api/menus/' + duplicate, json={'name': '一级报表', 'parent_id': None}).status_code == 409


def test_moving_second_level_menu_keeps_descendant_pages_discoverable(clients):
    a, _, _ = account(clients)
    for path in [['来源', '报表', '月报A'], ['来源', '报表', '月报B'], ['目标']]:
        assert a.post('/api/menus', json={'path': path}).status_code == 200
    page_a = publish(a, '<html><title>月报A</title></html>'.encode(),
                     {'menu_path': '["来源","报表","月报A"]'})['result']['page_id']
    page_b = publish(a, '<html><title>月报B</title></html>'.encode(),
                     {'menu_path': '["来源","报表","月报B"]'})['result']['page_id']
    menus = {row['name']: row for row in a.get('/api/menus').json()}
    branch_id, target_id = menus['报表']['id'], menus['目标']['id']

    assert a.patch('/api/menus/' + branch_id, json={'position': 17, 'visible': False}).status_code == 200
    assert a.patch('/api/menus/' + branch_id, json={'name': '报表组'}).status_code == 200
    branch = next(row for row in a.get('/api/menus').json() if row['id'] == branch_id)
    assert branch['position'] == 17 and branch['visible'] is False
    assert a.patch('/api/menus/' + branch_id, json={'visible': True}).status_code == 200
    moved = a.patch('/api/menus/' + branch_id, json={'parent_id': target_id})
    assert moved.status_code == 200, moved.text
    branch = next(row for row in a.get('/api/menus').json() if row['id'] == branch_id)
    assert branch['name'] == '报表组' and branch['position'] == 17 and branch['visible'] is True

    catalogue = a.get('/api/catalogue').json()['items']
    entries = {row['id']: row for row in catalogue}
    assert entries[menus['月报A']['id']]['path'] == '目标 / 报表组 / 月报A'
    assert entries[menus['月报B']['id']]['path'] == '目标 / 报表组 / 月报B'
    filtered_pages = a.get('/api/page-list', params={'menu': target_id}).json()['items']
    assert {row['id'] for row in filtered_pages} == {page_a, page_b}
    menu_rows = {row['id']: row for row in a.get('/api/menu-list').json()['items']}
    assert menu_rows[menus['月报A']['id']]['path'] == '目标 / 报表组 / 月报A'
    assert menu_rows[menus['月报B']['id']]['path'] == '目标 / 报表组 / 月报B'
    assert a.get('/api/pages/' + page_a).json()['menu_id'] == menus['月报A']['id']
    assert a.get('/api/pages/' + page_b).json()['menu_id'] == menus['月报B']['id']
    for page_id in (page_a, page_b):
        assert len(a.get('/api/pages/' + page_id + '/versions').json()) == 1

    hidden_target_id = a.post('/api/menus', json={'path': ['隐藏目标']}).json()['id']
    assert a.patch('/api/menus/' + hidden_target_id, json={'visible': False}).status_code == 200
    refused = a.patch('/api/menus/' + branch_id, json={'parent_id': hidden_target_id})
    assert refused.status_code == 409 and 'confirm_hidden=true' in refused.text
    assert next(row for row in a.get('/api/menus').json() if row['id'] == branch_id)['parent_id'] == target_id
    confirmed = a.patch('/api/menus/' + branch_id, json={'parent_id': hidden_target_id, 'confirm_hidden': True})
    assert confirmed.status_code == 200, confirmed.text
    assert {row['id'] for row in a.get('/api/page-list', params={'menu': hidden_target_id}).json()['items']} == {page_a, page_b}
    assert not any(row['id'] in {menus['月报A']['id'], menus['月报B']['id']} for row in a.get('/api/catalogue').json()['items'])


def test_page_attached_menu_nodes_cannot_become_parents_until_page_is_moved(clients):
    a, _, _ = account(clients)
    a.post('/api/menus', json={'path': ['技术资料', '技术文档']})
    a.post('/api/menus', json={'path': ['工单分类', '客户服务工单台账']})
    parent_page = publish(a, '<html><title>接口接入使用手册</title></html>'.encode(),
                          {'menu_path': '["技术资料","技术文档"]'})
    moving_page = publish(a, '<html><title>客户服务工单台账</title></html>'.encode(),
                          {'menu_path': '["工单分类","客户服务工单台账"]'})
    menus = a.get('/api/menus').json()
    target = next(m for m in menus if m['name'] == '技术文档')
    source = next(m for m in menus if m['name'] == '客户服务工单台账')

    refused = a.patch('/api/menus/' + source['id'], json={
        'name': source['name'], 'parent_id': target['id'], 'position': 0, 'visible': True,
    })
    assert refused.status_code == 409 and '先将页面移至其他空闲末级菜单' in refused.text
    assert a.get('/api/pages/' + parent_page['result']['page_id']).json()['menu_id'] == target['id']

    free_menu = a.post('/api/menus', json={'path': ['文档页面的新位置']}).json()['id']
    reassigned = a.patch('/api/pages/' + parent_page['result']['page_id'], json={
        'title': '接口接入使用手册', 'menu_id': free_menu,
    })
    assert reassigned.status_code == 200, reassigned.text
    moved = a.patch('/api/menus/' + source['id'], json={
        'name': source['name'], 'parent_id': target['id'], 'position': 0,
        'visible': True,
    })
    assert moved.status_code == 200, moved.text
    rows = a.get('/api/menus').json()
    target = next(m for m in rows if m['id'] == target['id'])
    source = next(m for m in rows if m['id'] == source['id'])
    assert source['parent_id'] == target['id'] and source['name'] == '客户服务工单台账'
    assert target['page_count'] == 0 and target['has_children'] and target['can_attach_page'] is False
    assert a.get('/api/pages/' + parent_page['result']['page_id']).json()['menu_id'] == free_menu
    assert a.get('/api/pages/' + moving_page['result']['page_id']).json()['menu_id'] == source['id']

    a.post('/api/menus', json={'path': ['已挂页面的一级菜单']})
    root_page = publish(a, '<html><title>一级节点页面</title></html>'.encode(),
                        {'menu_path': '["已挂页面的一级菜单"]'})
    second_source = a.post('/api/menus', json={'path': ['另一个分类', '待移动菜单']}).json()['id']
    root_target = next(m for m in a.get('/api/menus').json() if m['name'] == '已挂页面的一级菜单')
    blocked = a.patch('/api/menus/' + second_source, json={
        'name': '待移动菜单', 'parent_id': root_target['id'], 'position': 0, 'visible': True,
    })
    assert blocked.status_code == 409
    assert a.get('/api/pages/' + root_page['result']['page_id']).json()['menu_id'] == root_target['id']


def test_validation_messages_are_chinese_and_do_not_echo_inputs(clients):
    a, _, _ = account(clients)
    result = a.post('/api/credentials', json={'name': ''})
    assert result.status_code == 422
    assert result.json()['detail'][0]['msg'] == '凭证名称不能为空'
    result = a.post('/api/auth/register', json={'username': 'ab', 'password': 'secret', 'code': 'bad'})
    assert result.status_code == 422
    errors = result.json()['detail']
    assert any(e['msg'] == '手机号不能为空' for e in errors)
    assert any('密码至少需要 10 个字符' == e['msg'] for e in errors)
    assert all('input' not in e for e in errors)
    assert 'secret' not in result.text and 'String should' not in result.text


def test_admin_permissions_stop_and_reassign(clients):
    admin, _, _ = account(clients, admin=True)
    a, tenant, user_id = account(clients)
    b, target, _ = account(clients)
    assert a.get('/api/admin/tenants').status_code == 403
    assert admin.get('/api/admin/users').status_code == 200
    assert admin.get('/api/admin/resources').status_code == 200
    assert admin.patch('/api/admin/tenants/' + str(tenant), json={'name': 'test_paused', 'quota_mb': 1, 'active': False}).status_code == 200
    assert a.get('/api/workspace').status_code == 401
    assert admin.patch('/api/admin/tenants/' + str(tenant), json={'name': 'test_restored', 'quota_mb': 1, 'active': True}).status_code == 200
    assert a.get('/api/workspace').status_code == 200
    assert admin.patch('/api/admin/users/' + str(user_id), json={'tenant_id': str(target), 'active': True}).status_code == 200
    assert a.get('/api/workspace').status_code == 401
    with transaction(admin=True) as conn:
        assert conn.execute('SELECT tenant_id FROM users WHERE id=%s', (user_id,)).fetchone()['tenant_id'] == target
    assert a.post('/api/preferences', headers={'Origin': 'https://attacker.example'}, json={}).status_code == 403


def test_admin_lists_include_contact_details_and_failed_upload_reason(clients):
    admin, _, _ = account(clients, admin=True)
    user, tenant, user_id = account(clients)
    username = user.get('/api/auth/me').json()['username']
    phone = '13612345678'
    with transaction(admin=True) as conn:
        conn.execute('UPDATE users SET full_name=%s,phone=%s WHERE id=%s', ('列表测试姓名', phone, user_id))
        conn.execute('INSERT INTO audit_logs (tenant_id,user_id,action,detail) VALUES (%s,%s,%s,%s)',
                     (tenant, user_id, 'test.contact', '{}'))

    users = admin.get('/api/admin/records/users', params={'q': phone}).json()['items']
    assert any(row['full_name'] == '列表测试姓名' and row['phone'] == phone for row in users)
    failed = publish(user, b'not a zip archive', filename='broken-contact.zip')
    assert failed['status'] == 'failed'
    uploads = admin.get('/api/admin/records/uploads', params={'status': 'failed', 'q': phone}).json()['items']
    assert any(row['username'] == username and row['full_name'] == '列表测试姓名'
               and row['phone'] == phone and row['error'] for row in uploads)
    audits = admin.get('/api/admin/records/audit', params={'q': phone}).json()['items']
    assert any(row['username'] == username and row['full_name'] == '列表测试姓名' and row['phone'] == phone
               for row in audits)


def test_account_quota_update_enforcement_and_history(clients):
    admin, _, _ = account(clients, admin=True)
    a, tenant, user_id = account(clients)
    initial = a.get('/api/workspace').json()['storage']
    assert initial == {'quota_bytes': 1024**3, 'used_bytes': 0, 'remaining_bytes': 1024**3}
    assert a.patch('/api/admin/users/' + str(user_id), json={'tenant_id': str(tenant), 'quota_mb': 5}).status_code == 403
    credential = a.post('/api/credentials', json={'name': 'quota test'}).json()['token']
    changed = admin.patch('/api/admin/users/' + str(user_id), json={'tenant_id': str(tenant), 'quota_mb': 1})
    assert changed.status_code == 200, changed.text
    assert a.get('/api/workspace').json()['storage']['quota_bytes'] == 1024**2
    assert rpc(a, credential, 'tools/list').status_code == 200
    assert a.post('/api/uploads', files={'file': ('large.html', b'x' * (1024**2 + 1))}).status_code == 413
    with transaction(admin=True) as conn:
        conn.execute('UPDATE tenants SET quota_bytes=%s WHERE id=%s', (10 * 1024**2, tenant))
    import os
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('index.html', '<html>large asset</html>')
        archive.writestr('asset.bin', os.urandom(12000) * 100)
    result = publish(a, archive_bytes.getvalue(), filename='expanded.zip')
    assert result['status'] == 'failed' and '账号剩余存储配额' in result['error']
    result = publish(a)
    assert result['status'] == 'success', result
    used = a.get('/api/workspace').json()['storage']
    assert used['used_bytes'] > 0 and used['remaining_bytes'] == 1024**2 - used['used_bytes']
    assert a.delete('/api/pages/' + result['result']['page_id']).status_code == 200
    assert a.get('/api/workspace').json()['storage'] == used
    assert admin.patch('/api/admin/users/' + str(user_id), json={'tenant_id': str(tenant), 'quota_mb': 0}).status_code == 422
    b, target, _ = account(clients)
    assert admin.patch('/api/admin/users/' + str(user_id), json={'tenant_id': str(target), 'quota_mb': 2}).status_code == 200
    with transaction(admin=True) as conn:
        username = conn.execute('SELECT username FROM users WHERE id=%s', (user_id,)).fetchone()['username']
    assert a.post('/api/auth/login', json={'account': username, 'password': 'StrongTestPassword!'}).status_code == 200
    moved = a.get('/api/workspace').json()['storage']
    assert moved['used_bytes'] == used['used_bytes'] and moved['quota_bytes'] == 2 * 1024**2
    assert b.get('/api/workspace').json()['storage']['used_bytes'] == 0
    assert a.get('/api/pages').json() == []


def test_mcp_configuration_real_ip_and_protocol(clients):
    from urllib.parse import urlparse
    a, _, _ = account(clients)
    endpoint = a.get('/api/mcp/config')
    assert endpoint.status_code == 200
    url = endpoint.json()['url']
    assert endpoint.json()['authorization_server_metadata_url'].endswith('/.well-known/oauth-authorization-server')
    assert endpoint.json()['token_endpoint_url'].endswith('/api/oauth/token')
    assert urlparse(url).hostname not in ('127.0.0.1', 'localhost', '0.0.0.0')
    credential = a.post('/api/credentials', json={'name': 'LAN MCP'}).json()['token']
    result = a.post(url, headers={'Authorization': 'Bearer ' + credential, 'Accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    assert result.status_code == 200, result.text


def test_flat_layout_preserves_menu_hierarchy(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    assert a.post('/api/menus', json={'path': ['一级', '二级', '末级']}).status_code == 200
    before = a.get('/api/menus').json()
    for layout in ['flat', 'top', 'side', 'flat']:
        changed = a.patch('/api/preferences', json={'layout': layout})
        assert changed.status_code == 200, changed.text
        assert a.get('/api/workspace').json()['tenant']['layout'] == layout
        assert a.get('/api/menus').json() == before
    assert b.get('/api/workspace').json()['tenant']['layout'] == 'side'
    assert a.patch('/api/preferences', json={'layout': 'invalid'}).status_code == 422


def test_page_open_mode_is_tenant_setting_and_keeps_access_control(clients):
    a, _, _ = account(clients)
    b, _, _ = account(clients)
    assert a.get('/api/workspace').json()['tenant']['open_mode'] == 'embedded'
    assert a.get('/api/workspace').json()['external_preview_minutes'] == 5
    assert a.patch('/api/preferences', json={'open_mode': 'external', 'external_preview_minutes': 17}).status_code == 200
    assert a.get('/api/workspace').json()['tenant']['open_mode'] == 'external'
    assert a.get('/api/workspace').json()['external_preview_minutes'] == 17
    assert b.get('/api/workspace').json()['external_preview_minutes'] == 5
    assert b.patch('/api/preferences', json={'open_mode': 'external', 'external_preview_minutes': 9}).status_code == 200
    published = publish(a)
    assert published['status'] == 'success'
    page_id = published['result']['page_id']
    assert a.get('/api/pages/' + page_id + '/preview').json()['expires_in'] == 17 * 60
    assert b.get('/api/pages/' + page_id + '/preview').status_code == 404
    own_page = publish(b)['result']['page_id']
    assert b.get('/api/pages/' + own_page + '/preview').json()['expires_in'] == 9 * 60
    assert a.patch('/api/preferences', json={'external_preview_minutes': 0}).status_code == 422
    assert a.patch('/api/preferences', json={'external_preview_minutes': 1441}).status_code == 422
    assert a.patch('/api/preferences', json={'open_mode': 'embedded'}).status_code == 200
    assert a.get('/api/workspace').json()['tenant']['open_mode'] == 'embedded'
    assert a.patch('/api/preferences', json={'open_mode': 'invalid'}).status_code == 422
