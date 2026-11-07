# Debian "trixie" ships LibreOffice 25.x, whose Apple iWork importer is much better than bookworm's 7.4.
FROM python:3.12-slim-trixie

# LibreOffice (headless) reads Word/Excel/PowerPoint/OpenDocument and Apple iWork (.pages/.numbers/.key).
# Extra fonts so documents keep their look: Liberation (metric-compatible with Arial/Times/Calibri-ish),
# Noto (most world scripts, incl. Chinese/Japanese/Korean), DejaVu.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
        fonts-liberation fonts-dejavu fonts-crosextra-carlito fonts-crosextra-caladea \
        fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates templates
USER app

ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
# One process + threads so the parallel-job limit is shared by all requests.
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 180 app:app
