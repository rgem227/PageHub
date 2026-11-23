"""初始化独立数据库和应用账号；只在初始化时使用 postgres。"""
import getpass
import os
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from dotenv import dotenv_values
from pwdlib import PasswordHash

ROOT = Path(__file__).resolve().parents[1]
env_path = ROOT / '.env'
existing = dotenv_values(env_path) if env_path.exists() else {}
host = os.getenv('HUB_PG_HOST', '127.0.0.1')
port = os.getenv('HUB_PG_PORT', '5432')
password = os.getenv('HUB_PG_PASSWORD') or getpass.getpass('PostgreSQL postgres 密码: ')
with psycopg.connect(host=host, port=port, user='postgres', password=password, dbname='postgres', autocommit=True) as conn:
    app_password = existing.get('APP_DB_PASSWORD') or secrets.token_urlsafe(32)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='html_hub_app'").fetchone():
        conn.execute(sql.SQL('CREATE ROLE html_hub_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS').format(sql.Literal(app_password)))
    elif not existing:
        sys.exit('html_hub_app 已存在但本地配置缺失，停止以免覆盖已有账号。')
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname='html_hub'").fetchone():
        conn.execute('CREATE DATABASE html_hub')

with psycopg.connect(host=host, port=port, user='postgres', password=password, dbname='html_hub') as conn:
    conn.execute((ROOT / 'backend/schema.sql').read_text(encoding='utf-8'))
    conn.execute('GRANT CONNECT ON DATABASE html_hub TO html_hub_app')
    conn.execute('GRANT USAGE ON SCHEMA public TO html_hub_app')
    conn.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO html_hub_app')
    conn.execute('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO html_hub_app')
    admin_created = False
    if not conn.execute("SELECT 1 FROM users WHERE role='superadmin'").fetchone():
        tenant = conn.execute("INSERT INTO tenants(name) VALUES ('平台管理') RETURNING id").fetchone()[0]
        admin_password = secrets.token_urlsafe(18)
        conn.execute("INSERT INTO users(tenant_id,username,password_hash,role,must_change_password) VALUES (%s,'admin',%s,'superadmin',true)",
                     (tenant, PasswordHash.recommended().hash(admin_password)))
        admin_created = True

if not env_path.exists():
    conninfo = make_conninfo(host=host, port=port, dbname='html_hub', user='html_hub_app', password=app_password)
    env_path.write_text(f'DATABASE_URL="{conninfo}"\nAPP_DB_PASSWORD={app_password}\nSECRET_KEY={secrets.token_urlsafe(48)}\nENVIRONMENT=development\nSMS_MODE=development\nSTORAGE_BACKEND=local\nWEB_ORIGIN=http://127.0.0.1:5173\nCONTENT_ORIGIN=http://127.0.0.1:8011\n', encoding='utf-8')
if admin_created:
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    (runtime / 'admin-credentials.txt').write_text(f'用户名: admin\n临时密码: {admin_password}\n访问地址: http://127.0.0.1:5173\n首次登录必须修改密码，请保存后删除此文件。\n', encoding='utf-8')
    print('管理员初始凭据已保存至 .runtime/admin-credentials.txt，首次登录需修改密码。')
print('数据库初始化完成，应用使用非超级用户 html_hub_app。')
