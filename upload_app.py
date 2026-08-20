from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from data_loader import STORE_FILE_PATTERNS, find_store_workbooks
from upload_manager import install_uploaded_workbooks


st.set_page_config(page_title="天猫日报上传", page_icon="📤", layout="wide")


data_dir = Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))
dashboard_url = os.environ.get("TMALL_DASHBOARD_URL", "http://150.158.133.102:8080")

st.title("📤 财务日报上传")
st.caption("选择对应店铺文件。系统会先校验文件名与店铺是否匹配，成功后才替换服务器正式报表。")

st.info("当前统计范围从 2026-07-01 开始；支持 .xls、.xlsx、.xlsm，单个文件最大 25MB。")

try:
    current_sources = find_store_workbooks()
except Exception:
    current_sources = {}

current_rows = []
for store in STORE_FILE_PATTERNS:
    path = current_sources.get(store)
    current_rows.append(
        {
            "店铺": store,
            "当前服务器文件": path.name if path else "未上传",
            "更新时间": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if path
            else "—",
        }
    )
st.dataframe(pd.DataFrame(current_rows), width="stretch", hide_index=True)

with st.form("workbook_uploads", clear_on_submit=False):
    st.subheader("选择要更新的日报")
    columns = st.columns(2)
    selected_files = {}
    for index, store in enumerate(STORE_FILE_PATTERNS):
        with columns[index % 2]:
            selected_files[store] = st.file_uploader(
                f"{store}日报",
                type=["xls", "xlsx", "xlsm"],
                key=f"upload_{store}",
                help="文件名需要能看出对应店铺，系统会做模糊匹配，防止选错店铺。",
            )
    submitted = st.form_submit_button("校验并更新服务器报表", type="primary", width="stretch")

if submitted:
    uploads = {
        store: (uploaded.name, uploaded.getvalue())
        for store, uploaded in selected_files.items()
        if uploaded is not None
    }
    try:
        with st.spinner("正在解析日期 Sheet、校验列结构并聚合数据…"):
            results = install_uploaded_workbooks(uploads, data_dir)
    except Exception as exc:
        st.error(f"上传未生效：{exc}")
    else:
        st.success("上传成功。旧文件已归档，主看板刷新后会自动读取新文件。")
        result_rows = [
            {
                "店铺": result.store,
                "保存文件": result.saved_name,
                "起始日期": result.start_date,
                "截止日期": result.end_date,
                "日期数": result.date_sheets,
                "商品数": result.products,
                "销量": result.sales,
                "订单量": result.orders,
                "盈亏": result.profit,
            }
            for result in results
        ]
        st.dataframe(pd.DataFrame(result_rows), width="stretch", hide_index=True)
        st.link_button("打开主看板", dashboard_url, width="stretch")

with st.expander("上传规则与数据安全"):
    st.markdown(
        """
- 可以只上传一家，也可以一次上传多家；未选择的店铺不会改变。
- 文件名会与选择的店铺做模糊匹配；明显选错店铺时不会替换正式数据。
- 所有文件会先在临时目录完整解析，任一文件校验失败时不会替换正式数据。
- 校验通过后使用原子替换；旧报表保存在服务器 `data/archive/店铺/` 目录。
- 上传内容不会进入 Git 仓库，Git 只保存程序代码。
- 主看板刷新后按文件内容指纹自动重建缓存；文件不变时继续命中磁盘缓存。
"""
    )
