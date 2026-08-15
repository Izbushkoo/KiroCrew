@echo off
echo === Kiro Crew Fork Build ^& Install Script (Windows) ===
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%.."

echo [1/3] Building React Frontend SPA...
cd website
call npm ci
call npm run build

echo [2/3] Staging static assets to src/kiro_crew/static/dist...
cd ..
if not exist "src\kiro_crew\static\dist" mkdir "src\kiro_crew\static\dist"
xcopy /E /Y "website\dist\*" "src\kiro_crew\static\dist\"

echo [3/3] Installing Python package into environment...
pip install -e .

echo === Build Completed Successfully! ===
