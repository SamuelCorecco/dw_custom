#!/bin/bash

# Script di avvio veloce per Mac
# Rendi eseguibile: chmod +x start.command

cd "$(dirname "$0")"

echo "============================================"
echo "Vela Video Downloader v3 - Avvio"
echo "============================================"
echo ""

# Controlla Python
if ! command -v python3 &> /dev/null; then
    echo "Python 3 non trovato."
    echo "Installa da: https://www.python.org/downloads/"
    read -p "Premi INVIO per chiudere..."
    exit 1
fi

echo "Python trovato: $(python3 --version)"
echo ""

# Controlla dipendenze
if ! python3 -c "import fastapi" &> /dev/null; then
    echo "Installazione dipendenze..."
    pip3 install -r requirements.txt
    echo ""
fi

# Avvia server
echo "Avvio server FastAPI..."
echo "Download: ~/Downloads/VideoDownloader"
echo "Server: http://127.0.0.1:8000"
echo "API: http://127.0.0.1:8000/docs"
echo ""
echo "Premi CTRL+C per fermare"
echo "============================================"
echo ""
source venv/bin/activate 
python -m uvicorn api:app --host 127.0.0.1 --port 8000 --reload
