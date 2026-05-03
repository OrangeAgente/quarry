FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg2 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -s /bin/bash app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN chown -R app:app /app
USER app

# Install Chromium into /home/app/.cache so the runtime user can read it
RUN crawl4ai-setup

COPY --chown=app:app . .
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "app.py"]
