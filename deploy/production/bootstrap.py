import ipaddress
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pwdlib import PasswordHash


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / 'app'
APP_USER = 'pagehub_prod_app'
DB_NAME = 'pagehub_prod'
DB_PORT = 55432


def write_certificate(lan_ip):
    cert_dir = ROOT / 'certs'
    cert_dir.mkdir(exist_ok=True)
    if (cert_dir / 'pagehub.crt').exists():
        return

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PageHub Local Root CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=True,
                                      key_agreement=False, content_commitment=False, data_encipherment=False,
                                      crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PageHub')])
    leaf = (x509.CertificateBuilder().subject_name(leaf_name).issuer_name(ca.subject)
            .public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(lan_ip))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))

    (ROOT / 'credentials' / 'pagehub-root-ca.key').write_bytes(
        ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()))
    (ROOT / 'certs' / 'pagehub-root-ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (ROOT / 'certs' / 'pagehub-trust.cer').write_bytes(ca.public_bytes(serialization.Encoding.DER))
    (ROOT / 'certs' / 'pagehub.crt').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (ROOT / 'certs' / 'pagehub.key').write_bytes(
        leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()))


def main():
    lan_ip = os.environ['PAGEHUB_LAN_IP']
    pg_admin_password = os.environ['PAGEHUB_PG_ADMIN_PASSWORD']
    app_password = secrets.token_urlsafe(36)
    admin_password = secrets.token_urlsafe(24)
    s3_access_key = 'ph' + secrets.token_hex(10)
    s3_secret_key = secrets.token_urlsafe(36)
    secret_key = secrets.token_urlsafe(48)

    admin_conn = psycopg.connect(host='127.0.0.1', port=DB_PORT, user='postgres',
                                 password=pg_admin_password, dbname='postgres', autocommit=True)
    with admin_conn:
        if admin_conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (APP_USER,)).fetchone():
            raise RuntimeError(f'数据库角色 {APP_USER} 已存在，拒绝覆盖')
        if admin_conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (DB_NAME,)).fetchone():
            raise RuntimeError(f'数据库 {DB_NAME} 已存在，拒绝覆盖')
        admin_conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS')
                           .format(sql.Identifier(APP_USER), sql.Literal(app_password)))
        admin_conn.execute(sql.SQL('CREATE DATABASE {} OWNER postgres').format(sql.Identifier(DB_NAME)))
    admin_conn.close()

    with psycopg.connect(host='127.0.0.1', port=DB_PORT, user='postgres',
                         password=pg_admin_password, dbname=DB_NAME) as conn:
        conn.execute((APP_ROOT / 'backend' / 'schema.sql').read_text(encoding='utf-8'))
        conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(DB_NAME), sql.Identifier(APP_USER)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(APP_USER)))
        conn.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(APP_USER)))
        conn.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(APP_USER)))
        conn.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}').format(sql.Identifier(APP_USER)))
        conn.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}').format(sql.Identifier(APP_USER)))
        tenant_id = conn.execute("INSERT INTO tenants(name) VALUES ('PageHub 平台空间') RETURNING id").fetchone()[0]
        conn.execute('''INSERT INTO users(tenant_id,username,password_hash,role,must_change_password)
                        VALUES (%s,'admin',%s,'superadmin',true)''',
                     (tenant_id, PasswordHash.recommended().hash(admin_password)))

    database_url = make_conninfo(host='127.0.0.1', port=DB_PORT, dbname=DB_NAME,
                                 user=APP_USER, password=app_password)
    env = '\n'.join([
        f'DATABASE_URL="{database_url}"',
        f'APP_DB_PASSWORD={app_password}',
        f'SECRET_KEY={secret_key}',
        'ENVIRONMENT=production',
        'SMS_MODE=temporary',
        f'WEB_ORIGIN=https://{lan_ip}:51730',
        f'CONTENT_ORIGIN=https://{lan_ip}:51731',
        f'MCP_PUBLIC_URL=https://{lan_ip}:51730/mcp/',
        'STORAGE_BACKEND=s3',
        'MAX_UPLOAD_MB=20',
        'MAX_EXPANDED_MB=100',
        'MAX_ZIP_FILES=1000',
        'DEFAULT_QUOTA_MB=1024',
        'S3_ENDPOINT=http://127.0.0.1:59000',
        'S3_BUCKET=pagehub-prod',
        f'S3_ACCESS_KEY={s3_access_key}',
        f'S3_SECRET_KEY={s3_secret_key}',
        'S3_REGION=us-east-1',
        'NGINX_ACCEL=true',
        '',
    ])
    (APP_ROOT / '.env').write_text(env, encoding='utf-8')
    credentials = ROOT / 'credentials'
    credentials.mkdir(exist_ok=True)
    (credentials / 'admin-initial.txt').write_text(
        f'用户名: admin\n临时密码: {admin_password}\n访问地址: https://{lan_ip}:51730\n'
        '首次登录后必须立即修改密码；确认可登录后删除本文件。\n', encoding='utf-8')
    (credentials / 'postgres-superuser.txt').write_text(
        f'PostgreSQL 管理用户: postgres\n临时初始化密码: {pg_admin_password}\n'
        '请妥善保管，仅供数据库维护使用。\n', encoding='utf-8')
    write_certificate(lan_ip)
    print('production schema, administrator, secrets and local TLS certificate created')


if __name__ == '__main__':
    main()
