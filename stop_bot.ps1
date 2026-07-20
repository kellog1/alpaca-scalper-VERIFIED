Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "main\.py|dashboard\.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
