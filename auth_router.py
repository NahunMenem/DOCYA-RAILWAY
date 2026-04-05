"""
Router de autenticación y cuentas.

Agrupa:
- registro y login de pacientes
- registro y login de profesionales
- activación por email
- recuperación y reseteo de contraseña
- endpoints básicos de perfil/cuenta
"""

import os
import re
from datetime import date, datetime, time, timedelta, timezone

import cloudinary
import cloudinary.uploader
import jwt
import sib_api_v3_sdk
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt as jose_jwt
from pydantic import BaseModel, EmailStr
from psycopg2.extras import RealDictCursor
from sib_api_v3_sdk import SendSmtpEmail
from sib_api_v3_sdk.rest import ApiException
from unidecode import unidecode

from database import get_db
from settings import (
    ARG_TZ,
    CURRENT_ARGENTINA_WEEK_SQL,
    JWT_SECRET,
    create_access_token,
    now_argentina,
    pwd_context,
    start_of_week_argentina,
)

templates = Jinja2Templates(directory="templates")
router = APIRouter()

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))
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
E164_REGEX = r"^\+[1-9]\d{7,14}$"
TIPOS_DOCUMENTO = {"dni", "pasaporte", "otro"}
SEXOS_VALIDOS = {"masculino", "femenino", "otro"}


def _ensure_user_profile_columns(db):
    """Agrega columnas nuevas al esquema `users` si todavía no existen."""
    cur = db.cursor()
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tipo_documento TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS numero_documento TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS direccion TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS perfil_completo BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acepta_terminos BOOLEAN DEFAULT FALSE")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id_unique ON users (google_id) WHERE google_id IS NOT NULL")
    db.commit()


def _ensure_medico_profile_columns(db):
    """Asegura columnas modernas de perfil para profesionales."""
    cur = db.cursor()
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS google_id TEXT")
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS tipo_documento TEXT")
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS numero_documento TEXT")
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS direccion TEXT")
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS acepta_terminos BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE medicos ADD COLUMN IF NOT EXISTS perfil_completo BOOLEAN DEFAULT FALSE")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_medicos_google_id_unique ON medicos (google_id) WHERE google_id IS NOT NULL"
    )
    db.commit()


def _upload_base64_image(imagen_base64, folder, public_id):
    if not imagen_base64:
        return None
    try:
        if imagen_base64.startswith("data:image"):
            res = cloudinary.uploader.upload(
                imagen_base64,
                folder=folder,
                public_id=public_id,
                overwrite=True,
                resource_type="image",
            )
            return res["secure_url"]
        if imagen_base64.startswith("http"):
            return imagen_base64
    except Exception as exc:
        print(f"Error subiendo imagen {public_id}: {exc}")
    return None


def _normalize_bool(value) -> bool:
    return bool(value) is True


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    dni: str | None = None
    telefono: str | None = None
    pais: str | None = None
    provincia: str | None = None
    localidad: str | None = None
    fecha_nacimiento: date | None = None
    sexo: str | None = None
    acepto_condiciones: bool = False


class LoginIn(BaseModel):
    email: str
    password: str


class GoogleAuthIn(BaseModel):
    id_token: str


class UserOut(BaseModel):
    id: str
    full_name: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class FcmTokenIn(BaseModel):
    fcm_token: str


class RegisterMedicoIn(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    matricula: str
    especialidad: str | None = None
    tipo: str = "medico"
    telefono: str | None = None
    provincia: str | None = None
    localidad: str | None = None
    dni: str | None = None
    tipo_documento: str | None = None
    numero_documento: str | None = None
    direccion: str | None = None
    acepta_terminos: bool = False
    foto_perfil: str | None = None
    foto_dni_frente: str | None = None
    foto_dni_dorso: str | None = None
    selfie_dni: str | None = None


class LoginMedicoIn(BaseModel):
    email: str
    password: str


class AliasIn(BaseModel):
    alias: str | None = None
    alias_cbu: str | None = None


class ForgotPasswordIn(BaseModel):
    identificador: str


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


class CompletarPerfilIn(BaseModel):
    user_id: str
    telefono: str
    tipo_documento: str
    numero_documento: str
    direccion: str
    fecha_nacimiento: date
    sexo: str
    acepta_terminos: bool


class CompletarPerfilMedicoIn(BaseModel):
    medico_id: int
    tipo: str
    tipo_documento: str
    numero_documento: str
    matricula: str
    especialidad: str | None = None
    telefono: str
    direccion: str
    provincia: str | None = None
    localidad: str | None = None
    foto_dni_frente: str
    foto_dni_dorso: str
    selfie_dni: str
    acepta_terminos: bool


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def verify_token(token: str):
    try:
        payload = jose_jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        exp = payload.get("exp")
        if exp and datetime.fromtimestamp(exp, tz=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expirado")
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def _brevo_client():
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key["api-key"] = os.getenv("BREVO_API_KEY")
    return sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))


def enviar_email_validacion_paciente(email: str, user_id: str, full_name: str):
    token = create_access_token(
        {"sub": str(user_id), "email": email, "tipo": "validacion_paciente"},
        expires_minutes=60 * 24,
    )
    link_activacion = f"https://docya-railway-production.up.railway.app/auth/activar_paciente?token={token}"
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es"><head><meta charset="UTF-8"><title>Activación DocYa</title></head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr><td align="center">
          <table border="0" cellpadding="0" cellspacing="0" width="600" style="background:#ffffff; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
            <tr><td align="center" style="padding:30px 20px;">
              <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa" style="max-width:180px; margin-bottom:20px;">
              <h2 style="color:#00A8A8; font-size:22px; margin:0 0 15px;">¡Bienvenido a DocYa, {full_name}!</h2>
              <p style="color:#333333; font-size:15px; line-height:1.5; margin:0 0 25px;">Gracias por registrarte. Confirmá tu correo para activar tu cuenta.</p>
              <a href="{link_activacion}" target="_blank" style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">Activar mi cuenta</a>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """
    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa"},
        subject="Activa tu cuenta en DocYa",
        html_content=html_content,
    )
    try:
        _brevo_client().send_transac_email(email_data)
    except ApiException as exc:
        print(f"Error enviando email validación paciente: {exc}")


def enviar_email_validacion(email: str, medico_id: int, full_name: str):
    token = create_access_token(
        {"sub": str(medico_id), "email": email, "tipo": "validacion"},
        expires_minutes=60 * 24,
    )
    link_activacion = f"https://docya-railway-production.up.railway.app/auth/activar_medico?token={token}"
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es"><head><meta charset="UTF-8"><title>Activación DocYa</title></head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr><td align="center">
          <table border="0" cellpadding="0" cellspacing="0" width="600" style="background:#ffffff; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
            <tr><td align="center" style="padding:30px 20px;">
              <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" alt="DocYa" style="max-width:180px; margin-bottom:20px;">
              <h2 style="color:#00A8A8; font-size:22px; margin:0 0 15px;">¡Bienvenido al equipo DocYa, {full_name}!</h2>
              <p style="color:#333333; font-size:15px; line-height:1.5; margin:0 0 25px;">Confirmá tu correo para activar tu cuenta profesional.</p>
              <a href="{link_activacion}" target="_blank" style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">Activar mi cuenta</a>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """
    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "soporte@docya-railway-production.up.railway.app", "name": "DocYa"},
        subject="Activa tu cuenta en DocYa",
        html_content=html_content,
    )
    try:
        _brevo_client().send_transac_email(email_data)
    except ApiException as exc:
        print(f"Error enviando email validación profesional: {exc}")


def enviar_email_matricula_aprobada(email: str, full_name: str):
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es"><head><meta charset="UTF-8"><title>Matrícula aprobada</title></head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr><td align="center">
          <table border="0" cellpadding="0" cellspacing="0" width="600" style="background:#ffffff; border-radius:10px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
            <tr><td align="center" style="padding:34px 28px;">
              <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" alt="DocYa Pro" style="max-width:180px; margin-bottom:20px;">
              <h2 style="color:#00A8A8; font-size:24px; margin:0 0 14px;">Tu matrícula ya fue aprobada</h2>
              <p style="color:#334155; font-size:16px; line-height:1.6; margin:0 0 14px;">
                Hola <strong>{full_name}</strong>, revisamos tu documentación y tu cuenta profesional ya quedó habilitada.
              </p>
              <p style="color:#334155; font-size:15px; line-height:1.6; margin:0 0 24px;">
                Desde este momento ya podés ingresar a <strong>DocYa Pro</strong>, acceder a la app y comenzar a usar tus herramientas clínicas.
              </p>
              <div style="background:#ECFEFF; border:1px solid #A5F3FC; color:#155E75; border-radius:8px; padding:16px 18px; margin:0 0 24px; text-align:left;">
                <strong>Importante:</strong> si ya tenés la app instalada, simplemente volvé a abrirla e iniciá sesión con tu cuenta.
              </div>
              <a href="https://docya.online" target="_blank" style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">Ingresar a DocYa</a>
              <p style="color:#64748B; font-size:13px; line-height:1.5; margin:28px 0 0;">
                Gracias por sumarte a DocYa. Nos alegra tenerte en el equipo.
              </p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """
    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Pro"},
        subject="Tu matrícula fue aprobada y tu acceso a DocYa Pro ya está habilitado",
        html_content=html_content,
    )
    try:
        _brevo_client().send_transac_email(email_data)
    except ApiException as exc:
        print(f"Error enviando email aprobación profesional: {exc}")


@router.post("/auth/register")
def register(request: Request, data: RegisterIn, db=Depends(get_db)):
    """Alta de paciente con envío de mail de activación."""
    _ensure_user_profile_columns(db)
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    password_hash = pwd_context.hash(data.password)
    full_name = data.full_name.strip().title()

    try:
        ref_code = request.cookies.get("ref_code")
        cur.execute(
            """
            INSERT INTO users (
                email, full_name, password_hash,
                dni, telefono, pais, provincia, localidad,
                fecha_nacimiento, sexo,
                acepta_terminos, acepto_condiciones, fecha_aceptacion,
                version_texto, validado, role, perfil_completo,
                codigo_referido
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)
            RETURNING id, full_name
            """,
            (
                data.email.lower(),
                full_name,
                password_hash,
                data.dni,
                data.telefono,
                data.pais,
                data.provincia,
                data.localidad,
                data.fecha_nacimiento,
                data.sexo,
                data.acepto_condiciones,
                data.acepto_condiciones,
                now_argentina() if data.acepto_condiciones else None,
                "v1.0",
                "patient",
                False,
                ref_code,
            ),
        )
        user_id, full_name = cur.fetchone()
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno en registro: {exc}")

    enviar_email_validacion_paciente(data.email.lower(), user_id, full_name)
    return {
        "ok": True,
        "mensaje": "Registro exitoso. Revisa tu correo para activar tu cuenta.",
        "user_id": str(user_id),
        "full_name": full_name,
        "role": "patient",
    }


@router.get("/registro/paciente")
def registro(request: Request):
    """Página pública de registro paciente."""
    return templates.TemplateResponse("registro.html", {"request": request})


@router.get("/r")
def referido(request: Request):
    """Guarda el referido en cookie y redirige al registro."""
    ref = request.query_params.get("ref")
    response = RedirectResponse(url="/registro/paciente")
    if ref:
        response.set_cookie(
            key="ref_code",
            value=ref,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            domain=".docya.com.ar",
        )
    return response


@router.get("/users/{user_id}")
def get_user_by_id(user_id: str, db=Depends(get_db)):
    """Detalle de paciente con métricas básicas para perfil."""
    try:
        _ensure_user_profile_columns(db)
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        meses = 0
        if user.get("created_at"):
            try:
                created_at = user["created_at"]
                created_at_arg = created_at.replace(tzinfo=ARG_TZ) if created_at.tzinfo is None else created_at.astimezone(ARG_TZ)
                meses = (now_argentina() - created_at_arg).days // 30
            except Exception:
                meses = 0

        cur.execute("SELECT COUNT(*) AS total FROM consultas WHERE paciente_uuid = %s", (user_id,))
        consultas = cur.fetchone()
        user["consultas_count"] = consultas["total"] if consultas else 0
        user["meses_en_docya"] = meses
        return user
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/users/{user_id}/fcm_token")
def guardar_fcm_token_paciente(user_id: str, data: dict, db=Depends(get_db)):
    """Persiste el token FCM del paciente."""
    fcm_token = data.get("fcm_token")
    if not fcm_token:
        return {"detail": "Token FCM faltante"}, 400
    cur = db.cursor()
    try:
        cur.execute("UPDATE users SET fcm_token = %s WHERE id = %s", (fcm_token, user_id))
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"detail": f"Error guardando token: {exc}"}, 500
    return {"ok": True, "message": "Token actualizado"}


@router.post("/users/{user_id}/foto")
async def subir_foto_paciente(user_id: str, file: UploadFile = File(...), db=Depends(get_db)):
    """Sube y actualiza la foto de perfil del paciente."""
    try:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo debe ser una imagen válida")

        result = cloudinary.uploader.upload(
            file.file,
            folder="docya/pacientes",
            public_id=f"paciente_{user_id}",
            overwrite=True,
            resource_type="image",
        )
        foto_url = result.get("secure_url")
        if not foto_url:
            raise HTTPException(status_code=500, detail="Error al obtener la URL de Cloudinary")

        cur = db.cursor()
        cur.execute("UPDATE users SET foto_url = %s WHERE id = %s", (foto_url, user_id))
        db.commit()
        return {"foto_url": foto_url, "message": "Foto actualizada correctamente"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al subir la foto: {exc}")


@router.get("/auth/activar_paciente", response_class=HTMLResponse)
def activar_paciente(token: str, request: Request, db=Depends(get_db)):
    """Activa la cuenta paciente usando el token recibido por mail."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user_id = str(payload.get("sub"))
        cur = db.cursor()
        cur.execute("UPDATE users SET validado=TRUE WHERE id=%s RETURNING id, full_name", (user_id,))
        row = cur.fetchone()
        db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        return templates.TemplateResponse("activar_paciente.html", {"request": request, "nombre": row[1]})
    except jwt.ExpiredSignatureError:
        return HTMLResponse("<h1>El enlace de activación expiró</h1>", status_code=400)
    except Exception as exc:
        return HTMLResponse(f"<h1>Token inválido</h1><p>{exc}</p>", status_code=400)


@router.post("/auth/login")
def login(data: LoginIn, db=Depends(get_db)):
    """Login paciente por email o DNI."""
    _ensure_user_profile_columns(db)
    cur = db.cursor()
    input_value = data.email.strip().lower()
    password = data.password.strip()
    cur.execute(
        """
        SELECT id, full_name, password_hash, role, validado, email, dni, perfil_completo
        FROM users
        WHERE lower(email) = %s OR lower(trim(dni)) = %s
        LIMIT 1
        """,
        (input_value, input_value),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Usuario no encontrado")

    user_id, full_name, password_hash, role, validado, email, dni, perfil_completo = row
    if not pwd_context.verify(password, password_hash):
        raise HTTPException(status_code=400, detail="Contraseña incorrecta")
    if not validado:
        raise HTTPException(status_code=403, detail="Debes validar tu correo electrónico para iniciar sesión.")

    token = create_access_token({"sub": str(user_id), "email": email, "role": role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": str(user_id),
        "full_name": full_name,
        "user": {
            "id": str(user_id),
            "full_name": full_name,
            "email": email,
            "dni": dni,
            "role": role,
            "validado": True,
            "perfil_completo": bool(perfil_completo),
        },
        "perfil_completo": bool(perfil_completo),
    }


@router.post("/auth/google")
def auth_google(data: GoogleAuthIn, db=Depends(get_db)):
    """Login/registro con Google y creación automática de paciente base."""
    _ensure_user_profile_columns(db)
    try:
        request_adapter = google_requests.Request()
        payload = None
        last_error = None
        audiences = GOOGLE_CLIENT_IDS or [None]
        for audience in audiences:
            try:
                payload = google_id_token.verify_oauth2_token(data.id_token, request_adapter, audience)
                break
            except Exception as exc:
                last_error = exc
        if payload is None:
            raise HTTPException(status_code=401, detail=f"Token Google inválido: {last_error}")

        google_sub = payload.get("sub")
        email = (payload.get("email") or "").lower().strip()
        full_name = payload.get("name") or "Usuario DocYa"
        google_picture = (payload.get("picture") or "").strip() or None
        google_password_hash = get_password_hash(
            f"google-medico::{google_sub or 'sin-sub'}::{email or 'sin-email'}"
        )
        if not google_sub or not email:
            raise HTTPException(status_code=400, detail="Google no devolvió identidad suficiente")

        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, full_name, email, role, validado, perfil_completo, foto_url
            FROM users
            WHERE google_id = %s OR lower(email) = %s
            LIMIT 1
            """,
            (google_sub, email),
        )
        user = cur.fetchone()

        if user:
            cur.execute(
                """
                UPDATE users
                SET google_id = %s,
                    email = %s,
                    full_name = COALESCE(NULLIF(full_name, ''), %s),
                    foto_url = COALESCE(NULLIF(foto_url, ''), %s),
                    validado = TRUE
                WHERE id = %s
                RETURNING id, full_name, email, role, validado, perfil_completo, foto_url
                """,
                (google_sub, email, full_name, google_picture, user["id"]),
            )
            user = cur.fetchone()
        else:
            cur.execute(
                """
                INSERT INTO users (
                    email, full_name, google_id, foto_url, validado, role, acepta_terminos, perfil_completo
                )
                VALUES (%s, %s, %s, %s, TRUE, 'patient', FALSE, FALSE)
                RETURNING id, full_name, email, role, validado, perfil_completo, foto_url
                """,
                (email, full_name, google_sub, google_picture),
            )
            user = cur.fetchone()

        db.commit()

        token = create_access_token(
            {"sub": str(user["id"]), "email": user["email"], "role": user["role"]}
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "id": str(user["id"]),
                "full_name": user["full_name"],
                "email": user["email"],
                "role": user["role"],
                "validado": bool(user["validado"]),
                "perfil_completo": bool(user["perfil_completo"]),
                "foto_url": user.get("foto_url"),
            },
            "perfil_completo": bool(user["perfil_completo"]),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error en auth Google: {exc}")


@router.post("/completar_perfil")
def completar_perfil(data: CompletarPerfilIn, db=Depends(get_db)):
    """Completa el perfil obligatorio del paciente con validaciones globales."""
    _ensure_user_profile_columns(db)

    telefono = (data.telefono or "").strip()
    tipo_documento = (data.tipo_documento or "").strip().lower()
    numero_documento = (data.numero_documento or "").strip()
    direccion = (data.direccion or "").strip()
    sexo = (data.sexo or "").strip().lower()

    if not re.match(E164_REGEX, telefono):
        raise HTTPException(status_code=400, detail="El teléfono debe estar en formato internacional E.164")
    if tipo_documento not in TIPOS_DOCUMENTO:
        raise HTTPException(status_code=400, detail="Tipo de documento inválido")
    if not numero_documento:
        raise HTTPException(status_code=400, detail="Número de documento obligatorio")
    if not direccion:
        raise HTTPException(status_code=400, detail="Dirección obligatoria")
    if sexo not in SEXOS_VALIDOS:
        raise HTTPException(status_code=400, detail="Sexo inválido")
    if not data.acepta_terminos:
        raise HTTPException(status_code=400, detail="Debes aceptar los términos para continuar")

    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM users WHERE id = %s", (data.user_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    dni_value = numero_documento if tipo_documento == "dni" else None
    cur.execute(
        """
        UPDATE users
        SET telefono = %s,
            tipo_documento = %s,
            numero_documento = %s,
            dni = COALESCE(%s, dni),
            direccion = %s,
            fecha_nacimiento = %s,
            sexo = %s,
            acepta_terminos = %s,
            acepto_condiciones = %s,
            fecha_aceptacion = CASE WHEN %s THEN COALESCE(fecha_aceptacion, NOW()) ELSE fecha_aceptacion END,
            perfil_completo = TRUE
        WHERE id = %s
        RETURNING id, email, full_name, perfil_completo, telefono, tipo_documento, numero_documento, direccion, fecha_nacimiento, sexo, acepta_terminos
        """,
        (
            telefono,
            tipo_documento,
            numero_documento,
            dni_value,
            direccion,
            data.fecha_nacimiento,
            sexo,
            data.acepta_terminos,
            data.acepta_terminos,
            data.acepta_terminos,
            data.user_id,
        ),
    )
    updated = cur.fetchone()
    db.commit()

    return {
        "ok": True,
        "perfil_completo": True,
        "user": {
            "id": str(updated["id"]),
            "email": updated["email"],
            "full_name": updated["full_name"],
            "telefono": updated["telefono"],
            "tipo_documento": updated["tipo_documento"],
            "numero_documento": updated["numero_documento"],
            "direccion": updated["direccion"],
            "fecha_nacimiento": str(updated["fecha_nacimiento"]),
            "sexo": updated["sexo"],
            "acepta_terminos": bool(updated["acepta_terminos"]),
            "perfil_completo": bool(updated["perfil_completo"]),
        },
    }


@router.post("/auth/google_medico")
def auth_google_medico(data: GoogleAuthIn, db=Depends(get_db)):
    """Login/registro con Google para profesionales."""
    _ensure_medico_profile_columns(db)
    try:
        request_adapter = google_requests.Request()
        payload = None
        last_error = None
        audiences = GOOGLE_CLIENT_IDS or [None]
        for audience in audiences:
            try:
                payload = google_id_token.verify_oauth2_token(
                    data.id_token, request_adapter, audience
                )
                break
            except Exception as exc:
                last_error = exc

        if payload is None:
            raise HTTPException(
                status_code=401, detail=f"Token Google inválido: {last_error}"
            )

        google_sub = payload.get("sub")
        email = (payload.get("email") or "").lower().strip()
        full_name = (payload.get("name") or "Profesional DocYa").strip()
        google_picture = (payload.get("picture") or "").strip() or None
        google_password_hash = get_password_hash(
            f"google-medico::{google_sub or 'sin-sub'}::{email or 'sin-email'}"
        )
        provisional_matricula = f"GOOGLE-{(google_sub or email or 'PRO').upper()[:40]}"
        if not google_sub or not email:
            raise HTTPException(
                status_code=400,
                detail="Google no devolvió identidad suficiente",
            )

        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, full_name, email, tipo, validado, matricula_validada,
                   perfil_completo, foto_perfil
            FROM medicos
            WHERE google_id = %s OR lower(email) = %s
            LIMIT 1
            """,
            (google_sub, email),
        )
        medico = cur.fetchone()

        if medico:
            cur.execute(
                """
                UPDATE medicos
                SET google_id = %s,
                    email = %s,
                    full_name = COALESCE(NULLIF(full_name, ''), %s),
                    foto_perfil = COALESCE(NULLIF(foto_perfil, ''), %s),
                    password_hash = COALESCE(password_hash, %s)
                WHERE id = %s
                RETURNING id, full_name, email, tipo, validado, matricula_validada,
                          perfil_completo, foto_perfil
                """,
                (
                    google_sub,
                    email,
                    full_name,
                    google_picture,
                    google_password_hash,
                    medico["id"],
                ),
            )
            medico = cur.fetchone()
        else:
            cur.execute(
                """
                INSERT INTO medicos (
                    full_name, email, password_hash, google_id, foto_perfil, tipo, matricula,
                    validado, matricula_validada, perfil_completo, acepta_terminos
                )
                VALUES (%s, %s, %s, %s, %s, 'medico', %s, FALSE, FALSE, FALSE, FALSE)
                RETURNING id, full_name, email, tipo, validado, matricula_validada,
                          perfil_completo, foto_perfil
                """,
                (
                    full_name,
                    email,
                    google_password_hash,
                    google_sub,
                    google_picture,
                    provisional_matricula,
                ),
            )
            medico = cur.fetchone()

        db.commit()

        token = create_access_token(
            {"sub": str(medico["id"]), "email": medico["email"], "role": medico["tipo"]}
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "medico_id": medico["id"],
            "full_name": medico["full_name"],
            "tipo": medico["tipo"],
            "validado": bool(medico["validado"]),
            "matricula_validada": bool(medico["matricula_validada"]),
            "perfil_completo": bool(medico["perfil_completo"]),
            "medico": {
                "id": medico["id"],
                "full_name": medico["full_name"],
                "email": medico["email"],
                "tipo": medico["tipo"],
                "validado": bool(medico["validado"]),
                "matricula_validada": bool(medico["matricula_validada"]),
                "perfil_completo": bool(medico["perfil_completo"]),
                "foto_perfil": medico.get("foto_perfil"),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error en auth Google profesional: {exc}")


@router.post("/auth/completar_perfil_medico")
def completar_perfil_medico(data: CompletarPerfilMedicoIn, db=Depends(get_db)):
    """Completa el perfil obligatorio del profesional luego de Google."""
    _ensure_medico_profile_columns(db)

    tipo = unidecode((data.tipo or "").strip().lower())
    tipo_documento = (data.tipo_documento or "").strip().lower()
    numero_documento = (data.numero_documento or "").strip()
    telefono = (data.telefono or "").strip()
    direccion = (data.direccion or "").strip()
    matricula = (data.matricula or "").strip()

    if tipo not in {"medico", "enfermero"}:
        raise HTTPException(status_code=400, detail="Tipo profesional inválido")
    if tipo_documento not in TIPOS_DOCUMENTO:
        raise HTTPException(status_code=400, detail="Tipo de documento inválido")
    if not numero_documento:
        raise HTTPException(status_code=400, detail="Número de documento obligatorio")
    if not matricula:
        raise HTTPException(status_code=400, detail="Matrícula obligatoria")
    if not re.match(E164_REGEX, telefono):
        raise HTTPException(status_code=400, detail="El teléfono debe estar en formato internacional E.164")
    if not direccion:
        raise HTTPException(status_code=400, detail="Dirección obligatoria")
    if not data.acepta_terminos:
        raise HTTPException(status_code=400, detail="Debes aceptar los términos para continuar")

    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, foto_dni_frente, foto_dni_dorso, selfie_dni FROM medicos WHERE id = %s", (data.medico_id,))
    medico = cur.fetchone()
    if not medico:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    cur.execute(
        "SELECT id FROM medicos WHERE matricula = %s AND id <> %s",
        (matricula, data.medico_id),
    )
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    dni_value = numero_documento if tipo_documento == "dni" else None
    folder = f"docya/medicos/{data.medico_id}"
    foto_dni_frente_url = _upload_base64_image(data.foto_dni_frente, folder, "dni_frente") or medico.get("foto_dni_frente")
    foto_dni_dorso_url = _upload_base64_image(data.foto_dni_dorso, folder, "dni_dorso") or medico.get("foto_dni_dorso")
    selfie_dni_url = _upload_base64_image(data.selfie_dni, folder, "selfie_dni") or medico.get("selfie_dni")

    if not foto_dni_frente_url or not foto_dni_dorso_url or not selfie_dni_url:
        raise HTTPException(status_code=400, detail="Debes subir frente, dorso y selfie con documento")

    cur.execute(
        """
        UPDATE medicos
        SET tipo = %s,
            tipo_documento = %s,
            numero_documento = %s,
            dni = %s,
            matricula = %s,
            especialidad = %s,
            telefono = %s,
            direccion = %s,
            provincia = %s,
            localidad = %s,
            foto_dni_frente = %s,
            foto_dni_dorso = %s,
            selfie_dni = %s,
            acepta_terminos = %s,
            perfil_completo = TRUE,
            validado = FALSE,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id, full_name, email, tipo, validado, matricula_validada, perfil_completo
        """,
        (
            tipo,
            tipo_documento,
            numero_documento,
            dni_value,
            matricula,
            (data.especialidad or "").strip() or None,
            telefono,
            direccion,
            (data.provincia or "").strip() or None,
            (data.localidad or "").strip() or None,
            foto_dni_frente_url,
            foto_dni_dorso_url,
            selfie_dni_url,
            data.acepta_terminos,
            data.medico_id,
        ),
    )
    updated = cur.fetchone()
    db.commit()

    return {
        "ok": True,
        "perfil_completo": bool(updated["perfil_completo"]),
        "medico": {
            "id": updated["id"],
            "full_name": updated["full_name"],
            "email": updated["email"],
            "tipo": updated["tipo"],
            "validado": bool(updated["validado"]),
            "matricula_validada": bool(updated["matricula_validada"]),
            "perfil_completo": bool(updated["perfil_completo"]),
        },
    }


@router.post("/auth/register_medico")
def register_medico(data: RegisterMedicoIn, db=Depends(get_db)):
    """Alta profesional con validación por mail y subida inicial de imágenes."""
    _ensure_medico_profile_columns(db)
    cur = db.cursor()
    cur.execute("SELECT id FROM medicos WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
    cur.execute("SELECT id FROM medicos WHERE matricula=%s", (data.matricula,))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    telefono = (data.telefono or "").strip()
    if not re.match(E164_REGEX, telefono):
        raise HTTPException(status_code=400, detail="El teléfono debe estar en formato internacional, por ejemplo +5491122334455")

    tipo_documento = (data.tipo_documento or ("dni" if (data.dni or "").strip() else "")).strip().lower()
    numero_documento = (data.numero_documento or data.dni or "").strip()
    if tipo_documento not in TIPOS_DOCUMENTO:
        raise HTTPException(status_code=400, detail="Tipo de documento inválido")
    if not numero_documento:
        raise HTTPException(status_code=400, detail="Número de documento obligatorio")
    if not data.direccion or not data.direccion.strip():
        raise HTTPException(status_code=400, detail="Dirección obligatoria")
    if not data.acepta_terminos:
        raise HTTPException(status_code=400, detail="Debes aceptar los términos para continuar")
    if not data.foto_dni_frente or not data.foto_dni_dorso or not data.selfie_dni:
        raise HTTPException(status_code=400, detail="Debes subir frente, dorso y selfie con documento")

    password_hash = pwd_context.hash(data.password)
    full_name = data.full_name.strip().title()
    tipo_normalizado = unidecode(data.tipo.strip().lower())
    dni_value = numero_documento if tipo_documento == "dni" else None

    cur.execute(
        """
        INSERT INTO medicos (
            full_name, email, password_hash, matricula, especialidad, tipo, telefono,
            provincia, localidad, dni, tipo_documento, numero_documento, direccion,
            acepta_terminos, perfil_completo, validado
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,FALSE)
        RETURNING id, full_name, tipo
        """,
        (
            full_name,
            data.email.lower(),
            password_hash,
            data.matricula,
            (data.especialidad or "").strip() or None,
            tipo_normalizado,
            telefono,
            (data.provincia or "").strip() or None,
            (data.localidad or "").strip() or None,
            dni_value,
            tipo_documento,
            numero_documento,
            data.direccion.strip(),
            data.acepta_terminos,
        ),
    )
    medico_id, full_name, tipo = cur.fetchone()
    db.commit()

    foto_perfil_url = _upload_base64_image(data.foto_perfil, f"docya/medicos/{medico_id}", "perfil")
    foto_dni_frente_url = _upload_base64_image(data.foto_dni_frente, f"docya/medicos/{medico_id}", "dni_frente")
    foto_dni_dorso_url = _upload_base64_image(data.foto_dni_dorso, f"docya/medicos/{medico_id}", "dni_dorso")
    selfie_dni_url = _upload_base64_image(data.selfie_dni, f"docya/medicos/{medico_id}", "selfie_dni")

    cur.execute(
        """
        UPDATE medicos
        SET foto_perfil=%s,
            foto_dni_frente=%s,
            foto_dni_dorso=%s,
            selfie_dni=%s
        WHERE id=%s
        """,
        (foto_perfil_url, foto_dni_frente_url, foto_dni_dorso_url, selfie_dni_url, medico_id),
    )
    db.commit()

    enviar_email_validacion(data.email.lower(), medico_id, full_name)
    return {
        "ok": True,
        "mensaje": f"Registro exitoso como {tipo}. Revisa tu correo para activar tu cuenta.",
        "medico_id": medico_id,
        "full_name": full_name,
        "tipo": tipo,
        "perfil_completo": True,
        "fotos": {
            "perfil": foto_perfil_url,
            "dni_frente": foto_dni_frente_url,
            "dni_dorso": foto_dni_dorso_url,
            "selfie_dni": selfie_dni_url,
        },
    }


@router.get("/auth/activar_medico", response_class=HTMLResponse)
def activar_medico(token: str, request: Request, db=Depends(get_db)):
    """Activa la cuenta profesional desde el link recibido por correo."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        medico_id = int(payload.get("sub"))
        cur = db.cursor()
        cur.execute(
            "SELECT id, full_name FROM medicos WHERE id=%s",
            (medico_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Médico no encontrado")
        return templates.TemplateResponse(
            "activar_medico.html",
            {
                "request": request,
                "nombre": row[1],
                "mensaje_extra": "Tu correo fue confirmado. Tu cuenta seguirá bloqueada hasta que DocYa valide tu matrícula y habilite el acceso.",
            },
        )
    except jwt.ExpiredSignatureError:
        return HTMLResponse("<h1>El enlace de activación expiró</h1>", status_code=400)
    except Exception as exc:
        return HTMLResponse(f"<h1>Token inválido</h1><p>{exc}</p>", status_code=400)


@router.post("/auth/login_medico")
def login_medico(data: LoginMedicoIn, db=Depends(get_db)):
    """Login profesional por email o DNI."""
    _ensure_medico_profile_columns(db)
    cur = db.cursor()
    input_value = data.email.strip().lower()
    password = data.password.strip()
    cur.execute(
        """
        SELECT id, full_name, password_hash, validado, tipo, email, dni, matricula_validada, perfil_completo
        FROM medicos
        WHERE lower(email) = %s OR trim(lower(dni)) = %s
        """,
        (input_value, input_value),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Usuario no encontrado")
    if not pwd_context.verify(password, row[2]):
        raise HTTPException(status_code=400, detail="Contraseña incorrecta")
    if not row[3]:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta profesional está en revisión. Te avisaremos por email cuando la matrícula sea aprobada y el acceso quede habilitado.",
        )
    if not row[7]:
        raise HTTPException(
            status_code=403,
            detail="Tu matrícula todavía no fue aprobada por el equipo DocYa.",
        )

    token = create_access_token({"sub": str(row[0]), "email": row[5], "role": row[4]})
    return {
        "access_token": token,
        "token_type": "bearer",
        "medico_id": row[0],
        "full_name": row[1],
        "tipo": row[4],
        "email": row[5],
        "dni": row[6],
        "matricula_validada": row[7],
        "perfil_completo": bool(row[8]),
        "medico": {
            "id": row[0],
            "full_name": row[1],
            "validado": True,
            "tipo": row[4],
            "email": row[5],
            "dni": row[6],
            "matricula_validada": row[7],
            "perfil_completo": bool(row[8]),
        },
    }


@router.post("/auth/validar_medico/{medico_id}")
def validar_medico(medico_id: int, db=Depends(get_db)):
    """Habilita o bloquea el acceso profesional y sincroniza la aprobación de matrícula."""
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, full_name, email, tipo, validado, matricula_validada
        FROM medicos
        WHERE id = %s
        """,
        (medico_id,),
    )
    medico = cur.fetchone()
    if not medico:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    nuevo_estado = not bool(medico["validado"])
    cur.execute(
        """
        UPDATE medicos
        SET validado = %s,
            matricula_validada = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id, full_name, email, tipo, validado, matricula_validada
        """,
        (nuevo_estado, nuevo_estado, medico_id),
    )
    row = cur.fetchone()
    db.commit()

    email_enviado = False
    if row["validado"]:
        enviar_email_matricula_aprobada(row["email"], row["full_name"])
        email_enviado = True

    return {
        "ok": True,
        "medico_id": row["id"],
        "nombre": row["full_name"],
        "tipo": row["tipo"],
        "validado": bool(row["validado"]),
        "matricula_validada": bool(row["matricula_validada"]),
        "email_enviado": email_enviado,
        "mensaje": (
            "Acceso habilitado, matrícula aprobada y correo enviado al profesional."
            if row["validado"]
            else "Acceso bloqueado y matrícula marcada como no aprobada."
        ),
    }


@router.post("/auth/medico/{medico_id}/foto")
def actualizar_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    """Actualiza la foto de perfil del profesional."""
    try:
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="docya/medicos",
            public_id=f"medico_{medico_id}",
            overwrite=True,
        )
        foto_url = upload_result["secure_url"]
        cur = db.cursor()
        cur.execute(
            """
            UPDATE medicos
            SET foto_perfil=%s, updated_at=NOW()
            WHERE id=%s
            RETURNING id, foto_perfil
            """,
            (foto_url, medico_id),
        )
        row = cur.fetchone()
        db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")
        return {"ok": True, "medico_id": row[0], "foto_url": row[1]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error subiendo foto: {exc}")


@router.patch("/auth/medico/{medico_id}/alias")
def actualizar_alias(medico_id: int, data: AliasIn, conn=Depends(get_db)):
    """Actualiza alias o alias CBU del profesional."""
    alias = data.alias_cbu or data.alias
    if not alias:
        raise HTTPException(status_code=400, detail="Alias requerido")
    cursor = conn.cursor()
    cursor.execute("UPDATE medicos SET alias_cbu = %s WHERE id = %s", (alias, medico_id))
    conn.commit()
    cursor.close()
    return {"ok": True}


@router.post("/auth/medico/{medico_id}/disponibilidad")
def actualizar_disponibilidad(medico_id: int, disponible: bool, db=Depends(get_db)):
    """Setea el estado disponible/no disponible del profesional."""
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        UPDATE medicos
        SET disponible=%s
        WHERE id=%s
        RETURNING id, disponible
        """,
        (disponible, medico_id),
    )
    row = cur.fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": medico_id, "disponible": row["disponible"]}


@router.get("/auth/medico/{medico_id}/stats")
def medico_stats(medico_id: int, db=Depends(get_db)):
    """Resume actividad y ganancias semanales del profesional."""
    cur = db.cursor()
    cur.execute("SELECT tipo FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    tipo = row[0].lower().strip()
    inicio_semana = start_of_week_argentina()
    fin_semana = inicio_semana + timedelta(days=6)

    cur.execute(
        f"""
        SELECT id, fin_atencion, metodo_pago
        FROM consultas
        WHERE medico_id = %s
        AND estado = 'finalizada'
        AND DATE_TRUNC('week', fin_atencion) = {CURRENT_ARGENTINA_WEEK_SQL}
        """,
        (medico_id,),
    )
    consultas_finalizadas = cur.fetchall()

    consultas_diurnas = 0
    consultas_nocturnas = 0
    ganancias_diurnas = 0
    ganancias_nocturnas = 0
    consultas_diurnas_tarjeta = 0
    consultas_nocturnas_tarjeta = 0
    consultas_diurnas_efectivo = 0
    consultas_nocturnas_efectivo = 0
    tarifa_dia = 30000 if tipo == "medico" else 20000
    tarifa_noche = 40000 if tipo == "medico" else 30000
    metodo_contador = {}

    for _, fin_atencion, metodo_pago in consultas_finalizadas:
        hora = fin_atencion.time()
        metodo = (metodo_pago or "efectivo").lower().strip()
        metodo_contador[metodo] = metodo_contador.get(metodo, 0) + 1
        es_nocturna = (hora >= time(22, 0)) or (hora < time(6, 0))
        if es_nocturna:
            consultas_nocturnas += 1
            ganancias_nocturnas += tarifa_noche
            if metodo == "tarjeta":
                consultas_nocturnas_tarjeta += 1
            else:
                consultas_nocturnas_efectivo += 1
        else:
            consultas_diurnas += 1
            ganancias_diurnas += tarifa_dia
            if metodo == "tarjeta":
                consultas_diurnas_tarjeta += 1
            else:
                consultas_diurnas_efectivo += 1

    ganancias_total = ganancias_diurnas + ganancias_nocturnas
    consultas_total = consultas_diurnas + consultas_nocturnas
    metodo_frecuente = max(metodo_contador, key=metodo_contador.get) if metodo_contador else None

    cur.execute(
        f"""
        SELECT COALESCE(metodo_pago, 'efectivo') AS metodo_pago,
               COUNT(*) AS cantidad,
               COALESCE(SUM(medico_neto), 0) AS total
        FROM pagos_consulta
        WHERE medico_id = %s
        AND DATE_TRUNC('week', fecha) = {CURRENT_ARGENTINA_WEEK_SQL}
        GROUP BY metodo_pago
        """,
        (medico_id,),
    )
    rows = cur.fetchall()
    detalle_pagos = {row[0]: {"cantidad": int(row[1]), "monto": float(row[2])} for row in rows}

    return {
        "tipo": tipo,
        "periodo": f"{inicio_semana} → {fin_semana}",
        "consultas": consultas_total,
        "ganancias": ganancias_total,
        "consultas_diurnas": consultas_diurnas,
        "consultas_nocturnas": consultas_nocturnas,
        "ganancias_diurnas": ganancias_diurnas,
        "ganancias_nocturnas": ganancias_nocturnas,
        "consultas_diurnas_tarjeta": consultas_diurnas_tarjeta,
        "consultas_nocturnas_tarjeta": consultas_nocturnas_tarjeta,
        "consultas_diurnas_efectivo": consultas_diurnas_efectivo,
        "consultas_nocturnas_efectivo": consultas_nocturnas_efectivo,
        "metodo_frecuente": metodo_frecuente,
        "detalle_pagos": detalle_pagos,
    }


@router.post("/auth/medico/{medico_id}/fcm_token")
def actualizar_fcm_token(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    """Persiste el token push del profesional."""
    cur = db.cursor()
    cur.execute(
        """
        UPDATE medicos
        SET fcm_token=%s, updated_at=NOW()
        WHERE id=%s
        RETURNING id
        """,
        (data.fcm_token, medico_id),
    )
    row = cur.fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": medico_id, "fcm_token": data.fcm_token}


@router.get("/auth/medico/{medico_id}")
def obtener_medico(medico_id: int, db=Depends(get_db)):
    """Detalle de perfil profesional usado por DocYa Pro."""
    _ensure_medico_profile_columns(db)
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, full_name, email, especialidad, telefono,
               alias_cbu, matricula, foto_perfil, tipo, firma_url,
               direccion, tipo_documento, numero_documento, perfil_completo,
               acepta_terminos, matricula_validada
        FROM medicos
        WHERE id=%s
        """,
        (medico_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {
        "id": row[0],
        "full_name": row[1],
        "email": row[2],
        "especialidad": row[3],
        "telefono": row[4],
        "alias_cbu": row[5],
        "matricula": row[6],
        "foto_perfil": row[7],
        "tipo": row[8],
        "firma_url": row[9],
        "direccion": row[10],
        "tipo_documento": row[11],
        "numero_documento": row[12],
        "perfil_completo": bool(row[13]),
        "acepta_terminos": bool(row[14]),
        "matricula_validada": bool(row[15]),
    }


@router.post("/auth/forgot_password")
def forgot_password(data: ForgotPasswordIn, db=Depends(get_db)):
    """Inicia recuperación de contraseña para profesionales."""
    cur = db.cursor()
    identificador = data.identificador.strip().lower()
    cur.execute(
        """
        SELECT id, full_name, email
        FROM medicos
        WHERE LOWER(email) = %s OR dni = %s
        LIMIT 1
        """,
        (identificador, identificador),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No se encontró un profesional con esos datos")

    medico_id, full_name, email = row
    token = create_access_token({"sub": str(medico_id), "email": email, "tipo": "reset_password"}, expires_minutes=60)
    link_reset = f"https://docya-railway-production.up.railway.app/auth/reset_password?token={token}"
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es"><head><meta charset="UTF-8"><title>Restablecer contraseña</title></head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:20px 0;">
        <tr><td align="center">
          <table width="600" bgcolor="#ffffff" style="border-radius:10px; padding:35px; text-align:center; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
            <tr><td>
              <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" alt="DocYa Pro" style="width:180px; margin-bottom:20px;">
              <h2 style="color:#14B8A6; font-size:22px; margin-bottom:15px;">Restablecer tu contraseña</h2>
              <p style="color:#333; font-size:15px; line-height:1.6;">Hola <b>{full_name}</b>, recibimos una solicitud para restablecer tu contraseña.</p>
              <a href="{link_reset}" target="_blank" style="background-color:#14B8A6; color:#fff; padding:14px 28px; text-decoration:none; border-radius:6px; font-size:16px; font-weight:bold; display:inline-block; margin-top:25px;">Restablecer contraseña</a>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>
    """
    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Pro"},
        subject="Restablecé tu contraseña – DocYa Pro",
        html_content=html_content,
    )
    try:
        _brevo_client().send_transac_email(email_data)
    except ApiException as exc:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo de recuperación: {exc}")
    return {"ok": True, "message": f"Enviamos un correo a {email} para que recuperes tu contraseña."}


@router.post("/auth/reset_password")
def reset_password(data: ResetPasswordIn, db=Depends(get_db)):
    """Aplica el cambio de contraseña para un profesional."""
    try:
        payload = verify_token(data.token)
        medico_id = payload.get("sub")
        if not medico_id:
            raise HTTPException(status_code=400, detail="Token inválido")

        hashed = get_password_hash(data.new_password)
        cur = db.cursor()
        cur.execute("UPDATE medicos SET password_hash = %s WHERE id = %s RETURNING id", (hashed, medico_id))
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")

        cur.execute("SELECT full_name, email FROM medicos WHERE id = %s", (medico_id,))
        full_name, email = cur.fetchone()
        html_confirm = f"""
        <html><body style="font-family: Arial, sans-serif; background-color:#F4F6F8; margin:0; padding:0;">
          <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0;">
            <tr><td align="center">
              <table width="600" bgcolor="#ffffff" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1); padding:30px;">
                <tr><td align="center">
                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" alt="DocYa Pro" style="max-width:160px; margin-bottom:20px;">
                  <h2 style="color:#14B8A6;">Contraseña actualizada con éxito</h2>
                  <p style="font-size:15px; color:#333333;">Hola <b>{full_name}</b>, tu contraseña fue cambiada correctamente.</p>
                </td></tr>
              </table>
            </td></tr>
          </table>
        </body></html>
        """
        confirm_email = SendSmtpEmail(
            to=[{"email": email, "name": full_name}],
            sender={"email": "soporte@docya-railway-production.up.railway.app", "name": "DocYa Pro"},
            subject="Contraseña actualizada – DocYa Pro",
            html_content=html_confirm,
        )
        _brevo_client().send_transac_email(confirm_email)
        return {"ok": True, "message": "Contraseña actualizada correctamente."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error interno al restablecer la contraseña: {exc}")


@router.get("/auth/reset_password", response_class=HTMLResponse)
def render_reset_password_page(request: Request, token: str | None = None):
    """Renderiza la página HTML de reseteo para profesionales."""
    if not token:
        return HTMLResponse("<h3 style='font-family:sans-serif;color:#555;text-align:center;margin-top:80px;'>Enlace inválido o faltante.</h3>", status_code=400)
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


@router.post("/auth/forgot_password_paciente")
def forgot_password_paciente(data: ForgotPasswordIn, db=Depends(get_db)):
    """Inicia recuperación de contraseña para pacientes."""
    cur = db.cursor()
    identificador = data.identificador.strip().lower()
    cur.execute(
        """
        SELECT id, full_name, email
        FROM users
        WHERE LOWER(email) = %s OR dni = %s
        LIMIT 1
        """,
        (identificador, identificador),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No se encontró un paciente con esos datos")

    paciente_id, full_name, email = row
    token = create_access_token(
        {"sub": str(paciente_id), "email": email, "tipo": "reset_password_paciente"},
        expires_minutes=60,
    )
    link_reset = f"https://docya-railway-production.up.railway.app/auth/reset_password_paciente?token={token}"
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es"><head><meta charset="UTF-8"><title>Restablecer contraseña – DocYa</title></head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:20px 0;">
        <tr><td align="center">
          <table width="600" bgcolor="#ffffff" style="border-radius:10px; padding:35px; text-align:center; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
            <tr><td>
              <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa" style="width:180px; margin-bottom:20px;">
              <h2 style="color:#14B8A6; font-size:22px; margin-bottom:15px;">Restablecer tu contraseña</h2>
              <p style="color:#333; font-size:15px; line-height:1.6;">Hola <b>{full_name}</b>, recibimos una solicitud para restablecer tu contraseña.</p>
              <a href="{link_reset}" target="_blank" style="background-color:#14B8A6; color:#fff; padding:14px 28px; text-decoration:none; border-radius:6px; font-size:16px; font-weight:bold; display:inline-block; margin-top:25px;">Cambiar contraseña</a>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>
    """
    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Atención al Paciente"},
        subject="Restablecé tu contraseña – DocYa",
        html_content=html_content,
    )
    try:
        _brevo_client().send_transac_email(email_data)
    except ApiException as exc:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo de recuperación: {exc}")
    return {"ok": True, "message": f"Enviamos un correo a {email} para restablecer tu contraseña."}


@router.post("/auth/reset_password_paciente")
def reset_password_paciente(data: ResetPasswordIn, db=Depends(get_db)):
    """Aplica el cambio de contraseña para paciente."""
    try:
        payload = verify_token(data.token)
        paciente_id = payload.get("sub")
        if not paciente_id:
            raise HTTPException(status_code=400, detail="Token inválido o expirado")

        hashed = get_password_hash(data.new_password)
        cur = db.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s RETURNING id", (hashed, paciente_id))
        updated = cur.fetchone()
        db.commit()
        if not updated:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")

        cur.execute("SELECT full_name, email FROM users WHERE id = %s", (paciente_id,))
        full_name, email = cur.fetchone()
        html_confirm = f"""
        <html><body style="font-family: Arial, sans-serif; background-color:#F4F6F8; margin:0; padding:0;">
          <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0;">
            <tr><td align="center">
              <table width="600" bgcolor="#ffffff" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1); padding:30px;">
                <tr><td align="center">
                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa" style="max-width:160px; margin-bottom:20px;">
                  <h2 style="color:#14B8A6;">Contraseña actualizada con éxito</h2>
                  <p style="font-size:15px; color:#333333;">Hola <b>{full_name}</b>, tu contraseña fue cambiada correctamente.</p>
                </td></tr>
              </table>
            </td></tr>
          </table>
        </body></html>
        """
        confirm_email = SendSmtpEmail(
            to=[{"email": email, "name": full_name}],
            sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Atención al Paciente"},
            subject="Contraseña actualizada – DocYa",
            html_content=html_confirm,
        )
        _brevo_client().send_transac_email(confirm_email)
        return {"ok": True, "message": "Contraseña actualizada correctamente."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error interno al restablecer la contraseña del paciente: {exc}")


@router.get("/cambio_exitoso", response_class=HTMLResponse)
def cambio_exitoso():
    """Pantalla simple de confirmación tras cambiar la contraseña."""
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Contraseña Actualizada – DocYa</title>
    </head>
    <body style="font-family: Arial, sans-serif; background:#ffffff; text-align:center; padding:40px;">
        <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa" style="max-width:180px;">
        <h1 style="color:#14B8A6;">¡Contraseña actualizada con éxito!</h1>
        <p>Ya podés iniciar sesión en tu aplicación <b>DocYa</b> desde tu celular.</p>
        <div style="margin-top:24px;color:#6b7280;">© 2025 DocYa · Atención médica y de enfermería a domicilio</div>
    </body>
    </html>
    """
    return HTMLResponse(html)


@router.get("/auth/reset_password_paciente", response_class=HTMLResponse)
def render_reset_password_paciente_page(request: Request, token: str | None = None):
    """Renderiza la página HTML de reseteo para pacientes."""
    if not token:
        return HTMLResponse("<h3 style='font-family:sans-serif;color:#555;text-align:center;margin-top:80px;'>Enlace inválido o faltante.</h3>", status_code=400)
    return templates.TemplateResponse("reset_password_paciente.html", {"request": request, "token": token})


@router.post("/auth/medico/{medico_id}/firma")
def subir_firma_digital(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    """Sube la firma digital del profesional a Cloudinary y la persiste."""
    try:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo debe ser una imagen válida (PNG/JPG)")

        result = cloudinary.uploader.upload(
            file.file,
            folder=f"docya/firmas/{medico_id}",
            public_id=f"firma_{medico_id}",
            overwrite=True,
            resource_type="image",
        )
        firma_url = result.get("secure_url")
        if not firma_url:
            raise HTTPException(status_code=500, detail="Error al obtener URL de Cloudinary")

        cur = db.cursor()
        cur.execute(
            """
            UPDATE medicos
            SET firma_url = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING id, firma_url
            """,
            (firma_url, medico_id),
        )
        row = cur.fetchone()
        db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")
        return {"ok": True, "firma_url": row[1]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al subir la firma: {exc}")
