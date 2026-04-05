"""
Configuración central del backend.

Este archivo concentra variables de entorno, helpers de fecha y utilidades
de autenticación para que `main.py` no tenga que definir todo eso en línea.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()

# Base de datos y auth.
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET", "change_me")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "120"))
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
CURRENT_ARGENTINA_DATE_SQL = "(CURRENT_TIMESTAMP AT TIME ZONE 'America/Argentina/Buenos_Aires')::date"
CURRENT_ARGENTINA_WEEK_SQL = "date_trunc('week', (CURRENT_TIMESTAMP AT TIME ZONE 'America/Argentina/Buenos_Aires'))"

# Mercado Pago.
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_PUBLIC_KEY = os.getenv("MP_PUBLIC_KEY", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()
MP_COUNTRY_CODE = os.getenv("MP_COUNTRY_CODE", "ARG").strip().upper()
MP_TEST_MODE = os.getenv("MP_TEST_MODE", "false").strip().lower() in ("1", "true", "yes", "on")
MP_TEST_PAYER_EMAIL = os.getenv("MP_TEST_PAYER_EMAIL", "test_user_123456@testuser.com").strip()
MP_TEST_IDENTIFICATION_TYPE = os.getenv("MP_TEST_IDENTIFICATION_TYPE", "DNI").strip().upper()
MP_TEST_IDENTIFICATION_NUMBER = os.getenv("MP_TEST_IDENTIFICATION_NUMBER", "12345678").strip()
DOCYA_FORCE_CONSULTA_PRICE = os.getenv("DOCYA_FORCE_CONSULTA_PRICE", "").strip()

# Telegram.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")
TELEGRAM_GRUPO_ID = os.getenv("TELEGRAM_GRUPO_ID")
TELEGRAM_GRUPO_MEDICOS_ID = os.getenv("TELEGRAM_GRUPO_MEDICOS_ID", "").strip()
TELEGRAM_GRUPO_ENFERMEROS_ID = os.getenv("TELEGRAM_GRUPO_ENFERMEROS_ID", "").strip()

# Orígenes permitidos para CORS.
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "https://centrodemonitoreodocya.vercel.app",
    "https://docya-monitoreo-omwg.vercel.app",
    "https://docya-monitoreo.vercel.app",
    "https://comunidaddocya-tfq8.vercel.app",
    "https://www.docya.com.ar",
    "https://docyarecetario.vercel.app",
    "https://monitoreodocyasas-ua4l-gsyz2umjm.vercel.app/",
    "https://monitoreodocyasas-git-988b6f-nahundeveloper-gmailcoms-projects.vercel.app",
    "https://www.docya.online",
    "https://docyacomunidad-7ii3.vercel.app",
]


def now_argentina():
    """Devuelve la fecha/hora actual en la zona horaria de Argentina."""
    return datetime.now(ARG_TZ)


def today_argentina():
    """Devuelve la fecha actual en Argentina."""
    return now_argentina().date()


def start_of_week_argentina():
    """Devuelve el inicio de semana local de Argentina."""
    today = today_argentina()
    return today - timedelta(days=today.weekday())


def format_datetime_arg(dt):
    """Formatea fechas para respuestas legibles en frontend."""
    if not dt:
        return None
    dt = dt.astimezone(ARG_TZ)
    return dt.strftime("%d/%m/%Y %H:%M")


def create_access_token(payload: dict, expires_minutes: int = JWT_EXPIRE_MINUTES):
    """Firma JWTs de DocYa usando la clave configurada en entorno."""
    to_encode = payload.copy()
    expire = now_argentina() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm="HS256")


def get_forced_consulta_price() -> int | None:
    """Devuelve un monto temporal de prueba si fue configurado por entorno."""
    if not DOCYA_FORCE_CONSULTA_PRICE:
        return None
    try:
        value = int(float(DOCYA_FORCE_CONSULTA_PRICE))
    except ValueError:
        return None
    return value if value >= 0 else None
