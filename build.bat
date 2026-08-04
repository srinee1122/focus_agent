@echo off
echo ========================================
echo  Building ERP Agent Dashboard
echo ========================================
echo.

cd /d "C:\Users\srini\OneDrive\Desktop\focus_agent"

echo [1/4] Installing PyInstaller...
pip install pyinstaller --quiet

echo [2/4] Building Focus Agent executable...
python -m PyInstaller focus_agent_agent.spec --noconfirm
echo.

echo [3/4] Building Dashboard executable...
python -m PyInstaller focus_agent_dashboard.spec --noconfirm
echo.

echo [4/4] Assembling distribution package...
if not exist dist\package mkdir dist\package

copy dist\focus_agent.exe         dist\package\
copy dist\erp_dashboard.exe       dist\package\
copy focus_agent\config.py        dist\package\
copy focus_agent\credentials.xlsx dist\package\
copy run_dashboard.bat            dist\package\
copy setup.bat                    dist\package\
copy INSTALL.md                   dist\package\

echo.
echo ========================================
echo  Build complete!  dist\package\ is ready.
echo ========================================
pause
