@echo off
cd /d "C:\Users\srini\OneDrive\Desktop\focus_agent"
git add .
git commit -m "Auto-save: %date% %time%"
git push
echo.
echo Done! Changes pushed to GitHub.
pause
