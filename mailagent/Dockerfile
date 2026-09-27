# Image Debian + Python : les dépendances (pydantic-core…) y ont des paquets
# précompilés pour le Raspberry Pi (aarch64), donc pas de compilation longue.
FROM python:3.12-slim

# tzdata : pour que l'heure de l'analyse automatique suive le fuseau de HA (variable TZ)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mail_agent ./mail_agent

# Toutes les données (config, jetons, historique) dans /data : persistant et privé à l'add-on
ENV MAILAGENT_DATA=/data \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "mail_agent.server"]
