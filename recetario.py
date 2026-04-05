# ====================================================
# 📋 RECETARIO — Pacientes y Recetas por Médico
# ====================================================
# Endpoints:
#   POST   /recetario/pacientes               → Crear paciente
#   GET    /recetario/pacientes               → Listar mis pacientes
#   GET    /recetario/pacientes/{id}          → Ver paciente
#   PUT    /recetario/pacientes/{id}          → Editar paciente
#   DELETE /recetario/pacientes/{id}          → Eliminar paciente
#
#   POST   /recetario/recetas                 → Emitir receta
#   GET    /recetario/recetas                 → Mis recetas (historial)
#   GET    /recetario/recetas/{id}            → Ver receta (JSON)
#   GET    /recetario/recetas/{id}/html       → Ver receta (HTML imprimible)
#   PATCH  /recetario/recetas/{id}/anular     → Anular receta
#
#   GET    /recetario/verificar/{uuid}        → Verificar autenticidad pública
# ====================================================

import os
import jwt
import psycopg2
from datetime import datetime
from typing import Optional, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET   = os.getenv("JWT_SECRET", "change_me")

router = APIRouter(prefix="/recetario", tags=["Recetario"])


# ====================================================
# 🧩 DB
# ====================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


# ====================================================
# 🔐 AUTH — extrae medico_id del JWT Bearer
# ====================================================
def get_medico_id(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),          # permite ?token= en la URL
) -> int:
    # Prioridad: header Authorization > query param ?token=
    raw = None
    if authorization and authorization.startswith("Bearer "):
        raw = authorization.split(" ", 1)[1]
    elif token:
        raw = token

    if not raw:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(raw, JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")


# ====================================================
# 📦 MODELOS Pydantic
# ====================================================
TIPOS_DOC = ["DNI", "CI", "Pasaporte", "LC", "LE"]
SEXOS     = ["M", "F", "X"]

class PacienteIn(BaseModel):
    nombre:          str
    apellido:        str
    tipo_documento:  str = "DNI"
    nro_documento:   str
    sexo:            str
    fecha_nacimiento: Optional[str] = None   # "YYYY-MM-DD"
    telefono:        Optional[str] = None
    email:           Optional[str] = None
    obra_social:     Optional[str] = None
    plan:            Optional[str] = None
    nro_credencial:  Optional[str] = None
    cuil:            Optional[str] = None
    observaciones:   Optional[str] = None
    paciente_uuid:   Optional[str] = None

class MedicamentoItem(BaseModel):
    nombre:         str                       # nombre_comercial o principio activo
    concentracion:  Optional[str] = None
    presentacion:   Optional[str] = None      # "Envase x 30 comprimidos"
    cantidad:       int = 1
    indicaciones:   str                       # "Tomar 1 cada 8hs por 7 días"

class RecetaIn(BaseModel):
    paciente_id:    int
    obra_social:    Optional[str] = None
    plan:           Optional[str] = None
    nro_credencial: Optional[str] = None
    diagnostico:    Optional[str] = None
    medicamentos:   List[MedicamentoItem]

class AnularIn(BaseModel):
    motivo: Optional[str] = None


def _ensure_recetario_patient_columns(db) -> None:
    """Agrega compatibilidad opcional con pacientes DocYa sin romper la web."""
    cur = db.cursor()
    cur.execute("ALTER TABLE recetario_pacientes ADD COLUMN IF NOT EXISTS paciente_uuid UUID")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recetario_pacientes_paciente_uuid
        ON recetario_pacientes (paciente_uuid)
        """
    )
    db.commit()


# ====================================================
# 👤 PACIENTES
# ====================================================

@router.post("/pacientes", status_code=201)
def crear_paciente(
    data: PacienteIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Registra un nuevo paciente vinculado al médico autenticado."""
    _ensure_recetario_patient_columns(db)
    if data.tipo_documento not in TIPOS_DOC:
        raise HTTPException(400, f"tipo_documento inválido. Opciones: {TIPOS_DOC}")
    if data.sexo not in SEXOS:
        raise HTTPException(400, f"sexo inválido. Opciones: {SEXOS}")

    cur = db.cursor()

    # Verificar duplicado por médico + tipo + nro
    cur.execute("""
        SELECT id FROM recetario_pacientes
        WHERE medico_id=%s AND tipo_documento=%s AND nro_documento=%s
    """, (medico_id, data.tipo_documento, data.nro_documento.strip()))
    if cur.fetchone():
        raise HTTPException(409, "Ya existe un paciente con ese documento en tu listado")

    cur.execute("""
        INSERT INTO recetario_pacientes
            (medico_id, nombre, apellido, tipo_documento, nro_documento,
             sexo, fecha_nacimiento, telefono, email,
             obra_social, plan, nro_credencial, cuil, observaciones, paciente_uuid)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, creado_en
    """, (
        medico_id,
        data.nombre.strip().title(),
        data.apellido.strip().title(),
        data.tipo_documento,
        data.nro_documento.strip(),
        data.sexo,
        data.fecha_nacimiento or None,
        data.telefono,
        data.email.lower().strip() if data.email else None,
        data.obra_social,
        data.plan,
        data.nro_credencial,
        data.cuil,
        data.observaciones,
        data.paciente_uuid
    ))
    row = cur.fetchone()
    db.commit()
    return {"ok": True, "paciente_id": row[0], "creado_en": str(row[1])}


@router.get("/pacientes")
def listar_pacientes(
    q: Optional[str] = None,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Lista todos los pacientes del médico. Filtra por nombre/documento con ?q="""
    _ensure_recetario_patient_columns(db)
    cur = db.cursor()
    if q:
        filtro = f"%{q.strip()}%"
        cur.execute("""
            SELECT id, nombre, apellido, tipo_documento, nro_documento,
                   sexo, fecha_nacimiento, telefono, email,
                   obra_social, plan, nro_credencial, cuil, observaciones, creado_en, paciente_uuid
            FROM recetario_pacientes
            WHERE medico_id=%s
              AND (
                lower(nombre)        LIKE lower(%s)
                OR lower(apellido)   LIKE lower(%s)
                OR nro_documento     LIKE %s
                OR lower(email)      LIKE lower(%s)
              )
            ORDER BY apellido, nombre
        """, (medico_id, filtro, filtro, filtro, filtro))
    else:
        cur.execute("""
            SELECT id, nombre, apellido, tipo_documento, nro_documento,
                   sexo, fecha_nacimiento, telefono, email,
                   obra_social, plan, nro_credencial, cuil, observaciones, creado_en, paciente_uuid
            FROM recetario_pacientes
            WHERE medico_id=%s
            ORDER BY apellido, nombre
        """, (medico_id,))

    cols = ["id","nombre","apellido","tipo_documento","nro_documento",
            "sexo","fecha_nacimiento","telefono","email",
            "obra_social","plan","nro_credencial","cuil","observaciones","creado_en","paciente_uuid"]
    pacientes = []
    for row in cur.fetchall():
        p = dict(zip(cols, row))
        if p["fecha_nacimiento"]:
            p["fecha_nacimiento"] = str(p["fecha_nacimiento"])
        p["creado_en"] = str(p["creado_en"])
        pacientes.append(p)

    return {"total": len(pacientes), "pacientes": pacientes}


@router.get("/pacientes/{paciente_id}")
def ver_paciente(
    paciente_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    _ensure_recetario_patient_columns(db)
    cur = db.cursor()
    cur.execute("""
        SELECT id, nombre, apellido, tipo_documento, nro_documento,
               sexo, fecha_nacimiento, telefono, email,
               obra_social, plan, nro_credencial, cuil, observaciones, creado_en, paciente_uuid
        FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (paciente_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Paciente no encontrado")

    cols = ["id","nombre","apellido","tipo_documento","nro_documento",
            "sexo","fecha_nacimiento","telefono","email",
            "obra_social","plan","nro_credencial","cuil","observaciones","creado_en","paciente_uuid"]
    p = dict(zip(cols, row))
    if p["fecha_nacimiento"]:
        p["fecha_nacimiento"] = str(p["fecha_nacimiento"])
    p["creado_en"] = str(p["creado_en"])
    return p


@router.put("/pacientes/{paciente_id}")
def editar_paciente(
    paciente_id: int,
    data: PacienteIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    _ensure_recetario_patient_columns(db)
    if data.tipo_documento not in TIPOS_DOC:
        raise HTTPException(400, f"tipo_documento inválido. Opciones: {TIPOS_DOC}")
    if data.sexo not in SEXOS:
        raise HTTPException(400, f"sexo inválido. Opciones: {SEXOS}")

    cur = db.cursor()
    cur.execute("""
        UPDATE recetario_pacientes SET
            nombre=%s, apellido=%s, tipo_documento=%s, nro_documento=%s,
            sexo=%s, fecha_nacimiento=%s, telefono=%s, email=%s,
            obra_social=%s, plan=%s, nro_credencial=%s, cuil=%s,
            observaciones=%s, paciente_uuid=%s, updated_at=NOW()
        WHERE id=%s AND medico_id=%s
        RETURNING id
    """, (
        data.nombre.strip().title(),
        data.apellido.strip().title(),
        data.tipo_documento,
        data.nro_documento.strip(),
        data.sexo,
        data.fecha_nacimiento or None,
        data.telefono,
        data.email.lower().strip() if data.email else None,
        data.obra_social,
        data.plan,
        data.nro_credencial,
        data.cuil,
        data.observaciones,
        data.paciente_uuid,
        paciente_id,
        medico_id
    ))
    if not cur.fetchone():
        db.rollback()
        raise HTTPException(404, "Paciente no encontrado o sin permiso")
    db.commit()
    return {"ok": True}


@router.delete("/pacientes/{paciente_id}", status_code=200)
def eliminar_paciente(
    paciente_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    cur = db.cursor()
    # Verificar que no tenga recetas activas
    cur.execute("""
        SELECT COUNT(*) FROM recetario_recetas
        WHERE paciente_id=%s AND estado='valida'
    """, (paciente_id,))
    if cur.fetchone()[0] > 0:
        raise HTTPException(400, "El paciente tiene recetas activas. Anulá las recetas primero.")

    cur.execute("""
        DELETE FROM recetario_pacientes WHERE id=%s AND medico_id=%s RETURNING id
    """, (paciente_id, medico_id))
    if not cur.fetchone():
        db.rollback()
        raise HTTPException(404, "Paciente no encontrado o sin permiso")
    db.commit()
    return {"ok": True}


# ====================================================
# 💊 RECETAS
# ====================================================

@router.post("/recetas", status_code=201)
def emitir_receta(
    data: RecetaIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Emite una nueva receta. El médico selecciona uno de sus pacientes."""
    if not data.medicamentos:
        raise HTTPException(400, "Debés incluir al menos un medicamento")

    cur = db.cursor()

    # Verificar que el paciente pertenece al médico
    cur.execute("""
        SELECT id, nombre, apellido FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (data.paciente_id, medico_id))
    pac = cur.fetchone()
    if not pac:
        raise HTTPException(404, "Paciente no encontrado en tu listado")

    import json as _json
    meds_json = _json.dumps([m.dict() for m in data.medicamentos], ensure_ascii=False)

    cur.execute("""
        INSERT INTO recetario_recetas
            (medico_id, paciente_id, obra_social, plan, nro_credencial,
             diagnostico, medicamentos)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING id, uuid, creado_en
    """, (
        medico_id,
        data.paciente_id,
        data.obra_social,
        data.plan,
        data.nro_credencial,
        data.diagnostico,
        meds_json
    ))
    row = cur.fetchone()
    db.commit()

    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    return {
        "ok": True,
        "receta_id": row[0],
        "uuid": str(row[2]),
        "creado_en": str(row[2]),
        "url_html": f"{base}/recetario/recetas/{row[0]}/html",
        "url_verificar": f"{base}/recetario/verificar/{row[1]}",
    }


@router.get("/recetas")
def listar_recetas(
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Historial de recetas del médico."""
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.estado, r.diagnostico, r.creado_en,
               p.nombre, p.apellido, p.nro_documento, p.tipo_documento
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        WHERE r.medico_id=%s
        ORDER BY r.creado_en DESC
    """, (medico_id,))

    recetas = []
    for row in cur.fetchall():
        recetas.append({
            "id": row[0], "uuid": str(row[1]), "estado": row[2],
            "diagnostico": row[3],
            "fecha": row[4].strftime("%d/%m/%Y %H:%M") if row[4] else None,
            "paciente": f"{row[6]}, {row[5]}",
            "documento": f"{row[8]} {row[7]}",
        })
    return {"total": len(recetas), "recetas": recetas}


@router.get("/recetas/{receta_id}")
def ver_receta_json(
    receta_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.estado, r.diagnostico, r.medicamentos,
               r.obra_social, r.plan, r.nro_credencial, r.creado_en, r.motivo_anulacion,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento,
               p.sexo, p.fecha_nacimiento, p.cuil
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        WHERE r.id=%s AND r.medico_id=%s
    """, (receta_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Receta no encontrada")

    return {
        "id": row[0], "uuid": str(row[1]), "estado": row[2],
        "diagnostico": row[3], "medicamentos": row[4],
        "obra_social": row[5], "plan": row[6], "nro_credencial": row[7],
        "fecha": row[8].strftime("%d/%m/%Y %H:%M") if row[8] else None,
        "motivo_anulacion": row[9],
        "paciente": {
            "nombre": row[10], "apellido": row[11],
            "tipo_documento": row[12], "nro_documento": row[13],
            "sexo": row[14], "fecha_nacimiento": str(row[15]) if row[15] else None,
            "cuil": row[16],
        }
    }


@router.patch("/recetas/{receta_id}/anular")
def anular_receta(
    receta_id: int,
    data: AnularIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    cur = db.cursor()
    cur.execute("""
        UPDATE recetario_recetas
        SET estado='anulada', motivo_anulacion=%s, updated_at=NOW()
        WHERE id=%s AND medico_id=%s AND estado='valida'
        RETURNING id
    """, (data.motivo, receta_id, medico_id))
    if not cur.fetchone():
        db.rollback()
        raise HTTPException(404, "Receta no encontrada, ya anulada o sin permiso")
    db.commit()
    return {"ok": True, "receta_id": receta_id, "estado": "anulada"}


# ====================================================
# 🌐 VERIFICADOR PÚBLICO (sin auth)
# ====================================================

@router.get("/verificar/{uuid_receta}", response_class=HTMLResponse)
def verificar_receta(uuid_receta: str, db=Depends(get_db)):
    """
    Página pública de verificación de autenticidad de una receta.
    Accesible desde el QR impreso en la receta.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT r.uuid, r.estado, r.diagnostico, r.creado_en,
               p.nombre, p.apellido,
               m.full_name, m.matricula, m.especialidad, m.tipo
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        JOIN medicos             m ON m.id = r.medico_id
        WHERE r.uuid = %s
    """, (uuid_receta,))
    row = cur.fetchone()

    if not row:
        return HTMLResponse(_html_no_encontrada(uuid_receta), status_code=404)

    uuid_val, estado, diagnostico, creado_en, pac_nombre, pac_apellido, \
        med_nombre, matricula, especialidad, tipo_med = row

    fecha_str = creado_en.strftime("%d de %B de %Y") if creado_en else "—"
    es_valida  = estado == "valida"

    return HTMLResponse(_html_verificacion(
        uuid=str(uuid_val),
        estado=estado,
        es_valida=es_valida,
        fecha=fecha_str,
        paciente=f"{pac_apellido}, {pac_nombre}",
        medico=med_nombre,
        matricula=matricula or "—",
        especialidad=especialidad or tipo_med or "—",
        diagnostico=diagnostico or "—",
    ))


# ====================================================
# 📜 CERTIFICADOS MÉDICOS
# ====================================================

class CertificadoIn(BaseModel):
    paciente_id:   int
    diagnostico:   Optional[str] = None
    reposo_dias:   Optional[int] = None
    observaciones: Optional[str] = None

@router.post("/certificados", status_code=201)
def emitir_certificado(
    data: CertificadoIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Emite un certificado médico y lo persiste."""
    cur = db.cursor()
    # Verificar que el paciente pertenece al médico
    cur.execute("""
        SELECT id FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (data.paciente_id, medico_id))
    if not cur.fetchone():
        raise HTTPException(404, "Paciente no encontrado")

    cur.execute("""
        INSERT INTO recetario_certificados
            (medico_id, paciente_id, diagnostico, reposo_dias, observaciones)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id, creado_en
    """, (medico_id, data.paciente_id, data.diagnostico,
          data.reposo_dias, data.observaciones))
    row = cur.fetchone()
    db.commit()
    return {"id": row[0], "creado_en": str(row[1]),
            "url_html": f"/recetario/certificados/{row[0]}/html"}


@router.get("/certificados")
def listar_certificados(
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Lista todos los certificados emitidos por el médico."""
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.diagnostico, c.reposo_dias, c.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento
        FROM recetario_certificados c
        JOIN recetario_pacientes p ON p.id = c.paciente_id
        WHERE c.medico_id = %s
        ORDER BY c.creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()
    return {"total": len(rows), "certificados": [
        {
            "id": r[0], "diagnostico": r[1], "reposo_dias": r[2],
            "fecha": r[3].strftime("%d/%m/%Y") if r[3] else None,
            "paciente": f"{r[5]}, {r[4]}",
            "documento": f"{r[6]} {r[7]}",
        } for r in rows
    ]}


@router.get("/certificados/{cert_id}/html", response_class=HTMLResponse)
def certificado_html(
    cert_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Devuelve el certificado en HTML listo para imprimir / guardar como PDF."""
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.diagnostico, c.reposo_dias, c.observaciones, c.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento,
               p.sexo, p.fecha_nacimiento, p.cuil, p.obra_social,
               m.full_name, m.matricula, m.especialidad, m.tipo, m.firma_url
        FROM recetario_certificados c
        JOIN recetario_pacientes p ON p.id = c.paciente_id
        JOIN medicos             m ON m.id = c.medico_id
        WHERE c.id = %s AND c.medico_id = %s
    """, (cert_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Certificado no encontrado")

    (cert_id_val, diagnostico, reposo_dias, observaciones, creado_en,
     pac_nombre, pac_apellido, tipo_doc, nro_doc,
     sexo, fecha_nac, cuil, obra_social,
     med_nombre, matricula, especialidad, tipo_med, firma_url) = row

    fecha_emision = creado_en.strftime("%d/%m/%Y") if creado_en else "—"
    fecha_nac_str = fecha_nac.strftime("%d/%m/%Y") if fecha_nac else "—"
    sexo_label    = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo or "—")
    esp_label     = (especialidad or tipo_med or "Médico/a").title()
    mat_label     = matricula or "—"

    base    = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/certificados/{cert_id_val}/html"
    qr_url  = f"https://api.qrserver.com/v1/create-qr-code/?size=110x110&data={ver_url}"
    logo_src = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"

    # Firma
    firma_bloque = (f'<img src="{firma_url}" class="firma-img" alt="Firma">'
                    if firma_url else '<div class="firma-linea"></div>')

    # Cuerpo del certificado
    reposo_txt = (f"<strong>{reposo_dias}</strong> día{'s' if reposo_dias != 1 else ''}"
                  if reposo_dias else "el período indicado por el profesional")

    obs_parrafo = (f'<p style="text-align:justify;margin-top:14px;"><strong>Observaciones:</strong> {observaciones}</p>'
                   if observaciones else "")

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Certificado Médico — DocYa</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: Arial, Helvetica, sans-serif;
  font-size: 13px;
  color: #1f2937;
  background: #e2e8f0;
  -webkit-font-smoothing: antialiased;
}}
@media print {{
  body {{ background: #fff; }}
  .no-print {{ display: none !important; }}
  .page {{ box-shadow: none; margin: 0; border-radius: 0; }}
  @page {{ margin: 12mm; size: A4; }}
}}
/* Toolbar */
.no-print {{
  position: sticky; top: 0; z-index: 20;
  background: #1e293b; padding: 9px 16px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}}
.no-print button {{
  background: #14B8A6; color: #fff; border: none;
  padding: 6px 20px; border-radius: 20px;
  font-size: 12px; font-weight: 700; cursor: pointer;
}}
.no-print a {{ color: #14B8A6; font-size: 12px; text-decoration: none; }}
/* Page */
.page {{
  background: #fff;
  max-width: 210mm;
  min-height: 297mm;
  margin: 16px auto;
  padding: 40px 48px;
  box-shadow: 0 4px 28px rgba(0,0,0,0.14);
  border-radius: 2px;
  display: flex;
  flex-direction: column;
}}
/* Header */
.header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 3px solid #14B8A6;
  padding-bottom: 12px;
  margin-bottom: 28px;
}}
.logo {{ height: 48px; }}
.header-right {{ text-align: right; font-size: 11px; color: #6b7280; line-height: 1.7; }}
.header-right strong {{ color: #374151; }}
/* Title */
.cert-title {{
  text-align: center;
  font-size: 20px;
  font-weight: 900;
  color: #14B8A6;
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 28px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e5e7eb;
}}
/* Patient box */
.pac-box {{
  border: 1.5px solid #14B8A6;
  border-radius: 6px;
  background: #f0fdfa;
  padding: 14px 18px;
  margin-bottom: 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
}}
.pac-field {{ min-width: 140px; }}
.pac-field label {{
  display: block; font-size: 9px; color: #6b7280;
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px;
}}
.pac-field strong {{ font-size: 13px; }}
/* Cert body */
.cert-body {{
  border: 1px solid #d1fae5;
  border-radius: 6px;
  background: #f9fdfc;
  padding: 24px 28px;
  margin-bottom: 24px;
  flex: 1;
  line-height: 1.85;
}}
.cert-body p {{ text-align: justify; margin-bottom: 14px; }}
.cert-body p:last-child {{ margin-bottom: 0; }}
/* Reposo highlight */
.reposo-box {{
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(20,184,166,0.1); border: 1px solid rgba(20,184,166,0.35);
  border-radius: 6px; padding: 8px 14px; margin: 8px 0;
  font-weight: 600; font-size: 13px; color: #0f766e;
}}
/* Signature */
.sig-row {{
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-top: 32px;
  padding-top: 20px;
  border-top: 1px dashed #9ca3af;
  gap: 20px;
}}
.sig-legal {{ flex: 1; font-size: 9.5px; color: #6b7280; line-height: 1.6; }}
.sig-legal a {{ color: #14B8A6; }}
.sig-block {{ text-align: center; min-width: 160px; }}
.firma-img  {{ max-width: 140px; max-height: 60px; object-fit: contain; display: block; margin: 0 auto 4px; }}
.firma-linea {{ width: 140px; height: 52px; border-bottom: 1.5px solid #374151; margin: 0 auto 4px; }}
.firma-name  {{ font-size: 11px; font-weight: 700; }}
.firma-sub   {{ font-size: 10px; color: #555; margin-top: 1px; }}
.firma-stamp {{ font-size: 10px; font-weight: 800; color: #14B8A6; margin-top: 3px; letter-spacing: 0.5px; }}
/* QR strip */
.qr-strip {{
  display: flex; align-items: center; gap: 12px;
  background: #f8fafc; border: 1px solid #e5e7eb;
  border-radius: 6px; padding: 10px 14px; margin-top: 20px;
}}
.qr-img {{ flex-shrink: 0; border: 1px solid #e5e7eb; border-radius: 4px; }}
.qr-info {{ flex: 1; font-size: 9px; line-height: 1.7; color: #374151; }}
.qr-badge {{
  flex-shrink: 0;
  background: linear-gradient(135deg, #0AE6C7, #0d9488);
  color: #fff; font-size: 8px; font-weight: 800;
  text-align: center; padding: 6px 10px; border-radius: 4px;
  text-transform: uppercase; letter-spacing: 0.5px; line-height: 1.4;
}}
/* Footer */
.footer {{
  text-align: center; font-size: 9px; color: #9ca3af;
  margin-top: 20px; padding-top: 14px;
  border-top: 1px solid #f3f4f6;
}}
/* Mobile */
@media (max-width: 600px) {{
  .page {{ padding: 20px 18px; min-height: unset; margin: 8px; }}
  .header .logo {{ height: 36px; }}
  .cert-title {{ font-size: 15px; letter-spacing: 1px; }}
  .pac-box {{ gap: 8px 16px; }}
  .sig-row {{ flex-direction: column; align-items: center; }}
  .sig-block {{ min-width: unset; }}
}}
</style>
</head>
<body>

<div class="no-print">
  <button onclick="window.print()">🖨 Imprimir / PDF</button>
  <span style="color:#94a3b8;font-size:11px;">Certificado #{cert_id_val}</span>
</div>

<div class="page">

  <!-- HEADER -->
  <div class="header">
    <img src="{logo_src}" class="logo" alt="DocYa">
    <div class="header-right">
      <strong>Fecha de emisión:</strong> {fecha_emision}<br>
      <strong>ID:</strong> {cert_id_val:08d}<br>
      <strong>Conforme:</strong> Ley 25.506
    </div>
  </div>

  <!-- TITLE -->
  <div class="cert-title">Certificado Médico</div>

  <!-- PACIENTE -->
  <div class="pac-box">
    <div class="pac-field" style="flex:1 1 100%">
      <label>Paciente</label>
      <strong>{pac_apellido.upper()}, {pac_nombre}</strong>
    </div>
    <div class="pac-field"><label>{tipo_doc}</label><strong>{nro_doc}</strong></div>
    {"<div class='pac-field'><label>CUIL</label><strong>" + cuil + "</strong></div>" if cuil else ""}
    <div class="pac-field"><label>Sexo</label><strong>{sexo_label}</strong></div>
    <div class="pac-field"><label>F. Nacimiento</label><strong>{fecha_nac_str}</strong></div>
    {"<div class='pac-field'><label>Obra Social</label><strong>" + obra_social + "</strong></div>" if obra_social else ""}
  </div>

  <!-- CUERPO -->
  <div class="cert-body">
    <p>
      Por medio del presente, certifico que <strong>{pac_apellido.upper()}, {pac_nombre}</strong>,
      identificado/a con {tipo_doc} <strong>{nro_doc}</strong>,
      fue evaluado/a el día <strong>{fecha_emision}</strong> por el/la suscripto/a,
      constatándose el siguiente diagnóstico:
      <strong>{diagnostico or "sin diagnóstico especificado"}</strong>.
    </p>
    {"<p>Se recomienda reposo por <span class='reposo-box'>🛏 " + str(reposo_dias) + " día" + ("s" if reposo_dias != 1 else "") + " de reposo</span>, a partir de la fecha del presente certificado, debiendo evitar actividades laborales y/o físicas durante dicho período.</p>" if reposo_dias else ""}
    {obs_parrafo}
    <p>
      Se expide el presente certificado a pedido del/la interesado/a,
      para ser presentado ante quien corresponda.
    </p>
  </div>

  <!-- FIRMA -->
  <div class="sig-row">
    <div class="sig-legal">
      Este documento ha sido firmado digitalmente por<br>
      <strong>{med_nombre}</strong> — {esp_label} — MN {mat_label}<br>
      conforme a la <a href="#">Ley 25.506</a> de Firma Digital de la República Argentina.<br>
      Verificá su autenticidad en: <a href="{ver_url}">{ver_url}</a>
    </div>
    <div class="sig-block">
      {firma_bloque}
      <div class="firma-name">{med_nombre}</div>
      <div class="firma-sub">{esp_label}</div>
      <div class="firma-sub">MN {mat_label}</div>
      <div class="firma-stamp">FIRMA Y SELLO</div>
    </div>
  </div>

  <!-- QR -->
  <div class="qr-strip">
    <img src="{qr_url}" width="90" height="90" alt="QR" class="qr-img">
    <div class="qr-info">
      <strong>DocYa — Documentos Médicos Digitales</strong><br>
      {med_nombre} | {esp_label} | MN {mat_label}<br>
      Verificar autenticidad: {ver_url}
    </div>
    <div class="qr-badge">certificado<br>médico</div>
  </div>

  <!-- FOOTER -->
  <div class="footer">
    Certificado generado digitalmente mediante DocYa — Plataforma de Documentos Médicos Electrónicos.<br>
    © {datetime.now().year} DocYa — Todos los derechos reservados.
  </div>

</div>
</body>
</html>"""

    return HTMLResponse(html)


# ====================================================
# 🖨️ RECETA HTML IMPRIMIBLE
# ====================================================

@router.get("/recetas/{receta_id}/html", response_class=HTMLResponse)
def receta_html(
    receta_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Devuelve la receta en HTML listo para imprimir / descargar como PDF."""
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.estado, r.diagnostico, r.medicamentos,
               r.obra_social, r.plan, r.nro_credencial, r.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento,
               p.sexo, p.fecha_nacimiento, p.cuil,
               m.full_name, m.matricula, m.especialidad, m.tipo, m.firma_url
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        JOIN medicos             m ON m.id = r.medico_id
        WHERE r.id=%s AND r.medico_id=%s
    """, (receta_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Receta no encontrada")

    (rec_id, uuid_val, estado, diagnostico, medicamentos,
     obra_social, plan, nro_credencial, creado_en,
     pac_nombre, pac_apellido, tipo_doc, nro_doc,
     sexo, fecha_nac, cuil,
     med_nombre, matricula, especialidad, tipo_med, firma_url) = row

    fecha_emision = creado_en.strftime("%d/%m/%Y") if creado_en else "&mdash;"
    fecha_nac_str = fecha_nac.strftime("%d/%m/%Y") if fecha_nac else "&mdash;"
    sexo_label = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo)

    meds_rp_html = ""
    meds_com_html = ""
    for i, m in enumerate(medicamentos or [], 1):
        nombre = m.get("nombre", "")
        concentracion = m.get("concentracion") or ""
        presentacion = m.get("presentacion") or ""
        cantidad = m.get("cantidad", 1)
        indicaciones = m.get("indicaciones", "")
        cantidad_txt = {1: "uno", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco"}.get(int(cantidad), str(cantidad))
        indicaciones_html = indicaciones if indicaciones else '<em style="color:#aaa">Sin indicaciones</em>'
        meds_rp_html += (
            f'<div class="med-rp"><span class="med-num">{i})</span> '
            f'<strong>{nombre}{(" " + concentracion) if concentracion else ""}</strong>'
            f'{(" &mdash; " + presentacion) if presentacion else ""}<br>'
            f'<span class="med-cant">Cant: {cantidad} ({cantidad_txt})</span></div>'
        )
        meds_com_html += f'<div class="med-com"><span class="med-num">{i})</span> {indicaciones_html}</div>'

    diag_html = (
        f'<div class="diag-row"><strong>Diagn&oacute;stico:</strong> {diagnostico}</div>'
        if diagnostico else ""
    )

    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/verificar/{uuid_val}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=96x96&data={ver_url}"
    bc_doc = f"https://bwipjs-api.metafloor.com/?bcid=code128&text={nro_doc}&scale=2&height=10&includetext=false"
    bc_cred = (
        f"https://bwipjs-api.metafloor.com/?bcid=code128&text={nro_credencial}&scale=2&height=10&includetext=false"
        if nro_credencial else ""
    )

    logo_src = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    esp_label = "M&Eacute;DICO"
    mat_label = matricula or "&mdash;"
    anulada_pill = "<span class='anulada-pill'>? ANULADA</span>" if estado == "anulada" else ""
    cred_bc_html = f'<img class="barcode" src="{bc_cred}" alt="Cred">' if bc_cred else ""
    cuil_html = f'<div class="pf"><label>CUIL</label><strong>{cuil}</strong></div>' if cuil else ""
    firma_html = (
        f'<img src="{firma_url}" alt="Firma" class="firma-img">'
        if firma_url else '<div class="firma-linea"></div>'
    )

    def _top(badge):
        return f"""
      <div class="top-strip">
        <div class="top-barcodes">
          <img class="barcode" src="{bc_doc}" alt="{nro_doc}">
          {cred_bc_html}
        </div>
        <div class="top-center">
          <img src="{logo_src}" class="logo" alt="DocYa">
          <span class="copy-badge">{badge}</span>
        </div>
        <div class="top-info">
          <strong>{med_nombre}</strong><br>
          {esp_label}<br>
          MN {mat_label}<br>
          <span class="fecha-teal">{fecha_emision}</span>
        </div>
      </div>"""

    pac_grid = f"""
      <div class="pac-grid">
        <div class="pf pf-name"><label>Paciente</label><strong>{pac_apellido.upper()}, {pac_nombre}</strong></div>
        <div class="pf"><label>Sexo</label><strong>{sexo_label}</strong></div>
        <div class="pf"><label>{tipo_doc}</label><strong>{nro_doc}</strong></div>
        <div class="pf"><label>F. Nacimiento</label><strong>{fecha_nac_str}</strong></div>
        {cuil_html}
        <div class="pf"><label>Obra Social</label><strong>{obra_social or "&mdash;"}</strong></div>
        <div class="pf"><label>Plan</label><strong>{plan or "&mdash;"}</strong></div>
        <div class="pf"><label>N&deg; Credencial</label><strong>{nro_credencial or "&mdash;"}</strong></div>
      </div>"""

    sig_footer = f"""
      <div class="sig-footer">
        <div>
          <p class="sig-legal">Este documento ha sido firmado electr&oacute;nicamente por<br><strong>{med_nombre}</strong><br>conforme Ley 25.506 de Firma Digital.</p>
          <p class="sig-date">{fecha_emision}</p>
        </div>
        <div class="sig-right">
          {firma_html}
          <div class="firma-label">{med_nombre}</div>
          <div class="firma-sub">{esp_label} &middot; MN {mat_label}</div>
          <div class="firma-stamp">FIRMA Y SELLO</div>
        </div>
      </div>"""

    qr_strip = f"""
      <div class="qr-strip">
        <img src="{qr_url}" alt="QR" class="qr-img">
        <div class="strip-info">
          <strong>{esp_label}</strong><br>
          {med_nombre}<br>
          <span class="strip-note">Esta receta fue creada por un emisor inscripto en DocYa. RL-2024-{rec_id:09d}</span>
        </div>
        <div class="strip-badge">receta<br>electr&oacute;nica</div>
      </div>"""

    def _content_box(only_indications=False):
        if only_indications:
            return f"""
      <div class="content-box ind-only">
        <div class="col">
          <div class="sec-title ind-title">Indicaciones:</div>
          {meds_com_html}
        </div>
      </div>"""
        return f"""
      <div class="content-box">
        <div class="col">
          <div class="sec-title">Rp/</div>
          {meds_rp_html}
          {diag_html}
        </div>
        <div class="inner-divider"></div>
        <div class="col">
          <div class="sec-title ind-title">Indicaciones:</div>
          {meds_com_html}
        </div>
      </div>"""

    def _copy(badge, watermark_text="", only_indications=False):
        watermark_html = f'<div class="watermark">{watermark_text}</div>' if watermark_text else ""
        return f"""
    <div class="copy">
      {watermark_html}
      {_top(badge)}
      {pac_grid}
      {_content_box(only_indications)}
      {sig_footer}
      {qr_strip}
    </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receta #{rec_id} &mdash; DocYa</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --teal: #14b8a6;
  --teal-dark: #0d9488;
  --ink: #111827;
  --muted: #6b7280;
  --line: #e5e7eb;
  --sheet-w: 297mm;
  --sheet-h: 210mm;
  --print-w: 283mm;
  --print-h: 196mm;
  --half-w: 141.5mm;
  --half-h: 190mm;
}}
body {{
  font-family: Arial, Helvetica, sans-serif;
  color: var(--ink);
  background: #e2e8f0;
  -webkit-font-smoothing: antialiased;
}}
.no-print {{ position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 16px; background: #111827; }}
.no-print button {{ border: none; border-radius: 999px; padding: 8px 18px; background: var(--teal); color: #fff; font-size: 13px; font-weight: 700; cursor: pointer; }}
.no-print a {{ color: var(--teal); text-decoration: none; font-size: 13px; }}
.anulada-pill {{ background: #fef2f2; color: #dc2626; border: 1px solid #dc2626; border-radius: 999px; padding: 3px 10px; font-weight: 700; font-size: 11px; }}
.page-label {{ margin-left: auto; color: #94a3b8; font-size: 12px; }}
.page {{ width: min(100%, var(--sheet-w)); min-height: var(--sheet-h); margin: 14px auto; background: #fff; border-top: 3px solid var(--teal); box-shadow: 0 4px 28px rgba(0,0,0,0.15); }}
.sheet {{ width: var(--print-w); min-height: var(--print-h); margin: 0 auto; display: grid; grid-template-columns: var(--half-w) 1px var(--half-w); align-items: start; padding-top: 3mm; }}
.sheet.single {{ grid-template-columns: var(--half-w); justify-content: center; }}
.divider {{ width: 1px; height: var(--half-h); background: repeating-linear-gradient(to bottom, #6b7280 0, #6b7280 2px, transparent 2px, transparent 4px); }}
.copy {{ height: var(--half-h); padding: 7px 10px 6px; overflow: hidden; position: relative; }}
.watermark {{ position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 34px; font-weight: 900; letter-spacing: 4px; color: rgba(0,0,0,0.05); transform: rotate(-30deg); pointer-events: none; }}
.top-strip {{ display: grid; grid-template-columns: 76px 1fr 92px; gap: 8px; align-items: start; padding-bottom: 5px; margin-bottom: 5px; border-bottom: 1px solid var(--line); }}
.top-barcodes {{ display: flex; flex-direction: column; gap: 3px; }}
.barcode {{ display: block; width: auto; max-width: 72px; height: 18px; object-fit: contain; }}
.top-center {{ text-align: center; }}
.logo {{ display: block; height: 21px; margin: 0 auto 2px; }}
.copy-badge {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: linear-gradient(135deg, #0ae6c7, var(--teal-dark)); color: #fff; font-size: 6.5px; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; }}
.top-info {{ text-align: right; font-size: 8px; line-height: 1.35; color: #374151; }}
.fecha-teal {{ color: var(--teal-dark); font-weight: 700; }}
.pac-grid {{ display: flex; flex-wrap: wrap; margin-bottom: 5px; overflow: hidden; border: 1.5px solid var(--teal-dark); border-radius: 3px; }}
.pf {{ flex: 1 1 33%; min-width: 0; padding: 2px 5px; border-right: 1px solid #ccfbf1; border-bottom: 1px solid #ccfbf1; }}
.pf-name {{ flex: 1 1 100%; background: #f0fdfa; }}
.pf label {{ display: block; margin-bottom: 1px; color: var(--muted); font-size: 6.5px; letter-spacing: 0.3px; text-transform: uppercase; }}
.pf strong {{ font-size: 8.5px; }}
.content-box {{ display: grid; grid-template-columns: 1fr 1px 1fr; margin-bottom: 4px; border: 1px solid var(--line); border-radius: 3px; overflow: hidden; }}
.content-box.ind-only {{ grid-template-columns: 1fr; }}
.col {{ min-height: 96mm; padding: 4px 5px; }}
.col:last-child {{ background: #fafafa; }}
.inner-divider {{ width: 1px; background: var(--line); }}
.sec-title {{ margin-bottom: 4px; padding-bottom: 2px; border-bottom: 1px solid var(--line); color: var(--teal-dark); font-size: 11px; font-weight: 900; }}
.ind-title {{ color: #374151; font-size: 10px; }}
.med-rp, .med-com {{ margin: 2px 0; font-size: 8.5px; line-height: 1.35; }}
.med-num {{ color: var(--teal-dark); font-weight: 700; }}
.med-cant {{ color: var(--muted); font-size: 7.5px; }}
.diag-row {{ margin-top: 6px; padding: 2px 6px; border-left: 2px solid var(--teal-dark); background: #f0fdfa; color: #374151; font-size: 7.5px; }}
.sig-footer {{ display: grid; grid-template-columns: 1fr 92px; gap: 6px; align-items: end; margin-bottom: 4px; padding-top: 4px; border-top: 1px dashed #9ca3af; }}
.sig-legal, .firma-sub, .strip-note {{ font-size: 6.5px; }}
.sig-legal {{ color: var(--muted); line-height: 1.45; }}
.sig-date, .firma-label, .firma-stamp, .strip-info {{ font-size: 7px; }}
.sig-date {{ margin-top: 4px; font-weight: 700; }}
.sig-right {{ text-align: center; }}
.firma-img {{ display: block; width: auto; max-width: 84px; max-height: 32px; margin: 0 auto 2px; object-fit: contain; }}
.firma-linea {{ width: 80px; height: 28px; margin: 0 auto 2px; border-bottom: 1.5px solid #111; }}
.firma-label {{ font-weight: 700; }}
.firma-sub {{ color: #555; }}
.firma-stamp {{ margin-top: 3px; color: var(--teal-dark); font-weight: 800; letter-spacing: 0.5px; }}
.qr-strip {{ display: grid; grid-template-columns: 48px 1fr auto; gap: 5px; align-items: center; padding: 4px 5px; background: #f8fafc; border: 1px solid var(--line); border-radius: 3px; }}
.qr-img {{ display: block; width: 48px; height: 48px; border: 1px solid var(--line); border-radius: 2px; }}
.strip-info {{ line-height: 1.45; color: #374151; }}
.strip-note {{ display: block; margin-top: 1px; color: var(--muted); }}
.strip-badge {{ padding: 3px 5px; border-radius: 3px; background: linear-gradient(135deg, #0ae6c7, var(--teal-dark)); color: #fff; font-size: 6.5px; font-weight: 800; line-height: 1.35; letter-spacing: 0.4px; text-align: center; text-transform: uppercase; }}
@media print {{
  html, body {{ width: var(--sheet-w); height: var(--sheet-h); margin: 0; padding: 0; background: #fff; }}
  .no-print {{ display: none !important; }}
  .page {{ width: var(--sheet-w); min-height: var(--sheet-h); margin: 0; border-top: 0; box-shadow: none; page-break-after: always; break-after: page; }}
  .page:last-child {{ page-break-after: auto; break-after: auto; }}
  .sheet, .copy, .content-box, .sig-footer, .qr-strip {{ break-inside: avoid; page-break-inside: avoid; }}
  @page {{ size: A4 landscape; margin: 7mm; }}
}}
@media screen and (max-width: 700px) {{
  .no-print {{ position: static; gap: 8px; padding: 10px 12px; }}
  .page-label {{ width: 100%; margin-left: 0; }}
  .page {{ width: auto; min-height: unset; margin: 8px; border-top-width: 2px; border-radius: 6px; }}
  .sheet {{ width: 100%; min-height: unset; padding: 8px; grid-template-columns: 1fr; row-gap: 10px; }}
  .sheet.single {{ grid-template-columns: 1fr; }}
  .divider {{ width: 100%; height: 1px; background: repeating-linear-gradient(to right, #6b7280 0, #6b7280 2px, transparent 2px, transparent 4px); }}
  .copy {{ height: auto; min-height: unset; padding: 10px; }}
  .top-strip {{ grid-template-columns: 1fr; }}
  .top-barcodes, .barcode {{ display: none; }}
  .top-center, .top-info {{ text-align: left; }}
  .pf {{ flex: 1 1 100%; }}
  .content-box {{ grid-template-columns: 1fr; }}
  .inner-divider {{ width: 100%; height: 1px; }}
  .col {{ min-height: unset; }}
  .sig-footer {{ grid-template-columns: 1fr; }}
  .sig-right {{ text-align: left; }}
  .firma-img, .firma-linea {{ margin-left: 0; }}
  .qr-strip {{ grid-template-columns: 56px 1fr; }}
  .qr-img {{ width: 56px; height: 56px; }}
  .strip-badge {{ grid-column: 1 / -1; justify-self: end; }}
}}
</style>
</head>
<body>
<div class="no-print">
  <button onclick="window.print()">Imprimir / PDF</button>
  <a href="{ver_url}" target="_blank">Verificar</a>
  {anulada_pill}
  <span class="page-label">Receta #{rec_id}</span>
</div>
<div class="page">
  <div class="sheet">
    {_copy("Original")}
    <div class="divider"></div>
    {_copy("Duplicado", "DUPLICADO")}
  </div>
</div>
<div class="page">
  <div class="sheet single">
    {_copy("Indicaciones", only_indications=True)}
  </div>
</div>
</body>
</html>"""

    return HTMLResponse(html)




# ====================================================
# 🔧 Helpers HTML
# ====================================================
def _html_verificacion(uuid, estado, es_valida, fecha, paciente,
                        medico, matricula, especialidad, diagnostico):
    color  = "#14B8A6" if es_valida else "#dc2626"
    icono  = "✅" if es_valida else "❌"
    titulo = "Documento Válido" if es_valida else "Documento Anulado"
    subtxt = ("La firma digital es auténtica y el documento se encuentra vigente."
              if es_valida else
              "Este documento fue revocado por el profesional y no tiene validez legal.")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verificación — DocYa</title>
<style>
  body {{ font-family: Arial, sans-serif; background: #030b12; color: #fff;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; margin: 0; padding: 20px; }}
  .card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
           border-radius: 20px; padding: 40px 32px; max-width: 480px; width: 100%;
           text-align: center; border-top: 3px solid {color}; }}
  .icon {{ font-size: 3.5rem; margin-bottom: 16px; }}
  h2 {{ color: {color}; font-size: 1.6rem; margin-bottom: 8px; }}
  .sub {{ color: #94a3b8; font-size: 0.9rem; margin-bottom: 28px; }}
  .data {{ background: rgba(0,0,0,0.3); border-radius: 10px; padding: 18px;
           text-align: left; }}
  .row {{ display: flex; justify-content: space-between; padding: 10px 0;
          border-bottom: 1px solid rgba(255,255,255,0.07); font-size: 0.9rem; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #94a3b8; }}
  .value {{ font-weight: 600; color: {color}; }}
  .logo {{ margin-bottom: 28px; }}
  .logo img {{ height: 36px; }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa">
  </div>
  <div class="icon">{icono}</div>
  <h2>{titulo}</h2>
  <p class="sub">{subtxt}</p>
  <div class="data">
    <div class="row"><span class="label">Tipo</span><span class="value">Receta Médica Electrónica</span></div>
    <div class="row"><span class="label">Fecha emisión</span><span class="value">{fecha}</span></div>
    <div class="row"><span class="label">Médico emisor</span><span class="value">{medico}</span></div>
    <div class="row"><span class="label">Matrícula Nac.</span><span class="value">MN {matricula}</span></div>
    <div class="row"><span class="label">Especialidad</span><span class="value">{especialidad}</span></div>
    <div class="row"><span class="label">Paciente</span><span class="value">{paciente}</span></div>
    <div class="row"><span class="label">Estado</span>
      <span class="value" style="color:{'#4ade80' if es_valida else '#f87171'}">
        {'VÁLIDA' if es_valida else 'ANULADA'}
      </span>
    </div>
    <div class="row"><span class="label">UUID</span>
      <span class="value" style="font-size:0.75rem;color:#94a3b8">{uuid}</span>
    </div>
  </div>
</div>
</body>
</html>"""


def _html_no_encontrada(uuid_receta: str):
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>No encontrado — DocYa</title>
<style>
  body {{ font-family: Arial; background:#030b12; color:#fff;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1);
           border-radius:20px; padding:40px; text-align:center; max-width:420px;
           border-top:3px solid #dc2626; }}
  h2 {{ color:#dc2626; }} p {{ color:#94a3b8; font-size:0.9rem; margin-top:10px; }}
  code {{ font-size:0.75rem; color:#475569; word-break:break-all; }}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:3rem">🔍</div>
  <h2>Documento no encontrado</h2>
  <p>No existe ningún documento con el identificador:</p>
  <code>{uuid_receta}</code>
</div>
</body>
</html>"""
