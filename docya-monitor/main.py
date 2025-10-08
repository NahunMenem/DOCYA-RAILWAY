# ====================================================
# 🩺 DOCYA MONITOR SERVICE
# ====================================================
import os, json
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import asyncpg
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ====================================================
# 🔧 CONFIG
# ====================================================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("⚠️ Missing DATABASE_URL environment variable")

app = FastAPI(title="DocYa Monitor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================================================
# 🧩 DB CONNECTION
# ====================================================
@app.on_event("startup")
async def startup():
    app.state.db = await asyncpg.create_pool(DATABASE_URL)
    print("✅ DB Pool initialized")

@app.on_event("shutdown")
async def shutdown():
    await app.state.db.close()
    print("🧹 DB Pool closed")

# ====================================================
# 🪶 MODELS
# ====================================================
class EventIn(BaseModel):
    event_type: str
    payload: dict
    source: str = "docya-backend"

# ====================================================
# 📥 ENDPOINTS
# ====================================================

@app.get("/health")
async def health():
    return {"ok": True, "service": "docya-monitor", "time": datetime.utcnow().isoformat()}

@app.post("/api/events")
async def receive_event(event: EventIn):
    """Recibe eventos desde el backend principal"""
    try:
        async with app.state.db.acquire() as conn:
            await conn.execute("""
                INSERT INTO analytics.events (event_type, payload, source, created_at)
                VALUES ($1, $2, $3, NOW())
            """, event.event_type, json.dumps(event.payload), event.source)
        print(f"📩 Evento recibido: {event.event_type}")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats/summary")
async def summary():
    """KPIs principales para dashboard"""
    async with app.state.db.acquire() as conn:
        total_medicos = await conn.fetchval("SELECT COUNT(*) FROM medicos")
        total_pacientes = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_consultas = await conn.fetchval("SELECT COUNT(*) FROM consultas")
        total_finalizadas = await conn.fetchval("SELECT COUNT(*) FROM consultas WHERE estado='finalizada'")
        eventos = await conn.fetchval("SELECT COUNT(*) FROM analytics.events")
    return {
        "medicos": total_medicos,
        "pacientes": total_pacientes,
        "consultas": total_consultas,
        "finalizadas": total_finalizadas,
        "eventos_registrados": eventos
    }


@app.get("/api/stats/events")
async def stats_events(limit: int = 50):
    """Últimos eventos"""
    async with app.state.db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, event_type, source, created_at, payload
            FROM analytics.events
            ORDER BY created_at DESC
            LIMIT $1
        """, limit)
    return [dict(r) for r in rows]
