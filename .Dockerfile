FROM python:3.12-slim

# Instalar dependencias de sistema necesarias para WeasyPrint
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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Crear entorno de trabajo
WORKDIR /app

# Copiar dependencias y código
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Exponer puerto y comando de inicio
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
