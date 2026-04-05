"""
Helpers de acceso a PostgreSQL.

La idea es que cualquier router o módulo pueda reutilizar estas funciones
sin volver a declarar conexiones en cada archivo.
"""

import psycopg2

from settings import DATABASE_URL


def _connect():
    """Abre una conexión PostgreSQL fijando la sesión en hora argentina."""
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/Argentina/Buenos_Aires'")
    return conn


def get_db():
    """Dependency de FastAPI: abre una conexión por request y la cierra al final."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db_worker():
    """Devuelve una conexión directa para workers o tareas fuera del request."""
    return _connect()
