# Imagen base con librerías preinstaladas necesarias para WeasyPrint y psycopg2
FROM python:3.12-bullseye

# Evita prompts interactivos
ENV DEBIAN_FRONTEND=noninteractive

# Instalar dependencias del sistema necesarias para WeasyPrint, PostgreSQL y fonts
RUN apt-get update && apt-get install -y \
    build-essential \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libcairo2 \
    libffi-dev \
    libpq-dev \
    fonts-liberation \
    shared-mime-info \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Crear y usar el directorio de la app
WORKDIR /app

# Copiar archivos del proyecto
COPY . .

# Instalar dependencias Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Exponer el puerto
EXPOSE 8000

# Comando para ejecutar FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
