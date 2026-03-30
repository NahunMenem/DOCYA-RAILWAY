
# ====================================================
# 💊 MÓDULO: MEDICAMENTOS (Vademécum DocYa)
# ====================================================
# Uso en main.py:
#   from medicamentos import router as medicamentos_router
#   app.include_router(medicamentos_router)
#
# Primer uso — crear tabla e importar datos:
#   POST /medicamentos/admin/setup    (crea la tabla)
#   POST /medicamentos/admin/importar (carga meds_clean.json)
# ====================================================

import json
import os
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import RealDictCursor
import psycopg2

router = APIRouter(prefix="/medicamentos", tags=["Medicamentos"])

# ====================================================
# 🧩 CONEXIÓN (misma que main.py)
# ====================================================
DATABASE_URL = os.getenv("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


def cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


# ====================================================
# 🏗️ SETUP — crear tabla e índices
# ====================================================
@router.post("/admin/setup", summary="Crea tabla medicamentos e índices")
def setup_tabla(conn=Depends(get_db)):
    cur = cursor(conn)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS medicamentos (
            id                   SERIAL PRIMARY KEY,
            nombre_comercial     TEXT NOT NULL,
            nombre_completo      TEXT,
            principio_activo     TEXT[],
            principio_activo_str TEXT,
            laboratorio          TEXT,
            forma                TEXT,
            concentracion        TEXT,
            requiere_receta      BOOLEAN DEFAULT TRUE,
            categoria            TEXT,
            alertas              TEXT[],
            envases              TEXT[]
        );
    """)

    # Extensión trigram para autocompletar con tolerancia a errores
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # Índice trigram en nombre comercial
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_med_nombre_trgm
        ON medicamentos USING gin(nombre_comercial gin_trgm_ops);
    """)

    # Índice trigram en principio activo
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_med_pa_trgm
        ON medicamentos USING gin(principio_activo_str gin_trgm_ops);
    """)

    # Índice full-text en español
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_med_fts
        ON medicamentos
        USING gin(to_tsvector('spanish',
            nombre_comercial || ' ' || COALESCE(principio_activo_str, '')
        ));
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_med_categoria ON medicamentos(categoria);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_med_receta ON medicamentos(requiere_receta);")

    conn.commit()
    return {"ok": True, "mensaje": "Tabla e índices creados correctamente"}


# ====================================================
# 📥 IMPORTAR — carga meds_clean.json a la tabla
# ====================================================
@router.post("/admin/importar", summary="Importa meds_clean.json a la BD")
def importar_medicamentos(vaciar: bool = False, conn=Depends(get_db)):
    """
    Carga el archivo meds_clean.json (generado por normalizar.py) en la BD.
    Pasar ?vaciar=true para truncar antes de importar.
    """
    if not os.path.exists("meds_clean.json"):
        raise HTTPException(
            status_code=404,
            detail="meds_clean.json no encontrado. Correr primero normalizar.py"
        )

    cur = cursor(conn)

    if vaciar:
        cur.execute("TRUNCATE medicamentos RESTART IDENTITY")

    cur.execute("SELECT COUNT(*) as total FROM medicamentos")
    count = cur.fetchone()["total"]
    if count > 0 and not vaciar:
        return {
            "ok": False,
            "mensaje": f"Ya hay {count} medicamentos. Usar ?vaciar=true para reimportar."
        }

    with open("meds_clean.json", "r", encoding="utf-8") as f:
        meds = json.load(f)

    rows = [
        (
            m.get("nombre_comercial") or "",
            m.get("nombre_completo"),
            m.get("principio_activo") or [],
            m.get("principio_activo_str"),
            m.get("laboratorio"),
            m.get("forma"),
            m.get("concentracion"),
            m.get("requiere_receta", True),
            m.get("categoria"),
            m.get("alertas") or [],
            m.get("envases") or [],
        )
        for m in meds
    ]

    # Insertar en batches de 500
    sql = """
        INSERT INTO medicamentos
            (nombre_comercial, nombre_completo, principio_activo, principio_activo_str,
             laboratorio, forma, concentracion, requiere_receta, categoria, alertas, envases)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i+BATCH])

    conn.commit()
    return {"ok": True, "importados": len(rows)}


# ====================================================
# 🔍 BUSCAR — autocompletar para recetas
# ====================================================
@router.get("", summary="Buscar medicamento (autocompletar)")
def buscar_medicamentos(
    q: str = Query(..., min_length=2, description="Nombre comercial o principio activo"),
    limit: int = Query(10, le=50),
    solo_otc: Optional[bool] = Query(None, description="true = sin receta"),
    categoria: Optional[str] = Query(None),
    conn=Depends(get_db),
):
    """
    Busca por nombre comercial O principio activo.
    Prioriza coincidencias que empiezan con el texto buscado.
    Ideal para autocompletar mientras el médico escribe.
    """
    cur = cursor(conn)

    filtros = ["(nombre_comercial ILIKE %s OR principio_activo_str ILIKE %s)"]
    params: list = [f"%{q}%", f"%{q}%"]

    if solo_otc is not None:
        filtros.append("requiere_receta = %s")
        params.append(not solo_otc)

    if categoria:
        filtros.append("categoria = %s")
        params.append(categoria)

    where = " AND ".join(filtros)

    cur.execute(f"""
        SELECT
            id,
            nombre_comercial,
            principio_activo_str,
            forma,
            concentracion,
            laboratorio,
            requiere_receta,
            categoria,
            alertas
        FROM medicamentos
        WHERE {where}
        ORDER BY
            CASE WHEN nombre_comercial ILIKE %s THEN 0 ELSE 1 END,
            nombre_comercial
        LIMIT %s
    """, [*params, f"{q}%", limit])

    resultados = cur.fetchall()
    return {"total": len(resultados), "resultados": [dict(r) for r in resultados]}


# ====================================================
# 📋 DETALLE — datos completos para llenar la receta
# ====================================================
@router.get("/{med_id}", summary="Detalle de un medicamento")
def detalle_medicamento(med_id: int, conn=Depends(get_db)):
    """
    Devuelve todos los datos del medicamento.
    Llamar al hacer click en el resultado del autocompletar.
    """
    cur = cursor(conn)
    cur.execute("SELECT * FROM medicamentos WHERE id = %s", (med_id,))
    med = cur.fetchone()

    if not med:
        raise HTTPException(status_code=404, detail="Medicamento no encontrado")

    return dict(med)


# ====================================================
# 💊 POR PRINCIPIO ACTIVO — alternativas / genéricos
# ====================================================
@router.get("/principio/{nombre}", summary="Buscar por principio activo")
def por_principio_activo(
    nombre: str,
    limit: int = Query(20, le=100),
    conn=Depends(get_db),
):
    """
    Devuelve todas las marcas y genéricos que contienen ese principio activo.
    Útil para mostrar alternativas al médico.
    """
    cur = cursor(conn)
    cur.execute("""
        SELECT id, nombre_comercial, forma, concentracion,
               laboratorio, requiere_receta, categoria
        FROM medicamentos
        WHERE principio_activo_str ILIKE %s
        ORDER BY nombre_comercial
        LIMIT %s
    """, (f"%{nombre}%", limit))

    resultados = cur.fetchall()
    return {
        "principio_activo": nombre,
        "total": len(resultados),
        "resultados": [dict(r) for r in resultados],
    }


# ====================================================
# 🗂️ CATEGORÍAS — para filtros en la UI
# ====================================================
@router.get("/utils/categorias", summary="Listar categorías disponibles")
def listar_categorias(conn=Depends(get_db)):
    cur = cursor(conn)
    cur.execute("""
        SELECT categoria, COUNT(*) as total
        FROM medicamentos
        GROUP BY categoria
        ORDER BY total DESC
    """)
    return [dict(r) for r in cur.fetchall()]
