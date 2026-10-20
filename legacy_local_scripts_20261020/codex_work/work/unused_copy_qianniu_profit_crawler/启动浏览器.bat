@echo off
chcp 65001 >nul
title 多店铺抓取浏览器

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "ROOT=C:\Users\Administrator\Desktop\qianniu_profit_crawler\chrome_profiles"

echo ==========================================
echo      启动多店铺抓取专用 Chrome
echo ==========================================
echo.

if not exist "%CHROME%" (
    echo [错误] 找不到 Chrome：
    echo %CHROME%
    echo.
    echo 请截图发给我，我帮你找实际 Chrome 路径。
    pause
    exit /b
)

if not exist "%ROOT%" mkdir "%ROOT%"

echo 正在启动：易丽洁
start "易丽洁" "%CHROME%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir="%ROOT%\易丽洁" --start-maximized

timeout /t 3 /nobreak >nul

echo 正在启动：咖时光
start "店铺B" "%CHROME%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 --user-data-dir="%ROOT%\店铺B" --start-maximized

echo.
echo ==========================================
echo 两个店铺浏览器启动完成
echo 易丽洁：9222
echo 咖时光：9223
echo ==========================================
echo.
pause