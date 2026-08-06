# Dockerfile para Jofra AI - Motor de Prospección B2B & Hub
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Instalar dependencias del sistema requeridas para Playwright y SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements.txt e instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar navegadores headless de Playwright (Chromium) con dependencias del sistema
RUN playwright install --with-deps chromium

# Copiar el código fuente de la aplicación
COPY . .

EXPOSE 8000

# Ejecución dinámica leyendo $PORT asignado por Railway
CMD ["python", "main.py"]
