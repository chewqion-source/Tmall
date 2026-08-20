from __future__ import annotations

import os

import streamlit as st


SIDEBAR_BUTTON_WIDTH = 168


def sidebar_link(label: str, url: str) -> None:
    st.link_button(label, url, width=SIDEBAR_BUTTON_WIDTH)


def dashboard_url() -> str:
    return os.environ.get("TMALL_DASHBOARD_URL", "http://150.158.133.102/")


def roi_url() -> str:
    return os.environ.get("TMALL_ROI_URL", "http://150.158.133.102/roi/")


def koc_url() -> str:
    return os.environ.get("TMALL_KOC_URL", "http://150.158.133.102/koc/")


def upload_url() -> str:
    return os.environ.get("TMALL_UPLOAD_URL", "http://150.158.133.102:8080/upload/")


def ai_image_url() -> str:
    return os.environ.get("TMALL_AI_IMAGE_URL", "http://150.158.133.102/ai-image/")
