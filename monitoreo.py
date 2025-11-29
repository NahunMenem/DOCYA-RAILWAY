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
# ====================================================
# 📊 RESUMEN GENERAL (Dashboard)
# ====================================================
@router.get("/resumen")
def resumen_monitoreo(db=Depends(get_db)):
    try:
        cur = db.cursor()

        # 🩺 Total médicos registrados
        cur.execute("SELECT COUNT(*) FROM medicos WHERE tipo = 'medico';")
        total_medicos = cur.fetchone()[0]

        # 👩‍⚕️ Total enfermeros registrados
        cur.execute("SELECT COUNT(*) FROM medicos WHERE tipo = 'enfermero';")
        total_enfermeros = cur.fetchone()[0]

        # 👨‍⚕️ Médicos conectados (último ping menor a 1 min)
        cur.execute("""
            SELECT COUNT(*)
            FROM medicos
            WHERE tipo = 'medico'
            AND disponible = TRUE
            AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '1 minute');
        """)
        medicos_conectados = cur.fetchone()[0]

        # 👩‍⚕️ Enfermeros conectados (último ping menor a 1 min)
        cur.execute("""
            SELECT COUNT(*)
            FROM medicos
            WHERE tipo = 'enfermero'
            AND disponible = TRUE
            AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '1 minute');
        """)
        enfermeros_conectados = cur.fetchone()[0]

        # 💬 Consultas activas
        cur.execute("""
            SELECT COUNT(*) 
            FROM consultas 
            WHERE estado IN ('aceptada', 'en_camino', 'en_domicilio');
        """)
        consultas_en_curso = cur.fetchone()[0]

        # 📅 Consultas de hoy
        cur.execute("""
            SELECT COUNT(*)
            FROM consultas 
            WHERE DATE(creado_en) = CURRENT_DATE;
        """)
        consultas_hoy = cur.fetchone()[0]

        # 👥 Total usuarios pacientes
        cur.execute("SELECT COUNT(*) FROM users WHERE role = 'patient';")
        total_usuarios = cur.fetchone()[0]

        cur.close()

        return {
            "total_medicos": total_medicos,
            "total_enfermeros": total_enfermeros,
            "medicos_conectados": medicos_conectados,
            "enfermeros_conectados": enfermeros_conectados,
            "consultas_en_curso": consultas_en_curso,
            "consultas_hoy": consultas_hoy,
            "total_usuarios": total_usuarios
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
                tipo,
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
                localidad,
                tipo  
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
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        filtros = []
        params = []

        # 🔹 Filtros por fecha
        if desde:
            filtros.append("c.creado_en >= %s")
            params.append(desde)
        if hasta:
            filtros.append("c.creado_en <= %s")
            params.append(hasta)

        where_clause = "WHERE " + " AND ".join(filtros) if filtros else ""

        # 🔹 Consulta principal con duración
        cur.execute(f"""
            SELECT 
                c.id, 
                c.creado_en, 
                c.estado, 
                c.motivo, 
                c.metodo_pago,
                c.direccion, 
                COALESCE(u.full_name, 'Sin paciente') AS paciente,
                COALESCE(m.full_name, 'Sin profesional') AS profesional, 
                COALESCE(m.tipo, '-') AS tipo,
                c.inicio_atencion,
                c.fin_atencion,
                ROUND(EXTRACT(EPOCH FROM (c.fin_atencion - c.inicio_atencion)) / 60, 1) AS duracion_min
            FROM consultas c
            LEFT JOIN users u ON c.paciente_uuid = u.id
            LEFT JOIN medicos m ON m.id = c.medico_id
            {where_clause}
            ORDER BY c.creado_en DESC;
        """, params)

        consultas = cur.fetchall()

        # 🔹 KPIs por estado
        cur.execute("""
            SELECT estado, COUNT(*) 
            FROM consultas 
            GROUP BY estado
            ORDER BY estado;
        """)
        kpis = cur.fetchall()

        cur.close()

        return {"consultas": consultas, "kpis": kpis}

    except Exception as e:
        print("❌ Error en listar_consultas:", e)
        return {"error": str(e)}

# ====================================================
# ⏱ TIEMPO PROMEDIO DE ATENCIÓN
# ====================================================
@router.get("/tiempo_promedio")
async def tiempo_promedio_consultas(db=Depends(get_db)):
    """
    Devuelve el tiempo promedio de atención (en minutos)
    calculado como la diferencia entre fin_atencion e inicio_atencion
    para todas las consultas finalizadas.
    """
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                ROUND(AVG(EXTRACT(EPOCH FROM (fin_atencion - inicio_atencion)) / 60), 1) 
                AS tiempo_promedio_min
            FROM consultas
            WHERE fin_atencion IS NOT NULL
              AND inicio_atencion IS NOT NULL;
        """)
        resultado = cur.fetchone()
        cur.close()

        promedio = resultado["tiempo_promedio_min"] or 0
        return {"tiempo_promedio_min": promedio}

    except Exception as e:
        print("❌ Error en tiempo_promedio_consultas:", e)
        return {"tiempo_promedio_min": 0}

@router.get("/usuarios")
async def listar_usuarios(db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                email,
                full_name,
                password_hash,
                dni,
                telefono,
                pais,
                provincia,
                localidad,
                fecha_nacimiento,
                acepto_condiciones,
                fecha_aceptacion,
                version_texto,
                validado,
                role
            FROM users
            ORDER BY created_at DESC;
        """)
        usuarios = cur.fetchall()
        cur.close()
        return usuarios
    except Exception as e:
        print("❌ Error listando usuarios:", e)
        return {"error": str(e)}

# ====================================================
# 📍 MÉDICOS POR COMUNA (CABA)
# ====================================================
@router.get("/medicos_por_comuna")
def medicos_por_comuna(db=Depends(get_db)):
    """
    Devuelve un resumen por comuna basado en las localidades de CABA.
    """
    comuna_map = {
        1: ['Retiro', 'San Nicolás', 'Puerto Madero', 'San Telmo', 'Montserrat', 'Constitución'],
        2: ['Recoleta'],
        3: ['Balvanera', 'San Cristóbal'],
        4: ['La Boca', 'Barracas', 'Parque Patricios', 'Nueva Pompeya'],
        5: ['Almagro', 'Boedo'],
        6: ['Caballito'],
        7: ['Flores', 'Parque Chacabuco'],
        8: ['Villa Soldati', 'Villa Riachuelo', 'Villa Lugano'],
        9: ['Parque Avellaneda', 'Mataderos', 'Liniers'],
        10: ['Villa Real', 'Monte Castro', 'Versalles', 'Floresta', 'Vélez Sarsfield', 'Villa Luro'],
        11: ['Villa Devoto', 'Villa del Parque', 'Villa Santa Rita'],
        12: ['Coghlan', 'Saavedra', 'Villa Urquiza'],
        13: ['Núñez', 'Belgrano', 'Colegiales'],
        14: ['Palermo'],
        15: ['Chacarita', 'Villa Crespo', 'La Paternal', 'Villa Ortúzar', 'Agronomía', 'Parque Chas']
    }

    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT localidad, COUNT(*) AS total
        FROM medicos
        WHERE provincia ILIKE 'CABA'
        GROUP BY localidad;
    """)
    data = cur.fetchall()

    # Agrupar por comuna
    resultado = {str(i): {"total": 0, "barrios": comuna_map[i]} for i in comuna_map}

    for row in data:
        loc = row["localidad"]
        for comuna, barrios in comuna_map.items():
            if loc in barrios:
                resultado[str(comuna)]["total"] += row["total"]

    cur.close()
    return {"ok": True, "comunas": resultado}
