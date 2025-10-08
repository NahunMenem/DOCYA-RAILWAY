# 🩺 DocYa Monitor

Microservicio de monitoreo para DocYa.  
Recibe eventos del backend principal y genera métricas de actividad.

## 🚀 Despliegue en Railway

1. Crear un nuevo proyecto en Railway.
2. Agregar variable `DATABASE_URL` apuntando a tu PostgreSQL.
3. Deploy automático con este repo.

## 🔗 Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/health` | Estado del servicio |
| `POST` | `/api/events` | Recibe evento JSON |
| `GET` | `/api/stats/summary` | KPIs de sistema |
| `GET` | `/api/stats/events` | Últimos eventos registrados |

Ejemplo para enviar evento desde tu backend:

```python
import requests
requests.post("https://tuapp.railway.app/api/events", json={
    "event_type": "consulta_creada",
    "payload": {"consulta_id": 1, "paciente": "UUID"},
    "source": "backend"
})
