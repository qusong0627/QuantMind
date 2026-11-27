@echo off
"C:\Program Files\Python312\python.exe" -u "%~dp0qmt-external-bridge\pipe_redis_bridge.py" >> "%~dp0logs\pipe_bridge.log" 2>&1
