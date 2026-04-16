import json
import logging
import os
from typing import Any, Dict
from urllib import error, request


LOGGER = logging.getLogger("docya.farmalink")


def create_farmalink_payload(prescription: Dict[str, Any]) -> Dict[str, Any]:
    patient = prescription.get("patient", {})
    doctor = prescription.get("doctor", {})
    medications = prescription.get("medications", [])

    return {
        "prescription_number": prescription.get("cuir"),
        "patient_cuil": patient.get("cuil"),
        "patient_dni": patient.get("dni"),
        "doctor": {
            "full_name": doctor.get("full_name"),
            "specialty": doctor.get("specialty"),
            "license_number": doctor.get("license_number"),
            "care_address": doctor.get("care_address"),
        },
        "medications": [
            {
                "ifa": med.get("ifa"),
                "commercial_name": med.get("commercial_name"),
                "presentation": med.get("presentation"),
                "pharmaceutical_form": med.get("pharmaceutical_form"),
                "quantity": med.get("quantity"),
                "instructions": med.get("instructions"),
            }
            for med in medications
        ],
        "diagnosis": prescription.get("diagnosis"),
        "issued_at": prescription.get("issued_at"),
    }


def send_prescription_to_farmalink(payload: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = os.getenv("FARMALINK_ENDPOINT", "").strip()
    use_mock = os.getenv("FARMALINK_USE_MOCK", "true").strip().lower() != "false"

    if use_mock or not endpoint:
        response = {
            "ok": True,
            "mock": True,
            "status_code": 200,
            "message": "Mock Farmalink accepted payload",
            "payload_echo": payload,
        }
        LOGGER.info("Farmalink mock response: %s", json.dumps(response, ensure_ascii=False))
        return response

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "DocYa-Farmalink/1.0",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8") if resp else ""
            parsed = json.loads(raw) if raw else {}
            response = {
                "ok": 200 <= resp.status < 300,
                "mock": False,
                "status_code": resp.status,
                "response": parsed,
            }
            LOGGER.info("Farmalink response: %s", json.dumps(response, ensure_ascii=False))
            return response
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        response = {
            "ok": False,
            "mock": False,
            "status_code": exc.code,
            "error": raw or str(exc),
        }
        LOGGER.warning("Farmalink HTTP error: %s", json.dumps(response, ensure_ascii=False))
        return response
    except Exception as exc:
        response = {
            "ok": False,
            "mock": False,
            "status_code": None,
            "error": str(exc),
        }
        LOGGER.exception("Farmalink unexpected error")
        return response
