import os
import jwt
import psycopg2
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from dotenv import load_dotenv

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

    # Convertir a dict
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

#perfil datos --------------------------------------------------------------------

@app.get("/usuarios/{user_id}")
def obtener_usuario(user_id: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, full_name, email
        FROM users
        WHERE id = %s
    """, (user_id,))
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # buscar también el teléfono en la tabla direcciones (si existe)
    cur.execute("""
        SELECT telefono_contacto
        FROM direcciones
        WHERE user_id = %s
        LIMIT 1
    """, (user_id,))
    direccion = cur.fetchone()
    telefono = direccion[0] if direccion else None

    return {
        "id": row[0],
        "full_name": row[1],
        "email": row[2],
        "telefono": telefono
    }


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
                matricula, especialidad, telefono, provincia, localidad
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, full_name
        """, (
            data.full_name.strip(),
            data.email.lower(),
            password_hash,
            data.matricula,
            data.especialidad,
            data.telefono,
            data.provincia,
            data.localidad
        ))
        medico_id, full_name = cur.fetchone()
        db.commit()
    except Exception as e:
        db.rollback()
        print("❌ ERROR en register_medico:", e)
        raise HTTPException(status_code=500, detail="Error interno en registro")

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
            "full_name": full_name
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
        "SELECT id, full_name, password_hash FROM medicos WHERE email=%s",
        (data.email.lower(),)
    )
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    medico_id, full_name, password_hash = row

    if not password_hash or not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

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
            "full_name": full_name
        }
    }

#MANDA EL MEDICO SU UBICACION TODO MEL TIEMPO MIENTRAS ESTE DISPONIBLE
@app.post("/medico/{medico_id}/ubicacion")
def actualizar_ubicacion(medico_id: int, lat: float, lng: float, disponible: bool):
    db.execute("""
        UPDATE medicos 
        SET latitud = %s, longitud = %s, disponible = %s, updated_at = NOW()
        WHERE id = %s
    """, (lat, lng, disponible, medico_id))
    return {"status": "ok"}


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

class SolicitarConsultaIn(BaseModel):
    paciente_id: UUID
    motivo: str
    direccion: str
    lat: float
    lng: float



from pydantic import BaseModel

class SolicitarConsultaIn(BaseModel):
    paciente_id: int
    motivo: str
    direccion: str
    lat: float
    lng: float

@app.post("/consultas/solicitar")
def solicitar_consulta(data: SolicitarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()

    # 1. Buscar médico disponible más cercano
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

    # 2. Guardar la consulta en la tabla
    cur.execute("""
        INSERT INTO consultas (paciente_id, medico_id, estado, motivo, direccion)
        VALUES (%s, %s, 'pendiente', %s, %s)
        RETURNING id, creado_en
    """, (data.paciente_id, medico_id, data.motivo, data.direccion))

    consulta_id, creado_en = cur.fetchone()
    db.commit()

    return {
        "consulta_id": consulta_id,
        "paciente_id": data.paciente_id,
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


@app.post("/consultas/{consulta_id}/aceptar")
def aceptar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='aceptada', creado_en=NOW() WHERE id=%s RETURNING id", (consulta_id,))
    row = cur.fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"consulta_id": consulta_id, "estado": "aceptada"}


@app.post("/consultas/{consulta_id}/rechazar")
def rechazar_consulta(consulta_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='rechazada', creado_en=NOW() WHERE id=%s RETURNING id", (consulta_id,))
    row = cur.fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"consulta_id": consulta_id, "estado": "rechazada"}

@app.get("/consultas/mias/{medico_id}")
def consultas_mias(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, paciente_id, estado, motivo, direccion, creado_en
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
            "paciente_id": row[1],
            "estado": row[2],
            "motivo": row[3],
            "direccion": row[4],
            "creado_en": row[5]
        })

    return consultas

