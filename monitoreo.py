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



# ====================================================
# 📋 MÉDICOS REGISTRADOS – Listado completo
# ====================================================
@router.get("/medicos_registrados")
def medicos_registrados(db=Depends(get_db)):
    """
    Devuelve todos los médicos registrados con sus datos principales y fotos.
    Ideal para panel de administración / monitoreo.
    """
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                id,
                full_name,
                email,
                telefono,
                matricula,
                especialidad,
                provincia,
                localidad,
                dni,
                tipo ,
                foto_perfil,
                foto_dni_frente,
                foto_dni_dorso,
                selfie_dni,
                validado,
                matricula_validada,
                created_at
            FROM medicos
            ORDER BY created_at DESC;
        """)
        medicos = cur.fetchall()
        cur.close()
        return {"ok": True, "medicos": medicos}
    except Exception as e:
        print("❌ Error en medicos_registrados:", e)
        return {"ok": False, "error": str(e)}
# ====================================================
# ✅ VALIDAR / ❌ DESVALIDAR MATRÍCULA
# ====================================================
@router.put("/validar_matricula/{medico_id}")
def validar_matricula(medico_id: int, db=Depends(get_db)):
    """
    Alterna el estado de matricula_validada (True/False)
    """
    try:
        cur = db.cursor()
        # Obtener valor actual
        cur.execute("SELECT matricula_validada FROM medicos WHERE id = %s", (medico_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Médico no encontrado")

        nuevo_estado = not row[0]
        cur.execute(
            "UPDATE medicos SET matricula_validada = %s WHERE id = %s",
            (nuevo_estado, medico_id)
        )
        db.commit()
        cur.close()

        estado_str = "validada ✅" if nuevo_estado else "desvalidada ❌"
        return {"ok": True, "mensaje": f"Matrícula del médico {medico_id} {estado_str}"}

    except Exception as e:
        print("❌ Error en validar_matricula:", e)
        return {"ok": False, "error": str(e)}

# ====================================================
# 🗺️ UBICACIONES DE TODOS LOS MÉDICOS CONECTADOS
# ====================================================
@router.get("/medicos_ubicacion")
def medicos_ubicacion(db=Depends(get_db)):
    """
    Devuelve la ubicación actual (latitud, longitud) de todos los médicos disponibles
    que están conectados recientemente (último ping < 1 minuto).
    """
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                id,
                full_name AS nombre,
                latitud AS lat,
                longitud AS lng,
                telefono,
                especialidad,
                provincia,
                localidad
            FROM medicos
            WHERE disponible = TRUE
              AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '1 minute')
              AND latitud IS NOT NULL
              AND longitud IS NOT NULL
            ORDER BY id;
        """)
        medicos = cur.fetchall()
        cur.close()
        return {"ok": True, "medicos": medicos}
    except Exception as e:
        print("❌ Error en medicos_ubicacion:", e)
        return {"ok": False, "error": str(e)}


# ====================================================
# 📋 CONSULTAS – LISTADO Y KPIs
# ====================================================
@router.get("/consultas/")
async def listar_consultas(desde: str = None, hasta: str = None, db=Depends(get_db)):
    """
    Devuelve todas las consultas registradas, con filtros opcionales por fecha
    y KPIs por estado.
    """
    try:
        cur = db.cursor()
        filtros = []
        params = []

        if desde:
            filtros.append("c.creado_en >= %s")
            params.append(desde)
        if hasta:
            filtros.append("c.creado_en <= %s")
            params.append(hasta)

        where_clause = "WHERE " + " AND ".join(filtros) if filtros else ""

        cur.execute(f"""
            SELECT 
                c.id, 
                c.creado_en, 
                c.estado, 
                c.motivo, 
                c.metodo_pago,
                c.direccion, 
                p.full_name AS paciente, 
                m.full_name AS profesional, 
                m.tipo
            FROM consultas c
            JOIN pacientes p ON p.uuid = c.paciente_uuid
            JOIN medicos m ON m.id = c.medico_id
            {where_clause}
            ORDER BY c.creado_en DESC;
        """, params)
        consultas = cur.fetchall()

        # KPIs
        cur.execute("""
            SELECT estado, COUNT(*) 
            FROM consultas 
            GROUP BY estado;
        """)
        kpis = cur.fetchall()

        cur.close()
        return {"consultas": consultas, "kpis": kpis}

    except Exception as e:
        print("❌ Error en listar_consultas:", e)
        return {"error": str(e)}

