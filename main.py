# ====================================================
# 📌 IMPORTS Y CONFIGURACIÓN INICIAL
# ==================================================
import os
import jwt
import psycopg2
from weasyprint import HTML, CSS
from fastapi.templating import Jinja2Templates
import json
import math
import requests
from datetime import datetime, timedelta, date
from typing import Optional, Dict
from uuid import UUID
from zoneinfo import ZoneInfo
from fastapi import (
    FastAPI, HTTPException, Depends, Query,
    File, UploadFile, WebSocket, WebSocketDisconnect, Request
)
# ====================================================
# 🌐 CONEXIONES ACTIVAS (WEBSOCKETS)
# ====================================================
active_medicos: Dict[int, WebSocket] = {}
active_chats: Dict[int, list[WebSocket]] = {}
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

import cloudinary
import cloudinary.uploader

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account

import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from zoneinfo import ZoneInfo


# ====================================================
# 📊 EVENTOS / MONITOREO DOCYA
# ====================================================
import requests

MONITORING_URL = os.getenv("MONITORING_URL", "https://docya-monitoreo-production.up.railway.app/api/events")

def send_event(event_type: str, payload: dict):
    """Envía un evento al microservicio DocYa-Monitoreo."""
    try:
        data = {
            "event_type": event_type,
            "payload": payload,
            "source": "docya-backend"
        }
        r = requests.post(MONITORING_URL, json=data, timeout=3)
        if r.status_code != 200:
            print(f"⚠️ Error enviando evento {event_type}: {r.text}")
        else:
            print(f"✅ Evento enviado: {event_type}")
    except Exception as e:
        print(f"⚠️ Error conectando con monitor: {e}")

# ====================================================        


def format_datetime_arg(dt):
    if not dt:
        return None
    # Convertir a Argentina
    dt = dt.astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
    # Formato DD/MM/YYYY HH:MM
    return dt.strftime("%d/%m/%Y %H:%M")


load_dotenv()

# ====================================================
# 🔧 CONFIGURACIONES GENERALES
# ====================================================
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET", "change_me")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    try:
        yield conn
    finally:
        conn.close()

def create_access_token(payload: dict, expires_minutes: int = JWT_EXPIRE_MINUTES):
    to_encode = payload.copy()
    expire = now_argentina() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")

def now_argentina():
    return datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))

# Configuración Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# Configuración Firebase FCM
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
# 🚀 APP PRINCIPAL
# ====================================================
app = FastAPI(title="DocYa API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# ====================================================
# 🔑 AUTENTICACIÓN (PACIENTES)
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
# --- Valoraciones ---
class ValoracionIn(BaseModel):
    paciente_uuid: str
    medico_id: Optional[int] = None
    enfermero_id: Optional[int] = None
    puntaje: int
    comentario: Optional[str] = None

@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}


from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import HTTPException, Depends

@app.post("/auth/register")
def register(data: RegisterIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    password_hash = pwd_context.hash(data.password)
    try:
        cur.execute("""
            INSERT INTO users (
                email, full_name, password_hash,
                dni, telefono, pais, provincia, localidad, fecha_nacimiento,
                acepto_condiciones, fecha_aceptacion, version_texto, validado,
                role
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s)
            RETURNING id, full_name
        """, (
            data.email.lower(), data.full_name.strip(), password_hash,
            data.dni, data.telefono, data.pais, data.provincia, data.localidad,
            data.fecha_nacimiento, data.acepto_condiciones,
            datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")) if data.acepto_condiciones else None,
            "v1.0",
            "patient"   # 👈 importante: rol fijo para pacientes
        ))
        user_id, full_name = cur.fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno en registro: {e}")

    # 👇 Enviar mail de activación
    try:
        enviar_email_validacion_paciente(data.email.lower(), user_id, full_name)
    except Exception as e:
        print("⚠️ Error enviando email validación paciente:", e)

    return {
        "ok": True,
        "mensaje": "✅ Registro exitoso. Revisa tu correo para activar la cuenta.",
        "user_id": str(user_id),  # lo mando como string por si es UUID
        "full_name": full_name,
        "role": "patient"
    }

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
    link_activacion = f"https://docya.com.ar/auth/activar_paciente?token={token}"

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
    cur.execute(
        "SELECT id, full_name, password_hash, role FROM users WHERE email=%s",
        (data.email.lower(),)
    )
    row = cur.fetchone()
    if not row or not pwd_context.verify(data.password, row[2]):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    token = create_access_token(
        {"sub": str(row[0]), "email": data.email.lower(), "role": row[3]}
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": row[0],          # 👈 ahora devuelve int
            "full_name": row[1]
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

    # Validar email y matrícula únicos
    cur.execute("SELECT id FROM medicos WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
    cur.execute("SELECT id FROM medicos WHERE matricula=%s", (data.matricula,))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    password_hash = pwd_context.hash(data.password)

    cur.execute("""
        INSERT INTO medicos (
            full_name,email,password_hash,matricula,especialidad,tipo,telefono,
            provincia,localidad,dni,foto_perfil,foto_dni_frente,foto_dni_dorso,selfie_dni,validado
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
        RETURNING id, full_name, tipo
    """, (
        data.full_name.strip(), data.email.lower(), password_hash,
        data.matricula, data.especialidad, data.tipo, data.telefono,
        data.provincia, data.localidad, data.dni,
        data.foto_perfil, data.foto_dni_frente, data.foto_dni_dorso, data.selfie_dni
    ))

    medico_id, full_name, tipo = cur.fetchone()
    db.commit()

    # 👇 Enviar mail validación
    try:
        enviar_email_validacion(data.email.lower(), medico_id, full_name)
    except Exception as e:
        print("⚠️ Error enviando email validación:", e)
    send_event("medico_registrado", {
        "medico_id": medico_id,
        "nombre": full_name,
        "email": data.email.lower(),
        "especialidad": data.especialidad,
        "tipo": tipo
    })
    

    return {
        "ok": True,
        "mensaje": f"Registro exitoso como {tipo}. ✅ Revisa tu correo para activar tu cuenta.",
        "medico_id": medico_id,
        "full_name": full_name,
        "tipo": tipo
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
    link_activacion = f"https://docya.com.ar/auth/activar_medico?token={token}"

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
        sender={"email": "soporte@docya.com.ar", "name": "DocYa"},  # 👈 mejor usar dominio verificado
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
    email: EmailStr
    password: str

@app.post("/auth/login_medico")
def login_medico(data: LoginMedicoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute(
        "SELECT id, full_name, password_hash, validado, tipo FROM medicos WHERE email=%s",
        (data.email.lower(),)
    )
    row = cur.fetchone()
    if not row or not pwd_context.verify(data.password, row[2]):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")
    if not row[3]:
        raise HTTPException(status_code=403, detail="Cuenta aún no validada")

    token = create_access_token(
        {"sub": str(row[0]), "email": data.email.lower(), "role": row[4]}  # 👈 role = tipo
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "medico_id": row[0],
        "full_name": row[1],
        "tipo": row[4],   # 👈 ahora llega al frontend
        "medico": {
            "id": row[0],
            "full_name": row[1],
            "validado": True,
            "tipo": row[4]
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


@app.get("/auth/medico/{medico_id}/stats")
def medico_stats(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # traer tipo del profesional
    cur.execute("SELECT tipo FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    tipo = row[0]

    # consultas finalizadas este mes
    cur.execute("""
        SELECT COUNT(*)
        FROM consultas
        WHERE medico_id=%s AND estado='finalizada'
          AND DATE_TRUNC('month', creado_en) = DATE_TRUNC('month', CURRENT_DATE)
    """, (medico_id,))
    consultas = cur.fetchone()[0] or 0

    # definir tarifa según tipo
    tarifa = 24000 if tipo == "medico" else 15000

    ganancias = consultas * tarifa

    return {
        "consultas": int(consultas),
        "ganancias": int(ganancias),
        "tipo": tipo
    }


class FcmTokenIn(BaseModel):
    fcm_token: str

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
               alias_cbu, matricula, foto_perfil, tipo
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
        "tipo": row[8]
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


@app.post("/consultas/solicitar")
async def solicitar_consulta(data: SolicitarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()

    # Buscar profesional más cercano disponible del tipo solicitado
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
        ORDER BY distancia ASC
        LIMIT 1
    """, (data.lat, data.lng, data.lat, data.tipo))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No hay {data.tipo}s disponibles")

    profesional_id, profesional_nombre, profesional_lat, profesional_lng, tipo, distancia = row

    # Insertar consulta vinculada al profesional encontrado (médico o enfermero)
    cur.execute("""
        INSERT INTO consultas (paciente_uuid, medico_id, estado, motivo, direccion, lat, lng)
        VALUES (%s,%s,'pendiente',%s,%s,%s,%s)
        RETURNING id, creado_en
    """, (str(data.paciente_uuid), profesional_id, data.motivo, data.direccion, data.lat, data.lng))
    consulta_id, creado_en = cur.fetchone()
    db.commit()

    # 🔔 Notificación WS en tiempo real
    if profesional_id in active_medicos:
        try:
            await active_medicos[profesional_id].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id,
                "paciente_uuid": str(data.paciente_uuid),
                "paciente_nombre": None,  # opcional, si querés enviar nombre
                "motivo": data.motivo,
                "direccion": data.direccion,
                "lat": data.lat,
                "lng": data.lng,
                "distancia_km": round(distancia, 2),
                "profesional_tipo": tipo,
                "creado_en": str(creado_en)
            })
        except Exception as e:
            print(f"⚠️ WS error: {e}")

    # 🔔 Push notification
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (profesional_id,))
    row = cur.fetchone()
    if row and row[0]:
        try:
            enviar_push(row[0], "📢 Nueva consulta", f"{data.motivo}", {
                "tipo": "consulta_nueva",
                "consulta_id": str(consulta_id),
                "medico_id": str(profesional_id),   # 👈 genérico, puede ser médico o enfermero
                "profesional_tipo": tipo
            })
        except Exception as e:
            print(f"⚠️ Error push: {e}")
    send_event("consulta_creada", {
        "consulta_id": consulta_id,
        "paciente_uuid": str(data.paciente_uuid),
        "direccion": data.direccion,
        "lat": data.lat,
        "lng": data.lng,
        "tipo": data.tipo,
        "profesional_id": profesional_id,
        "distancia_km": round(distancia, 2)
    })
        

    return {
        "consulta_id": consulta_id,
        "paciente_uuid": str(data.paciente_uuid),
        "profesional": {
            "id": profesional_id,
            "nombre": profesional_nombre,
            "lat": profesional_lat,
            "lng": profesional_lng,
            "tipo": tipo,
            "distancia_km": round(distancia, 2)
        },
        "motivo": data.motivo,
        "direccion": data.direccion,
        "estado": "pendiente",
        "creado_en": format_datetime_arg(creado_en)
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
@app.get("/consultas/asignadas/{medico_id}")
def consultas_asignadas(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id,
               c.paciente_uuid,
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

    distancia = None
    tiempo = None
    if med_lat and med_lng and lat and lng:
        dlat = math.radians(lat - med_lat)
        dlon = math.radians(lng - med_lng)
        a = (math.sin(dlat/2)**2 +
             math.cos(math.radians(med_lat)) *
             math.cos(math.radians(lat)) *
             math.sin(dlon/2)**2)
        distancia = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        tiempo = (distancia / 40) * 60  # 🚗 promedio 40km/h

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
        "distancia_km": round(distancia, 2) if distancia else None,
        "tiempo_estimado_min": round(tiempo) if tiempo else None
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
    medico_id = data.medico_id  # <- ahora lo extraemos del JSON

    # marcar consulta como aceptada
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'aceptada', medico_id = %s
        WHERE id = %s AND estado = 'pendiente'
        RETURNING id
    """, (medico_id, consulta_id))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Consulta no disponible")

    # marcar médico como ocupado
    cur.execute("""
        UPDATE medicos SET disponible = false WHERE id = %s
    """, (medico_id,))
    db.commit()

    return {"ok": True, "consulta_id": consulta_id}


@app.post("/consultas/{consulta_id}/rechazar")
def rechazar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='pendiente', medico_id=NULL WHERE id=%s AND estado='pendiente' RETURNING id", (consulta_id,))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "pendiente"}

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
    send_event("consulta_finalizada", {
        "consulta_id": consulta_id,
        "medico_id": medico_id,
        "estado": "finalizada",
        "fecha_fin": datetime.now().isoformat()
    })

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


# --- POST: crear certificado y guardarlo en la base ---
@app.post("/consultas/{consulta_id}/certificado")
def crear_certificado(consulta_id: int, data: CertificadoIn, db: Session = Depends(get_db)):
    """
    Guarda un nuevo certificado médico en la base de datos
    y devuelve el ID generado.
    """
    cur = db.cursor()
    cur.execute("""
        INSERT INTO certificados (consulta_id, medico_id, paciente_uuid, diagnostico, reposo_dias, observaciones)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (consulta_id, data.medico_id, data.paciente_uuid, data.diagnostico, data.reposo_dias, data.observaciones))
    certificado_id = cur.fetchone()[0]
    db.commit()
    return {"ok": True, "certificado_id": certificado_id}


# --- GET: ver certificado real en navegador ---
from fastapi import Depends, Response, HTTPException
from sqlalchemy.orm import Session
from weasyprint import HTML, CSS
from datetime import datetime
import tempfile, os

@app.get("/consultas/{consulta_id}/certificado")
def ver_certificado_docya(consulta_id: int, db: Session = Depends(get_db)):
    """
    Genera un certificado médico DocYa con datos del paciente y médico,
    formato profesional, firma electrónica y QR de verificación.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT c.medico_id, c.paciente_uuid, c.diagnostico, c.reposo_dias, c.observaciones, c.creado_en,
               m.full_name AS medico_nombre, m.matricula, m.especialidad,
               u.full_name AS paciente_nombre, u.dni, u.fecha_nacimiento
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
        medico_nombre, matricula, especialidad, paciente_nombre, paciente_dni, paciente_nac
    ) = row

    logo_url = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png"
    firma_url = "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/firma_docya_1_sjgxop.png"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya.com.ar/ver_certificado/{consulta_id}"

    fecha_nac = paciente_nac.strftime("%d/%m/%Y") if paciente_nac else "—"
    fecha_emision = creado_en.strftime("%d/%m/%Y %H:%M")

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Certificado Médico</title>
      <style>
        body {{
          font-family: 'Helvetica', Arial, sans-serif;
          background-color: #fff;
          color: #1f2937;
          padding: 40px 60px;
          line-height: 1.6;
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
          font-size: 24px;
          margin-top: 10px;
        }}
        .section {{
          margin-top: 25px;
        }}
        .section-title {{
          color: #14B8A6;
          font-weight: bold;
          font-size: 16px;
          margin-bottom: 8px;
        }}
        .box {{
          border: 1px solid #14B8A6;
          border-radius: 10px;
          background: #f9fdfc;
          padding: 18px 25px;
        }}
        .firma {{
          margin-top: 70px;
          text-align: right;
        }}
        .firma img {{
          height: 80px;
          margin-bottom: -10px;
        }}
        .qr {{
          text-align: left;
          margin-top: 25px;
        }}
        .qr img {{
          height: 100px;
        }}
        footer {{
          text-align: center;
          color: #6b7280;
          font-size: 12px;
          margin-top: 50px;
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

      <div class="section">
        <div class="section-title">Datos del Paciente</div>
        <div class="box">
          <p><b>Nombre:</b> {paciente_nombre}</p>
          <p><b>DNI:</b> {paciente_dni or '—'}</p>
          <p><b>Fecha de Nacimiento:</b> {fecha_nac}</p>
        </div>
      </div>

      <div class="section">
        <div class="section-title">Detalle Médico</div>
        <div class="box">
          <p><b>Diagnóstico:</b> {diagnostico}</p>
          <p><b>Reposo indicado:</b> {reposo_dias} días</p>
          <p><b>Observaciones:</b> {observaciones or 'Sin observaciones adicionales.'}</p>
        </div>
      </div>

      <div class="firma">
        <img src="{firma_url}" alt="Firma digital">
        <p><b>{medico_nombre}</b></p>
        <p>{especialidad}</p>
        <p>M.P. {matricula}</p>
        <p style="color:#14B8A6;">Firma electrónica certificada</p>
      </div>

      <div class="qr">
        <img src="{qr_url}" alt="QR de verificación"><br>
        <small>Verificar autenticidad:<br>docya.com.ar/ver_certificado/{consulta_id}</small>
      </div>

      <footer>
        Documento firmado electrónicamente conforme Ley 25.506 — DocYa © {datetime.now().year}
      </footer>
    </body>
    </html>
    """

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


# --- Certificados medicos fin  -------------------------------------------------------
# ✅ NUEVO ENDPOINT UNIFICADO
# ✅ NUEVO ENDPOINT UNIFICADO CON FILTRO OPCIONAL
@app.get("/pacientes/{paciente_uuid}/archivos")
def listar_archivos_paciente(paciente_uuid: str, db=Depends(get_db)):
    """
    Devuelve todas las recetas y certificados del paciente.
    Soporta filtro opcional con ?tipo=receta o ?tipo=certificado
    """
    cur = db.cursor()

    # --- Recetas ---
    cur.execute("""
        SELECT id, consulta_id, medico_id, creado_en
        FROM recetas
        WHERE paciente_uuid = %s
        ORDER BY creado_en DESC
    """, (paciente_uuid,))
    recetas = [
        {
            "tipo": "Receta médica",
            "doctor": str(r[2]),
            "fecha": r[3].strftime('%d/%m/%Y'),
            "url": f"https://docya-railway-production.up.railway.app/consultas/{r[1]}/receta"
        }
        for r in cur.fetchall()
    ]

    # --- Certificados ---
    cur.execute("""
        SELECT id, consulta_id, medico_id, creado_en
        FROM certificados
        WHERE paciente_uuid = %s
        ORDER BY creado_en DESC
    """, (paciente_uuid,))
    certificados = [
        {
            "tipo": "Certificado médico",
            "doctor": str(r[2]),
            "fecha": r[3].strftime('%d/%m/%Y'),
            "url": f"https://docya-railway-production.up.railway.app/consultas/{r[1]}/certificado"
        }
        for r in cur.fetchall()
    ]

    return recetas + certificados




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
    send_event("receta_creada", {
        "receta_id": receta_id,
        "consulta_id": consulta_id,
        "medico_id": data.medico_id,
        "paciente_uuid": data.paciente_uuid,
        "diagnostico": data.diagnostico
    })

    return {"ok": True, "receta_id": receta_id}


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
    active_medicos[medico_id] = websocket
    print(f"✅ Médico conectado: {medico_id} | Total: {len(active_medicos)}")

    try:
        while True:
            data = await websocket.receive_text()
            print(f"📩 Mensaje recibido de médico {medico_id}: {data}")

            try:
                msg = json.loads(data)
                tipo = msg.get("tipo", "").lower()
            except json.JSONDecodeError:
                tipo = data.strip().lower()

            if tipo == "ping":
                await websocket.send_text("pong")
                await asyncio.sleep(0.05)  # 👈 pequeña pausa de seguridad
                continue

    except Exception as e:
        print(f"❌ Médico desconectado: {medico_id} → {e}")
        if medico_id in active_medicos:
            del active_medicos[medico_id]
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
        qr_data = f"https://docya.com.ar/ver_receta/{consulta_id}"
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
            <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya.com.ar/ver_receta/{consulta_id}">
            <div>Verificación:<br>docya.com.ar/ver_receta/{consulta_id}</div>
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
class UbicacionMedicoIn(BaseModel):
    lat: float
    lng: float
    disponible: bool

@app.post("/medico/{medico_id}/ubicacion")
def actualizar_ubicacion(medico_id: int, data: UbicacionMedicoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET latitud = %s, longitud = %s, disponible = %s, updated_at = NOW()
        WHERE id = %s
    """, (data.lat, data.lng, data.disponible, medico_id))
    db.commit()
    return {"status": "ok"}

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

# --- Ubicación alias ---
class UbicacionIn(BaseModel):
    lat: float
    lng: float
    disponible: bool

@app.post("/medico/{medico_id}/ubicacion")
def alias_ubicacion(medico_id: int, data: UbicacionIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET latitud=%s, longitud=%s, disponible=%s, updated_at=NOW()
        WHERE id=%s RETURNING id
    """, (data.lat, data.lng, data.disponible, medico_id))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {
        "ok": True,
        "medico_id": medico_id,
        "lat": data.lat,
        "lng": data.lng,
        "disponible": data.disponible
    }

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
    link_reset = f"https://docya.com.ar/auth/reset_password?token={token}"

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
        cur.execute("UPDATE medicos SET password = %s WHERE id = %s RETURNING id", (hashed, medico_id))
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
                      <a href="https://docya.com.ar/login" 
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
            sender={"email": "soporte@docya.com.ar", "name": "DocYa Pro"},
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
          <img src="https://api.qrserver.com/v1/create-qr-code/?size=100x100&data=https://docya.com.ar/ver_receta/{receta_id}" alt="QR">
          <p style="font-size:12px; color:#6b7280;">Verificar autenticidad<br>docya.com.ar/ver_receta/{receta_id}</p>
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


# Agregar al final de tu main.py

