"""相機連線診斷：連不上時找出「為什麼」。

go2rtc 模式下 App 連的是本機 127.0.0.1:8554，相機真正的錯誤（帳密錯、網路不通）
被 go2rtc 擋在後面看不到，畫面只會顯示「連線失敗」。這裡繞過 go2rtc，用 ffprobe
直接連相機一次，把錯誤分類成人看得懂的原因。

注意：Ptz02 / Ptz06 不需帳密也能看串流，其他相機沒帳密會回 401。
所以「只有 Ptz02、Ptz06 有畫面」幾乎一定是 config.ini 帳密沒填或填錯。
"""
import os
import subprocess

from .icons_utils import resource_path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# config.example.ini 範本裡的假帳密；clone 下來直接用就會是這組
PLACEHOLDER_USERS = {"", "your_camera_user"}
PLACEHOLDER_PASSES = {"", "your_camera_password"}


def credentials_look_unset(user: str, pwd: str) -> bool:
    """帳密是空的或還是範本值 → True。"""
    return (user or "").strip() in PLACEHOLDER_USERS or (pwd or "").strip() in PLACEHOLDER_PASSES


def resolve_ffprobe_exe() -> str:
    p = resource_path("ffprobe.exe")
    return p if os.path.isfile(p) else "ffprobe"


def diagnose_camera(ip: str, user: str, pwd: str, port: int = 554,
                    stream_path: str = "stream0", timeout_s: float = 6.0):
    """直接連相機 RTSP（不經 go2rtc），回傳 (ok: bool, reason: str)。
    reason 是給人看的簡短中文說明，不含密碼。"""
    auth = f"{user}:{pwd}@" if user else ""
    url = f"rtsp://{auth}{ip}:{port}/{stream_path}"
    cmd = [
        resolve_ffprobe_exe(), "-v", "error", "-rtsp_transport", "tcp",
        "-timeout", str(int(timeout_s * 1_000_000)),
        "-select_streams", "v:0", "-show_entries", "stream=width,height",
        "-of", "csv=p=0", url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s + 10,
                           creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return False, "找不到 ffprobe.exe，無法診斷（請放到專案根目錄）"
    except subprocess.TimeoutExpired:
        return False, "網路不通：相機沒有回應（檢查網路線 / 是否在 10.30.3.x 網段）"

    if r.returncode == 0:
        size = r.stdout.decode(errors="replace").strip().replace(",", "x")
        return True, f"相機直連正常 {size}"

    err = r.stderr.decode(errors="replace")
    if pwd:
        err = err.replace(pwd, "***")
    low = err.lower()
    if "401" in err or "unauthorized" in low:
        return False, "帳密錯誤（401）：請檢查 config.ini 的 username / password"
    if "timed out" in low or "timeout" in low or "10060" in err:
        return False, "網路不通：連線逾時（檢查網路線 / 是否在 10.30.3.x 網段）"
    if "refused" in low or "10061" in err:
        return False, "相機拒絕連線（RTSP 服務未開或 port 錯誤）"
    if "404" in err or "not found" in low:
        return False, f"相機上找不到串流路徑 /{stream_path}"
    last = err.strip().splitlines()[-1] if err.strip() else f"ffprobe exit {r.returncode}"
    return False, f"無法連線：{last[-120:]}"
