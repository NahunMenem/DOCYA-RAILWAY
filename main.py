# ====================================================
# 📌 IMPORTS Y CONFIGURACIÓN INICIAL.
# ====================================================
import os
import json
import math
import jwt
import psycopg2
import requests
from uuid import UUID
from typing import Optional, Dict
from datetime import datetime, timedelta, date
from unidecode import unidecode
from zoneinfo import ZoneInfo
from fastapi import Request

from fastapi import (
    FastAPI, HTTPException, Depends, Query,
    File, UploadFile, WebSocket, WebSocketDisconnect, Request
)
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

# Google & Email
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# Cloudinary
import cloudinary
import cloudinary.uploader
import httpx
from fastapi import APIRouter, Depends, HTTPException
# ====================================================
# 🌐 VARIABLES GLOBALES Y CONFIGURACIONES
# ====================================================
active_medicos: Dict[int, WebSocket] = {}
active_chats: Dict[int, list[WebSocket]] = {}

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET", "change_me")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ====================================================
# ⚙️ FUNCIONES UTILITARIAS
# ====================================================
def now_argentina():
    return datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))

def format_datetime_arg(dt):
    if not dt:
        return None
    dt = dt.astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
    return dt.strftime("%d/%m/%Y %H:%M")

def create_access_token(payload: dict, expires_minutes: int = JWT_EXPIRE_MINUTES):
    to_encode = payload.copy()
    expire = now_argentina() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")


# ====================================================
# ☁️ CONFIGURACIÓN CLOUDINARY / FIREBASE
# ====================================================
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/firebase.messaging"]
)

def get_access_token():
    request = google_requests.Request()
    credentials.refresh(request)
    return credentials.token


# ====================================================
# 🚀 CREAR APP FASTAPI
# ====================================================
app = FastAPI(title="DocYa API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ====================================================
# 🧩 CONEXIÓN BASE DE DATOS
# ====================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


# ====================================================
# 🩺 INCLUIR RUTA DE MONITOREO
# ====================================================
from monitoreo import router as monitoreo_router
app.include_router(monitoreo_router)


# ====================================================
# 🔑 MODELOS Pydantic (Auth y Valoraciones)
# ====================================================
class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    dni: Optional[str] = None
    telefono: Optional[str] = None
    pais: Optional[str] = None
    provincia: Optional[str] = None
    localidad: Optional[str] = None
    fecha_nacimiento: Optional[date] = None
    acepto_condiciones: bool = False

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class GoogleIn(BaseModel):
    id_token: str

class UserOut(BaseModel):
    id: str
    full_name: str

class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

class ValoracionIn(BaseModel):
    paciente_uuid: str
    medico_id: Optional[int] = None
    enfermero_id: Optional[int] = None
    puntaje: int
    comentario: Optional[str] = None

class PagoIn(BaseModel):
    monto: float
    paciente_uuid: str
    tipo: str  # "medico" o "enfermero"
    lat: float
    lng: float
    descripcion: str = "Consulta a domicilio"

# ====================================================
# 🩻 ENDPOINTS BASE / AUTH / USERS
# ====================================================
@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}


@app.post("/auth/register")
def register(data: RegisterIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    password_hash = pwd_context.hash(data.password)

    # 👉 Convertir nombre a formato capitalizado (primera letra de cada palabra en mayúscula)
    full_name = data.full_name.strip().title()

    try:
        cur.execute("""
            INSERT INTO users (
                email, full_name, password_hash,
                dni, telefono, pais, provincia, localidad, fecha_nacimiento,
                acepto_condiciones, fecha_aceptacion, version_texto, validado, role
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s)
            RETURNING id, full_name
        """, (
            data.email.lower(), full_name, password_hash,
            data.dni, data.telefono, data.pais, data.provincia, data.localidad,
            data.fecha_nacimiento, data.acepto_condiciones,
            now_argentina() if data.acepto_condiciones else None,
            "v1.0", "patient"
        ))
        user_id, full_name = cur.fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno en registro: {e}")

    try:
        enviar_email_validacion_paciente(data.email.lower(), user_id, full_name)
    except Exception as e:
        print("⚠️ Error enviando email validación paciente:", e)

    return {
        "ok": True,
        "mensaje": "✅ Registro exitoso. Revisa tu correo para activar la cuenta.",
        "user_id": str(user_id),
        "full_name": full_name,
        "role": "patient"
    }



@app.get("/users/{user_id}")
def get_user_by_id(user_id: str, db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        meses = 0
        if user.get("created_at"):
            try:
                meses = (datetime.utcnow() - user["created_at"]).days // 30
            except Exception:
                meses = 0

        cur.execute(
            "SELECT COUNT(*) AS total FROM consultas WHERE paciente_uuid = %s",
            (user_id,),
        )
        consultas = cur.fetchone()
        total_consultas = consultas["total"] if consultas else 0

        user["consultas_count"] = total_consultas
        user["meses_en_docya"] = meses

        cur.close()
        return user

    except Exception as e:
        print(f"⚠️ Error en get_user_by_id: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ====================================================
# 📸 ENDPOINT: Subir foto de perfil del paciente
# ====================================================
@app.post("/users/{user_id}/foto")
async def subir_foto_paciente(user_id: str, file: UploadFile = File(...), db=Depends(get_db)):
    try:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo debe ser una imagen válida")

        # 📤 Subir imagen a Cloudinary
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

        # 💾 Actualizar en la base de datos
        cur = db.cursor()
        cur.execute("UPDATE users SET foto_url = %s WHERE id = %s", (foto_url, user_id))
        db.commit()
        cur.close()

        return {"foto_url": foto_url, "message": "Foto actualizada correctamente"}

    except Exception as e:
        print(f"⚠️ Error al subir foto de paciente {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al subir la foto: {str(e)}")

# ====================================================
# 🟢 MÉDICOS CONECTADOS (ENDPOINT)
# ====================================================
@app.get("/auth/medicos_online")
def medicos_online():
    print(f"🩺 Médicos conectados actualmente: {len(active_medicos)} → {list(active_medicos.keys())}")
    return {
        "total": len(active_medicos),
        "ids": list(active_medicos.keys())
    }

# =========================================================
# 🔐 Hash de contraseñas (DocYa Pro)
# =========================================================
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    """
    Encripta la contraseña usando bcrypt.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifica si la contraseña ingresada coincide con el hash guardado.
    """
    return pwd_context.verify(plain_password, hashed_password)

# --- Activación paciente ---
@app.get("/auth/activar_paciente", response_class=HTMLResponse)
def activar_paciente(token: str, request: Request, db=Depends(get_db)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        user_id = str(payload.get("sub"))  # 👈 lo dejamos como string/UUID
        cur = db.cursor()
        cur.execute("UPDATE users SET validado=TRUE WHERE id=%s RETURNING id, full_name", (user_id,))
        row = cur.fetchone(); db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        return templates.TemplateResponse("activar_paciente.html", {"request": request, "nombre": row[1]})
    except jwt.ExpiredSignatureError:
        return HTMLResponse("<h1>⚠️ El enlace de activación expiró</h1>", status_code=400)
    except Exception as e:
        return HTMLResponse(f"<h1>⚠️ Token inválido</h1><p>{e}</p>", status_code=400)

        
def enviar_email_validacion_paciente(email: str, user_id: int, full_name: str):
    token = create_access_token(
        {"sub": str(user_id), "email": email, "tipo": "validacion_paciente"},
        expires_minutes=60*24
    )
    link_activacion = f"https://docya-railway-production.up.railway.app/auth/activar_paciente?token={token}"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Activación DocYa</title>
    </head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr>
          <td align="center">
            <table border="0" cellpadding="0" cellspacing="0" width="600" style="background:#ffffff; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
              <tr>
                <td align="center" style="padding:30px 20px;">
                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" alt="DocYa" style="max-width:180px; margin-bottom:20px;">
                  <h2 style="color:#00A8A8; font-size:22px; margin:0 0 15px;">¡Bienvenido a DocYa, {full_name}!</h2>
                  <p style="color:#333333; font-size:15px; line-height:1.5; margin:0 0 25px;">
                    Gracias por registrarte en nuestra aplicación de salud a domicilio.<br>
                    Antes de comenzar, confirma tu correo electrónico para activar tu cuenta de paciente.
                  </p>
                  <a href="{link_activacion}" target="_blank"
                     style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; 
                            border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">
                    ✅ Activar mi cuenta
                  </a>
                  <p style="color:#777777; font-size:12px; margin-top:30px;">
                    Si no solicitaste este registro, por favor ignora este correo.
                  </p>
                </td>
              </tr>
            </table>
            <p style="color:#999999; font-size:11px; margin-top:20px;">
              © {datetime.now().year} DocYa · Atención médica a domicilio con confianza.
            </p>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa"},
        subject="Activa tu cuenta en DocYa",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Correo enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email con Brevo API: {e}")




@app.post("/auth/login", response_model=AuthResponse)
def login(data: LoginIn, db=Depends(get_db)):
    cur = db.cursor()

    input_value = data.email.strip().lower()   # puede ser email o DNI
    password = data.password.strip()

    # Buscar al usuario por EMAIL o DNI
    cur.execute("""
        SELECT id, full_name, password_hash, role, validado, email, dni
        FROM users
        WHERE lower(email) = %s OR lower(trim(dni)) = %s
        LIMIT 1
    """, (input_value, input_value))

    row = cur.fetchone()

    # Usuario inexistente
    if not row:
        raise HTTPException(status_code=400, detail="Usuario no encontrado")

    user_id, full_name, password_hash, role, validado, email, dni = row

    # Contraseña incorrecta
    if not pwd_context.verify(password, password_hash):
        raise HTTPException(status_code=400, detail="Contraseña incorrecta")

    # Bloquear si NO validó email
    if not validado:
        raise HTTPException(
            status_code=403,
            detail="Debes validar tu correo electrónico para iniciar sesión."
        )

    # Generar token
    token = create_access_token({
        "sub": str(user_id),
        "email": email,
        "role": role,
    })

    # Respuesta profesional y completa
    return {
        "access_token": token,
        "token_type": "bearer",
    
        # 👇 COMPATIBILIDAD con tu Flutter actual (LO QUE NECESITAS)
        "user_id": str(user_id),
        "full_name": full_name,
    
        # 👇 Nuevo formato mejorado (queda para futuro)
        "user": {
            "id": str(user_id),
            "full_name": full_name,
            "email": email,
            "dni": dni,
            "role": role,
            "validado": True
        }
    }



# =========================================================
# =========================================================
# 🔐 JWT CONFIG GLOBAL (UNIFICADA)
# =========================================================
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException

JWT_SECRET = os.getenv("JWT_SECRET", "docya_secret_key")  # 👈 misma clave para crear y verificar
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))

def create_access_token(payload: dict, expires_minutes: int = TOKEN_EXPIRE_MINUTES):
    """Genera un JWT con expiración"""
    to_encode = payload.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)

def verify_token(token: str):
    """Verifica y decodifica el JWT. Lanza HTTPException si no es válido."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        exp = payload.get("exp")
        if exp and datetime.fromtimestamp(exp, tz=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expirado")
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")



# ====================================================
# 👨‍⚕️ MÉDICOS (Rutas originales bajo /auth)
# ====================================================

class RegisterMedicoIn(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    matricula: str
    especialidad: str
    tipo: str = "medico"   # 👈 nuevo campo
    telefono: Optional[str] = None
    provincia: Optional[str] = None
    localidad: Optional[str] = None
    dni: Optional[str] = None
    foto_perfil: Optional[str] = None
    foto_dni_frente: Optional[str] = None
    foto_dni_dorso: Optional[str] = None
    selfie_dni: Optional[str] = None


@app.post("/auth/register_medico")
def register_medico(data: RegisterMedicoIn, db=Depends(get_db)):
    cur = db.cursor()

    # 🔍 Validar email y matrícula únicos
    cur.execute("SELECT id FROM medicos WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    cur.execute("SELECT id FROM medicos WHERE matricula=%s", (data.matricula,))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    password_hash = pwd_context.hash(data.password)

    # ✨ Formatear nombre y tipo antes de guardar
    full_name = data.full_name.strip().title()
    tipo_normalizado = unidecode(data.tipo.strip().lower())

    # 🧠 Insertar el médico sin las fotos
    cur.execute("""
        INSERT INTO medicos (
            full_name, email, password_hash, matricula, especialidad, tipo, telefono,
            provincia, localidad, dni, validado
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
        RETURNING id, full_name, tipo
    """, (
        full_name, data.email.lower(), password_hash,
        data.matricula, data.especialidad, tipo_normalizado, data.telefono,
        data.provincia, data.localidad, data.dni
    ))

    medico_id, full_name, tipo = cur.fetchone()
    db.commit()

    # ☁️ Subir imágenes a Cloudinary
    def subir_a_cloudinary(imagen_base64, carpeta):
        if not imagen_base64:
            return None
        try:
            if imagen_base64.startswith("data:image"):
                res = cloudinary.uploader.upload(
                    imagen_base64,
                    folder=f"docya/medicos/{medico_id}",
                    public_id=f"{carpeta}",
                    overwrite=True,
                    resource_type="image"
                )
                return res["secure_url"]
            elif imagen_base64.startswith("http"):
                return imagen_base64
        except Exception as e:
            print(f"⚠️ Error subiendo {carpeta}: {e}")
        return None

    foto_perfil_url = subir_a_cloudinary(data.foto_perfil, "perfil")
    foto_dni_frente_url = subir_a_cloudinary(data.foto_dni_frente, "dni_frente")
    foto_dni_dorso_url = subir_a_cloudinary(data.foto_dni_dorso, "dni_dorso")
    selfie_dni_url = subir_a_cloudinary(data.selfie_dni, "selfie_dni")

    # 🧾 Actualizar URLs de fotos
    cur.execute("""
        UPDATE medicos
        SET foto_perfil=%s,
            foto_dni_frente=%s,
            foto_dni_dorso=%s,
            selfie_dni=%s
        WHERE id=%s
    """, (
        foto_perfil_url, foto_dni_frente_url, foto_dni_dorso_url, selfie_dni_url, medico_id
    ))
    db.commit()

    # 📧 Enviar mail de validación
    try:
        enviar_email_validacion(data.email.lower(), medico_id, full_name)
    except Exception as e:
        print(f"⚠️ Error enviando email validación: {e}")

    return {
        "ok": True,
        "mensaje": f"Registro exitoso como {tipo}. ✅ Revisa tu correo para activar tu cuenta.",
        "medico_id": medico_id,
        "full_name": full_name,
        "tipo": tipo,
        "fotos": {
            "perfil": foto_perfil_url,
            "dni_frente": foto_dni_frente_url,
            "dni_dorso": foto_dni_dorso_url,
            "selfie_dni": selfie_dni_url
        }
    }



@app.get("/auth/activar_medico", response_class=HTMLResponse)
def activar_medico(token: str, request: Request, db=Depends(get_db)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        medico_id = int(payload.get("sub"))
        cur = db.cursor()
        cur.execute("UPDATE medicos SET validado=TRUE, updated_at=NOW() WHERE id=%s RETURNING id, full_name", (medico_id,))
        row = cur.fetchone(); db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Médico no encontrado")
        return templates.TemplateResponse("activar_medico.html", {"request": request, "nombre": row[1]})
    except jwt.ExpiredSignatureError:
        return HTMLResponse("<h1>⚠️ El enlace de activación expiró</h1>", status_code=400)
    except Exception as e:
        return HTMLResponse(f"<h1>⚠️ Token inválido</h1><p>{e}</p>", status_code=400)


from sib_api_v3_sdk import SendSmtpEmail

#envio de mensaje de validacion por correo medico
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from sib_api_v3_sdk import SendSmtpEmail
from datetime import datetime
import os

def enviar_email_validacion(email: str, medico_id: int, full_name: str):
    token = create_access_token(
        {"sub": str(medico_id), "email": email, "tipo": "validacion"},
        expires_minutes=60*24
    )
    link_activacion = f"https://docya-railway-production.up.railway.app/auth/activar_medico?token={token}"

    # HTML seguro y compatible con correos
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Activación DocYa</title>
    </head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr>
          <td align="center">
            <table border="0" cellpadding="0" cellspacing="0" width="600" style="background:#ffffff; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
              <tr>
                <td align="center" style="padding:30px 20px;">
                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" alt="DocYa" style="max-width:180px; margin-bottom:20px;">
                  <h2 style="color:#00A8A8; font-size:22px; margin:0 0 15px;">¡Bienvenido al equipo DocYa, {full_name}!</h2>
                  <p style="color:#333333; font-size:15px; line-height:1.5; margin:0 0 25px;">
                    Gracias por unirte a nuestra red de profesionales de la salud.<br>
                    Antes de comenzar, confirma tu correo electrónico para activar tu cuenta.
                  </p>
                  <a href="{link_activacion}" target="_blank"
                     style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; 
                            border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">
                    ✅ Activar mi cuenta
                  </a>
                  <p style="color:#777777; font-size:12px; margin-top:30px;">
                    Si no solicitaste este registro, por favor ignora este correo.
                  </p>
                </td>
              </tr>
            </table>
            <p style="color:#999999; font-size:11px; margin-top:20px;">
              © {datetime.now().year} DocYa · Atención médica a domicilio con confianza.
            </p>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    # Configuración Brevo API
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "soporte@docya-railway-production.up.railway.app", "name": "DocYa"},  # 👈 mejor usar dominio verificado
        subject="Activa tu cuenta en DocYa",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Correo enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email con Brevo API: {e}")





    # Configuración Brevo API
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")  # 👈 clave en variable de entorno

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa"},
        subject="Activa tu cuenta en DocYa",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Correo enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email con Brevo API: {e}")


class LoginMedicoIn(BaseModel):
    email: str   # 👈 antes era EmailStr
    password: str

@app.post("/auth/login_medico")
def login_medico(data: LoginMedicoIn, db=Depends(get_db)):
    cur = db.cursor()

    input_value = data.email.strip().lower()
    password = data.password.strip()

    # Buscar por email o DNI
    cur.execute("""
        SELECT id, full_name, password_hash, validado, tipo, email, dni, matricula_validada
        FROM medicos
        WHERE lower(email) = %s OR trim(lower(dni)) = %s
    """, (input_value, input_value))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Usuario no encontrado")

    if not pwd_context.verify(password, row[2]):
        raise HTTPException(status_code=400, detail="Contraseña incorrecta")

    # ⚠️ Email aún no validado
    if not row[3]:
        raise HTTPException(status_code=403, detail="Cuenta aún no validada por correo")

    # ⚠️ Matrícula aún no validada por el administrador
    if not row[7]:
        raise HTTPException(status_code=403, detail="Matrícula aún no validada por el equipo DocYa")

    # ✅ Generar token si todo está bien
    token = create_access_token({
        "sub": str(row[0]),
        "email": row[5],
        "role": row[4],
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "medico_id": row[0],
        "full_name": row[1],
        "tipo": row[4],
        "email": row[5],
        "dni": row[6],
        "matricula_validada": row[7],
        "medico": {
            "id": row[0],
            "full_name": row[1],
            "validado": True,
            "tipo": row[4],
            "email": row[5],
            "dni": row[6],
            "matricula_validada": row[7]
        }
    }


@app.post("/auth/validar_medico/{medico_id}")
def validar_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET validado=TRUE, updated_at=NOW() 
        WHERE id=%s 
        RETURNING id, full_name, tipo
    """, (medico_id,))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": row[0], "nombre": row[1], "tipo": row[2]}


@app.post("/auth/medico/{medico_id}/foto")
def actualizar_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    try:
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="docya/medicos",
            public_id=f"medico_{medico_id}",
            overwrite=True
        )
        foto_url = upload_result["secure_url"]
        cur = db.cursor()
        cur.execute("""
            UPDATE medicos 
            SET foto_perfil=%s, updated_at=NOW() 
            WHERE id=%s 
            RETURNING id,foto_perfil
        """, (foto_url, medico_id))
        row = cur.fetchone(); db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")
        return {"ok": True, "medico_id": row[0], "foto_url": row[1]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo foto: {e}")



class AliasIn(BaseModel):
    alias: str

class FcmTokenIn(BaseModel):
    fcm_token: str
@app.patch("/auth/medico/{medico_id}/alias")
def actualizar_alias(medico_id: int, data: AliasIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET alias_cbu=%s, updated_at=NOW() 
        WHERE id=%s 
        RETURNING id,alias_cbu
    """, (data.alias, medico_id))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": medico_id, "alias": row[1]}


@app.post("/auth/medico/{medico_id}/disponibilidad")
def actualizar_disponibilidad(medico_id: int, disponible: bool, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        UPDATE medicos 
        SET disponible=%s 
        WHERE id=%s 
        RETURNING id,disponible
    """, (disponible, medico_id))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": medico_id, "disponible": row["disponible"]}


from datetime import date, timedelta
from fastapi import HTTPException, Depends

@app.get("/auth/medico/{medico_id}/stats")
def medico_stats(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # 1️⃣ Tipo del profesional
    cur.execute("SELECT tipo FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    tipo = row[0]

    # 2️⃣ Semana actual
    inicio_semana = date.today() - timedelta(days=date.today().weekday())
    fin_semana = inicio_semana + timedelta(days=6)

    # 3️⃣ Consultas finalizadas esta semana
    cur.execute("""
        SELECT COUNT(*) 
        FROM consultas
        WHERE medico_id = %s
          AND estado = 'finalizada'
          AND DATE_TRUNC('week', creado_en) = DATE_TRUNC('week', CURRENT_DATE)
    """, (medico_id,))
    consultas = cur.fetchone()[0] or 0

    # 4️⃣ Tarifa
    tarifa = 24000 if tipo == "medico" else 15000
    ganancias = consultas * tarifa

    # 5️⃣ Pagos reales por método
    cur.execute("""
        SELECT 
            COALESCE(metodo_pago, 'efectivo') AS metodo_pago,
            COUNT(*) AS cantidad,
            COALESCE(SUM(medico_neto), 0) AS total
        FROM pagos_consulta
        WHERE medico_id = %s
          AND DATE_TRUNC('week', fecha) = DATE_TRUNC('week', CURRENT_DATE)
        GROUP BY metodo_pago;
    """, (medico_id,))
    pagos = cur.fetchall()
    detalle_pagos = {row[0]: {"cantidad": int(row[1]), "monto": float(row[2])} for row in pagos}

    # 6️⃣ Si no hay pagos registrados pero hay consultas → asumir "efectivo"
    if not detalle_pagos and consultas > 0:
        detalle_pagos = {"efectivo": {"cantidad": consultas, "monto": float(ganancias)}}

    db.close()

    # 7️⃣ Respuesta final
    return {
        "consultas": int(consultas),
        "ganancias": int(ganancias),
        "tipo": tipo,
        "periodo": f"{inicio_semana} → {fin_semana}",
        "detalle_pagos": detalle_pagos
    }



@app.post("/auth/medico/{medico_id}/fcm_token")
def actualizar_fcm_token(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET fcm_token=%s, updated_at=NOW() 
        WHERE id=%s 
        RETURNING id
    """, (data.fcm_token, medico_id))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": medico_id, "fcm_token": data.fcm_token}


@app.get("/auth/medico/{medico_id}")
def obtener_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, full_name, email, especialidad, telefono, 
               alias_cbu, matricula, foto_perfil, tipo, firma_url
        FROM medicos 
        WHERE id=%s
    """, (medico_id,))
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
        "firma_url": row[9],  # ✅ ahora sí existe
    }


# ====================================================
# 📋 CONSULTAS (todas las rutas originales)
# ====================================================

# --- Modelo para solicitar consulta ---
class SolicitarConsultaIn(BaseModel):
    paciente_uuid: UUID
    motivo: str
    direccion: str
    lat: float
    lng: float
    tipo: str = "medico"   # 👈 puede ser "medico" o "enfermero"
    metodo_pago: str       # 👈 'efectivo', 'debito', 'credito'


@app.post("/consultas/solicitar")
async def solicitar_consulta(data: SolicitarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()

    # ----------------------------------------------------
    # 🔍 1) Buscar profesional más cercano disponible
    # ----------------------------------------------------
    cur.execute("""
        SELECT id, full_name, latitud, longitud, tipo,
        (6371 * acos(
            cos(radians(%s)) * cos(radians(latitud)) *
            cos(radians(longitud) - radians(%s)) +
            sin(radians(%s)) * sin(radians(latitud))
        )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
          AND tipo = %s
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
          AND (
                (6371 * acos(
                    cos(radians(%s)) * cos(radians(latitud)) *
                    cos(radians(longitud) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(latitud))
                )) <= 10
          )
        ORDER BY distancia ASC
        LIMIT 1
    """, (
        data.lat, data.lng, data.lat,
        data.tipo,
        data.lat, data.lng, data.lat
    ))

    row = cur.fetchone()

    # 🟡 2) No hay profesionales → Crear consulta pendiente
    # ----------------------------------------------------
    if not row:
        cur.execute("""
            INSERT INTO consultas (
                paciente_uuid, medico_id, estado, motivo,
                direccion, lat, lng, metodo_pago, tipo
            )
            VALUES (%s, NULL, 'pendiente', %s, %s, %s, %s, %s, %s)
            RETURNING id, creado_en
        """, (
            str(data.paciente_uuid),
            data.motivo,
            data.direccion,
            data.lat,
            data.lng,
            data.metodo_pago,
            data.tipo
        ))
    
        consulta_id, creado_en = cur.fetchone()
        db.commit()
    
        print(f"⚠️ No hay {data.tipo}s disponibles → consulta creada sin asignar")
    
        # ❌ NO ENVIAR PUSH A NADIE
        # ❌ NO AVISAR POR WS
        # Solo dejar la consulta como pendiente.
    
        return {
            "consulta_id": consulta_id,
            "estado": "pendiente",
            "mensaje": f"Consulta registrada. Aún no hay {data.tipo}s disponibles.",
            "profesional": None,
            "creado_en": str(creado_en)
        }



    # ----------------------------------------------------
    # 🟢 3) Sí hay profesional → Asignar normal
    # ----------------------------------------------------
    profesional_id, profesional_nombre, profesional_lat, profesional_lng, tipo, distancia = row

    cur.execute("""
        INSERT INTO consultas (
            paciente_uuid, medico_id, estado, motivo,
            direccion, lat, lng, metodo_pago
        )
        VALUES (%s,%s,'pendiente',%s,%s,%s,%s,%s)
        RETURNING id, creado_en
    """, (
        str(data.paciente_uuid),
        profesional_id,
        data.motivo,
        data.direccion,
        data.lat,
        data.lng,
        data.metodo_pago
    ))

    consulta_id, creado_en = cur.fetchone()
    db.commit()

    print(f"🟢 Consulta {consulta_id} asignada al médico {profesional_id}")

    # ----------------------------------------------------
    # 🔔 WS en tiempo real con todos los datos completos
    # ----------------------------------------------------
    if profesional_id in active_medicos:
        try:
            # Obtener datos del paciente
            cur.execute("""
                SELECT full_name, telefono 
                FROM users
                WHERE id = %s
            """, (str(data.paciente_uuid),))
            user_row = cur.fetchone()
            paciente_nombre = user_row[0] if user_row else "Paciente"
            paciente_telefono = user_row[1] if user_row else "Sin número"
    
            await active_medicos[profesional_id].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id,
                "paciente_uuid": str(data.paciente_uuid),
                "paciente_nombre": paciente_nombre,
                "paciente_telefono": paciente_telefono,
                "motivo": data.motivo,
                "direccion": data.direccion,
                "lat": data.lat,
                "lng": data.lng,
                "distancia_km": round(float(distancia), 2),
                "metodo_pago": data.metodo_pago,
                "profesional_tipo": tipo,
                "creado_en": str(creado_en)
            })
            print(f"📤 WS enviado al médico {profesional_id} con datos completos")
        except Exception as e:
            print(f"⚠️ Error WS médico {profesional_id}: {e}")


    # ----------------------------------------------------
    # 🔔 5) Enviar Push FCM
    # ----------------------------------------------------
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (profesional_id,))
    row = cur.fetchone()

    if row and row[0]:
        try:
            enviar_push(
                row[0],
                "📢 Nueva consulta",
                f"{data.motivo}",
                {
                    "tipo": "consulta_nueva",
                    "consulta_id": str(consulta_id),
                    "medico_id": str(profesional_id),
                    "profesional_tipo": tipo,
                    "metodo_pago": data.metodo_pago
                }
            )
            print(f"📤 Push enviado a médico {profesional_id}")
        except Exception as e:
            print(f"⚠️ Error enviando push: {e}")
    else:
        print(f"⚠️ Médico {profesional_id} no tiene FCM token registrado")

    # ----------------------------------------------------
    # 🔙 6) Respuesta a la app del paciente
    # ----------------------------------------------------
    return {
        "consulta_id": consulta_id,
        "paciente_uuid": str(data.paciente_uuid),
        "profesional": {
            "id": profesional_id,
            "nombre": profesional_nombre,
            "lat": profesional_lat,
            "lng": profesional_lng,
            "tipo": tipo,
            "distancia_km": round(float(distancia), 2)
        },
        "motivo": data.motivo,
        "direccion": data.direccion,
        "metodo_pago": data.metodo_pago,
        "estado": "pendiente",
        "creado_en": str(creado_en)
    }





#historial consultas medico 
@app.get("/consultas/historial_medico/{medico_id}")
def historial_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.estado, c.motivo, c.direccion, c.creado_en,
               COALESCE(u.full_name, 'Paciente') as paciente_nombre
        FROM consultas c
        LEFT JOIN users u ON c.paciente_uuid = u.id
        WHERE c.medico_id = %s
        ORDER BY c.creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()

    return [
        {
            "id": r[0],
            "estado": r[1],
            "motivo": r[2],
            "direccion": r[3],
            "creado_en": format_datetime_arg(r[4]),   # 👈
            "paciente_nombre": r[5]
        }
        for r in rows
    ]

# --- Consultas del médico ---
@app.get("/consultas/mias/{medico_id}")
def consultas_mias(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id,
               c.paciente_uuid,
               COALESCE(u.full_name, 'Paciente') AS paciente_nombre,
               COALESCE(u.telefono, 'Sin número') AS paciente_telefono,
               c.estado, c.motivo, c.direccion, c.creado_en
        FROM consultas c
        LEFT JOIN users u ON c.paciente_uuid = u.id
        WHERE c.medico_id = %s
          AND c.estado IN ('pendiente','aceptada')
        ORDER BY c.creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "paciente_uuid": str(r[1]),
            "paciente_nombre": r[2],
            "paciente_telefono": r[3],
            "estado": r[4],
            "motivo": r[5],
            "direccion": r[6],
            "creado_en": format_datetime_arg(r[7])
        }
        for r in rows
    ]

# --- Consulta asignada ---
GOOGLE_API_KEY = "AIzaSyB5sBLD81Hg3MRIggPhqL1a_57tjOo7vAk"  # 🔒 Reemplazá con tu API Key real de Google Cloud


@app.get("/consultas/asignadas/{medico_id}")
def consultas_asignadas(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.paciente_uuid,
               COALESCE(u.full_name, 'Paciente') AS paciente_nombre,
               COALESCE(u.telefono, 'Sin número') AS paciente_telefono,
               c.motivo, c.direccion, c.lat, c.lng, c.estado,
               m.latitud, m.longitud
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        LEFT JOIN users u ON c.paciente_uuid = u.id
        WHERE c.medico_id = %s
          AND c.estado = 'pendiente'
        ORDER BY c.creado_en DESC
        LIMIT 1
    """, (medico_id,))
    row = cur.fetchone()
    if not row:
        return {"consulta": None}

    (consulta_id, paciente_uuid, paciente_nombre, paciente_telefono,
     motivo, direccion, lat, lng, estado, med_lat, med_lng) = row

    distancia_km = None
    tiempo_min = None

    try:
        if all(v is not None for v in [lat, lng, med_lat, med_lng]):
            lat, lng, med_lat, med_lng = float(lat), float(lng), float(med_lat), float(med_lng)

            # 🌍 Google Directions API
            directions_url = (
                f"https://maps.googleapis.com/maps/api/directions/json?"
                f"origin={med_lat},{med_lng}&destination={lat},{lng}"
                f"&mode=driving&departure_time=now&traffic_model=best_guess&units=metric&key={GOOGLE_API_KEY}"
            )
            resp = requests.get(directions_url)
            data = resp.json()

            if data.get("status") == "OK":
                leg = data["routes"][0]["legs"][0]
                distancia_km = leg["distance"]["value"] / 1000
                tiempo_min = (     leg.get("duration_in_traffic", leg["duration"])["value"] / 60 )
            else:
                print("⚠️ Error Google Directions:", data.get("status"))

    except Exception as e:
        print("❌ Error cálculo distancia:", e)

    return {
        "id": consulta_id,
        "paciente_uuid": str(paciente_uuid),
        "paciente_nombre": paciente_nombre,
        "paciente_telefono": paciente_telefono,
        "motivo": motivo,
        "direccion": direccion,
        "lat": lat,
        "lng": lng,
        "estado": estado,
        "distancia_km": round(distancia_km, 2) if distancia_km else None,
        "tiempo_estimado_min": int(round(tiempo_min)) if tiempo_min else None
    }


# --- Aceptar / Rechazar / En camino / Llegó / Finalizar ---
@app.post("/consultas/{consulta_id}/iniciar")
def iniciar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, estado, medico_id FROM consultas WHERE id=%s", (consulta_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    consulta_id, estado, medico_id = row
    if estado != "en_domicilio":
        cur.execute(
            """
            UPDATE consultas
            SET estado = 'en_domicilio',
                inicio_atencion = NOW()
            WHERE id = %s
            RETURNING estado, inicio_atencion
            """,
            (consulta_id,)
        )
        new_estado, inicio = cur.fetchone()

        # 🔥 marcar al médico como ocupado
        if medico_id:
            cur.execute(
                """
                UPDATE medicos
                SET disponible = FALSE
                WHERE id = %s
                """,
                (medico_id,)
            )

        db.commit()
    else:
        new_estado, inicio = estado, None

    return {
        "msg": "Consulta iniciada",
        "consulta_id": consulta_id,
        "estado": new_estado,
        "inicio_atencion": inicio
    }

class MedicoAccion(BaseModel):
    medico_id: int

@app.post("/consultas/{consulta_id}/aceptar")
def aceptar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    import httpx  # aseguramos import local si no está arriba

    medico_id = data.medico_id

    cur = db.cursor()

    # 1) Traemos el payment_id de la consulta
    cur.execute("SELECT payment_id FROM consultas WHERE id = %s", (consulta_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    payment_id = row[0]  # puede ser None si es efectivo

    # 2) Marcar la consulta como aceptada (solo si estaba pendiente)
    cur.execute("""
        UPDATE consultas
        SET estado = 'aceptada', medico_id = %s
        WHERE id = %s AND estado = 'pendiente'
        RETURNING id
    """, (medico_id, consulta_id))
    updated = cur.fetchone()

    if not updated:
        raise HTTPException(status_code=400, detail="Consulta no disponible")

    # 3) Marcar médico como ocupado
    cur.execute("UPDATE medicos SET disponible = false WHERE id = %s", (medico_id,))
    db.commit()

    # 4) Si la consulta tiene pago con tarjeta → CAPTURAMOS EL PAGO
    if payment_id:
        try:
            url = f"https://api.mercadopago.com/v1/payments/{payment_id}/capture"

            headers = {
                "Authorization": f"Bearer {MERCADO_PAGO_TOKEN}",
                "Content-Type": "application/json"
            }

            with httpx.Client() as client:
                r = client.post(url, headers=headers, json={})

            if r.status_code not in (200, 201):
                print("⚠ Error capturando pago MercadoPago:", r.text)

        except Exception as e:
            print("⚠ Excepción capturando pago:", e)

    return {"ok": True, "consulta_id": consulta_id}


@app.post("/consultas/{consulta_id}/rechazar")
def rechazar_consulta(consulta_id: int, data: dict, db=Depends(get_db)):
    """
    Cuando un médico rechaza una consulta:
    - Se marca como disponible nuevamente.
    - La consulta vuelve a estado 'pendiente'.
    - Se reasigna automáticamente al médico disponible más cercano.
    """
    cur = db.cursor()

    medico_id = int(data.get("medico_id"))

    # 1️⃣ Dejar al médico disponible otra vez
    cur.execute("UPDATE medicos SET disponible = TRUE WHERE id = %s", (medico_id,))

    # 2️⃣ Obtener ubicación del paciente (lat/lng) de la consulta
    cur.execute("SELECT lat, lng FROM consultas WHERE id = %s", (consulta_id,))
    pos = cur.fetchone()
    if not pos or pos[0] is None or pos[1] is None:
        db.commit()
        return {"ok": True, "mensaje": "Consulta sin ubicación para reasignar"}

    paciente_lat, paciente_lng = float(pos[0]), float(pos[1])

    # 3️⃣ Buscar otro médico disponible más cercano
    cur.execute("""
        SELECT id, latitud, longitud,
            (6371 * acos(
                cos(radians(%s)) * cos(radians(latitud)) *
                cos(radians(longitud) - radians(%s)) +
                sin(radians(%s)) * sin(radians(latitud))
            )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
        ORDER BY distancia ASC
        LIMIT 1
    """, (paciente_lat, paciente_lng, paciente_lat))

    nuevo = cur.fetchone()
    if not nuevo:
        # 4️⃣ Si no hay médicos disponibles, dejar pendiente
        cur.execute(
            "UPDATE consultas SET estado = 'pendiente', medico_id = NULL WHERE id = %s",
            (consulta_id,),
        )
        db.commit()
        return {"ok": True, "mensaje": "Consulta pendiente, sin médicos disponibles"}

    nuevo_medico_id, nuevo_lat, nuevo_lng, distancia = nuevo

    # 5️⃣ Reasignar la consulta al nuevo médico
    cur.execute("""
        UPDATE consultas
        SET medico_id = %s, estado = 'pendiente'
        WHERE id = %s
    """, (nuevo_medico_id, consulta_id))

    db.commit()

    print(f"🔄 Consulta {consulta_id} reasignada al médico {nuevo_medico_id} ({distancia:.2f} km)")

    return {
        "ok": True,
        "mensaje": f"Consulta {consulta_id} reasignada al médico {nuevo_medico_id}",
        "nuevo_medico_id": nuevo_medico_id,
        "distancia_km": round(distancia, 2),
    }


@app.post("/consultas/{consulta_id}/encamino")
def medico_encamino(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='en_camino' WHERE id=%s AND medico_id=%s AND estado='aceptada' RETURNING id", (consulta_id, data.medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "en_camino"}

@app.post("/consultas/{consulta_id}/llego")
def medico_llego(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='en_domicilio' WHERE id=%s AND medico_id=%s AND estado='aceptada' RETURNING id", (consulta_id, data.medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "en_domicilio"}

@app.post("/consultas/{consulta_id}/finalizar")
def finalizar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, medico_id FROM consultas WHERE id=%s", (consulta_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    consulta_id, medico_id = row

    # 🔹 Finalizar consulta
    cur.execute(
        """
        UPDATE consultas
        SET estado = 'finalizada',
            fin_atencion = NOW()
        WHERE id = %s
        RETURNING estado, fin_atencion
        """,
        (consulta_id,)
    )
    new_estado, fin = cur.fetchone()

    # 🔹 Liberar médico para que vuelva a estar disponible
    if medico_id:
        cur.execute(
            """
            UPDATE medicos
            SET disponible = TRUE
            WHERE id = %s
            """,
            (medico_id,)
        )

    db.commit()

    return {
        "msg": "Consulta finalizada",
        "consulta_id": consulta_id,
        "estado": new_estado,
        "fin_atencion": fin
    }

@app.get("/pacientes/{paciente_uuid}/historia_clinica")
def historia_clinica(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id AS consulta_id,
               c.creado_en AS fecha_consulta,
               c.motivo,
               c.estado,
               m.full_name AS medico,
               n.contenido AS historia_clinica,
               n.creado_en AS fecha_nota
        FROM consultas c
        LEFT JOIN notas_medicas n ON c.id = n.consulta_id
        LEFT JOIN medicos m ON c.medico_id = m.id
        WHERE c.paciente_uuid = %s
        ORDER BY c.creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()

    return [
        {
            "consulta_id": r[0],
            "fecha_consulta": format_datetime_arg(r[1]),
            "motivo": r[2],
            "estado": r[3],
            "medico": r[4],
            "historia_clinica": r[5],
            "fecha_nota": format_datetime_arg(r[6]) if r[6] else None
        }
        for r in rows
    ]


# --- Certificados ---

# --- CERTIFICADOS MÉDICOS DOCYA ---
from fastapi import Depends, Response, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from weasyprint import HTML, CSS
import tempfile, os

# ✅ Modelo de entrada
class CertificadoIn(BaseModel):
    medico_id: int
    paciente_uuid: str
    diagnostico: str
    reposo_dias: int
    observaciones: str | None = None

# ------------------------------------------------------------
# 🚀 CREAR CERTIFICADO MÉDICO (POST)
# ------------------------------------------------------------
@app.post("/consultas/{consulta_id}/certificado")
def crear_certificado_docya(
    consulta_id: int,
    data: CertificadoIn,
    db = Depends(get_db)
):

    cur = db.cursor()

    # Verificar si la consulta existe
    cur.execute("SELECT id FROM consultas WHERE id = %s", (consulta_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="La consulta no existe")

    # Guardar certificado
    cur.execute("""
        INSERT INTO certificados (consulta_id, medico_id, paciente_uuid, diagnostico, reposo_dias, observaciones)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        consulta_id,
        data.medico_id,
        data.paciente_uuid,
        data.diagnostico,
        data.reposo_dias,
        data.observaciones
    ))

    db.commit()

    certificado_id = cur.fetchone()[0]
    cur.close()

    return {
        "status": "ok",
        "certificado_id": certificado_id,
        "consulta_id": consulta_id
    }

@app.get("/consultas/{consulta_id}/certificado")
def ver_certificado_docya(consulta_id: int, db=Depends(get_db)):
    """
    Genera un certificado médico profesional DocYa,
    con firma digital del médico y validez conforme a la Ley 25.506.
    Solo muestra la sección de certificación.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT c.medico_id, c.paciente_uuid, c.diagnostico, c.reposo_dias, c.observaciones, c.creado_en,
               m.full_name AS medico_nombre, m.matricula, m.especialidad, m.firma_url,
               u.full_name AS paciente_nombre, u.dni
        FROM certificados c
        JOIN medicos m ON c.medico_id = m.id
        JOIN users u ON c.paciente_uuid = u.id::text
        WHERE c.consulta_id = %s
        ORDER BY c.creado_en DESC
        LIMIT 1
    """, (consulta_id,))

    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="❌ No existe certificado para esta consulta")

    (
        medico_id, paciente_uuid, diagnostico, reposo_dias, observaciones, creado_en,
        medico_nombre, matricula, especialidad, firma_url,
        paciente_nombre, paciente_dni
    ) = row

    logo_url = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=https://docya-railway-production.up.railway.app/consultas/{consulta_id}/certificado"
    fecha_emision = creado_en.strftime("%d/%m/%Y %H:%M")

    # 🧾 Redacción formal y válida
    texto_certificacion = f"""
    <p style="text-align:justify;">
      Por medio del presente, <b>certifico que {paciente_nombre}</b>,
      identificado/a con DNI <b>{paciente_dni or '—'}</b>,
      fue evaluado/a en esta fecha, constatándose el siguiente diagnóstico:
      <b>{diagnostico or 'sin diagnóstico especificado'}</b>.
    </p>
    <p style="text-align:justify;">
      Se recomienda reposo por <b>{reposo_dias or '—'}</b> día(s),
      a partir de la fecha del presente certificado,
      debiendo evitar actividades laborales y/o físicas durante dicho período.
    </p>
    """

    if observaciones:
        texto_certificacion += f"""
        <p style="text-align:justify;"><b>Observaciones:</b> {observaciones}</p>
        """

    texto_certificacion += """
    <p style="text-align:justify;">
      Se expide el presente certificado a pedido del/la interesado/a,
      para ser presentado ante quien corresponda.
    </p>
    """

    # 🔏 Firma digital
    firma_html = f"""
    <div class="firma">
      <p><b>Firma digital:</b></p>
      {"<img src='" + firma_url + "' alt='Firma del médico'>" if firma_url else "<p><i>Firma no registrada</i></p>"}
      <p><b>{medico_nombre}</b><br>{especialidad}<br>M.P. {matricula}</p>
      <p style="color:#14B8A6;font-size:13px;">Documento firmado electrónicamente conforme Ley 25.506</p>
    </div>
    """

    # --- HTML completo ---
    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Certificado Médico</title>
      <style>
        body {{
          font-family: 'Helvetica', Arial, sans-serif;
          background-color: #ffffff;
          color: #1f2937;
          padding: 60px 70px;
          line-height: 1.7;
        }}
        .header {{
          display: flex;
          justify-content: space-between;
          align-items: center;
          border-bottom: 3px solid #14B8A6;
          padding-bottom: 10px;
          margin-bottom: 30px;
        }}
        .logo {{
          height: 65px;
        }}
        h1 {{
          color: #14B8A6;
          text-align: center;
          font-size: 26px;
          margin-top: 20px;
          margin-bottom: 40px;
        }}
        .box {{
          border: 1px solid #14B8A6;
          border-radius: 10px;
          background: #f9fdfc;
          padding: 25px 30px;
          margin-bottom: 25px;
        }}
        .firma {{
          margin-top: 60px;
          text-align: right;
        }}
        .firma img {{
          height: 85px;
          margin-bottom: -5px;
        }}
        .qr {{
          text-align: left;
          margin-top: 40px;
        }}
        .qr img {{
          height: 100px;
        }}
        footer {{
          text-align: center;
          color: #6b7280;
          font-size: 12px;
          margin-top: 60px;
        }}
      </style>
    </head>
    <body>
      <div class="header">
        <img src="{logo_url}" class="logo">
        <div style="text-align:right; font-size:13px;">
          <b>Emitido:</b> {fecha_emision}<br>
          <b>ID Consulta:</b> {consulta_id}
        </div>
      </div>

      <h1>CERTIFICADO MÉDICO</h1>

      <div class="box">
        {texto_certificacion}
      </div>

      {firma_html}

      <div class="qr">
        <img src="{qr_url}" alt="QR de verificación"><br>
        <small>Verificar autenticidad:<br>
        docya-railway-production.up.railway.app/consultas/{consulta_id}/certificado</small>
      </div>

      <footer>
        Certificado emitido digitalmente mediante la plataforma DocYa — Ley 25.506 de Firma Digital<br>
        © {datetime.now().year} DocYa — Atención médica a domicilio
      </footer>
    </body>
    </html>
    """

    # 📄 Generar PDF temporal
    tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    HTML(string=html).write_pdf(tmp_pdf.name, stylesheets=[
        CSS(string="body { font-family: Helvetica; }")
    ])

    with open(tmp_pdf.name, "rb") as f:
        pdf_bytes = f.read()
    os.remove(tmp_pdf.name)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=certificado_{consulta_id}.pdf"}
    )




# --- PACIENTE: LISTAR RECETAS Y CERTIFICADOS ---
from fastapi import Depends
from sqlalchemy.orm import Session

@app.get("/paciente/{paciente_uuid}/recetas")
def listar_recetas_paciente(paciente_uuid: str, db: Session = Depends(get_db)):
    """
    Devuelve todas las recetas generadas para un paciente.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT id, consulta_id, medico_id, creado_en
        FROM recetas
        WHERE paciente_uuid = %s
        ORDER BY creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()
    recetas = [
        {
            "id": r[0],
            "consulta_id": r[1],
            "medico_id": r[2],
            "fecha": r[3].strftime('%d/%m/%Y'),
            "url": f"https://docya-railway-production.up.railway.app/consultas/{r[1]}/receta"
        }
        for r in rows
    ]
    return {"recetas": recetas}


@app.get("/paciente/{paciente_uuid}/certificados")
def listar_certificados_paciente(paciente_uuid: str, db: Session = Depends(get_db)):
    """
    Devuelve todos los certificados generados para un paciente.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT id, consulta_id, medico_id, creado_en
        FROM certificados
        WHERE paciente_uuid = %s
        ORDER BY creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()
    certificados = [
        {
            "id": r[0],
            "consulta_id": r[1],
            "medico_id": r[2],
            "fecha": r[3].strftime('%d/%m/%Y'),
            "url": f"https://docya-railway-production.up.railway.app/consultas/{r[1]}/certificado"
        }
        for r in rows
    ]
    return {"certificados": certificados}

# --- Recetas ---


class RecetaIn(BaseModel):
    medico_id: int
    paciente_uuid: str
    obra_social: Optional[str] = None
    nro_credencial: Optional[str] = None
    diagnostico: Optional[str] = None
    medicamentos: list[dict]

# --- POST: crear receta ---
@app.post("/consultas/{consulta_id}/receta")
def crear_receta(consulta_id: int, data: RecetaIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        INSERT INTO recetas (consulta_id, medico_id, paciente_uuid, obra_social, nro_credencial, diagnostico)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        consulta_id,
        data.medico_id,
        data.paciente_uuid,
        data.obra_social,
        data.nro_credencial,
        data.diagnostico
    ))

    receta_id = cur.fetchone()[0]

    for m in data.medicamentos:
        cur.execute("""
            INSERT INTO receta_items (receta_id, nombre, dosis, frecuencia, duracion)
            VALUES (%s, %s, %s, %s, %s)
        """, (receta_id, m["nombre"], m["dosis"], m["frecuencia"], m["duracion"]))

    db.commit()
    return {"ok": True, "receta_id": receta_id}


# --- GET: ver receta de una consulta (HTML DocYa con firma digital) ---
from fastapi.responses import HTMLResponse

@app.get("/consultas/{consulta_id}/receta", response_class=HTMLResponse)
def ver_receta_consulta(consulta_id: int, db=Depends(get_db)):
    """
    Muestra la receta de una consulta (última generada) en formato HTML DocYa, incluyendo la firma digital real del médico.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT r.id
        FROM recetas r
        WHERE r.consulta_id = %s
        ORDER BY r.creado_en DESC
        LIMIT 1
    """, (consulta_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="❌ No existe receta para esta consulta")

    receta_id = row[0]

    # Obtener datos completos incluyendo firma del médico
    cur.execute("""
        SELECT r.id, r.obra_social, r.nro_credencial, r.diagnostico, r.creado_en,
               c.id AS consulta_id,
               m.full_name AS medico_nombre, m.especialidad, m.matricula, m.firma_url,
               u.full_name AS paciente_nombre, u.dni
        FROM recetas r
        JOIN consultas c ON c.id = r.consulta_id
        JOIN medicos m ON m.id = c.medico_id
        JOIN users u ON u.id = r.paciente_uuid
        WHERE r.id = %s
    """, (receta_id,))
    receta = cur.fetchone()
    if not receta:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    cur.execute("""
        SELECT nombre, dosis, frecuencia, duracion
        FROM receta_items
        WHERE receta_id = %s
    """, (receta_id,))
    medicamentos = cur.fetchall()

    # Asignar variables legibles
    obra_social = receta[1]
    nro_credencial = receta[2]
    diagnostico = receta[3]
    creado_en = receta[4]
    medico_nombre = receta[6]
    especialidad = receta[7]
    matricula = receta[8]
    firma_url = receta[9]
    paciente_nombre = receta[10]
    dni = receta[11]

    fecha = creado_en.strftime("%d/%m/%Y %H:%M") if creado_en else "—"

    # Generar bloque de firma dinámico
    firma_html = f"""
    <div class="firma" style="margin-top: 40px;">
      <p><b>Firma digital:</b></p>
      {"<img src='" + firma_url + "' alt='Firma del médico'>" if firma_url else "<p><i>Firma no registrada</i></p>"}
      <p style='font-size:13px;color:#4b5563;'>Documento firmado electrónicamente conforme Ley 25.506.</p>
    </div>
    """

    # --- HTML FINAL ---
    html = f"""
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Receta Médica Digital</title>
      <style>
        body {{
          font-family: 'Helvetica', Arial, sans-serif;
          background-color: #f9fafb;
          color: #1f2937;
          padding: 24px;
          max-width: 750px;
          margin: auto;
          line-height: 1.7;
        }}
        .card {{
          background: white;
          border-radius: 12px;
          padding: 30px 35px;
          box-shadow: 0 3px 10px rgba(0,0,0,0.08);
        }}
        .header {{
          display: flex;
          align-items: center;
          gap: 15px;
          margin-bottom: 20px;
        }}
        .header img {{
          height: 55px;
        }}
        .title {{
          color: #14B8A6;
          font-size: 24px;
          font-weight: bold;
        }}
        hr {{
          border: none;
          border-top: 2px solid #14B8A6;
          margin: 15px 0 25px;
        }}
        .section-title {{
          color: #14B8A6;
          font-weight: bold;
          font-size: 17px;
          margin-top: 28px;
          margin-bottom: 8px;
        }}
        .label {{ font-weight: bold; }}
        ul {{ margin-left: 25px; }}
        li {{ margin-bottom: 8px; }}
        .firma img {{ width: 160px; margin-top: 10px; }}
        .qr img {{ width: 90px; }}
        footer {{
          text-align: center;
          color: #9ca3af;
          font-size: 13px;
          margin-top: 50px;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">
          <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png" alt="DocYa Logo">
          <div class="title">Receta Médica Digital</div>
        </div>
        <hr>
        <p><b>Médico:</b> {medico_nombre}<br>
           <b>Especialidad:</b> {especialidad}<br>
           <b>Matrícula:</b> {matricula}<br>
           <b>Fecha:</b> {fecha}</p>

        <div class="section-title">Paciente</div>
        <p><b>Nombre:</b> {paciente_nombre}<br>
           <b>DNI:</b> {dni}<br>
           <b>Obra social:</b> {obra_social or '—'}<br>
           <b>Credencial:</b> {nro_credencial or '—'}</p>

        <div class="section-title">Diagnóstico</div>
        <p>{diagnostico or '—'}</p>

        <div class="section-title">Rp / Indicaciones</div>
        <ul>
          {''.join([f"<li><b>{m[0]}</b>: {m[1]}, {m[2]}, {m[3]}</li>" for m in medicamentos])}
        </ul>

        {firma_html}

        <div class="qr" style="text-align:right;margin-top:30px;">
          <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya-railway-production.up.railway.app/consultas/{consulta_id}/receta">
          <small>Verificar autenticidad:<br>
          docya-railway-production.up.railway.app/consultas/{consulta_id}/receta</small>
        </div>
      </div>
      <footer>© {datetime.now().year} DocYa — Atención médica a domicilio</footer>
    </body>
    </html>
    """

    return HTMLResponse(html)



# --- Notas ---
class NotaIn(BaseModel): medico_id:int; paciente_uuid:str; contenido:str
@app.post("/consultas/{consulta_id}/nota")
def crear_nota(consulta_id:int,data:NotaIn,db=Depends(get_db)):
    cur=db.cursor();cur.execute("INSERT INTO notas_medicas (consulta_id,medico_id,paciente_uuid,contenido) VALUES (%s,%s,%s,%s) RETURNING id",(consulta_id,data.medico_id,data.paciente_uuid,data.contenido))
    nota_id=cur.fetchone()[0];db.commit();return {"ok":True,"nota_id":nota_id}

# --- Ubicación actual del médico ---
@app.get("/consultas/{consulta_id}/ubicacion_medico")
def ubicacion_medico_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT m.id, m.full_name, m.latitud, m.longitud, m.telefono
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        WHERE c.id = %s 
          AND c.estado IN ('aceptada','en_camino','en_domicilio')
    """, (consulta_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No se encontró ubicación")
    return {
        "medico_id": row[0],
        "nombre": row[1],
        "lat": row[2],
        "lng": row[3],
        "telefono": row[4],
    }





import asyncio

# --- WebSocket de médicos ---
# ====================================================
# 🩺 WEBSOCKET MÉDICO (con ping/pong)
# ====================================================
import json

import asyncio
@app.websocket("/ws/medico/{medico_id}")
async def medico_ws(websocket: WebSocket, medico_id: int):
    await websocket.accept()
    print(f"🟢 Nuevo WebSocket aceptado para profesional {medico_id}")

    # ⭐ 1) OBTENER EL TIPO (medico / enfermero) — CAMBIO NUEVO
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()
        cur.execute("SELECT tipo FROM medicos WHERE id=%s", (medico_id,))
        row = cur.fetchone()
        tipo_profesional = row[0] if row else "medico"
        cur.close()
        conn.close()
    except Exception as e:
        print("⚠️ Error obteniendo tipo:", e)
        tipo_profesional = "medico"

    # ⭐ 2) GUARDAR WS + TIPO — CAMBIO NUEVO
    active_medicos[medico_id] = {
        "ws": websocket,
        "tipo": tipo_profesional
    }

    # 🔄 Marcar profesional como disponible (SIN CAMBIOS)
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()
        cur.execute("UPDATE medicos SET disponible = TRUE, ultimo_ping = NOW() WHERE id = %s;", (medico_id,))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Profesional {medico_id} ({tipo_profesional}) marcado como disponible")
    except Exception as e:
        print(f"⚠️ Error al marcar profesional disponible: {e}")

    try:
        while True:
            data = await websocket.receive_text()
            print(f"📩 Mensaje recibido de profesional {medico_id}: {data}")

            try:
                msg = json.loads(data)
                tipo = msg.get("tipo", "").lower()
            except json.JSONDecodeError:
                tipo = data.strip().lower()

            # 🕒 Mantener ping/pong (SIN CAMBIOS)
            if tipo == "ping":
                try:
                    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
                    cur = conn.cursor()
                    cur.execute("UPDATE medicos SET ultimo_ping = NOW() WHERE id = %s;", (medico_id,))
                    conn.commit()
                    cur.close()
                    conn.close()
                except Exception as e2:
                    print(f"⚠️ Error guardando ultimo_ping del profesional {medico_id}: {e2}")

                await websocket.send_text("pong")
                await asyncio.sleep(0.05)
                continue

    except Exception as e:
        print(f"❌ Profesional desconectado: {medico_id} → {e}")

        # ✔ BORRAR EL PROFESIONAL DE active_medicos (SIN CAMBIOS, solo adaptado al dict)
        if medico_id in active_medicos:
            del active_medicos[medico_id]

        # 🔴 Marcar NO disponible (SIN CAMBIOS)
        await asyncio.sleep(10)
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            cur = conn.cursor()
            cur.execute("UPDATE medicos SET disponible = FALSE WHERE id = %s;", (medico_id,))
            conn.commit()
            cur.close()
            conn.close()
            print(f"🔴 Profesional {medico_id} marcado como NO disponible")
        except Exception as e2:
            print(f"⚠️ Error al marcar desconexión del profesional {medico_id}: {e2}")

        print(f"🔻 Total conectados ahora: {len(active_medicos)}")



# --- Función para enviar notificaciones push ---
def enviar_push(fcm_token: str, titulo: str, cuerpo: str, data: dict = {}):
    project_id = service_account_info["project_id"]
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json; UTF-8",
    }
    payload = {
        "message": {
            "token": fcm_token,
            "notification": {"title": titulo, "body": cuerpo},
            "data": data,
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    print("📤 Push enviado:", r.status_code, r.text)

# --- Endpoint para testear notificación push ---
@app.post("/test_push/{medico_id}")
def test_push(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Médico sin fcm_token registrado")
    enviar_push(
        row[0],
        "📢 Notificación de prueba",
        "Esto es una notificación de prueba",
        {"tipo": "test_push", "medico_id": str(medico_id)},
    )
    return {"ok": True, "mensaje": "Notificación enviada"}


# ====================================================
# 🧑‍🤝‍🧑 PACIENTES
# ====================================================

@app.get("/pacientes/{user_id}")
def obtener_paciente(user_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, full_name, email FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return {"id": row[0], "full_name": row[1], "email": row[2]}


# ====================================================
# 🧭 ENDPOINTS RESTAURADOS (compatibilidad con back viejo)
# ====================================================

from uuid import UUID

# ---------- DIRECCIONES ----------
class DireccionIn(BaseModel):
    user_id: UUID
    direccion: str
    lat: float
    lng: float
    piso: str | None = None
    depto: str | None = None
    indicaciones: str | None = None
    telefono_contacto: str

@app.post("/direccion/guardar")
def guardar_direccion(data: DireccionIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE id=%s", (str(data.user_id),))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    cur.execute("""
        INSERT INTO direcciones (user_id, direccion, lat, lng, piso, depto, indicaciones, telefono_contacto, fecha_actualizacion)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id) DO UPDATE SET
            direccion = EXCLUDED.direccion,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            piso = EXCLUDED.piso,
            depto = EXCLUDED.depto,
            indicaciones = EXCLUDED.indicaciones,
            telefono_contacto = EXCLUDED.telefono_contacto,
            fecha_actualizacion = CURRENT_TIMESTAMP
    """, (
        str(data.user_id), data.direccion, data.lat, data.lng,
        data.piso, data.depto, data.indicaciones, data.telefono_contacto
    ))
    db.commit()
    return {"mensaje": "Dirección guardada correctamente"}

@app.get("/direccion/mia/{user_id}")
def obtener_direccion(user_id: UUID, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, user_id, direccion, lat, lng, piso, depto, indicaciones, telefono_contacto,
               fecha_creacion, fecha_actualizacion
        FROM direcciones
        WHERE user_id = %s
        LIMIT 1
    """, (str(user_id),))
    direccion = cur.fetchone()
    if not direccion:
        raise HTTPException(status_code=404, detail="No se encontró dirección para este usuario")
    return {
        "id": direccion[0],
        "user_id": direccion[1],
        "direccion": direccion[2],
        "lat": direccion[3],
        "lng": direccion[4],
        "piso": direccion[5],
        "depto": direccion[6],
        "indicaciones": direccion[7],
        "telefono_contacto": direccion[8],
        "fecha_creacion": direccion[9],
        "fecha_actualizacion": direccion[10],
    }


# ====================================================
# 💊 GENERAR RECETA PDF PROFESIONAL
# ====================================================
from fastapi import Form, UploadFile, File
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
import io, qrcode
from datetime import datetime

@app.post("/consultas/{consulta_id}/receta_pdf")
async def generar_receta_pdf(
    consulta_id: int,
    medico_id: int = Form(...),
    paciente_uuid: str = Form(...),
    obra_social: str = Form(""),
    nro_credencial: str = Form(""),
    diagnostico: str = Form(""),
    firma: UploadFile = File(None)
):
    try:
        # Buscar datos del médico y paciente
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()
        cur.execute("SELECT full_name, matricula, especialidad FROM medicos WHERE id=%s", (medico_id,))
        medico = cur.fetchone()
        cur.execute("SELECT full_name, dni, fecha_nacimiento FROM users WHERE id=%s", (paciente_uuid,))
        paciente = cur.fetchone()
        conn.close()

        if not medico or not paciente:
            raise Exception("Datos de médico o paciente no encontrados")

        medico_nombre, matricula, especialidad = medico
        paciente_nombre, paciente_dni, paciente_nac = paciente

        # Crear PDF
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        # Fondo y encabezado
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, width, height, fill=1)
        c.drawImage(
            "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png",
            40, height - 90, width=140, preserveAspectRatio=True, mask='auto'
        )

        c.setFont("Helvetica-Bold", 18)
        c.setFillColor(colors.HexColor("#14B8A6"))
        c.drawString(200, height - 70, "Receta Médica Digital")

        # Línea divisoria
        c.setStrokeColor(colors.HexColor("#14B8A6"))
        c.setLineWidth(1)
        c.line(40, height - 95, width - 40, height - 95)

        # Datos del médico
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, height - 120, f"Médico: {medico_nombre}")
        c.setFont("Helvetica", 11)
        c.drawString(40, height - 135, f"Especialidad: {especialidad}")
        c.drawString(40, height - 150, f"Matrícula: {matricula}")
        c.drawString(40, height - 165, f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

        # Datos del paciente
        y = height - 200
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Paciente:")
        c.setFont("Helvetica", 11)
        c.drawString(120, y, f"{paciente_nombre}")
        y -= 18
        c.drawString(40, y, f"DNI: {paciente_dni or '—'}")
        c.drawString(200, y, f"Fecha nac.: {paciente_nac.strftime('%d/%m/%Y') if paciente_nac else '—'}")
        y -= 18
        c.drawString(40, y, f"Obra social: {obra_social or '—'}")
        c.drawString(250, y, f"Credencial: {nro_credencial or '—'}")

        # Diagnóstico
        y -= 35
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Diagnóstico:")
        y -= 18
        c.setFont("Helvetica", 11)
        c.drawString(60, y, diagnostico or "—")

        # Medicamentos
        y -= 35
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Rp / Indicaciones:")
        y -= 20

        # Consultar medicamentos de la receta
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()
        cur.execute("""
            SELECT nombre, dosis, frecuencia, duracion
            FROM receta_items ri
            JOIN recetas r ON r.id = ri.receta_id
            WHERE r.consulta_id = %s AND r.medico_id = %s
        """, (consulta_id, medico_id))
        medicamentos = cur.fetchall()
        conn.close()

        c.setFont("Helvetica", 11)
        for m in medicamentos:
            y -= 18
            if y < 100:  # salto de página si se llena
                c.showPage()
                y = height - 100
            c.drawString(60, y, f"- {m[0]}  ({m[1]}), {m[2]}, {m[3]}")

        # Firma
        y -= 60
        if firma:
            firma_bytes = await firma.read()
            firma_img = ImageReader(io.BytesIO(firma_bytes))
            c.drawImage(firma_img, 60, y - 20, width=160, height=60, mask="auto")
        c.setFont("Helvetica", 10)
        c.drawString(60, y - 30, "Firma digital del profesional")

        # QR con link de verificación
        qr_data = f"https://docya-railway-production.up.railway.app/ver_receta/{consulta_id}"
        qr_img = qrcode.make(qr_data)
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf)
        qr_buf.seek(0)
        c.drawImage(ImageReader(qr_buf), width - 150, 90, width=100, height=100)
        c.setFont("Helvetica", 8)
        c.drawString(width - 150, 80, "Verificar autenticidad")

        c.showPage()
        c.save()
        buffer.seek(0)

        # Subir a Cloudinary
        result = cloudinary.uploader.upload(
            buffer,
            resource_type="raw",
            folder="recetas",
            public_id=f"receta_{consulta_id}",
            overwrite=True,
            format="pdf"
        )

        return {"status": "ok", "consulta_id": consulta_id, "pdf_url": result.get("secure_url")}

    except Exception as e:
        return {"error": str(e)}



# ====================================================
# 💾 GENERAR PDF DESDE HTML (receta verificada)
# ====================================================
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import tempfile

@app.post("/consultas/{consulta_id}/receta_pdf_html")
def generar_receta_pdf_html(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT r.id, r.consulta_id, r.paciente_uuid, r.medico_id,
               r.obra_social, r.nro_credencial, r.diagnostico,
               c.creado_en, m.full_name, m.matricula, m.especialidad,
               u.full_name AS paciente_nombre, u.dni
        FROM recetas r
        JOIN consultas c ON c.id = r.consulta_id
        JOIN medicos m ON r.medico_id = m.id
        JOIN users u ON r.paciente_uuid = u.id
        WHERE c.id = %s
    """, (consulta_id,))
    receta = cur.fetchone()
    if not receta:
        raise HTTPException(status_code=404, detail="Receta no encontrada")

    (
        receta_id, consulta_id, paciente_uuid, medico_id,
        obra_social, nro_credencial, diagnostico,
        creado_en, medico_nombre, matricula, especialidad,
        paciente_nombre, paciente_dni
    ) = receta

    cur.execute("""
        SELECT nombre, dosis, frecuencia, duracion
        FROM receta_items ri
        JOIN recetas r ON r.id = ri.receta_id
        WHERE r.consulta_id = %s
    """, (consulta_id,))
    medicamentos = cur.fetchall()

    fecha_str = creado_en.strftime("%d/%m/%Y %H:%M")

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: 'Helvetica', Arial, sans-serif;
                margin: 0;
                padding: 40px 60px;
                background-color: white;
                color: #222;
                font-size: 13px;
            }}
            h1 {{
                text-align: center;
                color: #14B8A6;
                border-bottom: 2px solid #14B8A6;
                padding-bottom: 5px;
            }}
            .header {{
                text-align: center;
                margin-bottom: 20px;
            }}
            .datos {{
                margin-top: 10px;
                line-height: 1.6;
            }}
            .titulo {{
                color: #14B8A6;
                font-weight: bold;
                margin-top: 20px;
                margin-bottom: 5px;
                font-size: 15px;
            }}
            ul {{
                margin: 0;
                padding-left: 18px;
            }}
            .firma {{
                margin-top: 50px;
            }}
            .firma img {{
                height: 80px;
            }}
            .qr {{
                position: absolute;
                right: 60px;
                bottom: 80px;
                text-align: center;
            }}
            .qr img {{
                height: 100px;
            }}
            .pie {{
                position: fixed;
                bottom: 40px;
                left: 0;
                width: 100%;
                text-align: center;
                font-size: 11px;
                color: #555;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png" height="60"><br>
            <h1>Receta Médica Digital</h1>
        </div>

        <div class="datos">
            <b>Médico:</b> {medico_nombre}<br>
            <b>Especialidad:</b> {especialidad}<br>
            <b>Matrícula:</b> {matricula}<br>
            <b>Fecha:</b> {fecha_str}
        </div>

        <div class="titulo">Paciente</div>
        <p>
            <b>Nombre:</b> {paciente_nombre}<br>
            <b>DNI:</b> {paciente_dni or '—'}<br>
            <b>Obra social:</b> {obra_social or '—'}<br>
            <b>Credencial:</b> {nro_credencial or '—'}
        </p>

        <div class="titulo">Diagnóstico</div>
        <p>{diagnostico or '—'}</p>

        <div class="titulo">Rp / Indicaciones</div>
        <ul>
            {''.join([f"<li><b>{m[0]}</b>: {m[1]}, {m[2]}, {m[3]}</li>" for m in medicamentos])}
        </ul>

        <div class="firma">
            <p><b>Firma digital:</b></p>
            <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/firma_docya_1_sjgxop.png">
            <p>Documento firmado electrónicamente conforme Ley 25.506.</p>
        </div>

        <div class="qr">
            <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya-railway-production.up.railway.app/ver_receta/{consulta_id}">
            <div>Verificación:<br>docya-railway-production.up.railway.app/ver_receta/{consulta_id}</div>
        </div>

        <div class="pie">
            © {datetime.now().year} DocYa · Atención médica a domicilio
        </div>
    </body>
    </html>
    """

    # Convertir HTML a PDF
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
        HTML(string=html).write_pdf(tmp_pdf.name, stylesheets=[
            CSS(string="body { font-family: Helvetica, sans-serif; }")
        ])
        tmp_pdf.seek(0)
        pdf_bytes = tmp_pdf.read()

    # Subir a Cloudinary
    result = cloudinary.uploader.upload(
        io.BytesIO(pdf_bytes),
        resource_type="raw",
        folder="recetas",
        public_id=f"receta_html_{consulta_id}",
        overwrite=True,
        format="pdf"
    )

    return {"status": "ok", "pdf_url": result.get("secure_url")}


# ---------- USUARIOS ----------
@app.get("/usuarios/{user_id}")
def alias_usuario(user_id: str, db=Depends(get_db)):
    return obtener_paciente(user_id, db)



#PAGOS MP -------------------------------------------------------------------------------------------------------------------------
@app.get("/consultas/post_pago")
async def post_pago(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id FROM consultas
        WHERE paciente_uuid=%s
        ORDER BY creado_en DESC
        LIMIT 1
    """, (paciente_uuid,))
    row = cur.fetchone()
    return {"consulta_id": row[0] if row else None}

@app.get("/consultas/hay_profesional")
async def hay_profesional(
    lat: str,
    lng: str,
    tipo: str,
    db=Depends(get_db)
):
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except:
        raise HTTPException(status_code=400, detail="Lat/Lng inválidos")

    cur = db.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM medicos
        WHERE disponible = TRUE
          AND tipo = %s
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
    """, (tipo,))

    count = cur.fetchone()[0]

    return {"disponibles": count > 0}


import mercadopago

sdk = mercadopago.SDK(os.getenv("MP_ACCESS_TOKEN").strip())


@app.post("/pagos/crear_preferencia")
async def crear_preferencia(data: PagoIn):
    preference_data = {
        "items": [
            {
                "title": data.descripcion,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(data.monto),
            }
        ],
        "metadata": {
            "paciente_uuid": data.paciente_uuid,
            "tipo": data.tipo,
            "lat": data.lat,
            "lng": data.lng
        },
        "back_urls": {
            "success": "https://docya.com/success",
            "failure": "https://docya.com/failure",
            "pending": "https://docya.com/pending"
        },
        "auto_return": "approved",
        "notification_url": "https://docya-railway-production.up.railway.app/pagos/notificacion"
    }

    preference_response = sdk.preference().create(preference_data)
    return {
        "init_point": preference_response["response"]["init_point"],
        "preference_id": preference_response["response"]["id"]
    }

@app.post("/pagos/notificacion")
async def pagos_notificacion(request: Request, db=Depends(get_db)):
    body = await request.json()
    
    if body.get("type") != "payment":
        return {"status": "ignored"}

    payment_id = body["data"]["id"]

    # Obtener info del pago
    payment_info = sdk.payment().get(payment_id)["response"]

    if payment_info["status"] != "approved":
        return {"status": "not_approved"}

    metadata = payment_info["metadata"]
    paciente_uuid = metadata["paciente_uuid"]
    tipo = metadata["tipo"]
    lat = metadata["lat"]
    lng = metadata["lng"]

    cur = db.cursor()

    # BUSCAR PROFESIONAL DISPONIBLE
    cur.execute("""
        SELECT id, full_name, latitud, longitud,
        (6371 * acos(
            cos(radians(%s)) * cos(radians(latitud)) *
            cos(radians(longitud) - radians(%s)) +
            sin(radians(%s)) * sin(radians(latitud))
        )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
        AND tipo = %s
        ORDER BY distancia ASC
        LIMIT 1
    """, (lat, lng, lat, tipo))

    row = cur.fetchone()

    if not row:
        # NO HAY PROFESIONAL —> REEMBOLSO AUTOMÁTICO
        sdk.payment().refund(payment_id)

        print("⚠️ Pago devuelto automáticamente, sin profesionales.")
        return {"status": "refunded_no_professional"}

    medico_id, nombre, mlat, mlng, distancia = row

    # CREAR CONSULTA
    cur.execute("""
        INSERT INTO consultas (paciente_uuid, medico_id, motivo, direccion, lat, lng, estado, metodo_pago)
        VALUES (%s,%s,'Pago aprobado', 'Dirección desde MP', %s, %s,'pendiente','tarjeta')
        RETURNING id, creado_en
    """, (paciente_uuid, medico_id, lat, lng))
    
    consulta_id, creado_en = cur.fetchone()
    db.commit()

    # WS + PUSH (igual que en tu back actual)
    if medico_id in active_medicos:
        try:
            await active_medicos[medico_id].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id,
                "paciente_uuid": paciente_uuid
            })
        except:
            print("WS error")

    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if row and row[0]:
        enviar_push(row[0], "📢 Nueva consulta", "Tienes una nueva solicitud", {
            "consulta_id": consulta_id
        })

    return {"status": "consulta_creada", "consulta_id": consulta_id}

    
# ---------- CONSULTAS DETALLE ----------
@app.get("/consultas/{consulta_id}")
def obtener_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.paciente_uuid, c.medico_id, c.estado, c.motivo, c.direccion, 
               c.lat, c.lng, c.creado_en,
               m.full_name, m.matricula
        FROM consultas c
        LEFT JOIN medicos m ON c.medico_id = m.id
        WHERE c.id = %s
    """, (consulta_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {
        "id": row[0],
        "paciente_uuid": row[1],
        "medico_id": row[2],
        "estado": row[3],
        "motivo": row[4],
        "direccion": row[5],
        "lat": row[6],
        "lng": row[7],
        "creado_en": format_datetime_arg(row[8]),   # 👈
        "medico_nombre": row[9],
        "medico_matricula": row[10],
    }


#valoraciones medicos -------------------------------------------------------------------------------
from typing import Optional
from pydantic import BaseModel
@app.post("/consultas/{consulta_id}/valorar")
def valorar_consulta(consulta_id: int, data: ValoracionIn, db=Depends(get_db)):
    cur = db.cursor()
    # Validar que la consulta ya esté finalizada y pertenece al paciente
    cur.execute("""
        SELECT id FROM consultas 
        WHERE id=%s AND paciente_uuid=%s AND estado='finalizada'
    """, (consulta_id, data.paciente_uuid))
    if not cur.fetchone():
        raise HTTPException(status_code=400, detail="Consulta no finalizada o no corresponde al paciente")

    # Determinar si es médico o enfermero
    if data.medico_id:
        cur.execute("""
            INSERT INTO valoraciones (consulta_id, paciente_uuid, medico_id, puntaje, comentario)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (consulta_id, data.paciente_uuid, data.medico_id, data.puntaje, data.comentario))
    elif data.enfermero_id:
        cur.execute("""
            INSERT INTO valoraciones (consulta_id, paciente_uuid, enfermero_id, puntaje, comentario)
            VALUES (%s,%s,%s,%s,%s) RETURNING id
        """, (consulta_id, data.paciente_uuid, data.enfermero_id, data.puntaje, data.comentario))
    else:
        raise HTTPException(status_code=400, detail="Debe indicar medico_id o enfermero_id")

    valoracion_id = cur.fetchone()[0]
    db.commit()
    return {"ok": True, "valoracion_id": valoracion_id}


@app.get("/medicos/{medico_id}/valoraciones")
def obtener_valoraciones(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT v.puntaje, v.comentario, v.creado_en, u.full_name as paciente
        FROM valoraciones v
        LEFT JOIN users u ON v.paciente_uuid = u.id
        WHERE v.medico_id=%s
        ORDER BY v.creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()
    return [
        {
            "puntaje": r[0],
            "comentario": r[1],
            "fecha": format_datetime_arg(r[2]),
            "paciente": r[3]
        }
        for r in rows
    ]

# ==========================
# 📋 Historial del paciente
# ==========================
@app.get("/consultas/historial_paciente/{paciente_uuid}")
def historial_paciente(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            c.id,
            c.motivo,
            c.estado,
            c.direccion,
            c.creado_en,
            COALESCE(m.full_name, 'Médico sin asignar') AS medico_nombre
        FROM consultas c
        LEFT JOIN users m ON c.medico_id = m.id
        WHERE c.paciente_uuid = %s
        ORDER BY c.creado_en DESC
        LIMIT 5
    """, (paciente_uuid,))
    
    rows = cur.fetchall()
    db.close()

    return [
        {
            "id": r[0],
            "motivo": r[1],
            "estado": r[2],
            "direccion": r[3],
            "creado_en": r[4].strftime("%d/%m/%Y %H:%M"),
            "medico_nombre": r[5]
        }
        for r in rows
    ]

# ---------- UBICACIÓN MÉDICO ----------
from zoneinfo import ZoneInfo
from datetime import datetime
from fastapi import HTTPException

class UbicacionIn(BaseModel):
    lat: float
    lng: float
    disponible: bool = True

@app.post("/medico/{medico_id}/ubicacion")
def actualizar_ubicacion(medico_id: int, data: UbicacionIn, db=Depends(get_db)):
    try:
        ahora_arg = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))

        cur = db.cursor()
        cur.execute("""
            UPDATE medicos
            SET latitud = %s,
                longitud = %s,
                disponible = %s,
                updated_at = %s,
                ultimo_ping = %s
            WHERE id = %s
            RETURNING id;
        """, (data.lat, data.lng, True, ahora_arg, ahora_arg, medico_id))

        row = cur.fetchone()
        db.commit()
        cur.close()

        if not row:
            raise HTTPException(status_code=404, detail="Médico no encontrado")

        print(f"📍 Médico {medico_id} actualizado → disponible=TRUE, ping={ahora_arg}")
        return {
            "ok": True,
            "medico_id": medico_id,
            "lat": data.lat,
            "lng": data.lng,
            "disponible": True,
            "ultimo_ping": ahora_arg.isoformat()
        }

    except Exception as e:
        db.rollback()
        print(f"⚠️ Error en actualizar_ubicacion: {e}")
        raise HTTPException(status_code=500, detail="Error actualizando ubicación del médico")


# ====================================================
# 🔄 ALIAS DE COMPATIBILIDAD (para no romper el frontend)
# ====================================================

# --- Obtener perfil alias ---
@app.get("/medicos/{medico_id}")
def alias_obtener_medico(medico_id: int, db=Depends(get_db)):
    return obtener_medico(medico_id, db)

# --- Foto alias ---
@app.post("/medicos/{medico_id}/foto")
def alias_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    return actualizar_foto(medico_id, file, db)

# --- Alias CBU alias ---
@app.patch("/medicos/{medico_id}/alias")
def alias_alias(medico_id: int, data: AliasIn, db=Depends(get_db)):
    return actualizar_alias(medico_id, data, db)

# --- FCM Token alias ---
@app.post("/medicos/{medico_id}/fcm_token")
def alias_fcm(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    return actualizar_fcm_token(medico_id, data, db)

# --- Stats alias ---
@app.get("/medicos/{medico_id}/stats")
def alias_stats(medico_id: int, db=Depends(get_db)):
    return medico_stats(medico_id, db)

# --- Disponibilidad alias ---
@app.post("/medicos/{medico_id}/disponibilidad")
def alias_disponibilidad(medico_id: int, disponible: bool, db=Depends(get_db)):
    return actualizar_disponibilidad(medico_id, disponible, db)


# Carpeta de plantillas HTML
templates = Jinja2Templates(directory="templates")



# ====================================================
# 🔑 Recuperar contraseña (Médicos)
# ====================================================
from sib_api_v3_sdk import SendSmtpEmail

class ForgotPasswordIn(BaseModel):
    identificador: str  # email o dni/pasaporte


@app.post("/auth/forgot_password")
def forgot_password(data: ForgotPasswordIn, db=Depends(get_db)):
    cur = db.cursor()
    identificador = data.identificador.strip().lower()

    # Buscar médico por email o DNI
    cur.execute("""
        SELECT id, full_name, email 
        FROM medicos
        WHERE LOWER(email) = %s OR dni = %s
        LIMIT 1
    """, (identificador, identificador))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No se encontró un profesional con esos datos")

    medico_id, full_name, email = row

    # Crear token de recuperación válido por 1 hora
    token = create_access_token(
        {"sub": str(medico_id), "email": email, "tipo": "reset_password"},
        expires_minutes=60
    )
    link_reset = f"https://docya-railway-production.up.railway.app/auth/reset_password?token={token}"

    # Plantilla HTML estilo DocYa Pro
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Recuperar contraseña – DocYa Pro</title>
    </head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;">
        <tr>
          <td align="center">
            <table border="0" cellpadding="0" cellspacing="0" width="600" 
                   style="background:#ffffff; border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1);">
              <tr>
                <td align="center" style="padding:30px 20px;">
                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" 
                       alt="DocYa" style="max-width:180px; margin-bottom:20px;">
                  <h2 style="color:#00A8A8; font-size:22px; margin:0 0 15px;">Recuperar tu contraseña</h2>
                  <p style="color:#333333; font-size:15px; line-height:1.5; margin:0 0 25px;">
                    Hola <b>{full_name}</b>, recibimos una solicitud para restablecer tu contraseña de acceso a <b>DocYa Pro</b>.<br><br>
                    Si fuiste vos, hacé clic en el botón siguiente para crear una nueva contraseña:
                  </p>
                  <a href="{link_reset}" target="_blank"
                     style="background-color:#00A8A8; color:#ffffff; padding:14px 28px; text-decoration:none; 
                            border-radius:6px; font-size:15px; font-weight:bold; display:inline-block;">
                    🔒 Restablecer contraseña
                  </a>
                  <p style="color:#555555; font-size:14px; line-height:1.5; margin:25px 0 0;">
                    Si no solicitaste este cambio, simplemente ignorá este mensaje.<br><br>
                    Por motivos de seguridad, el enlace expirará en 1 hora.
                  </p>
                </td>
              </tr>
            </table>
            <p style="color:#999999; font-size:11px; margin-top:20px;">
              © {datetime.now().year} DocYa · Profesionales a domicilio con confianza.
            </p>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    # Enviar correo con Brevo (igual que otros)
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Pro"},
        subject="Restablecé tu contraseña – DocYa Pro",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Correo de recuperación enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email con Brevo API: {e}")
        raise HTTPException(status_code=500, detail="Error al enviar el correo de recuperación")

    return {
        "ok": True,
        "message": f"Enviamos un correo a {email} para que recuperes tu contraseña."
    }


from fastapi import Form

# ====================================================
# 🔒 Restablecer contraseña (desde link del correo)
# ====================================================

class ResetPasswordIn(BaseModel):
    token: str
    new_password: str



@app.post("/auth/reset_password")
def reset_password(data: ResetPasswordIn, db=Depends(get_db)):
    """
    Permite al médico restablecer su contraseña desde el enlace recibido por email.
    """
    try:
        # 🔍 Verificar token JWT
        payload = verify_token(data.token)
        medico_id = payload.get("sub")
        if not medico_id:
            raise HTTPException(status_code=400, detail="Token inválido")

        # 🔐 Encriptar nueva contraseña
        hashed = get_password_hash(data.new_password)

        cur = db.cursor()
        cur.execute("UPDATE medicos SET password_hash = %s WHERE id = %s RETURNING id", (hashed, medico_id))
        db.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")

        # 📨 Correo de confirmación
        cur.execute("SELECT full_name, email FROM medicos WHERE id = %s", (medico_id,))
        full_name, email = cur.fetchone()

        html_confirm = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color:#F4F6F8; margin:0; padding:0;">
          <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0;">
            <tr>
              <td align="center">
                <table width="600" bgcolor="#ffffff" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1); padding:30px;">
                  <tr>
                    <td align="center">
                      <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/docyapro_1_uxxdjx.png" 
                           alt="DocYa Pro" style="max-width:160px; margin-bottom:20px;">
                      <h2 style="color:#14B8A6;">Contraseña actualizada con éxito</h2>
                      <p style="font-size:15px; color:#333333;">
                        Hola <b>{full_name}</b>, tu contraseña fue cambiada correctamente.<br>
                        Ya podés iniciar sesión con tu nueva clave desde la app o web de <b>DocYa Pro</b>.
                      </p>
                      <a href="https://docya-railway-production.up.railway.app/cambio_exitoso" 
                         style="display:inline-block; margin-top:20px; padding:12px 24px;
                                background-color:#14B8A6; color:#fff; text-decoration:none; border-radius:6px;">
                        Ir al inicio de sesión
                      </a>
                      <p style="color:#999; font-size:13px; margin-top:30px;">
                        Si no realizaste este cambio, comunicate con soporte inmediatamente.
                      </p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

        confirm_email = SendSmtpEmail(
            to=[{"email": email, "name": full_name}],
            sender={"email": "soporte@docya-railway-production.up.railway.app", "name": "DocYa Pro"},
            subject="Contraseña actualizada – DocYa Pro",
            html_content=html_confirm,
        )

        api_instance.send_transac_email(confirm_email)

        return {"ok": True, "message": "Contraseña actualizada correctamente."}

    except HTTPException as e:
        raise e
    except Exception as e:
        print("⚠️ Error en reset_password:", e)
        raise HTTPException(status_code=500, detail="Error interno al restablecer la contraseña")




# ====================================================
# 🌐 Página pública: Restablecer contraseña (HTML)
# ====================================================
@app.get("/auth/reset_password", response_class=HTMLResponse)
def render_reset_password_page(request: Request, token: str = None):
    """
    Renderiza la página profesional de restablecer contraseña DocYa Pro.
    """
    if not token:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif;color:#555;text-align:center;margin-top:80px;'>⚠️ Enlace inválido o faltante.</h3>",
            status_code=400,
        )
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

# ====================================================
# 🔒 Olvidé mi contraseña (PACIENTE)
# ====================================================

class ForgotPasswordIn(BaseModel):
    identificador: str  # puede ser email o DNI


@app.post("/auth/forgot_password_paciente")
def forgot_password_paciente(data: ForgotPasswordIn, db=Depends(get_db)):
    cur = db.cursor()
    identificador = data.identificador.strip().lower()

    # Buscar paciente por email o DNI
    cur.execute("""
        SELECT id, full_name, email 
        FROM users
        WHERE LOWER(email) = %s OR dni = %s
        LIMIT 1
    """, (identificador, identificador))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontró un paciente con esos datos")

    paciente_id, full_name, email = row

    # Crear token válido por 1 hora
    token = create_access_token(
        {"sub": str(paciente_id), "email": email, "tipo": "reset_password_paciente"},
        expires_minutes=60
    )

    link_reset = f"https://docya-railway-production.up.railway.app/auth/reset_password_paciente?token={token}"

    # HTML email DocYa
    html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Restablecer contraseña – DocYa</title>
    </head>
    <body style="margin:0; padding:0; background-color:#F4F6F8; font-family: Arial, sans-serif;">

      <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:20px 0;">
        <tr>
          <td align="center">

            <table width="600" bgcolor="#ffffff"
                   style="border-radius:10px; padding:35px; text-align:center; box-shadow:0 2px 6px rgba(0,0,0,0.1);">

              <tr>
                <td>

                  <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png"
                       alt="DocYa" style="width:180px; margin-bottom:20px;">

                  <h2 style="color:#14B8A6; font-size:22px; margin-bottom:15px;">Restablecer tu contraseña</h2>

                  <p style="color:#333; font-size:15px; line-height:1.6;">
                    Hola <b>{full_name}</b>, recibimos una solicitud para restablecer tu contraseña.<br>
                    Hacé clic en el siguiente botón para continuar:
                  </p>

                  <a href="{link_reset}" target="_blank"
                     style="background-color:#14B8A6; color:#fff; padding:14px 28px;
                            text-decoration:none; border-radius:6px; font-size:16px; font-weight:bold;
                            display:inline-block; margin-top:25px;">
                    🔒 Cambiar contraseña
                  </a>

                  <p style="color:#777; font-size:13px; margin-top:25px;">
                    Si no solicitaste este cambio, podés ignorar este mensaje.<br>
                    El enlace vence en 1 hora por motivos de seguridad.
                  </p>

                </td>
              </tr>

            </table>

            <p style="color:#aaa; font-size:11px; margin-top:20px;">
              © {datetime.now().year} DocYa — Atención médica a domicilio.
            </p>

          </td>
        </tr>
      </table>

    </body>
    </html>
    """

    # Enviar correo con Brevo
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = os.getenv("BREVO_API_KEY")
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa Atención al Paciente"},
        subject="Restablecé tu contraseña – DocYa",
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"📩 Email enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email recuperación: {e}")
        raise HTTPException(status_code=500, detail="Error al enviar el correo de recuperación")

    return {
        "ok": True,
        "message": f"Enviamos un correo a {email} para restablecer tu contraseña."
    }

# ====================================================
# 🔒 Restablecer contraseña (PACIENTE)
# ====================================================
@app.post("/auth/reset_password_paciente")
def reset_password_paciente(data: ResetPasswordIn, db=Depends(get_db)):
    """
    Permite al paciente restablecer su contraseña desde el enlace recibido por email.
    """
    try:
        # 🔍 Verificar token JWT
        payload = verify_token(data.token)
        paciente_id = payload.get("sub")   # <-- UUID REAL del paciente

        if not paciente_id:
            raise HTTPException(status_code=400, detail="Token inválido o expirado")

        # 🔐 Encriptar nueva contraseña
        hashed = get_password_hash(data.new_password)

        cur = db.cursor()

        # 🔥 FIX: usar la tabla correcta y pasar UUID directamente SIN str()
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s RETURNING id",
            (hashed, paciente_id),
        )
        updated = cur.fetchone()
        db.commit()

        if not updated:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")

        # 📧 Correo de confirmación (con UUID REAL)
        cur.execute(
            "SELECT full_name, email FROM users WHERE id = %s",
            (paciente_id,),
        )
        full_name, email = cur.fetchone()

        html_confirm = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color:#F4F6F8; margin:0; padding:0;">
          <table align="center" width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0;">
            <tr>
              <td align="center">
                <table width="600" bgcolor="#ffffff" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.1); padding:30px;">
                  <tr>
                    <td align="center">
                      <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png" 
                           alt="DocYa" style="max-width:160px; margin-bottom:20px;">
                      <h2 style="color:#14B8A6;">Contraseña actualizada con éxito</h2>
                      <p style="font-size:15px; color:#333333;">
                        Hola <b>{full_name}</b>, tu contraseña fue cambiada correctamente.<br>
                        Ya podés iniciar sesión con tu nueva clave desde la app o web de <b>DocYa</b>.
                      </p>
                      <a href="https://docya-railway-production.up.railway.app/cambio_exitoso" 
                         style="display:inline-block; margin-top:20px; padding:12px 24px;
                                background-color:#14B8A6; color:#fff; text-decoration:none; border-radius:6px;">
                        Ir al inicio de sesión
                      </a>
                      <p style="color:#999; font-size:13px; margin-top:30px;">
                        Si no realizaste este cambio, comunicate con soporte inmediatamente.
                      </p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = os.getenv("BREVO_API_KEY")
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )

        confirm_email = SendSmtpEmail(
            to=[{"email": email, "name": full_name}],
            sender={
                "email": "nahundeveloper@gmail.com",
                "name": "DocYa Atención al Paciente",
            },
            subject="Contraseña actualizada – DocYa",
            html_content=html_confirm,
        )

        api_instance.send_transac_email(confirm_email)

        return {"ok": True, "message": "Contraseña actualizada correctamente."}

    except HTTPException as e:
        raise e
    except Exception as e:
        print("⚠️ Error en reset_password_paciente:", e)
        raise HTTPException(
            status_code=500,
            detail="Error interno al restablecer la contraseña del paciente",
        )

from fastapi.responses import HTMLResponse

@app.get("/cambio_exitoso", response_class=HTMLResponse)
def cambio_exitoso():
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Contraseña Actualizada – DocYa</title>

        <style>
            body {
                margin: 0;
                padding: 0;
                font-family: 'Arial', sans-serif;
                background: linear-gradient(135deg, #0F2027, #203A43, #2C5364);
                color: white;
                text-align: center;
                padding: 40px 20px;
            }

            .card {
                background: rgba(255, 255, 255, 0.08);
                padding: 35px 30px;
                border-radius: 20px;
                max-width: 500px;
                margin: 0 auto;
                backdrop-filter: blur(14px);
                -webkit-backdrop-filter: blur(14px);
                box-shadow: 0 8px 25px rgba(0,0,0,0.25);
                border: 1px solid rgba(255, 255, 255, 0.12);
            }

            img.logo {
                width: 160px;
                margin-bottom: 20px;
                filter: drop-shadow(0px 4px 8px rgba(0,0,0,0.4));
            }

            h1 {
                font-size: 26px;
                margin-top: 10px;
                margin-bottom: 20px;
                font-weight: 700;
                color: #14B8A6;
            }

            p {
                font-size: 17px;
                color: #e8e8e8;
                line-height: 1.6;
                margin-bottom: 10px;
            }

            .footer {
                margin-top: 40px;
                font-size: 13px;
                color: #cccccc;
                opacity: 0.8;
            }
        </style>
    </head>

    <body>
        <div class="card">
            <img 
                class="logo"
                src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png"
                alt="DocYa"
            >

            <h1>¡Contraseña actualizada con éxito!</h1>

            <p>
                Tu contraseña fue cambiada correctamente.<br>
                Ya podés iniciar sesión en tu aplicación <b>DocYa</b> desde tu celular.
            </p>

            <p>
                Gracias por confiar en nuestro servicio de<br>
                <b>atención médica a domicilio</b>.
            </p>
        </div>

        <div class="footer">
            © 2025 DocYa · Atención médica y de enfermería a domicilio
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html)

# ====================================================
# 🌐 Página pública: Restablecer contraseña Paciente (HTML)
# ====================================================
@app.get("/auth/reset_password_paciente", response_class=HTMLResponse)
def render_reset_password_paciente_page(request: Request, token: str = None):
    """
    Renderiza la página pública de restablecer contraseña para pacientes DocYa.
    """
    if not token:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif;color:#555;text-align:center;margin-top:80px;'>⚠️ Enlace inválido o faltante.</h3>",
            status_code=400,
        )

    return templates.TemplateResponse(
        "reset_password_paciente.html", {"request": request, "token": token}
    )


# ==========================================================
# 🩺 Nueva ruta: Ver receta digital pública (DocYa)
# ==========================================================

from psycopg2.extras import RealDictCursor

@app.get("/ver_receta/{receta_id}", response_class=HTMLResponse)
def ver_receta(receta_id: int, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT r.id, r.obra_social, r.nro_credencial, r.diagnostico, r.creado_en,
               c.id AS consulta_id,
               m.full_name AS medico_nombre, m.especialidad, m.matricula,
               u.full_name AS paciente_nombre, u.dni
        FROM recetas r
        JOIN consultas c ON c.id = r.consulta_id
        JOIN medicos m ON m.id = c.medico_id
        JOIN users u ON u.id = r.paciente_uuid
        WHERE r.id = %s
    """, (receta_id,))
    receta = cur.fetchone()
    if not receta:
        return HTMLResponse("<h2>❌ Receta no encontrada</h2>", status_code=404)

    cur.execute("""
        SELECT nombre, dosis, frecuencia, duracion
        FROM receta_items
        WHERE receta_id = %s
    """, (receta_id,))
    medicamentos = cur.fetchall()

    fecha = receta["creado_en"].strftime("%d/%m/%Y %H:%M") if receta.get("creado_en") else "—"

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Receta Médica Digital</title>
      <style>
        body {{
          font-family: 'Helvetica', Arial, sans-serif;
          background-color: #f9fafb;
          color: #1f2937;
          padding: 24px;
          max-width: 750px;
          margin: auto;
          line-height: 1.7;
        }}
        .card {{
          background: white;
          border-radius: 12px;
          padding: 30px 35px;
          box-shadow: 0 3px 10px rgba(0,0,0,0.08);
        }}
        .header {{
          display: flex;
          align-items: center;
          gap: 15px;
          margin-bottom: 20px;
        }}
        .header img {{
          height: 55px;
          width: auto;
          object-fit: contain;
        }}
        .title {{
          color: #14B8A6;
          font-size: 24px;
          font-weight: bold;
          white-space: nowrap;
        }}
        hr {{
          border: none;
          border-top: 2px solid #14B8A6;
          margin: 15px 0 25px;
        }}
        .section-title {{
          color: #14B8A6;
          font-weight: bold;
          font-size: 17px;
          margin-top: 28px;
          margin-bottom: 8px;
        }}
        .label {{
          font-weight: bold;
        }}
        p {{
          margin: 6px 0;
        }}
        ul {{
          margin: 10px 0 0 25px;
        }}
        li {{
          margin-bottom: 8px;
        }}
        .firma {{
          margin-top: 45px;
          text-align: left;
        }}
        .firma img {{
          width: 160px;
          margin-top: 6px;
        }}
        .qr {{
          text-align: right;
          margin-top: 30px;
        }}
        .qr img {{
          width: 90px;
          height: 90px;
        }}
        footer {{
          text-align: center;
          color: #9ca3af;
          font-size: 13px;
          margin-top: 50px;
        }}
        @media (max-width: 600px) {{
          .card {{
            padding: 20px;
          }}
          .header img {{
            height: 45px;
          }}
          .title {{
            font-size: 20px;
          }}
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">
          <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png" alt="DocYa Logo">
          <div class="title">Receta Médica Digital</div>
        </div>
        <hr>

        <p><span class="label">Médico:</span> {receta['medico_nombre']}<br>
           <span class="label">Especialidad:</span> {receta['especialidad']}<br>
           <span class="label">Matrícula:</span> {receta['matricula']}<br>
           <span class="label">Fecha:</span> {fecha}</p>

        <div class="section-title">Paciente</div>
        <p>
          <span class="label">Nombre:</span> {receta['paciente_nombre']}<br>
          <span class="label">DNI:</span> {receta['dni']}<br>
          <span class="label">Obra social:</span> {receta['obra_social'] or '—'}<br>
          <span class="label">Credencial:</span> {receta['nro_credencial'] or '—'}
        </p>

        <div class="section-title">Diagnóstico</div>
        <p>{receta['diagnostico'] or '—'}</p>

        <div class="section-title">Rp / Indicaciones</div>
        <ul>
          {''.join([
            f"<li><b>{m['nombre']}</b>: {m['dosis']}, {m['frecuencia']}, {m['duracion']}</li>"
            for m in medicamentos
          ])}
        </ul>

        <div class="firma">
          <p><span class="label">Firma digital:</span></p>
          <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/firma_docya_1_sjgxop.png" alt="Firma digital">
          <p style="font-size:13px; color:#4b5563;">Documento firmado electrónicamente conforme Ley 25.506.</p>
        </div>

        <div class="qr">
          <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya-railway-production.up.railway.app/ver_receta/{receta_id}" alt="QR">
          <p style="font-size:12px; color:#6b7280;">Verificar autenticidad<br>docya-railway-production.up.railway.app/ver_receta/{receta_id}</p>
        </div>
      </div>

      <footer>© {datetime.now().year} DocYa — Atención médica a domicilio</footer>
    </body>
    </html>
    """
    return HTMLResponse(html)


    


#chat---------------------------------------------------------------
from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/ws/chat/{consulta_id}/{remitente_tipo}/{remitente_id}")
async def chat_ws(websocket: WebSocket, consulta_id: int, remitente_tipo: str, remitente_id: str):
    await websocket.accept()

    if consulta_id not in active_chats:
        active_chats[consulta_id] = []
    active_chats[consulta_id].append(websocket)

    print(f"✅ WS conectado consulta {consulta_id}: {remitente_tipo} {remitente_id}")

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    try:
        while True:
            try:
                data = await websocket.receive_json()
                mensaje = data.get("mensaje")
                if not mensaje:
                    continue
                print(f"📥 Recibido de {remitente_tipo} {remitente_id}: {mensaje}")

                # Guardar en DB
                try:
                    cur.execute("""
                        INSERT INTO mensajes_chat (consulta_id, remitente_tipo, remitente_id, mensaje)
                        VALUES (%s,%s,%s,%s) RETURNING id, creado_en
                    """, (consulta_id, remitente_tipo, remitente_id, mensaje))
                    row = cur.fetchone(); conn.commit()
                except Exception as e:
                    print(f"❌ Error guardando en DB: {e}")
                    conn.rollback()
                    continue

                msg_obj = {
                    "id": row[0],
                    "consulta_id": consulta_id,
                    "remitente_tipo": remitente_tipo,
                    "remitente_id": remitente_id,
                    "mensaje": mensaje,
                    "creado_en": format_datetime_arg(row[1])
                }

                print(f"📤 Reenviando a {len(active_chats[consulta_id])} sockets: {msg_obj}")
                for conn_ws in active_chats[consulta_id]:
                    try:
                        await conn_ws.send_json(msg_obj)
                    except Exception as e:
                        print(f"⚠️ Error enviando a cliente: {e}")

                # 🔔 Notificación push al otro participante
                try:
                    if remitente_tipo == "paciente":
                        cur.execute("""
                            SELECT m.fcm_token
                            FROM consultas c
                            JOIN medicos m ON c.medico_id = m.id
                            WHERE c.id = %s
                        """, (consulta_id,))
                        row_push = cur.fetchone()
                        if row_push and row_push[0]:
                            enviar_push(
                                row_push[0],
                                "💬 Nuevo mensaje de paciente",
                                mensaje[:80],
                                {"tipo": "chat", "consulta_id": str(consulta_id)}
                            )
                    else:  # médico/enfermero → notificar paciente
                        cur.execute("""
                            SELECT u.fcm_token
                            FROM consultas c
                            JOIN users u ON u.id = c.paciente_uuid
                            WHERE c.id = %s
                        """, (consulta_id,))
                        row_push = cur.fetchone()
                        if row_push and row_push[0]:
                            enviar_push(
                                row_push[0],
                                "💬 Nuevo mensaje del profesional",
                                mensaje[:80],
                                {"tipo": "chat", "consulta_id": str(consulta_id)}
                            )
                except Exception as e:
                    print(f"⚠️ Error enviando push: {e}")

            except Exception as e:
                print(f"⚠️ Error en loop WS: {e}")
                break

    except WebSocketDisconnect:
        print(f"❌ WS desconectado consulta {consulta_id}: {remitente_tipo} {remitente_id}")
    finally:
        active_chats[consulta_id].remove(websocket)
        if not active_chats[consulta_id]:
            del active_chats[consulta_id]
        cur.close()
        conn.close()

@app.get("/consultas/{consulta_id}/chat")
def historial_chat(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, remitente_tipo, remitente_id, mensaje, creado_en
        FROM mensajes_chat
        WHERE consulta_id=%s
        ORDER BY creado_en ASC
    """, (consulta_id,))
    rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "remitente_tipo": r[1],
            "remitente_id": r[2],
            "mensaje": r[3],
            "creado_en": format_datetime_arg(r[4])
        }
        for r in rows
    ]  # 👈 nunca None, siempre []



# 🧾 1️⃣ Registrar un pago de consulta --------------------------------------------------------------------------------------------------------------------------
class PagoConsultaIn(BaseModel):
    consulta_id: int
    medico_id: int
    paciente_uuid: str


@app.post("/consultas/{consulta_id}/pago")
def registrar_pago(consulta_id: int, data: PagoConsultaIn, db=Depends(get_db)):
    cur = db.cursor()

    # 1️⃣ Buscar el método de pago guardado en la consulta
    cur.execute("SELECT metodo_pago FROM consultas WHERE id = %s", (consulta_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=400, detail="No se encontró el método de pago para esta consulta")

    metodo_pago = row[0].lower().strip()

    # 2️⃣ Calcular montos según método de pago
    monto_total = 30000
    if metodo_pago == "efectivo":
        medico_neto = 30000
        docya_comision = 6000
        saldo_delta = -6000  # médico le debe a DocYa
    else:  # débito o crédito
        medico_neto = 24000
        docya_comision = 6000
        saldo_delta = 24000  # DocYa le debe al médico

    # 3️⃣ Registrar el pago
    cur.execute("""
        INSERT INTO pagos_consulta 
        (consulta_id, medico_id, paciente_uuid, metodo_pago, monto_total, medico_neto, docya_comision)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (data.consulta_id, data.medico_id, data.paciente_uuid,
          metodo_pago, monto_total, medico_neto, docya_comision))

    # 4️⃣ Actualizar o crear saldo del médico
    cur.execute("SELECT saldo FROM saldo_medico WHERE medico_id = %s", (data.medico_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE saldo_medico SET saldo = saldo + %s WHERE medico_id = %s",
                    (saldo_delta, data.medico_id))
    else:
        cur.execute("INSERT INTO saldo_medico (medico_id, saldo) VALUES (%s, %s)",
                    (data.medico_id, saldo_delta))

    # 5️⃣ Marcar la consulta como completada
    cur.execute("UPDATE consultas SET estado = 'finalizada' WHERE id = %s", (consulta_id,))

    db.commit()
    db.close()

    return {
        "ok": True,
        "consulta_id": consulta_id,
        "metodo_pago": metodo_pago,
        "docya_comision": docya_comision,
        "medico_neto": medico_neto,
        "saldo_delta": saldo_delta,
        "mensaje": (
            "Consulta registrada: el médico debe a DocYa $6.000"
            if metodo_pago == "efectivo"
            else "Consulta registrada: DocYa debe pagar $24.000 al médico"
        )
    }

    #💰 2️⃣ Consultar saldo del médico
@app.get("/medicos/{medico_id}/saldo")
def obtener_saldo(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT saldo FROM saldo_medico WHERE medico_id = %s", (medico_id,))
    row = cur.fetchone()

    saldo = float(row[0]) if row else 0.0
    estado = (
        "DocYa le debe" if saldo > 0 
        else "Debe a DocYa" if saldo < 0 
        else "Saldo en cero"
    )

    return {"medico_id": medico_id, "saldo": saldo, "estado": estado}

#📋 3️⃣ Listar pagos del médico
@app.get("/medicos/{medico_id}/pagos")
def listar_pagos_medico(medico_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, consulta_id, metodo_pago, monto_total, medico_neto, docya_comision, fecha
        FROM pagos_consulta
        WHERE medico_id = %s
        ORDER BY fecha DESC
    """, (medico_id,))
    rows = cur.fetchall()
    db.close()

    return [
        {
            "id": r[0],
            "consulta_id": r[1],
            "metodo_pago": r[2],
            "monto_total": float(r[3]),
            "medico_neto": float(r[4]),
            "docya_comision": float(r[5]),
            "fecha": r[6].strftime("%d/%m/%Y %H:%M")
        } for r in rows
    ]
#🧾 4️⃣ Registrar una liquidación
class LiquidacionIn(BaseModel):
    periodo_inicio: date
    periodo_fin: date
    monto_pagado: float

@app.post("/medicos/{medico_id}/liquidar")
def liquidar_medico(medico_id: int, data: LiquidacionIn):
    db = get_db()
    cur = db.cursor()

    # Insertar la liquidación
    cur.execute("""
        INSERT INTO liquidaciones_medico (medico_id, periodo_inicio, periodo_fin, monto_pagado)
        VALUES (%s, %s, %s, %s)
    """, (medico_id, data.periodo_inicio, data.periodo_fin, data.monto_pagado))

    # Actualizar saldo
    cur.execute("UPDATE saldo_medico SET saldo = saldo - %s WHERE medico_id = %s",
                (data.monto_pagado, medico_id))

    db.commit()
    db.close()

    return {"ok": True, "mensaje": f"Liquidación registrada por ${data.monto_pagado}"}

templates = Jinja2Templates(directory="templates")

@app.get("/inversores", response_class=HTMLResponse)
async def inversores(request: Request):
    return templates.TemplateResponse("inversores.html", {"request": request})

@app.get("/flujo", response_class=HTMLResponse)
async def flujo(request: Request):
    return templates.TemplateResponse("flujo.html", {"request": request})

@app.put("/admin/validar_matricula/{medico_id}")
def validar_matricula(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE medicos SET matricula_validada = TRUE WHERE id = %s", (medico_id,))
    db.commit()
    return {"ok": True, "mensaje": f"Matrícula del médico {medico_id} validada ✅"}

@app.put("/admin/desvalidar_matricula/{medico_id}")
def desvalidar_matricula(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE medicos SET matricula_validada = FALSE WHERE id = %s", (medico_id,))
    db.commit()
    return {"ok": True, "mensaje": f"Matrícula del médico {medico_id} marcada como NO válida 🚫"}
# =========================================================
# 🗺️ LOCALIDADES - API DOCYA
# =========================================================

@app.get("/localidades/{provincia}")
def obtener_localidades(provincia: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT nombre FROM localidades WHERE provincia = %s ORDER BY nombre ASC", (provincia,))
    results = cur.fetchall()
    if results:
        return {"provincia": provincia, "localidades": [r[0] for r in results]}

    try:
        # 🔗 API nacional (trae todas, filtramos por provincia)
        url = "https://apis.datos.gob.ar/georef/api/v2.0/localidades.json?campos=nombre,provincia&max=5000"
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            raise HTTPException(status_code=500, detail="Error en API externa")

        data = res.json()
        localidades = data.get("localidades", [])

        # 🔍 Filtrar solo las que pertenecen a la provincia solicitada
        localidades_filtradas = [
            l["nombre"]
            for l in localidades
            if l.get("provincia", {}).get("nombre", "").lower() == provincia.lower()
        ]

        # 💾 Guardar en BD
        for nombre in localidades_filtradas:
            cur.execute(
                "INSERT INTO localidades (nombre, provincia) VALUES (%s, %s)",
                (nombre, provincia),
            )
        db.commit()

        print(f"📍 {len(localidades_filtradas)} localidades guardadas para {provincia}")
        return {"provincia": provincia, "localidades": localidades_filtradas}

    except Exception as e:
        print(f"⚠️ Error al obtener localidades de {provincia}: {e}")
        raise HTTPException(status_code=500, detail=f"No se pudieron cargar las localidades de {provincia}")

@app.post("/auth/medico/{medico_id}/firma")
def subir_firma_digital(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    """
    Sube una imagen de firma digital del médico a Cloudinary y la guarda en la base.
    """
    try:
        if not file.content_type or not file.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="El archivo debe ser una imagen válida (PNG/JPG)")

        result = cloudinary.uploader.upload(
            file.file,
            folder=f"docya/firmas/{medico_id}",
            public_id=f"firma_{medico_id}",
            overwrite=True,
            resource_type="image"
        )

        firma_url = result.get("secure_url")
        if not firma_url:
            raise HTTPException(status_code=500, detail="Error al obtener URL de Cloudinary")

        cur = db.cursor()
        cur.execute("""
            UPDATE medicos 
            SET firma_url = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING id, firma_url
        """, (firma_url, medico_id))
        row = cur.fetchone()
        db.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Profesional no encontrado")

        return {"ok": True, "firma_url": row[1]}
    except Exception as e:
        print(f"⚠️ Error al subir firma del médico {medico_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al subir la firma: {str(e)}")

@app.get("/pacientes/{paciente_uuid}/archivos")
def listar_archivos_paciente(paciente_uuid: str, db=Depends(get_db)):
    """
    Devuelve todas las recetas y certificados del paciente
    en formato compatible con el frontend Flutter.
    """
    cur = db.cursor()

    # 🧾 Recetas
    cur.execute("""
        SELECT r.id, r.consulta_id, r.creado_en,
               m.full_name AS medico, m.especialidad
        FROM recetas r
        JOIN consultas c ON c.id = r.consulta_id
        JOIN medicos m ON m.id = c.medico_id
        WHERE r.paciente_uuid = %s
    """, (paciente_uuid,))
    recetas = []
    for r in cur.fetchall():
        fecha = r[2].strftime("%d/%m/%Y %H:%M") if r[2] else "—"
        recetas.append({
            "tipo": "Receta",  # 👈 con mayúscula
            "id": r[0],
            "consulta_id": r[1],
            "fecha": fecha,
            "doctor": r[3],    # 👈 el front usa "doctor"
            "especialidad": r[4],
            "url": f"https://docya-railway-production.up.railway.app/consultas/{r[1]}/receta"
        })

    # 📄 Certificados
    cur.execute("""
        SELECT c.id, c.consulta_id, c.creado_en,
               m.full_name AS medico, m.especialidad
        FROM certificados c
        JOIN medicos m ON m.id = c.medico_id
        WHERE c.paciente_uuid = %s
    """, (paciente_uuid,))
    certificados = []
    for c in cur.fetchall():
        fecha = c[2].strftime("%d/%m/%Y %H:%M") if c[2] else "—"
        certificados.append({
            "tipo": "Certificado",  # 👈 con mayúscula
            "id": c[0],
            "consulta_id": c[1],
            "fecha": fecha,
            "doctor": c[3],        # 👈 coincide con tu ListTile
            "especialidad": c[4],
            "url": f"https://docya-railway-production.up.railway.app/consultas/{c[1]}/certificado"
        })

    # 🔄 Unificar y ordenar por fecha (más recientes primero)
    archivos = recetas + certificados
    archivos.sort(key=lambda x: x["fecha"], reverse=True)

    return archivos

# --------------------------------------------------------------------------------
# ============================================================
# DOCYA - SISTEMA DE PAGOS COMPLETO (Checkout Pro + Webhook)
# ============================================================

# ============================================================
# DOCYA - SISTEMA DE PAGOS COMPLETO (Checkout Pro + Webhook)
# ============================================================

import requests
import psycopg2
from fastapi import APIRouter, HTTPException, Depends

router = APIRouter()

ACCESS_TOKEN = "TEST-1283715201688491-111914-f6b44560371df235c8338df2e1deffdb-69224828"      # ⚠ Cambiar para producción


# ============================================================
# 🔌 USAMOS TU MÉTODO DE CONEXIÓN ORIGINAL
# ============================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()


# ============================================================
# 🔵 1) CREAR PREFERENCE DE PAGO
# ============================================================
@app.post("/pagos/preautorizar")
def crear_preference(data: dict, db = Depends(get_db)):

    consulta_id = data.get("consulta_id")
    monto = float(data["monto"])
    email = data["email"]

    url = "https://api.mercadopago.com/checkout/preferences"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "items": [
            {
                "title": "Consulta médica a domicilio - DOCYA",
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": monto
            }
        ],
        "payer": {"email": email},
        "external_reference": str(consulta_id),
        "back_urls": {
            "success": "docya://pago_exitoso",
            "failure": "docya://pago_fallido",
            "pending": "docya://pago_pendiente"
        },
        "auto_return": "approved"
    }

    r = requests.post(url, headers=headers, json=payload).json()

    if "id" not in r:
        raise HTTPException(status_code=400, detail=r)

    return {
        "status": "preference_ok",
        "preference_id": r["id"],
        "init_point": r["init_point"]
    }


# ============================================================
# 🔔 2) WEBHOOK MP → Recibe payment_id
# ============================================================
@app.post("/webhook/mp")
def webhook_mp(payload: dict, db = Depends(get_db)):

    payment_id = payload.get("data", {}).get("id")
    if not payment_id:
        return {"received": False}

    # obtener info del pago
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    info = requests.get(url, headers=headers).json()

    estado = info.get("status")
    consulta_id = info.get("external_reference")

    if not consulta_id:
        print("⚠ ERROR: Payment sin external_reference")
        return {"received": False}

    # guardar en consulta
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET payment_id = %s,
            payment_status = %s
        WHERE id = %s
    """, (payment_id, estado, consulta_id))
    db.commit()
    cur.close()

    print(f"🔔 Webhook MP: consulta {consulta_id} → pago {payment_id} → {estado}")

    return {"received": True}


# ============================================================
# 🟢 3) CAPTURAR (cuando un médico acepta)
# ============================================================
@app.post("/pagos/capturar")
def capturar_pago(data: dict, db = Depends(get_db)):

    consulta_id = data["consulta_id"]

    # obtener payment_id
    cur = db.cursor()
    cur.execute("SELECT payment_id FROM consultas WHERE id = %s", (consulta_id,))
    row = cur.fetchone()
    cur.close()

    if not row or not row[0]:
        raise HTTPException(status_code=400, detail="❌ Payment no encontrado")

    payment_id = row[0]

    # capturar si es tarjeta de crédito
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {"capture": True}

    r = requests.put(url, headers=headers, json=payload).json()
    estado = r.get("status")

    if estado not in ["approved"]:
        raise HTTPException(status_code=400, detail=r)

    # marcar como pagada
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET payment_status = %s, pagado = TRUE
        WHERE id = %s
    """, (estado, consulta_id))
    db.commit()
    cur.close()

    return {"status": "capturado", "payment_status": estado}


# ============================================================
# 🔴 4) CANCELAR (si no hay médicos)
# ============================================================
@app.post("/pagos/cancelar")
def cancelar_pago(data: dict, db = Depends(get_db)):

    consulta_id = data["consulta_id"]

    # obtener payment
    cur = db.cursor()
    cur.execute("SELECT payment_id FROM consultas WHERE id = %s", (consulta_id,))
    row = cur.fetchone()
    cur.close()

    if not row or not row[0]:
        raise HTTPException(status_code=400, detail="Payment no encontrado")

    payment_id = row[0]

    # cancelar en MP
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {"status": "cancelled"}

    requests.put(url, headers=headers, json=payload).json()

    # actualizar BD
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET payment_status = %s
        WHERE id = %s
    """, ("cancelled", consulta_id))
    db.commit()
    cur.close()

    return {"status": "cancelado"}



#MANEJO DE VERSIONES PARA OBLIGAR A ACTUALIZAR LA APP --------------------------------------------
@app.get("/app/check_update")
def check_update(version: str, app: str = "paciente", db=Depends(get_db)):
    cur = db.cursor()

    cur.execute("""
        SELECT 
            latest_version,
            min_supported_version_paciente,
            min_supported_version_pro,
            mensaje,
            url_android,
            url_ios,
            url_android_pro,
            url_ios_pro
        FROM app_versions
        ORDER BY id DESC LIMIT 1
    """)
    row = cur.fetchone()

    if not row:
        raise HTTPException(500, "Sin configuración de versiones")

    latest, minPac, minPro, mensaje, urlA, urlI, urlAPro, urlIPro = row

    def parse(v): return list(map(int, v.split(".")))

    # Selección de versión mínima según QUÉ APP consulta
    min_required = minPro if app == "pro" else minPac

    force_update = parse(version) < parse(min_required)

    # URLs según el tipo de app
    url_android_final = urlAPro if app == "pro" else urlA
    url_ios_final = urlIPro if app == "pro" else urlI

    return {
        "force_update": force_update,
        "latest_version": latest,
        "min_supported_version": min_required,
        "mensaje": mensaje,
        "url_android": url_android_final,
        "url_ios": url_ios_final
    }



