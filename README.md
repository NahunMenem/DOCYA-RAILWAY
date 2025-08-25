# DocYa Auth Backend (FastAPI)

## 1) Crear tablas (manual)
Ejecutá el SQL en `sql/001_auth_schema.sql` en tu base:
`postgresql://docya_user:C6yw5ysJMTJECK8gZ9uoM3uZ36wDqpep@dpg-d2g0rtodl3ps73enqb3g-a.oregon-postgres.render.com/docya`

## 2) Configuración
Copiá `.env.example` a `.env` y ajustá `JWT_SECRET`.

## 3) Instalar y correr
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## 4) Endpoints
- `POST /auth/register` {email, password, full_name}
- `POST /auth/login` {email, password}
- `POST /auth/google` {id_token}  *(en producción verificá la firma con google-auth)*
- `GET /health`
