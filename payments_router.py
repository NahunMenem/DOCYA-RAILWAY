"""
Router de pagos de DocYa.

Agrupa toda la integración con Mercado Pago para dejar `main.py` más limpio.
Incluye:
- checkout embebido dentro de DocYa
- guardado de tarjetas
- preautorización/captura/cancelación
- webhook y estados de pago
"""

import json
import uuid
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor

from database import get_db
from settings import MP_ACCESS_TOKEN, MP_PUBLIC_KEY

router = APIRouter()


def _ensure_mp_access_token():
    """Falla rápido si el backend no tiene configurado el token productivo de MP."""
    if not MP_ACCESS_TOKEN:
        raise HTTPException(500, "MP_ACCESS_TOKEN no configurada")


def _mp_headers(extra: Optional[dict] = None):
    """Headers estándar para llamadas server-to-server a Mercado Pago."""
    _ensure_mp_access_token()
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _ensure_payment_method_tables(db):
    """Crea la tabla de métodos guardados si todavía no existe."""
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_methods_docya (
            id SERIAL PRIMARY KEY,
            paciente_uuid TEXT NOT NULL,
            mp_customer_id TEXT NOT NULL,
            mp_card_id TEXT NOT NULL,
            brand TEXT,
            last_four TEXT,
            expiration_month INTEGER,
            expiration_year INTEGER,
            holder_name TEXT,
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    db.commit()


def _get_user_profile(db, paciente_uuid: str):
    """Recupera datos básicos del paciente para asociarlo como payer/customer."""
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT id, full_name, email, dni FROM users WHERE id = %s",
        (str(paciente_uuid),),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Paciente no encontrado")
    return row


def _find_or_create_mp_customer(db, paciente_uuid: str):
    """Busca o crea el customer de Mercado Pago asociado al paciente."""
    user = _get_user_profile(db, paciente_uuid)
    cur = db.cursor(cursor_factory=RealDictCursor)
    _ensure_payment_method_tables(db)
    cur.execute(
        """
        SELECT mp_customer_id
        FROM payment_methods_docya
        WHERE paciente_uuid = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(paciente_uuid),),
    )
    existing = cur.fetchone()
    if existing and existing["mp_customer_id"]:
        return existing["mp_customer_id"], user

    response = requests.get(
        "https://api.mercadopago.com/v1/customers/search",
        headers=_mp_headers(),
        params={"email": user["email"]},
        timeout=20,
    )
    result = response.json()
    if response.ok and result.get("results"):
        return result["results"][0]["id"], user

    payload = {
        "email": user["email"],
        "first_name": (user.get("full_name") or "Paciente").split(" ")[0],
        "last_name": " ".join((user.get("full_name") or "").split(" ")[1:]) or "DocYa",
    }
    create_resp = requests.post(
        "https://api.mercadopago.com/v1/customers",
        headers=_mp_headers({"X-Idempotency-Key": str(uuid.uuid4())}),
        json=payload,
        timeout=20,
    )
    if not create_resp.ok:
        raise HTTPException(400, create_resp.json())
    return create_resp.json()["id"], user


def _save_local_payment_method(
    db,
    paciente_uuid: str,
    customer_id: str,
    mp_card: dict,
    set_default: bool = False,
):
    """Guarda una referencia local de la tarjeta para futuros cobros embebidos."""
    _ensure_payment_method_tables(db)
    cur = db.cursor()

    if set_default:
        cur.execute(
            "UPDATE payment_methods_docya SET is_default = FALSE WHERE paciente_uuid = %s",
            (str(paciente_uuid),),
        )

    cardholder = mp_card.get("cardholder") or {}
    cur.execute(
        """
        INSERT INTO payment_methods_docya (
            paciente_uuid, mp_customer_id, mp_card_id, brand, last_four,
            expiration_month, expiration_year, holder_name, is_default
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            str(paciente_uuid),
            customer_id,
            str(mp_card.get("id")),
            ((mp_card.get("payment_method") or {}).get("name") or mp_card.get("payment_method_id") or "Tarjeta"),
            mp_card.get("last_four_digits"),
            mp_card.get("expiration_month"),
            mp_card.get("expiration_year"),
            ((cardholder.get("name") or "").strip() or None),
            set_default,
        ),
    )
    db.commit()


def _update_consulta_payment_state(db, consulta_id: int, payment_data: dict):
    """Persistencia mínima del estado de pago en la consulta."""
    cur = db.cursor()
    status = payment_data.get("status")
    payment_id = payment_data.get("id")
    authorized = status in ("authorized", "approved")
    cur.execute(
        """
        UPDATE consultas
        SET mp_status = %s,
            mp_preautorizado = %s,
            mp_payment_id = %s
        WHERE id = %s
        """,
        (status, authorized, str(payment_id) if payment_id else None, consulta_id),
    )
    db.commit()
    return authorized


class PaymentMethodSaveIn(BaseModel):
    """Payload para guardar una tarjeta tokenizada en el customer de MP."""

    paciente_uuid: str
    token: str
    payment_method_id: str
    issuer_id: Optional[str] = None
    set_default: bool = True


class EmbeddedPaymentIn(BaseModel):
    """Payload para autorizar un pago manual-capture dentro de DocYa."""

    consulta_id: int
    paciente_uuid: str
    monto: float
    motivo: Optional[str] = None
    tipo: Optional[str] = "medico"
    token: str
    payment_method_id: str
    issuer_id: Optional[str] = None
    installments: Optional[int] = 1
    payer_email: Optional[str] = None
    identification_type: Optional[str] = "DNI"
    identification_number: Optional[str] = None
    save_card: bool = False


@router.post("/consultas/confirmar_pago")
def confirmar_pago(data: dict, db=Depends(get_db)):
    """Compatibilidad con el flujo viejo: no modifica estado, solo registra la vuelta."""
    consulta_id = data.get("consulta_id")
    print(f"Usuario volvió del pago -> consulta {consulta_id}")
    return {"status": "ok"}


@router.get("/pagos/metodos/{paciente_uuid}")
def listar_metodos_pago(paciente_uuid: str, db=Depends(get_db)):
    """Lista tarjetas guardadas del paciente para reutilizarlas en futuras consultas."""
    _ensure_payment_method_tables(db)
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, paciente_uuid, mp_customer_id, mp_card_id, brand, last_four,
               expiration_month, expiration_year, holder_name, is_default, created_at
        FROM payment_methods_docya
        WHERE paciente_uuid = %s
        ORDER BY is_default DESC, created_at DESC
        """,
        (str(paciente_uuid),),
    )
    return {"items": cur.fetchall()}


@router.delete("/pagos/metodos/{method_id}")
def eliminar_metodo_pago(method_id: int, db=Depends(get_db)):
    """Elimina la tarjeta local y también intenta borrarla en Mercado Pago."""
    _ensure_payment_method_tables(db)
    cur = db.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT id, mp_customer_id, mp_card_id
        FROM payment_methods_docya
        WHERE id = %s
        """,
        (method_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Método no encontrado")

    requests.delete(
        f"https://api.mercadopago.com/v1/customers/{row['mp_customer_id']}/cards/{row['mp_card_id']}",
        headers=_mp_headers(),
        timeout=20,
    )
    cur.execute("DELETE FROM payment_methods_docya WHERE id = %s", (method_id,))
    db.commit()
    return {"status": "deleted"}


@router.post("/pagos/metodos/guardar")
def guardar_metodo_pago(data: PaymentMethodSaveIn, db=Depends(get_db)):
    """Guarda una tarjeta tokenizada para simplificar próximos pedidos."""
    customer_id, _ = _find_or_create_mp_customer(db, data.paciente_uuid)
    response = requests.post(
        f"https://api.mercadopago.com/v1/customers/{customer_id}/cards",
        headers=_mp_headers({"X-Idempotency-Key": str(uuid.uuid4())}),
        json={"token": data.token},
        timeout=20,
    )
    if not response.ok:
        raise HTTPException(400, response.json())

    mp_card = response.json()
    _save_local_payment_method(
        db,
        paciente_uuid=data.paciente_uuid,
        customer_id=customer_id,
        mp_card=mp_card,
        set_default=data.set_default,
    )
    return {"status": "saved", "card": mp_card}


@router.post("/pagos/embebido/autorizar")
def autorizar_pago_embebido(data: EmbeddedPaymentIn, db=Depends(get_db)):
    """Autoriza el pago sin capturarlo todavía; la captura ocurre cuando aceptan la consulta."""
    user = _get_user_profile(db, data.paciente_uuid)
    payer_email = data.payer_email or user["email"]
    identification_number = data.identification_number or (user.get("dni") or "")

    payload = {
        "transaction_amount": float(data.monto),
        "token": data.token,
        "description": data.motivo or f"Consulta {data.tipo or 'medico'} DocYa",
        "installments": max(1, int(data.installments or 1)),
        "payment_method_id": data.payment_method_id,
        "capture": False,
        "external_reference": str(data.consulta_id),
        "payer": {
            "email": payer_email,
            "identification": {
                "type": data.identification_type or "DNI",
                "number": identification_number,
            },
        },
    }
    if data.issuer_id:
        payload["issuer_id"] = data.issuer_id

    response = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=_mp_headers({"X-Idempotency-Key": str(uuid.uuid4())}),
        json=payload,
        timeout=30,
    )
    payment = response.json()
    if not response.ok:
        raise HTTPException(400, payment)

    authorized = _update_consulta_payment_state(db, data.consulta_id, payment)

    if data.save_card:
        try:
            customer_id, _ = _find_or_create_mp_customer(db, data.paciente_uuid)
            card_resp = requests.post(
                f"https://api.mercadopago.com/v1/customers/{customer_id}/cards",
                headers=_mp_headers({"X-Idempotency-Key": str(uuid.uuid4())}),
                json={"token": data.token},
                timeout=20,
            )
            if card_resp.ok:
                _save_local_payment_method(
                    db,
                    paciente_uuid=data.paciente_uuid,
                    customer_id=customer_id,
                    mp_card=card_resp.json(),
                    set_default=False,
                )
        except Exception as exc:
            print("No se pudo guardar la tarjeta:", exc)

    return {
        "status": payment.get("status"),
        "authorized": authorized,
        "payment_id": payment.get("id"),
        "detail": payment,
    }


@router.get("/pagos/embebido/formulario", response_class=HTMLResponse)
def formulario_pago_embebido(
    paciente_uuid: str,
    monto: float,
    consulta_id: Optional[int] = None,
    tipo: str = "medico",
    motivo: str = "Consulta DocYa",
):
    """Sirve el card form de Mercado Pago embebido dentro de la app."""
    if not MP_PUBLIC_KEY:
        raise HTTPException(500, "MP_PUBLIC_KEY no configurada")

    safe_motivo = json.dumps(motivo)
    return f"""
    <!doctype html>
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Pago DocYa</title>
        <script src="https://sdk.mercadopago.com/js/v2"></script>
        <style>
          body {{
            margin: 0;
            background: #071b22;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif;
          }}
          .wrap {{ padding: 20px; }}
          .card {{
            background: rgba(255,255,255,.06);
            border: 1px solid rgba(255,255,255,.10);
            border-radius: 24px;
            padding: 20px;
          }}
          h1 {{ font-size: 24px; margin: 0 0 6px; }}
          p {{ color: rgba(255,255,255,.72); line-height: 1.4; }}
          .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
          .field {{ margin: 0 0 12px; }}
          label {{
            display: block;
            font-size: 13px;
            margin-bottom: 6px;
            color: rgba(255,255,255,.8);
          }}
          .mp-input, input, select {{
            width: 100%;
            box-sizing: border-box;
            border-radius: 16px;
            border: 1px solid rgba(255,255,255,.12);
            background: rgba(255,255,255,.08);
            color: #fff;
            min-height: 52px;
            padding: 14px;
          }}
          button {{
            width: 100%;
            border: none;
            border-radius: 18px;
            min-height: 54px;
            background: linear-gradient(90deg, #0ea896, #2dd4bf);
            color: white;
            font-size: 16px;
            font-weight: 700;
          }}
          .muted {{ font-size: 13px; color: rgba(255,255,255,.6); }}
          .error {{ color: #fca5a5; margin-top: 10px; font-size: 13px; }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h1>Pagar dentro de DocYa</h1>
            <p>Autorizás la consulta sin salir de la app. El cobro final se captura cuando un profesional acepta.</p>
            <form id="form-checkout">
              <div class="field">
                <label>Monto</label>
                <input value="ARS {monto:.0f}" disabled />
              </div>
              <div class="field">
                <label>Número de tarjeta</label>
                <div id="form-checkout__cardNumber" class="mp-input"></div>
              </div>
              <div class="row">
                <div class="field">
                  <label>Vencimiento</label>
                  <div id="form-checkout__expirationDate" class="mp-input"></div>
                </div>
                <div class="field">
                  <label>CVV</label>
                  <div id="form-checkout__securityCode" class="mp-input"></div>
                </div>
              </div>
              <div class="field">
                <label>Titular</label>
                <input id="form-checkout__cardholderName" />
              </div>
              <div class="field">
                <label>Email</label>
                <input id="form-checkout__cardholderEmail" />
              </div>
              <div class="row">
                <div class="field">
                  <label>Tipo de documento</label>
                  <select id="form-checkout__identificationType"></select>
                </div>
                <div class="field">
                  <label>Número</label>
                  <input id="form-checkout__identificationNumber" />
                </div>
              </div>
              <div class="row">
                <div class="field">
                  <label>Banco</label>
                  <select id="form-checkout__issuer"></select>
                </div>
                <div class="field">
                  <label>Cuotas</label>
                  <select id="form-checkout__installments"></select>
                </div>
              </div>
              <div class="field">
                <label><input id="save-card" type="checkbox" checked style="min-height:auto;width:auto" /> Guardar tarjeta para próximos pagos</label>
              </div>
              <button type="submit">Autorizar consulta</button>
              <div id="error" class="error"></div>
              <p class="muted">Mercado Pago procesa el pago, pero el paciente nunca sale de DocYa.</p>
            </form>
          </div>
        </div>

        <script>
          const mp = new MercadoPago("{MP_PUBLIC_KEY}", {{ locale: "es-AR" }});
          const form = mp.cardForm({{
            amount: "{monto:.0f}",
            iframe: false,
            form: {{
              id: "form-checkout",
              cardNumber: {{ id: "form-checkout__cardNumber", placeholder: "5031 4332 1540 6351" }},
              expirationDate: {{ id: "form-checkout__expirationDate", placeholder: "MM/YY" }},
              securityCode: {{ id: "form-checkout__securityCode", placeholder: "123" }},
              cardholderName: {{ id: "form-checkout__cardholderName", placeholder: "Nombre del titular" }},
              cardholderEmail: {{ id: "form-checkout__cardholderEmail", placeholder: "email@docya.com" }},
              issuer: {{ id: "form-checkout__issuer" }},
              installments: {{ id: "form-checkout__installments" }},
              identificationType: {{ id: "form-checkout__identificationType" }},
              identificationNumber: {{ id: "form-checkout__identificationNumber", placeholder: "30123456" }}
            }},
            callbacks: {{
              onSubmit: async (event) => {{
                event.preventDefault();
                document.getElementById("error").innerText = "";
                const data = form.getCardFormData();
                try {{
                  const response = await fetch("/pagos/embebido/autorizar", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify({{
                      consulta_id: {consulta_id or 0},
                      paciente_uuid: "{paciente_uuid}",
                      monto: {monto},
                      motivo: {safe_motivo},
                      tipo: "{tipo}",
                      token: data.token,
                      payment_method_id: data.paymentMethodId,
                      issuer_id: data.issuerId || null,
                      installments: parseInt(data.installments || "1"),
                      payer_email: data.cardholderEmail,
                      identification_type: data.identificationType,
                      identification_number: data.identificationNumber,
                      save_card: document.getElementById("save-card").checked
                    }})
                  }});
                  const result = await response.json();
                  if (!response.ok || !result.authorized) {{
                    document.getElementById("error").innerText = result.detail?.message || "No se pudo autorizar el pago.";
                    return;
                  }}
                  window.location.href = "docya://payment_result?status=success&consulta_id={consulta_id or 0}&payment_id=" + result.payment_id;
                }} catch (error) {{
                  document.getElementById("error").innerText = "Error iniciando el cobro dentro de DocYa.";
                }}
              }},
              onError: (error) => {{
                if (error) {{
                  document.getElementById("error").innerText = "Revisá los datos de la tarjeta.";
                }}
              }}
            }}
          }});
        </script>
      </body>
    </html>
    """


@router.get("/consultas/reembolsadas")
def consultas_reembolsadas(db=Depends(get_db)):
    """Devuelve las consultas canceladas cuyo pago fue reembolsado."""
    cur = db.cursor()
    cur.execute(
        """
        SELECT 
            c.id,
            c.paciente_uuid,
            u.full_name,
            u.telefono,
            c.motivo,
            c.direccion,
            c.creado_en,
            c.mp_payment_id,
            c.mp_status,
            c.metodo_pago
        FROM consultas c
        LEFT JOIN users u ON u.id = c.paciente_uuid
        WHERE c.estado = 'cancelada'
          AND c.mp_payment_id IS NOT NULL
          AND c.mp_status = 'refunded'
        ORDER BY c.creado_en DESC
        """
    )

    rows = cur.fetchall()
    resultados = []
    for r in rows:
        resultados.append(
            {
                "consulta_id": r[0],
                "paciente_uuid": r[1],
                "paciente_nombre": r[2],
                "paciente_telefono": r[3],
                "motivo": r[4],
                "direccion": r[5],
                "fecha": str(r[6]),
                "mp_payment_id": r[7],
                "status": r[8],
                "metodo_pago": r[9],
            }
        )

    return {"total": len(resultados), "reembolsos": resultados}


@router.post("/pagos/preautorizar")
def crear_preference(data: dict, db=Depends(get_db)):
    """Flujo legacy de Checkout Pro con redirect externo a MP."""
    consulta_id = str(data["consulta_id"])
    monto = float(data["monto"])
    email = data["email"]

    payload = {
        "items": [
            {
                "title": "Consulta médica a domicilio - DOCYA",
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": monto,
            }
        ],
        "payer": {"email": email},
        "external_reference": consulta_id,
        "back_urls": {
            "success": "docya://pago_exitoso",
            "failure": "docya://pago_fallido",
            "pending": "docya://pago_pendiente",
        },
        "auto_return": "all",
    }

    response = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=_mp_headers(),
        json=payload,
        timeout=20,
    )
    result = response.json()
    if not response.ok or "id" not in result:
        raise HTTPException(400, result)

    return {
        "status": "preference_ok",
        "preference_id": result["id"],
        "init_point": result["init_point"],
    }


@router.post("/webhook/mp")
def webhook_mp(request: Request, db=Depends(get_db)):
    """Webhook de Mercado Pago para reflejar estados y ejecutar refunds diferidos."""
    data_id = request.query_params.get("data.id")
    tipo = request.query_params.get("type")

    if not data_id:
        print("Webhook sin data.id")
        return {"ok": True}

    cur = db.cursor()

    if tipo == "payment":
        print(f"Webhook PAYMENT {data_id}")
        try:
            response = requests.get(
                f"https://api.mercadopago.com/v1/payments/{data_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                timeout=20,
            )
            payment = response.json()

            payment_id = payment.get("id")
            status = payment.get("status")
            consulta_id = payment.get("external_reference")

            if not consulta_id:
                print("PAYMENT sin external_reference -> ignorado")
                return {"ok": True}

            consulta_id = int(consulta_id)
            print(f"Webhook -> consulta {consulta_id} status={status}")

            if status in ["authorized", "in_process", "pending", "approved"]:
                cur.execute(
                    """
                    UPDATE consultas
                    SET 
                        mp_status='preautorizado',
                        mp_preautorizado=TRUE,
                        mp_payment_id=%s
                    WHERE id=%s
                    """,
                    (payment_id, consulta_id),
                )
                db.commit()

            cur.execute("SELECT estado FROM consultas WHERE id=%s", (consulta_id,))
            row = cur.fetchone()

            if row and row[0] == "pendiente_de_refund":
                print(f"Ejecutando refund diferido para consulta {consulta_id}")
                refund_resp = requests.post(
                    f"https://api.mercadopago.com/v1/payments/{payment_id}/refunds",
                    headers={
                        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                        "X-Idempotency-Key": str(uuid.uuid4()),
                    },
                    timeout=20,
                )
                print("Refund:", refund_resp.status_code, refund_resp.text)

                cur.execute(
                    """
                    UPDATE consultas
                    SET estado='cancelada', mp_status='refunded'
                    WHERE id=%s
                    """,
                    (consulta_id,),
                )
                db.commit()

        except Exception as exc:
            print("Error procesando webhook:", exc)

        return {"ok": True}

    print(f"Webhook merchant order {data_id}")
    return {"ok": True}


@router.get("/consultas/{consulta_id}/estado")
def estado_consulta(consulta_id: int, db=Depends(get_db)):
    """Estado resumido de la consulta y de su pago asociado."""
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, estado, mp_status, mp_preautorizado, mp_payment_id 
        FROM consultas
        WHERE id=%s
        """,
        (consulta_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Consulta no encontrada")

    return {
        "consulta_id": row[0],
        "estado": row[1],
        "mp_status": row[2],
        "mp_preautorizado": row[3],
        "payment_id": row[4],
    }


@router.post("/pagos/capturar")
def capturar_pago(data: dict, db=Depends(get_db)):
    """Captura una preautorización cuando la consulta ya fue aceptada."""
    consulta_id = data["consulta_id"]

    cur = db.cursor()
    cur.execute("SELECT mp_payment_id FROM consultas WHERE id=%s", (consulta_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(400, "Payment no encontrado")

    payment_id = row[0]
    response = requests.put(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        json={"capture": True},
        timeout=20,
    )
    result = response.json()
    status = result.get("status")
    if status != "approved":
        raise HTTPException(400, result)

    cur.execute(
        """
        UPDATE consultas
        SET mp_status='approved', pagado=TRUE
        WHERE id=%s
        """,
        (consulta_id,),
    )
    db.commit()
    return {"status": "capturado", "payment_status": "approved"}


@router.post("/pagos/cancelar")
def cancelar_pago(data: dict, db=Depends(get_db)):
    """Cancela una preautorización si no se asignó profesional."""
    consulta_id = data["consulta_id"]

    cur = db.cursor()
    cur.execute("SELECT mp_payment_id FROM consultas WHERE id=%s", (consulta_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(400, "Payment no encontrado")

    payment_id = row[0]
    requests.put(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        json={"status": "cancelled"},
        timeout=20,
    )

    cur.execute(
        """
        UPDATE consultas
        SET mp_status='cancelled'
        WHERE id=%s
        """,
        (consulta_id,),
    )
    db.commit()
    return {"status": "cancelado"}
