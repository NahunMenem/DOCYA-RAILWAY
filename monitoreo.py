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


# ====================================================
# 📊LQUIDACIONES
# ====================================================
from datetime import date, timedelta
from psycopg2.extras import RealDictCursor

from datetime import date, timedelta, time
from psycopg2.extras import RealDictCursor

@router.get("/liquidaciones/preview_semana_actual")
def preview_liquidaciones_semana_actual(db=Depends(get_db)):
    """
    Vista de control semanal (NO pagable).
    Muestra desglose claro de consultas y resultado provisorio.
    """
    cur = db.cursor(cursor_factory=RealDictCursor)

    hoy = date.today()
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    fin_semana = hoy

    cur.execute("""
        SELECT
            m.id AS medico_id,
            m.full_name AS medico,

            COUNT(DISTINCT c.id) AS consultas_totales,

            -- 🌞🌙 Diurnas / Nocturnas
            SUM(CASE 
                WHEN c.fin_atencion::time >= '06:00'
                 AND c.fin_atencion::time < '22:00'
                THEN 1 ELSE 0 END
            ) AS consultas_diurnas,

            SUM(CASE 
                WHEN c.fin_atencion::time >= '22:00'
                 OR c.fin_atencion::time < '06:00'
                THEN 1 ELSE 0 END
            ) AS consultas_nocturnas,

            -- 💳💵 Método de pago
            SUM(CASE WHEN pc.metodo_pago != 'efectivo' THEN 1 ELSE 0 END) AS consultas_tarjeta,
            SUM(CASE WHEN pc.metodo_pago = 'efectivo' THEN 1 ELSE 0 END) AS consultas_efectivo,

            -- 💰 Neto MP
            COALESCE(
                SUM(CASE 
                    WHEN pc.metodo_pago != 'efectivo'
                    THEN pc.medico_neto
                    ELSE 0 END
                ), 0
            ) AS neto_mp,

            -- 💰 Comisión efectivo
            COALESCE(
                SUM(CASE 
                    WHEN pc.metodo_pago = 'efectivo'
                    THEN pc.docya_comision
                    ELSE 0 END
                ), 0
            ) AS comision_efectivo,

            -- 🔥 Resultado provisorio
            COALESCE(
                SUM(CASE 
                    WHEN pc.metodo_pago != 'efectivo'
                    THEN pc.medico_neto
                    ELSE 0 END
                ), 0
            ) -
            COALESCE(
                SUM(CASE 
                    WHEN pc.metodo_pago = 'efectivo'
                    THEN pc.docya_comision
                    ELSE 0 END
                ), 0
            ) AS monto_provisorio

        FROM pagos_consulta pc
        JOIN consultas c ON c.id = pc.consulta_id
        JOIN medicos m ON m.id = pc.medico_id
        WHERE pc.fecha::date BETWEEN %s AND %s
        GROUP BY m.id, m.full_name
        ORDER BY m.full_name;
    """, (inicio_semana, fin_semana))

    rows = cur.fetchall()
    cur.close()

    return {
        "periodo": f"{inicio_semana} → {fin_semana}",
        "preview": rows
    }


# ====================================================
# 💰 LIQUIDACIONES SEMANA ACTUAL (Panel Monitoreo)
# ====================================================
# ====================================================
# 💰 LIQUIDACIONES SEMANA ACTUAL (Panel Monitoreo)
# ====================================================
from datetime import date, timedelta
from fastapi import Depends, HTTPException
from psycopg2.extras import RealDictCursor

@router.post("/liquidaciones/generar_semana_anterior")
def generar_liquidaciones_semana_anterior(db=Depends(get_db)):
    """
    Genera las liquidaciones de la semana anterior (lunes a domingo).
    Debe ejecutarse una sola vez por semana (lunes).
    """
    cur = db.cursor()

    # 📅 Semana anterior (lunes → domingo)
    hoy = date.today()
    fin_semana = hoy - timedelta(days=hoy.weekday() + 1)
    inicio_semana = fin_semana - timedelta(days=6)

    # 🛑 Evitar duplicados
    cur.execute("""
        SELECT COUNT(*)
        FROM liquidaciones_semanales
        WHERE semana_inicio = %s AND semana_fin = %s
    """, (inicio_semana, fin_semana))

    if cur.fetchone()[0] > 0:
        cur.close()
        raise HTTPException(
            status_code=409,
            detail="Las liquidaciones de esa semana ya fueron generadas"
        )

    # 🧮 Insertar liquidaciones
    cur.execute("""
        INSERT INTO liquidaciones_semanales (
            medico_id,
            semana_inicio,
            semana_fin,
            neto_mp,
            comision_efectivo,
            monto_final
        )
        SELECT
            pc.medico_id,
            %s AS semana_inicio,
            %s AS semana_fin,

            -- 💳 Neto por MercadoPago (DocYa le debe)
            COALESCE(
                SUM(
                    CASE 
                        WHEN pc.metodo_pago != 'efectivo'
                        THEN pc.medico_neto
                        ELSE 0
                    END
                ), 0
            ) AS neto_mp,

            -- 💵 Comisión de efectivo (médico le debe a DocYa)
            COALESCE(
                SUM(
                    CASE 
                        WHEN pc.metodo_pago = 'efectivo'
                        THEN pc.docya_comision
                        ELSE 0
                    END
                ), 0
            ) AS comision_efectivo,

            -- 🔥 Monto final a pagar
            COALESCE(
                SUM(
                    CASE 
                        WHEN pc.metodo_pago != 'efectivo'
                        THEN pc.medico_neto
                        ELSE 0
                    END
                ), 0
            ) -
            COALESCE(
                SUM(
                    CASE 
                        WHEN pc.metodo_pago = 'efectivo'
                        THEN pc.docya_comision
                        ELSE 0
                    END
                ), 0
            ) AS monto_final

        FROM pagos_consulta pc
        WHERE pc.fecha::date BETWEEN %s AND %s
        GROUP BY pc.medico_id
    """, (inicio_semana, fin_semana, inicio_semana, fin_semana))

    db.commit()
    cur.close()

    return {
        "ok": True,
        "semana": f"{inicio_semana} → {fin_semana}",
        "mensaje": "Liquidaciones generadas correctamente"
    }

@router.get("/liquidaciones")
def listar_liquidaciones(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT
            l.id,
            m.full_name AS medico,
            m.tipo,
            l.semana_inicio,
            l.semana_fin,
            l.neto_mp,
            l.comision_efectivo,
            l.monto_final,
            l.estado,
            l.pagado_en
        FROM liquidaciones_semanales l
        JOIN medicos m ON m.id = l.medico_id
        ORDER BY l.semana_inicio DESC, m.full_name
    """)
    data = cur.fetchall()
    cur.close()

    return {"liquidaciones": data}

@router.post("/liquidaciones/{liquidacion_id}/pagar")
def pagar_liquidacion(liquidacion_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # Obtener monto y médico
    cur.execute("""
        SELECT medico_id, monto_final, estado
        FROM liquidaciones_semanales
        WHERE id = %s
    """, (liquidacion_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        raise HTTPException(status_code=404, detail="Liquidación no encontrada")

    medico_id, monto_final, estado = row

    if estado == "pagado":
        cur.close()
        raise HTTPException(status_code=409, detail="La liquidación ya está pagada")

    # Marcar pagada
    cur.execute("""
        UPDATE liquidaciones_semanales
        SET estado = 'pagado',
            pagado_en = NOW()
        WHERE id = %s
    """, (liquidacion_id,))

    # Actualizar saldo del médico
    cur.execute("""
        INSERT INTO saldo_medico (medico_id, saldo)
        VALUES (%s, %s)
        ON CONFLICT (medico_id)
        DO UPDATE SET saldo = saldo_medico.saldo + EXCLUDED.saldo
    """, (medico_id, monto_final))

    db.commit()
    cur.close()

    return {
        "ok": True,
        "mensaje": "Liquidación pagada correctamente",
        "monto": float(monto_final)
    }


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
            AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '2 minutes');
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
async def tiempo_real(websocket: WebSocket, db=Depends(get_db)):
    await websocket.accept()
    active_admins.append(websocket)
    print(f"🟢 Admin conectado al monitoreo ({len(active_admins)} totales)")

    try:
        while True:
            await asyncio.sleep(5)
            data = obtener_estado_general(db)
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
def obtener_estado_general(db):
    cur = db.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM medicos
        WHERE disponible = TRUE
        AND (ultimo_ping IS NOT NULL AND ultimo_ping > NOW() - INTERVAL '2 minutes');
    """)
    medicos_conectados = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) 
        FROM consultas 
        WHERE estado IN ('aceptada', 'en_camino', 'en_domicilio');
    """)
    consultas_en_curso = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*)
        FROM consultas
        WHERE creado_en >= CURRENT_DATE
          AND creado_en < CURRENT_DATE + INTERVAL '1 day';
    """)
    consultas_hoy = cur.fetchone()[0]

    cur.close()

    return {
        "medicos_conectados": medicos_conectados,
        "consultas_en_curso": consultas_en_curso,
        "consultas_hoy": consultas_hoy
    }


# ====================================================
# 🚀 LANZAR LIMPIADOR AUTOMÁTICO AL INICIAR
# ====================================================




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

@router.get("/tiempo_llegada")
def tiempo_llegada(db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            ROUND(
                AVG(EXTRACT(EPOCH FROM (inicio_atencion - aceptada_en)) / 60)::numeric, 
            1)
        FROM consultas
        WHERE inicio_atencion IS NOT NULL
          AND aceptada_en IS NOT NULL;
    """)
    result = cur.fetchone()[0] or 0
    return {"tiempo_llegada_min": float(result)}


@router.get("/tiempo_llegada_promedio")
def tiempo_llegada_promedio(db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            ROUND(AVG(EXTRACT(EPOCH FROM (inicio_atencion - aceptada_en)) / 60), 1)
        FROM consultas
        WHERE inicio_atencion IS NOT NULL
          AND aceptada_en IS NOT NULL;
    """)
    result = cur.fetchone()[0] or 0
    return {"tiempo_llegada_promedio_min": result}
