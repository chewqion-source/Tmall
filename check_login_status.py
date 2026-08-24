# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.request import Request, urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
SHOPS_FILE = BASE_DIR / "shops.json"
CONFIG_FILE = BASE_DIR / "config" / "feishu_webhook.json"
STATE_FILE = BASE_DIR / "logs" / "scheduled" / "login_status_state.json"
TEST_URL = "https://myseller.taobao.com/home.htm/trade-platform/tp/sold"

PORT_LABELS = {
    9222: "易丽洁",
    9223: "咖时光",
    9224: "坐拥_宁静",
    9225: "国货严选",
}

BAD_URL_KEYWORDS = [
    "login.taobao.com",
    "login.tmall.com",
    "login.m.taobao.com",
    "passport.taobao.com",
    "x5sec",
    "punish",
    "sec.taobao.com",
]

LOGIN_TEXT_KEYWORDS = [
    "请登录",
    "验证码",
    "扫码登录",
    "手机淘宝扫码",
    "账户名登录",
    "密码登录",
]

CAPTCHA_TEXT_KEYWORDS = [
    "验证码",
    "安全验证",
    "滑块",
    "拖动滑块",
    "请输入验证码",
    "身份验证",
]

GOOD_URL_KEYWORDS = [
    "myseller.taobao.com",
    "trade-platform",
    "QnworkbenchHome",
]


def load_shops() -> list[dict[str, object]]:
    payload = json.loads(SHOPS_FILE.read_text(encoding="utf-8-sig"))
    shops = []
    for raw in payload.get("shops", []):
        if not raw.get("enabled", True):
            continue
        port = int(raw.get("port", 0))
        if not port:
            continue
        shops.append(
            {
                "name": PORT_LABELS.get(port) or str(raw.get("name") or f"端口{port}"),
                "port": port,
            }
        )
    return shops


def load_feishu_config() -> tuple[str, str]:
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    secret = os.environ.get("FEISHU_SECRET", "").strip()
    if CONFIG_FILE.exists():
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        webhook = webhook or str(payload.get("webhook", "")).strip()
        secret = secret or str(payload.get("secret", "")).strip()
    return webhook, secret


def sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu_alert(failed: list[dict[str, str]]) -> None:
    webhook, secret = load_feishu_config()
    if not webhook:
        print("未配置飞书 webhook，跳过登录异常提醒。")
        return

    lines = [
        "⚠️ 千牛登录状态异常，已暂停本轮实时盈亏抓取",
        f"检测时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "异常店铺：",
    ]
    for item in failed:
        lines.append(f"- {item['name']} / 端口 {item['port']}：{item['reason']}")
    lines.extend(
        [
            "",
            "请在对应浏览器完成登录或验证码验证。",
            "系统会在下一次定时任务继续检测，恢复后自动继续抓取。",
        ]
    )

    message: dict[str, object] = {
        "msg_type": "text",
        "content": {"text": "\n".join(lines)},
    }
    if secret:
        timestamp = str(int(time.time()))
        message["timestamp"] = timestamp
        message["sign"] = sign(timestamp, secret)

    request = Request(
        webhook,
        data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        print(response.read().decode("utf-8", errors="ignore"))
    print("飞书登录异常提醒已发送。")


def has_keyword(value: str, keywords: list[str]) -> str:
    lower = value.lower()
    for keyword in keywords:
        if keyword.lower() in lower:
            return keyword
    return ""


def pick_page(context):
    for page in context.pages:
        if "myseller.taobao.com" in page.url or "login" in page.url:
            return page
    if context.pages:
        return context.pages[0]
    return context.new_page()


def check_shop(pw, shop: dict[str, object]) -> dict[str, str]:
    name = str(shop["name"])
    port = int(shop["port"])
    result = {
        "name": name,
        "port": str(port),
        "status": "ok",
        "reason": "登录正常",
        "url": "",
    }

    try:
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}",
            timeout=20_000,
        )
    except Exception as exc:
        result["status"] = "bad"
        result["reason"] = f"浏览器调试端口无法连接：{exc}"
        return result

    try:
        if not browser.contexts:
            result["status"] = "bad"
            result["reason"] = "浏览器没有可用会话"
            return result

        context = browser.contexts[0]
        page = pick_page(context)
        try:
            page.goto(TEST_URL, wait_until="domcontentloaded", timeout=25_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(2_000)

        url = page.url
        title = ""
        text = ""
        try:
            title = page.title(timeout=5_000)
        except Exception:
            title = ""
        try:
            text = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            text = ""

        result["url"] = url
        bad_url = has_keyword(url, BAD_URL_KEYWORDS)
        if bad_url:
            result["status"] = "bad"
            result["reason"] = f"进入登录/安全验证页面：{bad_url}"
            return result

        combined = f"{title}\n{text[:3000]}"
        captcha_text = has_keyword(combined, CAPTCHA_TEXT_KEYWORDS)
        if captcha_text:
            result["status"] = "bad"
            result["reason"] = f"页面提示需要验证码或安全验证：{captcha_text}"
            return result

        bad_text = has_keyword(combined, LOGIN_TEXT_KEYWORDS)
        good_url = has_keyword(url, GOOD_URL_KEYWORDS)
        if bad_text and not good_url:
            result["status"] = "bad"
            result["reason"] = f"页面提示需要登录或验证：{bad_text}"
            return result

        if "myseller.taobao.com" not in url:
            result["status"] = "bad"
            result["reason"] = f"未停留在千牛卖家页面，当前页面：{url}"
            return result

        result["reason"] = f"登录正常，当前页面：{url}"
        return result
    finally:
        # Do not call browser.close(); for CDP sessions this can close the user's Chrome.
        pass


def save_state(results: list[dict[str, str]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    shops = load_shops()
    if not shops:
        print("没有启用的店铺，登录检测跳过。")
        return 0

    with sync_playwright() as pw:
        results = [check_shop(pw, shop) for shop in shops]

    save_state(results)
    failed = [item for item in results if item["status"] != "ok"]

    print("登录状态检测结果：")
    for item in results:
        mark = "OK" if item["status"] == "ok" else "BLOCKED"
        print(f"- {mark} {item['name']} / {item['port']}：{item['reason']}")

    if failed:
        send_feishu_alert(failed)
        return 20

    return 0


if __name__ == "__main__":
    sys.exit(main())
