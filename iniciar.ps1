# Arranca todo el proyecto: base de datos, modelo y servidor web.
# Uso (PowerShell, desde esta carpeta):  .\iniciar.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "1/3 Base de datos (PostgreSQL + pgvector)..." -ForegroundColor Cyan
docker compose up -d | Out-Null

Write-Host "2/3 Modelos de Ollama..." -ForegroundColor Cyan
$modelos = ollama list
foreach ($m in @("qwen2.5:1.5b", "embeddinggemma")) {
    if ($modelos -notmatch [regex]::Escape($m)) { Write-Host "  Falta $m -> ollama pull $m" -ForegroundColor Yellow }
}

$cfg = & .\.venv\Scripts\python.exe -c "from app import config; print(config.descripcion_modelo(), '|', len(config.GEMINI_KEYS), 'clave(s) de Gemini')"
Write-Host "    Modelo activo: $cfg" -ForegroundColor Cyan
Write-Host "3/3 Servidor web en http://127.0.0.1:8000  (Ctrl+C para detener)" -ForegroundColor Green
$env:PYTHONIOENCODING = "utf-8"
# El proveedor (Gemini u Ollama), el modelo y las claves se configuran en el archivo .env
Start-Process "http://127.0.0.1:8000"
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
