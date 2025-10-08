FROM python:3.12-slim

# 🔧 Instalar dependencias del sistema necesarias para WeasyPrint (versión Debian Trixie)
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

# Crear directorio de trabajo
WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app
COPY . .

# Exponer puerto
EXPOSE 8080

# Comando de inicio
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
