# Portada y Ajustes del servidor. La construye el propio servidor (servidor.py)
# con el codigo de cada version; no hace falta ningun registro de imagenes.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd -u 10001 -M -s /usr/sbin/nologin panel \
    && mkdir -p /datos && chown panel /datos
COPY panel panel
ARG VERSION=local
ENV VERSION=$VERSION
USER panel
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ajustes/salud', timeout=4)"
CMD ["uvicorn", "panel.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
