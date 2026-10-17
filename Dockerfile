# Slim image + Chromium headless shell only (rather than the ~1 GB all-browser base image).
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive
WORKDIR /app
COPY requirements.txt ./
# --with-deps installs system dependencies; --only-shell installs only headless Chromium.
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip
COPY browser.py parse_trains.py general_form.py watcher.py ./
CMD ["python", "-u", "watcher.py"]
