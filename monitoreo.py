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

from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor
# ====================================================
# 📊LQUIDACIONES
# ====================================================
MP_FEE_RATE = 0.0761

@router.get("/liquidaciones/preview_semana_actual")
def preview_semana_actual(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            m.id AS medico_id,
            m.full_name AS medico,
            m.telefono,
            m.tipo,
            c.metodo_pago,
            c.inicio_atencion
        FROM consultas c
        JOIN medicos m ON m.id = c.medico_id
        WHERE c.estado = 'finalizada'
          AND c.inicio_atencion >= date_trunc('week', CURRENT_DATE)
          AND c.inicio_atencion < date_trunc('week', CURRENT_DATE) + INTERVAL '7 days'
    """)

    rows = cur.fetchall()
    cur.close()

    medicos = {}

    for r in rows:
        mid = r["medico_id"]
        tipo = r["tipo"]
        metodo = r["metodo_pago"]
        hora = r["inicio_atencion"].hour
        nocturna = hora >= 22 or hora < 6

        # Precio base
        if tipo == "medico":
            precio = 40000 if nocturna else 30000
        else:
            precio = 30000 if nocturna else 20000

        # MP
        neto_post_mp = precio * (1 - MP_FEE_RATE) if metodo != "efectivo" else precio

        # DocYa
        docya = neto_post_mp * 0.20
        profesional = neto_post_mp - docya

        if mid not in medicos:
            medicos[mid] = {
                "medico_id": mid,
                "medico": r["medico"],
                "telefono": r["telefono"],  # ✅
                "resumen": {
                    "cantidad_consultas": 0,
                    "total_efectivo": 0,
                    "total_digital": 0,
                    "docya_comision_total": 0,
                    "a_pagar_medico": 0,
                },
            }

        medicos[mid]["resumen"]["cantidad_consultas"] += 1
        medicos[mid]["resumen"]["docya_comision_total"] += docya

        if metodo == "efectivo":
            medicos[mid]["resumen"]["total_efectivo"] += docya
        else:
            medicos[mid]["resumen"]["total_digital"] += profesional
            medicos[mid]["resumen"]["a_pagar_medico"] += profesional

    return {"medicos": list(medicos.values())}


@router.delete("/consultas/{consulta_id}")
def eliminar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("DELETE FROM consultas WHERE id = %s", (consulta_id,))
    db.commit()
    return {"ok": True}


MP_FEE_RATE = 0.0761

@router.post("/liquidaciones/generar_semana_anterior")
def generar_liquidaciones_semana_anterior(db=Depends(get_db)):
    cur = db.cursor()

    hoy = date.today()
    inicio = hoy - timedelta(days=hoy.weekday() + 7)
    fin = inicio + timedelta(days=7)

    cur.execute("""
        INSERT INTO liquidaciones_semanales (
            medico_id,
            semana_inicio,
            semana_fin,
            neto_mp,
            comision_efectivo,
            monto_final,
            estado
        )
        SELECT
            medico_id,
            %s,
            %s,

            -- Neto digital real (post MP y post DocYa)
            SUM(
              CASE WHEN metodo_pago != 'efectivo' THEN
                (
                  (
                    CASE
                      WHEN tipo='medico' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 40000
                      WHEN tipo='medico' THEN 30000
                      WHEN tipo='enfermero' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 30000
                      ELSE 20000
                    END
                  ) * (1 - %s)
                ) * 0.80
              ELSE 0 END
            ),

            -- Comisión efectivo (solo DocYa)
            SUM(
              CASE WHEN metodo_pago='efectivo' THEN
                (
                  CASE
                    WHEN tipo='medico' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 40000
                    WHEN tipo='medico' THEN 30000
                    WHEN tipo='enfermero' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 30000
                    ELSE 20000
                  END
                ) * 0.20
              ELSE 0 END
            ),

            -- Monto final
            SUM(
              CASE WHEN metodo_pago!='efectivo' THEN
                (
                  (
                    CASE
                      WHEN tipo='medico' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 40000
                      WHEN tipo='medico' THEN 30000
                      WHEN tipo='enfermero' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 30000
                      ELSE 20000
                    END
                  ) * (1 - %s)
                ) * 0.80
              ELSE
                (
                  CASE
                    WHEN tipo='medico' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 40000
                    WHEN tipo='medico' THEN 30000
                    WHEN tipo='enfermero' AND (EXTRACT(HOUR FROM inicio_atencion)>=22 OR EXTRACT(HOUR FROM inicio_atencion)<6) THEN 30000
                    ELSE 20000
                  END
                ) * 0.80
              END
            ),

            'pendiente'
        FROM consultas
        WHERE estado='finalizada'
          AND inicio_atencion>=%s AND inicio_atencion<%s
        GROUP BY medico_id
    """, (inicio, fin, MP_FEE_RATE, MP_FEE_RATE, inicio, fin))

    db.commit()
    cur.close()
    return {"ok": True, "semana": f"{inicio} → {fin}"}


@router.get("/liquidaciones")
def listar_liquidaciones(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            l.id,
            m.full_name AS medico,
            m.telefono,
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
        ORDER BY l.semana_inicio DESC
    """)

    data = cur.fetchall()
    cur.close()

    return {"liquidaciones": data}


@router.get("/liquidaciones/medico/{medico_id}")
def detalle_liquidacion_medico(medico_id: int, semana_inicio: date, semana_fin: date, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            inicio_atencion,
            metodo_pago,
            tipo,
            CASE
              WHEN tipo = 'medico' AND (EXTRACT(HOUR FROM inicio_atencion) >= 22 OR EXTRACT(HOUR FROM inicio_atencion) < 6) THEN 32000
              WHEN tipo = 'medico' THEN 24000
              WHEN tipo = 'enfermero' AND (EXTRACT(HOUR FROM inicio_atencion) >= 22 OR EXTRACT(HOUR FROM inicio_atencion) < 6) THEN 24000
              ELSE 16000
            END AS neto_profesional,
            CASE
              WHEN tipo = 'medico' AND (EXTRACT(HOUR FROM inicio_atencion) >= 22 OR EXTRACT(HOUR FROM inicio_atencion) < 6) THEN 8000
              WHEN tipo = 'medico' THEN 6000
              WHEN tipo = 'enfermero' AND (EXTRACT(HOUR FROM inicio_atencion) >= 22 OR EXTRACT(HOUR FROM inicio_atencion) < 6) THEN 6000
              ELSE 4000
            END AS comision_docya
        FROM consultas
        WHERE estado = 'finalizada'
          AND medico_id = %s
          AND inicio_atencion >= %s
          AND inicio_atencion < %s
        ORDER BY inicio_atencion
    """, (medico_id, semana_inicio, semana_fin))

    consultas = cur.fetchall()
    cur.close()

    return {"consultas": consultas}


@router.post("/liquidaciones/{id}/pagar")
def pagar_liquidacion(id: int, db=Depends(get_db)):
    cur = db.cursor()

    cur.execute("""
        UPDATE liquidaciones_semanales
        SET estado = 'pagado',
            pagado_en = NOW()
        WHERE id = %s AND estado != 'pagado'
        RETURNING monto_final
    """, (id,))

    row = cur.fetchone()
    if not row:
        cur.close()
        raise HTTPException(404, "Liquidación no encontrada o ya pagada")

    db.commit()
    cur.close()

    return {
        "ok": True,
        "monto": float(row[0])
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
                ultimo_ping,        
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
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (fin_atencion - inicio_atencion)) / 60),
                1) AS tiempo_promedio_min
            FROM consultas
            WHERE estado = 'finalizada'
              AND inicio_atencion IS NOT NULL
              AND fin_atencion IS NOT NULL;
        """)
        resultado = cur.fetchone()
        cur.close()

        return {
            "tiempo_promedio_min": float(resultado["tiempo_promedio_min"] or 0)
        }

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

@router.get("/profesionales_conectados")
def profesionales_conectados(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            id,
            full_name AS nombre,
            tipo,               -- medico | enfermero
            especialidad,
            telefono,
            matricula,
            latitud AS lat,
            longitud AS lng,
            ultimo_ping,
            CASE
              WHEN ultimo_ping > NOW() - INTERVAL '2 minutes' THEN 'activo'
              WHEN ultimo_ping > NOW() - INTERVAL '5 minutes' THEN 'latente'
              ELSE 'offline'
            END AS estado
        FROM medicos
        WHERE disponible = TRUE
          AND ultimo_ping > NOW() - INTERVAL '5 minutes'
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
        ORDER BY ultimo_ping DESC
    """)

    profesionales = cur.fetchall()
    cur.close()

    return {
        "ok": True,
        "total": len(profesionales),
        "profesionales": profesionales
    }


