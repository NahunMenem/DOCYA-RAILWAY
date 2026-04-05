from fastapi import APIRouter, HTTPException, Depends
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from datetime import timedelta
from os import getenv
import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from database import get_db
from settings import now_argentina

router = APIRouter(
    prefix="/auth/admin",
    tags=["Auth Admin"]
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = getenv("JWT_SECRET", "docya_secret")
ALGORITHM = "HS256"
EXP_MINUTES = 60 * 12  # 12 horas
GOOGLE_CLIENT_IDS = [
    item.strip()
    for item in getenv("GOOGLE_CLIENT_IDS", getenv("GOOGLE_CLIENT_ID", "")).split(",")
    if item.strip()
]
ALLOWED_ADMIN_GOOGLE_EMAILS = {
    item.strip().lower()
    for item in getenv("MONITOREO_GOOGLE_ALLOWED_EMAILS", "nahundeveloper@gmail.com").split(",")
    if item.strip()
}


def _build_admin_token(admin_id: str, email: str, full_name: str, role: str):
    payload = {
        "sub": str(admin_id),
        "email": email,
        "role": role,
        "type": "admin",
        "exp": now_argentina() + timedelta(minutes=EXP_MINUTES),
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    return {
        "access_token": token,
        "token_type": "bearer",
        "admin": {
            "id": admin_id,
            "email": email,
            "full_name": full_name,
            "role": role,
        },
    }


@router.post("/login")
def admin_login(data: dict, db=Depends(get_db)):
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Datos incompletos")

    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT
            id,
            email,
            full_name,
            role,
            password_hash
        FROM admins
        WHERE email = %s
          AND activo = TRUE
        LIMIT 1
        """,
        (email.lower(),)
    )

    admin = cur.fetchone()
    cur.close()

    if not admin:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    if not pwd_context.verify(password, admin["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    return _build_admin_token(
        str(admin["id"]),
        admin["email"],
        admin["full_name"],
        admin["role"],
    )


@router.post("/google")
def admin_google_login(data: dict, db=Depends(get_db)):
    id_token = (data.get("id_token") or "").strip()
    if not id_token:
        raise HTTPException(status_code=400, detail="Falta id_token")

    request_adapter = google_requests.Request()
    payload = None
    last_error = None

    for audience in (GOOGLE_CLIENT_IDS or [None]):
        try:
            payload = google_id_token.verify_oauth2_token(
                id_token,
                request_adapter,
                audience,
            )
            break
        except Exception as exc:
            last_error = exc

    if payload is None:
        raise HTTPException(
            status_code=401,
            detail=f"Token Google inválido: {last_error}",
        )

    email = (payload.get("email") or "").strip().lower()
    full_name = (payload.get("name") or "Admin DocYa").strip()
    google_sub = (payload.get("sub") or "").strip()

    if not email or not google_sub:
        raise HTTPException(
            status_code=400,
            detail="Google no devolvió identidad suficiente",
        )

    if email not in ALLOWED_ADMIN_GOOGLE_EMAILS:
        raise HTTPException(
            status_code=403,
            detail="Esta cuenta Google no tiene acceso al monitoreo",
        )

    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, email, full_name, role
        FROM admins
        WHERE lower(email) = %s
          AND activo = TRUE
        LIMIT 1
        """,
        (email,),
    )
    admin = cur.fetchone()
    cur.close()

    if admin:
        return _build_admin_token(
            str(admin["id"]),
            admin["email"],
            admin["full_name"],
            admin["role"],
        )

    return _build_admin_token(
        f"google:{google_sub}",
        email,
        full_name,
        "superadmin",
    )
