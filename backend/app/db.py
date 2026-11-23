import json
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

pool = ConnectionPool(settings.database_url, min_size=1, max_size=15,
                      kwargs={'row_factory': dict_row}, open=False)


@contextmanager
def transaction(tenant_id=None, admin=False, user_id=None):
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true), set_config('app.admin', %s, true)",
                         (str(tenant_id or ''), str(user_id or ''), 'true' if admin else 'false'))
            yield conn


def audit(conn, user, action, detail):
    conn.execute('INSERT INTO audit_logs (tenant_id, user_id, action, detail) VALUES (%s,%s,%s,%s)',
                 (user['tenant_id'], user['id'], action, json.dumps(detail, ensure_ascii=False)))
