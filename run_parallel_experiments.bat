@echo off
setlocal
python run_parallel_experiments.py
if errorlevel 1 pause
endlocal
