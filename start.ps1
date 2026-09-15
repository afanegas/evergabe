# Startet den eV-Checker lokal auf http://127.0.0.1:8765
# Beim ersten Mal wird die Python-Umgebung angelegt.
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

Start-Process "http://127.0.0.1:8765"
# --host 0.0.0.0: auch aus dem eigenen Netz erreichbar (z.B. per Handy/VPN), nicht nur von diesem PC
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8765
