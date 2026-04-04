"""
Helpers de acceso a PostgreSQL.

La idea es que cualquier router o módulo pueda reutilizar estas funciones
sin volver a declarar conexiones en cada archivo.
"""

import psycopg2

from settings import DATABASE_URL


def get_db():
    """Dependency de FastAPI: abre una conexión por request y la cierra al final."""
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


def get_db_worker():
    """Devuelve una conexión directa para workers o tareas fuera del request."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")
