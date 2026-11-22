"""「影像設定」視窗：直接在 App 裡調相機的亮度 / 對比 / 飽和度 / 銳利度 / 白平衡 / 寬動態。

- 拖滑桿 → 停手 0.3 秒後寫進相機（背景執行緒，不卡 UI），單機分頁大畫面立即看到效果。
- 第一次修改某台相機前，自動把原始值備份到 camera_imaging_backup.json。
- 「復原本次修改」= 回到開這個視窗時的值；「還原成原始備份」= 回到第一次被 App 修改前的值。
- 「從其他相機複製」= 讀另一台的設定套用到這台，方便 8 台統一。
"""
import threading

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QSlider, QSpinBox,
    QRadioButton, QButtonGroup, QCheckBox, QPushButton, QComboBox, QGroupBox, QMessageBox,
)

from utils.camera_imaging import (
    CameraImaging, NUMERIC_KEYS, WB_GAIN_KEYS, load_backups, save_backup_once,
)

APPLY_DEBOUNCE_MS = 300


class ImagingDialog(QDialog):
    # 背景執行緒 → UI
    _connected = pyqtSignal(object, object)   # (CameraImaging, settings dict)
    _failed = pyqtSignal(str)
    _applied = pyqtSignal(object, str)        # (settings 讀回值 or None, 錯誤訊息)
    _copied = pyqtSignal(object, str)         # (另一台的 settings or None, 錯誤訊息)

    def __init__(self, name: str, ip: str, camera_map: dict, parent=None):
        super().__init__(parent)
        self.name, self.ip, self.camera_map = name, ip, camera_map
        self.cam = None
        self.initial = None          # 開視窗時讀到的值（「復原本次修改」用）
        self._loading = False        # 程式設定 UI 時不要觸發寫入
        self._pending = None         # 最新待寫入的設定
        self._busy = False
        self.setWindowTitle(f"影像設定 - {name} ({ip})")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(460)
        self._build()
        self._set_controls_enabled(False)
        self._status("連線相機中…")

        self._connected.connect(self._on_connected)
        self._failed.connect(self._on_failed)
        self._applied.connect(self._on_applied)
        self._copied.connect(self._on_copied)
        self._debounce = QTimer(self, singleShot=True, interval=APPLY_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_now)
        threading.Thread(target=self._connect_bg, daemon=True).start()
        # 網路忙（例如經 VPN 同時拉 8 路串流）時 ONVIF 可能要等十幾秒，提示一下免得以為當掉
        QTimer.singleShot(8000, lambda: self.cam is None and "連線相機中" in self.lbl_status.text()
                          and self._status("連線相機中…（網路較忙，請稍候）"))

    # ── UI ────────────────────────────────────────────────────────
    def _build(self):
        f = QFont("Arial", 10)
        self.setFont(f)
        lay = QVBoxLayout(self)

        note = QLabel("調整會直接寫進相機（立即生效、重開機後仍保留），\n"
                      "使用這台相機的其他系統（轉播等）也會看到變化。")
        note.setStyleSheet("color:#a05a00;")
        lay.addWidget(note)

        self.sliders, self.spins = {}, {}

        def add_row(grid, row, key, label, lo, hi):
            grid.addWidget(QLabel(label), row, 0)
            sl = QSlider(Qt.Horizontal); sl.setRange(lo, hi)
            sp = QSpinBox(); sp.setRange(lo, hi); sp.setFixedWidth(64)
            sl.valueChanged.connect(sp.setValue)
            sp.valueChanged.connect(sl.setValue)
            sp.valueChanged.connect(self._on_user_change)
            grid.addWidget(sl, row, 1); grid.addWidget(sp, row, 2)
            self.sliders[key], self.spins[key] = sl, sp

        g_img = QGroupBox("畫面"); grid = QGridLayout(g_img)
        for r, (k, lbl, lo, hi) in enumerate(NUMERIC_KEYS):
            add_row(grid, r, k, lbl, lo, hi)
        self.chk_wdr = QCheckBox("寬動態 (WDR)")
        self.chk_wdr.toggled.connect(self._on_user_change)
        grid.addWidget(self.chk_wdr, len(NUMERIC_KEYS), 0, 1, 3)
        lay.addWidget(g_img)

        g_wb = QGroupBox("白平衡"); wgrid = QGridLayout(g_wb)
        self.rb_auto, self.rb_manual = QRadioButton("自動"), QRadioButton("手動")
        self.wb_group = QButtonGroup(self)
        self.wb_group.addButton(self.rb_auto); self.wb_group.addButton(self.rb_manual)
        self.rb_auto.toggled.connect(self._sync_wb_enabled)
        self.rb_auto.toggled.connect(self._on_user_change)
        h = QHBoxLayout(); h.addWidget(self.rb_auto); h.addWidget(self.rb_manual); h.addStretch()
        wgrid.addLayout(h, 0, 0, 1, 3)
        for r, (k, lbl, lo, hi) in enumerate(WB_GAIN_KEYS, start=1):
            add_row(wgrid, r, k, lbl, lo, hi)
        lay.addWidget(g_wb)

        g_cp = QGroupBox("從其他相機複製"); hc = QHBoxLayout(g_cp)
        self.cmb_src = QComboBox()
        self.cmb_src.addItems([n for n in self.camera_map if n.startswith("Ptz") and n != self.name])
        self.btn_copy = QPushButton("複製到這台")
        self.btn_copy.clicked.connect(self._copy_from)
        hc.addWidget(self.cmb_src, 1); hc.addWidget(self.btn_copy)
        lay.addWidget(g_cp)

        hb = QHBoxLayout()
        self.btn_undo = QPushButton("復原本次修改")
        self.btn_undo.setToolTip("回到打開這個視窗時的設定")
        self.btn_undo.clicked.connect(lambda: self._load_and_apply(self.initial))
        self.btn_restore = QPushButton("還原成原始備份")
        self.btn_restore.clicked.connect(self._restore_backup)
        btn_close = QPushButton("關閉"); btn_close.clicked.connect(self.close)
        hb.addWidget(self.btn_undo); hb.addWidget(self.btn_restore); hb.addStretch(); hb.addWidget(btn_close)
        lay.addLayout(hb)

        self.lbl_status = QLabel(); lay.addWidget(self.lbl_status)

    def _status(self, text, color="#555"):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color:{color};")

    def _set_controls_enabled(self, on):
        for w in list(self.sliders.values()) + list(self.spins.values()) + [
                self.chk_wdr, self.rb_auto, self.rb_manual, self.cmb_src, self.btn_copy,
                self.btn_undo, self.btn_restore]:
            w.setEnabled(on)
        if on:
            self._sync_wb_enabled()
            self.btn_restore.setEnabled(self.name in load_backups())

    def _sync_wb_enabled(self):
        manual = self.rb_manual.isChecked() and self.rb_manual.isEnabled()
        for k, *_ in WB_GAIN_KEYS:
            self.sliders[k].setEnabled(manual); self.spins[k].setEnabled(manual)

    # ── UI <-> dict ───────────────────────────────────────────────
    def _ui_values(self) -> dict:
        d = {k: self.spins[k].value() for k, *_ in NUMERIC_KEYS + WB_GAIN_KEYS}
        d["WB_Mode"] = "MANUAL" if self.rb_manual.isChecked() else "AUTO"
        d["WDR_Mode"] = "ON" if self.chk_wdr.isChecked() else "OFF"
        return d

    def _load_ui(self, d: dict):
        self._loading = True
        try:
            for k, *_ in NUMERIC_KEYS + WB_GAIN_KEYS:
                if d.get(k) is not None:
                    self.spins[k].setValue(int(d[k]))
            (self.rb_manual if d.get("WB_Mode") == "MANUAL" else self.rb_auto).setChecked(True)
            self.chk_wdr.setChecked(d.get("WDR_Mode") == "ON")
            self._sync_wb_enabled()
        finally:
            self._loading = False

    # ── 連線 ──────────────────────────────────────────────────────
    def _connect_bg(self):
        try:
            cam = CameraImaging(self.ip)
            self._connected.emit(cam, cam.get())
        except Exception as e:
            self._failed.emit(str(e))

    def _on_connected(self, cam, settings):
        self.cam, self.initial = cam, dict(settings)
        for k, (lo, hi) in cam.ranges.items():
            self.sliders[k].setRange(lo, hi); self.spins[k].setRange(lo, hi)
        self._load_ui(settings)
        self._set_controls_enabled(True)
        self._status("已讀取相機目前設定。拖動滑桿即會套用。")

    def _on_failed(self, msg):
        self._status(f"無法連線相機的 ONVIF 影像設定：{msg}", "#c00")

    # ── 寫入（debounce + 單一背景寫入，永遠只寫最新值）──────────────
    def _on_user_change(self, *_):
        if self._loading or self.cam is None:
            return
        self._pending = self._ui_values()
        self._status("套用中…")
        self._debounce.start()

    def _load_and_apply(self, d):
        if not d:
            return
        self._load_ui(d)
        self._pending = self._ui_values()
        self._status("套用中…")
        self._apply_now()

    def _apply_now(self):
        if self._busy or self._pending is None:
            return
        d, self._pending, self._busy = self._pending, None, True
        try:
            if save_backup_once(self.name, self.ip, self.initial):
                print(f"[imaging] {self.name} 原始設定已備份：{self.initial}")
        except OSError as e:
            print(f"[imaging] 備份寫入失敗：{e}")

        def run():
            try:
                self.cam.set(d)
                self._applied.emit(self.cam.get(), "")
            except Exception as e:
                self._applied.emit(None, str(e))
        threading.Thread(target=run, daemon=True).start()

    def _on_applied(self, readback, err):
        self._busy = False
        if self._pending is not None:      # 寫入期間使用者又動了 → 接著寫最新值
            self._apply_now()
            return
        if err:
            self._status(f"套用失敗：{err}", "#c00")
            return
        self.btn_restore.setEnabled(self.name in load_backups())
        want = self._ui_values()
        bad = [k for k in want if not (want["WB_Mode"] == "AUTO" and k in ("CrGain", "CbGain"))
               and readback.get(k) != want[k]]
        if bad:
            self._status(f"相機未完全接受：{', '.join(bad)}（已顯示相機實際值）", "#c00")
            self._load_ui(readback)
        else:
            self._status("✓ 已套用到相機")
            if want["WB_Mode"] == "AUTO":   # 顯示自動白平衡目前算出的增益
                self._load_ui(readback)

    # ── 複製 / 還原 ───────────────────────────────────────────────
    def _copy_from(self):
        src = self.cmb_src.currentText()
        ip = self.camera_map.get(src)
        if not ip:
            return
        self.btn_copy.setEnabled(False)
        self._status(f"讀取 {src} 的設定…")

        def run():
            try:
                self._copied.emit(CameraImaging(ip).get(), "")
            except Exception as e:
                self._copied.emit(None, f"{src}：{e}")
        threading.Thread(target=run, daemon=True).start()

    def _on_copied(self, d, err):
        self.btn_copy.setEnabled(True)
        if err:
            self._status(f"讀取失敗：{err}", "#c00")
            return
        self._load_and_apply(d)

    def _restore_backup(self):
        b = load_backups().get(self.name)
        if not b:
            return
        if QMessageBox.question(self, "還原成原始備份",
                                f"把 {self.name} 還原成 {b['saved_at']} 備份的原始設定？") != QMessageBox.Yes:
            return
        self._load_and_apply(b["settings"])
