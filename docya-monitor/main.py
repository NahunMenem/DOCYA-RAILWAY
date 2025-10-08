from fastapi import FastAPI, Request
import asyncpg, os, datetime, json

DATABASE_URL = os.getenv("DATABASE_URL")
app = FastAPI(title="DocYa Monitor", version="1.0.0")

@app.on_event("startup")
async def startup():
    app.state.db = await asyncpg.create_pool(DATABASE_URL)

@app.post("/api/events")
async def receive_event(req: Request):
    data = await req.json()
    event_type = data.get("event_type")
    payload = data.get("payload", {})
    source = data.get("source", "unknown")

    async with app.state.db.acquire() as conn:
        await conn.execute("""
            INSERT INTO analytics.events (event_type, payload, source, created_at)
            VALUES ($1, $2, $3, NOW())
        """, event_type, json.dumps(payload), source)

    return {"ok": True}

@app.get("/api/stats/summary")
async def summary():
    async with app.state.db.acquire() as conn:
        total_medicos = await conn.fetchval("SELECT COUNT(*) FROM medicos")
        total_pacientes = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_consultas = await conn.fetchval("SELECT COUNT(*) FROM consultas")
        total_finalizadas = await conn.fetchval("SELECT COUNT(*) FROM consultas WHERE estado='finalizada'")
    return {
        "medicos": total_medicos,
        "pacientes": total_pacientes,
        "consultas": total_consultas,
        "finalizadas": total_finalizadas
    }
