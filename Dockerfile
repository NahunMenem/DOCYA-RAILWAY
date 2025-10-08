# -------------------------
# Imagen base liviana de Python
# -------------------------
FROM python:3.12-slim

# Instala dependencias del sistema necesarias (para PDF, fuentes, etc.)
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Define el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia primero las dependencias para aprovechar la cache de Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código fuente
COPY . .

# Expone el puerto interno (Railway detecta este automáticamente)
EXPOSE 8080

# -------------------------
# Ejecuta el servidor con Python (sin shell, sin $PORT literal)
# -------------------------
CMD ["python", "start.py"]


