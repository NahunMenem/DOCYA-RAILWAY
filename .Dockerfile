# Imagen base oficial de Python
FROM python:3.12-slim

# Instalar dependencias del sistema necesarias para WeasyPrint y fuentes
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libcairo2 \
    libffi-dev \
    libpangoft2-1.0-0 \
    fonts-liberation \
    shared-mime-info \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Crear directorio de la app
WORKDIR /app

# Copiar archivos del proyecto
COPY . .

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Exponer el puerto
EXPOSE 8000

# Comando para ejecutar el servidor FastAPI con Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
