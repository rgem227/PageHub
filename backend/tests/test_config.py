import pytest

from app.config import Settings


def production_settings(web_origin, content_origin, sms_mode='provider'):
    return Settings(
        environment='production',
        database_url='postgresql://pagehub_app:secret@127.0.0.1:55432/pagehub_prod',
        secret_key='x' * 48,
        sms_mode=sms_mode,
        storage_backend='s3',
        s3_endpoint='http://127.0.0.1:59000',
        s3_access_key='access',
        s3_secret_key='secret',
        nginx_accel=True,
        web_origin=web_origin,
        content_origin=content_origin,
    )


def test_production_accepts_separate_origins_on_different_ports():
    production_settings('http://192.168.84.193:51730', 'http://192.168.84.193:51731').validate_runtime()


def test_production_rejects_same_origin_even_with_equivalent_default_port():
    settings = production_settings('http://pagehub.test', 'http://pagehub.test:80')
    with pytest.raises(RuntimeError, match='different origin|不同 origin'):
        settings.validate_runtime()


def test_production_accepts_temporary_sms_mode():
    production_settings('https://pagehub.test:51730', 'https://pagehub.test:51731', sms_mode='temporary').validate_runtime()


def test_production_rejects_development_sms_mode():
    settings = production_settings('https://pagehub.test:51730', 'https://pagehub.test:51731', sms_mode='development')
    with pytest.raises(RuntimeError, match='开发验证码'):
        settings.validate_runtime()


def test_session_cookies_are_separate_between_development_and_production():
    assert Settings(environment='development').session_cookie_name == 'hub_session_dev'
    assert Settings(environment='production').session_cookie_name == 'hub_session'
