@echo off
chcp 65001 >nul
title 千牛三店铺抓取浏览器

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "ROOT=C:\Users\Administrator\Desktop\qianniu_profit_crawler\chrome_profiles"

echo ==================================================
echo          启动千牛三店铺抓取专用 Chrome
echo ==================================================
echo.

if not exist "%CHROME%" (
    echo [错误] 找不到 Chrome：
    echo %CHROME%
    echo.
    pause
    exit /b
)

if not exist "%ROOT%" mkdir "%ROOT%"

echo [1/3] 正在启动：易丽洁  CDP 9222
start "易丽洁 - 9222" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%ROOT%\易丽洁" ^
  --start-maximized

timeout /t 3 /nobreak >nul

echo [2/3] 正在启动：咖时光  CDP 9223
rem 保留原来的“店铺B”用户目录，避免丢失已经登录好的咖时光账号状态
start "咖时光 - 9223" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9223 ^
  --user-data-dir="%ROOT%\店铺B" ^
  --start-maximized

timeout /t 3 /nobreak >nul

echo [3/3] 正在启动：坐拥_宁静  CDP 9224
start "坐拥_宁静 - 9224" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9224 ^
  --user-data-dir="%ROOT%\坐拥_宁静" ^
  --start-maximized

echo.
echo ==================================================
echo 三个店铺浏览器启动完成
echo.
echo 易丽洁     ：127.0.0.1:9222
echo 咖时光     ：127.0.0.1:9223
echo 坐拥_宁静  ：127.0.0.1:9224
echo ==================================================
echo.
echo 提示：
echo 1. 三个 Chrome 窗口都不要关闭
echo 2. 分别确认对应店铺已经登录
echo 3. 再运行 qianniu_profit_crawler_v5_5.py
echo.
pause
