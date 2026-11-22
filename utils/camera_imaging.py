"""相機影像參數（亮度 / 對比 / 飽和度 / 銳利度 / 白平衡 / 寬動態）的 ONVIF 讀寫。

Ptz01~08 透過 ONVIF Imaging service 開放的項目（GetOptions 實測）：
  Brightness / ColorSaturation / Contrast 0~100、Sharpness 0~31、
  WhiteBalance AUTO / MANUAL（CrGain / CbGain 1~255）、WideDynamicRange ON / OFF。
曝光（Exposure）沒有開放，只能在相機網頁調。

寫入用 ForcePersistence=True → 相機重開機後仍保留，跟在相機網頁上改一樣。

備份：每台相機「第一次被本 App 修改前」的值存到 camera_imaging_backup.json
（exe 所在資料夾 / 專案根目錄），之後不再覆蓋 → 永遠可以還原成最原始的設定。
"""
import datetime
import json
import os
import sys
import threading

from .app_config import get_camera_credentials

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WSDL_DIR = os.path.join(_PROJECT_ROOT, "wsdl")

# 本 App 處理的欄位（UI 顯示順序）。範圍以相機 GetOptions 為準，這裡是預設值。
NUMERIC_KEYS = [
    ("Brightness", "亮度", 0, 100),
    ("Contrast", "對比", 0, 100),
    ("ColorSaturation", "飽和度", 0, 100),
    ("Sharpness", "銳利度", 0, 31),
]
WB_GAIN_KEYS = [("CrGain", "紅色增益 (Cr)", 1, 255), ("CbGain", "藍色增益 (Cb)", 1, 255)]


def backup_path() -> str:
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else _PROJECT_ROOT
    return os.path.join(base, "camera_imaging_backup.json")


def load_backups() -> dict:
    try:
        with open(backup_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_backup_once(name: str, ip: str, settings: dict) -> bool:
    """只在這台相機還沒有備份時寫入。回傳是否有寫。"""
    data = load_backups()
    if name in data:
        return False
    data[name] = {"ip": ip, "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                  "settings": settings}
    tmp = backup_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, backup_path())
    return True


def _num(v, default=None):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


class CameraImaging:
    """一台相機的 imaging service 連線。建構時會連線（約 1 秒），請在背景執行緒建立。"""

    def __init__(self, ip: str, port: int = 80, user=None, pwd=None):
        from onvif import ONVIFCamera
        if user is None or pwd is None:
            user, pwd = get_camera_credentials()
        self.ip = ip
        cam = ONVIFCamera(ip, port, user, pwd, wsdl_dir=_WSDL_DIR)
        profile = cam.create_media_service().GetProfiles()[0]
        self.token = profile.VideoSourceConfiguration.SourceToken
        self.img = cam.create_imaging_service()
        self._lock = threading.Lock()
        self.ranges = self._read_ranges()

    def _read_ranges(self) -> dict:
        r = {k: (lo, hi) for k, _, lo, hi in NUMERIC_KEYS + WB_GAIN_KEYS}
        try:
            o = self.img.GetOptions({"VideoSourceToken": self.token})
        except Exception:
            return r
        for k, _, _, _ in NUMERIC_KEYS:
            rng = getattr(o, k, None)
            if rng is not None and rng.Min is not None and rng.Max is not None:
                r[k] = (_num(rng.Min), _num(rng.Max))
        wb = getattr(o, "WhiteBalance", None)
        if wb is not None:
            for k, ok in (("CrGain", "YrGain"), ("CbGain", "YbGain")):
                rng = getattr(wb, ok, None)
                if rng is not None and rng.Min is not None and rng.Max is not None:
                    r[k] = (_num(rng.Min), _num(rng.Max))
        return r

    def get(self) -> dict:
        """回傳扁平 dict：Brightness/Contrast/ColorSaturation/Sharpness/WB_Mode/CrGain/CbGain/WDR_Mode。"""
        with self._lock:
            s = self.img.GetImagingSettings({"VideoSourceToken": self.token})
        d = {k: _num(getattr(s, k, None)) for k, _, _, _ in NUMERIC_KEYS}
        wb = getattr(s, "WhiteBalance", None)
        d["WB_Mode"] = (wb.Mode if wb is not None else None) or "AUTO"
        d["CrGain"] = _num(getattr(wb, "CrGain", None)) if wb is not None else None
        d["CbGain"] = _num(getattr(wb, "CbGain", None)) if wb is not None else None
        wdr = getattr(s, "WideDynamicRange", None)
        d["WDR_Mode"] = (wdr.Mode if wdr is not None else None)
        return d

    def set(self, d: dict) -> None:
        """寫入相機（ForcePersistence）。只送 d 裡有值的欄位。

        一律送整數：這批相機韌體會把收到的字串原樣存起來、讀取時再補 ".0"，
        送 "144.0"（zeep 序列化 float 的結果）會讀回 "144.0.0" → 解析失敗。送 "144" 才正常。
        """
        req = {}
        for k, _, _, _ in NUMERIC_KEYS:
            if d.get(k) is not None:
                req[k] = int(d[k])
        if d.get("WB_Mode"):
            wb = {"Mode": d["WB_Mode"]}
            # AUTO 模式下增益由相機自己決定，送了可能被拒；只有 MANUAL 才送增益
            if d["WB_Mode"] == "MANUAL":
                for k in ("CrGain", "CbGain"):
                    if d.get(k) is not None:
                        wb[k] = int(d[k])
            req["WhiteBalance"] = wb
        if d.get("WDR_Mode"):
            req["WideDynamicRange"] = {"Mode": d["WDR_Mode"]}
        with self._lock:
            self.img.SetImagingSettings({"VideoSourceToken": self.token,
                                         "ImagingSettings": req,
                                         "ForcePersistence": True})
