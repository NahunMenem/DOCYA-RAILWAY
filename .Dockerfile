# Imagen base ligera de Python 3.12
FROM python:3.12-slim

# Instalar dependencias del sistema necesarias para WeasyPrint
RUN apt-get update && apt-get install -y \
    build-essential \
    libpango-1.0-0 \
    libcairo2 \
    libcairo2-dev \
    libgdk-pixbuf2.0-0 \
    libgobject-2.0-0 \
    libglib2.0-0 \
    libffi-dev \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libxml2 \
    libxslt1.1 \
    shared-mime-info \
    fonts-liberation \
    fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Crear directorio de trabajo
WORKDIR /app

# Copiar dependencias e instalarlas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo el proyecto
COPY . .

# Exponer el puerto (Railway usa 8080 por defecto)
EXPOSE 8080

# Comando de inicio (ajustá main:app si tu archivo principal se llama distinto)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

