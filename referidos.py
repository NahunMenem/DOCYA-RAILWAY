# ====================================================
# 📌 referidos.py — Programa de Referidos DocYa
# ====================================================

import os
import uuid
import string
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from sib_api_v3_sdk import SendSmtpEmail
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor

from database import get_db
from settings import JWT_SECRET, create_access_token, now_argentina, pwd_context

router = APIRouter(prefix="/referidos", tags=["referidos"])

ESTADOS_VALIDOS = {"pendiente", "pagado", "anulado"}
DEFAULT_GOOGLE_CLIENT_IDS = [
    "117956759164-9q555tbkl8ulrmcapgj4emoqn827ltti.apps.googleusercontent.com",
    "327572770521-tom99oocat1tcp9pahlejsar4iu62lhg.apps.googleusercontent.com",
]
GOOGLE_CLIENT_IDS = list(
    dict.fromkeys(
        DEFAULT_GOOGLE_CLIENT_IDS
        + [
            item.strip()
            for item in os.getenv(
                "GOOGLE_CLIENT_IDS", os.getenv("GOOGLE_CLIENT_ID", "")
            ).split(",")
            if item.strip()
        ]
    )
)


# ====================================================
# 📐 MODELOS PYDANTIC
# ====================================================

class ReferenteRegisterIn(BaseModel):
    full_name: str
    dni: str
    telefono: str
    email: EmailStr
    password: str
    cbu_alias: str
    tipo: str          # influencer | embajador | paciente | partner
    acepto_condiciones: bool = False


class ReferenteLoginIn(BaseModel):
    email: str
    password: str


class GoogleReferenteIn(BaseModel):
    id_token: str


class GoogleReferenteRegisterIn(BaseModel):
    id_token: str
    dni: str
    telefono: str
    cbu_alias: str
    tipo: str
    acepto_condiciones: bool = False


class ReferenteOut(BaseModel):
    id: str
    full_name: str
    email: str
    tipo: str
    link_referido: str
    codigo_referido: str


# ====================================================
# 🔧 HELPERS
# ====================================================

def _generar_codigo(full_name: str) -> str:
    """Genera un código único: primeras letras del nombre + random alfanumérico."""
    base = full_name.strip().split()[0].upper()[:4]
    sufijo = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{base}-{sufijo}"


def _ensure_referente_google_columns(db):
    cur = db.cursor()
    cur.execute("ALTER TABLE referentes ADD COLUMN IF NOT EXISTS google_id TEXT")
    cur.execute("ALTER TABLE referentes ADD COLUMN IF NOT EXISTS foto_url TEXT")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_referentes_google_id_unique ON referentes (google_id) WHERE google_id IS NOT NULL"
    )
    db.commit()


def _verify_google_identity(id_token: str) -> tuple[str, str, str, str | None]:
    request_adapter = google_requests.Request()
    payload = None
    last_error = None
    audiences = GOOGLE_CLIENT_IDS or [None]

    for audience in audiences:
        try:
            payload = google_id_token.verify_oauth2_token(id_token, request_adapter, audience)
            break
        except Exception as exc:
            last_error = exc

    if payload is None:
        raise HTTPException(status_code=401, detail=f"Token Google inválido: {last_error}")

    google_sub = (payload.get("sub") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    full_name = (payload.get("name") or "Embajador DocYa").strip()
    picture = (payload.get("picture") or "").strip() or None

    if not google_sub or not email:
        raise HTTPException(status_code=400, detail="Google no devolvió identidad suficiente.")

    return google_sub, email, full_name, picture


def _build_referente_auth_response(row):
    token = create_access_token({
        "sub": str(row["id"]),
        "email": row["email"],
        "role": "referente",
        "tipo": row["tipo"],
    })
    return {
        "access_token": token,
        "token_type": "bearer",
        "referente": {
            "id": str(row["id"]),
            "full_name": row["full_name"],
            "email": row["email"],
            "tipo": row["tipo"],
            "codigo_referido": row["codigo_referido"],
            "link_referido": row["link_referido"],
            "foto_url": row.get("foto_url"),
        },
    }


def _link_referido(codigo: str) -> str:
    base_url = os.getenv("FRONTEND_URL", "https://referidos.docya.online")
    return f"{base_url}/?ref={codigo}"


def _get_referente_id_from_token(authorization: str | None) -> str:
    """
    Extrae y valida el JWT del header Authorization.
    Devuelve el referente_id (sub) o lanza 401.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de autenticación requerido.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="El token expiró. Iniciá sesión nuevamente.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido.")
    if payload.get("role") != "referente":
        raise HTTPException(status_code=403, detail="Acceso denegado.")
    return payload["sub"]


def _require_admin(authorization: str | None):
    """Verifica la API key de admin desde el header Authorization."""
    admin_key = os.getenv("ADMIN_API_KEY")
    if not admin_key:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY no configurada.")
    if not authorization or authorization != f"Bearer {admin_key}":
        raise HTTPException(status_code=403, detail="Acceso de administrador requerido.")


def _enviar_email_bienvenida_referente(email: str, full_name: str, codigo: str, link: str):
    """Email de bienvenida al referente con su link y código personales."""
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key["api-key"] = os.getenv("BREVO_API_KEY")
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"><title>Bienvenido a DocYa Referidos</title></head>
    <body style="margin:0;padding:0;background:#F4F6F8;font-family:Arial,sans-serif;">
      <table align="center" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;" cellpadding="0" cellspacing="0">
        <tr><td align="center">
          <table width="600" style="background:#ffffff;border-radius:8px;box-shadow:0 2px 6px rgba(0,0,0,0.1);" cellpadding="0" cellspacing="0">
            <tr>
              <td style="background:#0F2027;border-radius:8px 8px 0 0;padding:30px;text-align:center;">
                <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png"
                     alt="DocYa" style="max-width:160px;">
              </td>
            </tr>
            <tr>
              <td style="padding:40px 36px;">
                <h2 style="color:#14B8A6;font-size:22px;margin:0 0 12px;">
                  ¡Hola {full_name}, ya sos parte del programa! 🎉
                </h2>
                <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 24px;">
                  Tu cuenta de embajador DocYa fue creada exitosamente.<br>
                  Compartí tu link personal y <strong>ganás $1.000 por cada consulta</strong>
                  que realicen tus referidos.
                </p>

                <!-- Código -->
                <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
                  <tr>
                    <td style="background:#F0FDFA;border:1px solid #99F6E4;border-radius:8px;padding:16px;text-align:center;">
                      <p style="margin:0 0 4px;color:#0F766E;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;">
                        Tu código de referido
                      </p>
                      <p style="margin:0;color:#0D9488;font-size:28px;font-weight:900;letter-spacing:4px;">
                        {codigo}
                      </p>
                    </td>
                  </tr>
                </table>

                <!-- Link -->
                <p style="color:#555;font-size:14px;margin:0 0 8px;">Tu link personalizado:</p>
                <a href="{link}"
                   style="display:block;background:#14B8A6;color:#fff;text-decoration:none;
                          padding:14px 24px;border-radius:6px;font-size:14px;font-weight:bold;
                          text-align:center;margin-bottom:24px;">
                  🔗 {link}
                </a>

                <p style="color:#777;font-size:13px;line-height:1.6;margin:0;">
                  Los pagos se acreditan <strong>semanalmente</strong> en tu CBU/Alias registrado.<br>
                  Podés ver tus métricas en tiempo real desde tu panel de embajador.
                </p>
              </td>
            </tr>
            <tr>
              <td style="background:#F9FAFB;border-radius:0 0 8px 8px;padding:20px;text-align:center;">
                <p style="color:#999;font-size:11px;margin:0;">
                  © {datetime.now().year} DocYa · Atención médica a domicilio con confianza.
                </p>
              </td>
            </tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa"},
        subject="¡Ya sos embajador DocYa! Tu link personal está listo 🚀",
        html_content=html,
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Email bienvenida referente enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email referente con Brevo: {e}")


# ====================================================
# 🚀 ENDPOINTS
# ====================================================

@router.post("/register", response_model=ReferenteOut, status_code=201)
def register_referente(data: ReferenteRegisterIn, db=Depends(get_db)):
    """Registra un nuevo referente. Genera automáticamente su código y link único."""
    _ensure_referente_google_columns(db)
    if not data.acepto_condiciones:
        raise HTTPException(
            status_code=422,
            detail="Debés aceptar los términos y condiciones para continuar."
        )

    cur = db.cursor()
    try:
        # ── Verificar duplicado por email ──────────────────────────────
        cur.execute("SELECT id FROM referentes WHERE email = %s", (data.email.lower(),))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="El email ya está registrado en el programa de referidos.")

        # ── Verificar duplicado por DNI ────────────────────────────────
        cur.execute("SELECT id FROM referentes WHERE dni = %s", (data.dni.strip(),))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="El DNI ya está registrado en el programa de referidos.")

        # ── Validar tipo ───────────────────────────────────────────────
        tipos_validos = {"influencer", "embajador", "paciente", "partner"}
        if data.tipo not in tipos_validos:
            raise HTTPException(
                status_code=422,
                detail=f"Tipo inválido. Valores permitidos: {', '.join(sorted(tipos_validos))}"
            )

        # ── Generar código único (retry en colisión) ───────────────────
        codigo = None
        for _ in range(10):
            candidato = _generar_codigo(data.full_name)
            cur.execute("SELECT id FROM referentes WHERE codigo_referido = %s", (candidato,))
            if not cur.fetchone():
                codigo = candidato
                break

        if not codigo:
            raise HTTPException(status_code=500, detail="No se pudo generar un código único. Intentá de nuevo.")

        link = _link_referido(codigo)
        password_hash = pwd_context.hash(data.password)
        full_name = data.full_name.strip().title()

        cur.execute(
            """
            INSERT INTO referentes (
                full_name, dni, telefono, email, password_hash,
                cbu_alias, tipo, codigo_referido, link_referido,
                acepto_condiciones, fecha_aceptacion,
                activo, creado_en
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                TRUE, %s,
                TRUE, %s
            )
            RETURNING id, full_name, email, tipo, link_referido, codigo_referido
            """,
            (
                full_name, data.dni.strip(), data.telefono.strip(),
                data.email.lower(), password_hash, data.cbu_alias.strip(),
                data.tipo, codigo, link,
                now_argentina(), now_argentina(),
            )
        )
        row = cur.fetchone()
        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno al registrar referente: {e}")
    finally:
        cur.close()

    referente_id, full_name_db, email_db, tipo_db, link_db, codigo_db = row

    try:
        _enviar_email_bienvenida_referente(email_db, full_name_db, codigo_db, link_db)
    except Exception as e:
        print(f"⚠️ Email bienvenida falló (no crítico): {e}")

    return ReferenteOut(
        id=str(referente_id),
        full_name=full_name_db,
        email=email_db,
        tipo=tipo_db,
        link_referido=link_db,
        codigo_referido=codigo_db,
    )


# ──────────────────────────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────────────────────────

@router.post("/login")
def login_referente(data: ReferenteLoginIn, db=Depends(get_db)):
    """Login del referente. Devuelve JWT + datos básicos del perfil."""
    _ensure_referente_google_columns(db)
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT id, full_name, email, tipo, password_hash,
                   codigo_referido, link_referido, activo, foto_url
            FROM referentes
            WHERE lower(email) = %s
            LIMIT 1
            """,
            (data.email.strip().lower(),)
        )
        row = cur.fetchone()
    finally:
        cur.close()

    if not row:
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

    ref_id, full_name, email, tipo, password_hash, codigo, link, activo, foto_url = row

    if not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

    if not activo:
        raise HTTPException(status_code=403, detail="Tu cuenta está desactivada. Contactá a soporte.")

    return _build_referente_auth_response(
        {
            "id": ref_id,
            "full_name": full_name,
            "email": email,
            "tipo": tipo,
            "codigo_referido": codigo,
            "link_referido": link,
            "foto_url": foto_url,
        }
    )


@router.post("/google")
def login_referente_google(data: GoogleReferenteIn, db=Depends(get_db)):
    """Login del referente con Google o aviso de registro incompleto."""
    _ensure_referente_google_columns(db)
    google_sub, email, full_name, picture = _verify_google_identity(data.id_token)

    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT id, full_name, email, tipo, codigo_referido, link_referido, activo, foto_url
            FROM referentes
            WHERE google_id = %s OR lower(email) = %s
            LIMIT 1
            """,
            (google_sub, email),
        )
        referente = cur.fetchone()

        if not referente:
            raise HTTPException(
                status_code=404,
                detail="Esta cuenta Google todavía no está registrada en el programa de referidos.",
            )

        if not referente["activo"]:
            raise HTTPException(status_code=403, detail="Tu cuenta está desactivada. Contactá a soporte.")

        cur.execute(
            """
            UPDATE referentes
            SET google_id = %s,
                email = %s,
                full_name = COALESCE(NULLIF(full_name, ''), %s),
                foto_url = COALESCE(NULLIF(foto_url, ''), %s)
            WHERE id = %s
            RETURNING id, full_name, email, tipo, codigo_referido, link_referido, activo, foto_url
            """,
            (google_sub, email, full_name, picture, referente["id"]),
        )
        referente = cur.fetchone()
        db.commit()
    finally:
        cur.close()

    return _build_referente_auth_response(referente)


@router.post("/google/register", status_code=201)
def register_referente_google(data: GoogleReferenteRegisterIn, db=Depends(get_db)):
    """Registro de referente con Google + datos adicionales obligatorios."""
    _ensure_referente_google_columns(db)
    if not data.acepto_condiciones:
        raise HTTPException(
            status_code=422,
            detail="Debés aceptar los términos y condiciones para continuar."
        )

    tipos_validos = {"influencer", "embajador", "paciente", "partner"}
    if data.tipo not in tipos_validos:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo inválido. Valores permitidos: {', '.join(sorted(tipos_validos))}"
        )

    google_sub, email, full_name, picture = _verify_google_identity(data.id_token)

    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id FROM referentes WHERE google_id = %s OR lower(email) = %s LIMIT 1", (google_sub, email))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Esta cuenta Google ya está registrada.")

        cur.execute("SELECT id FROM referentes WHERE dni = %s", (data.dni.strip(),))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="El DNI ya está registrado en el programa de referidos.")

        codigo = None
        for _ in range(10):
            candidato = _generar_codigo(full_name)
            cur.execute("SELECT id FROM referentes WHERE codigo_referido = %s", (candidato,))
            if not cur.fetchone():
                codigo = candidato
                break

        if not codigo:
            raise HTTPException(status_code=500, detail="No se pudo generar un código único. Intentá de nuevo.")

        link = _link_referido(codigo)
        google_password_hash = pwd_context.hash(f"google-referente::{google_sub}::{email}")

        cur.execute(
            """
            INSERT INTO referentes (
                full_name, dni, telefono, email, password_hash, cbu_alias, tipo,
                codigo_referido, link_referido, acepto_condiciones, fecha_aceptacion,
                activo, creado_en, google_id, foto_url
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, TRUE, %s,
                TRUE, %s, %s, %s
            )
            RETURNING id, full_name, email, tipo, link_referido, codigo_referido, foto_url
            """,
            (
                full_name,
                data.dni.strip(),
                data.telefono.strip(),
                email,
                google_password_hash,
                data.cbu_alias.strip(),
                data.tipo,
                codigo,
                link,
                now_argentina(),
                now_argentina(),
                google_sub,
                picture,
            )
        )
        row = cur.fetchone()
        db.commit()
    finally:
        cur.close()

    try:
        _enviar_email_bienvenida_referente(row["email"], row["full_name"], row["codigo_referido"], row["link_referido"])
    except Exception as e:
        print(f"⚠️ Email bienvenida Google referente falló (no crítico): {e}")

    return _build_referente_auth_response(
        {
            "id": row["id"],
            "full_name": row["full_name"],
            "email": row["email"],
            "tipo": row["tipo"],
            "codigo_referido": row["codigo_referido"],
            "link_referido": row["link_referido"],
            "foto_url": row.get("foto_url"),
        }
    )


# ──────────────────────────────────────────────────────────────────
# STATS
# ──────────────────────────────────────────────────────────────────

@router.get("/{referente_id}/stats")
def stats_referente(
    referente_id: str,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    """Devuelve las métricas del referente. Requiere JWT propio."""
    token_sub = _get_referente_id_from_token(authorization)
    if token_sub != referente_id:
        raise HTTPException(status_code=403, detail="No podés ver las stats de otro referente.")

    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id, full_name, codigo_referido FROM referentes WHERE id = %s",
            (referente_id,)
        )
        ref = cur.fetchone()
        if not ref:
            raise HTTPException(status_code=404, detail="Referente no encontrado.")

        _, full_name, codigo = ref

        # Usar TRIM/LOWER para consistencia con mis-referidos
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE TRIM(LOWER(codigo_referido)) = TRIM(LOWER(%s))",
            (codigo,)
        )
        total_referidos = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(monto_referente), 0)
            FROM recompensas_referentes
            WHERE referente_id = %s AND estado IN ('pendiente', 'pagado')
            """,
            (referente_id,)
        )
        row = cur.fetchone()
        total_consultas_validas = row[0]
        monto_total_acumulado = float(row[1])

        cur.execute(
            """
            SELECT COALESCE(SUM(monto_referente), 0)
            FROM recompensas_referentes
            WHERE referente_id = %s AND estado = 'pendiente'
            """,
            (referente_id,)
        )
        monto_pendiente = float(cur.fetchone()[0])
    finally:
        cur.close()

    return {
        "referente_id": referente_id,
        "full_name": full_name,
        "codigo_referido": codigo,
        "total_referidos": total_referidos,
        "total_consultas_validas": total_consultas_validas,
        "monto_total_acumulado": monto_total_acumulado,
        "monto_pendiente": monto_pendiente,
        "precio_por_consulta": 1000,
    }


# ──────────────────────────────────────────────────────────────────
# MIS REFERIDOS
# ──────────────────────────────────────────────────────────────────

@router.get("/{referente_id}/mis-referidos")
def mis_referidos(
    referente_id: str,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    """
    Devuelve la lista de pacientes referidos con su última consulta,
    monto generado y estado de pago. Requiere JWT propio.
    """
    token_sub = _get_referente_id_from_token(authorization)
    if token_sub != referente_id:
        raise HTTPException(status_code=403, detail="No podés ver los referidos de otro referente.")

    cur = db.cursor()
    try:
        cur.execute(
            "SELECT id, codigo_referido FROM referentes WHERE id = %s",
            (referente_id,)
        )
        ref = cur.fetchone()
        if not ref:
            raise HTTPException(status_code=404, detail="Referente no encontrado.")

        _, codigo = ref

        cur.execute(
            """
            SELECT
                u.id                                        AS paciente_uuid,
                u.full_name,
                u.localidad,
                u.created_at                                AS fecha_registro,
                MAX(c.creado_en)                            AS ultima_consulta,
                COALESCE(SUM(rr.monto_referente), 0)        AS monto_total,
                (
                    SELECT rr2.estado
                    FROM recompensas_referentes rr2
                    WHERE rr2.paciente_uuid = u.id
                      AND rr2.referente_id  = %s
                    ORDER BY rr2.creado_en DESC
                    LIMIT 1
                )                                           AS ultimo_estado,
                u.created_at + INTERVAL '12 months'         AS vence_en

            FROM users u

            LEFT JOIN consultas c
                   ON c.paciente_uuid = u.id

            LEFT JOIN recompensas_referentes rr
                   ON rr.paciente_uuid = u.id
                  AND rr.referente_id  = %s

            WHERE TRIM(LOWER(u.codigo_referido)) = TRIM(LOWER(%s))

            GROUP BY u.id, u.full_name, u.localidad, u.created_at

            ORDER BY MAX(c.creado_en) DESC NULLS LAST, u.created_at DESC
            """,
            (referente_id, referente_id, codigo)
        )

        rows = cur.fetchall()
    finally:
        cur.close()

    referidos = []
    for row in rows:
        (
            paciente_uuid, full_name, localidad, fecha_registro,
            ultima_consulta, monto_total, ultimo_estado, vence_en
        ) = row

        referidos.append({                          # ← corregido (era routerend)
            "paciente_uuid":   str(paciente_uuid),
            "full_name":       full_name,
            "localidad":       localidad or "—",
            "fecha_registro":  fecha_registro.isoformat() if fecha_registro else None,
            "ultima_consulta": ultima_consulta.isoformat() if ultima_consulta else None,
            "monto_total":     float(monto_total or 0),
            "estado_pago":     ultimo_estado or "sin_consulta",
            "vence_en":        vence_en.isoformat() if vence_en else None,
        })

    return {
        "referente_id": referente_id,
        "total": len(referidos),
        "referidos": referidos,
    }


# ====================================================
# 🔐 ADMIN ENDPOINTS
# ====================================================

@router.get("/admin/referentes")
def get_all_referentes(
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, full_name, email, telefono, dni, tipo,
                   codigo_referido, link_referido, cbu_alias, activo, creado_en
            FROM referentes
            ORDER BY creado_en DESC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
    return [dict(r) for r in rows]


@router.patch("/admin/referentes/{referente_id}/toggle")
def toggle_referente(
    referente_id: str,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT activo FROM referentes WHERE id = %s", (referente_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Referente no encontrado.")
        cur.execute(
            "UPDATE referentes SET activo = %s WHERE id = %s RETURNING activo",
            (not row["activo"], referente_id)
        )
        updated = cur.fetchone()
        db.commit()
    finally:
        cur.close()
    return {"ok": True, "activo": updated["activo"]}


@router.get("/admin/recompensas")
def get_recompensas(
    estado: str | None = None,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)

    if estado is not None and estado not in ESTADOS_VALIDOS:
        raise HTTPException(
            status_code=422,
            detail=f"Estado inválido. Valores permitidos: {', '.join(sorted(ESTADOS_VALIDOS))}"
        )

    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        if estado:
            cur.execute(
                """
                SELECT rr.id, rr.referente_id, rr.monto_referente, rr.estado, rr.creado_en,
                       r.full_name AS referente_nombre, r.cbu_alias AS referente_cbu,
                       u.full_name AS paciente_nombre
                FROM recompensas_referentes rr
                JOIN referentes r ON r.id::text = rr.referente_id::text
                JOIN users u ON u.id = rr.paciente_uuid
                WHERE rr.estado = %s
                ORDER BY rr.creado_en DESC
                """,
                (estado,)
            )
        else:
            cur.execute(
                """
                SELECT rr.id, rr.referente_id, rr.monto_referente, rr.estado, rr.creado_en,
                       r.full_name AS referente_nombre, r.cbu_alias AS referente_cbu,
                       u.full_name AS paciente_nombre
                FROM recompensas_referentes rr
                JOIN referentes r ON r.id::text = rr.referente_id::text
                JOIN users u ON u.id = rr.paciente_uuid
                ORDER BY rr.creado_en DESC
                """
            )
        rows = cur.fetchall()
    finally:
        cur.close()
    return [dict(r) for r in rows]


@router.patch("/admin/recompensas/{recompensa_id}/pagar")
def pagar_recompensa(
    recompensa_id: int,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            "UPDATE recompensas_referentes SET estado='pagado' WHERE id=%s RETURNING id",
            (recompensa_id,)
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Recompensa no encontrada.")
        db.commit()
    finally:
        cur.close()
    return {"ok": True}


@router.patch("/admin/referentes/{referente_id}/pagar-pendientes")
def pagar_pendientes(
    referente_id: str,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)
    cur = db.cursor()
    try:
        cur.execute(
            """
            UPDATE recompensas_referentes SET estado='pagado'
            WHERE referente_id::text = %s AND estado = 'pendiente'
            """,
            (referente_id,)
        )
        pagados = cur.rowcount
        db.commit()
    finally:
        cur.close()
    return {"ok": True, "pagados": pagados}


@router.get("/admin/referentes/{referente_id}/referidos")
def get_referidos_admin(
    referente_id: str,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    _require_admin(authorization)
    cur = db.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT codigo_referido FROM referentes WHERE id = %s", (referente_id,))
        ref = cur.fetchone()
        if not ref:
            raise HTTPException(status_code=404, detail="Referente no encontrado.")
        codigo = ref["codigo_referido"]
        cur.execute("""
            SELECT u.id, u.full_name, u.email, u.localidad, u.created_at AS fecha_registro,
                   COALESCE(SUM(rr.monto_referente), 0) AS monto_total,
                   COUNT(rr.id) AS total_consultas,
                   MAX(rr.creado_en) AS ultima_consulta,
                   (SELECT rr2.estado FROM recompensas_referentes rr2
                    WHERE rr2.paciente_uuid = u.id ORDER BY rr2.creado_en DESC LIMIT 1) AS estado
            FROM users u
            LEFT JOIN recompensas_referentes rr ON rr.paciente_uuid = u.id AND rr.referente_id::text = %s
            WHERE TRIM(LOWER(u.codigo_referido)) = TRIM(LOWER(%s))
            GROUP BY u.id, u.full_name, u.email, u.localidad, u.created_at
            ORDER BY u.created_at DESC
        """, (referente_id, codigo))
        rows = cur.fetchall()
    finally:
        cur.close()
    return [dict(r) for r in rows]    
