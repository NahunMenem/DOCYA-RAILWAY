#!/bin/bash
echo "Entrando a start.sh..."
if [ -z "$PORT" ]; then
  echo "⚠️  Variable PORT no detectada, usando 8080 por defecto"
  PORT=8080
else
  echo "✅ Puerto detectado: $PORT"
fi
exec uvicorn main:app --host 0.0.0.0 --port $PORT
