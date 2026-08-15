from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
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
    return os.environ.get("GRSAI_API_KEY", "").strip().strip('"').strip("'")


def validate_api_key(key: str) -> None:
    if not key:
        raise RuntimeError("服务器尚未配置 GRS AI API Key")
    if not key.isascii():
        raise RuntimeError("GRS AI API Key 里包含中文或特殊字符，请在服务器重新填写纯英文数字的密钥")
    if any(char.isspace() for char in key):
        raise RuntimeError("GRS AI API Key 里包含空格或换行，请在服务器重新填写密钥")


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    key = api_key()
    validate_api_key(key)

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
    except UnicodeEncodeError as exc:
        raise RuntimeError("接口请求编码失败，请检查 API Key 是否填成了中文说明文字") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"接口连接失败：{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"接口返回不是 JSON：{raw[:500]}") from exc


@st.cache_data(ttl=60, show_spinner=False)
def get_account_credits() -> int | float:
    key = api_key()
    validate_api_key(key)
    query = urllib.parse.urlencode({"apikey": key})
    request = urllib.request.Request(
        f"{api_base_url()}/client/common/getCredits?{query}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError("暂时无法查询积分") from exc

    try:
        result = json.loads(raw)
        credits = result["data"]["credits"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("积分接口返回格式异常") from exc
    if not isinstance(credits, (int, float)):
        raise RuntimeError("积分接口未返回有效余额")
    return credits


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_image_for_download(image_url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(image_url, headers={"Accept": "image/*"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            image_bytes = response.read()
            content_type = response.headers.get_content_type()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError("图片下载准备失败") from exc
    if not image_bytes:
        raise RuntimeError("图片内容为空")
    return image_bytes, content_type


def image_download_name(content_type: str) -> str:
    extension = mimetypes.guess_extension(content_type) or ".png"
    if extension == ".jpe":
        extension = ".jpg"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"grsai-{timestamp}{extension}"


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


def generate_image(prompt: str, aspect_ratio: str) -> str:
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
        return url

    task_id = result_id(response)
    if not task_id:
        raise RuntimeError(f"接口未返回图片 URL 或任务 ID：{response}")

    for _ in range(24):
        time.sleep(5)
        result = api_request("GET", f"/v1/api/result?id={task_id}")
        url = extract_image_url(result)
        if url:
            return url
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

title_col, credits_col = st.columns([3, 1], vertical_alignment="center")
with title_col:
    st.title("AI 生图")
with credits_col:
    try:
        credits = get_account_credits()
    except Exception:
        st.metric("GRS AI 剩余积分", "暂不可用")
    else:
        credits_text = f"{credits:,.0f}" if float(credits).is_integer() else f"{credits:,.2f}"
        st.metric("GRS AI 剩余积分", credits_text)

if not api_key():
    st.warning("服务器尚未配置 GRS AI API Key。配置后即可生成图片。")

with st.form("ai_image_form"):
    prompt = st.text_area(
        "生图提示词",
        height=180,
        placeholder="例如：天猫电商商品主图，干净白底，高级质感，突出产品卖点...",
    )
    aspect_ratio = st.selectbox("图片尺寸", ASPECT_RATIOS, index=0)
    submitted = st.form_submit_button("生成图片", type="primary", width="stretch")

if submitted:
    if not prompt.strip():
        st.error("请先填写生图提示词。")
    else:
        try:
            with st.spinner("正在生成图片，可能需要几十秒..."):
                image_url = generate_image(prompt.strip(), aspect_ratio)
                save_history(prompt.strip(), aspect_ratio, image_url)
        except Exception as exc:
            st.error(f"生成失败：{exc}")
        else:
            st.session_state["latest_generated_image"] = {
                "url": image_url,
                "prompt": prompt.strip(),
                "aspect_ratio": aspect_ratio,
            }

latest_image = st.session_state.get("latest_generated_image")
if latest_image:
    st.success("生成成功")
    st.image(latest_image["url"], width=520)
    try:
        image_bytes, image_mime = fetch_image_for_download(latest_image["url"])
    except Exception as exc:
        st.warning(f"暂时无法准备下载：{exc}")
    else:
        st.download_button(
            "下载图片",
            data=image_bytes,
            file_name=image_download_name(image_mime),
            mime=image_mime,
            type="primary",
            width="stretch",
            on_click="ignore",
        )

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
