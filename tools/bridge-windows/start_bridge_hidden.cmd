@echo off
cd /d C:\new_tdx_mock\PYPlugins\bridge-windows
"C:\Program Files\Python312\python.exe" -u -m main --mode auto --config config.yaml >> data\logs\bridge.out.log 2>&1
