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
     # Enviar correo de bienvenida
    send_welcome_email(data.email, data.full_name, data.password)
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


#para envio de correo email de bienveida ----------------------------------------------------------------------
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

def send_welcome_email(to_email: str, full_name: str, password: str):
    try:
        subject = "Bienvenido a DocYa - Tu sistema de salud"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f9f9f9; margin:0; padding:0;">
            <table align="center" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.1);">
                <tr style="background:#14B8A6;">
                    <td align="center" style="padding:20px;">
                        <img src="https://i.ibb.co/0YyytY9/docya-logo.png" alt="DocYa" width="120" style="display:block; margin-bottom:10px;" />
                        <h1 style="color:#ffffff; margin:0; font-size:22px;">Bienvenido a DocYa</h1>
                    </td>
                </tr>
                <tr>
                    <td style="padding:30px; color:#333;">
                        <h2 style="color:#14B8A6; margin-top:0;">¡Hola {full_name}!</h2>
                        <p>Gracias por unirte a <b>DocYa</b>, la app que conecta médicos y pacientes de manera rápida, segura y confiable.</p>
                        <p>Estos son tus datos de acceso:</p>
                        <table width="100%" style="margin:20px 0; border-collapse:collapse;">
                            <tr>
                                <td style="padding:10px; border:1px solid #ddd;"><b>Email</b></td>
                                <td style="padding:10px; border:1px solid #ddd;">{to_email}</td>
                            </tr>
                            <tr>
                                <td style="padding:10px; border:1px solid #ddd;"><b>Contraseña</b></td>
                                <td style="padding:10px; border:1px solid #ddd;">{password}</td>
                            </tr>
                        </table>
                        <p>Podes iniciar sesión en la app y comenzar a disfrutar de todos nuestros servicios.</p>
                        <p style="margin-top:30px;">Un cordial saludo,<br>
                        <b>Equipo DocYa</b></p>
                    </td>
                </tr>
                <tr style="background:#f1f1f1;">
                    <td align="center" style="padding:15px; font-size:12px; color:#666;">
                        © 2025 DocYa. Todos los derechos reservados.
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        print(f"Correo de bienvenida enviado a {to_email}")
    except Exception as e:
        print(f"Error enviando correo: {e}")
