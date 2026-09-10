FROM python:3.11-slim

RUN apt-get update && apt-get install -y poppler-utils libgomp1 curl && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --user --no-cache-dir --upgrade pip && pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Cloud Run は PORT 環境変数でポートを指定（デフォルト8080）
# HuggingFace は 7860 固定
ENV PORT=8080
EXPOSE ${PORT}

CMD ["python", "start.py"]
