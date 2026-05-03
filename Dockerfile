FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN chown -R app:app /app
USER app

# Install Chromium into /home/app/.cache as the app user
RUN crawl4ai-setup

USER root

COPY --chown=app:app . .
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
RUN mkdir -p /app/data && chown -R app:app /app

EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "app.py"]
