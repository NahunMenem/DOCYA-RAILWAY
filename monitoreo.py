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
from datetime import datetime, timedelta

router = APIRouter(prefix="/monitoreo", tags=["Monitoreo"])

# Lista de conexiones WebSocket activas (admins)
active_admins: list[WebSocket] = []


# ====================================================
# 🧹 LIMPIADOR AUTOMÁTICO DE MÉDICOS INACTIVOS
# ====================================================
async def limpiar_medicos_inactivos():
    """Marca como NO disponibles los médicos que no enviaron ping hace más de 60 segundos."""
    DATABASE_URL = getenv("DATABASE_URL")
    while True:
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            cur = conn.cursor()
            cur.execute("""
                UPDATE medicos
                SET disponible = FALSE
                WHERE ultimo_ping IS NOT NULL
                AND ultimo_ping < NOW() - INTERVAL '60 seconds'
                AND disponible = TRUE;
            """)
            afectados = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()

            if afectados > 0:
                print(f"🧹 {afectados} médicos marcados como NO disponibles por inactividad.")
        except Exception as e:
            print(f"⚠️ Error en limpieza automática: {e}")

        await asyncio.sleep(60)  # Ejecutar cada minuto


# ====================================================
# 📊 RESUMEN GENERAL
# ====================================================
@router.get("/resumen")
def resumen_monitoreo(db=Depends(get_db)):
    try:
        cur = db.cursor()

        # Total médicos registrados
        cur.execute("SELECT COUNT(*) FROM medicos;")
        total_medicos = cur.fetchone()[0]

        # ✅ Conectados (último ping menor a 30 s)
        cur.execute("""
            SELECT COUNT(*)
            FROM medicos
            WHERE disponible = TRUE
            AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '1 minute');
        """)
        medicos_conectados = cur.fetchone()[0]

        # Consultas activas
        cur.execute("""
            SELECT COUNT(*) 
            FROM consultas 
            WHERE estado IN ('aceptada', 'en_camino', 'en_domicilio');
        """)
        consultas_en_curso = cur.fetchone()[0]

        # Consultas de hoy
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
            SELECT id, full_name, especialidad, latitud, longitud, ultimo_ping
            FROM medicos
            WHERE disponible = TRUE
            AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '30 seconds');
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
        if websocket in active_admins:
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

    cur.execute("""
        SELECT COUNT(*)
        FROM medicos
        WHERE disponible = TRUE
        AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '30 seconds');
    """)
    medicos_conectados = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) 
        FROM consultas 
        WHERE estado IN ('aceptada', 'en_camino', 'en_domicilio');
    """)
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


# ====================================================
# 🚀 LANZAR LIMPIADOR AUTOMÁTICO AL INICIAR
# ====================================================
@router.on_event("startup")
async def iniciar_limpieza_automatica():
    asyncio.create_task(limpiar_medicos_inactivos())
    print("🧭 Limpieza automática de médicos inactivos iniciada cada 60 s.")
