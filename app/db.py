import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json, RealDictCursor

DSN = dict(
    dbname="domicilios",
    user="domicilios",
    password=os.getenv("DB_PASSWORD", "domicilios_dev"),
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "5432")),
)


def _default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date, time)):
        return o.isoformat()
    return str(o)


def dumps(o) -> str:
    return json.dumps(o, default=_default, ensure_ascii=False)


def js(o):
    """Envuelve un objeto para guardarlo en una columna JSONB."""
    return Json(o, dumps=dumps)


@contextmanager
def tx():
    """Transacción: commit al salir sin error, rollback si hay excepción."""
    c = psycopg2.connect(**DSN)
    try:
        with c:
            with c.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
    finally:
        c.close()


def q(sql, params=(), uno=False):
    with tx() as cur:
        cur.execute(sql, params)
        if cur.description:
            filas = cur.fetchall()
            if uno:
                return filas[0] if filas else None
            return filas
        return None
