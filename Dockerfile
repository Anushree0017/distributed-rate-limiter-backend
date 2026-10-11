# 3.14, not 3.12: repositories/client_repository.py defines a method named
# `list`, which shadows the builtin inside the class body and breaks the
# later `-> list[ClientSecret]` return annotation under eager annotation
# evaluation. Invisible in local dev because the venv runs 3.14, where
# annotations are lazily evaluated by default (PEP 649) - matching that
# interpreter here avoids hitting the bug without touching service code.
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
