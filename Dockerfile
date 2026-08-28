FROM python:3.11-slim

# Tesseract is the actual OCR engine; pytesseract is just a Python wrapper
# around it, so the engine itself has to be installed at the OS level.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "bot.py"]
