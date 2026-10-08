FROM python:3.11-slim

# System dependencies for crawl4ai and playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget gnupg2 \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 300 --retries 10 -r requirements.txt

# Install Playwright browsers for crawl4ai
RUN playwright install chromium && playwright install-deps chromium

# Copy project code
COPY . .

# Default: run the full pipeline
CMD ["python", "run_pipeline.py"]
