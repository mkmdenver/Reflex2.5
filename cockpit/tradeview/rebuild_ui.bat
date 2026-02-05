@echo off
setlocal
cd /d "%~dp0templates"
echo [BUILD] npm run build
npm run build
