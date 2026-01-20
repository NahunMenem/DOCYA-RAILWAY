from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import time, date
from typing import List, Optional
from psycopg2.extras import RealDictCursor

from main import get_db, now_argentina

router = APIRouter(prefix="/pastillero", tags=["Pastillero"])

class MedicacionIn(BaseModel):
    paciente_uuid: str
    nombre: str
    dosis: str
    horarios: List[time]
    fecha_inicio: date
    fecha_fin: Optional[date] = None

@router.post("/medicacion")
def crear_medicacion(data: MedicacionIn, db=Depends(get_db)):
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO medicaciones (
                paciente_uuid, nombre, dosis,
                horarios, fecha_inicio, fecha_fin
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.paciente_uuid,
            data.nombre,
            data.dosis,
            data.horarios,
            data.fecha_inicio,
            data.fecha_fin
        ))
        med_id = cur.fetchone()[0]
        db.commit()
        return {"ok": True, "medicacion_id": med_id}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))

