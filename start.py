import os, subprocess

# Addataは使用しないためDBダウンロードをスキップ（高速起動）
# Cloud Run は PORT 環境変数でポートを指定（デフォルト: 7860 = HuggingFace互換）
port = os.environ.get("PORT", "7860")
print(f"Starting Streamlit app on port {port}...")
subprocess.run([
    "streamlit", "run", "app.py",
    "--server.address=0.0.0.0",
    f"--server.port={port}",
    "--server.headless=true",
    "--browser.gatherUsageStats=false",
])
