from unittest.mock import MagicMock

import pytest

from app import storage


def test_permanent_object_cleanup_includes_storage_versions_and_markers(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(storage, 's3', lambda: client)
    monkeypatch.setattr(storage.settings, 'storage_backend', 's3')
    client.get_bucket_versioning.return_value = {'Status': 'Enabled'}
    client.get_paginator.return_value.paginate.return_value = [{
        'Versions': [{'Key': 'safe/version/index.html', 'VersionId': 'v1'}, {'Key': 'safe/version/index.html', 'VersionId': 'v2'}],
        'DeleteMarkers': [{'Key': 'safe/version/index.html', 'VersionId': 'marker'}],
    }]
    client.delete_objects.return_value = {}
    storage.remove_prefix('safe/version/', permanent=True)
    assert client.get_paginator.call_args.args == ('list_object_versions',)
    assert len(client.delete_objects.call_args.kwargs['Delete']['Objects']) == 3
    client.delete_objects.return_value = {'Errors': [{'Code': 'AccessDenied'}]}
    with pytest.raises(RuntimeError, match='删除失败'):
        storage.remove_prefix('safe/version/', permanent=True)


def test_unversioned_object_cleanup_and_path_boundary(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(storage, 's3', lambda: client)
    monkeypatch.setattr(storage.settings, 'storage_backend', 's3')
    client.get_bucket_versioning.return_value = {}
    client.get_paginator.return_value.paginate.return_value = [{'Contents': [{'Key': 'safe/version/index.html'}]}]
    client.delete_objects.return_value = {}
    storage.remove_prefix('safe/version/', permanent=True)
    assert client.get_paginator.call_args.args == ('list_objects_v2',)
    assert client.delete_objects.call_args.kwargs['Delete']['Objects'] == [{'Key': 'safe/version/index.html'}]
    for prefix in ('', '../outside/', '/absolute/', 'safe/../../outside', 'safe\\..\\outside'):
        with pytest.raises(ValueError):
            storage.remove_prefix(prefix, permanent=True)
