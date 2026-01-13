from fastapi import APIRouter, HTTPException, Depends
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from datetime import datetime, timedelta
from os import getenv
import jwt

from database import get_db

router = APIRouter(
    prefix="/auth/admin",
    tags=["Auth Admin"]
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = getenv("JWT_SECRET", "docya_secret")
ALGORITHM = "HS256"
EXP_MINUTES = 60 * 12  # 12 horas


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

    payload = {
        "sub": str(admin["id"]),
        "email": admin["email"],
        "role": admin["role"],
        "type": "admin",
        "exp": datetime.utcnow() + timedelta(minutes=EXP_MINUTES),
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    return {
        "access_token": token,
        "token_type": "bearer",
        "admin": {
            "id": admin["id"],
            "email": admin["email"],
            "full_name": admin["full_name"],
            "role": admin["role"],
        },
    }
