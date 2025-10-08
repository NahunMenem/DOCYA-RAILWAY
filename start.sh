#!/bin/bash
echo "🚀 Iniciando contenedor DOCYA..."

if [ -z "$PORT" ]; then
  echo "⚠️  Variable PORT no detectada, usando 8080 por defecto"
  PORT=8080
else
  echo "✅ Puerto detectado: $PORT"
fi

echo "🌐 Levantando servidor en puerto $PORT..."
exec uvicorn main:app --host 0.0.0.0 --port $PORT
