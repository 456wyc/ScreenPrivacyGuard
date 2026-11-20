from __future__ import annotations
import sys
from contextlib import contextmanager
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QComboBox, QSlider, QDoubleSpinBox,
    QGroupBox, QMessageBox, QProgressBar, QCheckBox, QFormLayout,
    QListWidget, QListWidgetItem, QDialog, QDialogButtonBox, QLineEdit,
    QScrollArea)
from .config import load_settings, save_settings, Settings
from .vision import VisionWorker
from .overlay import PrivacyOverlay
from . import windowctl

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.worker = None
        self.overlay = PrivacyOverlay(self.settings)
        # 自动置顶：记录被置顶窗口的快照 + 当前是否已置顶的状态位。
        # 置顶由 vision 的实时触发原因（on_risk）直接驱动，不再依赖覆盖层信号，
        # 因此与窗口模糊覆盖完全解耦、可各自独立开关与配置触发规则。
        self._raised_snapshot = []
        self._raise_active = False
        self.setWindowTitle("PrivacyGuard AI")
        # 窗口可自由缩放；设最小尺寸防止被拖到不可用，初始给个舒适尺寸。
        self.setMinimumSize(620, 420)
        self.resize(780, 720)
        self._build_ui()
        # 恢复预览画面的收起状态，并让窗口高度自适应
        self._apply_preview_state(initial=True)
        self._start_worker()
        QShortcut(QKeySequence("Ctrl+Shift+P"), self, activated=self.toggle_protection)
        QShortcut(QKeySequence("Escape"), self, activated=self.pause_protection)

    def _build_ui(self):
        # 所有内容先放进 content，再用 QScrollArea 包裹：窗口可自由缩放，
        # 内容超过可视高度时自动出现滚动条，不再受限于内容最小高度。
        content = QWidget()
        layout = QVBoxLayout(content)
        title = QLabel("PrivacyGuard AI")
        title.setStyleSheet("font-size: 26px; font-weight: 700;")
        subtitle = QLabel("本地 AI 隐私屏幕 · 摄像头识别陌生人并动态保护屏幕")
        subtitle.setStyleSheet("color: #718096;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # 预览区：一个折叠按钮 + 画面本身，可整体收起以节省空间
        self.preview_toggle = QPushButton("▾ 收起摄像头画面")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setToolTip("收起后摄像头仍在后台识别，保护功能不受影响")
        self.preview_toggle.setStyleSheet(
            "background:transparent;color:#4a5568;border:none;text-align:left;padding:2px;")
        # clicked 会携带 checked(bool)，必须用无参槽接收，避免参数错位到 initial
        self.preview_toggle.clicked.connect(self._on_preview_toggled)
        layout.addWidget(self.preview_toggle)

        self.preview = QLabel("正在启动摄像头…")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(270)
        self.preview.setStyleSheet("background:#111827;color:#d1d5db;border-radius:10px;")
        layout.addWidget(self.preview)

        self.status = QLabel("状态：初始化中")
        self.status.setStyleSheet("font-weight:600;")
        layout.addWidget(self.status)
        self.risk_label = QLabel("隐私风险：0% · 未检测到陌生人")
        layout.addWidget(self.risk_label)

        face_group = QGroupBox("机主身份")
        face_layout = QHBoxLayout(face_group)
        self.owner_label = QLabel("已注册机主" if self.settings.owner_embedding else "尚未注册机主")
        self.enroll_btn = QPushButton("注册 / 更新机主人脸")
        self.enroll_btn.clicked.connect(self.enroll_owner)
        face_layout.addWidget(self.owner_label, 1)
        face_layout.addWidget(self.enroll_btn)
        layout.addWidget(face_group)

        def labeled(label_text, widget):
            """标签 + 控件水平打包成一个 QWidget，便于多个字段并排一行。"""
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label_text))
            row.addWidget(widget, 1)
            return box

        def trigger_row(initial, enable_check=None):
            """启用开关 + 触发原因多选放同一行，返回 (容器, stranger, absent)。"""
            row = QHBoxLayout()
            if enable_check is not None:
                row.addWidget(enable_check)
                row.addSpacing(16)
            row.addWidget(QLabel("触发原因："))
            stranger = QCheckBox("有陌生人")
            absent = QCheckBox("主人离开")
            stranger.setChecked("stranger" in initial)
            absent.setChecked("owner_absent" in initial)
            row.addWidget(stranger)
            row.addWidget(absent)
            row.addStretch(1)
            holder = QWidget()
            holder.setLayout(row)
            return holder, stranger, absent

        # ========== 手段一：窗口模糊/遮挡覆盖 ==========
        overlay_group = QGroupBox("保护手段一 · 窗口模糊 / 遮挡覆盖")
        overlay_layout = QVBoxLayout(overlay_group)
        overlay_layout.setSpacing(6)
        self.overlay_check = QCheckBox("启用")
        self.overlay_check.setChecked(bool(self.settings.overlay_enabled))
        self.overlay_check.toggled.connect(self._on_overlay_toggled)
        # 启用开关 + 触发原因合并到一行
        trig_holder, self.overlay_stranger, self.overlay_absent = trigger_row(
            self.settings.overlay_triggers, enable_check=self.overlay_check)
        self.overlay_stranger.toggled.connect(self._on_overlay_triggers_changed)
        self.overlay_absent.toggled.connect(self._on_overlay_triggers_changed)
        overlay_layout.addWidget(trig_holder)

        self.mode = QComboBox()
        self.mode.addItem("简易模式：暗色覆盖 + 毛玻璃质感", "simple")
        self.mode.addItem("真实模糊：捕获桌面并实时模糊", "true_blur")
        self.mode.setCurrentIndex(0 if self.settings.effect_mode == "simple" else 1)
        self.mode.currentIndexChanged.connect(self.apply_settings)
        overlay_layout.addWidget(labeled("效果模式", self.mode))

        # 三个距离参数合并到一行
        self.trigger = QDoubleSpinBox()
        self.trigger.setRange(0.4, 3.0); self.trigger.setSingleStep(0.1); self.trigger.setSuffix(" 米")
        self.trigger.setValue(self.settings.trigger_distance_m)
        self.trigger.valueChanged.connect(self.apply_settings)

        self.outer = QDoubleSpinBox()
        self.outer.setRange(0.6, 5.0); self.outer.setSingleStep(0.1); self.outer.setSuffix(" 米")
        self.outer.setValue(self.settings.max_distance_m)
        self.outer.valueChanged.connect(self.apply_settings)

        self.fov = QDoubleSpinBox()
        self.fov.setRange(35, 110); self.fov.setSingleStep(1); self.fov.setSuffix("°")
        self.fov.setValue(self.settings.camera_fov_degrees)
        self.fov.valueChanged.connect(self.apply_settings)

        dist_row = QHBoxLayout()
        dist_row.addWidget(labeled("完全触发距离", self.trigger), 1)
        dist_row.addWidget(labeled("开始渐变距离", self.outer), 1)
        dist_row.addWidget(labeled("摄像头视角", self.fov), 1)
        overlay_layout.addLayout(dist_row)

        # 两个滑块合并到一行
        self.dim = QSlider(Qt.Orientation.Horizontal)
        self.dim.setRange(0, 90); self.dim.setValue(int(self.settings.max_dim*100))
        self.dim.valueChanged.connect(self.apply_settings)

        self.blur = QSlider(Qt.Orientation.Horizontal)
        self.blur.setRange(2, 40); self.blur.setValue(int(self.settings.max_blur))
        self.blur.valueChanged.connect(self.apply_settings)

        slider_row = QHBoxLayout()
        slider_row.addWidget(labeled("最大暗度", self.dim), 1)
        slider_row.addWidget(labeled("最大模糊强度", self.blur), 1)
        overlay_layout.addLayout(slider_row)
        layout.addWidget(overlay_group)

        # ========== 手段二：自动置顶指定程序 ==========
        raise_group = QGroupBox("保护手段二 · 自动置顶指定程序")
        raise_layout = QVBoxLayout(raise_group)
        raise_layout.setSpacing(6)
        self.auto_raise_check = QCheckBox("启用")
        self.auto_raise_check.setChecked(bool(self.settings.auto_raise_enabled))
        self.auto_raise_check.toggled.connect(self._on_auto_raise_toggled)
        # 启用开关 + 触发原因合并到一行
        trig_holder2, self.raise_stranger, self.raise_absent = trigger_row(
            self.settings.auto_raise_triggers, enable_check=self.auto_raise_check)
        self.raise_stranger.toggled.connect(self._on_auto_raise_triggers_changed)
        self.raise_absent.toggled.connect(self._on_auto_raise_triggers_changed)
        raise_layout.addWidget(trig_holder2)

        self.program_list = QListWidget()
        self.program_list.setMaximumHeight(110)
        raise_layout.addWidget(self.program_list)

        raise_btns = QHBoxLayout()
        self.pick_window_btn = QPushButton("点选当前窗口…")
        self.pick_window_btn.clicked.connect(self.pick_window)
        self.add_rule_btn = QPushButton("手动添加…")
        self.add_rule_btn.clicked.connect(self.add_rule_manual)
        self.remove_rule_btn = QPushButton("移除选中")
        self.remove_rule_btn.clicked.connect(self.remove_rule)
        for b in (self.pick_window_btn, self.add_rule_btn, self.remove_rule_btn):
            raise_btns.addWidget(b)
        raise_layout.addLayout(raise_btns)

        self.raise_hint = QLabel()
        self.raise_hint.setWordWrap(True)
        self.raise_hint.setStyleSheet("color:#718096;font-size:11px;")
        if not windowctl.is_supported():
            self.raise_hint.setText("当前平台暂不支持自动置顶（目前仅 Windows）。")
            for b in (self.pick_window_btn, self.add_rule_btn, self.remove_rule_btn):
                b.setEnabled(False)
        else:
            self.raise_hint.setText("规则按「进程名 + 标题关键字」匹配（两者可只填其一）。保护解除后自动还原窗口状态。")
        raise_layout.addWidget(self.raise_hint)
        self._reload_program_list()
        layout.addWidget(raise_group)

        buttons = QHBoxLayout()
        self.pause_btn = QPushButton("暂停保护")
        self.pause_btn.clicked.connect(self.toggle_protection)
        self.test_btn = QPushButton("测试覆盖层")
        self.test_btn.clicked.connect(lambda: self.overlay.set_risk(0.75))
        self.hide_btn = QPushButton("隐藏测试效果")
        self.hide_btn.clicked.connect(lambda: self.overlay.set_risk(0))
        self.quit_btn = QPushButton("退出")
        self.quit_btn.clicked.connect(self.close)
        for b in (self.pause_btn, self.test_btn, self.hide_btn, self.quit_btn):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        help_label = QLabel("快捷键：Ctrl+Shift+P 暂停/恢复；Esc 暂停。所有人脸处理均在本机完成。")
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color:#718096;font-size:11px;")
        layout.addWidget(help_label)

        # 用滚动区包裹全部内容：窗口可自由缩放，内容超高时出现滚动条。
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)  # 内容随窗口宽度自适应
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setWidget(content)
        self.setCentralWidget(self.scroll_area)

    def _on_preview_toggled(self, *args):
        """按钮点击处理：clicked 可能带 checked 参数，统一忽略，按按钮状态应用。"""
        self._apply_preview_state(initial=False)

    def _apply_preview_state(self, initial=False):
        """根据折叠状态显示/隐藏预览画面，并让窗口高度自适应。"""
        if initial:
            collapsed = bool(self.settings.preview_collapsed)
            # 阻止信号，避免恢复状态时重复写回
            self.preview_toggle.blockSignals(True)
            self.preview_toggle.setChecked(collapsed)
            self.preview_toggle.blockSignals(False)
        else:
            collapsed = self.preview_toggle.isChecked()
            self.settings.preview_collapsed = collapsed
            save_settings(self.settings)

        self.preview.setVisible(not collapsed)
        self.preview_toggle.setText("▸ 展开摄像头画面" if collapsed else "▾ 收起摄像头画面")
        if collapsed:
            # 收起：把画面高度压到 0，使其完全不占布局空间（minimumHeight 也要放开）
            self.preview.setMinimumHeight(0)
            self.preview.setMaximumHeight(0)
        else:
            # 展开：恢复画面的固定高度
            self.preview.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            self.preview.setMinimumHeight(270)
        # 窗口尺寸交给用户控制（可自由缩放），内容超高由 QScrollArea 滚动承载，
        # 不再用 adjustSize/resize 强制高度——那会覆盖用户手动调整过的尺寸。

    @contextmanager
    def _modal_space(self):
        """弹模态框前临时隐藏置顶覆盖层，避免对话框被盖住导致界面假死。

        覆盖层带 WindowStaysOnTopHint 且全屏，任何 QMessageBox / QDialog
        都会被它压在下面——用户看不到对话框，但模态事件循环已锁死主界面，
        表现就是"什么操作都点不了"。弹框期间暂时隐藏即可。
        """
        was_visible = self.overlay.isVisible()
        if was_visible:
            self.overlay.hide()
        try:
            yield
        finally:
            if was_visible:
                self.overlay.show()
                self.overlay.raise_()

    # ---------- 自动置顶程序 ----------
    @staticmethod
    def _program_label(rule: dict) -> str:
        parts = []
        if rule.get("label"):
            parts.append(rule["label"])
        bits = []
        if rule.get("process_name"):
            bits.append(f"进程:{rule['process_name']}")
        if rule.get("title_keyword"):
            bits.append(f"标题:{rule['title_keyword']}")
        detail = " / ".join(bits) if bits else "(空规则)"
        return f"{parts[0]} — {detail}" if parts else detail

    def _reload_program_list(self):
        self.program_list.clear()
        for rule in self.settings.auto_raise_programs or []:
            self.program_list.addItem(QListWidgetItem(self._program_label(rule)))

    def _save_raise_rules(self):
        save_settings(self.settings)
        self._reload_program_list()

    def _on_auto_raise_toggled(self, checked):
        self.settings.auto_raise_enabled = bool(checked)
        save_settings(self.settings)
        # 关闭时若正处于置顶状态，立即还原并复位状态位
        if not checked:
            self._raise_active = False
            self._restore_windows()

    def _on_overlay_toggled(self, checked):
        """窗口覆盖手段总开关。关闭时立即淡出覆盖层。"""
        self.settings.overlay_enabled = bool(checked)
        save_settings(self.settings)
        if not checked:
            self.overlay.set_risk(0.0)

    @staticmethod
    def _read_trigger_checks(stranger_cb, absent_cb):
        """从两个勾选框读出触发原因列表；全不勾时回退为默认（两种都触发）。"""
        triggers = []
        if stranger_cb.isChecked():
            triggers.append("stranger")
        if absent_cb.isChecked():
            triggers.append("owner_absent")
        return triggers or ["stranger", "owner_absent"]

    def _on_overlay_triggers_changed(self, *_):
        self.settings.overlay_triggers = self._read_trigger_checks(
            self.overlay_stranger, self.overlay_absent)
        save_settings(self.settings)

    def _on_auto_raise_triggers_changed(self, *_):
        self.settings.auto_raise_triggers = self._read_trigger_checks(
            self.raise_stranger, self.raise_absent)
        save_settings(self.settings)
        # 若当前正在置顶、但新规则已不含触发原因，则立即还原状态。
        # （下一帧 on_risk 也会纠正，这里主动处理让响应更即时。）
        reason_now = getattr(self, "_last_reason_key", None)
        if (self._raise_active and reason_now
                and reason_now not in self.settings.auto_raise_triggers):
            self._raise_active = False
            self._restore_windows()

    def pick_window(self):
        """弹出对话框列出当前窗口，用户点选后添加为置顶规则。"""
        import os
        windows = [w for w in windowctl.list_windows() if w.pid != os.getpid()]
        if not windows:
            with self._modal_space():
                QMessageBox.information(self, "点选窗口", "未检测到可选择的窗口（或当前平台不支持）。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("选择要置顶的窗口")
        dialog.resize(560, 420)
        dlg_layout = QVBoxLayout(dialog)
        dlg_layout.addWidget(QLabel("当前打开的窗口（选择后按确定）："))
        lst = QListWidget()
        for w in windows:
            name = w.process_name or f"pid:{w.pid}"
            lst.addItem(QListWidgetItem(f"{name} — {w.title}"))
        dlg_layout.addWidget(lst)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)
        with self._modal_space():
            result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            return
        row = lst.currentRow()
        if row < 0:
            return
        w = windows[row]
        rule = {"label": w.title[:40], "process_name": w.process_name, "title_keyword": ""}
        self.settings.auto_raise_programs.append(rule)
        self._save_raise_rules()
        with self._modal_space():
            QMessageBox.information(self, "已添加", f"已添加规则：\n{self._program_label(rule)}")

    def add_rule_manual(self):
        """手动输入进程名 / 标题关键字添加规则。"""
        from PySide6.QtWidgets import QInputDialog
        with self._modal_space():
            proc, ok = QInputDialog.getText(
                self, "手动添加规则", "进程名（如 chrome.exe，可留空）：")
            if not ok:
                return
            title, ok = QInputDialog.getText(
                self, "手动添加规则", "窗口标题关键字（可留空）：")
            if not ok:
                return
        proc, title = proc.strip(), title.strip()
        if not proc and not title:
            with self._modal_space():
                QMessageBox.warning(self, "手动添加", "进程名和标题关键字至少填一个。")
            return
        rule = {"label": proc or title, "process_name": proc, "title_keyword": title}
        self.settings.auto_raise_programs.append(rule)
        self._save_raise_rules()

    def remove_rule(self):
        row = self.program_list.currentRow()
        if row < 0:
            return
        if 0 <= row < len(self.settings.auto_raise_programs):
            self.settings.auto_raise_programs.pop(row)
            self._save_raise_rules()

    def _restore_windows(self):
        if self._raised_snapshot:
            windowctl.restore(self._raised_snapshot)
            self._raised_snapshot = []

    def _on_protection_activated(self):
        """执行置顶：把命中规则的窗口切到最上层。

        是否该置顶（开关 + 触发原因判断）已由 on_risk 决定，这里只负责执行。
        """
        self._restore_windows()  # 先还原上一次，避免残留
        self._raised_snapshot = windowctl.raise_matching(self.settings.auto_raise_programs or [])

    def _on_protection_deactivated(self):
        """还原被置顶窗口的原始状态。"""
        self._restore_windows()

    def _start_worker(self):
        self.worker = VisionWorker(self.settings)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.state_changed.connect(self.status.setText)
        self.worker.risk_changed.connect(self.on_risk)
        self.worker.enrollment_progress.connect(self.on_enrollment_progress)
        self.worker.enrollment_done.connect(self.on_enrollment_done)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def on_frame(self, rgb):
        # 画面收起时跳过缩放与贴图，省 CPU；后台识别照常进行
        if self.settings.preview_collapsed:
            return
        h, w, c = rgb.shape
        image = QImage(rgb.data, w, h, c*w, QImage.Format.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def on_risk(self, risk, distance, count, reason=0):
        # reason: 0=检测到陌生人, 1=主人离开（缺席）
        reason_key = "owner_absent" if reason == 1 else "stranger"
        self._last_reason_key = reason_key
        if reason == 1:
            self.risk_label.setText(f"隐私风险：{risk*100:.0f}% · 未检测到机主（触发：主人离开）")
        elif count:
            self.risk_label.setText(f"隐私风险：{risk*100:.0f}% · 陌生人：{count} · 最近距离约 {distance:.2f} 米")
        else:
            self.risk_label.setText("隐私风险：0% · 未检测到陌生人")

        # 是否处于"有效的保护触发状态"：risk>0 才说明当前有触发原因生效
        active = risk > 0.0 and self.settings.protection_enabled

        # ---- 手段一：窗口模糊覆盖 ----
        # 独立于置顶；仅当该手段启用 且 当前原因在其触发规则内时才工作。
        overlay_on = (active and self.settings.overlay_enabled
                      and reason_key in self.settings.overlay_triggers)
        self.overlay.set_risk(risk if overlay_on else 0.0)

        # ---- 手段二：自动置顶 ----
        # 由 vision 的实时 reason 直接驱动，不再依赖覆盖层的平滑信号，
        # 因此即使关闭了窗口模糊覆盖、或覆盖层因故未显示，置顶仍能独立工作。
        raise_on = (active and self.settings.auto_raise_enabled
                    and reason_key in self.settings.auto_raise_triggers)
        if raise_on != self._raise_active:
            self._raise_active = raise_on
            if raise_on:
                self._on_protection_activated()
            else:
                self._on_protection_deactivated()

    def on_enrollment_progress(self, current, total):
        self.progress.setVisible(True)
        self.progress.setMaximum(total)
        self.progress.setValue(current)
        self.status.setText(f"正在录入机主人脸：{current}/{total}")

    def on_enrollment_done(self, embedding):
        self.settings.owner_embedding = embedding
        save_settings(self.settings)
        self.owner_label.setText("已注册机主")
        self.progress.setVisible(False)
        self.worker.update_settings(self.settings)
        with self._modal_space():
            QMessageBox.information(self, "录入完成", "机主人脸已保存在本地。")

    def on_error(self, message):
        self.status.setText("错误：" + message)
        with self._modal_space():
            QMessageBox.warning(self, "PrivacyGuard AI", message)

    def enroll_owner(self):
        if self.worker and self.worker.isRunning():
            self.progress.setVisible(True)
            self.progress.setValue(0)
            self.worker.start_enrollment(20)

    def apply_settings(self, *_):
        self.settings.effect_mode = self.mode.currentData()
        self.settings.trigger_distance_m = self.trigger.value()
        self.settings.max_distance_m = max(self.outer.value(), self.trigger.value() + 0.1)
        self.settings.camera_fov_degrees = self.fov.value()
        self.settings.max_dim = self.dim.value() / 100.0
        self.settings.max_blur = float(self.blur.value())
        save_settings(self.settings)
        self.overlay.update_settings(self.settings)
        if self.worker:
            self.worker.update_settings(self.settings)

    def toggle_protection(self):
        self.settings.protection_enabled = not self.settings.protection_enabled
        save_settings(self.settings)
        self.overlay.set_enabled(self.settings.protection_enabled)
        # 全局暂停时立即还原置顶，不等下一帧的 on_risk
        if not self.settings.protection_enabled and self._raise_active:
            self._raise_active = False
            self._restore_windows()
        self.pause_btn.setText("暂停保护" if self.settings.protection_enabled else "恢复保护")

    def pause_protection(self):
        if self.settings.protection_enabled:
            self.toggle_protection()

    def closeEvent(self, event):
        self._restore_windows()  # 退出前还原被置顶的窗口
        self.overlay.hide()
        if self.worker:
            self.worker.stop()
        event.accept()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PrivacyGuard AI")
    window = MainWindow()
    window.show()
    return app.exec()
