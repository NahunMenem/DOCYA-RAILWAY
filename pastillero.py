from datetime import date, datetime, time, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from psycopg2.extras import RealDictCursor

from database import get_db
from settings import now_argentina

router = APIRouter(prefix="/pastillero", tags=["Pastillero"])
_tables_ready = False


class MedicacionIn(BaseModel):
    paciente_uuid: Optional[str] = None
    consulta_id: Optional[int] = None
    medico_id: Optional[int] = None
    nombre: str
    dosis: str
    frecuencia: Optional[str] = None
    horarios: List[time]
    fecha_inicio: date
    fecha_fin: Optional[date] = None
    observaciones: Optional[str] = None

    @field_validator("horarios")
    @classmethod
    def validar_horarios(cls, horarios: List[time]) -> List[time]:
        if not horarios:
            raise ValueError("Debes indicar al menos un horario")
        return sorted(horarios)


class TomaActualizarIn(BaseModel):
    toma_id: int
    estado: Literal["tomado", "omitido"]


class TomaConfirmarIn(BaseModel):
    toma_id: int


class MedicacionPatchIn(BaseModel):
    paciente_uuid: Optional[str] = None
    consulta_id: Optional[int] = None
    medico_id: Optional[int] = None
    nombre: str
    dosis: str
    frecuencia: Optional[str] = None
    horarios: List[time]
    fecha_inicio: date
    fecha_fin: Optional[date] = None
    observaciones: Optional[str] = None

    @field_validator("horarios")
    @classmethod
    def validar_horarios(cls, horarios: List[time]) -> List[time]:
        if not horarios:
            raise ValueError("Debes indicar al menos un horario")
        return sorted(horarios)


def _dict_cur(db):
    return db.cursor(cursor_factory=RealDictCursor)


def _ensure_tables(db) -> None:
    global _tables_ready
    if _tables_ready:
        return

    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS medicaciones (
            id SERIAL PRIMARY KEY,
            paciente_uuid UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            consulta_id INTEGER NULL REFERENCES consultas(id) ON DELETE SET NULL,
            medico_id INTEGER NULL REFERENCES medicos(id) ON DELETE SET NULL,
            nombre TEXT NOT NULL,
            dosis TEXT NOT NULL,
            frecuencia TEXT NULL,
            horarios TIME[] NOT NULL,
            fecha_inicio DATE NOT NULL,
            fecha_fin DATE NULL,
            observaciones TEXT NULL,
            activa BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS consulta_id INTEGER NULL REFERENCES consultas(id) ON DELETE SET NULL
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS medico_id INTEGER NULL REFERENCES medicos(id) ON DELETE SET NULL
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS frecuencia TEXT NULL
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS observaciones TEXT NULL
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS activa BOOLEAN NOT NULL DEFAULT TRUE
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )
    cur.execute(
        """
        ALTER TABLE medicaciones
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tomas (
            id SERIAL PRIMARY KEY,
            medicacion_id INTEGER NOT NULL REFERENCES medicaciones(id) ON DELETE CASCADE,
            fecha DATE NOT NULL,
            horario_programado TIME NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            hora_toma TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(medicacion_id, fecha, horario_programado)
        );
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'pendiente'
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS hora_toma TIMESTAMP NULL
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS recordatorio_push_enviado BOOLEAN NOT NULL DEFAULT FALSE
        """
    )
    cur.execute(
        """
        ALTER TABLE tomas
        ADD COLUMN IF NOT EXISTS recordatorio_push_enviado_en TIMESTAMP NULL
        """
    )
    cur.execute(
        """
        DELETE FROM tomas a
        USING tomas b
        WHERE a.ctid < b.ctid
          AND a.medicacion_id = b.medicacion_id
          AND a.fecha = b.fecha
          AND a.horario_programado = b.horario_programado
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tomas_unique_slot
        ON tomas(medicacion_id, fecha, horario_programado)
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_medicaciones_paciente ON medicaciones(paciente_uuid);"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_tomas_fecha_medicacion ON tomas(fecha, medicacion_id);"
    )
    db.commit()
    cur.close()
    _tables_ready = True


def procesar_recordatorios_push_pastillero(db, enviar_push_fn) -> int:
    """Envía pushes de tomas pendientes cercanas para reforzar alarmas locales."""
    _ensure_tables(db)

    ahora = now_argentina().replace(tzinfo=None)
    desde = ahora - timedelta(seconds=90)
    hasta = ahora + timedelta(seconds=30)

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT
            t.id,
            t.fecha,
            t.horario_programado,
            m.nombre,
            m.dosis,
            u.full_name,
            u.fcm_token
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        JOIN users u ON u.id = m.paciente_uuid
        WHERE t.estado = 'pendiente'
          AND COALESCE(u.fcm_token, '') <> ''
          AND COALESCE(t.recordatorio_push_enviado, FALSE) = FALSE
          AND (t.fecha + t.horario_programado) BETWEEN %s AND %s
        ORDER BY t.fecha, t.horario_programado
        """,
        (desde, hasta),
    )
    rows = cur.fetchall()
    cur.close()

    enviados = 0
    update_cur = db.cursor()

    for row in rows:
        toma_id = row["id"]
        horario = row["horario_programado"]
        hora_label = horario.strftime("%H:%M") if horario else ""
        titulo = "Recordatorio de medicacion"
        cuerpo = f'{row["nombre"]} - {row["dosis"]} ({hora_label})'.strip()

        try:
            enviar_push_fn(
                row["fcm_token"],
                titulo,
                cuerpo,
                {
                    "tipo": "medication_reminder",
                    "toma_id": str(toma_id),
                    "nombre": str(row["nombre"] or ""),
                    "dosis": str(row["dosis"] or ""),
                    "horario": hora_label,
                },
                android_channel_id="medication_reminders_v2",
                android_sound="alerta",
                apns_sound="default",
            )
            update_cur.execute(
                """
                UPDATE tomas
                SET recordatorio_push_enviado = TRUE,
                    recordatorio_push_enviado_en = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (toma_id,),
            )
            enviados += 1
        except Exception as exc:
            print(f"⚠️ Error enviando push pastillero toma {toma_id}: {exc}")

    db.commit()
    update_cur.close()
    return enviados


def _resolve_paciente_uuid(data: MedicacionIn, db) -> tuple[str, Optional[int]]:
    if data.paciente_uuid:
        return data.paciente_uuid, data.consulta_id

    if not data.consulta_id:
        raise HTTPException(
            status_code=400,
            detail="Debes informar paciente_uuid o consulta_id",
        )

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT id, paciente_uuid, medico_id
        FROM consultas
        WHERE id = %s
        """,
        (data.consulta_id,),
    )
    consulta = cur.fetchone()
    cur.close()

    if not consulta:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    if data.medico_id is not None and consulta["medico_id"] != data.medico_id:
        raise HTTPException(
            status_code=403,
            detail="La consulta no corresponde a ese profesional",
        )

    return str(consulta["paciente_uuid"]), data.consulta_id


def _iter_dates(desde: date, hasta: date):
    actual = desde
    while actual <= hasta:
        yield actual
        actual += timedelta(days=1)


def _fetch_medicacion(db, medicacion_id: int):
    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT *
        FROM medicaciones
        WHERE id = %s
        """,
        (medicacion_id,),
    )
    medicacion = cur.fetchone()
    cur.close()
    if not medicacion:
        raise HTTPException(status_code=404, detail="Medicacion no encontrada")
    return medicacion


def _sincronizar_tomas(
    db,
    paciente_uuid: str,
    desde: date,
    hasta: date,
) -> None:
    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT id, horarios, fecha_inicio, fecha_fin
        FROM medicaciones
        WHERE paciente_uuid = %s
          AND activa = TRUE
          AND fecha_inicio <= %s
          AND (fecha_fin IS NULL OR fecha_fin >= %s)
        """,
        (paciente_uuid, hasta, desde),
    )
    medicaciones = cur.fetchall()

    insert_cur = db.cursor()
    for medicacion in medicaciones:
        rango_inicio = max(desde, medicacion["fecha_inicio"])
        rango_fin = min(hasta, medicacion["fecha_fin"] or hasta)
        for dia in _iter_dates(rango_inicio, rango_fin):
            for horario in medicacion["horarios"] or []:
                insert_cur.execute(
                    """
                    INSERT INTO tomas (
                        medicacion_id,
                        fecha,
                        horario_programado,
                        estado
                    )
                    VALUES (%s, %s, %s, 'pendiente')
                    ON CONFLICT (medicacion_id, fecha, horario_programado) DO NOTHING
                    """,
                    (medicacion["id"], dia, horario),
                )

    db.commit()
    insert_cur.close()
    cur.close()


def _borrar_tomas_futuras(db, medicacion_id: int, desde: date) -> None:
    cur = db.cursor()
    cur.execute(
        """
        DELETE FROM tomas
        WHERE medicacion_id = %s
          AND fecha >= %s
          AND estado = 'pendiente'
        """,
        (medicacion_id, desde),
    )
    db.commit()
    cur.close()


@router.post("/admin/setup")
def setup_pastillero(db=Depends(get_db)):
    _ensure_tables(db)
    return {"ok": True}


@router.post("/medicacion")
def crear_medicacion(data: MedicacionIn, db=Depends(get_db)):
    _ensure_tables(db)

    if data.fecha_fin is not None and data.fecha_fin < data.fecha_inicio:
        raise HTTPException(
            status_code=400,
            detail="fecha_fin no puede ser anterior a fecha_inicio",
        )

    paciente_uuid, consulta_id = _resolve_paciente_uuid(data, db)
    cur = db.cursor()

    try:
        cur.execute(
            """
            INSERT INTO medicaciones (
                paciente_uuid,
                consulta_id,
                medico_id,
                nombre,
                dosis,
                frecuencia,
                horarios,
                fecha_inicio,
                fecha_fin,
                observaciones
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                paciente_uuid,
                consulta_id,
                data.medico_id,
                data.nombre.strip(),
                data.dosis.strip(),
                (data.frecuencia or "").strip() or None,
                data.horarios,
                data.fecha_inicio,
                data.fecha_fin,
                (data.observaciones or "").strip() or None,
            ),
        )
        medicacion_id = cur.fetchone()[0]
        db.commit()
        _sincronizar_tomas(
            db,
            paciente_uuid,
            min(data.fecha_inicio, now_argentina().date()),
            min(
                data.fecha_fin or (now_argentina().date() + timedelta(days=14)),
                now_argentina().date() + timedelta(days=14),
            ),
        )
        return {"ok": True, "medicacion_id": medicacion_id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()


@router.put("/medicacion/{medicacion_id}")
def editar_medicacion(
    medicacion_id: int,
    data: MedicacionPatchIn,
    db=Depends(get_db),
):
    _ensure_tables(db)

    if data.fecha_fin is not None and data.fecha_fin < data.fecha_inicio:
        raise HTTPException(
            status_code=400,
            detail="fecha_fin no puede ser anterior a fecha_inicio",
        )

    actual = _fetch_medicacion(db, medicacion_id)
    paciente_uuid = data.paciente_uuid or str(actual["paciente_uuid"])
    consulta_id = data.consulta_id if data.consulta_id is not None else actual.get("consulta_id")

    if data.consulta_id is not None or data.medico_id is not None:
        paciente_uuid, consulta_id = _resolve_paciente_uuid(
            MedicacionIn(
                paciente_uuid=data.paciente_uuid,
                consulta_id=data.consulta_id,
                medico_id=data.medico_id,
                nombre=data.nombre,
                dosis=data.dosis,
                frecuencia=data.frecuencia,
                horarios=data.horarios,
                fecha_inicio=data.fecha_inicio,
                fecha_fin=data.fecha_fin,
                observaciones=data.observaciones,
            ),
            db,
        )

    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE medicaciones
            SET paciente_uuid = %s,
                consulta_id = %s,
                medico_id = %s,
                nombre = %s,
                dosis = %s,
                frecuencia = %s,
                horarios = %s,
                fecha_inicio = %s,
                fecha_fin = %s,
                observaciones = %s,
                activa = TRUE,
                updated_at = NOW()
            WHERE id = %s
            RETURNING id
            """,
            (
                paciente_uuid,
                consulta_id,
                data.medico_id if data.medico_id is not None else actual.get("medico_id"),
                data.nombre.strip(),
                data.dosis.strip(),
                (data.frecuencia or "").strip() or None,
                data.horarios,
                data.fecha_inicio,
                data.fecha_fin,
                (data.observaciones or "").strip() or None,
                medicacion_id,
            ),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Medicacion no encontrada")
        db.commit()
        _borrar_tomas_futuras(db, medicacion_id, now_argentina().date())
        _sincronizar_tomas(
            db,
            paciente_uuid,
            min(data.fecha_inicio, now_argentina().date()),
            min(
                data.fecha_fin or (now_argentina().date() + timedelta(days=14)),
                now_argentina().date() + timedelta(days=14),
            ),
        )
        return {"ok": True, "medicacion_id": medicacion_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()


@router.delete("/medicacion/{medicacion_id}")
def eliminar_medicacion(medicacion_id: int, db=Depends(get_db)):
    _ensure_tables(db)
    medicacion = _fetch_medicacion(db, medicacion_id)
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE medicaciones
            SET activa = FALSE,
                updated_at = NOW()
            WHERE id = %s
            """,
            (medicacion_id,),
        )
        cur.execute(
            """
            DELETE FROM tomas
            WHERE medicacion_id = %s
              AND fecha >= %s
              AND estado = 'pendiente'
            """,
            (medicacion_id, now_argentina().date()),
        )
        db.commit()
        return {
            "ok": True,
            "medicacion_id": medicacion_id,
            "paciente_uuid": str(medicacion["paciente_uuid"]),
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()


@router.get("/medicaciones/{paciente_uuid}")
def listar_medicaciones(paciente_uuid: str, db=Depends(get_db)):
    _ensure_tables(db)
    hoy = now_argentina().date()
    _sincronizar_tomas(db, paciente_uuid, hoy, hoy + timedelta(days=14))

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT
            m.*,
            COALESCE(SUM(CASE WHEN t.estado = 'tomado' THEN 1 ELSE 0 END), 0) AS tomas_tomadas,
            COALESCE(SUM(CASE WHEN t.estado = 'omitido' THEN 1 ELSE 0 END), 0) AS tomas_omitidas,
            COALESCE(COUNT(t.id), 0) AS tomas_totales
        FROM medicaciones m
        LEFT JOIN tomas t ON t.medicacion_id = m.id
        WHERE m.paciente_uuid = %s
          AND m.activa = TRUE
        GROUP BY m.id
        ORDER BY m.activa DESC, m.created_at DESC
        """,
        (paciente_uuid,),
    )
    data = cur.fetchall()
    cur.close()
    return {"ok": True, "medicaciones": data}


@router.get("/tomas/hoy/{paciente_uuid}")
def tomas_hoy(paciente_uuid: str, db=Depends(get_db)):
    _ensure_tables(db)
    hoy = now_argentina().date()
    _sincronizar_tomas(db, paciente_uuid, hoy, hoy)

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT
            t.*,
            m.nombre,
            m.dosis,
            m.frecuencia,
            m.observaciones,
            m.medico_id,
            m.consulta_id
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        WHERE m.paciente_uuid = %s
          AND t.fecha = %s
        ORDER BY t.horario_programado
        """,
        (paciente_uuid, hoy),
    )
    tomas = cur.fetchall()
    cur.close()
    return {"ok": True, "tomas": tomas}


@router.get("/agenda/{paciente_uuid}")
def agenda_medicacion(
    paciente_uuid: str,
    dias: int = 7,
    db=Depends(get_db),
):
    _ensure_tables(db)
    dias = max(1, min(dias, 21))
    hoy = now_argentina().date()
    hasta = hoy + timedelta(days=dias - 1)
    _sincronizar_tomas(db, paciente_uuid, hoy, hasta)

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT
            t.id,
            t.fecha,
            t.horario_programado,
            t.estado,
            t.hora_toma,
            m.id AS medicacion_id,
            m.nombre,
            m.dosis,
            m.frecuencia,
            m.observaciones
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        WHERE m.paciente_uuid = %s
          AND t.fecha BETWEEN %s AND %s
        ORDER BY t.fecha, t.horario_programado
        """,
        (paciente_uuid, hoy, hasta),
    )
    agenda = cur.fetchall()
    cur.close()
    return {"ok": True, "agenda": agenda}


@router.get("/historial/{paciente_uuid}")
def historial_adherencia(
    paciente_uuid: str,
    days: int = 30,
    db=Depends(get_db),
):
    _ensure_tables(db)
    days = max(1, min(days, 120))
    hoy = now_argentina().date()
    desde = hoy - timedelta(days=days - 1)
    _sincronizar_tomas(db, paciente_uuid, desde, hoy)

    cur = _dict_cur(db)
    cur.execute(
        """
        SELECT
            t.id,
            t.fecha,
            t.horario_programado,
            t.estado,
            t.hora_toma,
            m.nombre,
            m.dosis
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        WHERE m.paciente_uuid = %s
          AND t.fecha BETWEEN %s AND %s
        ORDER BY t.fecha DESC, t.horario_programado DESC
        """,
        (paciente_uuid, desde, hoy),
    )
    historial = cur.fetchall()

    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN t.estado = 'tomado' THEN 1 ELSE 0 END), 0) AS tomadas,
            COALESCE(SUM(CASE WHEN t.estado = 'omitido' THEN 1 ELSE 0 END), 0) AS omitidas
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        WHERE m.paciente_uuid = %s
          AND t.fecha BETWEEN %s AND %s
        """,
        (paciente_uuid, desde, hoy),
    )
    resumen = cur.fetchone()
    cur.close()

    total = int(resumen["total"] or 0)
    tomadas = int(resumen["tomadas"] or 0)
    omitidas = int(resumen["omitidas"] or 0)
    adherencia = round((tomadas / total) * 100, 1) if total else 0.0

    return {
        "ok": True,
        "desde": desde,
        "hasta": hoy,
        "resumen": {
            "total": total,
            "tomadas": tomadas,
            "omitidas": omitidas,
            "pendientes": max(total - tomadas - omitidas, 0),
            "adherencia_pct": adherencia,
        },
        "historial": historial,
    }


@router.post("/toma/actualizar")
def actualizar_toma(data: TomaActualizarIn, db=Depends(get_db)):
    _ensure_tables(db)
    cur = db.cursor()
    timestamp = now_argentina() if data.estado == "tomado" else None
    cur.execute(
        """
        UPDATE tomas
        SET estado = %s,
            hora_toma = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id
        """,
        (data.estado, timestamp, data.toma_id),
    )
    row = cur.fetchone()
    if not row:
        db.rollback()
        cur.close()
        raise HTTPException(status_code=404, detail="Toma no encontrada")

    db.commit()
    cur.close()
    return {"ok": True, "toma_id": row[0], "estado": data.estado}


@router.post("/toma/confirmar")
def confirmar_toma_legacy(data: TomaConfirmarIn, db=Depends(get_db)):
    return actualizar_toma(
        TomaActualizarIn(toma_id=data.toma_id, estado="tomado"),
        db=db,
    )
