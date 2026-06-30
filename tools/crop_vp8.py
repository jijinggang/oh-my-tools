#!/usr/bin/env python3
"""裁剪 YUVA VP8 WebM 视频的透明边缘，保留音频不变。"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="裁剪 YUVA VP8 WebM 视频的透明边缘，保留音频不变。"
    )
    parser.add_argument("input", nargs="?", default=None, help="输入 YUVA VP8 WebM 视频路径")
    parser.add_argument(
        "output", nargs="?", default=None,
        help="输出 WebM 视频路径（默认：输入文件名 + _cropped.webm）",
    )
    parser.add_argument(
        "--speed", default="good", choices=("good", "best", "realtime"),
        help="编码速度预设（默认: good）",
    )
    parser.add_argument(
        "--alpha-threshold", type=int, default=0,
        help="alpha 阈值，大于此值视为不透明（默认: 0）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="全透明视频时强制输出原视频",
    )
    parser.add_argument(
        "--pre-crop", default=None,
        help="预裁剪边缘，格式: top,bottom,left,right（如 10,0,5,0）",
    )
    parser.add_argument(
        "--ffmpeg-path", default=None,
        help="ffmpeg 可执行文件路径",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="显示 ffmpeg 详细输出",
    )
    parser.add_argument(
        "--webui", action="store_true",
        help="启动 Web 界面",
    )
    parser.add_argument(
        "--port", type=int, default=7860,
        help="Web 界面端口号（默认: 7860）",
    )
    return parser.parse_args(argv)


def check_ffmpeg(ffmpeg_path=None):
    ffmpeg = ffmpeg_path or "ffmpeg"
    if ffmpeg_path:
        ffprobe = str(Path(ffmpeg_path).parent / "ffprobe")
    else:
        ffprobe = "ffprobe"

    for name, path in [("ffmpeg", ffmpeg), ("ffprobe", ffprobe)]:
        if not shutil.which(path):
            print(f"错误: 找不到 {name} ({path})", file=sys.stderr)
            sys.exit(1)
    return ffmpeg, ffprobe


def get_video_info(ffprobe_path, input_video):
    cmd = [
        ffprobe_path, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        input_video,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"错误: ffprobe 无法读取视频信息\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    fmt = data.get("format", {})
    orig_bitrate = int(fmt.get("bit_rate", 0))
    video_stream = None
    audio_stream = None

    for stream in data.get("streams", []):
        if stream["codec_type"] == "video":
            video_stream = stream
        elif stream["codec_type"] == "audio":
            audio_stream = stream

    if video_stream is None:
        print("错误: 输入文件不包含视频流", file=sys.stderr)
        sys.exit(1)

    width = video_stream["width"]
    height = video_stream["height"]
    pix_fmt = video_stream.get("pix_fmt", "")
    alpha_mode = video_stream.get("tags", {}).get("alpha_mode", "")

    fps_str = video_stream.get("r_frame_rate", "30")
    if "/" in fps_str:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    else:
        fps = float(fps_str)

    codec_name = video_stream.get("codec_name", "")
    has_audio = audio_stream is not None
    has_alpha = pix_fmt.startswith("yuva") or alpha_mode == "1"

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "pix_fmt": pix_fmt,
        "codec_name": codec_name,
        "has_audio": has_audio,
        "has_alpha": has_alpha,
        "bitrate": orig_bitrate,
        "nb_frames": int(video_stream.get("nb_frames", 0)),
    }


def analyze_alpha_bounds(ffmpeg_path, input_video, width, height, fps,
                         codec_name="", alpha_threshold=0, verbose=False):
    frame_size = width * height * 4
    decoder_cmd = [
        ffmpeg_path, "-y",
    ]
    # libvpx 解码器正确处理 VP8 alpha；原生 vp8 解码器会丢失 alpha 通道
    if codec_name == "vp8":
        decoder_cmd += ["-c:v", "libvpx"]
    decoder_cmd += [
        "-i", input_video,
        "-f", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "pipe:stdout",
    ]

    stderr_target = None if verbose else subprocess.DEVNULL
    decoder = subprocess.Popen(
        decoder_cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_target,
        bufsize=frame_size * 16,
    )

    min_x = width
    max_x = 0
    min_y = height
    max_y = 0
    frames_with_content = 0
    total_frames = 0

    try:
        while True:
            raw = decoder.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            total_frames += 1

            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
            alpha = frame[:, :, 3]

            rows, cols = np.where(alpha > alpha_threshold)

            if len(rows) > 0:
                frames_with_content += 1
                min_x = min(min_x, int(cols.min()))
                max_x = max(max_x, int(cols.max()))
                min_y = min(min_y, int(rows.min()))
                max_y = max(max_y, int(rows.max()))

            if total_frames % 100 == 0:
                print(f"\r  已分析 {total_frames} 帧...", end="", flush=True)
    finally:
        decoder.kill()
        decoder.wait()

    if total_frames > 0:
        print(f"\r  分析完成: {total_frames} 帧", flush=True)

    if frames_with_content == 0:
        return None, 0, total_frames

    return (min_x, min_y, max_x, max_y), frames_with_content, total_frames


def adjust_crop_to_even(min_x, min_y, max_x, max_y, width, height):
    crop_x = min_x if min_x % 2 == 0 else min_x - 1
    crop_x = max(0, crop_x)

    crop_y = min_y if min_y % 2 == 0 else min_y - 1
    crop_y = max(0, crop_y)

    crop_w = max_x - crop_x + 1
    if crop_w % 2 != 0:
        crop_w += 1

    crop_h = max_y - crop_y + 1
    if crop_h % 2 != 0:
        crop_h += 1

    crop_w = min(crop_w, width - crop_x)
    crop_h = min(crop_h, height - crop_y)

    return crop_x, crop_y, crop_w, crop_h


def crop_video(ffmpeg_path, input_video, output_video, crop_x, crop_y,
               crop_w, crop_h, bitrate, speed="good", verbose=False):
    crop_cmd = [
        ffmpeg_path, "-y",
        "-c:v", "libvpx",
        "-i", input_video,
        "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}",
        "-c:v", "libvpx",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", str(bitrate),
        "-deadline", speed,
        "-c:a", "copy",
        output_video,
    ]

    stderr_target = None if verbose else subprocess.DEVNULL
    stdout_target = None if verbose else subprocess.DEVNULL

    result = subprocess.run(crop_cmd, stdout=stdout_target, stderr=stderr_target)
    if result.returncode != 0:
        print("错误: ffmpeg 编码失败", file=sys.stderr)
        sys.exit(1)


def copy_video(ffmpeg_path, input_video, output_video, verbose=False):
    copy_cmd = [
        ffmpeg_path, "-y",
        "-i", input_video,
        "-c", "copy",
        output_video,
    ]

    stderr_target = None if verbose else subprocess.DEVNULL
    stdout_target = None if verbose else subprocess.DEVNULL

    result = subprocess.run(copy_cmd, stdout=stdout_target, stderr=stderr_target)
    if result.returncode != 0:
        print("错误: ffmpeg 复制失败", file=sys.stderr)
        sys.exit(1)


def apply_pre_crop(ffmpeg_path, ffprobe_path, input_video, top, bottom, left, right):
    """预裁剪视频边缘，返回临时文件路径。"""
    info = get_video_info(ffprobe_path, input_video)
    new_w = info["width"] - left - right
    new_h = info["height"] - top - bottom
    if new_w <= 0 or new_h <= 0:
        print("错误: 预裁剪后尺寸无效", file=sys.stderr)
        sys.exit(1)

    suffix = os.path.splitext(input_video)[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        output_path = f.name

    cmd = [
        ffmpeg_path, "-y",
        "-c:v", "libvpx",
        "-i", input_video,
        "-vf", f"crop={new_w}:{new_h}:{left}:{top}",
        "-c:v", "libvpx",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        print("错误: 预裁剪失败", file=sys.stderr)
        sys.exit(1)
    return output_path


BITRATE_CHOICES = [
    ("与原视频相同", "original"),
    ("500 kbps", "500000"),
    ("1000 kbps", "1000000"),
    ("2000 kbps", "2000000"),
    ("3000 kbps", "3000000"),
    ("自定义", "custom"),
]


def process_video(video_path, bitrate_choice, custom_bitrate, alpha_threshold, force,
                  pre_crop_enabled, pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right):
    """Gradio 回调：处理视频裁剪。"""
    import gradio as gr

    if video_path is None:
        gr.Warning("请先上传视频文件")
        return None, "### ⚠ 请先上传视频文件"

    ffmpeg_path, ffprobe_path = check_ffmpeg()

    try:
        info = get_video_info(ffprobe_path, video_path)
    except SystemExit:
        gr.Error("无法读取视频信息")
        return None, "### ❌ 无法读取视频信息"

    if not info["has_alpha"]:
        gr.Error("输入视频不含透明通道，无法分析透明边缘")
        return None, "### ❌ 输入视频不含透明通道，无法分析透明边缘"

    # 码率处理
    if bitrate_choice == "original":
        bitrate = info["bitrate"]
        bitrate_label = f"{bitrate / 1000:.0f} kbps (原始)"
    elif bitrate_choice == "custom":
        if custom_bitrate is None or custom_bitrate <= 0:
            gr.Warning("请输入有效的自定义码率")
            return None, "### ⚠ 请输入有效的自定义码率"
        bitrate = int(custom_bitrate * 1000)
        bitrate_label = f"{custom_bitrate:.0f} kbps (自定义)"
    else:
        bitrate = int(bitrate_choice)
        bitrate_label = f"{bitrate / 1000:.0f} kbps"

    # 预裁剪
    if pre_crop_enabled and any(v > 0 for v in (pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right)):
        pre_cropped = apply_pre_crop(
            ffmpeg_path, ffprobe_path, video_path,
            pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right,
        )
        video_path = pre_cropped
        info = get_video_info(ffprobe_path, video_path)

    # 分析 alpha 边界
    bounds, _, total_frames = analyze_alpha_bounds(
        ffmpeg_path, video_path, info["width"], info["height"], info["fps"],
        codec_name=info["codec_name"],
        alpha_threshold=int(alpha_threshold),
        verbose=False,
    )

    if bounds is None:
        if force:
            suffix = os.path.splitext(video_path)[1] or ".webm"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                output_path = f.name
            copy_video(ffmpeg_path, video_path, output_path)
            gr.Warning("所有帧均为全透明，已强制复制原视频")
            status = (
                f"### ⚠ 所有帧均为全透明，已强制复制原视频\n"
                f"- 原始尺寸: {info['width']}×{info['height']}\n"
                f"- 码率: {bitrate_label}"
            )
            return output_path, status
        else:
            gr.Warning("所有帧均为全透明，无法确定裁剪区域")
            return None, '### ❌ 所有帧均为全透明，无法确定裁剪区域（可勾选"强制输出"重试）'

    min_x, min_y, max_x, max_y = bounds
    crop_x, crop_y, crop_w, crop_h = adjust_crop_to_even(
        min_x, min_y, max_x, max_y, info["width"], info["height"]
    )

    suffix = os.path.splitext(video_path)[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        output_path = f.name

    if (crop_x == 0 and crop_y == 0
            and crop_w == info["width"] and crop_h == info["height"]):
        copy_video(ffmpeg_path, video_path, output_path)
        gr.Info("处理完成，未检测到透明边缘，已直接复制")
        status = (
            f"### ✅ 未检测到透明边缘，已直接复制\n"
            f"- 尺寸: {info['width']}×{info['height']} (无变化)\n"
            f"- 码率: {bitrate_label}"
        )
    else:
        crop_video(ffmpeg_path, video_path, output_path,
                   crop_x, crop_y, crop_w, crop_h,
                   bitrate=bitrate, speed="best", verbose=False)
        gr.Info("裁剪完成")
        status = (
            f"### ✅ 裁剪完成\n"
            f"- 原始尺寸: {info['width']}×{info['height']}\n"
            f"- 裁剪区域: ({crop_x}, {crop_y}) {crop_w}×{crop_h}\n"
            f"- 码率: {bitrate_label}\n"
            f"- 分析帧数: {total_frames}"
        )

    return output_path, status


def build_ui():
    """构建工具 UI，返回 gr.Column。"""
    import gradio as gr

    HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")

    def toggle_custom(choice):
        return gr.update(visible=(choice == "custom"))

    def load_history():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_history(records):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

    def make_download_choices(records):
        choices = []
        for r in records:
            if r.get("output_path") and os.path.isfile(r["output_path"]):
                choices.append(f"{r['filename']} — {r['crop_info']}")
        return choices

    def render_all(records, processing=None):
        """统一渲染排队 + 已完成记录。"""
        if not records and not processing:
            return "<div style='color:#888;text-align:center;padding:20px;'>暂无处理记录</div>"
        rows = []
        idx = 1
        if processing:
            rows.append(
                f"<tr style='background:#fffde7;'>"
                f"<td style='padding:6px 12px;'>{idx}</td>"
                f"<td style='padding:6px 12px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{processing['filename']}</td>"
                f"<td style='padding:6px 12px;'>—</td>"
                f"<td style='padding:6px 12px;'>—</td>"
                f"<td style='padding:6px 12px;'>⏳ {processing.get('status_text', '排队中...')}</td>"
                f"</tr>"
            )
            idx += 1
        for r in reversed(records):
            color = "background:#e8f5e9;" if "成功" in r.get("status", "") else ""
            rows.append(
                f"<tr style='border-bottom:1px solid #eee;{color}'>"
                f"<td style='padding:6px 12px;'>{idx}</td>"
                f"<td style='padding:6px 12px;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{r['filename']}</td>"
                f"<td style='padding:6px 12px;'>{r['orig_size']}</td>"
                f"<td style='padding:6px 12px;'>{r['crop_info']}</td>"
                f"<td style='padding:6px 12px;'>{r['status']}</td>"
                f"</tr>"
            )
            idx += 1
        return (
            "<table style='width:100%;border-collapse:collapse;font-size:14px;'>"
            "<thead><tr style='background:#f5f5f5;'>"
            "<th style='padding:8px 12px;text-align:left;'>#</th>"
            "<th style='padding:8px 12px;text-align:left;'>文件名</th>"
            "<th style='padding:8px 12px;text-align:left;'>原始尺寸</th>"
            "<th style='padding:8px 12px;text-align:left;'>裁剪结果</th>"
            "<th style='padding:8px 12px;text-align:left;'>状态</th>"
            "</tr></thead><tbody>"
            + "".join(rows) +
            "</tbody></table>"
        )

    # 预先加载历史记录
    initial_records = load_history()
    initial_choices = make_download_choices(initial_records)
    has_initial_choices = len(initial_choices) > 0

    with gr.Column() as col:
        gr.Markdown("## 🎬 YUVA VP8 WebM 透明边缘裁剪")

        with gr.Row():
            input_video = gr.Video(label="输入视频", height=360, width=360)
            output_video = gr.Video(label="输出视频", height=360, width=360)

        with gr.Row():
            bitrate_dropdown = gr.Dropdown(
                choices=BITRATE_CHOICES,
                value="original",
                label="码率",
            )
            custom_bitrate = gr.Number(
                label="自定义码率 (kbps)",
                value=2000,
                visible=False,
            )
            alpha_threshold = gr.Slider(
                0, 255, value=0, step=1,
                label="Alpha 阈值",
            )

        force_checkbox = gr.Checkbox(
            label="全透明时强制输出原视频",
            value=False,
        )

        with gr.Row():
            pre_crop_enabled = gr.Checkbox(
                label="启用预裁剪",
                value=False,
            )
        with gr.Row(visible=False) as pre_crop_row:
            pre_crop_top = gr.Slider(
                0, 200, value=0, step=1, label="上边缘 (px)",
            )
            pre_crop_bottom = gr.Slider(
                0, 200, value=0, step=1, label="下边缘 (px)",
            )
            pre_crop_left = gr.Slider(
                0, 200, value=0, step=1, label="左边缘 (px)",
            )
            pre_crop_right = gr.Slider(
                0, 200, value=0, step=1, label="右边缘 (px)",
            )

        process_btn = gr.Button("🔍 分析 & 裁剪", variant="primary")

        status_text = gr.Markdown("")

        gr.Markdown("---\n### 📋 队列与处理记录")
        history_html = gr.HTML(value=render_all(initial_records))
        download_dropdown = gr.Dropdown(
            label="选择下载项",
            choices=initial_choices,
            value=initial_choices[-1] if initial_choices else None,
            interactive=True,
            visible=has_initial_choices,
        )
        download_file = gr.File(label="下载", visible=has_initial_choices)

        def download_selected(choice):
            if choice:
                for r in load_history():
                    label = f"{r['filename']} — {r['crop_info']}"
                    if label == choice and os.path.isfile(r['output_path']):
                        return r['output_path']
            return None

        download_dropdown.change(
            fn=download_selected,
            inputs=[download_dropdown],
            outputs=[download_file],
        )

        def toggle_pre_crop(enabled):
            return gr.update(visible=enabled)

        pre_crop_enabled.change(
            toggle_pre_crop,
            inputs=[pre_crop_enabled],
            outputs=[pre_crop_row],
        )

        bitrate_dropdown.change(
            toggle_custom,
            inputs=[bitrate_dropdown],
            outputs=[custom_bitrate],
        )

        def on_process(video_path, bitrate_choice, custom_bitrate, alpha_threshold, force,
                       pre_crop_enabled, pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right):
            if video_path is None:
                saved = load_history()
                return None, "### ⚠ 请先上传视频文件", render_all(saved), gr.update(), gr.update()

            filename = os.path.basename(video_path)
            saved = load_history()
            yield (
                None,
                f"### ⏳ 处理中: {filename} ...",
                render_all(saved, processing={"filename": filename, "status_text": "分析中..."}),
                gr.update(), gr.update(),
            )
            output_path, status = process_video(
                video_path, bitrate_choice, custom_bitrate, alpha_threshold, force,
                pre_crop_enabled, pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right,
            )
            if "✅" in status:
                st = "✅ 成功"
            elif "⚠" in status:
                st = "⚠ 警告"
            else:
                st = "❌ 失败"
            orig_match = status.split("原始尺寸: ")[-1].split("\n")[0] if "原始尺寸" in status else "-"
            if "裁剪区域: " in status:
                crop_match = status.split("裁剪区域: ")[-1].split("\n")[0]
            elif "无变化" in status:
                crop_match = "无变化"
            elif "已强制复制" in status:
                crop_match = "强制复制"
            elif "无法确定" in status:
                crop_match = "无法裁剪"
            elif "无法读取" in status:
                crop_match = "读取失败"
            elif "不含透明" in status:
                crop_match = "无透明通道"
            else:
                crop_match = "-"
            record = {
                "filename": filename,
                "orig_size": orig_match,
                "crop_info": crop_match,
                "status": st,
                "output_path": output_path or "",
            }
            saved.append(record)
            if len(saved) > 50:
                saved = saved[-50:]
            save_history(saved)
            choices = make_download_choices(saved)
            has_choices = len(choices) > 0
            return (
                output_path,
                status,
                render_all(saved),
                gr.update(choices=choices, value=choices[-1] if choices else None, visible=has_choices),
                gr.update(value=output_path, visible=has_choices),
            )

        process_btn.click(
            fn=on_process,
            inputs=[input_video, bitrate_dropdown, custom_bitrate, alpha_threshold, force_checkbox,
                    pre_crop_enabled, pre_crop_top, pre_crop_bottom, pre_crop_left, pre_crop_right],
            outputs=[output_video, status_text, history_html, download_dropdown, download_file],
        )

    return col


def webui_main(port=7860):
    """启动独立 Gradio Web 界面（不含侧边栏）。"""
    import gradio as gr

    with gr.Blocks(
        title="YUVA VP8 WebM 透明边缘裁剪",
        css="""
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
        """,
    ) as demo:
        build_ui()
    demo.launch(server_name="0.0.0.0", server_port=port)


def main():
    args = parse_args()

    if args.webui:
        webui_main(port=args.port)
        return

    if args.input is None:
        print("错误: 请指定输入文件，或使用 --webui 启动 Web 界面", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.input):
        print(f"错误: 输入文件不存在: {args.input}", file=sys.stderr)
        sys.exit(1)

    output = args.output
    if output is None:
        base = os.path.splitext(args.input)[0]
        output = f"{base}_cropped.webm"

    ffmpeg, ffprobe = check_ffmpeg(args.ffmpeg_path)

    info = get_video_info(ffprobe, args.input)
    print(f"输入视频: {info['width']}x{info['height']}, "
          f"{info['fps']:.2f} fps, {info['pix_fmt']}")

    if not info["has_alpha"]:
        print(f"错误: 输入视频不含透明通道，无法分析透明边缘", file=sys.stderr)
        sys.exit(1)

    bitrate = info["bitrate"]
    if bitrate <= 0:
        print("错误: 无法获取输入视频的码率", file=sys.stderr)
        sys.exit(1)
    print(f"原始码率: {bitrate / 1000:.0f} kbps")

    # 预裁剪
    input_video = args.input
    if args.pre_crop:
        parts = args.pre_crop.split(",")
        if len(parts) != 4:
            print("错误: --pre-crop 格式应为 top,bottom,left,right（如 10,0,5,0）", file=sys.stderr)
            sys.exit(1)
        pc_top, pc_bottom, pc_left, pc_right = map(int, parts)
        if any(v > 0 for v in (pc_top, pc_bottom, pc_left, pc_right)):
            input_video = apply_pre_crop(
                ffmpeg, ffprobe, input_video,
                pc_top, pc_bottom, pc_left, pc_right,
            )
            info = get_video_info(ffprobe, input_video)
            print(f"预裁剪后尺寸: {info['width']}x{info['height']}")

    print("正在分析透明边缘...")
    bounds, _, _ = analyze_alpha_bounds(
        ffmpeg, input_video, info["width"], info["height"], info["fps"],
        codec_name=info["codec_name"],
        alpha_threshold=args.alpha_threshold, verbose=args.verbose,
    )

    if bounds is None:
        if args.force:
            print("警告: 所有帧均为全透明，使用 --force 强制复制原视频")
            copy_video(ffmpeg, input_video, output, verbose=args.verbose)
            print(f"完成: {output}")
            return
        else:
            print("错误: 视频所有帧均为全透明，无法确定裁剪区域", file=sys.stderr)
            print("提示: 使用 --force 强制复制原视频", file=sys.stderr)
            sys.exit(1)

    min_x, min_y, max_x, max_y = bounds
    crop_x, crop_y, crop_w, crop_h = adjust_crop_to_even(
        min_x, min_y, max_x, max_y, info["width"], info["height"]
    )

    print(f"检测到内容区域: ({crop_x},{crop_y}) {crop_w}x{crop_h}")

    if (crop_x == 0 and crop_y == 0
            and crop_w == info["width"] and crop_h == info["height"]):
        print("未检测到透明边缘，无需裁剪，直接复制")
        copy_video(ffmpeg, input_video, output, verbose=args.verbose)
    else:
        print(f"正在裁剪: {info['width']}x{info['height']} -> {crop_w}x{crop_h}")
        crop_video(ffmpeg, input_video, output, crop_x, crop_y, crop_w, crop_h,
                   bitrate=bitrate, speed=args.speed, verbose=args.verbose)

    print(f"完成: {output}")


if __name__ == "__main__":
    main()
