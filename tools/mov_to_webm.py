#!/usr/bin/env python3
"""将带透明通道的 MOV (Apple ProRes 4444) 视频转换为 WebM (YUVA VP8) 格式。"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

from pathlib import Path

# 修复 Python 3.13+ Windows 上 Proactor 事件循环的 ConnectionResetError
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _patched_call_connection_lost(self):
            try:
                _orig_call_connection_lost(self)
            except ConnectionResetError:
                pass

        _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost


BITRATE_CHOICES = [
    ("500 kbps", "500000"),
    ("1000 kbps (默认)", "1000000"),
    ("2000 kbps", "2000000"),
    ("3000 kbps", "3000000"),
    ("4000 kbps", "4000000"),
    ("5000 kbps", "5000000"),
    ("自定义", "custom"),
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="将带透明通道的 MOV (Apple ProRes 4444) 视频转换为 WebM (YUVA VP8) 格式。"
    )
    parser.add_argument("input", nargs="?", default=None, help="输入 MOV 视频路径")
    parser.add_argument(
        "output", nargs="?", default=None,
        help="输出 WebM 视频路径（默认：输入文件名 + _vp8.webm）",
    )
    parser.add_argument(
        "--bitrate", type=int, default=1000,
        help="输出码率 (kbps)，默认: 1000",
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
    codec_name = video_stream.get("codec_name", "")
    has_audio = audio_stream is not None
    # ProRes 4444 的像素格式 yuva444p10le 表示有 alpha
    has_alpha = (
        pix_fmt.startswith("yuva") or
        pix_fmt.startswith("rgba") or
        alpha_mode == "1"
    )

    return {
        "width": width,
        "height": height,
        "pix_fmt": pix_fmt,
        "codec_name": codec_name,
        "has_audio": has_audio,
        "has_alpha": has_alpha,
    }


def convert_to_webm(ffmpeg_path, input_video, output_video, bitrate,
                    verbose=False):
    """将 MOV 转换为 VP8 WebM（保留透明通道，音频转 Vorbis）。"""
    cmd = [
        ffmpeg_path, "-y",
        "-i", input_video,
        "-c:v", "libvpx",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", str(bitrate),
        "-deadline", "good",
        "-c:a", "libvorbis",
        output_video,
    ]

    stderr_target = None if verbose else subprocess.DEVNULL
    stdout_target = None if verbose else subprocess.DEVNULL

    result = subprocess.run(cmd, stdout=stdout_target, stderr=stderr_target)
    if result.returncode != 0:
        print("错误: ffmpeg 转换失败", file=sys.stderr)
        sys.exit(1)


_TEMP_PREFIX = "mov_to_webm_"
_TEMP_MAX_AGE_SECONDS = 2 * 24 * 3600  # 2 天


def _cleanup_old_temp_files():
    """清理超过 2 天的旧临时输出文件。"""
    import time

    temp_dir = tempfile.gettempdir()
    now = time.time()
    count = 0
    try:
        for name in os.listdir(temp_dir):
            if not name.startswith(_TEMP_PREFIX):
                continue
            path = os.path.join(temp_dir, name)
            if not os.path.isfile(path):
                continue
            if now - os.path.getmtime(path) > _TEMP_MAX_AGE_SECONDS:
                try:
                    os.unlink(path)
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    if count > 0:
        print(f"[清理] 已删除 {count} 个超过 2 天的旧临时文件", flush=True)


def process_video(file_path, bitrate_choice, custom_bitrate):
    """Gradio 回调：将 MOV 转换为 VP8 WebM。"""
    import gradio as gr

    _cleanup_old_temp_files()

    if file_path is None:
        gr.Warning("请先上传 MOV 文件")
        return None, None, "### ⚠ 请先上传 MOV 文件"

    # 校验文件扩展名
    if not file_path.lower().endswith(".mov"):
        gr.Warning("仅支持 .mov 格式文件")
        return None, None, "### ⚠ 仅支持 .mov 格式文件，请重新上传"

    ffmpeg_path, ffprobe_path = check_ffmpeg()

    try:
        info = get_video_info(ffprobe_path, file_path)
    except SystemExit:
        gr.Error("无法读取视频信息")
        return None, None, "### ❌ 无法读取视频信息"

    # 码率处理
    if bitrate_choice == "custom":
        if custom_bitrate is None or custom_bitrate <= 0:
            gr.Warning("请输入有效的自定义码率")
            return None, None, "### ⚠ 请输入有效的自定义码率"
        bitrate = int(custom_bitrate * 1000)
        bitrate_label = f"{custom_bitrate:.0f} kbps (自定义)"
    else:
        bitrate = int(bitrate_choice)
        bitrate_label = f"{bitrate / 1000:.0f} kbps"

    # 输出到临时文件
    with tempfile.NamedTemporaryFile(
        delete=False, prefix=_TEMP_PREFIX, suffix=".webm"
    ) as f:
        output_path = f.name

    convert_to_webm(
        ffmpeg_path, file_path, output_path,
        bitrate=bitrate,
        verbose=False,
    )

    alpha_note = "✅ 含透明通道" if info["has_alpha"] else "⚠ 未检测到透明通道"
    audio_note = "✅ 含音频" if info["has_audio"] else "— 无音频"

    status = (
        f"### ✅ 转换完成\n"
        f"- 格式: VP8 WebM (libvpx)\n"
        f"- 尺寸: {info['width']}×{info['height']}\n"
        f"- 码率: {bitrate_label}\n"
        f"- 透明通道: {alpha_note}\n"
        f"- 音频: {audio_note}\n"
        f"- 像素格式: yuva420p"
    )

    return output_path, output_path, status


def build_ui(visible=True):
    """构建工具 UI，返回 gr.Column。"""
    import gradio as gr

    def toggle_custom(choice):
        return gr.update(visible=(choice == "custom"))

    with gr.Column(visible=visible) as col:
        gr.Markdown("## 🎞 MOV (ProRes 4444) → WebM VP8 (透明通道)")

        with gr.Row():
            input_file = gr.File(
                label="输入 MOV 文件",
                file_types=[".mov"],
            )
            output_video = gr.Video(
                label="输出 WebM 视频（预览）",
                height=360, width=360,
            )

        with gr.Row():
            bitrate_dropdown = gr.Dropdown(
                choices=BITRATE_CHOICES,
                value="1000000",
                label="输出码率",
            )
            custom_bitrate = gr.Number(
                label="自定义码率 (kbps)",
                value=2000,
                visible=False,
            )

        convert_btn = gr.Button("🔄 转换为 VP8 WebM", variant="primary")

        status_text = gr.Markdown("")
        download_file = gr.File(label="下载转换结果", visible=True)

        bitrate_dropdown.change(
            toggle_custom,
            inputs=[bitrate_dropdown],
            outputs=[custom_bitrate],
        )

        def on_convert(file_obj, bitrate_choice, custom_bitrate):
            if file_obj is None:
                yield None, None, "### ⚠ 请先上传 MOV 文件", None
                return

            # gradio File 组件返回文件路径（字符串）或文件对象
            file_path = file_obj if isinstance(file_obj, str) else file_obj.name
            filename = os.path.basename(file_path)

            yield None, None, f"### ⏳ 转换中: {filename} ...", None

            output_path, video_path, status = process_video(
                file_path, bitrate_choice, custom_bitrate,
            )
            yield video_path, output_path, status, output_path

        convert_btn.click(
            fn=on_convert,
            inputs=[input_file, bitrate_dropdown, custom_bitrate],
            outputs=[output_video, download_file, status_text, download_file],
        )

    return col


def webui_main(port=7860):
    """启动独立 Gradio Web 界面（不含侧边栏）。"""
    try:
        import gradio as gr
    except ImportError:
        print("错误: 使用 --webui 需要安装 gradio", file=sys.stderr)
        print("运行: pip install gradio", file=sys.stderr)
        sys.exit(1)

    with gr.Blocks(
        title="MOV (ProRes 4444) → WebM VP8 (透明通道)",
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

    if not args.input.lower().endswith(".mov"):
        print("错误: 仅支持 .mov 格式文件", file=sys.stderr)
        sys.exit(1)

    output = args.output
    if output is None:
        base = os.path.splitext(args.input)[0]
        output = f"{base}_vp8.webm"

    ffmpeg, ffprobe = check_ffmpeg(args.ffmpeg_path)

    info = get_video_info(ffprobe, args.input)
    print(f"输入视频: {info['width']}x{info['height']}, "
          f"{info['pix_fmt']}, codec={info['codec_name']}")

    alpha_str = "✅ 含透明通道" if info["has_alpha"] else "⚠ 未检测到透明通道"
    print(f"透明通道: {alpha_str}")
    print(f"音频: {'有' if info['has_audio'] else '无'}")

    bitrate = args.bitrate * 1000
    print(f"输出码率: {args.bitrate} kbps")

    print("正在转换为 VP8 WebM...")
    convert_to_webm(
        ffmpeg, args.input, output,
        bitrate=bitrate,
        verbose=args.verbose,
    )

    print(f"完成: {output}")


if __name__ == "__main__":
    main()
