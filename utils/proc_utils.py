"""輕量的程序 / 埠工具（不 import cv2 / PyQt，check_env.py 也能用）。"""
import os
import subprocess
from typing import List, Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_orphan_go2rtc_pids() -> List[int]:
    """找出「父程序已經不存在」的 go2rtc.exe（Windows 限定）。

    App 正常執行時，go2rtc 的父程序就是 App 本身；App 當掉 / 被強制停止後，go2rtc
    會變成孤兒繼續跑。只挑孤兒，才不會誤殺「另一個正在執行（可能正在錄影）的 App」
    底下的 go2rtc。打包成 exe 時 go2rtc 每次解壓到不同暫存資料夾，所以不比對路徑。

    父程序 PID 可能被 Windows 重新分配給別的程式 → 再比較啟動時間：
    「父程序」比 go2rtc 晚啟動，代表那只是重用了同一個 PID，實際上仍是孤兒。
    無法取得資訊（例如權限不足）的程序一律不動。
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    procs = {}   # pid -> (ppid, exe name)
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)

    def _created(pid):
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
                return None
            return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
        finally:
            k32.CloseHandle(h)

    orphans = []
    for pid, (ppid, exe) in procs.items():
        if exe.lower() != "go2rtc.exe":
            continue
        child_t = _created(pid)
        if child_t is None:
            continue                      # 讀不到（權限不足等）→ 不碰
        if ppid not in procs:
            orphans.append(pid)           # 父程序已不存在
            continue
        parent_t = _created(ppid)
        if parent_t is not None and parent_t > child_t:
            orphans.append(pid)           # 父 PID 被重用，原本的父程序已結束
    return orphans


def port_listener_pid(port: int) -> Optional[int]:
    """回傳在本機 TCP port 上 LISTENING 的程序 PID（Windows netstat），找不到回 None。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=10, creationflags=_NO_WINDOW).stdout.decode(errors="replace")
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                pass
    return None


def process_name(pid: int) -> str:
    """PID → 執行檔名稱（例如 go2rtc.exe），查不到回 "?"。"""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=10,
                             creationflags=_NO_WINDOW).stdout.decode(errors="replace").strip()
        if out.startswith('"'):
            return out.split('","')[0].strip('"')
    except Exception:
        pass
    return "?"
