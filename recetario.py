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
             obra_social, plan, nro_credencial, cuil, observaciones)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        data.observaciones
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
    cur = db.cursor()
    if q:
        filtro = f"%{q.strip()}%"
        cur.execute("""
            SELECT id, nombre, apellido, tipo_documento, nro_documento,
                   sexo, fecha_nacimiento, telefono, email,
                   obra_social, plan, nro_credencial, cuil, observaciones, creado_en
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
                   obra_social, plan, nro_credencial, cuil, observaciones, creado_en
            FROM recetario_pacientes
            WHERE medico_id=%s
            ORDER BY apellido, nombre
        """, (medico_id,))

    cols = ["id","nombre","apellido","tipo_documento","nro_documento",
            "sexo","fecha_nacimiento","telefono","email",
            "obra_social","plan","nro_credencial","cuil","observaciones","creado_en"]
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
    cur = db.cursor()
    cur.execute("""
        SELECT id, nombre, apellido, tipo_documento, nro_documento,
               sexo, fecha_nacimiento, telefono, email,
               obra_social, plan, nro_credencial, cuil, observaciones, creado_en
        FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (paciente_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Paciente no encontrado")

    cols = ["id","nombre","apellido","tipo_documento","nro_documento",
            "sexo","fecha_nacimiento","telefono","email",
            "obra_social","plan","nro_credencial","cuil","observaciones","creado_en"]
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
            observaciones=%s, updated_at=NOW()
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

    fecha_emision  = creado_en.strftime("%d/%m/%Y") if creado_en else "—"
    fecha_nac_str  = fecha_nac.strftime("%d/%m/%Y") if fecha_nac else "—"
    sexo_label     = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo)

    # ── Medicamentos ─────────────────────────────────────────────────────────
    meds_rp_html  = ""
    meds_com_html = ""
    for i, m in enumerate(medicamentos or [], 1):
        nombre        = m.get("nombre", "")
        concentracion = m.get("concentracion") or ""
        presentacion  = m.get("presentacion") or ""
        cantidad      = m.get("cantidad", 1)
        indicaciones  = m.get("indicaciones", "")
        cantidad_txt  = {1:"uno",2:"dos",3:"tres",4:"cuatro",5:"cinco"}.get(int(cantidad), str(cantidad))
        indicaciones_html = indicaciones if indicaciones else '<em style="color:#aaa">Sin indicaciones</em>'
        meds_rp_html += (
            f'<div class="med-rp">'
            f'<span class="med-num">{i})</span>&nbsp;'
            f'<strong>{nombre}{(" " + concentracion) if concentracion else ""}</strong>'
            f'{(" — " + presentacion) if presentacion else ""}<br>'
            f'<span class="med-cant">Cant: {cantidad} ({cantidad_txt})</span>'
            f'</div>'
        )
        meds_com_html += (
            f'<div class="med-com">'
            f'<span class="med-num">{i})</span>&nbsp;'
            f"{indicaciones_html}"
            f'</div>'
        )

    diag_html = (f'<div class="diag-row"><strong>Diagnóstico:</strong> {diagnostico}</div>'
                 if diagnostico else "")

    # ── Firma ────────────────────────────────────────────────────────────────
    firma_bloque = (f'<img src="{firma_url}" alt="Firma" class="firma-img">'
                    if firma_url else '<div class="firma-linea"></div>')

    # ── URLs / recursos ──────────────────────────────────────────────────────
    base    = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/verificar/{uuid_val}"
    qr_url  = f"https://api.qrserver.com/v1/create-qr-code/?size=96x96&data={ver_url}"
    bc_doc  = f"https://bwipjs-api.metafloor.com/?bcid=code128&text={nro_doc}&scale=2&height=10&includetext=false"
    bc_cred = (f"https://bwipjs-api.metafloor.com/?bcid=code128&text={nro_credencial}&scale=2&height=10&includetext=false"
               if nro_credencial else "")

    logo_src  = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    esp_label = (especialidad or tipo_med or "").upper()
    mat_label = matricula or "—"
    anulada_pill = "<span class='anulada-pill'>⚠ ANULADA</span>" if estado == "anulada" else ""

    # ── Bloques HTML reutilizables ────────────────────────────────────────────
    cred_bc_html = f'<img class="barcode" src="{bc_cred}" alt="Cred">' if bc_cred else ""

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
        {"<div class='pf'><label>CUIL</label><strong>" + cuil + "</strong></div>" if cuil else ""}
        <div class="pf"><label>Obra Social</label><strong>{obra_social or "—"}</strong></div>
        <div class="pf"><label>Plan</label><strong>{plan or "—"}</strong></div>
        <div class="pf"><label>N° Credencial</label><strong>{nro_credencial or "—"}</strong></div>
      </div>"""

    sig_footer = f"""
      <div class="sig-footer">
        <div class="sig-left">
          <p class="sig-legal">Este documento ha sido firmado electrónicamente por<br>
          <strong>{med_nombre}</strong><br>
          conforme Ley 25.506 de Firma Digital.</p>
          <p class="sig-date">{fecha_emision}</p>
        </div>
        <div class="sig-right">
          {firma_bloque}
          <div class="firma-label">{med_nombre}</div>
          <div class="firma-sub">{esp_label} · MN {mat_label}</div>
          <div class="firma-stamp">FIRMA Y SELLO</div>
        </div>
      </div>"""

    qr_strip = f"""
      <div class="qr-strip">
        <img src="{qr_url}" width="64" height="64" alt="QR" class="qr-img">
        <div class="strip-info">
          <strong>{esp_label}</strong><br>
          {med_nombre}<br>
          <span class="strip-note">Esta receta fue creada por un emisor inscripto en DocYa — Sistema de Recetas Médicas Digitales. RL-2024-{rec_id:09d}</span>
        </div>
        <div class="strip-badge">receta<br>electrónica</div>
      </div>"""

    # ── COPY builders ────────────────────────────────────────────────────────
    # Página 1: sección Rp (solo medicamentos + diagnóstico)
    def _copy_rp(badge, extra_class=""):
        return f"""
<div class="copy {extra_class}">
  {_top(badge)}
  {pac_grid}
  <div class="sec-title">Rp/</div>
  <div class="sec-body rp-body">
    {meds_rp_html}
    {diag_html}
  </div>
  <div class="blank-space"></div>
  {sig_footer}
  {qr_strip}
</div>"""

    # Página 2: sección Indicaciones (solo comentarios)
    def _copy_ind(badge, extra_class=""):
        return f"""
<div class="copy {extra_class}">
  {_top(badge)}
  {pac_grid}
  <div class="sec-title ind-title">Indicaciones:</div>
  <div class="sec-body ind-body">
    {meds_com_html}
  </div>
  <div class="blank-space"></div>
  {sig_footer}
  {qr_strip}
</div>"""

    # Página 3: DUPLICADO con ambas secciones y marca de agua
    def _copy_full(badge, extra_class="", watermark_text=""):
        watermark_html = f'<div class="watermark">{watermark_text}</div>' if watermark_text else ""
        return f"""
<div class="copy {extra_class}">
  {watermark_html}
  {_top(badge)}
  {pac_grid}
  <div class="dup-content">
    <div class="dup-col">
      <div class="sec-title">Rp/</div>
      <div class="sec-body">
        {meds_rp_html}
        {diag_html}
      </div>
    </div>
    <div class="dup-divider"></div>
    <div class="dup-col">
      <div class="sec-title ind-title">Indicaciones:</div>
      <div class="sec-body">
        {meds_com_html}
      </div>
    </div>
  </div>
  <div class="blank-space" style="min-height:16px"></div>
  {sig_footer}
  {qr_strip}
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receta #{rec_id} — DocYa</title>
<style>
/* ── Reset ───────────────────────────────────────────────────────────────── */
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: Arial, Helvetica, sans-serif;
  font-size: 11px;
  color: #111;
  background: #e2e8f0;
  -webkit-font-smoothing: antialiased;
}}

/* ── Print ───────────────────────────────────────────────────────────────── */
@media print {{
  body {{ background: #fff; font-size: 10px; }}
  .no-print {{ display: none !important; }}
  .page {{ page-break-after: always; box-shadow: none; margin: 0; border-radius: 0; }}
  .page:last-child {{ page-break-after: avoid; }}
  @page {{ margin: 7mm; size: A4; }}
}}

/* ── Toolbar ─────────────────────────────────────────────────────────────── */
.no-print {{
  position: sticky; top: 0; z-index: 20;
  background: #1e293b; padding: 9px 16px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}}
.no-print button {{
  background: #14B8A6; color: #fff; border: none;
  padding: 6px 18px; border-radius: 20px;
  font-size: 12px; font-weight: 700; cursor: pointer;
  white-space: nowrap;
}}
.no-print a {{ color: #14B8A6; font-size: 12px; text-decoration: none; white-space: nowrap; }}
.anulada-pill {{
  background: #fef2f2; color: #dc2626; border: 1px solid #dc2626;
  border-radius: 20px; padding: 3px 10px; font-weight: 700; font-size: 11px;
}}
.page-label {{
  color: #94a3b8; font-size: 11px; margin-left: auto;
}}

/* ── Page wrapper ────────────────────────────────────────────────────────── */
.page {{
  background: #fff;
  max-width: 210mm;
  min-height: 297mm;
  margin: 14px auto;
  box-shadow: 0 4px 28px rgba(0,0,0,0.15);
  border-radius: 2px;
  display: flex;
  flex-direction: column;
  border-top: 3px solid #14B8A6;
}}

/* ── Two-copy row ────────────────────────────────────────────────────────── */
.copies {{
  display: flex;
  flex: 1;
  min-height: 0;
}}

/* ── Dashed vertical divider ─────────────────────────────────────────────── */
.copy-divider {{
  flex-shrink: 0;
  width: 1px;
  background: repeating-linear-gradient(
    to bottom, #9ca3af 0, #9ca3af 5px, transparent 5px, transparent 10px
  );
}}

/* ── Single copy ─────────────────────────────────────────────────────────── */
.copy {{
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 9px 11px 7px;
  position: relative;
  overflow: hidden;
}}

/* ── Watermark ───────────────────────────────────────────────────────────── */
.watermark {{
  position: absolute;
  top: 46%; left: 50%;
  transform: translate(-50%, -50%) rotate(-30deg);
  font-size: 58px; font-weight: 900;
  color: rgba(0,0,0,0.055);
  pointer-events: none; white-space: nowrap;
  letter-spacing: 6px; z-index: 0;
}}

/* ── Top strip (barcodes + logo + info) ──────────────────────────────────── */
.top-strip {{
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding-bottom: 7px;
  border-bottom: 1px solid #e5e7eb;
  margin-bottom: 6px;
}}
.top-barcodes {{ display: flex; flex-direction: column; gap: 2px; flex-shrink: 0; }}
.barcode {{ max-height: 22px; max-width: 80px; }}
.top-center {{ flex: 1; text-align: center; }}
.logo {{ height: 26px; display: block; margin: 0 auto 3px; }}
.copy-badge {{
  display: inline-block;
  background: linear-gradient(135deg, #0AE6C7, #0d9488);
  color: #fff; font-size: 7.5px; font-weight: 800;
  letter-spacing: 1px; padding: 2px 9px; border-radius: 9999px;
  text-transform: uppercase;
}}
.top-info {{ text-align: right; font-size: 9px; line-height: 1.6; color: #374151; flex-shrink: 0; }}
.fecha-teal {{ color: #0d9488; font-weight: 700; }}

/* ── Patient grid ────────────────────────────────────────────────────────── */
.pac-grid {{
  display: flex;
  flex-wrap: wrap;
  border: 1.5px solid #0d9488;
  border-radius: 3px;
  margin-bottom: 6px;
  overflow: hidden;
}}
.pf {{
  flex: 1 1 33%;
  padding: 3px 6px;
  border-right: 1px solid #ccfbf1;
  border-bottom: 1px solid #ccfbf1;
  min-width: 0;
}}
.pf:last-child, .pf-name {{ border-right: none; }}
.pf-name {{ flex: 1 1 100%; background: #f0fdfa; font-size: 10px; }}
.pf label {{
  display: block; font-size: 7.5px; color: #6b7280;
  text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 1px;
}}
.pf strong {{ font-size: 10px; }}

/* ── Section title ───────────────────────────────────────────────────────── */
.sec-title {{
  font-size: 13px; font-weight: 900; color: #0d9488;
  border-bottom: 1px solid #e5e7eb;
  padding-bottom: 3px; margin-bottom: 5px;
}}
.ind-title {{ font-size: 11px; font-weight: 800; color: #374151; }}

/* ── Section body ────────────────────────────────────────────────────────── */
.sec-body {{ flex: 1; }}
.rp-body  {{ min-height: 60px; }}
.ind-body {{ min-height: 60px; }}
.med-rp  {{ margin: 4px 0; line-height: 1.55; font-size: 10px; }}
.med-com {{ margin: 5px 0; font-size: 10px; line-height: 1.6; color: #374151; }}
.med-cant {{ color: #6b7280; font-size: 9px; }}
.med-num  {{ color: #0d9488; font-weight: 700; }}
.diag-row {{
  font-size: 9.5px; border-left: 2px solid #0d9488;
  padding: 2px 6px; margin-top: 6px;
  background: #f0fdfa; color: #374151;
}}

/* ── Blank writing space ─────────────────────────────────────────────────── */
.blank-space {{
  flex: 1;
  min-height: 32px;
  border: 1px dashed #d1d5db;
  border-radius: 3px;
  margin: 5px 0;
}}

/* ── Signature footer ────────────────────────────────────────────────────── */
.sig-footer {{
  display: flex;
  gap: 8px;
  border-top: 1px dashed #9ca3af;
  padding-top: 5px;
  margin-bottom: 5px;
}}
.sig-left {{ flex: 1; }}
.sig-legal {{ font-size: 7.5px; color: #6b7280; line-height: 1.6; }}
.sig-date  {{ font-size: 8px; color: #374151; margin-top: 4px; font-weight: 600; }}
.sig-right {{ min-width: 115px; text-align: center; flex-shrink: 0; }}
.firma-img  {{ max-width: 105px; max-height: 44px; object-fit: contain; display: block; margin: 0 auto 2px; }}
.firma-linea {{ width: 100px; height: 40px; border-bottom: 1.5px solid #111; margin: 0 auto 2px; }}
.firma-label {{ font-size: 8px; font-weight: 700; }}
.firma-sub   {{ font-size: 7.5px; color: #555; }}
.firma-stamp {{ font-size: 8px; font-weight: 800; color: #0d9488; margin-top: 3px; letter-spacing: 0.5px; }}

/* ── QR bottom strip ─────────────────────────────────────────────────────── */
.qr-strip {{
  display: flex;
  align-items: center;
  gap: 7px;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 3px;
  padding: 5px 7px;
}}
.qr-img {{ flex-shrink: 0; border: 1px solid #e5e7eb; border-radius: 2px; }}
.strip-info {{ flex: 1; min-width: 0; font-size: 8px; line-height: 1.55; color: #374151; }}
.strip-note {{ color: #6b7280; display: block; font-size: 7px; margin-top: 1px; }}
.strip-badge {{
  flex-shrink: 0;
  background: linear-gradient(135deg, #0AE6C7, #0d9488);
  color: #fff; font-size: 7.5px; font-weight: 800;
  text-align: center; padding: 4px 7px; border-radius: 3px;
  text-transform: uppercase; letter-spacing: 0.5px; line-height: 1.4;
}}

/* ── DUPLICADO: two columns inside single copy ───────────────────────────── */
.dup-content {{
  display: flex;
  gap: 0;
  flex: 1;
  min-height: 0;
  border: 1px solid #e5e7eb;
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 4px;
}}
.dup-col {{ flex: 1; padding: 5px 7px; display: flex; flex-direction: column; }}
.dup-col:first-child {{ background: #fff; }}
.dup-col:last-child  {{ background: #fafafa; }}
.dup-divider {{ width: 1px; background: #e5e7eb; flex-shrink: 0; }}

/* ── Single-copy page ────────────────────────────────────────────────────── */
.copies.single {{
  justify-content: center;
}}
.copies.single .copy {{
  max-width: 105mm;
}}

.copy.compact {{
  padding: 6px 9px 5px;
  min-height: 0;
}}
.copy.compact .top-strip {{
  gap: 5px;
  padding-bottom: 5px;
  margin-bottom: 5px;
}}
.copy.compact .barcode {{
  max-height: 18px;
  max-width: 70px;
}}
.copy.compact .logo {{
  height: 21px;
  margin-bottom: 2px;
}}
.copy.compact .copy-badge {{
  font-size: 6.5px;
  padding: 2px 7px;
}}
.copy.compact .top-info {{
  font-size: 8px;
  line-height: 1.4;
}}
.copy.compact .pac-grid {{
  margin-bottom: 5px;
}}
.copy.compact .pf {{
  padding: 2px 5px;
}}
.copy.compact .pf-name {{
  font-size: 9px;
}}
.copy.compact .pf label {{
  font-size: 6.5px;
}}
.copy.compact .pf strong {{
  font-size: 8.5px;
}}
.copy.compact .sec-title {{
  font-size: 11px;
  padding-bottom: 2px;
  margin-bottom: 4px;
}}
.copy.compact .ind-title {{
  font-size: 10px;
}}
.copy.compact .rp-body,
.copy.compact .ind-body {{
  min-height: 0;
}}
.copy.compact .med-rp {{
  margin: 2px 0;
  line-height: 1.35;
  font-size: 8.5px;
}}
.copy.compact .med-com {{
  margin: 2px 0;
  line-height: 1.35;
  font-size: 8.5px;
}}
.copy.compact .med-cant,
.copy.compact .diag-row {{
  font-size: 7.5px;
}}
.copy.compact .blank-space {{
  min-height: 12px;
  margin: 4px 0;
}}
.copy.compact .sig-footer {{
  gap: 6px;
  padding-top: 4px;
  margin-bottom: 4px;
}}
.copy.compact .sig-legal,
.copy.compact .firma-sub,
.copy.compact .strip-note {{
  font-size: 6.5px;
}}
.copy.compact .sig-date,
.copy.compact .firma-label,
.copy.compact .firma-stamp,
.copy.compact .strip-info {{
  font-size: 7px;
}}
.copy.compact .sig-right {{
  min-width: 92px;
}}
.copy.compact .firma-img {{
  max-width: 84px;
  max-height: 32px;
}}
.copy.compact .firma-linea {{
  width: 80px;
  height: 28px;
}}
.copy.compact .qr-strip {{
  gap: 5px;
  padding: 4px 5px;
}}
.copy.compact .qr-img {{
  width: 48px;
  height: 48px;
}}
.copy.compact .strip-badge {{
  font-size: 6.5px;
  padding: 3px 5px;
}}
.copy.compact .dup-content {{
  margin-bottom: 3px;
}}
.copy.compact .dup-col {{
  padding: 4px 5px;
}}
.copy.compact .watermark {{
  font-size: 40px;
  letter-spacing: 4px;
}}

/* ── Mobile responsive ───────────────────────────────────────────────────── */
@media (max-width: 600px) {{
  body {{ font-size: 12px; background: #f1f5f9; }}
  .page {{
    max-width: 100%;
    min-height: unset;
    margin: 8px;
    border-radius: 6px;
  }}
  .copies {{
    flex-direction: column;
  }}
  .copy-divider {{
    width: 100%;
    height: 1px;
    background: repeating-linear-gradient(
      to right, #9ca3af 0, #9ca3af 5px, transparent 5px, transparent 10px
    );
  }}
  .top-barcodes {{ display: none; }}
  .barcode {{ display: none; }}
  .top-strip {{ flex-wrap: wrap; gap: 4px; }}
  .top-info {{ font-size: 10px; }}
  .pf {{ flex: 1 1 45%; font-size: 11px; }}
  .pf strong {{ font-size: 11px; }}
  .sec-title {{ font-size: 15px; }}
  .med-rp, .med-com {{ font-size: 12px; }}
  .firma-img {{ max-width: 130px; max-height: 55px; }}
  .firma-linea {{ width: 130px; height: 50px; }}
  .qr-strip {{ gap: 10px; padding: 8px; }}
  .strip-info {{ font-size: 10px; }}
  .strip-note {{ font-size: 9px; }}
  .dup-content {{ flex-direction: column; }}
  .dup-divider {{ width: 100%; height: 1px; }}
  .copies.single .copy {{ max-width: 100%; }}
  .no-print {{ gap: 8px; }}
  .no-print button {{ font-size: 13px; padding: 8px 20px; }}
}}
</style>
</head>
<body>

<!-- Toolbar -->
<div class="no-print">
  <button onclick="window.print()">🖨 Imprimir / PDF</button>
  <a href="{ver_url}" target="_blank">🔗 Verificar</a>
  {anulada_pill}
  <span class="page-label">Receta #{rec_id}</span>
</div>

<!-- ═══ PÁGINA 1: ORIGINAL + COPIA ══════════════════════════════════════════ -->
<div class="page">
  <div class="copies">
    {_copy_full("ORIGINAL", "compact")}
    <div class="copy-divider"></div>
    {_copy_full("COPIA", "compact")}
  </div>
</div>

<!-- ═══ PÁGINA 2: DUPLICADO ══════════════════════════════════════════════════ -->
<div class="page">
  <div class="copies single">
    {_copy_full("DUPLICADO", "compact", "DUPLICADO")}
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

