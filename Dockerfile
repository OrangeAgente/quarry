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

# Install Chromium's OS-level dependencies as root. The unprivileged app user
# cannot apt-install, which is why crawl4ai-setup's dependency step failed
# (su: Authentication failure) and left Chromium unable to launch at runtime.
RUN python -m playwright install-deps chromium

RUN chown -R app:app /app
USER app

# Download the Chromium browser binary into /home/app/.cache/ms-playwright as the
# app user (OS deps already present from the root step). Do this explicitly —
# crawl4ai-setup aborts its own browser download when its dep-install step
# (which needs root) fails, leaving no Chromium binary at runtime.
RUN python -m playwright install chromium
RUN crawl4ai-setup || true

USER root

COPY --chown=app:app . .
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
RUN mkdir -p /app/data && chown -R app:app /app

EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "app.py"]
