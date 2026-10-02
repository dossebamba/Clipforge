# Lance Clipforge (dashboard + worker + surveillance) : http://127.0.0.1:8000
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m clipforge.cli serve
