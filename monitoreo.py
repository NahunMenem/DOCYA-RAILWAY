# ====================================================
# 🩺 MÓDULO DE MONITOREO – DOCYA
# ====================================================
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from psycopg2.extras import RealDictCursor
import json
import asyncio
from database import get_db
import psycopg2
from os import getenv

router = APIRouter(prefix="/monitoreo", tags=["Monitoreo"])

# Lista de conexiones WebSocket activas (admins)
active_admins: list[WebSocket] = []


# ====================================================
# 📊 RESUMEN GENERAL
# ====================================================
@router.get("/resumen")
def resumen_monitoreo(db=Depends(get_db)):
    try:
        cur = db.cursor()

        cur.execute("SELECT COUNT(*) FROM medicos;")
        total_medicos = cur.fetchone()[0]

        # ✅ Usar 'disponible' (tu campo real)
        cur.execute("SELECT COUNT(*) FROM medicos WHERE disponible = TRUE;")
        medicos_conectados = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM consultas WHERE estado = 'en_domicilio';")
        consultas_en_curso = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM consultas WHERE DATE(creado_en) = CURRENT_DATE;")
        consultas_hoy = cur.fetchone()[0]

        cur.close()
        return {
            "total_medicos": total_medicos,
            "medicos_conectados": medicos_conectados,
            "consultas_en_curso": consultas_en_curso,
            "consultas_hoy": consultas_hoy
        }

    except Exception as e:
        print("❌ Error en resumen_monitoreo:", e)
        return {"error": str(e)}


# ====================================================
# 📍 MÉDICOS CONECTADOS
# ====================================================
@router.get("/medicos_conectados")
def medicos_conectados(db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, full_name, especialidad, latitud, longitud
            FROM medicos
            WHERE disponible = TRUE;
        """)
        data = cur.fetchall()
        cur.close()
        return data
    except Exception as e:
        print("❌ Error en medicos_conectados:", e)
        return {"error": str(e)}


# ====================================================
# 📍 MÉDICOS POR ZONA
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
        data = cur.fetchall()
        cur.close()
        return data
    except Exception as e:
        print("❌ Error en medicos_por_zona:", e)
        return {"error": str(e)}


# ====================================================
# 🔴 MONITOREO EN TIEMPO REAL (WebSocket)
# ====================================================
@router.websocket("/tiempo_real")
async def tiempo_real(websocket: WebSocket):
    await websocket.accept()
    active_admins.append(websocket)
    print(f"🟢 Admin conectado al monitoreo ({len(active_admins)} totales)")

    try:
        while True:
            await asyncio.sleep(5)
            data = await obtener_estado_general()
            await websocket.send_text(json.dumps(data))
    except WebSocketDisconnect:
        active_admins.remove(websocket)
        print(f"🔴 Admin desconectado del monitoreo ({len(active_admins)} restantes)")
    except Exception as e:
        print("❌ Error en tiempo_real:", e)
        if websocket in active_admins:
            active_admins.remove(websocket)


# ====================================================
# 🔁 FUNCIÓN AUXILIAR PARA OBTENER ESTADO ACTUAL
# ====================================================
async def obtener_estado_general():
    DATABASE_URL = getenv("DATABASE_URL")
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM medicos WHERE disponible = TRUE;")
    medicos_conectados = cur.fetchone()[0]

    # ✅ cambiar aquí:
    cur.execute("SELECT COUNT(*) FROM consultas WHERE estado = 'en_domicilio';")
    consultas_en_curso = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM consultas WHERE DATE(creado_en) = CURRENT_DATE;")
    consultas_hoy = cur.fetchone()[0]

    cur.close()
    conn.close()

    return {
        "medicos_conectados": medicos_conectados,
        "consultas_en_curso": consultas_en_curso,
        "consultas_hoy": consultas_hoy
    }
