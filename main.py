# ====================================================
# 📌 IMPORTS Y CONFIGURACIÓN INICIAL
# ====================================================
import os
import jwt
import psycopg2
import json
import math
import requests
from datetime import datetime, timedelta, date
from typing import Optional, Dict
from uuid import UUID

from fastapi import (
    FastAPI, HTTPException, Depends, Query,
    File, UploadFile, WebSocket, WebSocketDisconnect
)
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
    expire = datetime.utcnow() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")

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


@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}


@app.post("/auth/register", response_model=AuthResponse)
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
                acepto_condiciones, fecha_aceptacion, version_texto
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, full_name, role
        """, (
            data.email.lower(), data.full_name.strip(), password_hash,
            data.dni, data.telefono, data.pais, data.provincia, data.localidad,
            data.fecha_nacimiento, data.acepto_condiciones,
            datetime.utcnow() if data.acepto_condiciones else None, "v1.0"
        ))
        user_id, full_name, role = cur.fetchone()
        db.commit()
    except:
        db.rollback()
        raise HTTPException(status_code=500, detail="Error interno en registro")

    token = create_access_token({"sub": str(user_id), "email": data.email.lower(), "role": role})
    return {"access_token": token, "token_type": "bearer", "user": {"id": str(user_id), "full_name": full_name}}


@app.post("/auth/login", response_model=AuthResponse)
def login(data: LoginIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, full_name, password_hash, role FROM users WHERE email=%s", (data.email.lower(),))
    row = cur.fetchone()
    if not row or not pwd_context.verify(data.password, row[2]):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")
    token = create_access_token({"sub": str(row[0]), "email": data.email.lower(), "role": row[3]})
    return {"access_token": token, "token_type": "bearer", "user": {"id": str(row[0]), "full_name": row[1]}}


@app.post("/auth/google", response_model=AuthResponse)
def auth_google(data: GoogleIn, db=Depends(get_db)):
    try:
        payload = id_token.verify_oauth2_token(data.id_token, google_requests.Request(), os.getenv("GOOGLE_CLIENT_ID"))
    except Exception:
        raise HTTPException(status_code=400, detail="id_token inválido")

    email = payload["email"].lower()
    name = payload.get("name") or email.split("@")[0]
    picture = payload.get("picture")
    sub = payload["sub"]

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
            cur.execute("""
                INSERT INTO users (email, full_name, avatar_url)
                VALUES (%s,%s,%s) RETURNING id, full_name, role
            """, (email, name, picture))
            user_id, full_name, role = cur.fetchone()

        cur.execute("""
            INSERT INTO auth_providers (user_id, provider, provider_uid)
            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
        """, (user_id, "google", sub))
        db.commit()

    token = create_access_token({"sub": str(user_id), "email": email, "role": role})
    return {"access_token": token, "token_type": "bearer", "user": {"id": str(user_id), "full_name": full_name}}

# ====================================================
# 👨‍⚕️ MÉDICOS (Rutas originales bajo /auth)
# ====================================================

class RegisterMedicoIn(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    matricula: str
    especialidad: str
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
    cur.execute("SELECT id FROM medicos WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
    cur.execute("SELECT id FROM medicos WHERE matricula=%s", (data.matricula,))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="La matrícula ya está registrada")

    password_hash = pwd_context.hash(data.password)
    cur.execute("""
        INSERT INTO medicos (
            full_name,email,password_hash,matricula,especialidad,telefono,
            provincia,localidad,dni,foto_perfil,foto_dni_frente,foto_dni_dorso,selfie_dni,validado
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)
        RETURNING id, full_name
    """, (
        data.full_name.strip(), data.email.lower(), password_hash,
        data.matricula, data.especialidad, data.telefono,
        data.provincia, data.localidad, data.dni,
        data.foto_perfil, data.foto_dni_frente, data.foto_dni_dorso, data.selfie_dni
    ))
    medico_id, full_name = cur.fetchone()
    db.commit()
    token = create_access_token({"sub": str(medico_id), "email": data.email.lower(), "role": "medico"})
    return {"access_token": token, "token_type": "bearer",
            "medico": {"id": str(medico_id), "full_name": full_name, "validado": False}}

class LoginMedicoIn(BaseModel):
    email: EmailStr
    password: str

@app.post("/auth/login_medico")
def login_medico(data: LoginMedicoIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, full_name, password_hash, validado FROM medicos WHERE email=%s", (data.email.lower(),))
    row = cur.fetchone()
    if not row or not pwd_context.verify(data.password, row[2]):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")
    if not row[3]:
        raise HTTPException(status_code=403, detail="Cuenta aún no validada")
    token = create_access_token({"sub": str(row[0]), "email": data.email.lower(), "role": "medico"})
    return {"access_token": token, "token_type": "bearer",
            "medico": {"id": str(row[0]), "full_name": row[1], "validado": True}}

@app.post("/auth/validar_medico/{medico_id}")
def validar_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE medicos SET validado=TRUE, updated_at=NOW() WHERE id=%s RETURNING id, full_name", (medico_id,))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"ok": True, "medico_id": row[0], "nombre": row[1]}

@app.post("/auth/medico/{medico_id}/foto")
def actualizar_foto(medico_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    try:
        upload_result = cloudinary.uploader.upload(file.file, folder="docya/medicos",
                                                   public_id=f"medico_{medico_id}", overwrite=True)
        foto_url = upload_result["secure_url"]
        cur = db.cursor()
        cur.execute("UPDATE medicos SET foto_perfil=%s, updated_at=NOW() WHERE id=%s RETURNING id,foto_perfil",
                    (foto_url, medico_id))
        row = cur.fetchone(); db.commit()
        if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
        return {"ok": True, "medico_id": row[0], "foto_url": row[1]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo foto: {e}")

class AliasIn(BaseModel): alias: str
@app.patch("/auth/medico/{medico_id}/alias")
def actualizar_alias(medico_id: int, data: AliasIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE medicos SET alias_cbu=%s, updated_at=NOW() WHERE id=%s RETURNING id,alias_cbu",
                (data.alias, medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"ok": True, "medico_id": medico_id, "alias": row[1]}

@app.post("/auth/medico/{medico_id}/disponibilidad")
def actualizar_disponibilidad(medico_id: int, disponible: bool, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("UPDATE medicos SET disponible=%s WHERE id=%s RETURNING id,disponible", (disponible, medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"ok": True, "medico_id": medico_id, "disponible": row["disponible"]}

@app.get("/auth/medico/{medico_id}/stats")
def medico_stats(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM consultas WHERE medico_id=%s AND estado='aceptada' AND DATE_TRUNC('month',creado_en)=DATE_TRUNC('month',CURRENT_DATE)", (medico_id,))
    consultas = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*)*24000 FROM consultas WHERE medico_id=%s AND estado='aceptada' AND DATE_TRUNC('month',creado_en)=DATE_TRUNC('month',CURRENT_DATE)", (medico_id,))
    ganancias = cur.fetchone()[0] or 0
    return {"consultas": consultas, "ganancias": ganancias}

class FcmTokenIn(BaseModel): fcm_token: str
@app.post("/auth/medico/{medico_id}/fcm_token")
def actualizar_fcm_token(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE medicos SET fcm_token=%s, updated_at=NOW() WHERE id=%s RETURNING id",
                (data.fcm_token, medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"ok": True, "medico_id": medico_id, "fcm_token": data.fcm_token}

@app.get("/auth/medico/{medico_id}")
def obtener_medico(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, full_name, email, especialidad, telefono, alias_cbu, matricula, foto_perfil FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if not row: raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"id": row[0], "full_name": row[1], "email": row[2], "especialidad": row[3], "telefono": row[4],
            "alias_cbu": row[5], "matricula": row[6], "foto_perfil": row[7]}

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

@app.post("/consultas/solicitar")
async def solicitar_consulta(data: SolicitarConsultaIn, db=Depends(get_db)):
    cur = db.cursor()
    # Buscar médico más cercano disponible
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
        raise HTTPException(status_code=404, detail="No hay médicos disponibles")

    medico_id, medico_nombre, medico_lat, medico_lng, distancia = row

    cur.execute("""
        INSERT INTO consultas (paciente_uuid, medico_id, estado, motivo, direccion, lat, lng)
        VALUES (%s,%s,'pendiente',%s,%s,%s,%s)
        RETURNING id, creado_en
    """, (str(data.paciente_uuid), medico_id, data.motivo, data.direccion, data.lat, data.lng))
    consulta_id, creado_en = cur.fetchone()
    db.commit()

    # Notificar al médico por WS
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
        except Exception as e:
            print(f"⚠️ WS error: {e}")

    # Notificar push
    cur.execute("SELECT fcm_token FROM medicos WHERE id=%s", (medico_id,))
    row = cur.fetchone()
    if row and row[0]:
        try:
            enviar_push(row[0], "📢 Nueva consulta", f"{data.motivo}", {
                "tipo": "consulta_nueva",
                "consulta_id": str(consulta_id)
            })
        except Exception as e:
            print(f"⚠️ Error push: {e}")

    return {
        "consulta_id": consulta_id,
        "paciente_uuid": str(data.paciente_uuid),
        "medico": {"id": medico_id, "nombre": medico_nombre,
                   "lat": medico_lat, "lng": medico_lng,
                   "distancia_km": round(distancia, 2)},
        "motivo": data.motivo,
        "direccion": data.direccion,
        "estado": "pendiente",
        "creado_en": creado_en
    }

# --- Consultas del médico ---
@app.get("/consultas/mias/{medico_id}")
def consultas_mias(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT id, paciente_uuid, estado, motivo, direccion, creado_en
        FROM consultas
        WHERE medico_id=%s AND estado IN ('pendiente','aceptada')
        ORDER BY creado_en DESC
    """, (medico_id,))
    rows = cur.fetchall()
    return [{"id": r[0], "paciente_uuid": r[1], "estado": r[2],
             "motivo": r[3], "direccion": r[4], "creado_en": r[5]} for r in rows]

# --- Consulta asignada ---
@app.get("/consultas/asignadas/{medico_id}")
def consultas_asignadas(medico_id: int, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id,c.paciente_uuid,COALESCE(u.full_name,'Paciente') as paciente_nombre,
               c.motivo,c.direccion,c.lat,c.lng,c.estado,m.latitud,m.longitud
        FROM consultas c
        JOIN medicos m ON c.medico_id=m.id
        LEFT JOIN users u ON c.paciente_uuid=u.id
        WHERE c.medico_id=%s AND c.estado='pendiente'
        ORDER BY c.creado_en DESC LIMIT 1
    """, (medico_id,))
    row = cur.fetchone()
    if not row: return {"consulta": None}

    consulta_id, paciente_uuid, paciente_nombre, motivo, direccion, lat, lng, estado, med_lat, med_lng = row
    distancia = None; tiempo = None
    if med_lat and med_lng and lat and lng:
        dlat = math.radians(lat-med_lat); dlon = math.radians(lng-med_lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(med_lat))*math.cos(math.radians(lat))*math.sin(dlon/2)**2
        distancia = 6371*2*math.atan2(math.sqrt(a), math.sqrt(1-a))
        tiempo = (distancia/40)*60
    return {"id": consulta_id, "paciente_uuid": str(paciente_uuid),
            "paciente_nombre": paciente_nombre, "motivo": motivo,
            "direccion": direccion, "lat": lat, "lng": lng, "estado": estado,
            "distancia_km": round(distancia,2) if distancia else None,
            "tiempo_estimado_min": round(tiempo) if tiempo else None}

# --- Aceptar / Rechazar / En camino / Llegó / Finalizar ---
class MedicoAccion(BaseModel): medico_id: int

@app.post("/consultas/{consulta_id}/aceptar")
def aceptar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='aceptada', medico_id=%s WHERE id=%s AND estado='pendiente' RETURNING id", (data.medico_id, consulta_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "aceptada"}

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
def finalizar_consulta(consulta_id: int, data: MedicoAccion, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("UPDATE consultas SET estado='finalizada' WHERE id=%s AND medico_id=%s AND estado IN ('aceptada','en_domicilio') RETURNING id", (consulta_id, data.medico_id))
    row = cur.fetchone(); db.commit()
    if not row: raise HTTPException(status_code=404, detail="Consulta no encontrada")
    return {"ok": True, "consulta_id": row[0], "estado": "finalizada"}

# --- Historial del paciente ---
@app.get("/consultas/historial/{paciente_uuid}")
def historial_consultas(paciente_uuid: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        SELECT c.id,c.estado,c.motivo,c.direccion,c.creado_en,m.full_name,m.especialidad
        FROM consultas c LEFT JOIN medicos m ON c.medico_id=m.id
        WHERE c.paciente_uuid=%s ORDER BY c.creado_en DESC
    """, (paciente_uuid,))
    rows = cur.fetchall()
    return [{"id": r[0], "estado": r[1], "motivo": r[2], "direccion": r[3],
             "creado_en": r[4], "medico":{"nombre": r[5], "especialidad": r[6]}} for r in rows]

# --- Certificados ---
class CertificadoIn(BaseModel): medico_id:int; paciente_uuid:str; contenido:str
@app.post("/consultas/{consulta_id}/certificado")
def crear_certificado(consulta_id:int,data:CertificadoIn,db=Depends(get_db)):
    cur=db.cursor();cur.execute("INSERT INTO certificados (consulta_id,medico_id,paciente_uuid,contenido) VALUES (%s,%s,%s,%s) RETURNING id",(consulta_id,data.medico_id,data.paciente_uuid,data.contenido))
    row=cur.fetchone()[0];db.commit();return {"ok":True,"certificado_id":row}

# --- Recetas ---
class RecetaIn(BaseModel): medico_id:int; paciente_uuid:str; medicamentos:list[dict]
@app.post("/consultas/{consulta_id}/receta")
def crear_receta(consulta_id:int,data:RecetaIn,db=Depends(get_db)):
    cur=db.cursor();cur.execute("INSERT INTO recetas (consulta_id,medico_id,paciente_uuid) VALUES (%s,%s,%s) RETURNING id",(consulta_id,data.medico_id,data.paciente_uuid))
    receta_id=cur.fetchone()[0]
    for m in data.medicamentos:
        cur.execute("INSERT INTO receta_items (receta_id,nombre,dosis,frecuencia,duracion) VALUES (%s,%s,%s,%s,%s)",(receta_id,m["nombre"],m["dosis"],m["frecuencia"],m["duracion"]))
    db.commit();return {"ok":True,"receta_id":receta_id}

# --- Notas ---
class NotaIn(BaseModel): medico_id:int; paciente_uuid:str; contenido:str
@app.post("/consultas/{consulta_id}/nota")
def crear_nota(consulta_id:int,data:NotaIn,db=Depends(get_db)):
    cur=db.cursor();cur.execute("INSERT INTO notas_medicas (consulta_id,medico_id,paciente_uuid,contenido) VALUES (%s,%s,%s,%s) RETURNING id",(consulta_id,data.medico_id,data.paciente_uuid,data.contenido))
    nota_id=cur.fetchone()[0];db.commit();return {"ok":True,"nota_id":nota_id}

# --- Ubicación actual del médico ---
@app.get("/consultas/{consulta_id}/ubicacion_medico")
def ubicacion_medico_consulta(consulta_id: int, db=Depends(get_db)):
    cur=db.cursor();cur.execute("SELECT m.id,m.full_name,m.latitud,m.longitud,m.telefono FROM consultas c JOIN medicos m ON c.medico_id=m.id WHERE c.id=%s AND c.estado='aceptada'",(consulta_id,))
    row=cur.fetchone()
    if not row: raise HTTPException(status_code=404, detail="No se encontró ubicación")
    return {"medico_id":row[0],"nombre":row[1],"lat":row[2],"lng":row[3],"telefono":row[4]}

# ====================================================
# 🔔 NOTIFICACIONES Y WS
# ====================================================

# Diccionario para conexiones activas de médicos
active_medicos: Dict[int, WebSocket] = {}

# --- WebSocket de médicos ---
@app.websocket("/ws/medico/{medico_id}")
async def medico_ws(websocket: WebSocket, medico_id: int):
    await websocket.accept()
    active_medicos[medico_id] = websocket
    try:
        while True:
            data = await websocket.receive_text()
            if data == '{"tipo":"ping"}':
                print(f"❤️ Ping recibido de médico {medico_id}")
    except WebSocketDisconnect:
        if medico_id in active_medicos:
            del active_medicos[medico_id]
            print(f"❌ WebSocket cerrado para médico {medico_id}")

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
# 🔄 ALIAS DE COMPATIBILIDAD (para no romper el frontend)
# ====================================================

# --- FCM Token alias ---
@app.post("/medicos/{medico_id}/fcm_token")
def alias_fcm(medico_id: int, data: FcmTokenIn, db=Depends(get_db)):
    return actualizar_fcm_token(medico_id, data, db)

# --- Stats alias ---
@app.get("/medicos/{medico_id}/stats")
def alias_stats(medico_id: int, db=Depends(get_db)):
    return medico_stats(medico_id, db)

# --- Ubicación alias ---
@app.post("/medico/{medico_id}/ubicacion")
def alias_ubicacion(medico_id: int, lat: float, lng: float, disponible: bool, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE medicos
        SET latitud=%s, longitud=%s, disponible=%s, updated_at=NOW()
        WHERE id=%s RETURNING id
    """, (lat, lng, disponible, medico_id))
    row = cur.fetchone(); db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Médico no encontrado")
    return {"ok": True, "medico_id": medico_id, "lat": lat, "lng": lng, "disponible": disponible}

