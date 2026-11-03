# Reinicia SOLO el servidor de la aplicación (úsalo después de cambiar el archivo .env o las claves).
# No toca la base de datos ni Ollama.   Uso:  .\reiniciar.ps1
Set-Location $PSScriptRoot

Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn app.main' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 2

$env:PYTHONIOENCODING = "utf-8"
Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -RedirectStandardOutput "$env:TEMP\uvicorn.out" -RedirectStandardError "$env:TEMP\uvicorn.err" -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep 1
    try {
        $r = Invoke-RestMethod http://127.0.0.1:8000/api/llm -TimeoutSec 2
        Write-Host "Servidor reiniciado -> $($r.modelo)" -ForegroundColor Green
        if ($r.claves) { Write-Host "Claves de Gemini: $($r.claves.configuradas) configuradas, $($r.claves.disponibles) disponibles" }
        Write-Host "Recarga la página (Ctrl+F5)."
        return
    } catch { }
}
Write-Host "El servidor no respondió. Revisa $env:TEMP\uvicorn.err" -ForegroundColor Red
