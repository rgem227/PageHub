def pattern(value):
    return '%' + value.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def paginated(conn, fields, source, params, order, page, size, prefix=''):
    total = conn.execute(prefix + 'SELECT count(*) AS n ' + source, params).fetchone()['n']
    page = min(page, max(1, (total + size - 1) // size))
    items = conn.execute(prefix + 'SELECT ' + fields + ' ' + source + ' ORDER BY ' + order + ' LIMIT %s OFFSET %s',
                         (*params, size, (page - 1) * size)).fetchall()
    return {'items': items, 'total': total, 'page': page, 'size': size}
