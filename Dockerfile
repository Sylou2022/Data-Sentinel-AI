FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATASENTINEL_HOME=/data \
    DATASENTINEL_HOST=0.0.0.0 ACCEPT_EULA=Y
WORKDIR /app

# Pilote ODBC Microsoft pour SQL Server (msodbcsql18)
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
      > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update && apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY datasentinel ./datasentinel
COPY api ./api
COPY web ./web
COPY app/image ./app/image

RUN useradd --create-home sentinel && mkdir -p /data && chown sentinel /data
USER sentinel
VOLUME ["/data"]

EXPOSE 8000
CMD ["python", "-m", "api"]
