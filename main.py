import os
import jwt
import psycopg2
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header, Query, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
load_dotenv()

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
    expire = datetime.utcnow() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")

from datetime import date
from typing import Optional

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

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"

class GoogleIn(BaseModel):
    id_token: str  # Obtained on device via Google Sign-In

app = FastAPI(title="DocYa Auth API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserOut(BaseModel):
    id: str
    full_name: str

class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut



@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}

from datetime import datetime

@app.post("/auth/register", response_model=AuthResponse)
def register(data: RegisterIn, db=Depends(get_db)):
    cur = db.cursor()

    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    password_hash = pwd_context.hash(data.password)

    try:
        cur.execute(
            """
            INSERT INTO users (
                email, full_name, password_hash,
                dni, telefono, pais, provincia, localidad, fecha_nacimiento,
                acepto_condiciones, fecha_aceptacion, version_texto
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, full_name, role
            """,
            (
                data.email.lower(),
                data.full_name.strip(),
                password_hash,
                data.dni,
                data.telefono,
                data.pais,
                data.provincia,
                data.localidad,
                data.fecha_nacimiento,   # debe ser date, no string
                data.acepto_condiciones,
                datetime.utcnow() if data.acepto_condiciones else None,
                "v1.0",
            )
        )
        user_id, full_name, role = cur.fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        print("❌ ERROR en register:", e)
        raise HTTPException(status_code=500, detail="Error interno en registro")

    enviar_correo_bienvenida(data.email, data.full_name, data.password)

    token = create_access_token({
        "sub": str(user_id),
        "email": data.email.lower(),
        "role": role
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user_id),
            "full_name": full_name
        }
    }







@app.post("/auth/login", response_model=AuthResponse)
def login(data: LoginIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, full_name, password_hash, role FROM users WHERE email=%s", (data.email.lower(),))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    user_id, full_name, password_hash, role = row

    if not password_hash or not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    token = create_access_token({"sub": str(user_id), "email": data.email.lower(), "role": role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user_id),
            "full_name": full_name
        }
    }


# Minimal Google ID token verification without external libs (signature is NOT verified here).
# In production, verify with Google's certs (google-auth library). For now, we decode without verify for demo.
import base64
import json

def decode_id_token_no_verify(id_token: str):
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "==="
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return None

from google.oauth2 import id_token
from google.auth.transport import requests

@app.post("/auth/google", response_model=AuthResponse)
def auth_google(data: GoogleIn, db=Depends(get_db)):
    try:
        payload = id_token.verify_oauth2_token(
            data.id_token,
            requests.Request(),
            os.getenv("GOOGLE_CLIENT_ID")
        )
    except Exception:
        raise HTTPException(status_code=400, detail="id_token inválido")

    sub = payload["sub"]
    email = payload["email"].lower()
    name = payload.get("name") or email.split("@")[0]
    picture = payload.get("picture")

    cur = db.cursor()
    cur.execute("""
        SELECT u.id, u.full_name, u.role FROM auth_providers ap
        JOIN users u ON ap.user_id = u.id
        WHERE ap.provider=%s AND ap.provider_uid=%s
    """, ("google", sub))
    row = cur.fetchone()

    if row:
        user_id, full_name, role = row
    else:
        cur.execute("SELECT id, full_name, role FROM users WHERE email=%s", (email,))
        user = cur.fetchone()
        if user:
            user_id, full_name, role = user
        else:
            cur.execute(
                "INSERT INTO users (email, full_name, avatar_url) VALUES (%s, %s, %s) RETURNING id, full_name, role",
                (email, name, picture)
            )
            user_id, full_name, role = cur.fetchone()

        cur.execute(
            "INSERT INTO auth_providers (user_id, provider, provider_uid) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, "google", sub)
        )
        db.commit()

    token = create_access_token({"sub": str(user_id), "email": email, "role": role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user_id),
            "full_name": full_name
        }
    }




#para envio de correo email de bienveida ----------------------------------------------------------------------
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL")

import os
import requests

BREVO_API_KEY = os.getenv("BREVO_API_KEY")  # en Railway ponés tu clave SMTP como variable

import os
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "nahundeveloper@gmail.com")

def enviar_correo_bienvenida(destinatario: str, nombre: str, password: str):
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY

    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = "Bienvenido a DocYa 🚀"
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #f4f6f8; padding: 20px; margin:0;">
        <div style="max-width: 600px; margin: auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 6px rgba(0,0,0,0.15);">
          
          <!-- Header -->
          <div style="background: #14B8A6; padding: 20px; text-align: center;">
            <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1756262934/b73a7a0d-b1dd-4e93-93fb-867d205a2031_qc39ml.png" alt="DocYa" style="height: 120px;"/>
            <h1 style="color: white; margin: 10px 0 0; font-size: 24px;">¡Bienvenido a DocYa!</h1>
          </div>
    
          <!-- Body -->
          <div style="padding: 30px; color: #333333;">
            <p style="font-size: 16px;">Hola <b>{nombre}</b>,</p>
            <p style="font-size: 15px; line-height: 1.6;">
              Nos alegra darte la bienvenida a <b>DocYa</b>, tu nuevo sistema de salud en la palma de tu mano.
            </p>
            
            <p style="font-size: 15px;">Estos son tus datos de acceso:</p>
            <div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; margin: 20px 0;">
              <p><b>Email:</b> {destinatario}</p>
              <p><b>Contraseña:</b> {password}</p>
            </div>
    
            <p style="font-size: 15px; line-height: 1.6;">
              Ya podés ingresar desde la app y comenzar a disfrutar de nuestros servicios médicos a domicilio, de manera rápida y segura.
            </p>
    
            <div style="text-align: center; margin: 30px 0;">
              <a href="https://play.google.com/store" 
                 style="background: #14B8A6; color: white; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-size: 16px;">
                Iniciar en DocYa
              </a>
            </div>
          </div>
    
          <!-- Footer -->
          <div style="background: #f1f5f9; padding: 15px; text-align: center; font-size: 12px; color: #6b7280;">
            <p>© 2025 DocYa · Salud a tu puerta</p>
          </div>
        </div>
      </body>
    </html>
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": destinatario, "name": nombre}],
        sender={"email": FROM_EMAIL, "name": "DocYa"},
        subject=subject,
        html_content=html_content
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        print("Correo enviado con éxito 🚀")
    except ApiException as e:
        print(f"Error enviando correo: {e}")


# GUARDAMOS LA DIRECCION DE CADA PACIENTE -------------------------------------------
from fastapi import HTTPException, Depends
from pydantic import BaseModel
from uuid import UUID

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

    # Verificar que el usuario exista
    cur.execute("SELECT id FROM users WHERE id=%s", (str(data.user_id),))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Insertar o actualizar dirección
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

    result = {
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

    print(f"📍 Dirección devuelta: {result}")  # 👈 Log en Railway

    return result


    

#perfil datos --------------------------------------------------------------------




#registro medicos -----------------------------------
from pydantic import BaseModel

class RegisterMedicoIn(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    matricula: str
    especialidad: str
    telefono: str | None = None
    provincia: str | None = None
    localidad: str | None = None
    dni: str | None = None
    foto_perfil: str | None = None       # 👈 URL en Cloudinary
    foto_dni_frente: str | None = None
    foto_dni_dorso: str | None = None
    selfie_dni: str | None = None



@app.post("/auth/register_medico")
def register_medico(data: RegisterMedicoIn, db=Depends(get_db)):
    cur = db.cursor()

    # validar email
    cur.execute("SELECT id FROM medicos WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")

    # validar matrícula
    cur.execute("SELECT id FROM medicos WHERE matricula=%s", (data.matricula,))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    password_hash = pwd_context.hash(data.password)

    try:
        cur.execute("""
            INSERT INTO medicos (
                full_name, email, password_hash,
                matricula, especialidad, telefono, provincia, localidad,
                dni, foto_perfil, foto_dni_frente, foto_dni_dorso, selfie_dni, validado
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
            RETURNING id, full_name
        """, (
            data.full_name.strip(),
            data.email.lower(),
            password_hash,
            data.matricula,
            data.especialidad,
            data.telefono,
            data.provincia,
            data.localidad,
            data.dni,
            data.foto_perfil,
            data.foto_dni_frente,
            data.foto_dni_dorso,
            data.selfie_dni
        ))
        medico_id, full_name = cur.fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        print("❌ ERROR en register_medico:", e)
        raise HTTPException(status_code=500, detail="Error interno en registro")

    # ⚠️ IMPORTANTE: aunque devolvemos token, el médico no podrá loguearse hasta que se valide
    token = create_access_token({
        "sub": str(medico_id),
        "email": data.email.lower(),
        "role": "medico"
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "medico": {
            "id": str(medico_id),
            "full_name": full_name,
            "validado": False
        }
    }

#login medicos ---------------------------------------------------------------------------------
class LoginMedicoIn(BaseModel):
    email: EmailStr
    password: str

@app.post("/auth/login_medico")
def login_medico(data: LoginMedicoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, full_name, password_hash, validado
        FROM medicos
        WHERE email=%s
        """,
        (data.email.lower(),)
    )
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    medico_id, full_name, password_hash, validado = row

    # Verificar contraseña
    if not password_hash or not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    # Verificar validación manual
    if not validado:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta aún no fue validada. Te avisaremos cuando esté habilitada."
        )

    token = create_access_token({
        "sub": str(medico_id),
        "email": data.email.lower(),
        "role": "medico"
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "medico": {
            "id": str(medico_id),
            "full_name": full_name,
            "validado": True
        }
    }




@app.post("/medicos/{medico_id}/validar")
def validar_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET validado = TRUE, updated_at = NOW()
        WHERE id = %s
        RETURNING id, full_name
    """, (medico_id,))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")

    return {"ok": True, "medico_id": row[0], "nombre": row[1]}


#MANDA EL MEDICO SU UBICACION TODO MEL TIEMPO MIENTRAS ESTE DISPONIBLE
#MANDA EL MEDICO SU UBICACION TODO EL TIEMPO MIENTRAS ESTE DISPONIBLE
from pydantic import BaseModel

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




# PACIENTE SOLICITA CONSULTA
from pydantic import BaseModel
class SolicitarConsultaIn(BaseModel):
    paciente_uuid: Optional[int] = None
    paciente_uuid: Optional[str] = None
    motivo: str
    direccion: str
    lat: float
    lng: float




#PACIENTE BUSCA MEDICO 
from fastapi import Depends

@app.get("/buscar_medico_cercano")
def buscar_medico(lat: float = Query(...), lng: float = Query(...), db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, full_name, latitud, longitud,
        (6371 * acos(
            cos(radians(%s)) * cos(radians(latitud)) *
            cos(radians(longitud) - radians(%s)) +
            sin(radians(%s)) * sin(radians(latitud))
        )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
        ORDER BY distancia ASC
        LIMIT 1
    """, (lat, lng, lat))

    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No hay médicos disponibles cerca tuyo")

    return {
        "id": row[0],
        "full_name": row[1],
        "lat": row[2],
        "lng": row[3],
        "distancia_km": round(row[4], 2)
    }


from pydantic import BaseModel
from uuid import UUID



from pydantic import BaseModel

from uuid import UUID
from typing import Optional

class SolicitarConsultaIn(BaseModel):
    paciente_uuid: UUID   # 👈 ahora obligatorio
    motivo: str
    direccion: str
    lat: float
    lng: float


@app.post("/consultas/solicitar")
async def solicitar_consulta(data: SolicitarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()

    # Buscar médico disponible más cercano
    cur.execute("""
        SELECT id, full_name, latitud, longitud,
        (6371 * acos(
            cos(radians(%s)) * cos(radians(latitud)) *
            cos(radians(longitud) - radians(%s)) +
            sin(radians(%s)) * sin(radians(latitud))
        )) AS distancia
        FROM medicos
        WHERE disponible = TRUE
          AND latitud IS NOT NULL
          AND longitud IS NOT NULL
        ORDER BY distancia ASC
        LIMIT 1
    """, (data.lat, data.lng, data.lat))

    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No hay médicos disponibles en este momento")

    medico_id, medico_nombre, medico_lat, medico_lng, distancia = row

    # Guardar la consulta con paciente_uuid
    cur.execute("""
        INSERT INTO consultas (paciente_uuid, medico_id, estado, motivo, direccion, lat, lng)
        VALUES (%s, %s, 'pendiente', %s, %s, %s, %s)
        RETURNING id, creado_en
    """, (str(data.paciente_uuid), medico_id, data.motivo, data.direccion, data.lat, data.lng))

    consulta_id, creado_en = cur.fetchone()
    db.commit()

    # 🔔 Notificar al médico si está conectado por WS
    if medico_id in active_medicos:
        try:
            await active_medicos[medico_id].send_json({
                "tipo": "consulta_nueva",
                "consulta_id": consulta_id,
                "paciente_uuid": str(data.paciente_uuid),
                "motivo": data.motivo,
                "direccion": data.direccion,
                "lat": data.lat,
                "lng": data.lng,
                "distancia_km": round(distancia, 2),
                "creado_en": str(creado_en)
            })
            print(f"📨 Consulta {consulta_id} enviada en tiempo real al médico {medico_id}")
        except Exception as e:
            print(f"⚠️ No se pudo notificar al médico {medico_id}: {e}")

    # 🔔 Notificar al médico por Push (FCM)
    cur.execute("SELECT fcm_token FROM medicos WHERE id = %s", (medico_id,))
    row = cur.fetchone()
    if row and row[0]:
        try:
            enviar_push(
                row[0],  # fcm_token
                "📢 Nueva consulta disponible",
                f"Paciente solicita atención: {data.motivo}",
                {
                    "tipo": "consulta_nueva",
                    "consulta_id": str(consulta_id),
                    "paciente_uuid": str(data.paciente_uuid),
                    "direccion": data.direccion,
                    "lat": str(data.lat),
                    "lng": str(data.lng),
                    "creado_en": str(creado_en)
                }
            )
            print(f"📩 Push notification enviada al médico {medico_id}")
        except Exception as e:
            print(f"⚠️ Error enviando push notification al médico {medico_id}: {e}")

    return {
        "consulta_id": consulta_id,
        "paciente_uuid": str(data.paciente_uuid),
        "medico": {
            "id": medico_id,
            "nombre": medico_nombre,
            "lat": medico_lat,
            "lng": medico_lng,
            "distancia_km": round(distancia, 2)
        },
        "motivo": data.motivo,
        "direccion": data.direccion,
        "estado": "pendiente",
        "creado_en": creado_en
    }



@app.get("/consultas/debug/{consulta_id}")
def debug_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, paciente_uuid, paciente_uuid FROM consultas WHERE id = %s", (consulta_id,))
    return cur.fetchone()

from fastapi import HTTPException

@app.get("/consultas/mias/{medico_id}")
def consultas_mias(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, paciente_uuid, estado, motivo, direccion, creado_en
        FROM consultas
        WHERE medico_id = %s
          AND estado IN ('pendiente','aceptada')
        ORDER BY creado_en DESC
    """, (medico_id,))

    rows = cur.fetchall()
    consultas = []
    for row in rows:
        consultas.append({
            "id": row[0],
            "paciente_uuid": row[1],
            "estado": row[2],
            "motivo": row[3],
            "direccion": row[4],
            "creado_en": row[5]
        })

    return consultas


#El backend asigna al médico disponible más cercano
#Creamos un endpoint para el médico que pregunte: "¿tengo consultas nuevas?"
import math

def calcular_distancia(lat1, lon1, lat2, lon2):
    R = 6371  # radio Tierra en km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

@app.get("/consultas/asignadas/{medico_id}")
def consultas_asignadas(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT 
            c.id,
            c.paciente_uuid,
            COALESCE(u.full_name, 'Paciente desconocido') AS paciente_nombre,
            c.motivo,
            c.direccion,
            c.lat,
            c.lng,
            c.estado,
            m.latitud,
            m.longitud
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        LEFT JOIN users u ON c.paciente_uuid = u.id
        WHERE c.medico_id = %s AND c.estado = 'pendiente'
        ORDER BY c.creado_en DESC
        LIMIT 1
    """, (medico_id,))
    row = cur.fetchone()

    if not row:
        return {"consulta": None}

    (consulta_id, paciente_uuid, paciente_nombre,
     motivo, direccion, lat, lng, estado, med_lat, med_lng) = row

    # ⚠️ validar coordenadas
    if not med_lat or not med_lng or not lat or not lng:
        return {
            "id": consulta_id,
            "paciente_uuid": str(paciente_uuid) if paciente_uuid else None,
            "paciente_nombre": paciente_nombre,
            "motivo": motivo,
            "direccion": direccion,
            "lat": lat,
            "lng": lng,
            "estado": estado,
            "distancia_km": None,
            "tiempo_estimado_min": None
        }

    # calcular distancia y tiempo
    dist_km = calcular_distancia(med_lat, med_lng, lat, lng)
    tiempo_min = (dist_km / 40) * 60

    return {
        "id": consulta_id,
        "paciente_uuid": str(paciente_uuid) if paciente_uuid else None,
        "paciente_nombre": paciente_nombre,
        "motivo": motivo,
        "direccion": direccion,
        "lat": lat,
        "lng": lng,
        "estado": estado,
        "distancia_km": round(dist_km, 2),
        "tiempo_estimado_min": round(tiempo_min)
    }




from pydantic import BaseModel

class MedicoAccion(BaseModel):
    medico_id: int

# 🔹 Aceptar consulta
@app.post("/consultas/{consulta_id}/aceptar")
def aceptar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'aceptada', medico_id = %s
        WHERE id = %s AND estado = 'pendiente'
        RETURNING id
    """, (data.medico_id, consulta_id))
    consulta = cur.fetchone()
    db.commit()

    if not consulta:
        raise HTTPException(status_code=404, detail="Consulta no encontrada o ya asignada")

    return {"status": "ok", "id": consulta[0]}


# 🔹 Rechazar consulta
@app.post("/consultas/{consulta_id}/rechazar")
def rechazar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'pendiente',  -- vuelve a pendiente
            medico_id = NULL       -- liberamos al médico anterior
        WHERE id = %s AND estado = 'pendiente'
        RETURNING id
    """, (consulta_id,))
    consulta = cur.fetchone()
    db.commit()

    if not consulta:
        raise HTTPException(status_code=404, detail="Consulta no encontrada o ya reasignada")

    return {"status": "reopened", "id": consulta[0]}


from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor

router = APIRouter(prefix="/medicos", tags=["Medicos"])


@router.post("/{medico_id}/disponibilidad")
def actualizar_disponibilidad(medico_id: int, disponible: bool, db=Depends(get_db)):
    try:
        cur = db.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "UPDATE medicos SET disponible = %s WHERE id = %s RETURNING id, disponible",
            (disponible, medico_id)
        )
        result = cur.fetchone()
        db.commit()
        cur.close()

        if not result:
            raise HTTPException(status_code=404, detail="Médico no encontrado")

        return {"ok": True, "medico_id": medico_id, "disponible": result["disponible"]}
    except Exception as e:
        db.rollback()
        print("❌ Error al actualizar disponibilidad:", e)
        raise HTTPException(status_code=500, detail="Error interno")


@router.get("/{medico_id}/stats")
def medico_stats(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()

    # Consultas del mes
    cur.execute("""
        SELECT COUNT(*) 
        FROM consultas
        WHERE medico_id = %s 
          AND estado = 'aceptada'
          AND DATE_TRUNC('month', creado_en) = DATE_TRUNC('month', CURRENT_DATE);
    """, (medico_id,))
    total_consultas = cur.fetchone()[0]

    # Ganancias del mes
    cur.execute("""
        SELECT COUNT(*) * 24000
        FROM consultas
        WHERE medico_id = %s 
          AND estado = 'aceptada'
          AND DATE_TRUNC('month', creado_en) = DATE_TRUNC('month', CURRENT_DATE);
    """, (medico_id,))
    ganancias = cur.fetchone()[0] or 0

    cur.close()
    return {"consultas": total_consultas, "ganancias": ganancias}


# 👇 Muy importante: recién al final montamos el router en la app
app.include_router(router)

@app.get("/pacientes/{user_id}")
def obtener_paciente(user_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, full_name, email
        FROM users
        WHERE id = %s
    """, (user_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    return {"id": row[0], "full_name": row[1], "email": row[2]}


@app.get("/medicos/{medico_id}")
def obtener_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, full_name, email, especialidad, telefono, alias_cbu, matricula, foto_perfil
        FROM medicos
        WHERE id = %s
    """, (medico_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")

    return {
        "id": row[0],
        "full_name": row[1],
        "email": row[2],
        "especialidad": row[3],
        "telefono": row[4],
        "alias_cbu": row[5],
        "matricula": row[6],
        "foto_perfil": row[7],
    }


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
        "creado_en": row[8],
        "medico_nombre": row[9],
        "medico_matricula": row[10],
    }

#Endpoint para que el paciente vea en tiempo real la ubicación
@app.get("/consultas/{consulta_id}/ubicacion_medico")
def ubicacion_medico_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT m.id, m.full_name, m.latitud, m.longitud, m.telefono
        FROM consultas c
        JOIN medicos m ON c.medico_id = m.id
        WHERE c.id = %s AND c.estado = 'aceptada'
    """, (consulta_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No se encontró ubicación para esta consulta")

    return {
        "medico_id": row[0],
        "nombre": row[1],
        "lat": row[2],
        "lng": row[3],
        "telefono": row[4]
    }
class LlegoIn(BaseModel):
    medico_id: int



# Configuración desde variables de entorno
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

@app.post("/medicos/{medico_id}/foto")
def actualizar_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    try:
        # Subir a Cloudinary
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="docya/medicos",
            public_id=f"medico_{medico_id}",  # 👈 opcional: sobrescribe siempre la última
            overwrite=True
        )
        foto_url = upload_result["secure_url"]

        # Guardar en la base
        cur = db.cursor()
        cur.execute("""
            UPDATE medicos
            SET foto_perfil = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING id, foto_perfil
        """, (foto_url, medico_id))
        row = cur.fetchone()
        db.commit()

        if not row:
            raise HTTPException(status_code=404, detail="Médico no encontrado")

        return {"ok": True, "medico_id": row[0], "foto_url": row[1]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo foto: {e}")


@app.post("/consultas/{consulta_id}/llego")
def medico_llego(consulta_id: int, data: LlegoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'en_domicilio'
        WHERE id = %s AND medico_id = %s AND estado = 'aceptada'
        RETURNING id
    """, (consulta_id, data.medico_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada o estado inválido")

    return {"ok": True, "consulta_id": row[0], "estado": "en_domicilio"}

class FinalizarConsultaIn(BaseModel):
    medico_id: int

@app.post("/consultas/{consulta_id}/finalizar")
def finalizar_consulta(consulta_id: int, data: FinalizarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'finalizada'
        WHERE id = %s AND medico_id = %s 
          AND estado IN ('aceptada', 'en_domicilio')
        RETURNING id
    """, (consulta_id, data.medico_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada o ya finalizada")

    return {"ok": True, "consulta_id": row[0], "estado": "finalizada"}
@app.get("/consultas/historial/{paciente_uuid}")
def historial_consultas(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.estado, c.motivo, c.direccion, c.creado_en,
               m.full_name AS medico_nombre, m.especialidad
        FROM consultas c
        LEFT JOIN medicos m ON c.medico_id = m.id
        WHERE c.paciente_uuid = %s
        ORDER BY c.creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()

    historial = []
    for row in rows:
        historial.append({
            "id": row[0],
            "estado": row[1],
            "motivo": row[2],
            "direccion": row[3],
            "creado_en": row[4],
            "medico": {
                "nombre": row[5],
                "especialidad": row[6]
            }
        })

    return historial


from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict

# Guardamos conexiones activas de médicos
active_medicos: Dict[int, WebSocket] = {}

@app.websocket("/ws/medico/{medico_id}")
async def medico_ws(websocket: WebSocket, medico_id: int):
    await websocket.accept()
    active_medicos[medico_id] = websocket
    print(f"👨‍⚕️ Médico {medico_id} conectado vía WS")

    try:
        while True:
            data = await websocket.receive_text()
            if data == '{"tipo":"ping"}':
                print(f"❤️ Ping recibido de médico {medico_id}")
    except WebSocketDisconnect:
        print(f"❌ Médico {medico_id} desconectado")
        if medico_id in active_medicos:
            del active_medicos[medico_id]

from pydantic import BaseModel

class FcmTokenIn(BaseModel):
    fcm_token: str

@app.post("/medicos/{medico_id}/fcm_token")
def actualizar_fcm_token(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET fcm_token = %s, updated_at = NOW()
        WHERE id = %s
        RETURNING id
    """, (data.fcm_token, medico_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")

    return {"ok": True, "medico_id": medico_id, "fcm_token": data.fcm_token}


import requests, json, os
from google.oauth2 import service_account
import google.auth.transport.requests

# 🔑 Guardá todo el JSON de la cuenta de servicio en Railway como:
# GOOGLE_APPLICATION_CREDENTIALS_JSON={...}
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
credentials = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/firebase.messaging"]
)

def get_access_token():
    """Genera un token OAuth2 válido para llamar a FCM V1"""
    request = google.auth.transport.requests.Request()
    credentials.refresh(request)
    return credentials.token

def enviar_push(fcm_token: str, titulo: str, cuerpo: str, data: dict = {}):
    """
    Envía una notificación push usando FCM V1
    """
    project_id = service_account_info["project_id"]
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json; UTF-8",
    }

    payload = {
        "message": {
            "token": fcm_token,
            "notification": {
                "title": titulo,
                "body": cuerpo
            },
            "data": data
        }
    }

    r = requests.post(url, headers=headers, json=payload)
    print("📤 Push enviado:", r.status_code, r.text)
    return r.json()


#test eliominar depues notificaciones 
@app.post("/test_push/{medico_id}")
def test_push(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT fcm_token FROM medicos WHERE id = %s", (medico_id,))
    row = cur.fetchone()

    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Este médico no tiene un fcm_token registrado")

    fcm_token = row[0]

    try:
        enviar_push(
            fcm_token,
            "📢 Notificación de prueba",
            "Esto es una notificación de prueba de DocYa",
            {
                "tipo": "test_push",
                "mensaje": "Hola doctor 👨‍⚕️, esta es una notificación de prueba",
                "medico_id": str(medico_id)
            }
        )
        return {"ok": True, "mensaje": "Notificación de prueba enviada"}
    except Exception as e:
        print(f"⚠️ Error enviando test push: {e}")
        raise HTTPException(status_code=500, detail="Error enviando push")


from pydantic import BaseModel

# ---------- MODELOS ----------
class CertificadoIn(BaseModel):
    medico_id: int
    paciente_uuid: str
    contenido: str

class RecetaIn(BaseModel):
    medico_id: int
    paciente_uuid: str
    medicamentos: list[dict]  # [{nombre, dosis, frecuencia, duracion}]

class NotaIn(BaseModel):
    medico_id: int
    paciente_uuid: str
    contenido: str

# ---------- CERTIFICADO ----------
@app.post("/consultas/{consulta_id}/certificado")
def crear_certificado(consulta_id: int, data: CertificadoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        INSERT INTO certificados (consulta_id, medico_id, paciente_uuid, contenido)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (consulta_id, data.medico_id, data.paciente_uuid, data.contenido))
    certificado_id = cur.fetchone()[0]
    db.commit()
    return {"ok": True, "certificado_id": certificado_id}

# ---------- RECETA ----------
@app.post("/consultas/{consulta_id}/receta")
def crear_receta(consulta_id: int, data: RecetaIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        INSERT INTO recetas (consulta_id, medico_id, paciente_uuid)
        VALUES (%s, %s, %s) RETURNING id
    """, (consulta_id, data.medico_id, data.paciente_uuid))
    receta_id = cur.fetchone()[0]

    for med in data.medicamentos:
        cur.execute("""
            INSERT INTO receta_items (receta_id, nombre, dosis, frecuencia, duracion)
            VALUES (%s, %s, %s, %s, %s)
        """, (receta_id, med["nombre"], med["dosis"], med["frecuencia"], med["duracion"]))

    db.commit()
    return {"ok": True, "receta_id": receta_id}

# ---------- NOTAS ----------
@app.post("/consultas/{consulta_id}/nota")
def crear_nota(consulta_id: int, data: NotaIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        INSERT INTO notas_medicas (consulta_id, medico_id, paciente_uuid, contenido)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (consulta_id, data.medico_id, data.paciente_uuid, data.contenido))
    nota_id = cur.fetchone()[0]
    db.commit()
    return {"ok": True, "nota_id": nota_id}


class EnCaminoIn(BaseModel):
    medico_id: int

@app.post("/consultas/{consulta_id}/encamino")
def medico_encamino(consulta_id: int, data: EnCaminoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'en_camino'
        WHERE id = %s AND medico_id = %s AND estado = 'aceptada'
        RETURNING id
    """, (consulta_id, data.medico_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada o estado inválido")

    return {"ok": True, "consulta_id": row[0], "estado": "en_camino"}


from pydantic import BaseModel

class AceptarConsultaIn(BaseModel):
    medico_id: int

@app.post("/consultas/{consulta_id}/aceptar")
def aceptar_consulta(consulta_id: int, data: AceptarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE consultas
        SET estado = 'aceptada',
            medico_id = %s
        WHERE id = %s AND estado = 'pendiente'
        RETURNING id
    """, (data.medico_id, consulta_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(
            status_code=400,
            detail="Consulta no encontrada o ya fue aceptada"
        )

    return {"ok": True, "consulta_id": row[0], "estado": "aceptada"}


from pydantic import BaseModel

class AliasIn(BaseModel):
    alias: str

@app.patch("/medicos/{medico_id}/alias")
def actualizar_alias(medico_id: int, data: AliasIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET alias_cbu = %s, updated_at = NOW()
        WHERE id = %s
        RETURNING id, alias_cbu
    """, (data.alias, medico_id))
    row = cur.fetchone()
    db.commit()

    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")

    return {"ok": True, "medico_id": medico_id, "alias": row[1]}
