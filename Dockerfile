FROM python:3.11-slim

# p7zip-full: pdf-to-neo スキル（vendor/pdf_to_neo）が塗装指数表（CHM）を展開するのに使う（Linux には hh.exe が無い）
RUN apt-get update && apt-get install -y poppler-utils libgomp1 curl p7zip-full && rm -rf /var/lib/apt/lists/*

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
