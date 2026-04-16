# ====================================================
# ðŸ“‹ RECETARIO â€” Pacientes y Recetas por MÃ©dico
# ====================================================
# Endpoints:
#   POST   /recetario/pacientes               â†’ Crear paciente
#   GET    /recetario/pacientes               â†’ Listar mis pacientes
#   GET    /recetario/pacientes/{id}          â†’ Ver paciente
#   PUT    /recetario/pacientes/{id}          â†’ Editar paciente
#   DELETE /recetario/pacientes/{id}          â†’ Eliminar paciente
#
#   POST   /recetario/recetas                 â†’ Emitir receta
#   GET    /recetario/recetas                 â†’ Mis recetas (historial)
#   GET    /recetario/recetas/{id}            â†’ Ver receta (JSON)
#   GET    /recetario/recetas/{id}/html       â†’ Ver receta (HTML imprimible)
#   PATCH  /recetario/recetas/{id}/anular     â†’ Anular receta
#
#   GET    /recetario/verificar/{uuid}        â†’ Verificar autenticidad pÃºblica
# ====================================================

import base64
import json
import logging
import os
import random
import re
import time
import jwt
import psycopg2
from datetime import datetime
from html import escape
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from services.farmalink import create_farmalink_payload, send_prescription_to_farmalink

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET   = os.getenv("JWT_SECRET", "change_me")
LOGGER = logging.getLogger("docya.recetario")

router = APIRouter(prefix="/recetario", tags=["Recetario"])


# ====================================================
# ðŸ§© DB
# ====================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


# ====================================================
# ðŸ” AUTH â€” extrae medico_id del JWT Bearer
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
        raise HTTPException(status_code=401, detail="Token invÃ¡lido")


# ====================================================
# ðŸ“¦ MODELOS Pydantic
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
    nombre:         Optional[str] = None      # fallback legacy
    ifa:            Optional[str] = None
    nombre_comercial: Optional[str] = None
    forma_farmaceutica: Optional[str] = None
    concentracion:  Optional[str] = None
    presentacion:   Optional[str] = None      # "Envase x 30 comprimidos"
    cantidad:       int = 1
    indicaciones:   str                       # "Tomar 1 cada 8hs por 7 dÃ­as"

class RecetaIn(BaseModel):
    paciente_id:    int
    obra_social:    Optional[str] = None
    plan:           Optional[str] = None
    nro_credencial: Optional[str] = None
    diagnostico:    Optional[str] = None
    medicamentos:   List[MedicamentoItem]

class AnularIn(BaseModel):
    motivo: Optional[str] = None


CERTIFICADO_TIPOS = {
    "ausentismo_laboral": "Ausentismo laboral",
    "ausentismo_escolar": "Ausentismo escolar",
    "constancia_asistencia": "Constancia de asistencia",
    "reposo_domiciliario": "Reposo domiciliario",
}


def _ensure_recetario_certificados_schema(db) -> None:
    _ensure_recetario_recetas_schema(db)
    cur = db.cursor()
    cur.execute("""
        ALTER TABLE recetario_certificados
        ADD COLUMN IF NOT EXISTS tipo_certificado VARCHAR(40)
    """)
    cur.execute("""
        ALTER TABLE recetario_certificados
        ADD COLUMN IF NOT EXISTS campos_json JSONB
    """)
    cur.execute("""
        UPDATE recetario_certificados
        SET tipo_certificado = COALESCE(tipo_certificado, 'reposo_domiciliario'),
            campos_json = COALESCE(campos_json, '{}'::jsonb)
        WHERE tipo_certificado IS NULL OR campos_json IS NULL
    """)
    db.commit()


def _certificado_tipo_label(tipo: Optional[str]) -> str:
    return CERTIFICADO_TIPOS.get(tipo or "", "Certificado mÃ©dico")


def _certificado_campos(campos_raw) -> Dict[str, Any]:
    if isinstance(campos_raw, dict):
        return campos_raw
    if not campos_raw:
        return {}
    if isinstance(campos_raw, str):
        try:
            value = json.loads(campos_raw)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _fmt_fecha(value) -> str:
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _fmt_datetime(value) -> str:
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value)


def _edad_paciente(fecha_nacimiento) -> Optional[int]:
    if not fecha_nacimiento:
        return None
    today = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    years = today.year - fecha_nacimiento.year
    if (today.month, today.day) < (fecha_nacimiento.month, fecha_nacimiento.day):
        years -= 1
    return years


def _valor_campo(campos: Dict[str, Any], key: str, default: str = "-") -> str:
    value = campos.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _render_certificado_body(
    *,
    tipo_certificado: str,
    campos: Dict[str, Any],
    paciente_nombre: str,
    paciente_documento: str,
    edad: Optional[int],
    diagnostico: Optional[str],
    reposo_dias: Optional[int],
    fecha_emision: str,
) -> str:
    paciente = escape(paciente_nombre)
    documento = escape(paciente_documento)
    edad_txt = str(edad) if edad is not None else "-"
    diagnostico_html = escape(diagnostico or "Sin diagn&oacute;stico especificado")

    if tipo_certificado == "ausentismo_laboral":
        return f"""
  <div class="body-grid">
    <div class="body-copy">
      <div class="body-kicker">Constancia profesional</div>
      <h2>Ausentismo laboral</h2>
      <p>Se deja constancia de que <strong>{paciente}</strong>, {documento}, de <strong>{edad_txt}</strong> a&ntilde;os, fue evaluado/a por el profesional firmante en fecha <strong>{fecha_emision}</strong>.</p>
      <p>Diagn&oacute;stico o motivo cl&iacute;nico informado: <strong>{diagnostico_html}</strong>.</p>
      <p>Se indica <strong>{escape(_valor_campo(campos, 'tipo_indicacion', 'ausencia laboral justificada'))}</strong> por <strong>{escape(_valor_campo(campos, 'dias_indicados', str(reposo_dias or '-')))}</strong> d&iacute;a(s), desde <strong>{escape(_valor_campo(campos, 'fecha_inicio'))}</strong> hasta <strong>{escape(_valor_campo(campos, 'fecha_fin'))}</strong>.</p>
      <p>El presente se extiende para ser presentado ante <strong>{escape(_valor_campo(campos, 'presentar_ante'))}</strong>.</p>
    </div>
    <div class="body-side">
      <div class="side-card">
        <span class="side-label">Indicacion</span>
        <strong>{escape(_valor_campo(campos, 'tipo_indicacion', 'Ausencia laboral justificada'))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Periodo</span>
        <strong>{escape(_valor_campo(campos, 'fecha_inicio'))}</strong>
        <small>hasta {escape(_valor_campo(campos, 'fecha_fin'))}</small>
      </div>
      <div class="side-card">
        <span class="side-label">Dias</span>
        <strong>{escape(_valor_campo(campos, 'dias_indicados', str(reposo_dias or '-')))}</strong>
      </div>
    </div>
  </div>"""

    if tipo_certificado == "ausentismo_escolar":
        return f"""
  <div class="body-grid">
    <div class="body-copy">
      <div class="body-kicker">Certificaci&oacute;n para instituci&oacute;n educativa</div>
      <h2>Ausentismo escolar</h2>
      <p>Se certifica que <strong>{paciente}</strong>, {documento}, de <strong>{edad_txt}</strong> a&ntilde;os, fue evaluado/a por el profesional firmante.</p>
      <p>Motivo cl&iacute;nico o cuadro constatado: <strong>{diagnostico_html}</strong>.</p>
      <p>Por tal motivo, estuvo imposibilitado/a de concurrir al establecimiento educativo <strong>{escape(_valor_campo(campos, 'institucion'))}</strong> desde <strong>{escape(_valor_campo(campos, 'fecha_desde'))}</strong> hasta <strong>{escape(_valor_campo(campos, 'fecha_hasta'))}</strong>, por <strong>{escape(_valor_campo(campos, 'dias_habiles'))}</strong> d&iacute;a(s) h&aacute;biles.</p>
      <p>Consta adem&aacute;s que el presente se emite a solicitud de <strong>{escape(_valor_campo(campos, 'responsable'))}</strong>.</p>
    </div>
    <div class="body-side">
      <div class="side-card">
        <span class="side-label">Institucion</span>
        <strong>{escape(_valor_campo(campos, 'institucion'))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Responsable</span>
        <strong>{escape(_valor_campo(campos, 'responsable'))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Periodo</span>
        <strong>{escape(_valor_campo(campos, 'fecha_desde'))}</strong>
        <small>hasta {escape(_valor_campo(campos, 'fecha_hasta'))}</small>
      </div>
    </div>
  </div>"""

    if tipo_certificado == "constancia_asistencia":
        return f"""
  <div class="body-grid">
    <div class="body-copy">
      <div class="body-kicker">Documento sin revelaci&oacute;n diagn&oacute;stica obligatoria</div>
      <h2>Constancia de asistencia</h2>
      <p>Se deja constancia de que <strong>{paciente}</strong>, {documento}, concurri&oacute; a consulta m&eacute;dica el d&iacute;a <strong>{escape(_valor_campo(campos, 'fecha_asistencia', fecha_emision.split(' ')[0]))}</strong> a las <strong>{escape(_valor_campo(campos, 'hora_asistencia'))}</strong>.</p>
      <p>La atenci&oacute;n tuvo una duraci&oacute;n aproximada de <strong>{escape(_valor_campo(campos, 'duracion_minutos'))}</strong> minutos.</p>
      <p>Motivo de consulta consignado: <strong>{escape(_valor_campo(campos, 'motivo_consulta', diagnostico or 'Consulta m&eacute;dica general'))}</strong>.</p>
      <p>La presente constancia se emite a pedido del/la interesado/a para ser presentada ante quien corresponda, manteniendo reserva profesional sobre detalles cl&iacute;nicos adicionales.</p>
    </div>
    <div class="body-side">
      <div class="side-card">
        <span class="side-label">Hora</span>
        <strong>{escape(_valor_campo(campos, 'hora_asistencia'))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Duracion</span>
        <strong>{escape(_valor_campo(campos, 'duracion_minutos'))} min</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Motivo</span>
        <strong>{escape(_valor_campo(campos, 'motivo_consulta', diagnostico or 'Consulta m&eacute;dica'))}</strong>
      </div>
    </div>
  </div>"""

    return f"""
  <div class="body-grid">
    <div class="body-copy">
      <div class="body-kicker">Indicaci&oacute;n cl&iacute;nica</div>
      <h2>Reposo domiciliario</h2>
      <p>Se certifica que <strong>{paciente}</strong>, {documento}, de <strong>{edad_txt}</strong> a&ntilde;os, fue evaluado/a por el profesional firmante.</p>
      <p>Diagn&oacute;stico o cuadro cl&iacute;nico: <strong>{diagnostico_html}</strong>.</p>
      <p>Se prescribe <strong>reposo domiciliario {escape(_valor_campo(campos, 'tipo_reposo', 'relativo'))}</strong> por <strong>{escape(_valor_campo(campos, 'dias_indicados', str(reposo_dias or '-')))}</strong> d&iacute;a(s), desde <strong>{escape(_valor_campo(campos, 'fecha_inicio'))}</strong> hasta <strong>{escape(_valor_campo(campos, 'fecha_fin'))}</strong>.</p>
      <p>Indicaciones adicionales: <strong>{escape(_valor_campo(campos, 'indicaciones_adicionales', 'Sin indicaciones adicionales'))}</strong>.</p>
    </div>
    <div class="body-side">
      <div class="side-card">
        <span class="side-label">Tipo</span>
        <strong>{escape(_valor_campo(campos, 'tipo_reposo', 'Relativo'))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Dias</span>
        <strong>{escape(_valor_campo(campos, 'dias_indicados', str(reposo_dias or '-')))}</strong>
      </div>
      <div class="side-card">
        <span class="side-label">Periodo</span>
        <strong>{escape(_valor_campo(campos, 'fecha_inicio'))}</strong>
        <small>hasta {escape(_valor_campo(campos, 'fecha_fin'))}</small>
      </div>
    </div>
  </div>"""


def _medicamento_campos(m: dict) -> tuple[str, str, str, str, str]:
    ifa = (m.get("ifa") or m.get("principio_activo_str") or m.get("nombre") or "").strip()
    nombre_comercial = (m.get("nombre_comercial") or "").strip()
    forma = (m.get("forma_farmaceutica") or m.get("forma") or "").strip()
    concentracion = (m.get("concentracion") or "").strip()
    presentacion = (m.get("presentacion") or "").strip()

    if nombre_comercial and ifa and nombre_comercial.lower() == ifa.lower():
        nombre_comercial = ""

    if not ifa:
        ifa = nombre_comercial or "Medicamento"

    return ifa, nombre_comercial, forma, concentracion, presentacion


def _detalle_medicamento(forma: str, concentracion: str, presentacion: str) -> str:
    forma_concentracion = " ".join(part for part in [forma, concentracion] if part).strip()
    if not presentacion:
        return forma_concentracion
    if not forma_concentracion:
        return presentacion

    presentacion_norm = " ".join(presentacion.lower().split())
    forma_norm = " ".join(forma_concentracion.lower().split())

    if presentacion_norm == forma_norm:
        return presentacion
    if presentacion_norm.startswith(forma_norm):
        return presentacion
    if forma_norm.startswith(presentacion_norm):
        return forma_concentracion

    return f"{forma_concentracion} &mdash; {presentacion}"


def _ensure_recetario_recetas_schema(db) -> None:
    cur = db.cursor()
    cur.execute("""
        ALTER TABLE recetario_recetas
        ADD COLUMN IF NOT EXISTS cuir VARCHAR(50)
    """)
    cur.execute("""
        ALTER TABLE recetario_recetas
        ADD COLUMN IF NOT EXISTS sent_to_farmalink BOOLEAN NOT NULL DEFAULT FALSE
    """)
    cur.execute("""
        ALTER TABLE recetario_recetas
        ADD COLUMN IF NOT EXISTS farmalink_response JSONB
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_recetario_recetas_cuir
        ON recetario_recetas (cuir)
        WHERE cuir IS NOT NULL
    """)
    db.commit()


def _normalize_digits(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def _sexo_label(sexo: Optional[str]) -> str:
    return {"M": "Masculino", "F": "Femenino", "X": "X / No binario"}.get((sexo or "").upper(), sexo or "—")


def _build_patient_cuil(nro_documento: Optional[str], sexo: Optional[str]) -> Optional[str]:
    dni = _normalize_digits(nro_documento)
    if len(dni) < 7:
        return None
    dni = dni.zfill(8)
    prefix = {"M": "20", "F": "27"}.get((sexo or "").upper(), "23")
    base = f"{prefix}{dni}"
    multipliers = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(digit) * factor for digit, factor in zip(base, multipliers))
    remainder = 11 - (total % 11)
    if remainder == 11:
        check_digit = "0"
    elif remainder == 10:
        if prefix == "20":
            base = f"23{dni}"
            check_digit = "9"
        elif prefix == "27":
            base = f"23{dni}"
            check_digit = "4"
        else:
            check_digit = "3"
    else:
        check_digit = str(remainder)
    return f"{base}{check_digit}"


def _generate_prescription_group_id() -> str:
    timestamp = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%Y%m%d%H%M%S%f")
    random_suffix = f"{random.SystemRandom().randint(0, 99999):05d}"
    return f"{timestamp}{random_suffix}"[:25]


def _build_cuir(group_id: str, item_number: str = "01") -> str:
    return f"02590000020101{group_id}{item_number}"


def _generate_unique_cuir(db) -> str:
    cur = db.cursor()
    for _ in range(25):
        cuir = _build_cuir(_generate_prescription_group_id())
        cur.execute("SELECT 1 FROM recetario_recetas WHERE cuir=%s LIMIT 1", (cuir,))
        if not cur.fetchone():
            return cuir
        time.sleep(0.005)
    raise HTTPException(500, "No se pudo generar un CUIR único")


_CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212",
    "221213", "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221",
    "223211", "221132", "221231", "213212", "223112", "312131", "311222", "321122", "321221",
    "312212", "322112", "322211", "212123", "212321", "232121", "111323", "131123", "131321",
    "112313", "132113", "132311", "211313", "231113", "231311", "112133", "112331", "132131",
    "113123", "113321", "133121", "313121", "211331", "231131", "213113", "213311", "213131",
    "311123", "311321", "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214", "112412", "122114",
    "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111", "111242",
    "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311",
    "113141", "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]


def _code128_svg(value: str) -> str:
    if not value:
        return ""

    start_code_b = 104
    stop_code = 106
    values = [start_code_b] + [ord(char) - 32 for char in value]
    checksum = start_code_b
    for idx, code in enumerate(values[1:], 1):
        checksum += code * idx
    values.extend([checksum % 103, stop_code])

    bar_width = 2
    quiet_zone = 12
    height = 52
    x = quiet_zone
    rects: List[str] = []

    for code in values:
        pattern = _CODE128_PATTERNS[code]
        for pos, width_char in enumerate(pattern):
            width = int(width_char) * bar_width
            if pos % 2 == 0:
                rects.append(f'<rect x="{x}" y="0" width="{width}" height="{height}" fill="#111827" />')
            x += width

    total_width = x + quiet_zone
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="{height + 24}" '
        f'viewBox="0 0 {total_width} {height + 24}" role="img" aria-label="Barcode {escape(value)}">'
        f'<rect width="{total_width}" height="{height + 24}" fill="white" />'
        f'{"".join(rects)}'
        f'<text x="{total_width / 2}" y="{height + 18}" text-anchor="middle" '
        f'font-family="Arial, Helvetica, sans-serif" font-size="12" fill="#111827">{escape(value)}</text>'
        f'</svg>'
    )


def _barcode_data_uri(value: str) -> str:
    svg = _code128_svg(value)
    if not svg:
        return ""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _medication_display_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    ifa = (raw.get("ifa") or raw.get("principio_activo_str") or raw.get("nombre") or "").strip()
    commercial_name = (raw.get("nombre_comercial") or "").strip()
    pharmaceutical_form = (raw.get("forma_farmaceutica") or raw.get("forma") or "").strip()
    presentation = (raw.get("presentacion") or "").strip()
    return {
        "ifa": ifa,
        "commercial_name": commercial_name if commercial_name and commercial_name.lower() != ifa.lower() else "",
        "presentation": presentation,
        "pharmaceutical_form": pharmaceutical_form,
        "quantity": raw.get("cantidad", 1),
        "instructions": (raw.get("indicaciones") or "").strip(),
        "detail": _detalle_medicamento(pharmaceutical_form, (raw.get("concentracion") or "").strip(), presentation),
    }


def _prepare_farmalink_record(*, row: tuple) -> Dict[str, Any]:
    (
        receta_id, cuir, diagnostico, medicamentos, creado_en,
        pac_nombre, pac_apellido, pac_dni, pac_sexo, pac_cuil,
        med_nombre, matricula, especialidad, tipo_med, direccion_medico
    ) = row

    return {
        "id": receta_id,
        "cuir": cuir,
        "diagnosis": diagnostico,
        "issued_at": creado_en.isoformat() if creado_en else None,
        "patient": {
            "full_name": f"{pac_apellido}, {pac_nombre}",
            "dni": pac_dni,
            "sexo": pac_sexo,
            "cuil": pac_cuil or _build_patient_cuil(pac_dni, pac_sexo),
        },
        "doctor": {
            "full_name": med_nombre,
            "specialty": especialidad or tipo_med,
            "license_number": matricula,
            "care_address": direccion_medico,
        },
        "medications": [_medication_display_fields(m) for m in (medicamentos or [])],
    }


def _send_prescription_to_farmalink_task(receta_id: int) -> None:
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.id, r.cuir, r.diagnostico, r.medicamentos, r.creado_en,
                   p.nombre, p.apellido, p.nro_documento, p.sexo, p.cuil,
                   m.full_name, m.matricula, m.especialidad, m.tipo, m.direccion
            FROM recetario_recetas r
            JOIN recetario_pacientes p ON p.id = r.paciente_id
            JOIN medicos m ON m.id = r.medico_id
            WHERE r.id=%s
        """, (receta_id,))
        row = cur.fetchone()
        if not row:
            LOGGER.warning("No se encontró receta %s para envío Farmalink", receta_id)
            return

        payload = create_farmalink_payload(_prepare_farmalink_record(row=row))
        response = send_prescription_to_farmalink(payload)
        cur.execute("""
            UPDATE recetario_recetas
            SET sent_to_farmalink=%s,
                farmalink_response=%s::jsonb,
                updated_at=NOW()
            WHERE id=%s
        """, (bool(response.get("ok")), json.dumps(response, ensure_ascii=False), receta_id))
        conn.commit()
    except Exception:
        LOGGER.exception("Error enviando receta %s a Farmalink", receta_id)
        conn.rollback()
    finally:
        conn.close()


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
# ðŸ‘¤ PACIENTES
# ====================================================

@router.post("/pacientes", status_code=201)
def crear_paciente(
    data: PacienteIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Registra un nuevo paciente vinculado al mÃ©dico autenticado."""
    _ensure_recetario_patient_columns(db)
    if data.tipo_documento not in TIPOS_DOC:
        raise HTTPException(400, f"tipo_documento invÃ¡lido. Opciones: {TIPOS_DOC}")
    if data.sexo not in SEXOS:
        raise HTTPException(400, f"sexo invÃ¡lido. Opciones: {SEXOS}")

    cur = db.cursor()

    # Verificar duplicado por mÃ©dico + tipo + nro
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
    """Lista todos los pacientes del mÃ©dico. Filtra por nombre/documento con ?q="""
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
        raise HTTPException(400, f"tipo_documento invÃ¡lido. Opciones: {TIPOS_DOC}")
    if data.sexo not in SEXOS:
        raise HTTPException(400, f"sexo invÃ¡lido. Opciones: {SEXOS}")

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
        raise HTTPException(400, "El paciente tiene recetas activas. AnulÃ¡ las recetas primero.")

    cur.execute("""
        DELETE FROM recetario_pacientes WHERE id=%s AND medico_id=%s RETURNING id
    """, (paciente_id, medico_id))
    if not cur.fetchone():
        db.rollback()
        raise HTTPException(404, "Paciente no encontrado o sin permiso")
    db.commit()
    return {"ok": True}


# ====================================================
# ðŸ’Š RECETAS
# ====================================================

@router.post("/recetas", status_code=201)
def emitir_receta(
    data: RecetaIn,
    background_tasks: BackgroundTasks,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Emite una nueva receta. El mÃ©dico selecciona uno de sus pacientes."""
    if not data.medicamentos:
        raise HTTPException(400, "DebÃ©s incluir al menos un medicamento")

    cur = db.cursor()

    # Verificar que el paciente pertenece al mÃ©dico
    cur.execute("""
        SELECT id, nombre, apellido, obra_social, plan, nro_credencial FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (data.paciente_id, medico_id))
    pac = cur.fetchone()
    if not pac:
        raise HTTPException(404, "Paciente no encontrado en tu listado")

    import json as _json
    meds_json = _json.dumps([m.dict() for m in data.medicamentos], ensure_ascii=False)
    cuir = _generate_unique_cuir(db)
    obra_social = data.obra_social or pac[3]
    plan = data.plan or pac[4]
    nro_credencial = data.nro_credencial or pac[5]

    cur.execute("""
        INSERT INTO recetario_recetas
            (medico_id, paciente_id, obra_social, plan, nro_credencial,
             diagnostico, medicamentos, cuir, sent_to_farmalink)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,FALSE)
        RETURNING id, uuid, creado_en, cuir
    """, (
        medico_id,
        data.paciente_id,
        obra_social,
        plan,
        nro_credencial,
        data.diagnostico,
        meds_json,
        cuir
    ))
    row = cur.fetchone()
    db.commit()

    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    background_tasks.add_task(_send_prescription_to_farmalink_task, row[0])
    return {
        "ok": True,
        "id": row[0],
        "receta_id": row[0],
        "uuid": str(row[1]),
        "cuir": row[3],
        "creado_en": str(row[2]),
        "url_html": f"{base}/recetario/recetas/{row[0]}/html",
        "url_verificar": f"{base}/recetario/verificar/{row[1]}",
        "pdf_url": f"{base}/recetario/recetas/{row[0]}/html",
        "status": "generated",
    }


@router.get("/recetas")
def listar_recetas(
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Historial de recetas del mÃ©dico."""
    _ensure_recetario_recetas_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.cuir, r.estado, r.diagnostico, r.creado_en,
               r.sent_to_farmalink,
               p.nombre, p.apellido, p.nro_documento, p.tipo_documento
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        WHERE r.medico_id=%s
        ORDER BY r.creado_en DESC
    """, (medico_id,))

    recetas = []
    for row in cur.fetchall():
        recetas.append({
            "id": row[0], "uuid": str(row[1]), "cuir": row[2], "estado": row[3],
            "diagnostico": row[4],
            "fecha": row[5].strftime("%d/%m/%Y %H:%M") if row[5] else None,
            "sent_to_farmalink": bool(row[6]),
            "paciente": f"{row[8]}, {row[7]}",
            "documento": f"{row[10]} {row[9]}",
        })
    return {"total": len(recetas), "recetas": recetas}


@router.get("/recetas/{receta_id}")
def ver_receta_json(
    receta_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    _ensure_recetario_recetas_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.cuir, r.estado, r.diagnostico, r.medicamentos,
               r.obra_social, r.plan, r.nro_credencial, r.creado_en, r.motivo_anulacion,
               r.sent_to_farmalink, r.farmalink_response,
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
        "id": row[0], "uuid": str(row[1]), "cuir": row[2], "estado": row[3],
        "diagnostico": row[4], "medicamentos": row[5],
        "obra_social": row[6], "plan": row[7], "nro_credencial": row[8],
        "fecha": row[9].strftime("%d/%m/%Y %H:%M") if row[9] else None,
        "motivo_anulacion": row[10],
        "sent_to_farmalink": bool(row[11]),
        "farmalink_response": row[12],
        "paciente": {
            "nombre": row[13], "apellido": row[14],
            "tipo_documento": row[15], "nro_documento": row[16],
            "sexo": row[17], "fecha_nacimiento": str(row[18]) if row[18] else None,
            "cuil": row[19] or _build_patient_cuil(row[16], row[17]),
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
# ðŸŒ VERIFICADOR PÃšBLICO (sin auth)
# ====================================================

@router.get("/verificar/{uuid_receta}", response_class=HTMLResponse)
def verificar_receta(uuid_receta: str, db=Depends(get_db)):
    """
    PÃ¡gina pÃºblica de verificaciÃ³n de autenticidad de una receta.
    Accesible desde el QR impreso en la receta.
    """
    _ensure_recetario_recetas_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT r.uuid, r.cuir, r.estado, r.diagnostico, r.creado_en,
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

    uuid_val, cuir, estado, diagnostico, creado_en, pac_nombre, pac_apellido, \
        med_nombre, matricula, especialidad, tipo_med = row

    fecha_str = creado_en.strftime("%d de %B de %Y") if creado_en else "â€”"
    es_valida  = estado == "valida"

    return HTMLResponse(_html_verificacion(
        uuid=str(uuid_val),
        cuir=cuir or "—",
        estado=estado,
        es_valida=es_valida,
        fecha=fecha_str,
        paciente=f"{pac_apellido}, {pac_nombre}",
        medico=med_nombre,
        matricula=matricula or "â€”",
        especialidad=especialidad or tipo_med or "â€”",
        diagnostico=diagnostico or "â€”",
    ))


# ====================================================
# ðŸ“œ CERTIFICADOS MÃ‰DICOS
# ====================================================

class CertificadoIn(BaseModel):
    paciente_id:   int
    tipo_certificado: str
    diagnostico:   Optional[str] = None
    reposo_dias:   Optional[int] = None
    observaciones: Optional[str] = None
    campos:        Optional[Dict[str, Any]] = None

@router.post("/certificados", status_code=201)
def emitir_certificado(
    data: CertificadoIn,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Emite un certificado mÃ©dico y lo persiste."""
    _ensure_recetario_certificados_schema(db)
    if data.tipo_certificado not in CERTIFICADO_TIPOS:
        raise HTTPException(400, f"tipo_certificado invÃ¡lido. Opciones: {list(CERTIFICADO_TIPOS.keys())}")
    cur = db.cursor()
    # Verificar que el paciente pertenece al mÃ©dico
    cur.execute("""
        SELECT id FROM recetario_pacientes
        WHERE id=%s AND medico_id=%s
    """, (data.paciente_id, medico_id))
    if not cur.fetchone():
        raise HTTPException(404, "Paciente no encontrado")

    cur.execute("""
        INSERT INTO recetario_certificados
            (medico_id, paciente_id, tipo_certificado, diagnostico, reposo_dias, observaciones, campos_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id, creado_en
    """, (
        medico_id,
        data.paciente_id,
        data.tipo_certificado,
        data.diagnostico,
        data.reposo_dias,
        data.observaciones,
        json.dumps(data.campos or {}, ensure_ascii=False),
    ))
    row = cur.fetchone()
    db.commit()
    return {"id": row[0], "creado_en": str(row[1]),
            "url_html": f"/recetario/certificados/{row[0]}/html"}


@router.get("/certificados")
def listar_certificados(
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Lista todos los certificados emitidos por el mÃ©dico."""
    _ensure_recetario_certificados_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.tipo_certificado, c.diagnostico, c.reposo_dias, c.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento
        FROM recetario_certificados c
        JOIN recetario_pacientes p ON p.id = c.paciente_id
        WHERE c.medico_id = %s
        ORDER BY c.creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()
    return {"total": len(rows), "certificados": [
        {
            "id": r[0], "tipo_certificado": r[1], "tipo_label": _certificado_tipo_label(r[1]),
            "diagnostico": r[2], "reposo_dias": r[3],
            "fecha": r[4].strftime("%d/%m/%Y") if r[4] else None,
            "paciente": f"{r[6]}, {r[5]}",
            "documento": f"{r[7]} {r[8]}",
        } for r in rows
    ]}


@router.get("/certificados/{cert_id}/html", response_class=HTMLResponse)
def certificado_html(
    cert_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Devuelve el certificado en HTML listo para imprimir / guardar como PDF."""
    _ensure_recetario_certificados_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.tipo_certificado, c.diagnostico, c.reposo_dias, c.observaciones, c.campos_json, c.creado_en,
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

    (cert_id_val, tipo_certificado, diagnostico, reposo_dias, observaciones, campos_json, creado_en,
     pac_nombre, pac_apellido, tipo_doc, nro_doc,
     sexo, fecha_nac, cuil, obra_social,
     med_nombre, matricula, especialidad, tipo_med, firma_url) = row

    campos = _certificado_campos(campos_json)
    fecha_emision = _fmt_fecha(creado_en)
    fecha_emision_larga = _fmt_datetime(creado_en)
    fecha_nac_str = _fmt_fecha(fecha_nac)
    sexo_label = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo or "-")
    esp_label = (especialidad or tipo_med or "M&eacute;dico/a").title()
    mat_label = matricula or "-"
    paciente_nombre = f"{pac_apellido.upper()}, {pac_nombre}"
    paciente_documento = f"{tipo_doc} {nro_doc}"
    edad = _edad_paciente(fecha_nac)

    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/certificados/{cert_id_val}/html"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=110x110&data={ver_url}"
    logo_src = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    titulo_cert = _certificado_tipo_label(tipo_certificado)
    firma_bloque = (f'<img src="{firma_url}" class="firma-img" alt="Firma">' if firma_url else '<div class="firma-linea"></div>')
    obs_html = f"<div class='note-box'><strong>Observaciones:</strong> {escape(observaciones)}</div>" if observaciones else ""
    body_html = _render_certificado_body(
        tipo_certificado=tipo_certificado or "reposo_domiciliario",
        campos=campos,
        paciente_nombre=paciente_nombre,
        paciente_documento=paciente_documento,
        edad=edad,
        diagnostico=diagnostico,
        reposo_dias=reposo_dias,
        fecha_emision=fecha_emision,
    )

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(titulo_cert)} - DocYa</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
 :root {{
  --teal: #14b8a6;
  --teal-dark: #0f766e;
  --ink: #0f172a;
  --muted: #64748b;
  --line: #dbe4ea;
  --soft: #f4fbfa;
  --soft-2: #eef7ff;
}}
body {{
  font-family: Arial, Helvetica, sans-serif;
  font-size: 13px;
  color: var(--ink);
  background: #e2e8f0;
  -webkit-font-smoothing: antialiased;
}}
@media print {{
  body {{ background: #fff; }}
  .no-print {{ display: none !important; }}
  .page {{ box-shadow: none; margin: 0; border-radius: 0; }}
  @page {{ margin: 12mm; size: A4; }}
}}
.no-print {{
  position: sticky; top: 0; z-index: 20;
  background: #1e293b; padding: 9px 16px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}}
.no-print button {{
  background: var(--teal); color: #fff; border: none;
  padding: 6px 20px; border-radius: 20px;
  font-size: 12px; font-weight: 700; cursor: pointer;
}}
.no-print a {{ color: var(--teal); font-size: 12px; text-decoration: none; }}
.page {{
  background: #fff;
  max-width: 210mm;
  min-height: 297mm;
  margin: 16px auto;
  padding: 34px 40px 30px;
  box-shadow: 0 4px 28px rgba(0,0,0,0.14);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
.header {{
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 20px;
  align-items: start;
  border-bottom: 3px solid var(--teal);
  padding-bottom: 16px;
  margin-bottom: 22px;
}}
.logo-wrap {{
  display: flex; align-items: center; gap: 14px;
}}
.logo {{ height: 46px; }}
.brand-copy {{ display: flex; flex-direction: column; gap: 5px; }}
.eyebrow {{
  font-size: 10px; font-weight: 700; letter-spacing: .16em;
  text-transform: uppercase; color: var(--muted);
}}
.brand-copy strong {{
  font-size: 22px; color: var(--ink); letter-spacing: -.03em;
}}
.brand-copy span {{
  color: var(--muted); font-size: 12px;
}}
.header-right {{
  min-width: 180px; text-align: right; background: linear-gradient(180deg, var(--soft), #fff);
  border: 1px solid rgba(20,184,166,0.16); border-radius: 14px; padding: 14px 16px;
  font-size: 11px; color: var(--muted); line-height: 1.8;
}}
.header-right strong {{ color: var(--ink); }}
.cert-title {{
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  margin-bottom: 18px;
}}
.cert-title-main strong {{
  display: block; font-size: 24px; color: var(--ink); letter-spacing: -.03em;
}}
.cert-title-main span {{
  display: block; margin-top: 4px; color: var(--teal-dark); font-size: 11px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase;
}}
.cert-pill {{
  background: linear-gradient(135deg, #0ae6c7, var(--teal-dark));
  color: #fff; border-radius: 999px; padding: 8px 14px; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .12em;
}}
.pac-box {{
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px;
  margin-bottom: 20px;
}}
.pac-field {{
  min-width: 0; padding: 12px 14px; border-radius: 12px; background: var(--soft);
  border: 1px solid rgba(20,184,166,0.15);
}}
.pac-field.wide {{ grid-column: 1 / -1; background: linear-gradient(180deg, var(--soft), #fff); }}
.pac-field label {{
  display: block; font-size: 9px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px;
}}
.pac-field strong {{ font-size: 13px; color: var(--ink); }}
.cert-body {{
  border: 1px solid rgba(15,118,110,0.14);
  border-radius: 18px;
  background: linear-gradient(180deg, #ffffff 0%, #fbfffe 100%);
  padding: 24px 24px 20px;
  margin-bottom: 24px;
  flex: 1;
  line-height: 1.8;
}}
.body-grid {{
  display: grid; grid-template-columns: 1.4fr .75fr; gap: 18px;
}}
.body-kicker {{
  font-size: 10px; color: var(--teal-dark); letter-spacing: .16em; text-transform: uppercase; font-weight: 800; margin-bottom: 8px;
}}
.body-copy h2 {{
  font-size: 22px; letter-spacing: -.03em; margin-bottom: 12px;
}}
.body-copy p {{ text-align: justify; margin-bottom: 12px; }}
.body-side {{
  display: flex; flex-direction: column; gap: 12px;
}}
.side-card {{
  border-radius: 14px; padding: 14px 15px; background: var(--soft-2); border: 1px solid #d8e6f8;
}}
.side-card strong {{
  display: block; font-size: 15px; color: var(--ink);
}}
.side-card small {{
  display: block; margin-top: 4px; color: var(--muted);
}}
.side-label {{
  display: block; margin-bottom: 6px; color: var(--muted); font-size: 9px; text-transform: uppercase; letter-spacing: .12em;
}}
.note-box {{
  margin-top: 16px; padding: 14px 16px; border-radius: 12px; background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
}}
.sig-row {{
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-top: 32px;
  padding-top: 20px;
  border-top: 1px dashed #94a3b8;
  gap: 20px;
}}
.sig-legal {{ flex: 1; font-size: 9.5px; color: var(--muted); line-height: 1.6; }}
.sig-legal a {{ color: var(--teal); }}
.sig-block {{ text-align: center; min-width: 160px; }}
.firma-img  {{ max-width: 140px; max-height: 60px; object-fit: contain; display: block; margin: 0 auto 4px; }}
.firma-linea {{ width: 140px; height: 52px; border-bottom: 1.5px solid var(--ink); margin: 0 auto 4px; }}
.firma-name  {{ font-size: 11px; font-weight: 700; }}
.firma-sub   {{ font-size: 10px; color: #555; margin-top: 1px; }}
.firma-stamp {{ font-size: 10px; font-weight: 800; color: var(--teal); margin-top: 3px; letter-spacing: 0.5px; }}
.qr-strip {{
  display: flex; align-items: center; gap: 12px;
  background: #f8fafc; border: 1px solid var(--line);
  border-radius: 14px; padding: 10px 14px; margin-top: 20px;
}}
.qr-img {{ flex-shrink: 0; border: 1px solid var(--line); border-radius: 8px; }}
.qr-info {{ flex: 1; font-size: 9px; line-height: 1.7; color: #374151; }}
.qr-badge {{
  flex-shrink: 0;
  background: linear-gradient(135deg, #0AE6C7, #0d9488);
  color: #fff; font-size: 8px; font-weight: 800;
  text-align: center; padding: 6px 10px; border-radius: 4px;
  text-transform: uppercase; letter-spacing: 0.5px; line-height: 1.4;
}}
.footer {{
  text-align: center; font-size: 9px; color: #9ca3af;
  margin-top: 20px; padding-top: 14px;
  border-top: 1px solid #f3f4f6;
}}
@media (max-width: 600px) {{
  .page {{ padding: 20px 18px; min-height: unset; margin: 8px; }}
  .header {{ grid-template-columns: 1fr; }}
  .logo {{ height: 36px; }}
  .cert-title {{ flex-direction: column; align-items: flex-start; }}
  .pac-box {{ grid-template-columns: 1fr; }}
  .body-grid {{ grid-template-columns: 1fr; }}
  .sig-row {{ flex-direction: column; align-items: center; }}
  .sig-block {{ min-width: unset; }}
}}
</style>
</head>
<body>

<div class="no-print">
  <button onclick="window.print()">Imprimir / PDF</button>
  <span style="color:#94a3b8;font-size:11px;">Certificado #{cert_id_val}</span>
</div>

<div class="page">

  <div class="header">
    <div class="logo-wrap">
      <img src="{logo_src}" class="logo" alt="DocYa">
      <div class="brand-copy">
        <div class="eyebrow">Documentaci&oacute;n m&eacute;dica digital</div>
        <strong>DocYa Certificados</strong>
        <span>Dise&ntilde;o institucional con firma y validaci&oacute;n</span>
      </div>
    </div>
    <div class="header-right">
      <strong>Fecha de emisi&oacute;n:</strong> {fecha_emision_larga}<br>
      <strong>ID:</strong> {cert_id_val:08d}<br>
      <strong>Modelo:</strong> {escape(titulo_cert)}
    </div>
  </div>

  <div class="cert-title">
    <div class="cert-title-main">
      <strong>{escape(titulo_cert)}</strong>
      <span>Documento m&eacute;dico con validez profesional</span>
    </div>
    <div class="cert-pill">DocYa</div>
  </div>

  <div class="pac-box">
    <div class="pac-field wide">
      <label>Paciente</label>
      <strong>{escape(paciente_nombre)}</strong>
    </div>
    <div class="pac-field"><label>{escape(tipo_doc)}</label><strong>{escape(nro_doc)}</strong></div>
    {"<div class='pac-field'><label>CUIL</label><strong>" + escape(cuil) + "</strong></div>" if cuil else ""}
    <div class="pac-field"><label>Sexo</label><strong>{sexo_label}</strong></div>
    <div class="pac-field"><label>F. Nacimiento</label><strong>{fecha_nac_str}</strong></div>
    {"<div class='pac-field'><label>Obra Social</label><strong>" + escape(obra_social) + "</strong></div>" if obra_social else ""}
  </div>

  <div class="cert-body">
    {body_html}
    {obs_html}
  </div>

  <div class="sig-row">
    <div class="sig-legal">
      Este documento ha sido firmado digitalmente por<br>
      <strong>{escape(med_nombre)}</strong> - {escape(esp_label)} - MN {escape(mat_label)}<br>
      conforme a la <a href="#">Ley 25.506</a> de Firma Digital de la Rep&uacute;blica Argentina.<br>
      Verifica su autenticidad en: <a href="{ver_url}">{ver_url}</a>
    </div>
    <div class="sig-block">
      {firma_bloque}
      <div class="firma-name">{escape(med_nombre)}</div>
      <div class="firma-sub">{escape(esp_label)}</div>
      <div class="firma-sub">MN {escape(mat_label)}</div>
      <div class="firma-stamp">FIRMA Y SELLO</div>
    </div>
  </div>

  <div class="qr-strip">
    <img src="{qr_url}" width="90" height="90" alt="QR" class="qr-img">
    <div class="qr-info">
      <strong>DocYa - Documentos M&eacute;dicos Digitales</strong><br>
      {escape(med_nombre)} | {escape(esp_label)} | MN {escape(mat_label)}<br>
      Verificar autenticidad: {ver_url}
    </div>
    <div class="qr-badge">{escape(titulo_cert)}<br>digital</div>
  </div>

  <div class="footer">
    Certificado generado digitalmente mediante DocYa - Plataforma de Documentos M&eacute;dicos Electr&oacute;nicos.<br>
    &copy; {datetime.now().year} DocYa - Todos los derechos reservados.
  </div>

</div>
</body>
</html>"""

    return HTMLResponse(html)


# ====================================================
# ðŸ–¨ï¸ RECETA HTML IMPRIMIBLE
# ====================================================

@router.get("/recetas/{receta_id}/html", response_class=HTMLResponse)
def receta_html(
    receta_id: int,
    medico_id: int = Depends(get_medico_id),
    db=Depends(get_db)
):
    """Devuelve la receta en HTML listo para imprimir / descargar como PDF."""
    _ensure_recetario_recetas_schema(db)
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.uuid, r.cuir, r.estado, r.diagnostico, r.medicamentos,
               r.obra_social, r.plan, r.nro_credencial, r.creado_en,
               p.nombre, p.apellido, p.tipo_documento, p.nro_documento,
               p.sexo, p.fecha_nacimiento, p.cuil,
               m.full_name, m.matricula, m.especialidad, m.tipo, m.firma_url, m.direccion
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        JOIN medicos             m ON m.id = r.medico_id
        WHERE r.id=%s AND r.medico_id=%s
    """, (receta_id, medico_id))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Receta no encontrada")

    (rec_id, uuid_val, cuir, estado, diagnostico, medicamentos,
     obra_social, plan, nro_credencial, creado_en,
     pac_nombre, pac_apellido, tipo_doc, nro_doc,
     sexo, fecha_nac, cuil,
     med_nombre, matricula, especialidad, tipo_med, firma_url, direccion_medico) = row

    fecha_emision = creado_en.strftime("%d/%m/%Y") if creado_en else "&mdash;"
    fecha_nac_str = fecha_nac.strftime("%d/%m/%Y") if fecha_nac else "&mdash;"
    sexo_label = {"M": "Masculino", "F": "Femenino", "X": "No binario"}.get(sexo, sexo)

    meds_rp_html = ""
    meds_com_html = ""
    for i, m in enumerate(medicamentos or [], 1):
        med = _medication_display_fields(m)
        ifa = (med["ifa"] or "").upper()
        nombre_comercial = (med["commercial_name"] or "").upper()
        forma = (med["pharmaceutical_form"] or "").upper()
        presentacion = (med["presentation"] or "").upper()
        cantidad = med["quantity"]
        indicaciones = med["instructions"]
        cantidad_txt = {1: "uno", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco"}.get(int(cantidad), str(cantidad))
        indicaciones_html = indicaciones if indicaciones else '<em style="color:#aaa">Sin indicaciones</em>'
        sugerido_html = (
            f'<span class="med-brand">Marca: {nombre_comercial}</span><br>'
            if nombre_comercial else ""
        )
        detalle_html = (
            f'<span class="med-det">Forma farmac&eacute;utica: {forma or "&mdash;"} &nbsp;&middot;&nbsp; Presentaci&oacute;n: {presentacion or "&mdash;"}</span><br>'
        )
        meds_rp_html += (
            f'<div class="med-rp"><span class="med-num">{i})</span> '
            f'<strong>{ifa or "NO INFORMADO"}</strong><br>'
            f'{sugerido_html}'
            f'{detalle_html}'
            f'<span class="med-cant">Cantidad: {cantidad} ({cantidad_txt})</span></div>'
        )
        meds_com_html += f'<div class="med-com"><span class="med-num">{i})</span> {indicaciones_html}</div>'

    diag_html = (
        f'<div class="diag-row"><strong>Diagn&oacute;stico:</strong> {diagnostico}</div>'
        if diagnostico else ""
    )

    base = os.getenv("API_BASE_URL", "https://docya-railway-production.up.railway.app")
    ver_url = f"{base}/recetario/verificar/{uuid_val}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=96x96&data={ver_url}"
    bc_cuir = _barcode_data_uri(cuir or "")
    logo_src = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    esp_label = escape(especialidad or tipo_med or "Médico").upper()
    mat_label = matricula or "&mdash;"
    direccion_label = direccion_medico or "&mdash;"
    anulada_pill = "<span class='anulada-pill'>ANULADA</span>" if estado == "anulada" else ""
    firma_html = (
        f'<img src="{firma_url}" alt="Firma" class="firma-img">'
        if firma_url else '<div class="firma-linea"></div>'
    )

    def _top(badge):
        return f"""
      <div class="top-strip">
        <div class="top-barcodes">
          <img class="barcode barcode-cuir" src="{bc_cuir}" alt="CUIR">
          <div class="cuir-code">{escape(cuir or "&mdash;")}</div>
        </div>
        <div class="top-center">
          <img src="{logo_src}" class="logo" alt="DocYa">
          <span class="copy-badge">{badge}</span>
        </div>
        <div class="top-info">
          <span class="fecha-teal">{fecha_emision}</span>
        </div>
      </div>"""

    med_grid = f"""
      <div class="med-grid">
        <div class="mf mf-name"><label>Profesional</label><strong>{med_nombre}</strong></div>
        <div class="mf"><label>Profesi&oacute;n / Especialidad</label><strong>{esp_label}</strong></div>
        <div class="mf"><label>Matr&iacute;cula N&deg;</label><strong>{mat_label}</strong></div>
        <div class="mf mf-address"><label>Domicilio profesional</label><strong>{direccion_label}</strong></div>
      </div>"""

    pac_grid = f"""
      <div class="pac-grid">
        <div class="pf pf-name"><label>Paciente</label><strong>{pac_apellido.upper()}, {pac_nombre}</strong></div>
        <div class="pf"><label>Sexo</label><strong>{sexo_label}</strong></div>
        <div class="pf"><label>{tipo_doc}</label><strong>{nro_doc}</strong></div>
        <div class="pf"><label>F. Nacimiento</label><strong>{fecha_nac_str}</strong></div>
        <div class="pf"><label>Obra Social</label><strong>{obra_social or "&mdash;"}</strong></div>
        <div class="pf"><label>Plan</label><strong>{plan or "&mdash;"}</strong></div>
        <div class="pf"><label>N&deg; Credencial</label><strong>{nro_credencial or "&mdash;"}</strong></div>
      </div>"""

    sig_footer = f"""
      <div class="sig-footer">
        <div>
          <p class="sig-legal"><strong>Diagn&oacute;stico:</strong> {diagnostico or "&mdash;"}</p>
          <p class="sig-legal"><strong>Fecha:</strong> {fecha_emision}</p>
          <p class="sig-legal"><strong>Firma profesional:</strong> {med_nombre}</p>
          <p class="sig-legal">Este documento ha sido firmado electr&oacute;nica o digitalmente por Dr. {med_nombre}</p>
          <p class="sig-legal">Esta receta fue creada por un emisor inscripto y validado en el Registro de Recetarios Electr&oacute;nicos del Ministerio de Salud de la Naci&oacute;n - RL-2026-37903200-APN-SSVEIYES#MS</p>
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
          <span class="strip-note">CUIR: {escape(cuir or "&mdash;")}</span>
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
      {med_grid}
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
.top-strip {{ display: grid; grid-template-columns: 118px 1fr 92px; gap: 8px; align-items: start; padding-bottom: 5px; margin-bottom: 5px; border-bottom: 1px solid var(--line); }}
.top-barcodes {{ display: flex; flex-direction: column; gap: 3px; }}
.barcode {{ display: block; width: 100%; max-width: 112px; height: 24px; object-fit: contain; }}
.barcode-cuir {{ height: 28px; }}
.cuir-code {{ font-size: 7px; color: var(--muted); word-break: break-all; line-height: 1.25; }}
.top-center {{ text-align: center; }}
.logo {{ display: block; height: 21px; margin: 0 auto 2px; }}
.copy-badge {{ display: inline-block; padding: 2px 7px; border-radius: 999px; background: linear-gradient(135deg, #0ae6c7, var(--teal-dark)); color: #fff; font-size: 6.5px; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; }}
.top-info {{ text-align: right; font-size: 8px; line-height: 1.35; color: #374151; }}
.top-address {{ display: inline-block; max-width: 88px; color: var(--muted); line-height: 1.25; }}
.fecha-teal {{ color: var(--teal-dark); font-weight: 700; }}
.med-grid {{ display: flex; flex-wrap: wrap; margin-bottom: 5px; overflow: hidden; border: 1.5px solid var(--teal-dark); border-radius: 3px; }}
.mf {{ flex: 1 1 33%; min-width: 0; padding: 2px 5px; border-right: 1px solid #ccfbf1; border-bottom: 1px solid #ccfbf1; }}
.mf-name {{ flex: 1 1 100%; background: #f0fdfa; }}
.mf-address {{ flex: 1 1 100%; }}
.mf label {{ display: block; margin-bottom: 1px; color: var(--muted); font-size: 6.5px; letter-spacing: 0.3px; text-transform: uppercase; }}
.mf strong {{ font-size: 8.5px; }}
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
.med-cant, .med-det, .med-brand {{ color: var(--muted); font-size: 7.5px; }}
.diag-row {{ margin-top: 6px; padding: 2px 6px; border-left: 2px solid var(--teal-dark); background: #f0fdfa; color: #374151; font-size: 7.5px; }}
.sig-footer {{ display: grid; grid-template-columns: 1fr 92px; gap: 6px; align-items: end; margin-bottom: 4px; padding-top: 4px; border-top: 1px dashed #9ca3af; }}
.sig-legal, .firma-sub, .strip-note {{ font-size: 6.2px; }}
.sig-legal {{ color: var(--muted); line-height: 1.35; }}
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
  .top-center, .top-info {{ text-align: left; }}
  .mf {{ flex: 1 1 100%; }}
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
# ðŸ”§ Helpers HTML
# ====================================================
def _html_verificacion(uuid, cuir, estado, es_valida, fecha, paciente,
                        medico, matricula, especialidad, diagnostico):
    color  = "#14B8A6" if es_valida else "#dc2626"
    icono  = "âœ…" if es_valida else "âŒ"
    titulo = "Documento VÃ¡lido" if es_valida else "Documento Anulado"
    subtxt = ("La firma digital es autÃ©ntica y el documento se encuentra vigente."
              if es_valida else
              "Este documento fue revocado por el profesional y no tiene validez legal.")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VerificaciÃ³n â€” DocYa</title>
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
    <div class="row"><span class="label">Tipo</span><span class="value">Receta MÃ©dica ElectrÃ³nica</span></div>
    <div class="row"><span class="label">Fecha emisiÃ³n</span><span class="value">{fecha}</span></div>
    <div class="row"><span class="label">MÃ©dico emisor</span><span class="value">{medico}</span></div>
    <div class="row"><span class="label">MatrÃ­cula Nac.</span><span class="value">MN {matricula}</span></div>
    <div class="row"><span class="label">Especialidad</span><span class="value">{especialidad}</span></div>
    <div class="row"><span class="label">Paciente</span><span class="value">{paciente}</span></div>
    <div class="row"><span class="label">Estado</span>
      <span class="value" style="color:{'#4ade80' if es_valida else '#f87171'}">
        {'VÃLIDA' if es_valida else 'ANULADA'}
      </span>
    </div>
    <div class="row"><span class="label">UUID</span>
      <span class="value" style="font-size:0.75rem;color:#94a3b8">{uuid}</span>
    </div>
    <div class="row"><span class="label">CUIR</span>
      <span class="value" style="font-size:0.75rem;color:#94a3b8">{cuir}</span>
    </div>
  </div>
</div>
</body>
</html>"""


def _html_no_encontrada(uuid_receta: str):
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><title>No encontrado â€” DocYa</title>
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
  <div style="font-size:3rem">ðŸ”</div>
  <h2>Documento no encontrado</h2>
  <p>No existe ningÃºn documento con el identificador:</p>
  <code>{uuid_receta}</code>
</div>
</body>
</html>"""

