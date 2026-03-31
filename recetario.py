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
# 🖨️ RECETA HTML IMPRIMIBLE
# ====================================================

from fastapi.responses import HTMLResponse
from fastapi import Depends, HTTPException
from datetime import datetime
import os
import random

@router.get("/recetas/{receta_id}/html", response_class=HTMLResponse)
def receta_html(
    receta_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.estado, r.diagnostico, r.medicamentos,
               r.obra_social, r.plan, r.nro_credencial, r.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento,
               p.sexo, p.fecha_nacimiento, p.cuil,
               m.full_name, m.matricula, m.especialidad, m.tipo, m.firma_url
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        JOIN medicos m ON m.id = r.medico_id
        WHERE r.id=%s AND r.medico_id=%s
    """, (receta_id, medico_id))

    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Receta no encontrada")

    (
        rec_id, uuid_val, estado, diagnostico, medicamentos,
        obra_social, plan, nro_credencial, creado_en,
        pac_nombre, pac_apellido, tipo_doc, nro_doc,
        sexo, fecha_nac, cuil,
        med_nombre, matricula, especialidad, tipo_med, firma_url
    ) = row

    fecha = creado_en.strftime("%d/%m/%Y") if creado_en else "—"
    fecha_nac = fecha_nac.strftime("%d/%m/%Y") if fecha_nac else "—"

    sexo_label = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo)

    # 🔹 Código RL tipo ministerio
    rl_code = f"RL-2024-{random.randint(100000000,999999999)}"

    # 🔹 QR + Barcode
    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/verificar/{uuid_val}"

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=120x120&data={ver_url}"
    barcode_url = f"https://bwipjs-api.metafloor.com/?bcid=code128&text={nro_doc}&scale=2&height=12"

    logo = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"

    # 🔹 Medicamentos
    meds = ""
    for i, m in enumerate(medicamentos or [], 1):
        meds += f"""
        <div class="med">
            {i}) <strong>{m.get("nombre","")}</strong> {m.get("concentracion","")}<br>
            Cantidad: {m.get("cantidad",1)}<br>
            {m.get("indicaciones","")}
        </div>
        """

    # 🔹 Firma
    firma = f'<img src="{firma_url}" class="firma-img">' if firma_url else '<div class="firma-line"></div>'

    def receta_copy(tipo):
        return f"""
        <div class="copy">
            
            <!-- HEADER -->
            <div class="header">
                <img src="{logo}" class="logo">

                <div class="medico">
                    <strong>{med_nombre}</strong><br>
                    {especialidad or "MÉDICO"}<br>
                    MN {matricula}
                </div>

                <div class="info">
                    <span class="badge">{tipo}</span><br>
                    Fecha: {fecha}<br>
                    <span class="rl">{rl_code}</span>
                </div>
            </div>

            <!-- PACIENTE -->
            <div class="paciente">
                <p><strong>Paciente:</strong> {pac_apellido}, {pac_nombre} | Sexo: {sexo_label}</p>
                <p><strong>{tipo_doc}:</strong> {nro_doc} | CUIL: {cuil or "—"} | Nac: {fecha_nac}</p>
                <p><strong>{obra_social or "—"}</strong> | Plan: {plan or "—"} | Credencial: {nro_credencial or "—"}</p>
            </div>

            <!-- RP -->
            <div class="rp">
                <h3>Rp:</h3>
                {meds}
            </div>

            <!-- DIAG -->
            <div class="diag">
                <strong>Diagnóstico:</strong> {diagnostico or "—"}
            </div>

            <!-- FIRMA -->
            <div class="firma">
                {firma}
                <p><strong>{med_nombre}</strong></p>
                <p>MN {matricula}</p>
                <span>FIRMA Y SELLO</span>
            </div>

            <!-- FOOTER -->
            <div class="footer">
                <img src="{barcode_url}" class="barcode">
                <img src="{qr_url}" class="qr">

                <p class="legal">
                    Documento firmado electrónicamente bajo Ley 25.506.  
                    Verificar en: {ver_url}
                </p>
            </div>

        </div>
        """

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>
body {{
    margin:0;
    font-family: Arial;
    background:#f1f5f9;
}}

.page {{
    width:210mm;
    height:297mm;
    margin:auto;
    background:white;
    padding:10mm;
    display:flex;
    flex-direction:column;
    justify-content:space-between;
}}

.copies {{
    display:flex;
    gap:10px;
    height:48%;
}}

.copy {{
    flex:1;
    border:1px solid #e5e7eb;
    border-top:4px solid #14B8A6;
    padding:10px;
    display:flex;
    flex-direction:column;
}}

.header {{
    display:flex;
    justify-content:space-between;
    align-items:center;
}}

.logo {{
    height:35px;
}}

.medico {{
    font-size:10px;
    text-align:center;
}}

.info {{
    text-align:right;
    font-size:9px;
}}

.badge {{
    background:#14B8A6;
    color:white;
    padding:2px 6px;
    border-radius:6px;
    font-weight:bold;
}}

.rl {{
    color:#14B8A6;
    font-weight:bold;
}}

.paciente {{
    margin-top:5px;
    border-top:1px solid #ccc;
    border-bottom:1px solid #ccc;
    padding:5px 0;
    font-size:10px;
}}

.rp {{
    margin-top:10px;
    flex:1;
}}

.med {{
    margin-bottom:5px;
    font-size:10px;
}}

.diag {{
    font-size:10px;
    margin-top:5px;
}}

.firma {{
    text-align:center;
    margin-top:10px;
    font-size:10px;
}}

.firma-img {{
    max-height:50px;
}}

.firma-line {{
    width:120px;
    height:40px;
    border-bottom:1px solid black;
    margin:auto;
}}

.footer {{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-top:10px;
}}

.barcode {{
    height:40px;
}}

.qr {{
    height:80px;
}}

.legal {{
    font-size:8px;
    text-align:center;
    width:100%;
    margin-top:5px;
}}

button {{
    position:fixed;
    top:10px;
    left:10px;
    background:#14B8A6;
    color:white;
    border:none;
    padding:6px 12px;
    border-radius:6px;
}}

@media print {{
    button {{display:none;}}
    body {{background:white;}}
}}
</style>

</head>

<body>

<button onclick="window.print()">Imprimir</button>

<div class="page">
    <div class="copies">
        {receta_copy("ORIGINAL")}
        {receta_copy("COPIA")}
    </div>

    <div class="copies">
        {receta_copy("DUPLICADO")}
    </div>
</div>

</body>
</html>
"""

    return HTMLResponse(html)
