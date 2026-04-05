
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

import csv
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDICAMENTOS_DIRS = [
    os.path.join(BASE_DIR, "medicamentos"),
    os.path.normpath(os.path.join(BASE_DIR, "..", "medicamentos")),
]
DEFAULT_JSON_PATH = os.path.join(BASE_DIR, "meds_clean.json")


def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


def cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


def _find_csv_source_path() -> str:
    csv_files: list[str] = []
    for source_dir in MEDICAMENTOS_DIRS:
        if not os.path.isdir(source_dir):
            continue
        csv_files.extend(
            [
                os.path.join(source_dir, name)
                for name in os.listdir(source_dir)
                if name.lower().endswith(".csv")
            ]
        )
    if not csv_files:
        raise HTTPException(
            status_code=404,
            detail="No se encontró ningún CSV dentro de las carpetas de medicamentos.",
        )

    csv_files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return csv_files[0]


def _clean_price(value) -> Optional[float]:
    if value is None:
        return None
    raw = str(value).replace("$", "").replace(" ", "").replace(",", ".").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _clean_pct(value) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).replace("%", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_presentacion(presentacion: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not presentacion:
        return None, None
    raw = presentacion.strip()
    lower = raw.lower()
    separators = [" comp.", " caps.", " jbe.", " iny.", " crema", " gotas", " amp.", " sobres", " sachet"]
    for token in separators:
        idx = lower.find(token)
        if idx > 0:
            return raw[idx:].strip(), raw[:idx].strip()
    parts = raw.split(" ", 1)
    if len(parts) == 2:
        return parts[1].strip(), parts[0].strip()
    return raw, None


def _build_csv_rows(csv_path: str) -> list[tuple]:
    rows: list[tuple] = []
    last_error: Optional[Exception] = None
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            with open(csv_path, "r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f, delimiter=";")
                for item in reader:
                    codigo_alfabeta = item.get("ALFABETA")
                    principio_activo = (item.get("PRINCIPIO ACTIVO") or "").strip()
                    marca = (item.get("MARCA COMERCIAL") or "").strip()
                    presentacion = (item.get("PRESENTACION") or "").strip()
                    laboratorio = (item.get("LABORATORIO") or "").strip() or None
                    pvp_pami = _clean_price(item.get("PVP PAMI AL 01/04/2026"))
                    cobertura_pct = _clean_pct(item.get("COBERTURA"))
                    importe_afiliado = _clean_price(item.get("IMPORTE AFILIADO"))
                    forma, concentracion = _parse_presentacion(presentacion)
                    requiere_receta = True if cobertura_pct is None else cobertura_pct < 100

                    rows.append(
                        (
                            marca or principio_activo or presentacion or "Medicamento sin nombre",
                            f"{marca} {presentacion}".strip() or None,
                            [principio_activo] if principio_activo else [],
                            principio_activo or None,
                            laboratorio,
                            forma,
                            concentracion,
                            requiere_receta,
                            None,
                            [],
                            [presentacion] if presentacion else [],
                            int(codigo_alfabeta) if codigo_alfabeta and str(codigo_alfabeta).isdigit() else None,
                            presentacion or None,
                            pvp_pami,
                            cobertura_pct,
                            importe_afiliado,
                        )
                    )
            return rows
        except Exception as exc:
            last_error = exc
            rows.clear()

    raise HTTPException(
        status_code=500,
        detail=f"No se pudo leer el CSV de medicamentos: {last_error}",
    )


def _build_json_rows(json_path: str) -> list[tuple]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meds = data["medicamentos"] if isinstance(data, dict) and "medicamentos" in data else data

    rows = []
    for m in meds:
        presentacion = (m.get("presentacion") or "").strip() if isinstance(m, dict) else ""
        forma, concentracion = _parse_presentacion(presentacion)
        principle = m.get("principio_activo")
        principle_list = principle if isinstance(principle, list) else ([principle] if principle else [])
        principle_str = (
            m.get("principio_activo_str")
            or (principle if isinstance(principle, str) else ", ".join([p for p in principle_list if p]))
            or None
        )
        rows.append(
            (
                m.get("nombre_comercial") or m.get("marca_comercial") or "",
                m.get("nombre_completo") or f"{m.get('marca_comercial', '')} {presentacion}".strip() or None,
                principle_list,
                principle_str,
                m.get("laboratorio"),
                m.get("forma") or forma,
                m.get("concentracion") or concentracion,
                m.get("requiere_receta", True),
                m.get("categoria"),
                m.get("alertas") or [],
                m.get("envases") or ([presentacion] if presentacion else []),
                m.get("codigo_alfabeta"),
                presentacion or m.get("presentacion"),
                _clean_price(m.get("pvp_pami")),
                _clean_pct(m.get("cobertura_pct")),
                _clean_price(m.get("importe_afiliado")),
            )
        )
    return rows


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
            envases              TEXT[],
            codigo_alfabeta      INTEGER,
            presentacion         TEXT,
            pvp_pami             DOUBLE PRECISION,
            cobertura_pct        INTEGER,
            importe_afiliado     DOUBLE PRECISION
        );
    """)

    cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS codigo_alfabeta INTEGER")
    cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS presentacion TEXT")
    cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS pvp_pami DOUBLE PRECISION")
    cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS cobertura_pct INTEGER")
    cur.execute("ALTER TABLE medicamentos ADD COLUMN IF NOT EXISTS importe_afiliado DOUBLE PRECISION")

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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_med_codigo_alfabeta ON medicamentos(codigo_alfabeta);")

    conn.commit()
    return {"ok": True, "mensaje": "Tabla e índices creados correctamente"}


# ====================================================
# 📥 IMPORTAR — carga meds_clean.json a la tabla
# ====================================================
@router.post("/admin/importar", summary="Importa meds_clean.json a la BD")
def importar_medicamentos(vaciar: bool = False, conn=Depends(get_db)):
    """
    Importa medicamentos priorizando el CSV real de la carpeta /medicamentos.
    Si no existe, usa meds_clean.json como fallback.
    """
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

    source_type = ""
    source_path = ""
    if any(os.path.isdir(path) for path in MEDICAMENTOS_DIRS):
        try:
            source_path = _find_csv_source_path()
            rows = _build_csv_rows(source_path)
            source_type = "csv"
        except HTTPException:
            raise
        except Exception:
            rows = []
    else:
        rows = []

    if not rows:
        if not os.path.exists(DEFAULT_JSON_PATH):
            raise HTTPException(
                status_code=404,
                detail="No se encontró ni el CSV de /medicamentos ni meds_clean.json.",
            )
        source_path = DEFAULT_JSON_PATH
        rows = _build_json_rows(DEFAULT_JSON_PATH)
        source_type = "json"

    # Insertar en batches de 500
    sql = """
        INSERT INTO medicamentos
            (nombre_comercial, nombre_completo, principio_activo, principio_activo_str,
             laboratorio, forma, concentracion, requiere_receta, categoria, alertas, envases,
             codigo_alfabeta, presentacion, pvp_pami, cobertura_pct, importe_afiliado)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i+BATCH])

    conn.commit()
    return {
        "ok": True,
        "importados": len(rows),
        "fuente": source_type,
        "archivo": source_path,
    }


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
            presentacion,
            forma,
            concentracion,
            laboratorio,
            requiere_receta,
            categoria,
            alertas,
            codigo_alfabeta,
            pvp_pami,
            cobertura_pct,
            importe_afiliado
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
