#!/usr/bin/env python3
"""Oh My Tools — Web 界面主入口"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 修复 Python 3.13+ Windows 上 Proactor 事件循环的 ConnectionResetError
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        # Python 3.13+ 移除了 WindowsSelectorEventLoopPolicy
        # 抑制 Proactor 连接关闭时的 ConnectionResetError
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _patched_call_connection_lost(self):
            try:
                _orig_call_connection_lost(self)
            except ConnectionResetError:
                pass

        _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost

import gradio as gr

from tools.crop_vp8 import build_ui as build_crop_vp8_ui
from tools.webm_to_mov import build_ui as build_webm_to_mov_ui

CSS = """
html, body {
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    height: 100% !important;
}
.gradio-container {
    max-height: 100vh !important;
    overflow: hidden !important;
}
.main-row {
    height: 100vh !important;
    overflow: hidden !important;
    align-items: flex-start !important;
}
.sidebar {
    background: #f8f9fa !important;
    height: 100vh !important;
    overflow-y: auto !important;
    padding: 0 !important;
    border-right: 1px solid #dee2e6 !important;
}
.sidebar-title {
    color: #212529 !important;
    font-size: 18px !important;
    font-weight: 700 !important;
    padding: 20px 16px 12px !important;
    margin: 0 !important;
    border-bottom: 1px solid #dee2e6 !important;
}
.sidebar-subtitle {
    color: #6c757d !important;
    font-size: 11px !important;
    padding: 4px 16px 16px !important;
    margin: 0 !important;
    text-transform: uppercase !important;
    letter-spacing: 1px !important;
}
.tool-btn {
    display: block !important;
    width: 100% !important;
    text-align: left !important;
    padding: 10px 16px !important;
    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    color: #495057 !important;
    font-size: 14px !important;
    cursor: pointer !important;
    transition: background 0.15s !important;
    margin: 0 !important;
}
.tool-btn:hover {
    background: #e9ecef !important;
    color: #212529 !important;
}
.tool-btn.active {
    background: #e7f1ff !important;
    color: #0d6efd !important;
    border-left: 3px solid #0d6efd !important;
    padding-left: 13px !important;
}
.content {
    padding: 20px !important;
    background: #ffffff !important;
    height: 100vh !important;
    overflow-y: auto !important;
}
footer {
    display: none !important;
}
video {
    display: block !important;
    margin-left: auto !important;
    margin-right: auto !important;
    max-width: 360px !important;
    max-height: 360px !important;
    object-fit: contain !important;
    background-image:
        linear-gradient(45deg, #ccc 25%, transparent 25%),
        linear-gradient(-45deg, #ccc 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #ccc 75%),
        linear-gradient(-45deg, transparent 75%, #ccc 75%);
    background-size: 20px 20px;
    background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
    background-color: #999;
}
"""


def webui_main(port=7860):
    """启动 Oh My Tools Web 界面（侧边栏 + 工具内容布局）。"""
    with gr.Blocks(title="Oh My Tools", css=CSS, theme=gr.themes.Default()) as demo:
        with gr.Row(elem_classes="main-row"):
            # === 左侧边栏 ===
            with gr.Column(scale=1, elem_classes="sidebar"):
                gr.Markdown("## 🛠 Oh My Tools", elem_classes="sidebar-title")
                gr.Markdown("工具列表", elem_classes="sidebar-subtitle")
                btn_crop_vp8 = gr.Button(
                    "🎬 裁剪 VP8 透明视频", elem_classes="tool-btn active"
                )
                btn_webm_to_mov = gr.Button(
                    "🎞 WebM → ProRes 4444 MOV", elem_classes="tool-btn"
                )

            # === 右侧内容区 ===
            with gr.Column(scale=4, elem_classes="content"):
                crop_vp8_ui = build_crop_vp8_ui()
                webm_to_mov_ui = build_webm_to_mov_ui(visible=False)

        # === 工具切换逻辑 ===
        def switch_tool(tool_name):
            """切换工具：更新所有工具 UI 的可见性和按钮样式。"""
            is_crop = tool_name == "crop_vp8"
            is_mov = tool_name == "webm_to_mov"

            # 各工具 UI 可见性
            crop_vp8_visible = gr.update(visible=is_crop)
            webm_to_mov_visible = gr.update(visible=is_mov)

            # 各按钮样式
            btn_crop_active = gr.update(
                elem_classes="tool-btn active" if is_crop else "tool-btn"
            )
            btn_mov_active = gr.update(
                elem_classes="tool-btn active" if is_mov else "tool-btn"
            )

            return [
                crop_vp8_visible, webm_to_mov_visible,
                btn_crop_active, btn_mov_active,
            ]

        btn_crop_vp8.click(
            fn=lambda: switch_tool("crop_vp8"),
            outputs=[crop_vp8_ui, webm_to_mov_ui, btn_crop_vp8, btn_webm_to_mov],
        )

        btn_webm_to_mov.click(
            fn=lambda: switch_tool("webm_to_mov"),
            outputs=[crop_vp8_ui, webm_to_mov_ui, btn_crop_vp8, btn_webm_to_mov],
        )

    demo.launch(server_name="0.0.0.0", server_port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Oh My Tools Web 界面")
    parser.add_argument("--port", type=int, default=7860, help="端口号（默认: 7860）")
    args = parser.parse_args()
    webui_main(port=args.port)