# -*- coding: utf-8 -*-

"""
千牛多店铺管理器 V1.0

作用：
1. 查看现有店铺
2. 新增店铺
3. 自动分配 Chrome 调试端口
4. 自动创建独立 Chrome 用户目录
5. 自动启动 Chrome
6. 自动生成当天 URL
7. 自动写入 shops.json
8. 启用 / 停用店铺
9. 删除店铺

注意：
本程序不修改 qianniu_profit_crawler.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode


# ============================================================
# 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

SHOPS_FILE = BASE_DIR / "shops.json"

PROFILE_ROOT = BASE_DIR / "chrome_profiles"

PROFILE_ROOT.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# Chrome 可能安装位置
# ============================================================

CHROME_PATHS = [

    r"C:\Program Files\Google\Chrome\Application\chrome.exe",

    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",

    os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
    ),

]


# ============================================================
# 日期
# ============================================================

def today_string():

    return datetime.now().strftime(
        "%Y-%m-%d"
    )


# ============================================================
# 查找 Chrome
# ============================================================

def find_chrome():

    for path in CHROME_PATHS:

        path = os.path.expandvars(
            path
        )

        if os.path.exists(
            path
        ):

            return path

    return None


# ============================================================
# 读取 shops.json
# ============================================================

def load_config():

    if not SHOPS_FILE.exists():

        return {
            "shops": []
        }

    try:

        with open(
            SHOPS_FILE,
            "r",
            encoding="utf-8-sig"
        ) as f:

            data = json.load(
                f
            )

        if not isinstance(
            data,
            dict
        ):

            return {
                "shops": []
            }

        if (
            "shops"
            not in data
            or
            not isinstance(
                data["shops"],
                list
            )
        ):

            data["shops"] = []

        return data

    except Exception as e:

        print()
        print(
            "❌ shops.json 读取失败："
        )
        print(
            e
        )
        print()

        return None


# ============================================================
# 保存 shops.json
# ============================================================

def save_config(
    config
):

    # ========================================================
    # 保存前自动备份
    # ========================================================

    if SHOPS_FILE.exists():

        backup = (
            BASE_DIR
            /
            "shops_backup.json"
        )

        try:

            backup.write_bytes(
                SHOPS_FILE.read_bytes()
            )

        except Exception:

            pass


    with open(
        SHOPS_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            config,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 自动寻找下一个端口
#
# 9222
# 9223
# 9224
# ...
# ============================================================

def next_port(
    shops
):

    used = set()

    for shop in shops:

        try:

            used.add(
                int(
                    shop.get(
                        "port"
                    )
                )
            )

        except Exception:

            pass


    port = 9222

    while port in used:

        port += 1

    return port


# ============================================================
# 安全文件夹名称
# ============================================================

def safe_name(
    name
):

    bad_chars = (
        '<>:"/\\|?*'
    )

    result = name

    for char in bad_chars:

        result = result.replace(
            char,
            "_"
        )

    return result.strip()


# ============================================================
# 生意参谋 URL
# ============================================================

def make_sycm_url():

    today = today_string()

    return (

        "https://sycm.taobao.com/cc/item_rank"

        f"?dateRange={today}%7C{today}"

        "&dateType=today"

    )


# ============================================================
# 普通货品全站推广 URL
# ============================================================

def make_site_url():

    return (

        "https://one.alimama.com/index.html"

        "#!/manage/onesite"

    )


# ============================================================
# 关键词推广 URL
# ============================================================

def make_search_url():

    today = today_string()

    params = {

        "mx_bizCode":
            "onebpSearch",

        "bizCode":
            "onebpSearch",

        "tab":
            "adgroup",

        "startTime":
            today,

        "endTime":
            today,

    }

    return (

        "https://one.alimama.com/index.html"

        "#!/manage/search?"

        +

        urlencode(
            params
        )

    )


# ============================================================
# 智能托管入口 URL
#
# 注意：
#
# 这里故意不写固定 campaignId。
#
# 原因：
# 每个店铺 campaignId 不一样，
# 新建计划后也可能变化。
#
# 先进入全站推广入口，
# 后续由抓取器监听真实接口。
# ============================================================

def make_smart_site_url():

    today = today_string()

    params = {

        "mx_bizCode":
            "onebpSite",

        "bizCode":
            "onebpSite",

        "tab":
            "campaignShopGroup",

        "startTime":
            today,

        "endTime":
            today,

        "effectEqual":
            "15",

        "unifyType":
            "last_click_by_effect_time",

    }

    return (

        "https://one.alimama.com/index.html"

        "#!/manage/onesite?"

        +

        urlencode(
            params
        )

    )


# ============================================================
# 创建店铺配置
# ============================================================

def build_shop_config(
    name,
    port
):

    profile_name = (
        f"shop_{port}_"
        f"{safe_name(name)}"
    )

    return {

        "name":
            name,

        "enabled":
            True,

        "port":
            port,

        "profile":
            profile_name,

        "sycm_url":
            make_sycm_url(),

        "site_url":
            make_site_url(),

        "search_url":
            make_search_url(),

        "smart_site_url":
            make_smart_site_url(),

    }


# ============================================================
# 打印店铺
# ============================================================

def print_shops(
    shops
):

    print()
    print(
        "=" * 68
    )

    print(
        "当前店铺"
    )

    print(
        "=" * 68
    )


    if not shops:

        print(
            "暂无店铺"
        )

        return


    for i, shop in enumerate(
        shops,
        1
    ):

        enabled = shop.get(
            "enabled",
            True
        )

        status = (
            "启用"
            if enabled
            else
            "停用"
        )

        print(
            f"{i}. "
            f"{shop.get('name', '')}"
        )

        print(
            f"   状态：{status}"
        )

        print(
            f"   Chrome端口："
            f"{shop.get('port', '')}"
        )

        print(
            f"   Profile："
            f"{shop.get('profile', '')}"
        )

        print()


# ============================================================
# 启动某个店铺 Chrome
# ============================================================

def launch_shop_chrome(
    shop
):

    chrome = find_chrome()

    if not chrome:

        print()
        print(
            "❌ 没找到 Google Chrome。"
        )

        print(
            "请确认 Chrome 已安装。"
        )

        return False


    port = int(
        shop[
            "port"
        ]
    )


    profile_name = (
        shop.get(
            "profile"
        )
        or
        f"shop_{port}"
    )


    profile_dir = (
        PROFILE_ROOT
        /
        safe_name(
            profile_name
        )
    )


    profile_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    command = [

        chrome,

        f"--remote-debugging-port={port}",

        f"--user-data-dir={profile_dir}",

        "--no-first-run",

        "--no-default-browser-check",

        make_sycm_url(),

    ]


    try:

        subprocess.Popen(
            command
        )

        print()
        print(
            f"✅ 已启动："
            f"{shop['name']}"
        )

        print(
            f"Chrome 调试端口："
            f"{port}"
        )

        print(
            f"独立用户目录："
            f"{profile_dir}"
        )

        return True

    except Exception as e:

        print(
            f"❌ Chrome启动失败："
            f"{e}"
        )

        return False


# ============================================================
# 新增店铺
# ============================================================

def add_shop(
    config
):

    shops = config[
        "shops"
    ]


    print()
    print(
        "=" * 68
    )

    print(
        "新增店铺向导"
    )

    print(
        "=" * 68
    )


    name = input(
        "\n请输入新店铺名称："
    ).strip()


    if not name:

        print(
            "❌ 店铺名称不能为空"
        )

        return


    # ========================================================
    # 检查重名
    # ========================================================

    for shop in shops:

        if (
            str(
                shop.get(
                    "name",
                    ""
                )
            ).strip()
            ==
            name
        ):

            print()
            print(
                f"❌ 店铺【{name}】"
                "已经存在。"
            )

            return


    port = next_port(
        shops
    )


    print()
    print(
        f"自动分配 Chrome 端口："
        f"{port}"
    )


    shop = build_shop_config(
        name,
        port
    )


    # ========================================================
    # 先启动 Chrome
    # ========================================================

    success = launch_shop_chrome(
        shop
    )


    if not success:

        return


    print()
    print(
        "=" * 68
    )

    print(
        "接下来请在刚刚打开的 Chrome 中："
    )

    print()
    print(
        "1. 登录新店铺对应的淘宝 / 千牛账号"
    )

    print(
        "2. 确认能够正常打开生意参谋"
    )

    print(
        "3. 确认能够正常打开万相台"
    )

    print()
    print(
        "没开关键词推广也没关系。"
    )

    print(
        "没开智能托管也没关系。"
    )

    print(
        "=" * 68
    )


    input(
        "\n登录完成后按 Enter 继续..."
    )


    # ========================================================
    # 加入配置
    # ========================================================

    shops.append(
        shop
    )


    save_config(
        config
    )


    print()
    print(
        "✅ 店铺添加完成"
    )

    print(
        f"店铺：{name}"
    )

    print(
        f"端口：{port}"
    )

    print(
        f"配置文件：{SHOPS_FILE}"
    )

    print()
    print(
        "下一步可以直接运行："
    )

    print()
    print(
        "python qianniu_profit_crawler.py"
    )


# ============================================================
# 启动某一家店铺 Chrome
# ============================================================

def start_one_shop(
    config
):

    shops = config[
        "shops"
    ]


    if not shops:

        print(
            "暂无店铺"
        )

        return


    print_shops(
        shops
    )


    raw = input(
        "请输入要启动的店铺编号："
    ).strip()


    try:

        index = int(
            raw
        ) - 1

    except Exception:

        print(
            "❌ 编号错误"
        )

        return


    if (
        index < 0
        or
        index >= len(
            shops
        )
    ):

        print(
            "❌ 编号不存在"
        )

        return


    launch_shop_chrome(
        shops[
            index
        ]
    )


# ============================================================
# 启动所有启用店铺
# ============================================================

def start_all_shops(
    config
):

    shops = config[
        "shops"
    ]


    enabled_shops = [

        shop

        for shop in shops

        if shop.get(
            "enabled",
            True
        )

    ]


    if not enabled_shops:

        print(
            "没有启用的店铺"
        )

        return


    print()
    print(
        f"准备启动 "
        f"{len(enabled_shops)} "
        "家店铺..."
    )


    for shop in enabled_shops:

        launch_shop_chrome(
            shop
        )


    print()
    print(
        "✅ 启用店铺 Chrome "
        "已全部启动"
    )


# ============================================================
# 启用 / 停用
# ============================================================

def toggle_shop(
    config
):

    shops = config[
        "shops"
    ]


    if not shops:

        print(
            "暂无店铺"
        )

        return


    print_shops(
        shops
    )


    raw = input(
        "请输入店铺编号："
    ).strip()


    try:

        index = int(
            raw
        ) - 1

    except Exception:

        print(
            "编号错误"
        )

        return


    if (
        index < 0
        or
        index >= len(
            shops
        )
    ):

        print(
            "编号不存在"
        )

        return


    shop = shops[
        index
    ]


    current = shop.get(
        "enabled",
        True
    )


    shop[
        "enabled"
    ] = not current


    save_config(
        config
    )


    new_status = (
        "启用"
        if shop["enabled"]
        else
        "停用"
    )


    print()
    print(
        f"✅ {shop['name']} "
        f"已{new_status}"
    )


# ============================================================
# 删除店铺
# ============================================================

def delete_shop(
    config
):

    shops = config[
        "shops"
    ]


    if not shops:

        print(
            "暂无店铺"
        )

        return


    print_shops(
        shops
    )


    raw = input(
        "请输入要删除的店铺编号："
    ).strip()


    try:

        index = int(
            raw
        ) - 1

    except Exception:

        print(
            "编号错误"
        )

        return


    if (
        index < 0
        or
        index >= len(
            shops
        )
    ):

        print(
            "编号不存在"
        )

        return


    shop = shops[
        index
    ]


    print()
    print(
        f"准备删除："
        f"{shop['name']}"
    )

    print(
        "注意：只删除 shops.json 中的配置。"
    )

    print(
        "不会删除历史数据、成本表或 Chrome 登录目录。"
    )


    confirm = input(
        "\n确认删除？输入 YES："
    ).strip()


    if confirm != "YES":

        print(
            "已取消"
        )

        return


    shops.pop(
        index
    )


    save_config(
        config
    )


    print(
        f"✅ 已删除店铺配置："
        f"{shop['name']}"
    )


# ============================================================
# 刷新所有店铺 URL 日期
# ============================================================

def refresh_urls(
    config
):

    shops = config[
        "shops"
    ]


    for shop in shops:

        shop[
            "sycm_url"
        ] = make_sycm_url()


        shop[
            "site_url"
        ] = make_site_url()


        shop[
            "search_url"
        ] = make_search_url()


        # ====================================================
        # 智能托管：
        # 如果老配置里有明确 onesite-detail + campaignId，
        # 暂时不强制覆盖。
        #
        # 防止影响现在已经跑通的老店。
        # ====================================================

        old_smart = str(
            shop.get(
                "smart_site_url",
                ""
            )
        )


        if not old_smart:

            shop[
                "smart_site_url"
            ] = make_smart_site_url()


    save_config(
        config
    )


    print()
    print(
        f"✅ URL 日期已刷新为："
        f"{today_string()}"
    )


# ============================================================
# 主菜单
# ============================================================

def main():

    while True:

        config = load_config()


        if config is None:

            return


        shops = config[
            "shops"
        ]


        print()
        print()
        print(
            "=" * 68
        )

        print(
            "千牛多店铺管理器 V1.0"
        )

        print(
            "=" * 68
        )

        print(
            f"当前日期："
            f"{today_string()}"
        )

        print(
            f"当前店铺："
            f"{len(shops)} 家"
        )

        print()
        print(
            "1. 查看店铺"
        )

        print(
            "2. 新增店铺"
        )

        print(
            "3. 启动一家店铺 Chrome"
        )

        print(
            "4. 启动全部启用店铺 Chrome"
        )

        print(
            "5. 启用 / 停用店铺"
        )

        print(
            "6. 刷新所有店铺当天 URL"
        )

        print(
            "7. 删除店铺配置"
        )

        print(
            "0. 退出"
        )

        print(
            "=" * 68
        )


        choice = input(
            "\n请选择："
        ).strip()


        if choice == "1":

            print_shops(
                shops
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "2":

            add_shop(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "3":

            start_one_shop(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "4":

            start_all_shops(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "5":

            toggle_shop(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "6":

            refresh_urls(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "7":

            delete_shop(
                config
            )

            input(
                "\n按 Enter 返回..."
            )


        elif choice == "0":

            print()
            print(
                "已退出。"
            )

            break


        else:

            print(
                "\n请输入正确编号。"
            )


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":

    main()