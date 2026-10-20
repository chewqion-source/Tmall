# -*- coding: utf-8 -*-
"""
咖时光退款接口探针 V1.0
用途：
- 只诊断咖时光（CDP 9223）
- 不改正式退款数据
- 自动连接退款页
- 自动点击“搜索售后单”
- 捕获本次查询后所有 XHR / fetch JSON
- 递归搜索金额 13.41、退款字段、订单字段
- 输出候选接口报告，帮助一次定位真正的退款明细接口

运行：
python coffee_refund_probe.py
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "logs" / "咖时光"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CDP_PORT = 9223

TARGET_AMOUNT_TEXTS = [
    "13.41",
    "13.410",
    "1341",
]

KEYWORDS = [
    "refundFee",
    "refundAmount",
    "buyerActualFee",
    "bizOrderId",
    "orderId",
    "退款总额",
    "退款金额",
    "退款成功",
]


def compact(obj, limit=1200):
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)

    if len(s) > limit:
        return s[:limit] + "..."

    return s


def recursive_find(obj, path=""):
    hits = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)

            lk = str(k).lower()

            if any(
                kw.lower() in lk
                for kw in [
                    "refund",
                    "order",
                    "amount",
                    "fee",
                    "dispute",
                    "total",
                ]
            ):
                if not isinstance(v, (dict, list)):
                    hits.append(
                        (
                            p,
                            v
                        )
                    )

            hits.extend(
                recursive_find(
                    v,
                    p
                )
            )

    elif isinstance(obj, list):
        for i, v in enumerate(obj[:100]):
            hits.extend(
                recursive_find(
                    v,
                    f"{path}[{i}]"
                )
            )

    return hits


async def find_refund_page(context):
    candidates = []

    for p in context.pages:
        u = (p.url or "").lower()

        if "refund-list" in u:
            candidates.append(p)

    if not candidates:
        raise RuntimeError(
            "CDP 9223 没有找到退款管理页，请先打开咖时光退款管理。"
        )

    # 取最后一个退款页
    return candidates[-1]


async def click_search(page):
    scopes = [page] + list(page.frames)

    texts = [
        "搜索售后单",
        "查询",
        "搜索",
    ]

    for scope in scopes:
        for text in texts:
            try:
                loc = scope.get_by_text(
                    text,
                    exact=True
                )

                count = await loc.count()

                for i in range(count):
                    item = loc.nth(i)

                    try:
                        if await item.is_visible():
                            await item.click(
                                timeout=5000
                            )

                            print(
                                f"已点击查询按钮：{text}"
                            )

                            return True

                    except Exception:
                        continue

            except Exception:
                continue

    return False


async def main():
    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    raw_dir = OUT_DIR / f"refund_probe_{stamp}"
    raw_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    report_path = raw_dir / "report.txt"
    page_text_path = raw_dir / "page_text.txt"

    responses = []

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )

        if not browser.contexts:
            raise RuntimeError(
                "CDP 9223 没有可用 context"
            )

        context = browser.contexts[0]
        page = await find_refund_page(
            context
        )

        print(
            f"咖时光退款页：{page.url}"
        )

        # 先保存所有 frame 信息
        print(
            f"Frame 数量：{len(page.frames)}"
        )

        for i, frame in enumerate(
            page.frames
        ):
            print(
                f"Frame[{i}] {frame.url}"
            )

        # 尝试保存所有 frame 文本
        page_text_parts = []

        for i, frame in enumerate(
            page.frames
        ):
            try:
                text = await frame.locator(
                    "body"
                ).inner_text(
                    timeout=5000
                )

                page_text_parts.append(
                    f"\n===== FRAME {i} =====\nURL: {frame.url}\n{text}\n"
                )

            except Exception as e:
                page_text_parts.append(
                    f"\n===== FRAME {i} =====\nURL: {frame.url}\n[读取失败] {e}\n"
                )

        page_text_path.write_text(
            "\n".join(
                page_text_parts
            ),
            encoding="utf-8"
        )

        async def on_response(response):
            try:
                rtype = (
                    response.request.resource_type
                    or
                    ""
                ).lower()

                if rtype not in (
                    "xhr",
                    "fetch",
                ):
                    return

                url = response.url

                # 只关心淘宝相关
                if not any(
                    d in url.lower()
                    for d in [
                        "taobao.com",
                        "tmall.com",
                        "alibaba.com",
                    ]
                ):
                    return

                text = await response.text()

                # JSONP也兼容
                obj = None

                try:
                    obj = json.loads(
                        text
                    )
                except Exception:
                    m = re.search(
                        r"^[^(]*\((.*)\)\s*;?\s*$",
                        text,
                        re.S
                    )

                    if m:
                        try:
                            obj = json.loads(
                                m.group(1)
                            )
                        except Exception:
                            pass

                amount_hit = any(
                    t in text
                    for t in TARGET_AMOUNT_TEXTS
                )

                keyword_hits = [
                    kw
                    for kw in KEYWORDS
                    if kw in text
                ]

                record = {
                    "url":
                        url,

                    "status":
                        response.status,

                    "resource_type":
                        rtype,

                    "amount_hit":
                        amount_hit,

                    "keyword_hits":
                        keyword_hits,

                    "text_len":
                        len(text),

                    "json":
                        obj,
                }

                responses.append(
                    record
                )

            except Exception:
                pass

        context.on(
            "response",
            on_response
        )

        print(
            "开始捕获查询后的所有退款相关网络响应..."
        )

        clicked = await click_search(
            page
        )

        if not clicked:
            print(
                "未找到搜索售后单按钮，尝试 Enter..."
            )

            try:
                await page.keyboard.press(
                    "Enter"
                )
            except Exception:
                pass

        # 充分等待所有异步接口
        await page.wait_for_timeout(
            15000
        )

        try:
            context.remove_listener(
                "response",
                on_response
            )
        except Exception:
            pass

    # 输出每条原始响应
    report = []

    report.append(
        f"咖时光退款探针时间：{stamp}"
    )

    report.append(
        f"捕获 XHR/fetch：{len(responses)} 条"
    )

    report.append("")

    strong = []
    medium = []

    for idx, rec in enumerate(
        responses,
        start=1
    ):
        obj = rec[
            "json"
        ]

        recursive_hits = (
            recursive_find(
                obj
            )
            if obj is not None
            else []
        )

        score = 0

        if rec[
            "amount_hit"
        ]:
            score += 10

        score += min(
            len(
                rec[
                    "keyword_hits"
                ]
            ),
            5
        )

        if any(
            "refund" in p.lower()
            or
            "order" in p.lower()
            for p, _
            in recursive_hits
        ):
            score += 2

        filename = raw_dir / f"response_{idx:03d}.json"

        if obj is not None:
            filename.write_text(
                json.dumps(
                    obj,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )
        else:
            filename = raw_dir / f"response_{idx:03d}.txt"
            filename.write_text(
                "",
                encoding="utf-8"
            )

        summary = {
            "index":
                idx,

            "score":
                score,

            "amount_hit":
                rec["amount_hit"],

            "keyword_hits":
                rec["keyword_hits"],

            "url":
                rec["url"],

            "text_len":
                rec["text_len"],

            "recursive_hits":
                recursive_hits[:80],
        }

        if score >= 8:
            strong.append(
                summary
            )
        elif score >= 3:
            medium.append(
                summary
            )

    report.append(
        "===== 强候选（最值得看） ====="
    )

    if not strong:
        report.append(
            "无"
        )

    for item in strong:
        report.append(
            compact(
                item,
                5000
            )
        )

    report.append("")
    report.append(
        "===== 中等候选 ====="
    )

    if not medium:
        report.append(
            "无"
        )

    for item in medium[:20]:
        report.append(
            compact(
                item,
                3500
            )
        )

    report.append("")
    report.append(
        "===== 页面文本检查 ====="
    )

    try:
        page_text = page_text_path.read_text(
            encoding="utf-8"
        )

        for target in [
            "13.41",
            "退款总额",
            "退款成功",
            "订单编号",
        ]:
            report.append(
                f"{target}: {'找到' if target in page_text else '未找到'}"
            )

    except Exception:
        pass

    report_path.write_text(
        "\n".join(
            report
        ),
        encoding="utf-8"
    )

    print()
    print("=" * 72)
    print("咖时光退款探针完成")
    print("=" * 72)
    print(
        f"捕获网络响应：{len(responses)} 条"
    )
    print(
        f"强候选：{len(strong)} 条"
    )
    print(
        f"中等候选：{len(medium)} 条"
    )
    print(
        f"报告：{report_path}"
    )
    print(
        f"页面文本：{page_text_path}"
    )

    if strong:
        print()
        print("最强候选：")

        for item in strong[:5]:
            print(
                f"[{item['index']}] score={item['score']} "
                f"amount_hit={item['amount_hit']}"
            )

            print(
                item[
                    "url"
                ]
            )


if __name__ == "__main__":
    try:
        asyncio.run(
            main()
        )
    except Exception as e:
        print(
            f"\n程序异常：{type(e).__name__}: {e}"
        )

    input(
        "\n按 Enter 退出..."
    )
