# ====================================================
# 📌 referidos.py — Programa de Referidos DocYa
# ====================================================

import os
import uuid
import string
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from sib_api_v3_sdk import SendSmtpEmail

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from psycopg2.extras import RealDictCursor

# ────────────────────────────────────────────────────
# Se reutiliza la misma DB/JWT que en main.py
# (importar get_db, JWT_SECRET, etc. desde allí en el proyecto real;
#  aquí los re-declaramos para que el archivo sea autocontenido)
# ────────────────────────────────────────────────────
from main import get_db, pwd_context, JWT_SECRET, JWT_EXPIRE_MINUTES, now_argentina, create_access_token

router = APIRouter(prefix="/referidos", tags=["referidos"])


# ====================================================
# 📐 MODELOS PYDANTIC
# ====================================================

class ReferenteRegisterIn(BaseModel):
    full_name: str
    dni: str
    telefono: str
    email: EmailStr
    password: str
    cbu_alias: str
    tipo: str          # influencer | embajador | paciente | partner
    acepto_condiciones: bool = False


class ReferenteLoginIn(BaseModel):
    email: str
    password: str


class ReferenteOut(BaseModel):
    id: str
    full_name: str
    email: str
    tipo: str
    link_referido: str
    codigo_referido: str


# ====================================================
# 🔧 HELPERS
# ====================================================

def _generar_codigo(full_name: str, length: int = 8) -> str:
    """
    Genera un código único: primeras letras del nombre + random alfanumérico.
    Ej: 'JUAN-A3X9'
    """
    base = full_name.strip().split()[0].upper()[:4]
    sufijo = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{base}-{sufijo}"


def _link_referido(codigo: str) -> str:
    base_url = os.getenv("FRONTEND_URL", "https://www.docya.com.ar/")
    return f"{base_url}/?ref={codigo}"


def _enviar_email_bienvenida_referente(email: str, full_name: str, codigo: str, link: str):
    """Email de bienvenida al referente con su link y código personales."""
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key["api-key"] = os.getenv("BREVO_API_KEY")
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"><title>Bienvenido a DocYa Referidos</title></head>
    <body style="margin:0;padding:0;background:#F4F6F8;font-family:Arial,sans-serif;">
      <table align="center" width="100%" bgcolor="#F4F6F8" style="padding:20px 0;" cellpadding="0" cellspacing="0">
        <tr><td align="center">
          <table width="600" style="background:#ffffff;border-radius:8px;box-shadow:0 2px 6px rgba(0,0,0,0.1);" cellpadding="0" cellspacing="0">
            <tr>
              <td style="background:#0F2027;border-radius:8px 8px 0 0;padding:30px;text-align:center;">
                <img src="https://res.cloudinary.com/dqsacd9ez/image/upload/v1757197807/logoblanco_1_qdlnog.png"
                     alt="DocYa" style="max-width:160px;">
              </td>
            </tr>
            <tr>
              <td style="padding:40px 36px;">
                <h2 style="color:#14B8A6;font-size:22px;margin:0 0 12px;">
                  ¡Hola {full_name}, ya sos parte del programa! 🎉
                </h2>
                <p style="color:#333;font-size:15px;line-height:1.6;margin:0 0 24px;">
                  Tu cuenta de embajador DocYa fue creada exitosamente.<br>
                  Compartí tu link personal y <strong>ganás $1.000 por cada consulta</strong>
                  que realicen tus referidos.
                </p>

                <!-- Código -->
                <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
                  <tr>
                    <td style="background:#F0FDFA;border:1px solid #99F6E4;border-radius:8px;padding:16px;text-align:center;">
                      <p style="margin:0 0 4px;color:#0F766E;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;">
                        Tu código de referido
                      </p>
                      <p style="margin:0;color:#0D9488;font-size:28px;font-weight:900;letter-spacing:4px;">
                        {codigo}
                      </p>
                    </td>
                  </tr>
                </table>

                <!-- Link -->
                <p style="color:#555;font-size:14px;margin:0 0 8px;">Tu link personalizado:</p>
                <a href="{link}"
                   style="display:block;background:#14B8A6;color:#fff;text-decoration:none;
                          padding:14px 24px;border-radius:6px;font-size:14px;font-weight:bold;
                          text-align:center;margin-bottom:24px;">
                  🔗 {link}
                </a>

                <p style="color:#777;font-size:13px;line-height:1.6;margin:0;">
                  Los pagos se acreditan <strong>semanalmente</strong> en tu CBU/Alias registrado.<br>
                  Podés ver tus métricas en tiempo real desde tu panel de embajador.
                </p>
              </td>
            </tr>
            <tr>
              <td style="background:#F9FAFB;border-radius:0 0 8px 8px;padding:20px;text-align:center;">
                <p style="color:#999;font-size:11px;margin:0;">
                  © {datetime.now().year} DocYa · Atención médica a domicilio con confianza.
                </p>
              </td>
            </tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

    email_data = SendSmtpEmail(
        to=[{"email": email, "name": full_name}],
        sender={"email": "nahundeveloper@gmail.com", "name": "DocYa"},
        subject="¡Ya sos embajador DocYa! Tu link personal está listo 🚀",
        html_content=html,
    )

    try:
        api_instance.send_transac_email(email_data)
        print(f"✅ Email bienvenida referente enviado a {email}")
    except ApiException as e:
        print(f"⚠️ Error enviando email referente con Brevo: {e}")


# ====================================================
# 🚀 ENDPOINTS
# ====================================================

@router.post("/register", response_model=ReferenteOut, status_code=201)
def register_referente(data: ReferenteRegisterIn, db=Depends(get_db)):
    """
    Registra un nuevo referente (embajador) en el programa de referidos.
    Genera automáticamente su código y link único.
    """
    if not data.acepto_condiciones:
        raise HTTPException(
            status_code=422,
            detail="Debés aceptar los términos y condiciones para continuar."
        )

    cur = db.cursor()

    # ── Verificar duplicado por email ──────────────────────────────
    cur.execute(
        "SELECT id FROM referentes WHERE email = %s",
        (data.email.lower(),)
    )
    if cur.fetchone():
        raise HTTPException(
            status_code=409,
            detail="El email ya está registrado en el programa de referidos."
        )

    # ── Verificar duplicado por DNI ────────────────────────────────
    cur.execute(
        "SELECT id FROM referentes WHERE dni = %s",
        (data.dni.strip(),)
    )
    if cur.fetchone():
        raise HTTPException(
            status_code=409,
            detail="El DNI ya está registrado en el programa de referidos."
        )

    # ── Validar tipo ───────────────────────────────────────────────
    tipos_validos = {"influencer", "embajador", "paciente", "partner"}
    if data.tipo not in tipos_validos:
        raise HTTPException(
            status_code=422,
            detail=f"Tipo inválido. Valores permitidos: {', '.join(tipos_validos)}"
        )

    # ── Generar código único (retry en colisión) ───────────────────
    codigo = None
    for _ in range(10):
        candidato = _generar_codigo(data.full_name)
        cur.execute("SELECT id FROM referentes WHERE codigo_referido = %s", (candidato,))
        if not cur.fetchone():
            codigo = candidato
            break

    if not codigo:
        raise HTTPException(
            status_code=500,
            detail="No se pudo generar un código único. Intentá de nuevo."
        )

    link = _link_referido(codigo)

    # ── Hash de contraseña ─────────────────────────────────────────
    password_hash = pwd_context.hash(data.password)
    full_name = data.full_name.strip().title()

    # ── INSERT ─────────────────────────────────────────────────────
    try:
        cur.execute(
            """
            INSERT INTO referentes (
                full_name, dni, telefono, email, password_hash,
                cbu_alias, tipo, codigo_referido, link_referido,
                acepto_condiciones, fecha_aceptacion,
                activo, creado_en
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                TRUE, %s,
                TRUE, %s
            )
            RETURNING id, full_name, email, tipo, link_referido, codigo_referido
            """,
            (
                full_name,
                data.dni.strip(),
                data.telefono.strip(),
                data.email.lower(),
                password_hash,
                data.cbu_alias.strip(),
                data.tipo,
                codigo,
                link,
                now_argentina(),   # fecha_aceptacion
                now_argentina(),   # creado_en
            )
        )
        row = cur.fetchone()
        db.commit()

    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Error interno al registrar referente: {e}"
        )

    referente_id, full_name_db, email_db, tipo_db, link_db, codigo_db = row

    # ── Email de bienvenida (no bloqueante) ────────────────────────
    try:
        _enviar_email_bienvenida_referente(email_db, full_name_db, codigo_db, link_db)
    except Exception as e:
        print(f"⚠️ Email bienvenida falló (no crítico): {e}")

    return ReferenteOut(
        id=str(referente_id),
        full_name=full_name_db,
        email=email_db,
        tipo=tipo_db,
        link_referido=link_db,
        codigo_referido=codigo_db,
    )


# ──────────────────────────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────────────────────────

@router.post("/login")
def login_referente(data: ReferenteLoginIn, db=Depends(get_db)):
    """Login del referente. Devuelve JWT + datos básicos del perfil."""
    cur = db.cursor()

    cur.execute(
        """
        SELECT id, full_name, email, tipo, password_hash,
               codigo_referido, link_referido, activo
        FROM referentes
        WHERE lower(email) = %s
        LIMIT 1
        """,
        (data.email.strip().lower(),)
    )
    row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

    (
        ref_id, full_name, email, tipo, password_hash,
        codigo, link, activo
    ) = row

    if not pwd_context.verify(data.password, password_hash):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

    if not activo:
        raise HTTPException(
            status_code=403,
            detail="Tu cuenta está desactivada. Contactá a soporte."
        )

    token = create_access_token({
        "sub": str(ref_id),
        "email": email,
        "role": "referente",
        "tipo": tipo,
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "referente": {
            "id": str(ref_id),
            "full_name": full_name,
            "email": email,
            "tipo": tipo,
            "codigo_referido": codigo,
            "link_referido": link,
        }
    }


# ──────────────────────────────────────────────────────────────────
# DASHBOARD — estadísticas del referente
# ──────────────────────────────────────────────────────────────────

@router.get("/{referente_id}/stats")
def stats_referente(referente_id: str, db=Depends(get_db)):
    """
    Devuelve las métricas del referente:
    - Total de referidos registrados
    - Total de consultas válidas
    - Monto total acumulado
    - Monto pendiente de cobro
    """
    cur = db.cursor()

    # Verificar que existe
    cur.execute("SELECT id, full_name, codigo_referido FROM referentes WHERE id = %s", (referente_id,))
    ref = cur.fetchone()
    if not ref:
        raise HTTPException(status_code=404, detail="Referente no encontrado.")

    ref_id_db, full_name, codigo = ref

    # Pacientes registrados con el código
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE codigo_referido = %s",
        (codigo,)
    )
    total_referidos = cur.fetchone()[0]

    # Consultas válidas (pagadas) de esos pacientes
    cur.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(monto_referente), 0)
        FROM recompensas_referentes
        WHERE referente_id = %s AND estado IN ('pendiente', 'pagado')
        """,
        (referente_id,)
    )
    row = cur.fetchone()
    total_consultas_validas = row[0]
    monto_total_acumulado = float(row[1])

    # Monto pendiente de cobro
    cur.execute(
        """
        SELECT COALESCE(SUM(monto_referente), 0)
        FROM recompensas_referentes
        WHERE referente_id = %s AND estado = 'pendiente'
        """,
        (referente_id,)
    )
    monto_pendiente = float(cur.fetchone()[0])

    return {
        "referente_id": referente_id,
        "full_name": full_name,
        "codigo_referido": codigo,
        "total_referidos": total_referidos,
        "total_consultas_validas": total_consultas_validas,
        "monto_total_acumulado": monto_total_acumulado,
        "monto_pendiente": monto_pendiente,
        "precio_por_consulta": 1000,
    }


@router.get("/{referente_id}/mis-referidos")
def mis_referidos(referente_id: str, db=Depends(get_db)):
    """
    Devuelve la lista de pacientes referidos con su última consulta,
    monto generado y estado de pago.
    """
    cur = db.cursor()

    # 🔹 Verificar referente
    cur.execute(
        "SELECT id, codigo_referido FROM referentes WHERE id = %s",
        (referente_id,)
    )
    ref = cur.fetchone()
    if not ref:
        raise HTTPException(status_code=404, detail="Referente no encontrado.")

    _, codigo = ref

    # 🔥 QUERY CORREGIDA (UUID BIEN MANEJADO)
    cur.execute(
        """
        SELECT
            u.id                                        AS paciente_uuid,
            u.full_name,
            u.localidad,
            u.created_at                                AS fecha_registro,

            -- última consulta
            MAX(c.creado_en)                            AS ultima_consulta,

            -- total generado
            COALESCE(SUM(rr.monto_referente), 0)        AS monto_total,

            -- último estado
            (
                SELECT rr2.estado
                FROM recompensas_referentes rr2
                WHERE rr2.paciente_uuid = u.id
                  AND rr2.referente_id  = %s
                ORDER BY rr2.creado_en DESC
                LIMIT 1
            )                                           AS ultimo_estado,

            -- vencimiento
            u.created_at + INTERVAL '12 months'         AS vence_en

        FROM users u

        LEFT JOIN consultas c
               ON c.paciente_uuid = u.id   -- ✅ UUID = UUID

        LEFT JOIN recompensas_referentes rr
               ON rr.paciente_uuid = u.id  -- ✅ UUID = UUID
              AND rr.referente_id  = %s

        WHERE TRIM(LOWER(u.codigo_referido)) = TRIM(LOWER(%s))

        GROUP BY u.id, u.full_name, u.localidad, u.created_at

        ORDER BY MAX(c.creado_en) DESC NULLS LAST, u.created_at DESC
        """,
        (referente_id, referente_id, codigo)
    )

    rows = cur.fetchall()

    referidos = []
    for row in rows:
        (
            paciente_uuid, full_name, localidad, fecha_registro,
            ultima_consulta, monto_total, ultimo_estado, vence_en
        ) = row

        referidos.routerend({
            "paciente_uuid":   str(paciente_uuid),
            "full_name":       full_name,
            "localidad":       localidad or "—",
            "fecha_registro":  fecha_registro.isoformat() if fecha_registro else None,
            "ultima_consulta": ultima_consulta.isoformat() if ultima_consulta else None,
            "monto_total":     float(monto_total or 0),
            "estado_pago":     ultimo_estado or "sin_consulta",
            "vence_en":        vence_en.isoformat() if vence_en else None,
        })

    return {
        "referente_id": referente_id,
        "total": len(referidos),
        "referidos": referidos,
    }

# Listar todos los referentes
@router.get("/admin/referentes")
def get_all_referentes(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, full_name, email, telefono, dni, tipo,
               codigo_referido, link_referido, cbu_alias, activo, creado_en
        FROM referentes
        ORDER BY creado_en DESC
    """)
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# Activar / desactivar referente
@router.patch("/admin/referentes/{referente_id}/toggle")
def toggle_referente(referente_id: str, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT activo FROM referentes WHERE id = %s", (referente_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Referente no encontrado")
    cur.execute(
        "UPDATE referentes SET activo = %s WHERE id = %s RETURNING activo",
        (not row["activo"], referente_id)
    )
    updated = cur.fetchone()
    db.commit()
    cur.close()
    return {"ok": True, "activo": updated["activo"]}
# Listar todas las recompensas (con join a referentes y users)
@router.get("/admin/recompensas")
def get_recompensas(estado: str = None, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    filtro = "WHERE rr.estado = %s" if estado else ""
    params = (estado,) if estado else ()
    cur.execute(f"""
        SELECT rr.id, rr.referente_id, rr.monto_referente, rr.estado, rr.creado_en,
               r.full_name AS referente_nombre, r.cbu_alias AS referente_cbu,
               u.full_name AS paciente_nombre
        FROM recompensas_referentes rr
        JOIN referentes r ON r.id::text = rr.referente_id::text
        JOIN users u ON u.id = rr.paciente_uuid
        {filtro}
        ORDER BY rr.creado_en DESC
    """, params)
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# Marcar una recompensa individual como pagada
@router.patch("/admin/recompensas/{recompensa_id}/pagar")
def pagar_recompensa(recompensa_id: int, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "UPDATE recompensas_referentes SET estado='pagado' WHERE id=%s RETURNING id",
        (recompensa_id,)
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Recompensa no encontrada")
    db.commit(); cur.close()
    return {"ok": True}


# Pagar todas las pendientes de un referente
@router.patch("/admin/referentes/{referente_id}/pagar-pendientes")
def pagar_pendientes(referente_id: str, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("""
        UPDATE recompensas_referentes SET estado='pagado'
        WHERE referente_id::text = %s AND estado = 'pendiente'
    """, (referente_id,))
    pagados = cur.rowcount
    db.commit(); cur.close()
    return {"ok": True, "pagados": pagados}
