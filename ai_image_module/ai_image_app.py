from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from ui_helpers import dashboard_url, koc_url, roi_url, sidebar_link


st.set_page_config(page_title="AI 生图", page_icon="🎨", layout="wide")

MODEL_NAME = "gpt-image-2"
ASPECT_RATIOS = ("1024x1024", "1024x1536", "1536x1024")
REFERENCE_IMAGE_TYPES = ("jpg", "jpeg", "png", "webp")
REFERENCE_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_BATCH_IMAGES = 12
GENERATE_REQUEST_TIMEOUT_SECONDS = 240
RESULT_REQUEST_TIMEOUT_SECONDS = 90
RESULT_POLL_SECONDS = 5
RESULT_MAX_POLLS = 48


@dataclass(frozen=True)
class AssetRequest:
    category: str
    title: str
    aspect_ratio: str
    prompt: str


ASSET_TYPE_LABELS = {
    "main": "主图",
    "detail": "详情页组",
    "ad": "广告素材",
}

STYLE_PRESETS = {
    "干净白底电商": "干净白底，主体居中，产品边缘清晰，质感真实，适合天猫主图",
    "居家生活场景": "自然居家场景，柔和日光，真实桌面或收纳环境，氛围温暖但产品清楚",
    "INS 通透风": "INS 风格布景，通透明亮，浅色道具，午后自然光，画面高级干净",
    "高点击广告风": "强视觉冲击，清晰焦点，明快配色，适合信息流广告素材",
}


def get_data_dir() -> Path:
    return Path(os.environ.get("TMALL_DATA_DIR", Path(__file__).resolve().parent / "data"))


def get_history_path() -> Path:
    return get_data_dir() / "ai-image-history.jsonl"


def get_history_image_dir() -> Path:
    return get_data_dir() / "ai-images"


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


def api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = GENERATE_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
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
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"接口返回错误 {exc.code}：{body[:500]}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError("接口响应超时，请稍后重试；如果连续出现，建议先把生成数量调到 1-2 张") from exc
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


def image_extension(content_type: str) -> str:
    extension = mimetypes.guess_extension(content_type) or ".png"
    return ".jpg" if extension == ".jpe" else extension


def image_download_name(content_type: str, prefix: str = "grsai") -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{timestamp}{image_extension(content_type)}"


def save_history_image(image_url: str, created_at: datetime) -> tuple[str | None, str | None]:
    try:
        image_bytes, content_type = fetch_image_for_download(image_url)
    except Exception:
        return None, None
    image_dir = get_history_image_dir() / created_at.strftime("%Y%m%d")
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / f"{created_at.strftime('%H%M%S')}-{abs(hash(image_url)) % 1000000:06d}{image_extension(content_type)}"
    image_path.write_bytes(image_bytes)
    return str(image_path), content_type


def reference_image_data_url(uploaded_image: Any) -> str:
    image_bytes = uploaded_image.getvalue()
    if not image_bytes:
        raise RuntimeError("上传的参考图片内容为空")
    if len(image_bytes) > MAX_REFERENCE_IMAGE_BYTES:
        raise RuntimeError(f"参考图片 {uploaded_image.name} 不能超过 10 MB")

    content_type = str(getattr(uploaded_image, "type", "")).lower()
    if content_type not in REFERENCE_IMAGE_MIME_TYPES:
        raise RuntimeError("参考图片仅支持 JPG、PNG 或 WEBP 格式")
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{encoded_image}"


def extract_image_url(response: dict[str, Any]) -> str | None:
    for key in ("url", "image_url"):
        value = response.get(key)
        if value:
            return str(value)
    for collection_key in ("results", "data"):
        results = response.get(collection_key)
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                url = first.get("url") or first.get("image_url")
                return str(url) if url else None
            if isinstance(first, str):
                return first
    return None


def result_id(response: dict[str, Any]) -> str | None:
    for key in ("id", "task_id", "taskId"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def generate_image(prompt: str, aspect_ratio: str, reference_images: list[str] | None = None) -> str:
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "images": reference_images or [],
        "aspectRatio": aspect_ratio,
        "replyType": "json",
    }
    response = api_request("POST", "/v1/api/generate", payload, timeout_seconds=GENERATE_REQUEST_TIMEOUT_SECONDS)
    url = extract_image_url(response)
    if url:
        return url

    task_id = result_id(response)
    if not task_id:
        raise RuntimeError(f"接口未返回图片 URL 或任务 ID：{response}")

    for _ in range(RESULT_MAX_POLLS):
        time.sleep(RESULT_POLL_SECONDS)
        result = api_request("GET", f"/v1/api/result?id={task_id}", timeout_seconds=RESULT_REQUEST_TIMEOUT_SECONDS)
        url = extract_image_url(result)
        if url:
            return url
        status = str(result.get("status", "")).lower()
        if status in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"生图任务失败：{result}")
    raise RuntimeError("生图任务超时，请稍后再试")


def save_history(
    prompt: str,
    aspect_ratio: str,
    image_url: str,
    has_reference: bool,
    asset_type: str = "single",
    title: str = "单张生图",
    batch_id: str | None = None,
) -> dict[str, Any]:
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    local_image_path, image_mime = save_history_image(image_url, created_at)
    record = {
        "created_at": created_at.isoformat(),
        "model": MODEL_NAME,
        "asset_type": asset_type,
        "title": title,
        "batch_id": batch_id,
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "image_url": image_url,
        "local_image_path": local_image_path,
        "image_mime": image_mime,
        "has_reference": has_reference,
    }
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load_history(limit: int = 24) -> list[dict[str, Any]]:
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


def build_asset_requests(
    product_name: str,
    selling_points: str,
    audience: str,
    style_text: str,
    extra_prompt: str,
    main_count: int,
    detail_count: int,
    ad_count: int,
) -> list[AssetRequest]:
    base = "\n".join(
        part
        for part in [
            f"产品：{product_name.strip()}",
            f"核心卖点：{selling_points.strip()}",
            f"目标人群/使用场景：{audience.strip()}",
            f"整体风格：{style_text.strip()}",
            "要求：画面真实、商品主体准确、质感清楚、适合天猫/淘宝电商使用。不要生成错误文字、乱码、水印、品牌 Logo 或夸张变形。",
            extra_prompt.strip(),
        ]
        if part.strip()
    )
    detail_themes = [
        "产品整体展示，突出材质、形态和高级感",
        "使用场景展示，体现实际用途和生活氛围",
        "卖点特写展示，突出结构、细节、容量或功能",
        "组合陈列展示，适合详情页中的场景过渡图",
        "问题解决场景，体现使用前后的便利感",
        "简洁收尾图，干净背景，适合作为详情页最后一屏",
    ]
    ad_themes = [
        "信息流广告素材，强视觉冲击，主体醒目，留出文案空间",
        "促销广告素材，明快背景，突出商品价值感，画面利落",
        "场景种草广告素材，生活化构图，适合小红书或达人投放",
        "搜索广告素材，产品清晰，占比高，点击感强",
    ]
    requests: list[AssetRequest] = []
    for index in range(main_count):
        requests.append(
            AssetRequest(
                "main",
                f"主图 {index + 1}",
                "1024x1024",
                f"{base}\n本张任务：生成电商主图第 {index + 1} 张。主体完整居中，背景干净，卖点通过画面表达，不要添加文字。",
            )
        )
    for index in range(detail_count):
        theme = detail_themes[index % len(detail_themes)]
        requests.append(
            AssetRequest(
                "detail",
                f"详情页图 {index + 1}",
                "1024x1536",
                f"{base}\n本张任务：生成详情页竖图第 {index + 1} 张。主题：{theme}。构图适合详情页连续展示，不要添加文字。",
            )
        )
    for index in range(ad_count):
        theme = ad_themes[index % len(ad_themes)]
        requests.append(
            AssetRequest(
                "ad",
                f"广告图 {index + 1}",
                "1536x1024",
                f"{base}\n本张任务：生成横版广告素材第 {index + 1} 张。主题：{theme}。画面有点击吸引力，预留干净区域方便后期加字。",
            )
        )
    return requests


def make_zip(records: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(records, 1):
            image_bytes: bytes | None = None
            content_type = item.get("image_mime") or "image/png"
            local_path = item.get("local_image_path")
            if local_path and Path(str(local_path)).exists():
                image_bytes = Path(str(local_path)).read_bytes()
            elif item.get("image_url"):
                try:
                    image_bytes, content_type = fetch_image_for_download(str(item["image_url"]))
                except Exception:
                    image_bytes = None
            if not image_bytes:
                continue
            asset_type = item.get("asset_type") or "image"
            title = str(item.get("title") or f"image-{index}").replace(" ", "-")
            archive.writestr(f"{index:02d}-{asset_type}-{title}{image_extension(content_type)}", image_bytes)
    return buffer.getvalue()


def render_record_card(item: dict[str, Any], image_width: int = 300, key_prefix: str = "image") -> None:
    asset_type = str(item.get("asset_type") or "single")
    label = ASSET_TYPE_LABELS.get(asset_type, "单张")
    reference_label = "参考图" if item.get("has_reference") else "纯文字"
    st.caption(f"{item.get('title') or label} | {item.get('aspect_ratio', '')} | {reference_label}")
    if item.get("prompt"):
        with st.expander("查看提示词"):
            st.write(item.get("prompt", ""))
    local_image_path = item.get("local_image_path")
    image_url = item.get("image_url")
    if local_image_path and Path(str(local_image_path)).exists():
        path = Path(str(local_image_path))
        st.image(str(path), width=image_width)
        st.download_button(
            "下载",
            data=path.read_bytes(),
            file_name=path.name,
            mime=item.get("image_mime") or "image/png",
            width="stretch",
            on_click="ignore",
            key=f"download_{key_prefix}_{path}_{item.get('created_at', '')}",
        )
    elif image_url:
        st.image(image_url, width=image_width)
        st.link_button("打开图片", image_url, width="stretch")


with st.sidebar:
    st.header("AI 生图")
    sidebar_link("返回主看板", dashboard_url())
    sidebar_link("投产计算器", roi_url())
    sidebar_link("达人管理", koc_url())

title_col, credits_col = st.columns([3, 1], vertical_alignment="center")
with title_col:
    st.title("AI 商品素材一键生成")
    st.caption("一次生成主图、详情页组和广告素材图，适合先快速出一版电商素材。")
with credits_col:
    try:
        credits = get_account_credits()
    except Exception:
        st.metric("剩余积分", "暂不可用")
    else:
        credits_text = f"{credits:,.0f}" if float(credits).is_integer() else f"{credits:,.2f}"
        st.metric("剩余积分", credits_text)

if not api_key():
    st.warning("服务器尚未配置 GRS AI API Key。配置后即可生成图片。")

input_col, output_col = st.columns([0.9, 1.4], gap="large")

with input_col:
    st.subheader("填写功能区")
    with st.form("ai_batch_form"):
        product_name = st.text_input("商品名称", placeholder="例如：玩具收纳架 / 不锈钢勺子 / 浴室置物架")
        selling_points = st.text_area(
            "核心卖点",
            height=120,
            placeholder="例如：大容量、稳固承重、圆角不刮手、免安装、适合儿童房收纳",
        )
        audience = st.text_area(
            "人群和场景",
            height=105,
            placeholder="例如：年轻家庭、儿童房、客厅桌面、租房收纳、礼品场景",
        )
        style_name = st.selectbox("画面风格", list(STYLE_PRESETS.keys()), index=1)

        uploaded_references = st.file_uploader(
            "参考图片（可多选，可选）",
            type=REFERENCE_IMAGE_TYPES,
            accept_multiple_files=True,
            help="建议上传商品原图、白底图或现有主图。单张最大 10 MB。",
        )
        if uploaded_references:
            preview_cols = st.columns(2)
            for index, uploaded in enumerate(uploaded_references[:4]):
                with preview_cols[index % 2]:
                    st.image(uploaded, caption=uploaded.name, width=150)

        st.markdown("**生成数量**")
        count_cols = st.columns(3)
        with count_cols[0]:
            main_count = st.number_input("主图", min_value=0, max_value=4, value=2, step=1)
        with count_cols[1]:
            detail_count = st.number_input("详情页组", min_value=0, max_value=8, value=4, step=1)
        with count_cols[2]:
            ad_count = st.number_input("广告素材", min_value=0, max_value=6, value=2, step=1)

        extra_prompt = st.text_area(
            "补充要求（可选）",
            height=90,
            placeholder="例如：保留原商品外观，不改变颜色；背景更明亮；不要出现人物手部；适合天猫首图审核。",
        )
        submitted = st.form_submit_button("一键生成整套素材", type="primary", width="stretch")

with output_col:
    st.subheader("生成图片区")
    if submitted:
        total_count = int(main_count) + int(detail_count) + int(ad_count)
        if not product_name.strip():
            st.error("请先填写商品名称。")
        elif not selling_points.strip():
            st.error("请先填写核心卖点。")
        elif total_count <= 0:
            st.error("请至少选择生成 1 张图片。")
        elif total_count > MAX_BATCH_IMAGES:
            st.error(f"一次最多生成 {MAX_BATCH_IMAGES} 张，请减少数量。")
        else:
            batch_id = datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                reference_images = [reference_image_data_url(image) for image in uploaded_references]
                requests = build_asset_requests(
                    product_name=product_name,
                    selling_points=selling_points,
                    audience=audience,
                    style_text=STYLE_PRESETS[style_name],
                    extra_prompt=extra_prompt,
                    main_count=int(main_count),
                    detail_count=int(detail_count),
                    ad_count=int(ad_count),
                )
                generated_records = []
                progress = st.progress(0, text="准备生成素材...")
                for index, request in enumerate(requests, 1):
                    progress.progress(
                        (index - 1) / len(requests),
                        text=f"正在生成 {request.title}（{index}/{len(requests)}）",
                    )
                    image_url = generate_image(request.prompt, request.aspect_ratio, reference_images)
                    generated_records.append(
                        save_history(
                            prompt=request.prompt,
                            aspect_ratio=request.aspect_ratio,
                            image_url=image_url,
                            has_reference=bool(reference_images),
                            asset_type=request.category,
                            title=request.title,
                            batch_id=batch_id,
                        )
                    )
                progress.progress(1.0, text="素材生成完成")
            except Exception as exc:
                st.error(f"生成失败：{exc}")
            else:
                st.session_state["latest_generated_batch"] = generated_records
                st.success(f"已生成 {len(generated_records)} 张素材")

    latest_batch = st.session_state.get("latest_generated_batch") or []
    if latest_batch:
        zip_bytes = make_zip(latest_batch)
        if zip_bytes:
            st.download_button(
                "打包下载本次全部图片",
                data=zip_bytes,
                file_name=f"ai-product-assets-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
                mime="application/zip",
                type="primary",
                width="stretch",
                on_click="ignore",
            )
        for category, label in ASSET_TYPE_LABELS.items():
            group_items = [item for item in latest_batch if item.get("asset_type") == category]
            if not group_items:
                continue
            st.markdown(f"**{label}**")
            column_count = min(len(group_items), 3)
            columns = st.columns([1] * column_count)
            for index, item in enumerate(group_items):
                with columns[index % len(columns)]:
                    render_record_card(item, image_width=240, key_prefix=f"latest_{category}_{index}")
    else:
        st.info("填写左侧信息后，点击生成，图片会在这里按素材类型展示。")

    history = load_history()
    if history:
        with st.expander("最近生成", expanded=not latest_batch):
            columns = st.columns(2)
            for index, item in enumerate(history):
                with columns[index % 2]:
                    with st.container(border=True):
                        render_record_card(item, image_width=220, key_prefix=f"history_{index}")
