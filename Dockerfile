# -------------------------
# Etapa base
# -------------------------
FROM python:3.12-slim

# Instala dependencias del sistema necesarias (PDF, fuentes, etc.)
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

# Establece el directorio de trabajo
WORKDIR /app

# Copia requirements e instala dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto de los archivos del proyecto
COPY . .

# Da permisos de ejecución al script start.sh
RUN chmod +x /app/start.sh

# Expone el puerto 8080 (Railway lo redirige internamente)
EXPOSE 8080

# Ejecuta el script bash que arranca uvicorn con el puerto real
CMD ["bash", "/app/start.sh"]

