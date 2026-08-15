$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "未找到项目虚拟环境。请先按 README.md 安装 requirements.txt。"
}

Set-Location $projectDir
& $python -m streamlit run app.py --browser.gatherUsageStats=false
