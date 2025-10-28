# ====================================================
# 🩺 MÓDULO DE MONITOREO – DOCYA
# ====================================================
from fastapi import APIRouter, Depends
from psycopg2.extras import RealDictCursor
from database import get_db  # si get_db está en el mismo main.py, podés importarlo directo

router = APIRouter(prefix="/monitoreo", tags=["Monitoreo"])


# ====================================================
# 📊 RESUMEN GENERAL
# ====================================================
@router.get("/resumen")
def resumen_monitoreo(db=Depends(get_db)):
    try:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM medicos;")
        total_medicos = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM medicos WHERE conectado = TRUE;")
        medicos_conectados = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM consultas WHERE estado = 'en_curso';")
        consultas_en_curso = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM consultas WHERE DATE(fecha_creacion) = CURRENT_DATE;")
        consultas_hoy = cur.fetchone()[0]

        cur.close()
        return {
            "total_medicos": total_medicos,
            "medicos_conectados": medicos_conectados,
            "consultas_en_curso": consultas_en_curso,
            "consultas_hoy": consultas_hoy
        }

    except Exception as e:
        print("Error en resumen_monitoreo:", e)
        return {"error": str(e)}


# ====================================================
# 📍 MÉDICOS CONECTADOS CON UBICACIÓN
# ====================================================
@router.get("/medicos_conectados")
def medicos_conectados(db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, nombre, apellido, especialidad, latitud, longitud
            FROM medicos
            WHERE conectado = TRUE;
        """)
        medicos = cur.fetchall()
        cur.close()
        return medicos
    except Exception as e:
        print("Error en medicos_conectados:", e)
        return {"error": str(e)}


# ====================================================
# 🗺️ MÉDICOS AGRUPADOS POR ZONA
# ====================================================
@router.get("/zonas")
def medicos_por_zona(db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT provincia, localidad, COUNT(*) AS total
            FROM medicos
            GROUP BY provincia, localidad
            ORDER BY provincia, localidad;
        """)
        zonas = cur.fetchall()
        cur.close()
        return zonas
    except Exception as e:
        print("Error en medicos_por_zona:", e)
        return {"error": str(e)}
