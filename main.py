
# ====================================================
# 📌 IMPORTS Y CONFIGURACIÓN INICIAL.
# ====================================================
import os
import json
import math
import jwt
from urllib.parse import quote
from datetime import datetime, timedelta, date, time
import asyncio
import psycopg2
import requests
import uuid
from uuid import UUID
from typing import Optional, Dict
from unidecode import unidecode
from zoneinfo import ZoneInfo
from fastapi import (
    FastAPI, HTTPException, Depends, Query, BackgroundTasks,
    File, UploadFile, WebSocket, WebSocketDisconnect, Request
)
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor

# Google & Email
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# Cloudinary.
import cloudinary
import cloudinary.uploader
import httpx
from database import get_db, get_db_worker
from settings import (
    ARG_TZ,
    ALLOWED_ORIGINS,
    CURRENT_ARGENTINA_WEEK_SQL,
    DATABASE_URL,
    JWT_SECRET,
    MP_ACCESS_TOKEN,
    TELEGRAM_ADMIN_ID,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_GRUPO_ENFERMEROS_ID,
    TELEGRAM_GRUPO_ID,
    TELEGRAM_GRUPO_MEDICOS_ID,
    create_access_token,
    format_datetime_arg,
    get_forced_consulta_price,
    now_argentina,
    pwd_context,
)
# ====================================================
# 🌐 VARIABLES GLOBALES Y CONFIGURACIONES
# ====================================================
active_medicos: Dict[int, WebSocket] = {}
active_chats: Dict[int, list[WebSocket]] = {}

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

# ====================================================
# 🔐 CORS GLOBAL
# ====================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # 🔑 CAMBIO CLAVE (antes True)
    allow_methods=["*"],
    allow_headers=["*"],
)





# ====================================================
# 🩺 INCLUIR RUTA DE MONITOREO
# ====================================================
from monitoreo import router as monitoreo_router
app.include_router(monitoreo_router)
from auth_admin import router as auth_admin_router

app.include_router(auth_admin_router)
from pastillero import (
    procesar_recordatorios_push_pastillero,
    router as pastillero_router,
)
app.include_router(pastillero_router)

from referidos import router as referidos_router
app.include_router(referidos_router)

from chat_ia import router as chat_ia_router
app.include_router(chat_ia_router)

from medicamentos import router as medicamentos_router
app.include_router(medicamentos_router)

from recetario import router as recetario_router
app.include_router(recetario_router)

from auth_router import (
    LoginIn,
    LoginMedicoIn,
    RegisterIn,
    RegisterMedicoIn,
    router as auth_router,
)
app.include_router(auth_router)

from payments_router import router as payments_router
app.include_router(payments_router)
# ====================================================
# 🔑 MODELOS Pydantic (Auth y Valoraciones)
# ====================================================
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



#@app.on_event("startup")
#async def iniciar_timeout_worker():
#    asyncio.create_task(timeout_worker())

#async def timeout_worker():
#    while True:
#        db = None
#        try:
#            db = get_db_worker()           # 👈 conexión NUEVA
#            await procesar_timeouts(db)
#        except Exception as e:
#            print("❌ Error timeout_worker:", e)
#        finally:
#            if db:
#                db.close()                 # 👈 cerrar SIEMPRE
#        await asyncio.sleep(3)
background_tasks = set()

@app.on_event("startup")
async def iniciar_worker():
    task = asyncio.create_task(timeout_worker())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
async def timeout_worker():
    print("🚀 Worker de Timeouts iniciado")
    while True:
        db = None
        try:
            db = get_db_worker()
            # Importante: Asegurar que no empiece con una transacción colgada
            db.rollback() 
            
            while True:
                # 1. Procesar lógica
                await procesar_timeouts(db)
                enviados_pastillero = procesar_recordatorios_push_pastillero(
                    db,
                    enviar_push,
                )
                if enviados_pastillero:
                    print(f"💊 Recordatorios push enviados: {enviados_pastillero}")
                
                # 2. Respiro para la CPU y la DB
                await asyncio.sleep(5) 
                
                # 3. Test de salud + Limpieza de transacciones
                # Esto evita el error "Idle in transaction" y verifica si seguimos conectados
                cur = db.cursor()
                cur.execute("SELECT 1")
                cur.close()
                db.commit() 
                
        except Exception as e:
            print(f"⚠️ Error en timeout_worker (reintentando en 10s): {e}")
            if db:
                try: db.rollback() # Intentar limpiar antes de morir
                except: pass
            await asyncio.sleep(10)
        finally:
            if db:
                try:
                    db.close()
                    print("🔌 Conexión de worker cerrada limpiamente")
                except:
                    pass


# ====================================================
# 🩻 ENDPOINTS BASE / AUTH / USERS
# ====================================================
@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}


# movido a auth_router.py
def register(request: Request, data: RegisterIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    password_hash = pwd_context.hash(data.password)

    # Normalización del nombre
    full_name = data.full_name.strip().title()

    try:
        ref_code = request.cookies.get("ref_code")
        cur.execute("""
            INSERT INTO users (
                email, full_name, password_hash,
                dni, telefono, pais, provincia, localidad,
                fecha_nacimiento, sexo,
                acepto_condiciones, fecha_aceptacion,
                version_texto, validado, role,
                codigo_referido
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s)
            RETURNING id, full_name
        """, (
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
            now_argentina() if data.acepto_condiciones else None,
            "v1.0",
            "patient",
            ref_code   # 👈 ESTE ES EL REFERIDO
        ))

        user_id, full_name = cur.fetchone()
        db.commit()

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno en registro: {e}")

    # Email de validación
    try:
        enviar_email_validacion_paciente(data.email.lower(), user_id, full_name)
    except Exception as e:
        print("⚠️ Error enviando email validación paciente:", e)

    return {
        "ok": True,
        "mensaje": "Registro exitoso. Revisa tu correo para activar tu cuenta.",
        "user_id": str(user_id),
        "full_name": full_name,
        "role": "patient"
    }
#esto es para referidos
from fastapi.responses import RedirectResponse
# movido a auth_router.py
def registro(request: Request):
    return templates.TemplateResponse("registro.html", {"request": request})
# movido a auth_router.py
def referido(request: Request):
    ref = request.query_params.get("ref")

    response = RedirectResponse(url="/registro/paciente")

    if ref:
        response.set_cookie(
            key="ref_code",
            value=ref,
            max_age=60*60*24*30,
            httponly=True,
            samesite="lax",
            domain=".docya.com.ar"
        )

    return response    

class FcmTokenIn(BaseModel):
    fcm_token: str

# movido a auth_router.py
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
                created_at = user["created_at"]
                created_at_arg = created_at.replace(tzinfo=ARG_TZ) if created_at.tzinfo is None else created_at.astimezone(ARG_TZ)
                meses = (now_argentina() - created_at_arg).days // 30
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


# movido a auth_router.py
def guardar_fcm_token_paciente(user_id: str, data: dict, db=Depends(get_db)):
    fcm_token = data.get("fcm_token")
    if not fcm_token:
        return {"detail": "Token FCM faltante"}, 400

    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET fcm_token = %s
            WHERE id = %s
        """, (fcm_token, user_id))
        db.commit()
    except Exception as e:
        db.rollback()
        return {"detail": f"Error guardando token: {e}"}, 500

    return {"ok": True, "message": "Token actualizado"}
    

# ====================================================
# 📸 ENDPOINT: Subir foto de perfil del paciente
# ====================================================
# movido a auth_router.py
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
# movido a auth_router.py
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




# movido a auth_router.py
def login(data: LoginIn, db=Depends(get_db)):
    cur = db.cursor()

    # Acepta email o DNI sin validar formato
    input_value = data.email.strip().lower()
    password = data.password.strip()

    # Buscar al usuario por EMAIL o DNI
    cur.execute("""
        SELECT id, full_name, password_hash, role, validado, email, dni
        FROM users
        WHERE lower(email) = %s OR lower(trim(dni)) = %s
        LIMIT 1
    """, (input_value, input_value))

    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Usuario no encontrado")

    user_id, full_name, password_hash, role, validado, email, dni = row

    if not pwd_context.verify(password, password_hash):
        raise HTTPException(status_code=400, detail="Contraseña incorrecta")

    if not validado:
        raise HTTPException(
            status_code=403,
            detail="Debes validar tu correo electrónico para iniciar sesión."
        )

    token = create_access_token({
        "sub": str(user_id),
        "email": email,
        "role": role,
    })

    return {
        "access_token": token,
        "token_type": "bearer",

        # Compatibilidad con Flutter
        "user_id": str(user_id),
        "full_name": full_name,

        # Formato mejorado
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



# movido a auth_router.py
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



# movido a auth_router.py
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

# movido a auth_router.py
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


# movido a auth_router.py
def validar_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET validado = NOT validado, updated_at=NOW() 
        WHERE id=%s 
        RETURNING id, full_name, tipo, validado
    """, (medico_id,))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": row[0], "nombre": row[1], "tipo": row[2], "validado": row[3]}



# movido a auth_router.py
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



from pydantic import BaseModel, constr


class AliasIn(BaseModel):
    alias: str | None = None
    alias_cbu: str | None = None
#   alias: str



# ================================
# ENDPOINT FINAL
# ================================
# movido a auth_router.py
def actualizar_alias(
    medico_id: int,
    data: AliasIn,
    conn=Depends(get_db)
):
    alias = data.alias_cbu or data.alias
    if not alias:
        raise HTTPException(status_code=400, detail="Alias requerido")

    cursor = conn.cursor()

    cursor.execute("""
        UPDATE medicos
        SET alias_cbu = %s
        WHERE id = %s
    """, (alias, medico_id))

    conn.commit()
    cursor.close()

    return {"ok": True}




# movido a auth_router.py
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

# movido a auth_router.py
def medico_stats(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # 1️⃣ Obtener tipo del profesional (medico/enfermero)
    cur.execute("SELECT tipo FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")

    tipo = row[0].lower().strip()

    # 2️⃣ Calcular semana actual
    hoy_arg = now_argentina().date()
    inicio_semana = hoy_arg - timedelta(days=hoy_arg.weekday())
    fin_semana = inicio_semana + timedelta(days=6)

    # 3️⃣ Consultas finalizadas esta semana
    cur.execute(f"""
        SELECT id, fin_atencion, metodo_pago
        FROM consultas
        WHERE medico_id = %s
        AND estado = 'finalizada'
        AND DATE_TRUNC('week', fin_atencion) = {CURRENT_ARGENTINA_WEEK_SQL}
    """, (medico_id,))

    consultas_finalizadas = cur.fetchall()

    # Inicialización de contadores
    consultas_diurnas = 0
    consultas_nocturnas = 0
    ganancias_diurnas = 0
    ganancias_nocturnas = 0

    # 🔥 Nuevos contadores por método de pago (diurna/nocturna)
    consultas_diurnas_tarjeta = 0
    consultas_nocturnas_tarjeta = 0
    consultas_diurnas_efectivo = 0
    consultas_nocturnas_efectivo = 0

    # Tarifa según tipo
    tarifa_dia = 30000 if tipo == "medico" else 20000
    tarifa_noche = 40000 if tipo == "medico" else 30000

    metodo_contador = {}

    # 4️⃣ Procesar cada consulta
    for consulta_id, fin_atencion, metodo_pago in consultas_finalizadas:
        hora = fin_atencion.time()
        metodo = (metodo_pago or "efectivo").lower().strip()

        # Contar métodos
        metodo_contador[metodo] = metodo_contador.get(metodo, 0) + 1

        # Determinar si es nocturna
        es_nocturna = (hora >= time(22, 0)) or (hora < time(6, 0))

        if es_nocturna:
            consultas_nocturnas += 1
            ganancias_nocturnas += tarifa_noche

            # *** Método nocturno ***
            if metodo == "tarjeta":
                consultas_nocturnas_tarjeta += 1
            else:
                consultas_nocturnas_efectivo += 1

        else:
            consultas_diurnas += 1
            ganancias_diurnas += tarifa_dia

            # *** Método diurno ***
            if metodo == "tarjeta":
                consultas_diurnas_tarjeta += 1
            else:
                consultas_diurnas_efectivo += 1

    # 5️⃣ Ganancia total
    ganancias_total = ganancias_diurnas + ganancias_nocturnas
    consultas_total = consultas_diurnas + consultas_nocturnas

    # 6️⃣ Método más frecuente
    metodo_frecuente = max(metodo_contador, key=metodo_contador.get) if metodo_contador else None

    # 7️⃣ Pagos reales registrados (pie chart)
    cur.execute(f"""
        SELECT 
            COALESCE(metodo_pago, 'efectivo') AS metodo_pago,
            COUNT(*) AS cantidad,
            COALESCE(SUM(medico_neto), 0) AS total
        FROM pagos_consulta
        WHERE medico_id = %s
        AND DATE_TRUNC('week', fecha) = {CURRENT_ARGENTINA_WEEK_SQL}
        GROUP BY metodo_pago
    """, (medico_id,))
    
    rows = cur.fetchall()

    detalle_pagos = {
        row[0]: {
            "cantidad": int(row[1]),
            "monto": float(row[2])
        }
        for row in rows
    }

    db.close()

    return {
        "tipo": tipo,
        "periodo": f"{inicio_semana} → {fin_semana}",

        # Totales
        "consultas": consultas_total,
        "ganancias": ganancias_total,

        # Diurnas / Nocturnas
        "consultas_diurnas": consultas_diurnas,
        "consultas_nocturnas": consultas_nocturnas,
        "ganancias_diurnas": ganancias_diurnas,
        "ganancias_nocturnas": ganancias_nocturnas,

        # 🔥 NUEVOS CAMPOS (para tu nuevo diseño Flutter)
        "consultas_diurnas_tarjeta": consultas_diurnas_tarjeta,
        "consultas_nocturnas_tarjeta": consultas_nocturnas_tarjeta,
        "consultas_diurnas_efectivo": consultas_diurnas_efectivo,
        "consultas_nocturnas_efectivo": consultas_nocturnas_efectivo,

        # Método más frecuente
        "metodo_frecuente": metodo_frecuente,

        # Gráfico de pagos
        "detalle_pagos": detalle_pagos,
    }



# movido a auth_router.py
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


# movido a auth_router.py
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

@app.post("/consultas/{consulta_id}/cancelar_busqueda")
def cancelar_busqueda(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # 1️⃣ Traer estado actual
    cur.execute("""
        SELECT estado, medico_id, mp_payment_id, mp_status
        FROM consultas
        WHERE id = %s
    """, (consulta_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(404, "Consulta no encontrada")

    estado, medico_id, mp_payment_id, mp_status = row

    # 2️⃣ Solo se puede cancelar si está pendiente
    if estado != "pendiente":
        return {
            "ok": True,
            "consulta_id": consulta_id,
            "estado": estado,
            "mensaje": "La consulta ya no está en búsqueda"
        }

    # 3️⃣ Liberar médico si estaba asignado
    if medico_id:
        cur.execute("""
            UPDATE medicos
            SET disponible = TRUE
            WHERE id = %s
        """, (medico_id,))

    # 4️⃣ Cancelar consulta + limpiar reloj
    cur.execute("""
        UPDATE consultas
        SET estado = 'cancelada',
            medico_id = NULL,
            asignada_en = NULL,
            expira_en = NULL
        WHERE id = %s
    """, (consulta_id,))

    if mp_payment_id and mp_status in ("preautorizado", "authorized", "in_process", "pending"):
        try:
            cancel_resp = requests.put(
                f"https://api.mercadopago.com/v1/payments/{mp_payment_id}",
                headers={
                    "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                    "X-Idempotency-Key": str(uuid.uuid4()),
                    "Content-Type": "application/json"
                },
                json={"status": "cancelled"},
                timeout=20,
            )
            print(f"⏱️ Cancelación por timeout {consulta_id}: {cancel_resp.status_code} {cancel_resp.text}")
            if cancel_resp.ok:
                cur.execute("""
                    UPDATE consultas
                    SET mp_status = 'cancelled',
                        mp_preautorizado = FALSE
                    WHERE id = %s
                """, (consulta_id,))
        except Exception as e:
            print(f"⚠️ Error cancelando preautorización por timeout: {e}")

    db.commit()

    print(f"🛑 Consulta {consulta_id} cancelada por el paciente")

    return {
        "ok": True,
        "consulta_id": consulta_id,
        "estado": "cancelada",
        "mensaje": "Búsqueda cancelada por el paciente."
    }



# ====================================================
# 🧠 MOTOR INTELIGENTE DE REASIGNACIÓN – CEREBRO
# ====================================================

import asyncio

# ====================================================
# 🧠 MOTOR INTELIGENTE DE REASIGNACIÓN – CEREBRO
# ====================================================

# ====================================================
# 📲 NOTIFICACIONES TELEGRAM – SIN MÉDICO DISPONIBLE
# ====================================================

async def _telegram_send(chat_id: str, text: str, reply_markup: dict = None):
    """Envía un mensaje por Telegram. Falla silenciosamente si no hay token."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
            )
    except Exception as e:
        print(f"⚠️ Telegram error: {e}")


def _telegram_group_for_tipo(tipo: str) -> str | None:
    """Devuelve el grupo objetivo según el tipo de consulta."""
    tipo_norm = (tipo or "").strip().lower()
    if tipo_norm == "enfermero":
        return TELEGRAM_GRUPO_ENFERMEROS_ID or TELEGRAM_GRUPO_ID
    return TELEGRAM_GRUPO_MEDICOS_ID or TELEGRAM_GRUPO_ID


def _wa_link(telefono: str, texto: str) -> str:
    """Genera un link de WhatsApp con texto pre-escrito para Argentina."""
    from urllib.parse import quote
    # Normalizar número argentino: sacar +, espacios, guiones
    numero = telefono.strip().replace("+", "").replace(" ", "").replace("-", "")
    # Si empieza con 0 (ej: 011...) lo reemplazamos por 54
    if numero.startswith("0"):
        numero = "54" + numero[1:]
    # Si es solo 10 dígitos (ej: 1155667788) agregar 54 adelante
    if len(numero) == 10:
        numero = "54" + numero
    return f"https://wa.me/{numero}?text={quote(texto)}"


async def notificar_sin_medico(consulta_id: int, db):
    """
    Se llama cuando intentar_reasignar() agota todos los ciclos sin éxito.
    Manda un mensaje a Telegram con:
    - Datos completos del paciente
    - Mensaje para el grupo de médicos (listo para copiar)
    - Link de WhatsApp → "seguimos buscando"
    - Link de WhatsApp → "encontramos un médico, volvé a pedir"
    """
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT
                c.id, c.motivo, c.direccion, c.tipo,
                u.full_name, u.telefono, u.email,
                c.lat, c.lng
            FROM consultas c
            LEFT JOIN users u ON c.paciente_uuid::text = u.id::text
            WHERE c.id = %s
        """, (consulta_id,))
        row = cur.fetchone()
        if not row:
            return

        cid, motivo, direccion, tipo, nombre, telefono, email, lat, lng = row
        tipo_label = "Médico" if tipo == "medico" else "Enfermero/a"
        maps_url = f"https://maps.google.com/?q={lat},{lng}" if lat and lng else "Sin coordenadas"
        nombre_str = nombre or "el paciente"

        # Links de WhatsApp pre-escritos
        wa_buscando = wa_encontrado = None
        if telefono:
            texto_buscando = (
                f"Hola {nombre_str}, te habla el equipo de DocYa 👋\n\n"
                f"Estamos buscando un profesional disponible cerca de tu domicilio. "
                f"En breve te confirmamos. ¡Gracias por tu paciencia! 🙏"
            )
            texto_encontrado = (
                f"Hola {nombre_str}, buenas noticias desde DocYa 🎉\n\n"
                f"Encontramos un profesional disponible cerca tuyo. "
                f"Por favor, mantene la App abierta y te lo asignamos de inmediato. ¡Te esperamos! 👩‍⚕️"
            )
            wa_buscando  = _wa_link(telefono, texto_buscando)
            wa_encontrado = _wa_link(telefono, texto_encontrado)

        wa_lineas = ""
        if wa_buscando:
            wa_lineas += f"\n📲 <b>WA — Seguimos buscando:</b>\n{wa_buscando}\n"
        if wa_encontrado:
            wa_lineas += f"\n📲 <b>WA — Encontramos médico:</b>\n{wa_encontrado}\n"

        # ── Mensaje principal al admin ─────────────────────────────────────
        msg_admin = (
            f"🚨 <b>Sin {tipo_label} disponible</b>\n\n"
            f"📋 <b>Consulta #{cid}</b>\n"
            f"👤 Paciente: <b>{nombre or 'N/D'}</b>\n"
            f"📞 Teléfono: {telefono or 'N/D'}\n"
            f"📧 Email: {email or 'N/D'}\n"
            f"🏠 Dirección: {direccion}\n"
            f"📍 Mapa: {maps_url}\n"
            f"📝 Motivo: {motivo}"
            f"{wa_lineas}"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, msg_admin)

        # ── Mensaje listo para copiar y mandar al grupo de médicos ─────────
        msg_grupo = (
            f"🔔 Consulta disponible – DocYa\n\n"
            f"📋 #{cid} | {tipo_label}\n"
            f"🏠 {direccion}\n"
            f"📍 {maps_url}\n"
            f"📝 {motivo}\n\n"
            f"¿Alguien puede tomar esta consulta? Avisame al privado ✅"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, f"👇 <b>Copiá y pegá en el grupo de {tipo_label}es:</b>\n\n{msg_grupo}")

    except Exception as e:
        print(f"⚠️ Error en notificar_sin_medico: {e}")
    finally:
        if not cur.closed:
            cur.close()


async def notificar_consulta_nueva_telegram(consulta_id: int, medico_asignado: bool):
    """Envía notificación Telegram al admin en cuanto ingresa una consulta."""
    db = get_db_worker()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT
                c.id, c.motivo, c.direccion, c.tipo,
                u.full_name, u.telefono, u.email,
                c.lat, c.lng, c.metodo_pago
            FROM consultas c
            LEFT JOIN users u ON c.paciente_uuid::text = u.id::text
            WHERE c.id = %s
        """, (consulta_id,))
        row = cur.fetchone()
        if not row:
            return
        cid, motivo, direccion, tipo, nombre, telefono, email, lat, lng, metodo_pago = row
        tipo_label = "Médico" if tipo == "medico" else "Enfermero/a"
        maps_url = f"https://maps.google.com/?q={lat},{lng}" if lat and lng else "Sin coordenadas"
        nombre_str = nombre or "el paciente"
        estado_str = "✅ Médico asignado" if medico_asignado else "⚠️ Sin médico disponible aún"

        # ── Links de WhatsApp al paciente ──────────────────────────────────
        wa_buscando = wa_encontrado = None
        if telefono:
            texto_buscando = (
                f"Hola {nombre_str}, te habla el equipo de DocYa 👋\n\n"
                f"Estamos buscando un profesional disponible cerca de tu domicilio. "
                f"En breve te confirmamos. ¡Gracias por tu paciencia! 🙏"
            )
            texto_encontrado = (
                f"Hola {nombre_str}, buenas noticias desde DocYa 🎉\n\n"
                f"Encontramos un profesional disponible cerca tuyo. "
                f"Por favor, volvé a solicitar la consulta desde la app y te lo asignamos de inmediato. ¡Te esperamos! 👩‍⚕️"
            )
            wa_buscando  = _wa_link(telefono, texto_buscando)
            wa_encontrado = _wa_link(telefono, texto_encontrado)

        wa_lineas = ""
        if wa_buscando:
            wa_lineas += f"\n📲 <b>WA — Seguimos buscando:</b>\n{wa_buscando}\n"
        if wa_encontrado:
            wa_lineas += f"\n📲 <b>WA — Encontramos médico:</b>\n{wa_encontrado}\n"

        # ── Mensaje principal al admin ──────────────────────────────────────
        msg_admin = (
            f"📥 <b>Nueva consulta ingresada</b>\n\n"
            f"📋 <b>Consulta #{cid}</b>\n"
            f"👤 Paciente: <b>{nombre or 'N/D'}</b>\n"
            f"📞 Teléfono: {telefono or 'N/D'}\n"
            f"📧 Email: {email or 'N/D'}\n"
            f"🏠 Dirección: {direccion}\n"
            f"📍 Mapa: {maps_url}\n"
            f"📝 Motivo: {motivo}\n"
            f"💳 Pago: {metodo_pago}\n"
            f"🩺 Tipo: {tipo_label}\n"
            f"{estado_str}"
            f"{wa_lineas}"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, msg_admin)

        # ── Mensaje para copiar y pegar en el grupo de médicos ─────────────
        msg_grupo = (
            f"🔔 Consulta disponible – DocYa\n\n"
            f"📋 #{cid} | {tipo_label}\n"
            f"🏠 {direccion}\n"
            f"📍 {maps_url}\n"
            f"📝 {motivo}\n\n"
            f"¿Alguien puede tomar esta consulta? Avisame al privado ✅"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, f"👇 <b>Copiá y pegá en el grupo de {tipo_label}es:</b>\n\n{msg_grupo}")

    except Exception as e:
        print(f"⚠️ Error en notificar_consulta_nueva_telegram: {e}")
    finally:
        if not cur.closed:
            cur.close()
        db.close()


def _telegram_group_for_tipo(tipo: str) -> str | None:
    """Devuelve el grupo objetivo según el tipo de consulta."""
    tipo_norm = (tipo or "").strip().lower()
    if tipo_norm == "enfermero":
        return TELEGRAM_GRUPO_ENFERMEROS_ID or TELEGRAM_GRUPO_ID
    return TELEGRAM_GRUPO_MEDICOS_ID or TELEGRAM_GRUPO_ID


async def notificar_sin_medico(consulta_id: int, db):
    """Avisa al admin y al grupo correcto cuando una consulta queda sin asignación."""
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT
                c.id, c.motivo, c.direccion, c.tipo,
                u.full_name, u.telefono, u.email,
                c.lat, c.lng
            FROM consultas c
            LEFT JOIN users u ON c.paciente_uuid::text = u.id::text
            WHERE c.id = %s
            """,
            (consulta_id,),
        )
        row = cur.fetchone()
        if not row:
            return

        cid, motivo, direccion, tipo, nombre, telefono, email, lat, lng = row
        tipo_label = "Médico" if tipo == "medico" else "Enfermero/a"
        maps_url = (
            f"https://maps.google.com/?q={lat},{lng}"
            if lat is not None and lng is not None
            else "Sin coordenadas"
        )
        nombre_str = nombre or "el paciente"

        wa_buscando = wa_encontrado = None
        if telefono:
            texto_buscando = (
                f"Hola {nombre_str}, te habla el equipo de DocYa.\n\n"
                f"Seguimos buscando un profesional disponible cerca de tu domicilio. "
                f"En breve te confirmamos novedades. Gracias por tu paciencia."
            )
            texto_encontrado = (
                f"Hola {nombre_str}, te escribimos desde DocYa.\n\n"
                f"Encontramos un profesional disponible cerca tuyo. "
                f"Volvé a solicitar la consulta desde la app y te lo asignamos de inmediato."
            )
            wa_buscando = _wa_link(telefono, texto_buscando)
            wa_encontrado = _wa_link(telefono, texto_encontrado)

        wa_lineas = ""
        if wa_buscando:
            wa_lineas += f"\n📲 <b>WA - Seguimos buscando:</b>\n{wa_buscando}\n"
        if wa_encontrado:
            wa_lineas += f"\n📲 <b>WA - Profesional encontrado:</b>\n{wa_encontrado}\n"

        msg_admin = (
            f"🚨 <b>Sin {tipo_label} disponible</b>\n\n"
            f"📋 <b>Consulta #{cid}</b>\n"
            f"👤 Paciente: <b>{nombre or 'N/D'}</b>\n"
            f"📞 Teléfono: {telefono or 'N/D'}\n"
            f"📧 Email: {email or 'N/D'}\n"
            f"🏠 Dirección: {direccion}\n"
            f"📍 Mapa: {maps_url}\n"
            f"📝 Motivo: {motivo}"
            f"{wa_lineas}"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, msg_admin)

        msg_grupo = (
            f"🚨 Consulta sin asignar - DocYa\n\n"
            f"📋 #{cid} | {tipo_label}\n"
            f"👤 Paciente: {nombre or 'N/D'}\n"
            f"🏠 {direccion}\n"
            f"📍 {maps_url}\n"
            f"📝 {motivo}\n\n"
            f"¿Algún/a {tipo_label.lower()} disponible puede tomar esta consulta? "
            f"Respondan por este grupo o al admin."
        )
        grupo_chat_id = _telegram_group_for_tipo(tipo)
        if grupo_chat_id:
            await _telegram_send(grupo_chat_id, msg_grupo)
        else:
            await _telegram_send(
                TELEGRAM_ADMIN_ID,
                f"⚠️ No hay grupo configurado para {tipo_label.lower()}s.\n\n{msg_grupo}",
            )
    except Exception as e:
        print(f"⚠️ Error en notificar_sin_medico: {e}")
    finally:
        if not cur.closed:
            cur.close()


async def notificar_consulta_nueva_telegram(consulta_id: int, medico_asignado: bool):
    """Envía aviso al admin y, si no hay asignación, al grupo correcto."""
    db = get_db_worker()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT
                c.id, c.motivo, c.direccion, c.tipo,
                u.full_name, u.telefono, u.email,
                c.lat, c.lng, c.metodo_pago
            FROM consultas c
            LEFT JOIN users u ON c.paciente_uuid::text = u.id::text
            WHERE c.id = %s
            """,
            (consulta_id,),
        )
        row = cur.fetchone()
        if not row:
            return

        cid, motivo, direccion, tipo, nombre, telefono, email, lat, lng, metodo_pago = row
        tipo_label = "Médico" if tipo == "medico" else "Enfermero/a"
        maps_url = (
            f"https://maps.google.com/?q={lat},{lng}"
            if lat is not None and lng is not None
            else "Sin coordenadas"
        )
        estado_str = "✅ Profesional asignado" if medico_asignado else "⚠️ Sin profesional disponible aún"

        msg_admin = (
            f"📥 <b>Nueva consulta ingresada</b>\n\n"
            f"📋 <b>Consulta #{cid}</b>\n"
            f"👤 Paciente: <b>{nombre or 'N/D'}</b>\n"
            f"📞 Teléfono: {telefono or 'N/D'}\n"
            f"📧 Email: {email or 'N/D'}\n"
            f"🏠 Dirección: {direccion}\n"
            f"📍 Mapa: {maps_url}\n"
            f"📝 Motivo: {motivo}\n"
            f"💳 Pago: {metodo_pago}\n"
            f"🩺 Tipo: {tipo_label}\n"
            f"{estado_str}"
        )
        await _telegram_send(TELEGRAM_ADMIN_ID, msg_admin)

        if not medico_asignado:
            msg_grupo = (
                f"🔔 Nueva consulta disponible - DocYa\n\n"
                f"📋 #{cid} | {tipo_label}\n"
                f"👤 Paciente: {nombre or 'N/D'}\n"
                f"🏠 {direccion}\n"
                f"📍 {maps_url}\n"
                f"📝 {motivo}\n"
                f"💳 Pago: {metodo_pago}\n\n"
                f"¿Algún/a {tipo_label.lower()} disponible puede tomar esta consulta? "
                f"Respondan por este grupo o al admin."
            )
            grupo_chat_id = _telegram_group_for_tipo(tipo)
            if grupo_chat_id:
                await _telegram_send(grupo_chat_id, msg_grupo)
            else:
                await _telegram_send(
                    TELEGRAM_ADMIN_ID,
                    f"⚠️ No hay grupo configurado para {tipo_label.lower()}s.\n\n{msg_grupo}",
                )
    except Exception as e:
        print(f"⚠️ Error en notificar_consulta_nueva_telegram: {e}")
    finally:
        if not cur.closed:
            cur.close()
        db.close()


async def intentar_reasignar(
    consulta_id,
    db,
    excluir_medico_id=None,
    max_ciclos=20,
    espera_segundos=3
):
    ciclo = 0

    while ciclo < max_ciclos:
        ciclo += 1
        print(f"🧠 Ciclo de reasignación #{ciclo} | Consulta {consulta_id}")
        
        # Usamos un cursor por ciclo para asegurar limpieza total en Render
        cur = db.cursor()
        
        try:
            # 1️⃣ Verificar estado actual de la consulta
            cur.execute("""
                SELECT lat, lng, tipo, medico_id, estado
                FROM consultas
                WHERE id = %s
            """, (consulta_id,))
            row = cur.fetchone()

            if not row:
                print("❌ Consulta no existe")
                return False

            lat, lng, tipo, medico_actual, estado_actual = row

            # Si el paciente canceló, no tiene sentido seguir
            if estado_actual in ("cancelada", "finalizada"):
                print(f"🛑 Consulta {consulta_id} {estado_actual} — deteniendo reasignación")
                return False

            # Si ya se asignó en el interín, salimos con éxito
            if medico_actual is not None:
                print(f"✅ Consulta {consulta_id} ya asignada al médico {medico_actual}")
                return True

            # 2️⃣ Buscar médicos disponibles
            cur.execute("""
                SELECT
                    id,
                    latitud,
                    longitud,
                    (6371 * acos(
                        cos(radians(%s)) *
                        cos(radians(latitud)) *
                        cos(radians(longitud) - radians(%s)) +
                        sin(radians(%s)) *
                        sin(radians(latitud))
                    )) AS distancia
                FROM medicos
                WHERE
                    disponible = TRUE
                    AND tipo = %s
                    AND latitud IS NOT NULL
                    AND longitud IS NOT NULL
            """, (lat, lng, lat, tipo))

            # 3️⃣ Ordenar por cercanía
            medicos = sorted(cur.fetchall(), key=lambda x: x[3])

            if not medicos:
                print("❌ No hay médicos disponibles en este ciclo")
                db.rollback() # Liberamos transacción antes de dormir
                cur.close()
                await asyncio.sleep(espera_segundos)
                continue

            # 4️⃣ Intentar asignar uno por uno
            for medico_id, mlat, mlng, dist in medicos:
                if excluir_medico_id and medico_id == excluir_medico_id:
                    continue

                print(f"🔑 Intentando médico {medico_id} ({dist:.2f} km)")

                # Intento atómico de UPDATE
                cur.execute("""
                    UPDATE consultas
                    SET
                        medico_id = %s,
                        estado = 'pendiente',
                        asignada_en = NOW(),
                        expira_en = NOW() + INTERVAL '20 seconds'
                    WHERE
                        id = %s
                        AND medico_id IS NULL
                """, (medico_id, consulta_id))

                if cur.rowcount == 0:
                    db.rollback() # Alguien ganó la carrera, reintentamos ciclo
                    continue

                # Confirmamos asignación
                db.commit()

                # 5️⃣ Registrar intento
                cur.execute("""
                    INSERT INTO intentos_asignacion (consulta_id, medico_id)
                    VALUES (%s, %s)
                """, (consulta_id, medico_id))
                db.commit()

                print(f"🔁 Consulta {consulta_id} asignada a médico {medico_id}")

                # 6️⃣ Notificaciones (WS y Push)
                await enviar_notificaciones_asignacion(medico_id, consulta_id, cur)
                
                cur.close()
                return True

        except Exception as e:
            print(f"⚠️ Error en ciclo {ciclo}: {e}")
            db.rollback()
        finally:
            if not cur.closed:
                cur.close()

        # 8️⃣ Esperar antes del próximo ciclo
        await asyncio.sleep(espera_segundos)

    print("🔴 No se logró asignar médico tras múltiples ciclos")
    # Verificar que la consulta no fue cancelada antes de notificar
    try:
        cur_check = db.cursor()
        cur_check.execute("SELECT estado FROM consultas WHERE id = %s", (consulta_id,))
        row_check = cur_check.fetchone()
        cur_check.close()
        if row_check and row_check[0] not in ("cancelada", "finalizada"):
            await notificar_sin_medico(consulta_id, db)
        else:
            print(f"ℹ️ Consulta {consulta_id} ya {row_check[0] if row_check else 'inexistente'} — sin notificación")
    except Exception as e:
        print(f"⚠️ Error enviando notificación sin médico: {e}")
    return False

async def enviar_notificaciones_asignacion(medico_id, consulta_id, cur):
    """Encapsula la lógica de WS y Push para no ensuciar el motor"""
    # WebSocket
    if medico_id in active_medicos:
        try:
            await active_medicos[medico_id]["ws"].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id
            })
            print("📤 WS enviado")
        except Exception as e:
            print("⚠️ WS error:", e)

    # Push Fallback
    try:
        cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (medico_id,))
        token_row = cur.fetchone()
        if token_row and token_row[0]:
            enviar_push(
                token_row[0],
                "📢 Nueva consulta disponible",
                "Tenés una consulta asignada",
                {
                    "tipo": "consulta_nueva",
                    "consulta_id": str(consulta_id),
                    "medico_id": str(medico_id),
                    "origen": "reasignacion"
                }
            )
            print("📤 PUSH enviado")
    except Exception as e:
        print("❌ Error push:", e)


# ====================================================
# ⏳ PROCESADOR DE TIMEOUTS BACKEND
# ====================================================
async def procesar_timeouts(db):
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT id, medico_id
            FROM consultas
            WHERE estado = 'pendiente'
              AND medico_id IS NOT NULL
              AND expira_en IS NOT NULL
              AND expira_en < NOW()
        """)
        vencidas = cur.fetchall()

        if not vencidas:
            db.rollback() # Limpieza de transacción idle
            return

        for consulta_id, medico_id in vencidas:
            print(f"⏳ Timeout BACKEND consulta {consulta_id} médico {medico_id}")

            # 1. Registrar intento fallido
            cur.execute("""
                INSERT INTO intentos_asignacion (consulta_id, medico_id)
                VALUES (%s, %s)
            """, (consulta_id, medico_id))

            # 2. Liberar médico
            cur.execute("UPDATE medicos SET disponible = TRUE WHERE id = %s", (medico_id,))

            # 3. Limpiar consulta (Reset para reasignación)
            cur.execute("""
                UPDATE consultas
                SET medico_id = NULL,
                    asignada_en = NULL,
                    expira_en = NULL
                WHERE id = %s
            """, (consulta_id,))

            db.commit()

            # 4. Disparar motor de reasignación
            # IMPORTANTE: Pasamos el ID del médico que falló para excluirlo en la búsqueda inmediata
            await intentar_reasignar(consulta_id, db, excluir_medico_id=medico_id)

    except Exception as e:
        print(f"❌ Error en procesar_timeouts: {e}")
        db.rollback()
    finally:
        cur.close()





# --- Modelo para solicitar consulta ---
class SolicitarConsultaIn(BaseModel):
    paciente_uuid: UUID
    motivo: str
    direccion: str
    lat: float
    lng: float
    tipo: str = "medico"
    metodo_pago: str
    consulta_id: int | None = None



import requests
import os

FCM_KEY = os.getenv("FCM_SERVER_KEY")

def enviar_fcm(token: str, titulo: str, cuerpo: str, data_extra: dict):
    """
    Envía una notificación FCM a un dispositivo Android/iOS.
    """
    headers = {
        "Authorization": f"key={FCM_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "to": token,
        "priority": "high",
        "notification": {
            "title": titulo,
            "body": cuerpo,
            "sound": "default"
        },
        "data": data_extra
    }

    try:
        r = requests.post(
            "https://fcm.googleapis.com/fcm/send",
            headers=headers,
            json=payload,
            timeout=5
        )
        print(f"🔔 FCM status: {r.status_code} → {r.text[:80]}")
    except Exception as e:
        print(f"❌ Error enviando FCM: {e}")


import psycopg2
import os

def get_db_direct():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("❌ DATABASE_URL no está definida")

    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = False
    return conn


def _validar_perfil_paciente_completo(db, paciente_uuid: str):
    """Bloquea la solicitud si el paciente todavía no completó su perfil global."""
    cur = db.cursor()
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS perfil_completo BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acepta_terminos BOOLEAN DEFAULT FALSE")
    db.commit()
    cur.execute(
        "SELECT COALESCE(perfil_completo, FALSE), COALESCE(acepta_terminos, FALSE) FROM users WHERE id = %s",
        (str(paciente_uuid),),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    if not row[0] or not row[1]:
        raise HTTPException(
            status_code=403,
            detail="Debes completar tu perfil antes de solicitar un profesional.",
        )


async def worker_busqueda_consulta_pendiente(
    consulta_id: int,
    tipo: str,
    espera_segundos: int = 4,
    max_ciclos: int = 15,
):
    """Mantiene la búsqueda de profesionales durante una ventana corta."""
    print(
        f"🔎 Worker de búsqueda extendida iniciado para consulta {consulta_id} ({tipo})"
    )
    db_worker = get_db_worker()
    try:
        await intentar_reasignar(
            consulta_id,
            db_worker,
            excluir_medico_id=None,
            max_ciclos=max_ciclos,
            espera_segundos=espera_segundos,
        )
    except Exception as e:
        print(f"⚠️ Error en worker_busqueda_consulta_pendiente: {e}")
    finally:
        db_worker.close()


@app.post("/consultas/solicitar")
async def solicitar_consulta(
    data: SolicitarConsultaIn,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    cur = db.cursor()
    _validar_perfil_paciente_completo(db, str(data.paciente_uuid))

    # NORMALIZAR MÉTODO DE PAGO
    data.metodo_pago = (
        "tarjeta" if data.metodo_pago in ["debito", "credito", "tarjeta"] else data.metodo_pago
    )

    consulta_id_previa = data.consulta_id

    # Evita crear duplicados cuando el cliente reintenta o hace doble tap
    # sobre el flujo de efectivo. Si ya existe una consulta abierta muy reciente
    # del mismo paciente con los mismos datos, devolvemos esa.
    if data.metodo_pago == "efectivo":
        cur.execute("""
            SELECT id, estado, medico_id
            FROM consultas
            WHERE paciente_uuid = %s
              AND tipo = %s
              AND metodo_pago = 'efectivo'
              AND motivo = %s
              AND direccion = %s
              AND estado IN ('pendiente', 'aceptada', 'en_camino', 'en_domicilio')
              AND creado_en >= NOW() - INTERVAL '2 minutes'
            ORDER BY creado_en DESC
            LIMIT 1
        """, (
            str(data.paciente_uuid),
            data.tipo,
            data.motivo,
            data.direccion,
        ))
        consulta_existente = cur.fetchone()

        if consulta_existente:
            consulta_existente_id, estado_existente, medico_existente_id = consulta_existente
            print(f"♻️ Reutilizando consulta existente {consulta_existente_id} para paciente {data.paciente_uuid}")
            return {
                "consulta_id": consulta_existente_id,
                "estado": estado_existente,
                "profesional": {"id": medico_existente_id} if medico_existente_id else None,
                "duplicado": True,
            }

    # ============================================================
    # 🔵 TARJETA (ACTUALIZA CONSULTA EXISTENTE)
    # ============================================================
    if data.metodo_pago == "tarjeta" and consulta_id_previa:
        print(f"💳 Actualizando consulta previa {consulta_id_previa}")

        cur.execute("""
            SELECT id, full_name, latitud, longitud, tipo,
            (6371 * acos(
                cos(radians(%s)) * cos(radians(latitud)) *
                cos(radians(longitud) - radians(%s)) +
                sin(radians(%s)) * sin(radians(latitud))
            )) AS distancia
            FROM medicos
            WHERE disponible = TRUE
              AND activo = TRUE
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
            LIMIT 1;
        """, (
            data.lat, data.lng, data.lat,
            data.tipo,
            data.lat, data.lng, data.lat
        ))

        row = cur.fetchone()

        # NO HAY PROFESIONAL → REFUND
        if not row:
            print("⚠️ No hay profesionales inmediatos → mantener búsqueda por 60s")

            cur.execute("""
                UPDATE consultas
                SET estado='pendiente',
                    medico_id = NULL,
                    metodo_pago='tarjeta',
                    tipo=%s,
                    asignada_en = NOW(),
                    expira_en = NOW() + INTERVAL '60 seconds'
                WHERE id=%s
            """, (data.tipo, consulta_id_previa))
            db.commit()

            background_tasks.add_task(
                worker_busqueda_consulta_pendiente,
                consulta_id_previa,
                data.tipo,
            )

            return {
                "consulta_id": consulta_id_previa,
                "estado": "pendiente",
                "mensaje": "Seguimos buscando profesionales disponibles durante los próximos segundos.",
                "profesional": None
            }

        # =============================
        # ✔ Hay profesional disponible
        # =============================
        profesional_id, profesional_nombre, profesional_lat, profesional_lng, tipo, distancia = row

        cur.execute("""
            UPDATE consultas
            SET medico_id=%s,
                estado='pendiente',
                motivo=%s,
                direccion=%s,
                lat=%s,
                lng=%s,
                metodo_pago='tarjeta',
                tipo=%s,
                asignada_en = NOW(),
                expira_en = NOW() + INTERVAL '20 seconds'
            WHERE id=%s
            RETURNING creado_en;
        """, (
            profesional_id, data.motivo, data.direccion,
            data.lat, data.lng, data.tipo,
            consulta_id_previa
        ))

        creado_en = cur.fetchone()[0]
        db.commit()

        print(f"🟢 Consulta {consulta_id_previa} asignada al médico {profesional_id}")

        # Registrar intento
        cur.execute("""
            INSERT INTO intentos_asignacion (consulta_id, medico_id)
            VALUES (%s, %s)
        """, (consulta_id_previa, profesional_id))
        db.commit()

        # ---------------------------------------------------------
        # 1) WS REAL TIME
        # ---------------------------------------------------------
        if profesional_id in active_medicos:
            try:
                pro = active_medicos[profesional_id]

                cur.execute("SELECT full_name, telefono FROM users WHERE id=%s",
                            (str(data.paciente_uuid),))
                u = cur.fetchone()
                paciente_nombre = u[0] if u else "Paciente"
                paciente_telefono = u[1] if u else "Sin número"
                # 🔥 NUEVO: timestamp real en milisegundos
                creado_ts = int(creado_en.timestamp() * 1000)
                await pro["ws"].send_json({
                    "tipo": "consulta_nueva",
                    "consulta_id": consulta_id_previa,
                    "paciente_uuid": str(data.paciente_uuid),
                    "paciente_nombre": paciente_nombre,
                    "paciente_telefono": paciente_telefono,
                    "motivo": data.motivo,
                    "direccion": data.direccion,
                    "lat": data.lat,
                    "lng": data.lng,
                    "distancia_km": round(float(distancia), 2),
                    "metodo_pago": "tarjeta",
                    "profesional_tipo": tipo,
                    "creado_en": str(creado_en),
                })
                print("📤 WS enviado")
            except Exception as e:
                print("⚠️ Error WS:", e)

        # ---------------------------------------------------------
        # === DOCYA PUSH INMEDIATO (siempre) ===
        # ---------------------------------------------------------
        cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (profesional_id,))
        row = cur.fetchone()

        if row and row[0]:
            try:
                enviar_push(
                    row[0],
                    "📢 Nueva consulta disponible",
                    data.motivo,
                    {
                        "tipo": "consulta_nueva",
                        "consulta_id": str(consulta_id_previa),
                        "medico_id": str(profesional_id),
                        "origen": "asignacion"
                    }
                )
                print("📤 PUSH enviado (asignación directa)")
            except:
                print("❌ Error enviando push inmediato")

        # ---------------------------------------------------------
        # 2) WATCHDOG TARJETA
        # ---------------------------------------------------------
        async def watchdog_tarjeta():
            await asyncio.sleep(3)
        
            # Si el médico ya está conectado por WS → OK
            if profesional_id in active_medicos:
                print("🟢 Watchdog OK → WS activo")
                return
        
            print("🟡 Watchdog: No WS en 3s → enviar PUSH fallback")
        
            # 🔐 CONEXIÓN AISLADA DEL REQUEST
            db_wd = get_db_direct()
            cur_wd = None
        
            try:
                cur_wd = db_wd.cursor()
        
                # PUSH fallback
                cur_wd.execute(
                    "SELECT fcm_token FROM medicos WHERE id=%s",
                    (profesional_id,)
                )
                row = cur_wd.fetchone()
        
                if row and row[0]:
                    try:
                        enviar_push(
                            row[0],
                            "📢 Nueva consulta",
                            data.motivo,
                            {
                                "tipo": "consulta_nueva",
                                "consulta_id": str(consulta_id_previa),
                                "medico_id": str(profesional_id),
                                "fallback": True
                            }
                        )
                        print("📤 PUSH fallback enviado")
                    except:
                        print("❌ Error push fallback")
        
                # ⏱ Segunda ventana
                await asyncio.sleep(10)
        
                # Verificar estado real de la consulta
                cur_wd.execute(
                    "SELECT estado FROM consultas WHERE id=%s",
                    (consulta_id_previa,)
                )
                estado_row = cur_wd.fetchone()
        
        
            except Exception as e:
                print("❌ Error watchdog_tarjeta:", e)
        
            finally:
                if cur_wd:
                    cur_wd.close()
                db_wd.close()

        
        
        # ---------------------------------------------------------
        # LANZAR WATCHDOG TARJETA (NO BLOQUEANTE)
        # ---------------------------------------------------------
        asyncio.create_task(watchdog_tarjeta())
        asyncio.create_task(notificar_consulta_nueva_telegram(consulta_id_previa, True))

        # ---------------------------------------------------------
        # RETURN FINAL DEL ENDPOINT (NO TOCAR)
        # ---------------------------------------------------------
        return {
            "consulta_id": consulta_id_previa,
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
            "estado": "pendiente"
        }

    # ============================================================
    # 🟩 EFECTIVO (idéntico flujo + push inmediato)
    # ============================================================
    cur.execute("""
        SELECT id, full_name, latitud, longitud, tipo,
        (6371 * acos(
            cos(radians(%s)) * cos(radians(latitud)) *
            cos(radians(longitud) - radians(%s)) +
            sin(radians(%s)) * sin(radians(latitud))
        )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
          AND activo = TRUE
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
        LIMIT 1;
    """, (
        data.lat, data.lng, data.lat,        # cálculo distancia (SELECT)
        data.tipo,
        data.lat, data.lng, data.lat         # filtro radio <= 10 km (WHERE)
    ))


    row = cur.fetchone()

    # No hay profesional
    if not row:
        cur.execute("""
            INSERT INTO consultas (
                paciente_uuid, medico_id, estado, motivo,
                direccion, lat, lng, metodo_pago, tipo
            )
            VALUES (%s,NULL,'pendiente',%s,%s,%s,%s,%s,%s)
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

        print("⚠️ Consulta sin médico disponible")
        asyncio.create_task(notificar_consulta_nueva_telegram(consulta_id, False))

        return {
            "consulta_id": consulta_id,
            "estado": "pendiente",
            "profesional": None
        }

    # ✔ Hay profesional
    profesional_id, profesional_nombre, profesional_lat, profesional_lng, tipo, distancia = row
    
    cur.execute("""
        INSERT INTO consultas (
            paciente_uuid,
            medico_id,
            estado,
            motivo,
            direccion,
            lat,
            lng,
            metodo_pago,
            tipo,
            asignada_en,
            expira_en
        )
        VALUES (
            %s, %s, 'pendiente',
            %s, %s, %s, %s, %s,
            %s,
            NOW(), NOW() + INTERVAL '20 seconds'
        )
        RETURNING id, creado_en
    """, (
        str(data.paciente_uuid),
        profesional_id,
        data.motivo,
        data.direccion,
        data.lat,
        data.lng,
        data.metodo_pago,
        data.tipo        # 🔑 ACÁ ESTÁ EL FIX
    ))
    
    consulta_id, creado_en = cur.fetchone()
    db.commit()


    print(f"🟢 Consulta {consulta_id} asignada a {profesional_id}")

    # Registrar intento
    cur.execute("""
        INSERT INTO intentos_asignacion (consulta_id, medico_id)
        VALUES (%s, %s)
    """, (consulta_id, profesional_id))
    db.commit()

    # WS REAL TIME
    if profesional_id in active_medicos:
        try:
            pro = active_medicos[profesional_id]
            creado_ts = int(creado_en.timestamp() * 1000)
            await pro["ws"].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id,
                "paciente_uuid": str(data.paciente_uuid),
                "paciente_nombre": data.motivo,
                "motivo": data.motivo,
                "direccion": data.direccion,
                "lat": data.lat,
                "lng": data.lng,
                "distancia_km": round(float(distancia), 2),
                "metodo_pago": data.metodo_pago,
                "profesional_tipo": tipo,
                "creado_en": str(creado_en),
    
            })
            print("📤 WS enviado")
        except:
            print("⚠️ Error WS")

    # ---------------------------------------------------------
    # === DOCYA PUSH INMEDIATO (siempre) ===
    # ---------------------------------------------------------
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (profesional_id,))
    row = cur.fetchone()

    if row and row[0]:
        try:
            enviar_push(
                row[0],
                "📢 Nueva consulta disponible",
                data.motivo,
                {
                    "tipo": "consulta_nueva",
                    "consulta_id": str(consulta_id),
                    "medico_id": str(profesional_id),
                    "origen": "asignacion"
                }
            )
            print("📤 PUSH enviado (asignación directa)")
        except:
            print("❌ Error enviando push")

    # ---------------------------------------------------------
    # WATCHDOG EFECTIVO (fallback) — FIX DEFINITIVO
    # ---------------------------------------------------------
    async def watchdog_efectivo(profesional_id: int, consulta_id: int, motivo: str):
        await asyncio.sleep(3)
    
        # Si el médico ya está conectado por WS → OK
        if profesional_id in active_medicos:
            print("🟢 Watchdog OK → WS activo")
            return
    
        print("🟡 Watchdog: No WS → push fallback")
    
        # 🔐 NUEVA CONEXIÓN (aislada del request)
        db_wd = get_db_direct()
        cur_wd = None
    
        try:
            cur_wd = db_wd.cursor()
    
            # PUSH fallback
            cur_wd.execute(
                "SELECT fcm_token FROM medicos WHERE id=%s",
                (profesional_id,)
            )
            row = cur_wd.fetchone()
    
            if row and row[0]:
                enviar_push(
                    row[0],
                    "📢 Nueva consulta",
                    motivo,
                    {
                        "tipo": "consulta_nueva",
                        "consulta_id": str(consulta_id),
                        "medico_id": str(profesional_id),
                        "fallback": True
                    }
                )
                print("📤 PUSH fallback enviado")
    
            # ⏱ Segunda ventana
            await asyncio.sleep(10)
    
            # Verificar estado real de la consulta
            cur_wd.execute(
                "SELECT estado FROM consultas WHERE id=%s",
                (consulta_id,)
            )
            estado_row = cur_wd.fetchone()
    

    
        except Exception as e:
            print("❌ Error watchdog_efectivo:", e)
    
        finally:
            if cur_wd:
                cur_wd.close()
            db_wd.close()
    
        
    
    # ---------------------------------------------------------
    # LANZAR WATCHDOG (NO BLOQUEANTE)
    # ---------------------------------------------------------
    asyncio.create_task(
        watchdog_efectivo(
            profesional_id=profesional_id,
            consulta_id=consulta_id,
            motivo=data.motivo
        )
    )
    asyncio.create_task(notificar_consulta_nueva_telegram(consulta_id, True))

    # ---------------------------------------------------------
    # RETURN FINAL DEL ENDPOINT (OBLIGATORIO)
    # ---------------------------------------------------------
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
        "estado": "pendiente"
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
GOOGLE_API_KEY = "AIzaSyDVv_barlVwHJTgLF66dP4ESUffCBuS3uA"  # 🔒 Reemplazá con tu API Key real de Google Cloud


@app.get("/consultas/asignadas/{medico_id}")
def consultas_asignadas(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.paciente_uuid,
               COALESCE(u.full_name, 'Paciente') AS paciente_nombre,
               COALESCE(u.telefono, 'Sin número') AS paciente_telefono,
               c.motivo, c.direccion, c.lat, c.lng, c.estado,
               m.latitud, m.longitud,
               m.tipo,
               c.creado_en     -- 🔥 AGREGADO AQUÍ
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        LEFT JOIN users u ON c.paciente_uuid = u.id
        WHERE c.medico_id = %s
          AND c.estado IN ('pendiente', 'aceptada', 'en_camino', 'en_domicilio')
        ORDER BY c.creado_en DESC
        LIMIT 1
    """, (medico_id,))
    
    row = cur.fetchone()
    if not row:
        return {"consulta": None}

    (
        consulta_id, paciente_uuid, paciente_nombre, paciente_telefono,
        motivo, direccion, lat, lng, estado,
        med_lat, med_lng, tipo_profesional,
        creado_en                      # 🔥 RECIBIDO AQUÍ
    ) = row

    distancia_km = None
    tiempo_min = None

    try:
        if all(v is not None for v in [lat, lng, med_lat, med_lng]):
            lat = float(lat)
            lng = float(lng)
            med_lat = float(med_lat)
            med_lng = float(med_lng)

            distancia_km = calcular_distancia_km(med_lat, med_lng, lat, lng)
            tiempo_min = calcular_eta_local(distancia_km)

            print(f"🧮 ETA local: {tiempo_min} min | 📏 Distancia: {distancia_km:.2f} km")

    except Exception as e:
        print("❌ Error cálculo ETA:", e)

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
        "tipo": tipo_profesional,
        "creado_en": str(creado_en),     # 🔥 ENVIADO AL FRONT AQUÍ
        "distancia_km": round(distancia_km, 2) if distancia_km else None,
        "tiempo_estimado_min": tiempo_min if tiempo_min is not None else 0
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
def aceptar_consulta(
    consulta_id: int,
    data: MedicoAccion,
    db=Depends(get_db)
):
    medico_id = data.medico_id
    cur = db.cursor()

    # 1️⃣ Obtener estado actual y pago
    cur.execute("""
        SELECT estado, medico_id, mp_payment_id
        FROM consultas
        WHERE id = %s
    """, (consulta_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Consulta inexistente")

    estado_actual, medico_actual, payment_id = row

    # ❌ Consulta ya no disponible para este médico
    if estado_actual != "pendiente" or medico_actual != medico_id:
        # ⚠️ ESTE MENSAJE ES CLAVE PARA EL FRONT
        raise HTTPException(
            status_code=400,
            detail="CONSULTA_EXPIRADA"
        )

    # 2️⃣ Aceptar consulta (limpia reloj)
    cur.execute("""
        UPDATE consultas
        SET estado = 'aceptada',
            aceptada_en = NOW(),
            asignada_en = NULL,
            expira_en = NULL
        WHERE id = %s
          AND estado = 'pendiente'
          AND medico_id = %s
        RETURNING id
    """, (consulta_id, medico_id))

    updated = cur.fetchone()
    if not updated:
        # doble seguridad
        raise HTTPException(
            status_code=400,
            detail="CONSULTA_EXPIRADA"
        )

    # 3️⃣ Marcar médico como NO disponible
    cur.execute("""
        UPDATE medicos
        SET disponible = FALSE
        WHERE id = %s
    """, (medico_id,))

    db.commit()

    # 4️⃣ Capturar pago si corresponde
    if payment_id:
        try:
            url = f"https://api.mercadopago.com/v1/payments/{payment_id}/capture"
            headers = {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }

            with httpx.Client(timeout=5) as client:
                r = client.post(url, headers=headers, json={})

            if r.status_code not in (200, 201):
                print("⚠ Error capturando pago MercadoPago:", r.text)

        except Exception as e:
            print("⚠ Excepción capturando pago:", e)

    return {
        "ok": True,
        "consulta_id": consulta_id
    }



from datetime import datetime
import asyncio

@app.post("/consultas/{consulta_id}/rechazar")
async def rechazar_consulta(
    consulta_id: int, 
    data: dict, 
    background_tasks: BackgroundTasks, 
    db=Depends(get_db)
):
    cur = db.cursor()
    medico_id = int(data.get("medico_id"))

    print(f"❌ Rechazo médico {medico_id} consulta {consulta_id}")

    try:
        # 1️⃣ Validar estado actual (con bloqueo de fila para evitar race conditions)
        cur.execute("""
            SELECT estado, medico_id, expira_en
            FROM consultas
            WHERE id = %s FOR UPDATE
        """, (consulta_id,))
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "CONSULTA_INEXISTENTE")

        estado_actual, medico_asignado, expira_en = row

        # Si ya no es el médico asignado o ya no está pendiente, salimos rápido
        if estado_actual != "pendiente" or medico_asignado != medico_id:
            return {"ok": True, "detalle": "CONSULTA_YA_PROCESADA"}

        # 2️⃣ Registrar rechazo en historial e intentos
        cur.execute("""
            INSERT INTO intentos_asignacion (consulta_id, medico_id)
            VALUES (%s, %s)
        """, (consulta_id, medico_id))

        # 3️⃣ Liberar médico y limpiar consulta para que sea reasignable
        cur.execute("UPDATE medicos SET disponible = TRUE WHERE id = %s", (medico_id,))
        cur.execute("""
            UPDATE consultas
            SET medico_id = NULL,
                estado = 'pendiente',
                asignada_en = NULL,
                expira_en = NULL
            WHERE id = %s
        """, (consulta_id,))

        # 4️⃣ Commit de la limpieza
        db.commit()
        print(f"✅ Consulta {consulta_id} liberada por rechazo de médico {medico_id}")

    except Exception as e:
        db.rollback()
        print(f"❌ Error al procesar rechazo: {e}")
        raise HTTPException(500, "ERROR_INTERNO")
    finally:
        cur.close()

    # 5️⃣ REASIGNACIÓN EN SEGUNDO PLANO
    # Esto corre por fuera del request, evitando que el médico espere el loop de búsqueda
    background_tasks.add_task(worker_reasignacion_tras_rechazo, consulta_id, medico_id)

    return {"ok": True}

async def worker_reasignacion_tras_rechazo(consulta_id, medico_rechazante):
    """
    Worker que intenta reasignar la consulta sin bloquear el request del médico.
    """
    print(f"🔄 Iniciando reasignación automática para consulta {consulta_id}...")
    
    # Pequeña espera para asegurar que la DB impactó los cambios
    await asyncio.sleep(1) 
    
    # Usamos get_db_worker para no tocar el pool principal de la API
    db_reasig = get_db_worker() 
    try:
        # Intentamos reasignar excluyendo al que acaba de rechazar
        await intentar_reasignar(
            consulta_id,
            db_reasig,
            excluir_medico_id=medico_rechazante,
            max_ciclos=10, # Intentamos 10 veces
            espera_segundos=4
        )
    except Exception as e:
        print(f"❌ Error en reasignación post-rechazo: {e}")
    finally:
        db_reasig.close()






@app.post("/consultas/{consulta_id}/timeout")
async def timeout_consulta(consulta_id: int, data: dict, db=Depends(get_db)):
    cur = db.cursor()
    medico_id = int(data.get("medico_id"))

    print(f"⏳ Timeout FRONT médico {medico_id} consulta {consulta_id}")

    # Ver estado actual de la consulta
    cur.execute("""
        SELECT medico_id, expira_en
        FROM consultas
        WHERE id = %s
    """, (consulta_id,))
    row = cur.fetchone()

    if not row:
        return {"ok": False, "error": "Consulta inexistente"}

    medico_actual, expira_en = row

    # Si ya no está asignada a este médico → ignorar
    if medico_actual != medico_id:
        print("ℹ️ Timeout tardío → consulta ya reasignada")
        return {"ok": True, "reasignado": False}

    # Si ya expiró, el worker backend ya se encargó
    if expira_en:
        expira_en_arg = expira_en.replace(tzinfo=ARG_TZ) if expira_en.tzinfo is None else expira_en.astimezone(ARG_TZ)
    else:
        expira_en_arg = None

    if expira_en_arg and expira_en_arg < now_argentina():
        print("ℹ️ Timeout ya procesado por backend")
        return {"ok": True, "reasignado": False}

    # Registrar intento
    cur.execute("""
        INSERT INTO intentos_asignacion (consulta_id, medico_id)
        VALUES (%s, %s)
    """, (consulta_id, medico_id))

    # Liberar médico
    cur.execute("""
        UPDATE medicos
        SET disponible = TRUE
        WHERE id = %s
    """, (medico_id,))

    # Limpiar asignación (SIN tocar estado)
    cur.execute("""
        UPDATE consultas
        SET medico_id = NULL,
            asignada_en = NULL,
            expira_en = NULL
        WHERE id = %s
    """, (consulta_id,))

    db.commit()

    # Reasignar
    reasignado = await intentar_reasignar(
        consulta_id,
        db,
        excluir_medico_id=medico_id
    )


    return {
        "ok": True,
        "reasignado": reasignado
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
    cur.execute("""
        UPDATE consultas
        SET estado = 'en_domicilio'
        WHERE id = %s
          AND medico_id = %s
          AND estado IN ('aceptada', 'en_camino')
        RETURNING id
    """, (consulta_id, data.medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "en_domicilio"}

from datetime import datetime, timedelta
from fastapi import HTTPException

def registrar_pago_interno(consulta_id, medico_id, paciente_uuid, metodo_pago, db):
    cur = db.cursor()

    # 1️⃣ Obtener monto total de la consulta
    cur.execute("SELECT precio_final FROM consultas WHERE id=%s", (consulta_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada para pago")

    monto_total = row[0]

    # 2️⃣ Calcular comisión y neto
    docya_comision = int(monto_total * 0.20)

    if metodo_pago == "efectivo":
        # Médico cobra todo → luego DocYa descuenta su parte
        medico_neto = monto_total
        saldo_delta = -docya_comision     # médico debe a DocYa
    else:
        # MP / Tarjeta / Transferencia → DocYa recauda
        medico_neto = int(monto_total * 0.80)
        saldo_delta = medico_neto         # DocYa le debe al médico

    # 3️⃣ Insertar pago (SIN paciente_uuid)
    cur.execute("""
        INSERT INTO pagos_consulta 
        (consulta_id, medico_id, metodo_pago, monto_total, medico_neto, docya_comision)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (
        consulta_id,
        medico_id,
        metodo_pago,
        monto_total,
        medico_neto,
        docya_comision
    ))

    # 4️⃣ Actualizar saldo del médico
    cur.execute("SELECT saldo FROM saldo_medico WHERE medico_id=%s", (medico_id,))
    row2 = cur.fetchone()

    if row2:
        cur.execute(
            "UPDATE saldo_medico SET saldo = saldo + %s WHERE medico_id=%s",
            (saldo_delta, medico_id)
        )
    else:
        cur.execute(
            "INSERT INTO saldo_medico (medico_id, saldo) VALUES (%s, %s)",
            (medico_id, saldo_delta)
        )



@app.post("/consultas/{consulta_id}/finalizar")
def finalizar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # 🔹 Obtener consulta + tipo de profesional + paciente_uuid + metodo_pago
    cur.execute("""
        SELECT c.id, c.medico_id, m.tipo, c.paciente_uuid, c.metodo_pago
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        WHERE c.id = %s
    """, (consulta_id,))
    
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    consulta_id, medico_id, tipo, paciente_uuid, metodo_pago = row

    # ============================================================
    # 🕒 HORARIO ARGENTINA
    # ============================================================
    ahora_ar = now_argentina()
    hora = ahora_ar.hour
    es_nocturno = hora >= 22 or hora < 6

    # ============================================================
    # 💵 TARIFA SEGÚN TIPO + HORARIO
    # ============================================================
    if tipo == "medico":
        precio = 40000 if es_nocturno else 30000
    else:  # enfermero
        precio = 30000 if es_nocturno else 20000

    # ============================================================
    # 🔹 Finalizar consulta
    # ============================================================
    cur.execute("""
        UPDATE consultas
        SET estado = 'finalizada',
            fin_atencion = NOW(),
            precio_final = %s
        WHERE id = %s
        RETURNING estado, fin_atencion
    """, (precio, consulta_id))
    
    new_estado, fin = cur.fetchone()

    # ============================================================
    # 🔹 Liberar profesional
    # ============================================================
    cur.execute("UPDATE medicos SET disponible = TRUE WHERE id = %s", (medico_id,))

    # ============================================================
    # 🔥 REGISTRAR PAGO AUTOMÁTICAMENTE (SIN TestClient)
    # ============================================================
    registrar_pago_interno(
        consulta_id=consulta_id,
        medico_id=medico_id,
        paciente_uuid=paciente_uuid,
        metodo_pago=metodo_pago or "efectivo",
        db=db
    )

    db.commit()

    return {
        "msg": "Consulta finalizada y pago registrado",
        "consulta_id": consulta_id,
        "tipo": tipo,
        "nocturno": es_nocturno,
        "precio_cobrado": precio,
        "estado": new_estado,
        "fin_atencion": fin,
    }

@app.get("/consultas/{consulta_id}/estado_pago")
def estado_pago_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()

    cur.execute("""
        SELECT metodo_pago
        FROM consultas
        WHERE id = %s
    """, (consulta_id,))

    row = cur.fetchone()

    if not row:
        return {"pagado": False, "metodo": None}

    metodo_pago = row[0]

    # --------------------------------
    #   EFECTIVO → médico cobra
    # --------------------------------
    if metodo_pago == "efectivo":
        return {
            "pagado": False,      # debe cobrar
            "metodo": "efectivo"
        }

    # --------------------------------
    #   TARJETA → paciente ya pagó a través de la app
    # --------------------------------
    if metodo_pago == "tarjeta":
        return {
            "pagado": True,        # NO debe cobrar
            "metodo": "tarjeta"
        }

    # fallback (por si agregás otro método)
    return {
        "pagado": False,
        "metodo": metodo_pago
    }




@app.get("/pacientes/{paciente_uuid}/historia_clinica")
def historia_clinica(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            c.id AS consulta_id,
            c.creado_en AS fecha_consulta,
            c.motivo,
            c.estado,

            m.full_name AS medico_nombre,
            m.tipo AS medico_tipo,   -- medico / enfermero

            n.contenido AS historia_clinica,
            n.creado_en AS fecha_nota
        FROM consultas c
        LEFT JOIN notas_medicas n ON c.id = n.consulta_id
        LEFT JOIN medicos m ON c.medico_id = m.id
        WHERE c.paciente_uuid = %s
          AND c.estado = 'finalizada'          -- 🔥 FILTRO SOLO FINALIZADAS
        ORDER BY c.creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()

    lista = []

    for r in rows:
        consulta_id = r[0]
        fecha_consulta = r[1]
        motivo = r[2]
        estado = r[3]
        nombre = r[4] or ""
        tipo = (r[5] or "").lower()
        historia = r[6]
        fecha_nota = r[7]

        # ============================
        # 🟢 PREFIJO GENÉRICO
        # ============================
        if tipo == "medico":
            prefijo = "Dr/a."
        elif tipo == "enfermero":
            prefijo = "Enfermero/a."
        else:
            prefijo = ""

        nombre_completo = f"{prefijo} {nombre}".strip()

        lista.append({
            "consulta_id": consulta_id,
            "fecha_consulta": format_datetime_arg(fecha_consulta),
            "motivo": motivo,
            "estado": estado,
            "medico": nombre_completo,
            "tipo_profesional": tipo,
            "historia_clinica": historia,
            "fecha_nota": format_datetime_arg(fecha_nota) if fecha_nota else None
        })

    return lista


# --- Función para enviar notificaciones push ---
def enviar_push(
    fcm_token: str,
    titulo: str,
    cuerpo: str,
    data: dict = {},
    android_channel_id: str = "default_channel_id",
    android_sound: str | None = None,
    apns_sound: str = "default",
):
    project_id = service_account_info["project_id"]
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    payload = {
        "message": {
            "token": fcm_token,

            # ================================
            # 🔔 NOTIFICACIÓN VISIBLE
            # ================================
            "notification": {
                "title": titulo,
                "body": cuerpo,
            },

            # ================================
            # 🤖 ANDROID
            # ================================
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": android_channel_id,
                    **({"sound": android_sound} if android_sound else {}),
                }
            },

            # ================================
            # 🍏 iOS / APNS
            # ================================
            "apns": {
                "headers": {
                    "apns-priority": "10"  # entrega inmediata
                },
                "payload": {
                    "aps": {
                        "alert": {"title": titulo, "body": cuerpo},
                        "sound": apns_sound,
                        "badge": 1
                    }
                }
            },

            # ================================
            # 📦 DATA personalizada (obligatoria como string)
            # ================================
            "data": {k: str(v) for k, v in data.items()},
        }
    }

    r = requests.post(url, headers=headers, json=payload)
    print("📤 Push enviado:", r.status_code, r.text)




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
        "lat": float(row[2]) if row[2] is not None else None,
        "lng": float(row[3]) if row[3] is not None else None,
        "telefono": row[4],
    }


@app.post("/consultas/{consulta_id}/ubicacion_medico")
def actualizar_ubicacion_medico(consulta_id: int, datos: dict, db=Depends(get_db)):
    cur = db.cursor()

    try:
        lat_med = datos.get("lat")
        lng_med = datos.get("lng")

        if lat_med is None or lng_med is None:
            return {"error": "Faltan coordenadas"}

        lat_med = float(lat_med)
        lng_med = float(lng_med)

        # --------------------------------------------------
        # 0) VALIDAR CONSULTA Y MÉDICO ASIGNADO
        # --------------------------------------------------
        cur.execute("""
            SELECT c.medico_id
            FROM consultas c
            WHERE c.id = %s
        """, (consulta_id,))
        
        row_med = cur.fetchone()

        if not row_med:
            return {"error": "Consulta no válida"}

        medico_id = row_med[0]


        # -----------------------------------------
        # 1) ACTUALIZAR UBICACIÓN EN TABLA MEDICOS
        #    ❌ NO tocar ultimo_ping acá
        # -----------------------------------------
        cur.execute("""
            UPDATE medicos
            SET latitud = %s,
                longitud = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (lat_med, lng_med, medico_id))

        # -----------------------------------------
        # 2) ACTUALIZAR UBICACIÓN EN CONSULTAS
        # -----------------------------------------
        cur.execute("""
            UPDATE consultas
            SET medico_lat = %s,
                medico_lng = %s,
                actualizado_en = NOW()
            WHERE id = %s
        """, (lat_med, lng_med, consulta_id))

        # -----------------------------------------
        # 3) TRAER UBICACIÓN DEL PACIENTE
        # -----------------------------------------
        cur.execute("""
            SELECT lat, lng
            FROM consultas
            WHERE id = %s
        """, (consulta_id,))
        
        row = cur.fetchone()

        if not row or row[0] is None or row[1] is None:
            db.commit()
            return {
                "status": "ubicacion guardada",
                "eta": None
            }

        lat_pac = float(row[0])
        lng_pac = float(row[1])

        # -----------------------------------------
        # 4) CALCULAR DISTANCIA HAVERSINE (km)
        # -----------------------------------------
        distancia_km = calcular_distancia_km(
            lat_med, lng_med, lat_pac, lng_pac
        )

        # -----------------------------------------
        # 5) CALCULAR ETA LOCAL
        # -----------------------------------------
        tiempo_min = calcular_eta_local(distancia_km)

        print(
            f"🧮 ETA local consulta {consulta_id}: "
            f"{tiempo_min} min (dist {distancia_km:.2f} km)"
        )

        # -----------------------------------------
        # 6) GUARDAR ETA EN CONSULTAS
        # -----------------------------------------
        cur.execute("""
            UPDATE consultas
            SET tiempo_estimado_min = %s
            WHERE id = %s
        """, (tiempo_min, consulta_id))

        db.commit()

        return {
            "status": "ok",
            "distancia_km": round(distancia_km, 2),
            "eta": tiempo_min
        }

    except Exception as e:
        db.rollback()
        print("❌ Error ETA:", e)
        return {"error": str(e)}



@app.post("/medico/{id}/cerrar_app")
def cerrar_app(id: int, db=Depends(get_db)):
    cur = db.cursor()

    # Marcar no disponible en BD
    cur.execute("""
        UPDATE medicos 
        SET disponible = FALSE,
            activo = FALSE
        WHERE id=%s;
    """, (id,))
    db.commit()

    # Cerrar WS si existe
    if id in active_medicos:
        try:
            ws = active_medicos[id]["ws"]
            asyncio.create_task(ws.close(code=1000))
        except:
            pass

    return {
        "status": "ok",
        "mensaje": "médico cerrado correctamente"
    }


# --- WebSocket de médicos ---
# ====================================================
# 🩺 WEBSOCKET MÉDICO (con ping/pong mejorado para iOS)
# ====================================================
import json
import asyncio
from datetime import datetime

monitor_tasks = {}       # Para guardar la task del monitor
last_ping_times = {}     # Último ping real registrado
last_message_times = {}  # NEW: último mensaje recibido (anti-falso-timeout)


@app.websocket("/ws/medico/{medico_id}")
async def medico_ws(websocket: WebSocket, medico_id: int):

    await websocket.accept()
    print(f"🟢 Nuevo WebSocket aceptado para médico {medico_id}")

    # ============================================================
    # 1) REGISTRAR WS (❌ NO cerrar WS previo acá)
    # ============================================================
    active_medicos[medico_id] = {"ws": websocket}

    now = now_argentina().replace(tzinfo=None)
    last_message_times[medico_id] = now
    last_ping_times[medico_id] = now

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE medicos 
            SET activo = TRUE,
                disponible = TRUE,
                ultimo_ping = NOW()
            WHERE id=%s
        """, (medico_id,))
        conn.commit()
        print(f"✅ Médico {medico_id} marcado activo=TRUE, disponible=TRUE")
    except Exception as e:
        print(f"⚠️ Error actualizando BD para médico {medico_id}: {e}")
    finally:
        put_conn(conn)

    # ============================================================
    # 2) MONITOR ANTI-TIMEOUT REAL (cada 5s)
    # ============================================================
    async def monitor_ws(local_ws):
        while True:
            await asyncio.sleep(5)
            now = now_argentina().replace(tzinfo=None)

            # Si este WS ya no es el activo → salir sin tocar nada
            if active_medicos.get(medico_id, {}).get("ws") != local_ws:
                return

            diff_msg = (now - last_message_times.get(medico_id, now)).total_seconds()
            diff_ping = (now - last_ping_times.get(medico_id, now)).total_seconds()

            if diff_msg < 30:
                continue

            if diff_ping > 120:
                print(f"⏳ TIMEOUT REAL médico {medico_id}: {diff_ping:.1f}s sin ping")
                await local_ws.close(code=1000)
                return

    monitor_tasks[medico_id] = asyncio.create_task(monitor_ws(websocket))

    # ============================================================
    # 3) LOOP PRINCIPAL DEL WEBSOCKET
    # ============================================================
    try:
        while True:
            raw = await websocket.receive_text()
            last_message_times[medico_id] = now_argentina().replace(tzinfo=None)

            print(f"📩 WS médico {medico_id}: {raw}")

            try:
                data = json.loads(raw)
                tipo = data.get("tipo", "").lower()
            except:
                tipo = raw.strip().lower()

            # -------------------------
            # 🟢 PING → actualizar estado
            # -------------------------
            if tipo == "ping":
                last_ping_times[medico_id] = now_argentina().replace(tzinfo=None)

                try:
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE medicos SET ultimo_ping = NOW() WHERE id=%s",
                        (medico_id,)
                    )
                    conn.commit()
                except Exception as e:
                    print(f"⚠️ Error guardando ping médico {medico_id}: {e}")
                finally:
                    put_conn(conn)

                await websocket.send_text("pong")
                continue

    # ============================================================
    # 4) MANEJO DE DESCONEXIÓN
    # ============================================================
    except Exception as e:
        print(f"❌ WS médico {medico_id} desconectado: {e}")

    finally:
        # 🔒 SOLO limpiar si este WS sigue siendo el activo
        if active_medicos.get(medico_id, {}).get("ws") == websocket:

            active_medicos.pop(medico_id, None)

            if medico_id in monitor_tasks:
                monitor_tasks[medico_id].cancel()
                monitor_tasks.pop(medico_id, None)

            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE medicos
                    SET disponible = FALSE,
                        activo = FALSE
                    WHERE id = %s
                """, (medico_id,))

                conn.commit()
            except Exception as e2:
                print(f"⚠️ Error marcando offline médico {medico_id}: {e2}")
            finally:
                put_conn(conn)

            print(f"🟡 Médico {medico_id} marcado como OFFLINE y NO disponible (WS finalizado)")

        print(f"🔻 Total médicos conectados ahora: {len(active_medicos)}")







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
# 💊 GENERAR RECETA PDF PROFESIONAL (CORREGIDO)
# ====================================================
from fastapi import Form
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
import io, qrcode, requests
from datetime import datetime

@app.post("/consultas/{consulta_id}/receta_pdf")
async def generar_receta_pdf(
    consulta_id: int,
    medico_id: int = Form(...),
    paciente_uuid: str = Form(...),
    obra_social: str = Form(""),
    nro_credencial: str = Form(""),
    diagnostico: str = Form("")
):
    try:
        # Buscar datos del médico y paciente
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()

        cur.execute("""
            SELECT full_name, matricula, especialidad, firma_url
            FROM medicos WHERE id=%s
        """, (medico_id,))
        medico = cur.fetchone()

        cur.execute("""
            SELECT full_name, dni, fecha_nacimiento
            FROM users WHERE id=%s
        """, (paciente_uuid,))
        paciente = cur.fetchone()

        conn.close()

        if not medico or not paciente:
            raise Exception("Datos de médico o paciente no encontrados")

        medico_nombre, matricula, especialidad, firma_url = medico
        paciente_nombre, paciente_dni, paciente_nac = paciente

        # Crear PDF
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        # ------------------------------------------------------
        # ENCABEZADO
        # ------------------------------------------------------
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, width, height, fill=1)

        c.drawImage(
            "https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logo_1_svfdye.png",
            40, height - 90, width=140, preserveAspectRatio=True, mask='auto'
        )

        c.setFont("Helvetica-Bold", 18)
        c.setFillColor(colors.HexColor("#14B8A6"))
        c.drawString(200, height - 70, "Receta Médica Digital")

        c.setStrokeColor(colors.HexColor("#14B8A6"))
        c.line(40, height - 95, width - 40, height - 95)

        # ------------------------------------------------------
        # DATOS DEL PROFESIONAL
        # ------------------------------------------------------
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, height - 120, f"Médico: {medico_nombre}")

        c.setFont("Helvetica", 11)
        c.drawString(40, height - 135, f"Especialidad: {especialidad}")
        c.drawString(40, height - 150, f"Matrícula: {matricula}")
        c.drawString(40, height - 165, f"Fecha de emisión: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

        # ------------------------------------------------------
        # DATOS DEL PACIENTE
        # ------------------------------------------------------
        y = height - 205
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

        # ------------------------------------------------------
        # DIAGNOSTICO
        # ------------------------------------------------------
        y -= 35
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Diagnóstico:")

        y -= 18
        c.setFont("Helvetica", 11)
        c.drawString(60, y, diagnostico or "—")

        # ------------------------------------------------------
        # MEDICAMENTOS
        # ------------------------------------------------------
        y -= 35
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Rp / Indicaciones:")

        y -= 20
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        cur = conn.cursor()

        cur.execute("""
            SELECT nombre, dosis, frecuencia, duracion
            FROM receta_items ri
            JOIN recetas r ON r.id = ri.receta_id
            WHERE r.consulta_id=%s AND r.medico_id=%s
        """, (consulta_id, medico_id))

        medicamentos = cur.fetchall()
        conn.close()

        c.setFont("Helvetica", 11)

        for m in medicamentos:
            y -= 18
            if y < 100:
                c.showPage()
                y = height - 100
            c.drawString(60, y, f"- {m[0]} ({m[1]}), {m[2]}, {m[3]}")

        # ------------------------------------------------------
        # FIRMA DEL MÉDICO
        # ------------------------------------------------------
        y -= 60
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, y, "Firma electrónica del profesional:")

        if firma_url:
            try:
                response = requests.get(firma_url)
                img = ImageReader(io.BytesIO(response.content))
                c.drawImage(img, 40, y - 70, width=180, height=70, mask='auto')
            except:
                c.setFont("Helvetica-Oblique", 10)
                c.drawString(40, y - 50, "(Error cargando la firma digital)")
        else:
            c.setFont("Helvetica-Oblique", 10)
            c.drawString(40, y - 50, "(El profesional no cargó su firma digital)")

        c.setFont("Helvetica", 10)
        c.drawString(40, y - 95, f"{medico_nombre} – Matrícula: {matricula}")
        c.drawString(40, y - 110, "Firmado electrónicamente según Ley 25.506 y Ley 27.553")
        c.drawString(40, y - 125, f"Fecha de firma: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

        # ------------------------------------------------------
        # QR
        # ------------------------------------------------------
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

        # ------------------------------------------------------
        # SUBIR A CLOUDINARY
        # ------------------------------------------------------
        result = cloudinary.uploader.upload(
            buffer,
            resource_type="raw",
            folder="recetas",
            public_id=f"receta_{consulta_id}",
            overwrite=True,
            format="pdf"
        )

        return {
            "status": "ok",
            "consulta_id": consulta_id,
            "pdf_url": result.get("secure_url")
        }

    except Exception as e:
        return {"error": str(e)}




# ====================================================
# 💾 GENERAR PDF DESDE HTML (receta verificada) – CORREGIDO
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

    # Obtener firma del médico
    cur.execute("SELECT firma_url FROM medicos WHERE id = %s", (medico_id,))
    firma_row = cur.fetchone()
    firma_url = firma_row[0] if firma_row and firma_row[0] else None

    # Medicamentos
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
            <p><b>Firma electrónica del profesional:</b></p>
            {f'<img src="{firma_url}">' if firma_url else '<p><i>El profesional no cargó firma digital</i></p>'}
            <p><b>{medico_nombre}</b> – Matrícula: {matricula}</p>
            <p>Firmado electrónicamente según Ley 25.506 y Ley 27.553.</p>
            <p>Fecha de firma: {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
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
        HTML(string=html).write_pdf(tmp_pdf.name)
        tmp_pdf.seek(0)
        pdf_bytes = tmp_pdf.read()

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
          AND activo = TRUE
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
    """, (tipo,))

    count = cur.fetchone()[0]

    return {"disponibles": count > 0}


import mercadopago

ACCESS_TOKEN = MP_ACCESS_TOKEN
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None


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

    # Metadata enviada desde el frontend (muy importante!)
    metadata = payment_info["metadata"]
    paciente_uuid = metadata["paciente_uuid"]
    tipo = metadata["tipo"]           # medico / enfermero
    lat = metadata["lat"]
    lng = metadata["lng"]

    cur = db.cursor()

    # --------------------------------------------------------
    # 🔍 Buscar profesional del tipo correcto
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # 📝 Crear consulta (agregar columna tipo!)
    # --------------------------------------------------------
    cur.execute("""
        INSERT INTO consultas (
            paciente_uuid,
            medico_id,
            motivo,
            direccion,
            lat,
            lng,
            estado,
            metodo_pago,
            tipo
        )
        VALUES (%s,%s,'Pago aprobado','Dirección desde MP',%s,%s,'pendiente','tarjeta',%s)
        RETURNING id, creado_en
    """, (paciente_uuid, medico_id, lat, lng, tipo))
    
    consulta_id, creado_en = cur.fetchone()
    db.commit()

    # --------------------------------------------------------
    # 🔔 WebSocket — enviar solo si coincide el tipo
    # --------------------------------------------------------
    if medico_id in active_medicos:
        try:
            pro = active_medicos[medico_id]

            if pro["tipo"] == tipo:  # médico <-> enfermero
                await pro["ws"].send_json({
                    "tipo": "consulta_nueva",
                    "consulta_id": consulta_id,
                    "paciente_uuid": paciente_uuid,
                    "profesional_tipo": tipo
                })
                print(f"📤 WS enviado al {tipo} {medico_id}")
        except Exception as e:
            print(f"⚠️ WS error: {e}")

    # --------------------------------------------------------
    # 🔔 PUSH
    # --------------------------------------------------------
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()

    if row and row[0]:
        enviar_push(
            row[0],
            "📢 Nueva consulta",
            "Tienes una nueva solicitud",
            {
                "consulta_id": consulta_id,
                "profesional_tipo": tipo
            }
        )

    return {"status": "consulta_creada", "consulta_id": consulta_id}


    
# ---------- CONSULTAS DETALLE ----------
# ---------- CONSULTAS DETALLE ----------
# ---------- CONSULTAS DETALLE ----------
@app.get("/consultas/{consulta_id}")
def obtener_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            c.id, 
            c.paciente_uuid, 
            c.medico_id, 
            c.estado, 
            c.motivo, 
            c.direccion, 
            c.lat, 
            c.lng, 
            c.creado_en,
            c.mp_status,
            m.full_name, 
            m.matricula,
            m.tipo,
            c.tiempo_estimado_min,
            c.medico_lat,
            c.medico_lng,
            COALESCE(u.full_name, 'Paciente') AS paciente_nombre,      -- 🔥 NUEVO
            COALESCE(u.telefono, '') AS paciente_telefono              -- 🔥 NUEVO
        FROM consultas c
        LEFT JOIN medicos m ON c.medico_id = m.id
        LEFT JOIN users u ON c.paciente_uuid = u.id                  -- 🔥 NUEVO
        WHERE c.id = %s
    """, (consulta_id,))
    
    row = cur.fetchone()
    
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")

    (
        cid, paciente_uuid, medico_id, estado,
        motivo, direccion, lat, lng, creado_en,
        mp_status,
        medico_nombre, medico_matricula, tipo,
        tiempo_estimado_raw,
        medico_lat, medico_lng,
        paciente_nombre, paciente_telefono      # 🔥 NUEVO
    ) = row

    # -------------------------
    # 🔥 Normalizar ETA
    # -------------------------
    if tiempo_estimado_raw is None:
        tiempo_estimado_min = 0
    else:
        try:
            tiempo_estimado_min = int(round(float(tiempo_estimado_raw)))
        except:
            tiempo_estimado_min = 0

    # -------------------------
    # 📏 CALCULAR DISTANCIA (KM)
    # -------------------------
    distancia_km = 0

    try:
        if all(v is not None for v in [lat, lng, medico_lat, medico_lng]):
            distancia_km = calcular_distancia_km(
                float(medico_lat),
                float(medico_lng),
                float(lat),
                float(lng),
            )
    except Exception as e:
        print("❌ Error cálculo distancia:", e)
        distancia_km = 0

    return {
        "id": cid,
        "paciente_uuid": paciente_uuid,
        "paciente_nombre": paciente_nombre,       # ✅ RESTAURA SPLASH
        "paciente_telefono": paciente_telefono,   # ✅ RESTAURA SPLASH
        "medico_id": medico_id,
        "estado": estado,
        "motivo": motivo,
        "direccion": direccion,
        "lat": lat,
        "lng": lng,
        "creado_en": format_datetime_arg(creado_en),
        "mp_status": mp_status,
        "medico_nombre": medico_nombre,
        "medico_matricula": medico_matricula,
        "tipo": tipo,
        "tiempo_estimado_min": tiempo_estimado_min,
        "distancia_km": round(distancia_km, 2),   # ✔ YA USADO POR PACIENTE
        "medico_lat": medico_lat,
        "medico_lng": medico_lng,
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


# ==========================================
# 🔥 POSTGRESQL CONNECTION POOL (100% necesario)
# ==========================================
import psycopg2
from psycopg2.pool import SimpleConnectionPool

POOL = SimpleConnectionPool(
    1, 20,  # min/max conexiones
    dsn=DATABASE_URL,
    sslmode="require"
)

def get_conn():
    return POOL.getconn()

def put_conn(conn):
    try:
        POOL.putconn(conn)
    except:
        pass


from fastapi import Depends
from psycopg2 import sql

@app.post("/medico/{medico_id}/status")
async def actualizar_estado_medico(
    medico_id: int,
    data: dict,
    db=Depends(get_db)
):
    """
    Gestiona disponibilidad del médico.
    - NO requiere WebSocket para activar (clave para iOS)
    - El watchdog se encarga de apagar si no hay WS / ping
    """

    nuevo_estado = data.get("disponible")
    platform = data.get("platform", "unknown")

    print(f"🔄 Médico {medico_id} solicita cambiar disponible → {nuevo_estado} ({platform})")

    # ============================================================
    # 🔴 DESACTIVAR → siempre permitido
    # ============================================================
    if nuevo_estado is False:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE medicos
                SET disponible = FALSE,
                    activo = FALSE
                WHERE id = %s
            """, (medico_id,))
            db.commit()

        print(f"🔴 Médico {medico_id} quedó NO disponible")
        return {
            "ok": True,
            "disponible": False
        }

    # ============================================================
    # 🟢 ACTIVAR → permitido SIEMPRE
    # ============================================================
    if nuevo_estado is True:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE medicos
                SET disponible = TRUE,
                    activo = TRUE,
                    ultimo_ping = NOW()
                WHERE id = %s
            """, (medico_id,))
            db.commit()

        ws_activo = medico_id in active_medicos

        if ws_activo:
            print(f"🟢 Médico {medico_id} activado con WS")
        else:
            print(f"🟡 Médico {medico_id} activado SIN WS (esperando conexión)")

        return {
            "ok": True,
            "disponible": True,
            "ws_activo": ws_activo,
            "platform": platform
        }

    return {
        "ok": False,
        "error": "Estado inválido"
    }




from fastapi import HTTPException
from datetime import datetime, time
from zoneinfo import ZoneInfo
import math

# --------------------------------------
# 🔵 Cálculo de distancia (Haversine)
# --------------------------------------
def calcular_distancia_km(lat1, lon1, lat2, lon2):
    R = 6371  # radio de la Tierra en km
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    dlat = lat2 - lat1
    dlon = math.radians(lon2 - lon1)

    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# --------------------------------------
# 🔵 ETA LOCAL (sin Google, sin ORS)
# --------------------------------------
def calcular_eta_local(distancia_km):
    ahora = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).time()

    # Velocidades promedio REALISTAS
    if time(7, 30) <= ahora <= time(20, 30):   # día + tráfico
        velocidad_kmh = 15   # <-- ANTES era 22, ahora es REAL
    else:
        velocidad_kmh = 28   # noche

    if distancia_km <= 0.15:
        return 1

    return round((distancia_km / velocidad_kmh) * 60)



# --------------------------------------
# 🟦 NUEVO ENDPOINT SIN ORS (LISTO)
# --------------------------------------
@app.post("/medico/{medico_id}/ubicacion")
def actualizar_ubicacion(medico_id: int, data: UbicacionIn, db=Depends(get_db)):
    try:
        ahora_arg = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))

        lat_med = float(data.lat)
        lng_med = float(data.lng)

        cur = db.cursor()

        # 1️⃣ ACTUALIZAR UBICACIÓN DEL MÉDICO
        cur.execute("""
            UPDATE medicos
            SET latitud = %s,
                longitud = %s,
                updated_at = %s,
                ultimo_ping = %s
            WHERE id = %s
            RETURNING disponible;
        """, (lat_med, lng_med, ahora_arg, ahora_arg, medico_id))

        row = cur.fetchone()
        if not row:
            db.commit()
            cur.close()
            raise HTTPException(status_code=404, detail="Médico no encontrado")

        disponible_actual = row[0]
        print(f"📍 Médico {medico_id} → lat/lng actualizado (disponible={disponible_actual})")

        # -------------------------------------------------------
        # 2️⃣ BUSCAR CONSULTA ACTIVA SOLO EN ESTADOS VÁLIDOS
        # -------------------------------------------------------
        cur.execute("""
            SELECT id, lat, lng, estado
            FROM consultas
            WHERE medico_id = %s
              AND estado IN ('pendiente','aceptada','en_camino')
            ORDER BY creado_en DESC
            LIMIT 1;
        """, (medico_id,))

        consulta = cur.fetchone()

        consulta_id = None
        tiempo_min = None

        if consulta:
            consulta_id, lat_pac, lng_pac, estado = consulta

            # ---------------------------------------------------
            # ❌ SI LA CONSULTA YA NO ES VÁLIDA → LIMPIAR Y SALIR
            # ---------------------------------------------------
            if estado not in ("pendiente", "aceptada", "en_camino"):
                print(f"🗑️ Consulta {consulta_id} ignorada (estado {estado}) → limpiando médico_id")

                cur.execute("""
                    UPDATE consultas
                    SET medico_id = NULL
                    WHERE id = %s
                """, (consulta_id,))
                db.commit()
                cur.close()

                return {
                    "ok": True,
                    "medico_id": medico_id,
                    "lat": lat_med,
                    "lng": lng_med,
                    "disponible": disponible_actual,
                    "ultimo_ping": ahora_arg.isoformat(),
                    "consulta_id": None,
                    "eta": None
                }

            # ---------------------------------------------------
            # SI LA CONSULTA ES VÁLIDA → CALCULAR ETA
            # ---------------------------------------------------
            print(f"📦 Consulta ACTIVA del médico {medico_id}: {consulta_id}")

            if lat_pac is not None and lng_pac is not None:
                distancia_km = calcular_distancia_km(lat_med, lng_med, float(lat_pac), float(lng_pac))
                tiempo_min = calcular_eta_local(distancia_km)

                print(f"🧮 Distancia: {distancia_km:.2f} km → ETA: {tiempo_min} min")

            # Guardar cambios
            cur.execute("""
                UPDATE consultas
                SET tiempo_estimado_min = %s,
                    medico_lat = %s,
                    medico_lng = %s,
                    actualizado_en = NOW()
                WHERE id = %s
            """, (tiempo_min, lat_med, lng_med, consulta_id))

        else:
            # ⭐ NO HAY CONSULTA ACTIVA
            cur.execute("""
                UPDATE consultas
                SET medico_lat = %s,
                    medico_lng = %s,
                    actualizado_en = NOW()
                WHERE medico_id = %s
                  AND estado = 'pendiente'
            """, (lat_med, lng_med, medico_id))

        db.commit()
        cur.close()

        return {
            "ok": True,
            "medico_id": medico_id,
            "lat": lat_med,
            "lng": lng_med,
            "disponible": disponible_actual,
            "ultimo_ping": ahora_arg.isoformat(),
            "consulta_id": consulta_id,
            "eta": tiempo_min
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
        "firma_url": row[9],
    }


# --- Foto alias ---
@app.post("/medicos/{medico_id}/foto")
def alias_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    return actualizar_foto(medico_id, file, db)

# --- Alias CBU alias ---
@app.patch("/medicos/{medico_id}/alias")
def actualizar_alias(medico_id: int, data: AliasIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET alias_cbu = %s
        WHERE id = %s
        RETURNING alias_cbu
    """, (data.alias, medico_id))

    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Medico no encontrado")

    return {
        "ok": True,
        "alias": row[0]
    }


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


# movido a auth_router.py
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



# movido a auth_router.py
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
# movido a auth_router.py
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


# movido a auth_router.py
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
# movido a auth_router.py
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

# movido a auth_router.py
def cambio_exitoso():
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Contraseña Actualizada – DocYa</title>

        <style>
          @page {
            size: A4;
            margin: 18mm 12mm; /* 🔥 Márgenes reducidos para entrar en 1 sola hoja */
          }
        
          body {
            font-family: 'Helvetica', Arial, sans-serif;
            background-color: #ffffff;
            color: #1f2937;
            padding: 0;
            line-height: 1.45; /* 🔥 Compactado */
            font-size: 14px;
          }
        
          .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #14B8A6;
            padding-bottom: 6px;
            margin-bottom: 16px;
          }
        
          .logo {
            height: 55px; /* 🔥 Antes 65px */
          }
        
          h1 {
            color: #14B8A6;
            text-align: center;
            font-size: 22px; /* 🔥 Antes 26px */
            margin-top: 10px;
            margin-bottom: 15px; /* 🔥 Antes 40px */
          }
        
          .box {
            border: 1px solid #14B8A6;
            border-radius: 8px;
            background: #f9fdfc;
            padding: 18px 20px; /* 🔥 Antes 25px 30px */
            margin-bottom: 18px;
            page-break-inside: avoid;
          }
        
          .firma {
            margin-top: 25px; /* 🔥 Antes 60px */
            text-align: right;
          }
        
          .firma img {
            height: 65px; /* 🔥 Antes 85px */
            margin-bottom: -5px;
          }
        
          .qr {
            text-align: left;
            margin-top: 20px; /* 🔥 Antes 40px */
          }
        
          .qr img {
            height: 80px; /* 🔥 Antes 100px */
          }
        
          footer {
            text-align: center;
            color: #6b7280;
            font-size: 11px;
            margin-top: 20px; /* 🔥 Antes 60px */
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
# movido a auth_router.py
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

    # Traer datos principales
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
        return HTMLResponse("<h2>❌ Receta no encontrada</h2>", status_code=404)

    # Traer medicamentos
    cur.execute("""
        SELECT nombre, dosis, frecuencia, duracion
        FROM receta_items
        WHERE receta_id = %s
    """, (receta_id,))
    medicamentos = cur.fetchall()

    fecha = receta["creado_en"].strftime("%d/%m/%Y %H:%M") if receta.get("creado_en") else "—"

    firma_url = receta.get("firma_url")

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

          {f'<img src="{firma_url}" alt="Firma del profesional">' 
            if firma_url 
            else '<p><i>El profesional no cargó su firma digital</i></p>'}

          <p style="font-size:13px; color:#4b5563;">
            Firmado electrónicamente conforme Ley 25.506 y Ley 27.553.
          </p>

          <p style="font-size:13px; color:#4b5563;">
            Fecha de firma: {datetime.now().strftime("%d/%m/%Y %H:%M")}
          </p>
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
                    row = cur.fetchone()
                    conn.commit()
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
                dead_sockets = []
                for conn_ws in active_chats[consulta_id]:
                    try:
                        await conn_ws.send_json(msg_obj)
                    except Exception as e:
                        print(f"⚠️ Error enviando a cliente (socket muerto): {e}")
                        dead_sockets.append(conn_ws)
                # Limpiar sockets muertos para evitar memory leak
                for dead in dead_sockets:
                    try:
                        active_chats[consulta_id].remove(dead)
                    except ValueError:
                        pass
                if consulta_id in active_chats and not active_chats[consulta_id]:
                    del active_chats[consulta_id]

                # --------------------------------------------------------
                # 🔔 NOTIFICACIÓN PUSH — FIX DEFINITIVO
                # --------------------------------------------------------
                try:
                    # ------------ Paciente -> Profesional ------------
                    if remitente_tipo == "paciente":
                        cur.execute("""
                            SELECT m.fcm_token
                            FROM consultas c
                            JOIN medicos m ON c.medico_id = m.id
                            WHERE c.id = %s
                        """, (consulta_id,))
                        row_push = cur.fetchone()

                    # ------------ Profesional -> Paciente ------------
                    else:
                        cur.execute("""
                            SELECT u.fcm_token
                            FROM consultas c
                            JOIN users u ON u.id = c.paciente_uuid
                            WHERE c.id = %s
                        """, (consulta_id,))
                        row_push = cur.fetchone()

                    # Enviar push si existe token
                    if row_push and row_push[0]:
                        enviar_push(
                            row_push[0],
                            "Nuevo mensaje",
                            mensaje[:80],
                            {
                                "tipo": "nuevo_mensaje",
                                "consulta_id": str(consulta_id),
                                "remitente_id": str(remitente_id),
                                "mensaje": mensaje
                            }
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

    # 2️⃣ Buscar el tipo de profesional (medico/enfermero)
    cur.execute("SELECT tipo FROM personal WHERE id = %s", (data.medico_id,))
    row_tipo = cur.fetchone()
    if not row_tipo:
        raise HTTPException(status_code=400, detail="No se encontró el profesional")

    tipo = row_tipo[0].lower().strip()  # "medico" o "enfermero"

    # 3️⃣ Determinar si es diurna o nocturna (22:00 - 06:00)
    ahora = now_argentina().time()
    es_nocturna = (ahora >= time(22, 0)) or (ahora < time(6, 0))

    # 4️⃣ Asignar tarifa según tipo y horario
    if tipo == "medico":
        monto_total = 40000 if es_nocturna else 30000
    elif tipo == "enfermero":
        monto_total = 30000 if es_nocturna else 20000
    else:
        raise HTTPException(status_code=400, detail="Tipo de profesional inválido")

    # 5️⃣ Calcular según método de pago
    # Comisión estándar DocYa = 20%
    docya_comision = int(monto_total * 0.20)

    if metodo_pago == "efectivo":
        # Médico cobró el total → le debe 20% a DocYa
        medico_neto = monto_total
        saldo_delta = -docya_comision  # médico le debe a DocYa
        mensaje = f"Consulta registrada: el profesional debe a DocYa ${docya_comision}"
    else:
        # Tarjeta: DocYa cobra → debe pagar 80% al profesional
        medico_neto = int(monto_total * 0.80)
        saldo_delta = medico_neto      # DocYa le debe al profesional
        mensaje = f"Consulta registrada: DocYa debe pagar ${medico_neto} al profesional"

    # 6️⃣ Registrar el pago
    cur.execute("""
        INSERT INTO pagos_consulta 
        (consulta_id, medico_id, paciente_uuid, metodo_pago, monto_total, medico_neto, docya_comision)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (
        data.consulta_id,
        data.medico_id,
        data.paciente_uuid,
        metodo_pago,
        monto_total,
        medico_neto,
        docya_comision
    ))

    # 7️⃣ Actualizar saldo del profesional
    cur.execute("SELECT saldo FROM saldo_medico WHERE medico_id = %s", (data.medico_id,))
    row = cur.fetchone()

    if row:
        cur.execute("UPDATE saldo_medico SET saldo = saldo + %s WHERE medico_id = %s",
                    (saldo_delta, data.medico_id))
    else:
        cur.execute("INSERT INTO saldo_medico (medico_id, saldo) VALUES (%s, %s)",
                    (data.medico_id, saldo_delta))

    # 8️⃣ Marcar consulta como finalizada
    cur.execute("UPDATE consultas SET estado = 'finalizada' WHERE id = %s", (consulta_id,))

    db.commit()
    db.close()

    return {
        "ok": True,
        "consulta_id": consulta_id,
        "metodo_pago": metodo_pago,
        "monto_total": monto_total,
        "medico_neto": medico_neto,
        "docya_comision": docya_comision,
        "saldo_delta": saldo_delta,
        "tipo_profesional": tipo,
        "nocturna": es_nocturna,
        "mensaje": mensaje
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

# movido a auth_router.py
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

from uuid import UUID
from fastapi import HTTPException

@app.get("/pacientes/{paciente_uuid}/archivos")
def listar_archivos_paciente(paciente_uuid: str, db=Depends(get_db)):
    """
    Devuelve todas las recetas y certificados del paciente
    en formato compatible con el frontend Flutter.
    """

    # 🛑 VALIDACIÓN DEFENSIVA (OBLIGATORIA)
    if not paciente_uuid or paciente_uuid in ("null", "undefined"):
        raise HTTPException(
            status_code=400,
            detail="paciente_uuid inválido"
        )

    try:
        UUID(paciente_uuid)  # valida formato UUID real
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="paciente_uuid inválido"
        )

    cur = db.cursor()

    cur.execute(
        """
        SELECT email,
               COALESCE(NULLIF(numero_documento, ''), NULLIF(dni, '')) AS documento
        FROM users
        WHERE id = %s
        """,
        (paciente_uuid,),
    )
    paciente_row = cur.fetchone()
    if not paciente_row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    paciente_email = (paciente_row[0] or "").strip().lower() or None
    paciente_documento = (paciente_row[1] or "").strip() or None


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

    # Recetas del recetario nuevo
    cur.execute(
        """
        SELECT DISTINCT r.id, r.creado_en, m.full_name, m.especialidad
        FROM recetario_recetas r
        JOIN recetario_pacientes p ON p.id = r.paciente_id
        JOIN medicos m ON m.id = r.medico_id
        WHERE p.paciente_uuid = %s
           OR (%s IS NOT NULL AND lower(COALESCE(p.email, '')) = lower(%s))
           OR (%s IS NOT NULL AND COALESCE(p.nro_documento, '') = %s)
        """,
        (
            paciente_uuid,
            paciente_email,
            paciente_email,
            paciente_documento,
            paciente_documento,
        ),
    )
    for r in cur.fetchall():
        fecha = r[1].strftime("%d/%m/%Y %H:%M") if r[1] else "—"
        recetas.append({
            "tipo": "Receta",
            "id": r[0],
            "consulta_id": None,
            "fecha": fecha,
            "doctor": r[2],
            "especialidad": r[3],
            "url": f"https://docya-railway-production.up.railway.app/pacientes/{paciente_uuid}/recetario/recetas/{r[0]}",
        })

    # Certificados del recetario nuevo
    cur.execute(
        """
        SELECT DISTINCT c.id, c.creado_en, m.full_name, m.especialidad
        FROM recetario_certificados c
        JOIN recetario_pacientes p ON p.id = c.paciente_id
        JOIN medicos m ON m.id = c.medico_id
        WHERE p.paciente_uuid = %s
           OR (%s IS NOT NULL AND lower(COALESCE(p.email, '')) = lower(%s))
           OR (%s IS NOT NULL AND COALESCE(p.nro_documento, '') = %s)
        """,
        (
            paciente_uuid,
            paciente_email,
            paciente_email,
            paciente_documento,
            paciente_documento,
        ),
    )
    for c in cur.fetchall():
        fecha = c[1].strftime("%d/%m/%Y %H:%M") if c[1] else "—"
        certificados.append({
            "tipo": "Certificado",
            "id": c[0],
            "consulta_id": None,
            "fecha": fecha,
            "doctor": c[2],
            "especialidad": c[3],
            "url": f"https://docya-railway-production.up.railway.app/pacientes/{paciente_uuid}/recetario/certificados/{c[0]}",
        })

    # 🔄 Unificar y ordenar por fecha (más recientes primero)
    archivos = recetas + certificados
    archivos.sort(key=lambda x: x["fecha"], reverse=True)

    return archivos


def _build_recetario_html_redirect_url(
    medico_id: int,
    target_path: str,
) -> str:
    token = create_access_token(
        {"sub": str(medico_id), "role": "medico", "tipo": "recetario_html"},
        expires_minutes=20,
    )
    separator = "&" if "?" in target_path else "?"
    return f"https://docya-railway-production.up.railway.app{target_path}{separator}token={quote(token)}"


def _validar_documento_recetario_para_paciente(
    db,
    paciente_uuid: str,
    table_name: str,
    document_id: int,
):
    cur = db.cursor()
    cur.execute(
        """
        SELECT email,
               COALESCE(NULLIF(numero_documento, ''), NULLIF(dni, '')) AS documento
        FROM users
        WHERE id = %s
        """,
        (paciente_uuid,),
    )
    paciente_row = cur.fetchone()
    if not paciente_row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    paciente_email = (paciente_row[0] or "").strip().lower() or None
    paciente_documento = (paciente_row[1] or "").strip() or None

    cur.execute(
        f"""
        SELECT d.medico_id
        FROM {table_name} d
        JOIN recetario_pacientes p ON p.id = d.paciente_id
        WHERE d.id = %s
          AND (
            p.paciente_uuid = %s
            OR (%s IS NOT NULL AND lower(COALESCE(p.email, '')) = lower(%s))
            OR (%s IS NOT NULL AND COALESCE(p.nro_documento, '') = %s)
          )
        LIMIT 1
        """,
        (
            document_id,
            paciente_uuid,
            paciente_email,
            paciente_email,
            paciente_documento,
            paciente_documento,
        ),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return int(row[0])


@app.get("/pacientes/{paciente_uuid}/recetario/recetas/{receta_id}")
def abrir_receta_recetario_paciente(
    paciente_uuid: str,
    receta_id: int,
    db=Depends(get_db),
):
    medico_id = _validar_documento_recetario_para_paciente(
        db,
        paciente_uuid,
        "recetario_recetas",
        receta_id,
    )
    return RedirectResponse(
        url=_build_recetario_html_redirect_url(
            medico_id,
            f"/recetario/recetas/{receta_id}/html",
        )
    )


@app.get("/pacientes/{paciente_uuid}/recetario/certificados/{cert_id}")
def abrir_certificado_recetario_paciente(
    paciente_uuid: str,
    cert_id: int,
    db=Depends(get_db),
):
    medico_id = _validar_documento_recetario_para_paciente(
        db,
        paciente_uuid,
        "recetario_certificados",
        cert_id,
    )
    return RedirectResponse(
        url=_build_recetario_html_redirect_url(
            medico_id,
            f"/recetario/certificados/{cert_id}/html",
        )
    )

# --------------------------------------------------------------------------------
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


# ================================
@app.post("/consultas/crear_previa")
def crear_consulta_previa(data: dict, db = Depends(get_db)):
    cur = db.cursor()

    paciente_uuid = data.get("paciente_uuid")
    _validar_perfil_paciente_completo(db, str(paciente_uuid))
    motivo = data.get("motivo", "")
    direccion = data.get("direccion", "")
    lat = data.get("lat")
    lng = data.get("lng")
    tipo = data.get("tipo", "medico")

    # Crear consulta previa correctamente SIN marcar pago
    cur.execute("""
        INSERT INTO consultas (
            paciente_uuid, medico_id, estado,
            motivo, direccion, lat, lng, tipo,
            metodo_pago,
            mp_preautorizado, mp_capturado,
            mp_payment_id, mp_status
        )
        VALUES (
            %s, NULL, 'pendiente',
            %s, %s, %s, %s, %s,
            'tarjeta',
            FALSE, FALSE,
            NULL, NULL
        )
        RETURNING id, creado_en;
    """, (
        str(paciente_uuid),
        motivo,
        direccion,
        lat,
        lng,
        tipo
    ))

    consulta_id, creado_en = cur.fetchone()
    db.commit()

    print(f"🆕 Consulta previa creada ID={consulta_id}")

    return {
        "status": "ok",
        "consulta_id": consulta_id,
        "creado_en": str(creado_en)
    }

#FUNCIONES PARA CALCULOAR ETA ----------------------------------------------------
import math

from datetime import datetime, time

def calcular_eta_minutos(distancia_km):
    ahora = now_argentina().time()

    # Definir velocidades según horario
    if time(8, 0) <= ahora <= time(20, 0):
        velocidad = 22  # km/h
    else:
        velocidad = 30  # km/h (menos tráfico)

    if distancia_km <= 0.1:
        return 1  # está demasiado cerca, 1 minuto

    minutos = (distancia_km / velocidad) * 60
    return round(minutos)


@app.delete("/usuarios/{user_id}/delete", tags=["usuarios"])
def delete_account(user_id: str, db=Depends(get_db)):
    """
    Eliminación PERMANENTE de cuenta del paciente.
    Requisito de Apple (App Store Guideline 5.1.1(v)).
    """

    cur = db.cursor()

    # 1) Verificar si existe
    cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="El usuario no existe")

    # 2) Borrar pagos de consultas asociadas
    cur.execute("""
        DELETE FROM pagos_consulta
        WHERE consulta_id IN (
            SELECT id FROM consultas WHERE paciente_uuid = %s
        )
    """, (user_id,))

    # 3) Borrar consultas del paciente
    cur.execute("DELETE FROM consultas WHERE paciente_uuid = %s", (user_id,))

    # 4) Borrar al usuario
    cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

    db.commit()

    return {
        "status": "ok",
        "message": "Cuenta eliminada permanentemente"
    }

@app.delete("/medicos/{medico_id}/delete", tags=["medicos"])
def delete_medico(medico_id: int, db=Depends(get_db)):
    """
    Eliminación PERMANENTE de la cuenta del médico.
    Cumple con Apple (Guideline 5.1.1(v)).
    No elimina consultas históricas para preservar historia clínica.
    """

    cur = db.cursor()

    # 1) Verificar si el médico existe
    cur.execute("SELECT id FROM medicos WHERE id = %s", (medico_id,))
    medico = cur.fetchone()
    if not medico:
        raise HTTPException(status_code=404, detail="El médico no existe")

    # 2) Desvincular consultas históricas (NO se eliminan)
    cur.execute("""
        UPDATE consultas
        SET medico_id = NULL
        WHERE medico_id = %s
    """, (medico_id,))

    # 3) Eliminar tokens push del médico
    cur.execute("DELETE FROM fcm_tokens WHERE medico_id = %s", (medico_id,))

    # 4) Eliminar la cuenta del médico
    cur.execute("DELETE FROM medicos WHERE id = %s", (medico_id,))

    db.commit()

    return {
        "status": "ok",
        "message": "Cuenta de médico eliminada permanentemente"
    }

@app.post("/medico/{medico_id}/ping")
def ping_medico(medico_id: int, db=Depends(get_db)):
    try:
        ahora = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))

        cur = db.cursor()
        cur.execute("""
            UPDATE medicos
            SET ultimo_ping = %s
            WHERE id = %s
        """, (ahora, medico_id))

        db.commit()
        cur.close()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        print(f"⚠️ Error en ping médico {medico_id}: {e}")
        raise HTTPException(status_code=500, detail="Ping error")

@app.post("/admin/medicos/{medico_id}/desconectar")
def desconectar_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos 
        SET disponible=FALSE, activo=FALSE, latitud=NULL, longitud=NULL, updated_at=NOW()
        WHERE id=%s RETURNING id, full_name
    """, (medico_id,))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Profesional no encontrado")
    return {"ok": True, "medico_id": row[0], "nombre": row[1]}


#mapa 
@app.get("/monitoreo/medicos_mapa")
def medicos_mapa(db=Depends(get_db)):
    cur = db.cursor()

    cur.execute("""
        SELECT
            id,
            full_name,
            tipo,
            disponible,
            activo,
            latitud,
            longitud,
            telefono,
            matricula
        FROM medicos
        WHERE activo = TRUE
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
        ORDER BY full_name;
    """)

    rows = cur.fetchall()

    profesionales = [
        {
            "id": r[0],
            "nombre": r[1],
            "tipo": r[2],
            "disponible": r[3],
            "activo": r[4],
            "latitud": float(r[5]),
            "longitud": float(r[6]),
            "telefono": r[7],
            "matricula": r[8],
        }
        for r in rows
    ]

    return {
        "ok": True,
        "total": len(profesionales),
        "profesionales": profesionales
    }


# ====================================================
# 💰 TARIFAS DE CONSULTA
# ====================================================

@app.get("/tarifas/consulta-medico")
def get_tarifa_consulta_medico(conn=Depends(get_db)):
    forced_price = get_forced_consulta_price()
    if forced_price is not None:
        return {
            "tipo": "prueba",
            "monto": forced_price,
            "descripcion": "Monto temporal de prueba configurado desde backend.",
        }

    ahora = now_argentina()
    h = ahora.hour
    nocturno = h >= 22 or h < 6

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT tipo, monto, descripcion
        FROM tarifas_consulta
        WHERE tipo = %s AND activa = TRUE
        LIMIT 1
    """, ('nocturna' if nocturno else 'diurna',))
    tarifa = cur.fetchone()
    cur.close()

    if not tarifa:
        raise HTTPException(status_code=404, detail="Tarifa no configurada")

    return {
        "tipo": tarifa["tipo"],
        "monto": tarifa["monto"],
        "descripcion": tarifa["descripcion"],
    }


@app.get("/tarifas/consulta-enfermero")
def get_tarifa_consulta_enfermero(conn=Depends(get_db)):
    forced_price = get_forced_consulta_price()
    if forced_price is not None:
        return {
            "tipo": "prueba",
            "monto": forced_price,
            "descripcion": "Monto temporal de prueba configurado desde backend.",
        }

    ahora = now_argentina()
    h = ahora.hour
    nocturno = h >= 22 or h < 6

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT tipo, monto, descripcion
        FROM tarifas_consulta
        WHERE tipo = %s AND activa = TRUE
        LIMIT 1
    """, ('nocturna_enfermero' if nocturno else 'diurna_enfermero',))
    tarifa = cur.fetchone()
    cur.close()

    if not tarifa:
        raise HTTPException(status_code=404, detail="Tarifa no configurada")

    return {
        "tipo": tarifa["tipo"],
        "monto": tarifa["monto"],
        "descripcion": tarifa["descripcion"],
    }


# ====================================================
# 🗺️ ZONAS DE COBERTURA
# ====================================================

@app.get("/zonas-cobertura")
def get_zonas_cobertura(conn=Depends(get_db)):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, nombre, detalle, estado, orden
        FROM zonas_cobertura
        ORDER BY orden ASC, nombre ASC
    """)
    zonas = cur.fetchall()
    cur.close()
    return {
        "activas":  [z for z in zonas if z["estado"] == "activa"],
        "proximas": [z for z in zonas if z["estado"] == "proxima"],
    }

# ====================================================
# 💰 GESTIÓN DE TARIFAS (CRUD)
# ====================================================

@app.get("/admin/tarifas")
def get_all_tarifas(conn=Depends(get_db)):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM tarifas_consulta ORDER BY tipo ASC")
    tarifas = cur.fetchall()
    cur.close()
    return tarifas


@app.post("/admin/tarifas")
def crear_tarifa(payload: dict, conn=Depends(get_db)):
    """
    Body esperado:
    {
        "tipo": "diurna" | "nocturna" | "diurna_enfermero" | "nocturna_enfermero",
        "monto": 5000.00,
        "descripcion": "Consulta médica diurna",
        "activa": true
    }
    """
    tipo        = payload.get("tipo")
    monto       = payload.get("monto")
    descripcion = payload.get("descripcion", "")
    activa      = payload.get("activa", True)

    if not tipo or monto is None:
        raise HTTPException(status_code=400, detail="Campos 'tipo' y 'monto' son obligatorios")

    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Verificar que no exista ya ese tipo
    cur.execute("SELECT id FROM tarifas_consulta WHERE tipo = %s", (tipo,))
    if cur.fetchone():
        cur.close()
        raise HTTPException(status_code=409, detail=f"Ya existe una tarifa con tipo '{tipo}'")

    cur.execute("""
        INSERT INTO tarifas_consulta (tipo, monto, descripcion, activa)
        VALUES (%s, %s, %s, %s)
        RETURNING *
    """, (tipo, monto, descripcion, activa))
    nueva = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": "Tarifa creada exitosamente", "tarifa": nueva}


@app.put("/admin/tarifas/{tipo}")
def editar_tarifa(tipo: str, payload: dict, conn=Depends(get_db)):
    """
    Body esperado (todos opcionales, solo se actualizan los enviados):
    {
        "monto": 6000.00,
        "descripcion": "Nueva descripción",
        "activa": true
    }
    """
    campos_permitidos = {"monto", "descripcion", "activa"}
    updates = {k: v for k, v in payload.items() if k in campos_permitidos}

    if not updates:
        raise HTTPException(status_code=400, detail="No se enviaron campos válidos para actualizar")

    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Verificar que exista
    cur.execute("SELECT id FROM tarifas_consulta WHERE tipo = %s", (tipo,))
    if not cur.fetchone():
        cur.close()
        raise HTTPException(status_code=404, detail=f"No existe tarifa con tipo '{tipo}'")

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    valores    = list(updates.values()) + [tipo]

    cur.execute(f"""
        UPDATE tarifas_consulta
        SET {set_clause}
        WHERE tipo = %s
        RETURNING *
    """, valores)
    actualizada = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": "Tarifa actualizada exitosamente", "tarifa": actualizada}


@app.patch("/admin/tarifas/{tipo}/toggle")
def toggle_tarifa(tipo: str, conn=Depends(get_db)):
    """Activa o desactiva una tarifa sin tocar el resto de campos."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, activa FROM tarifas_consulta WHERE tipo = %s", (tipo,))
    tarifa = cur.fetchone()
    if not tarifa:
        cur.close()
        raise HTTPException(status_code=404, detail=f"No existe tarifa con tipo '{tipo}'")

    cur.execute("""
        UPDATE tarifas_consulta SET activa = %s WHERE tipo = %s RETURNING *
    """, (not tarifa["activa"], tipo))
    actualizada = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": f"Tarifa {'activada' if actualizada['activa'] else 'desactivada'}", "tarifa": actualizada}


# ====================================================
# 🗺️ GESTIÓN DE ZONAS DE COBERTURA (CRUD)
# ====================================================

@app.post("/admin/zonas-cobertura")
def crear_zona(payload: dict, conn=Depends(get_db)):
    """
    Body esperado:
    {
        "nombre": "Palermo",
        "detalle": "Palermo Soho, Hollywood y Chico",
        "estado": "activa" | "proxima",
        "orden": 1          ← opcional
    }
    """
    nombre  = payload.get("nombre")
    detalle = payload.get("detalle", "")
    estado  = payload.get("estado", "activa")
    orden   = payload.get("orden", 99)

    if not nombre:
        raise HTTPException(status_code=400, detail="El campo 'nombre' es obligatorio")

    if estado not in ("activa", "proxima"):
        raise HTTPException(status_code=400, detail="Estado debe ser 'activa' o 'proxima'")

    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id FROM zonas_cobertura WHERE nombre = %s", (nombre,))
    if cur.fetchone():
        cur.close()
        raise HTTPException(status_code=409, detail=f"Ya existe una zona llamada '{nombre}'")

    cur.execute("""
        INSERT INTO zonas_cobertura (nombre, detalle, estado, orden)
        VALUES (%s, %s, %s, %s)
        RETURNING *
    """, (nombre, detalle, estado, orden))
    nueva = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": "Zona creada exitosamente", "zona": nueva}


@app.put("/admin/zonas-cobertura/{zona_id}")
def editar_zona(zona_id: int, payload: dict, conn=Depends(get_db)):
    """
    Body esperado (todos opcionales):
    {
        "nombre": "Palermo",
        "detalle": "Descripción actualizada",
        "estado": "activa" | "proxima",
        "orden": 2
    }
    """
    campos_permitidos = {"nombre", "detalle", "estado", "orden"}
    updates = {k: v for k, v in payload.items() if k in campos_permitidos}

    if not updates:
        raise HTTPException(status_code=400, detail="No se enviaron campos válidos para actualizar")

    if "estado" in updates and updates["estado"] not in ("activa", "proxima"):
        raise HTTPException(status_code=400, detail="Estado debe ser 'activa' o 'proxima'")

    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id FROM zonas_cobertura WHERE id = %s", (zona_id,))
    if not cur.fetchone():
        cur.close()
        raise HTTPException(status_code=404, detail=f"No existe zona con id {zona_id}")

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    valores    = list(updates.values()) + [zona_id]

    cur.execute(f"""
        UPDATE zonas_cobertura
        SET {set_clause}
        WHERE id = %s
        RETURNING *
    """, valores)
    actualizada = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": "Zona actualizada exitosamente", "zona": actualizada}


@app.patch("/admin/zonas-cobertura/{zona_id}/estado")
def cambiar_estado_zona(zona_id: int, payload: dict, conn=Depends(get_db)):
    """
    Cambia solo el estado de una zona.
    Body: { "estado": "activa" | "proxima" }
    """
    estado = payload.get("estado")
    if estado not in ("activa", "proxima"):
        raise HTTPException(status_code=400, detail="Estado debe ser 'activa' o 'proxima'")

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM zonas_cobertura WHERE id = %s", (zona_id,))
    if not cur.fetchone():
        cur.close()
        raise HTTPException(status_code=404, detail=f"No existe zona con id {zona_id}")

    cur.execute("""
        UPDATE zonas_cobertura SET estado = %s WHERE id = %s RETURNING *
    """, (estado, zona_id))
    actualizada = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": f"Estado de zona actualizado a '{estado}'", "zona": actualizada}


@app.delete("/admin/zonas-cobertura/{zona_id}")
def eliminar_zona(zona_id: int, conn=Depends(get_db)):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id FROM zonas_cobertura WHERE id = %s", (zona_id,))
    if not cur.fetchone():
        cur.close()
        raise HTTPException(status_code=404, detail=f"No existe zona con id {zona_id}")

    cur.execute("DELETE FROM zonas_cobertura WHERE id = %s RETURNING nombre", (zona_id,))
    eliminada = cur.fetchone()
    conn.commit()
    cur.close()
    return {"message": f"Zona '{eliminada['nombre']}' eliminada exitosamente"}


# ====================================================
# 🖼️ NOTICIAS / CAROUSEL - Imágenes desde Cloudinary
# ====================================================
import cloudinary.api

@app.get("/noticias")
def obtener_noticias():
    try:
        result = cloudinary.api.resources_by_tag(
            "noticias-docya",
            resource_type="image",
            max_results=50
        )
        cloud = cloudinary.config().cloud_name
        urls = [
            f"https://res.cloudinary.com/{cloud}/image/upload/c_fill,w_1200,h_500,q_auto,f_auto/{r['public_id']}.{r['format']}"
            for r in result.get("resources", [])
        ]
        return {"urls": urls}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
