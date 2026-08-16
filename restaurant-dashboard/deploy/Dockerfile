FROM python:3.13-slim

WORKDIR /app

# Playwright system dependencies (installed inside the image — nothing needed on the host)
RUN pip install --no-cache-dir playwright==1.47.0 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8787

CMD ["python", "-m", "app.main"]
