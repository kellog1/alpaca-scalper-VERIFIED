Set-Location "C:\Users\s_tam\Downloads\alpaca-scalper-VERIFIED"
Start-Process -FilePath ".\venv\Scripts\python.exe" -ArgumentList "main.py" -WindowStyle Hidden
Start-Process -FilePath ".\venv\Scripts\python.exe" -ArgumentList "dashboard.py" -WindowStyle Hidden
