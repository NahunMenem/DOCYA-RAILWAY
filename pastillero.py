# ====================================================
# 💊 PASTILLERO DOCYA
# ====================================================
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, time, datetime
from psycopg2.extras import RealDictCursor

from main import get_db, now_argentina

router = APIRouter(prefix="/pastillero", tags=["Pastillero"])

# ====================================================
# 📦 MODELOS PYDANTIC
# ====================================================

class MedicacionIn(BaseModel):
    paciente_uuid: str
    nombre: str
    dosis: str
    horarios: List[time]     # ej: ["08:00", "20:00"]
    fecha_inicio: date
    fecha_fin: Optional[date] = None
    observaciones: Optional[str] = None


class TomaConfirmarIn(BaseModel):
    toma_id: int


# ====================================================
# 🧪 CREAR MEDICACIÓN
# ====================================================
from datetime import datetime
from psycopg2.extras import RealDictCursor

@router.post("/medicacion")
def crear_medicacion(data: MedicacionIn, db=Depends(get_db)):
    cur = db.cursor()
    try:
        # 1️⃣ Insertar medicación (SOLO columnas existentes)
        cur.execute("""
            INSERT INTO medicaciones (
                paciente_uuid,
                nombre,
                dosis,
                horarios,
                fecha_inicio,
                fecha_fin
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.paciente_uuid,
            data.nombre,
            data.dosis,
            [datetime.strptime(h, "%H:%M").time() for h in data.horarios],
            data.fecha_inicio,
            data.fecha_fin
        ))

        medicacion_id = cur.fetchone()[0]

        # 2️⃣ Crear tomas SOLO para hoy (si corresponde)
        hoy = now_argentina().date()

        if hoy >= data.fecha_inicio and (data.fecha_fin is None or hoy <= data.fecha_fin):
            for h in data.horarios:
                cur.execute("""
                    INSERT INTO tomas (
                        medicacion_id,
                        fecha,
                        horario_programado,
                        tomado
                    )
                    VALUES (%s,%s,%s,FALSE)
                """, (
                    medicacion_id,
                    hoy,
                    datetime.strptime(h, "%H:%M").time()
                ))

        db.commit()
        return {"ok": True, "medicacion_id": medicacion_id}

    except Exception as e:
        db.rollback()
        print("❌ Error creando medicación:", e)
        raise HTTPException(status_code=500, detail=str(e))


# ====================================================
# 📋 LISTAR MEDICACIONES DEL PACIENTE
# ====================================================

@router.get("/medicaciones/{paciente_uuid}")
def listar_medicaciones(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT *
        FROM medicaciones
        WHERE paciente_uuid = %s
        ORDER BY created_at DESC
    """, (paciente_uuid,))
    return cur.fetchall()


# ====================================================
# ⏰ TOMAS DEL DÍA
# ====================================================

@router.get("/tomas/hoy/{paciente_uuid}")
def tomas_hoy(paciente_uuid: str, db=Depends(get_db)):
    hoy = now_argentina().date()
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT t.*, m.nombre, m.dosis
        FROM tomas t
        JOIN medicaciones m ON m.id = t.medicacion_id
        WHERE m.paciente_uuid = %s
          AND t.fecha = %s
        ORDER BY t.horario_programado
    """, (paciente_uuid, hoy))
    return cur.fetchall()


# ====================================================
# ✅ CONFIRMAR TOMA
# ====================================================

@router.post("/toma/confirmar")
def confirmar_toma(data: TomaConfirmarIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE tomas
        SET tomado = TRUE,
            hora_toma = %s
        WHERE id = %s
    """, (now_argentina(), data.toma_id))

    if cur.rowcount == 0:
        raise HTTPException(404, "Toma no encontrada")

    db.commit()
    return {"ok": True}

