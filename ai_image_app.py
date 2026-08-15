from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st


st.set_page_config(page_title="AI 生图", page_icon="🎨", layout="wide")

MODEL_NAME = "gpt-image-2"
ASPECT_RATIOS = ("1024x1024", "1024x1536", "1536x1024")


def get_data_dir() -> Path:
    return Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))


def get_history_path() -> Path:
    return get_data_dir() / "ai-image-history.jsonl"


def api_base_url() -> str:
    return os.environ.get("GRSAI_API_BASE_URL", "https://grsai.dakka.com.cn").rstrip("/")


def api_key() -> str:
    return os.environ.get("GRSAI_API_KEY", "").strip()


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    key = api_key()
    if not key:
        raise RuntimeError("服务器尚未配置 GRS AI API Key")

    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_base_url() + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"接口返回错误 {exc.code}：{body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"接口连接失败：{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"接口返回不是 JSON：{raw[:500]}") from exc


def extract_image_url(response: dict[str, Any]) -> str | None:
    results = response.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("image_url")
            return str(url) if url else None
        if isinstance(first, str):
            return first
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("image_url")
            return str(url) if url else None
    return None


def result_id(response: dict[str, Any]) -> str | None:
    for key in ("id", "task_id", "taskId"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def generate_image(prompt: str, aspect_ratio: str) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "images": [],
        "aspectRatio": aspect_ratio,
        "replyType": "json",
    }
    response = api_request("POST", "/v1/api/generate", payload)
    url = extract_image_url(response)
    if url:
        return url, response

    task_id = result_id(response)
    if not task_id:
        raise RuntimeError(f"接口未返回图片 URL 或任务 ID：{response}")

    for _ in range(24):
        time.sleep(5)
        result = api_request("GET", f"/v1/api/result?id={task_id}")
        url = extract_image_url(result)
        if url:
            return url, result
        status = str(result.get("status", "")).lower()
        if status in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"生图任务失败：{result}")
    raise RuntimeError("生图任务超时，请稍后再试")


def save_history(prompt: str, aspect_ratio: str, image_url: str) -> None:
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_NAME,
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "image_url": image_url,
    }
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history(limit: int = 20) -> list[dict[str, Any]]:
    history_path = get_history_path()
    if not history_path.exists():
        return []
    rows = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows[-limit:]))


with st.sidebar:
    st.header("AI 生图")
    st.link_button(
        "返回主看板",
        os.environ.get("TMALL_DASHBOARD_URL", "http://150.158.133.102/"),
        width="stretch",
    )
    st.link_button(
        "保本 ROI 计算器",
        os.environ.get("TMALL_ROI_URL", "http://150.158.133.102/roi/"),
        width="stretch",
    )
    st.link_button(
        "达人管理",
        os.environ.get("TMALL_KOC_URL", "http://150.158.133.102/koc/"),
        width="stretch",
    )

st.title("AI 生图")

if not api_key():
    st.warning("服务器尚未配置 GRS AI API Key。配置后即可生成图片。")

with st.form("ai_image_form"):
    prompt = st.text_area(
        "生图提示词",
        height=180,
        placeholder="例如：天猫电商商品主图，干净白底，高级质感，突出产品卖点...",
    )
    col1, col2 = st.columns([1, 2])
    with col1:
        aspect_ratio = st.selectbox("图片尺寸", ASPECT_RATIOS, index=0)
    with col2:
        st.caption(f"模型：{MODEL_NAME}；接口节点：{api_base_url()}")
    submitted = st.form_submit_button("生成图片", type="primary", width="stretch")

if submitted:
    if not prompt.strip():
        st.error("请先填写生图提示词。")
    else:
        try:
            with st.spinner("正在生成图片，可能需要几十秒..."):
                image_url, raw_response = generate_image(prompt.strip(), aspect_ratio)
                save_history(prompt.strip(), aspect_ratio, image_url)
        except Exception as exc:
            st.error(f"生成失败：{exc}")
        else:
            st.success("生成成功")
            st.image(image_url, width=520)
            st.link_button("打开原图", image_url, width="stretch")
            with st.expander("接口返回"):
                st.json(raw_response)

history = load_history()
if history:
    st.subheader("最近生成")
    for item in history:
        with st.container(border=True):
            st.caption(f"{item.get('created_at', '')} ｜ {item.get('aspect_ratio', '')}")
            st.write(item.get("prompt", ""))
            image_url = item.get("image_url")
            if image_url:
                st.image(image_url, width=320)
                st.link_button("打开图片", image_url)
