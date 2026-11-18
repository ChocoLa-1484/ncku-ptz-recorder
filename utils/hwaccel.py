"""ffmpeg 硬體解碼後端自動選擇（預覽 + 同步播放共用）。

原本程式寫死 Intel QSV（h264_qsv），在沒有 Intel iGPU 的電腦（例如只有 NVIDIA 顯卡）
上 ffmpeg 會報 "Failed to find d3d11va adapter by vendor id 0x8086" 然後整個預覽黑畫面。

這裡在第一次使用時依序探測：qsv（Intel iGPU）→ cuda（NVIDIA NVDEC）→ sw（CPU 軟解），
選第一個能初始化的。結果寫進環境變數 PTZ_HWACCEL，讓之後 spawn 的 camera worker
子行程直接沿用、不必再探測。

手動指定：啟動前設環境變數 PTZ_HWACCEL=qsv / cuda / sw。
"""
import os
import subprocess

ENV_HWACCEL = "PTZ_HWACCEL"
_CANDIDATES = ("qsv", "cuda")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _probe(ffmpeg_exe: str, kind: str) -> bool:
    """用 ffmpeg 初始化指定的 HW device 一次，成功回 True。"""
    cmd = [
        ffmpeg_exe, "-hide_banner", "-loglevel", "error",
        "-init_hw_device", f"{kind}=hw",
        "-f", "lavfi", "-i", "nullsrc=s=64x64",
        "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def get_hwaccel(ffmpeg_exe: str) -> str:
    """回傳 'qsv' / 'cuda' / 'sw'。結果快取在環境變數中（子行程會繼承）。"""
    v = os.environ.get(ENV_HWACCEL, "").strip().lower()
    if v in ("qsv", "cuda", "sw"):
        return v
    chosen = "sw"
    for kind in _CANDIDATES:
        if _probe(ffmpeg_exe, kind):
            chosen = kind
            break
    os.environ[ENV_HWACCEL] = chosen
    print(f"[hwaccel] ffmpeg 解碼後端 = {chosen}"
          + ("（找不到 QSV/CUDA，改用 CPU 軟解，8 路 1080p120 會很吃 CPU）" if chosen == "sw" else ""))
    return chosen


def decode_input_args(kind: str) -> list:
    """放在 -i 之前的解碼參數。"""
    if kind == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", "h264_qsv"]
    if kind == "cuda":
        # -threads 1：相機串流（H.264 Main L5.1, 1080p120）DPB 大，預設多執行緒會要 37 個
        # decode surface，超過 NVDEC 上限 32 → cuvidCreateDecoder INVALID_VALUE。
        # NVDEC 是硬體解碼，CPU 執行緒數對效能沒影響。
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-threads", "1"]
    return []


def build_vf(kind: str, fps: int, scale_wh=None) -> str:
    """-vf 字串：fps 先在 GPU surface 上丟幀，再 hwdownload 回 CPU NV12，最後 CPU scale。"""
    parts = [f"fps={fps}"]
    if kind in ("qsv", "cuda"):
        parts += ["hwdownload", "format=nv12"]
    if scale_wh:
        parts.append(f"scale={scale_wh[0]}:{scale_wh[1]}")
    return ",".join(parts)
