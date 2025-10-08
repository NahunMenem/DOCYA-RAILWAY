FROM python:3.12-slim

# Dependencias del sistema necesarias
RUN apt-get update && apt-get install -y \
    build-essential \
    libcairo2 \
    libcairo2-dev \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libgobject-2.0-0 \
    libglib2.0-0 \
    libffi-dev \
    libxml2 \
    libxslt1.1 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    bash \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .



# 🚀 Fijamos puerto 8080 directamente
ENV PORT=8080
EXPOSE 8080

# Ejecutar Uvicorn fijo en 8080 (Railway redirige automáticamente)
CMD ["bash", "-c", "uvicorn main:app --host 0.0.0.0 --port 8080"]

