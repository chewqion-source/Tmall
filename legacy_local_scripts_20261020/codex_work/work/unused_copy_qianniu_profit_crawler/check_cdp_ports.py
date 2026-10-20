# -*- coding: utf-8 -*-
"""
CDP 三端口健康检查
运行：
    python check_cdp_ports.py

用途：
- 检查 9222 / 9223 / 9224 是否真正可连接
- 显示 /json/version 是否正常
- 显示占用端口的 PID
- 不会结束任何进程
"""

import json
import socket
import subprocess
import urllib.request

PORTS = [9222, 9223, 9224]


def tcp_ok(port):
    try:
        with socket.create_connection(
            ("127.0.0.1", port),
            timeout=2
        ):
            return True
    except Exception:
        return False


def get_version(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=3
        ) as resp:
            data = json.loads(
                resp.read().decode(
                    "utf-8",
                    errors="ignore"
                )
            )

        return {
            "ok": True,
            "browser": data.get("Browser", ""),
            "ws": data.get("webSocketDebuggerUrl", ""),
        }

    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }


def get_pid(port):
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            timeout=10,
        )

        rows = []

        for line in result.stdout.splitlines():
            if f":{port}" not in line:
                continue

            if "LISTENING" not in line.upper():
                continue

            parts = line.split()

            if len(parts) >= 5:
                rows.append(parts[-1])

        return sorted(set(rows))

    except Exception:
        return []


def main():
    print("=" * 72)
    print("Chrome CDP 端口健康检查")
    print("=" * 72)

    for port in PORTS:
        print()
        print(f"端口 {port}")
        print("-" * 40)

        tcp = tcp_ok(port)
        print(f"TCP监听：{'正常' if tcp else '失败'}")

        pids = get_pid(port)
        print(
            "占用PID："
            +
            (", ".join(pids) if pids else "未找到")
        )

        ver = get_version(port)

        if ver["ok"]:
            print("json/version：正常")
            print(f"Browser：{ver['browser']}")
            print(
                "WebSocket："
                +
                ("存在" if ver["ws"] else "缺失")
            )

            if tcp and ver["ws"]:
                print("基础CDP状态：正常")
            else:
                print("基础CDP状态：异常")
        else:
            print(
                f"json/version：失败 -> {ver['error']}"
            )
            print("基础CDP状态：异常")

    print()
    print("=" * 72)
    print("说明：")
    print("1. TCP正常 + json/version正常，并不保证Playwright WebSocket一定不僵死。")
    print("2. 如果主程序仍卡在 connect_over_cdp，而这里显示端口正常，")
    print("   通常是该 Chrome 调试进程僵死，需要结束对应PID后重新启动该店铺Chrome。")
    print("=" * 72)


if __name__ == "__main__":
    main()

input("\n按 Enter 退出...")
