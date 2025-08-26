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

class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str

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

@app.get("/health")
def health():
    return {"ok": True, "service": "docya-auth"}

@app.post("/auth/register", response_model=TokenOut)
def register(data: RegisterIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email=%s", (data.email.lower(),))
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
    password_hash = pwd_context.hash(data.password)
    cur.execute(
        "INSERT INTO users (email, full_name, password_hash) VALUES (%s, %s, %s) RETURNING id, role",
        (data.email.lower(), data.full_name.strip(), password_hash)
    )
    user_id, role = cur.fetchone()
    db.commit()
    token = create_access_token({"sub": str(user_id), "email": data.email.lower(), "role": role})
    return {"access_token": token, "token_type": "bearer"}

@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT id, password_hash, role FROM users WHERE email=%s", (data.email.lower(),))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Credenciales inválidas")
    user_id, password_hash, role = row
    if not password_hash or not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=400, detail="Credenciales inválidas")
    token = create_access_token({"sub": str(user_id), "email": data.email.lower(), "role": role})
    return {"access_token": token, "token_type": "bearer"}

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

@app.post("/auth/google", response_model=TokenOut)
def auth_google(data: GoogleIn, db=Depends(get_db)):
    try:
        # Validar token con Google
        payload = id_token.verify_oauth2_token(
            data.id_token,
            requests.Request(),
            os.getenv("GOOGLE_CLIENT_ID")  # tu client_id de Google
        )
    except Exception:
        raise HTTPException(status_code=400, detail="id_token inválido")

    sub = payload["sub"]
    email = payload["email"].lower()
    name = payload.get("name") or email.split("@")[0]
    picture = payload.get("picture")

    cur = db.cursor()
    # Buscar si ya existe el vínculo con Google
    cur.execute("""
        SELECT u.id, u.role FROM auth_providers ap
        JOIN users u ON ap.user_id = u.id
        WHERE ap.provider=%s AND ap.provider_uid=%s
    """, ("google", sub))
    row = cur.fetchone()

    if row:
        user_id, role = row
    else:
        # Crear usuario si no existe
        cur.execute("SELECT id, role FROM users WHERE email=%s", (email,))
        user = cur.fetchone()
        if user:
            user_id, role = user
        else:
            cur.execute(
                "INSERT INTO users (email, full_name, avatar_url) VALUES (%s, %s, %s) RETURNING id, role",
                (email, name, picture)
            )
            user_id, role = cur.fetchone()

        # Vincular proveedor
        cur.execute(
            "INSERT INTO auth_providers (user_id, provider, provider_uid) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, "google", sub)
        )
        db.commit()

    token = create_access_token({"sub": str(user_id), "email": email, "role": role})
    return {"access_token": token, "token_type": "bearer"}
