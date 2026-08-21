# 千牛多店铺实时盈亏抓取系统

本项目用于在本机连接已经登录的 Chrome 调试窗口，抓取千牛/生意参谋/万相相关数据，并按店铺生成实时盈亏 CSV。

## 系统做什么

一键主流程是 `qianniu_profit_crawler_v5_5.py`：

1. 抓取当天订单 SKU、商家编码、成交件数，并从 `config/sku_cost.xlsx` 匹配真实货品成本和快递费。
2. 抓取生意参谋商品实时经营数据。
3. 抓取万相推广消耗，包括全站推广、关键词推广、智能托管等。
4. 抓取当天退款成功金额。
5. 合并重算货品成本、快递成本、平台费用、税费、推广费、退款后的实时盈亏。
6. 输出每家店 `data/<店铺>/latest.csv` 和三店总表 `data/all_shops_latest.csv`。

## 环境依赖

建议使用 Python 3.10 或更高版本。

安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

本系统实际连接的是本机 Chrome 调试端口，仍需要电脑已安装 Google Chrome。

## 首次准备

1. 复制示例配置：

```powershell
Copy-Item shops.example.json shops.json
Copy-Item config.example.json config.json
```

2. 按真实店铺修改 `shops.json`：

- `name`：店铺名称。
- `enabled`：是否启用。
- `port`：Chrome 调试端口，三店通常使用 `9222`、`9223`、`9224`。
- `sycm_url`：生意参谋商品排行页面地址。
- `site_url`：全站推广页面地址。
- `search_url`：关键词推广页面地址。
- `smart_site_url`：智能托管或对应万相页面地址。

3. 准备 SKU 成本表：

系统会使用 `config/sku_cost.xlsx`。表头为：

```text
店铺, 商品ID, 商家编码, SKU规格, 单件货价, 快递费, 备注, 首次发现日期, 最近成交日期
```

如果当天出现未填写成本的 SKU，会输出到 `data/<店铺>/SKU成本未匹配_YYYYMMDD.csv`，按这个文件补齐 `config/sku_cost.xlsx` 后重新运行即可。

## 日常启动步骤

1. 双击运行：

```text
启动浏览器_三店正式版.bat
```

2. 等三个 Chrome 窗口打开后，分别确认店铺已经登录。

3. 检查端口是否正常：

```powershell
python check_cdp_ports.py
```

看到 `TCP监听：正常` 和 `json/version：正常` 后再继续。

4. 运行一键抓取：

```powershell
python qianniu_profit_crawler_v5_5.py
```

5. 查看结果：

- 单店最新结果：`data/<店铺>/latest.csv`
- 三店最新总表：`data/all_shops_latest.csv`
- 未匹配 SKU：`data/<店铺>/SKU成本未匹配_YYYYMMDD.csv`
- 退款明细：`data/<店铺>/refund_detail_YYYYMMDD.csv`
- 退款汇总：`data/<店铺>/refund_summary_YYYYMMDD.csv`

## 本地定时抓取

本项目已提供本地定时抓取脚本，适合使用本机已登录的三店浏览器。

- 安装/启动后台调度：运行 `install_startup_scheduler.ps1`
- 定时频率：每天 09:00-23:59，每 2 小时一次
- 手动立即跑一次：运行 `run_scheduled_realtime.bat`
- 抓取日志：`logs/scheduled/realtime_YYYYMMDD.log`
- 调度日志：`logs/scheduled/scheduler_loop.log`

后台调度会防止重复运行：如果上一轮还没结束，下一轮会自动跳过。抓取完成后会自动上传 `data/realtime_snapshot/latest.json` 到网站服务器。

## 常见问题

### 端口检查失败

先确认 `启动浏览器_三店正式版.bat` 已经运行，且三个 Chrome 窗口没有关闭。

如果 Chrome 窗口开着但端口仍异常，可能是调试进程僵死。关闭对应 Chrome 窗口后重新运行启动脚本。

### 提示登录失效或接口未捕获

通常是对应店铺没有登录、登录态过期，或者页面 URL 不再适用。

处理顺序：

1. 打开对应端口的 Chrome 窗口。
2. 手动访问千牛/生意参谋/万相页面。
3. 确认当前登录的是正确店铺。
4. 如页面地址变化，更新 `shops.json` 里的 URL。
5. 重新运行一键抓取。

### SKU 成本未匹配

查看 `data/<店铺>/SKU成本未匹配_YYYYMMDD.csv`。

多数情况下是 `config/sku_cost.xlsx` 中对应 SKU 的 `单件货价` 或 `快递费` 没有填写。补齐后重新运行，系统会把完整匹配的订单 SKU 成本合并回实时盈亏。

### Excel 打开了成本表

如果 `config/sku_cost.xlsx` 正在被 Excel 占用，脚本可能无法写入新增 SKU。关闭 Excel 后重试。

### 推广消耗显示为 0

可能原因：

- 该商品当天确实没有推广消耗。
- 对应推广页面没有打开成功。
- 万相页面接口没有被捕获。
- URL 日期或页面参数不对。

先看终端提示，再检查 `shops.json` 里的 `site_url`、`search_url`、`smart_site_url`。

### 退款扣在第一行

退款抓取器输出的是订单级退款，实时经营表是商品级。当前系统为了保证整店总盈亏准确，会把店铺当天退款成功总额只记在该店第一行，避免同一笔退款被多个商品重复扣减。

因此，三店总盈亏和单店总盈亏更可靠；如果看单个商品利润，第一行会包含店铺退款总额，需要注意这个口径。

## 本地版本备份

本项目建议只做本地 git 备份，或上传到私有仓库。不要提交以下内容：

- 浏览器 profile、Cookie、登录态。
- `data/` 抓取结果。
- `logs/` 调试日志。
- 真实 `shops.json`、`config.json`。
- `config/sku_cost.xlsx` 和真实成本配置。

这些已经在 `.gitignore` 中排除。
