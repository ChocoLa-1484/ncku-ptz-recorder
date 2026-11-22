"""環境自我檢查：到現場 / 新電腦 clone 後，開 App 前先跑一次。

    check.bat            （或 .venv\\Scripts\\python.exe check_env.py）

逐項檢查並告訴你哪裡有問題、要怎麼修：
  1. Python 套件      2. ffmpeg / ffprobe / go2rtc     3. config.ini 帳密
  4. wsdl 檔名        5. 8 台 PTZ：網路、RTSP 帳密、ONVIF port
  6. go2rtc 轉流      7. 解碼後端（QSV / CUDA / CPU）  8. NAS 路徑（僅提醒）
全部 OK 才代表 App 開起來 8 台會有畫面。
"""
import os
import socket
import subprocess
import sys
import time
import concurrent.futures as cf

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

PTZ = [(f"Ptz{i:02d}", f"10.30.3.{26 + i}") for i in range(1, 9)]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

results = []   # (ok: True/False/None(提醒), 項目, 說明)


def report(ok, item, msg=""):
    results.append((ok, item, msg))
    tag = {True: "[ OK ]", False: "[FAIL]", None: "[WARN]"}[ok]
    print(f"{tag} {item}" + (f"  -  {msg}" if msg else ""), flush=True)


def tcp_open(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def section(title):
    print(f"\n== {title} " + "=" * max(0, 50 - len(title)), flush=True)


# ── 1. Python 套件 ───────────────────────────────────────────────
section("1. Python 環境")
report(sys.version_info[:2] >= (3, 14), f"Python {sys.version.split()[0]}",
       "" if sys.version_info[:2] >= (3, 14) else "建議用 .venv 的 Python 3.14（跑 install.bat）")
for mod, pkg in [("numpy", "numpy"), ("cv2", "opencv-python"), ("PyQt5.QtWidgets", "PyQt5"),
                 ("onvif", "onvif_zeep")]:
    try:
        __import__(mod)
        report(True, f"套件 {pkg}")
    except Exception as e:
        report(False, f"套件 {pkg}", f"import 失敗：{e}（跑 install.bat）")

# ── 2. 外部執行檔 ────────────────────────────────────────────────
section("2. 外部執行檔（專案根目錄）")
exe_ok = {}
for exe, args, want in [("ffmpeg.exe", ["-version"], "8.1.1"),
                        ("ffprobe.exe", ["-version"], "8.1.1"),
                        ("go2rtc.exe", ["-version"], "1.9.14")]:
    if not os.path.isfile(exe):
        exe_ok[exe] = False
        report(False, exe, "不存在！依 README 2-3 下載後放到和 main.py 同一層")
        continue
    try:
        out = subprocess.run([os.path.abspath(exe)] + args, capture_output=True, timeout=15,
                             creationflags=_NO_WINDOW).stdout.decode(errors="replace")
        first = out.strip().splitlines()[0] if out.strip() else "?"
        exe_ok[exe] = True
        report(True if want in first else None, exe,
               first[:70] + ("" if want in first else f"（建議版本 {want}）"))
    except Exception as e:
        exe_ok[exe] = False
        report(False, exe, f"無法執行：{e}")

# ── 3. 帳密 ──────────────────────────────────────────────────────
section("3. 相機帳密（config.ini）")
from utils.app_config import get_camera_credentials
from utils.camera_check import credentials_look_unset, diagnose_camera

user, pwd = get_camera_credentials()
cred_ok = not credentials_look_unset(user, pwd)
if not os.path.isfile("config.ini") and not os.environ.get("PTZ_CAMERA_USER"):
    report(False, "config.ini", "不存在：copy config.example.ini config.ini 後填帳密")
elif not cred_ok:
    report(False, "config.ini", "帳密是空的或仍是範本值 → 只有 Ptz02/Ptz06 會有畫面")
else:
    report(True, "config.ini", f"username = {user}，password 已設定")
if any(c in pwd for c in "@:/"):
    report(False, "密碼字元", "密碼含 @ : / 會讓 RTSP URL 解析錯誤")

# ── 4. wsdl ──────────────────────────────────────────────────────
section("4. ONVIF wsdl 檔")
missing = [f for f in ("xmlmime", "envelope", "include", "addressing", "ptz.wsdl", "media.wsdl",
                       "devicemgmt.wsdl") if not os.path.isfile(os.path.join("wsdl", f))]
report(not missing, "wsdl/", "完整" if not missing else
       f"缺少 {missing}（xmlmime/envelope/include/addressing 本來就沒有副檔名，被加上 .xml 要拿掉）")

# ── 5. 8 台 PTZ 直連 ─────────────────────────────────────────────
section("5. 8 台 PTZ 直連（不經 go2rtc）")


def check_cam(name_ip):
    name, ip = name_ip
    rtsp_port = tcp_open(ip, 554)
    onvif_port = tcp_open(ip, 80)
    if not rtsp_port:
        return name, ip, False, "網路不通：連不到 554 port（網路線 / 網段 10.30.3.x / 相機電源）", onvif_port
    if exe_ok.get("ffprobe.exe"):
        ok, why = diagnose_camera(ip, user, pwd)
    else:
        ok, why = None, "554 port 通，但缺 ffprobe.exe 無法驗證帳密"
    return name, ip, ok, why, onvif_port


with cf.ThreadPoolExecutor(8) as ex:
    cam_rows = list(ex.map(check_cam, PTZ))
for name, ip, ok, why, onvif_port in cam_rows:
    report(ok, f"{name} {ip} RTSP", why)
for name, ip, ok, why, onvif_port in cam_rows:
    report(True if onvif_port else None, f"{name} {ip} ONVIF(80)",
           "通" if onvif_port else "不通（不影響畫面；PTZ 方向控制 / 起錄同步會失效）")

# ── 6. go2rtc 轉流 ───────────────────────────────────────────────
section("6. go2rtc 本機轉流（App 實際使用的路徑）")
if not (exe_ok.get("go2rtc.exe") and exe_ok.get("ffprobe.exe")):
    report(False, "go2rtc", "缺 go2rtc.exe 或 ffprobe.exe，跳過")
elif tcp_open("127.0.0.1", 8554, 0.5):
    report(None, "go2rtc", "8554 port 已被佔用（App 可能正在執行），跳過；請關掉 App 再檢查")
else:
    from utils.stream_thread import maybe_start_go2rtc, stop_go2rtc
    proc = maybe_start_go2rtc()
    try:
        for _ in range(30):
            if tcp_open("127.0.0.1", 8554, 0.2):
                break
            time.sleep(0.2)

        def via_go2rtc(name_ip):
            name, _ = name_ip
            t = time.time()
            r = subprocess.run([os.path.abspath("ffprobe.exe"), "-v", "error", "-rtsp_transport", "tcp",
                                "-timeout", "15000000", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                                f"rtsp://127.0.0.1:8554/{name}"],
                               capture_output=True, timeout=40, creationflags=_NO_WINDOW)
            dt = time.time() - t
            if r.returncode == 0:
                return name, True, f"{r.stdout.decode().strip().replace(',', 'x')}（{dt:.1f}s）"
            return name, False, f"失敗（{dt:.1f}s）：" + r.stderr.decode(errors="replace").strip()[-100:]

        with cf.ThreadPoolExecutor(8) as ex:
            for name, ok, msg in ex.map(via_go2rtc, PTZ):
                report(ok, f"{name} via go2rtc", msg)
    finally:
        stop_go2rtc(proc)

# ── 7. 解碼後端 ──────────────────────────────────────────────────
section("7. 預覽解碼後端")
if exe_ok.get("ffmpeg.exe"):
    from utils.hwaccel import get_hwaccel
    hw = get_hwaccel(os.path.abspath("ffmpeg.exe"))
    report(True if hw != "sw" else None, f"解碼 = {hw}",
           {"qsv": "Intel iGPU", "cuda": "NVIDIA GPU", "sw": "CPU 軟解（8 路可能吃緊）"}[hw])

# ── 8. NAS ───────────────────────────────────────────────────────
section("8. NAS 存檔位置")
nas_root = os.path.splitdrive(r"Z:\PTZ recordings")[0] + "\\"
report(True if os.path.isdir(nas_root) else None, f"NAS {nas_root}",
       "存在" if os.path.isdir(nas_root) else "不存在（錄影前 App 會問要改存桌面或自選資料夾）")

# ── 總結 ─────────────────────────────────────────────────────────
fails = [r for r in results if r[0] is False]
warns = [r for r in results if r[0] is None]
print("\n" + "=" * 60)
if not fails:
    print(f"全部通過（{len(warns)} 個提醒）。可以執行 start.bat。")
else:
    print(f"有 {len(fails)} 項失敗，請先修正：")
    for _, item, msg in fails:
        print(f"  - {item}：{msg}")
print("=" * 60)
sys.exit(1 if fails else 0)
