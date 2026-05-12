@echo off
echo ========================================
echo    Склад Телефонов - Запуск
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: Python не установлен!
    echo Скачай с https://python.org
    pause
    exit
)

echo Устанавливаю зависимости...
pip install -r requirements.txt -q

echo.
echo Запускаю программу...
echo Открой браузер и перейди на: http://localhost:5000
echo Для остановки нажми Ctrl+C
echo.
python app.py
pause
