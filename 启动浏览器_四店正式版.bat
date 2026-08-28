@echo off
chcp 65001 >nul
title 千牛四店铺抓取浏览器

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "ROOT=C:\Users\Administrator\Desktop\qianniu_profit_crawler\chrome_profiles"

echo ==================================================
echo          启动千牛四店铺抓取专用 Chrome
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

echo [1/4] 正在启动：易丽洁  CDP 9222
start "易丽洁 - 9222" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%ROOT%\易丽洁" ^
  --start-maximized ^
  "https://myseller.taobao.com/home.htm"

timeout /t 3 /nobreak >nul

echo [2/4] 正在启动：咖时光  CDP 9223
rem 保留原来的“店铺B”用户目录，避免丢失已经登录好的咖时光账号状态
start "咖时光 - 9223" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9223 ^
  --user-data-dir="%ROOT%\店铺B" ^
  --start-maximized ^
  "https://myseller.taobao.com/home.htm"

timeout /t 3 /nobreak >nul

echo [3/4] 正在启动：坐拥_宁静  CDP 9224
start "坐拥_宁静 - 9224" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9224 ^
  --user-data-dir="%ROOT%\坐拥_宁静" ^
  --start-maximized ^
  "https://myseller.taobao.com/home.htm"

timeout /t 3 /nobreak >nul

echo [4/4] 正在启动：国货严选  CDP 9225
start "国货严选 - 9225" "%CHROME%" ^
  --remote-debugging-address=127.0.0.1 ^
  --remote-debugging-port=9225 ^
  --user-data-dir="%ROOT%\国货严选" ^
  --start-maximized ^
  "https://myseller.taobao.com/home.htm"

echo.
echo ==================================================
echo 四个店铺浏览器启动完成
echo.
echo 易丽洁     ：127.0.0.1:9222
echo 咖时光     ：127.0.0.1:9223
echo 坐拥_宁静  ：127.0.0.1:9224
echo 国货严选   ：127.0.0.1:9225
echo ==================================================
echo.
echo 提示：
echo 1. 四个 Chrome 窗口都不要关闭
echo 2. 分别确认对应店铺已经登录
echo 3. 国货严选登录完成并配置到 shops.json 后，才会进入正式抓取
echo.
pause
