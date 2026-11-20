from __future__ import annotations
import sys
import numpy as np
import cv2
import mss
from PySide6.QtCore import Qt, QTimer, QRect, Signal
from PySide6.QtGui import QPainter, QColor, QPixmap, QImage, QGuiApplication
from PySide6.QtWidgets import QWidget, QApplication

# Windows: WDA_EXCLUDEFROMCAPTURE 让本窗口不进入所有屏幕捕获（截屏/录屏/mss）。
# 这样抓屏时覆盖层不会截到自己，也就不必再 hide/show 窗口 —— 从根源消除闪烁。
_WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _set_exclude_from_capture(widget, enable: bool) -> bool:
    """尽力为本窗口设置/取消"排除捕获"。成功返回 True。非 Windows 直接返回 False。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        value = _WDA_EXCLUDEFROMCAPTURE if enable else 0x0  # WDA_NONE
        return bool(user32.SetWindowDisplayAffinity(hwnd, value))
    except Exception:
        return False


# Windows: 给窗口加 WS_EX_TRANSPARENT / WS_EX_NOACTIVATE 扩展样式，实现系统级
# 鼠标穿透（点击直接落到下层窗口）。这比 Qt 的 WA_TransparentForMouseEvents
# 和 WM_NCHITTEST->HTTRANSPARENT 都可靠，是覆盖层不吞鼠标的主力方案。
_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_NOACTIVATE = 0x08000000


def _set_click_through(widget, enable: bool) -> bool:
    """设置/取消窗口的鼠标穿透扩展样式。非 Windows 返回 False。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        flags = _WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE
        new_style = (style | flags) if enable else (style & ~flags)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, new_style)
        return True
    except Exception:
        return False


# Windows 消息结构：用于全屏覆盖层的鼠标穿透（WM_NCHITTEST -> HTTRANSPARENT）
if sys.platform.startswith("win"):
    import ctypes

    class _WinMSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint32),
            ("wParam", ctypes.c_size_t),
            ("lParam", ctypes.c_ssize_t),
            ("time", ctypes.c_uint32),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]
else:
    _WinMSG = None


class PrivacyOverlay(QWidget):
    """全屏隐私保护覆盖层（渐进式防闪烁版）。

    相比"隐藏窗口再抓屏"的旧方案，这里做了三件事让变化连续、不再突变：

    1. 排除捕获：Windows 上用 WDA_EXCLUDEFROMCAPTURE 让覆盖层不进入屏幕捕获，
       抓屏时无需 hide/show —— 窗口全程都在，周期性黑闪从根上消失。
       抓到新图后只在后台替换像素，画面始终是连续的。
       （非 Windows 平台回退到极短的隐藏方式，仍保持平滑过渡。）

    2. 渐进淡入：引入 visual_risk，真实模糊首帧就绪前画面从 0 平滑升起，
       避免保护刚触发时"啪"地整屏突变；退出时同样平滑淡出。

    3. 连续强度：模糊用固定高斯核 + 连续 sigma 控制强度，暗化用连续 alpha，
       强度随风险连续变化，不会因 kernel 取整而一档一档地跳。

    风险本身仍用非对称平滑（升快降慢）+ 显示迟滞，吸收检测抖动。
    """

    # 保护状态（迟滞后的逻辑可见性）发生变化时发出，供自动置顶等功能联动
    protection_activated = Signal()
    protection_deactivated = Signal()

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._target_risk = 0.0        # 识别线程给出的目标风险
        self._risk = 0.0               # 平滑后的逻辑风险
        self._visual_risk = 0.0        # 用于绘制的显示风险（负责渐进淡入淡出）
        self._logical_visible = False  # 迟滞后的"是否处于保护中"
        self.enabled = True
        self._pixmap = None
        self._capturing = False
        self._capture_ok = False       # 抓屏是否曾经成功（决定是否允许淡入）
        self._exclude_ok = False       # 是否成功让窗口排除捕获

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 全屏透明层不拦截鼠标，不影响用户正常使用其它窗口
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        screen = QGuiApplication.primaryScreen()
        self.setGeometry(screen.geometry() if screen else QRect(0, 0, 1920, 1080))

        # 动画定时器：驱动风险平滑逼近（约 30fps）
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(33)

        # 抓屏定时器：仅在真实模糊模式启用
        self._capture_timer = QTimer(self)
        self._capture_timer.timeout.connect(self._capture_blurred_screen)

        # 系统级鼠标穿透：覆盖层永不吞掉下层窗口的点击。
        self._click_through_ok = _set_click_through(self, True)
        # 初始隐藏：没有保护时覆盖层根本不出现在屏幕上，物理上不可能拦截鼠标。
        # 只在真正进入保护（_logical_visible）时才 show，淡出后再 hide。
        self.hide()
        # 关键：构造后立即按初始设置初始化一次。
        # 抓屏定时器只在 update_settings 里启动；若等到用户第一次改设置才调用，
        # 启动后的 true_blur 模式将永远抓不到首帧、保护画面无法显示。
        self.update_settings(self.settings)

    def update_settings(self, settings):
        self.settings = settings
        if settings.effect_mode == "true_blur":
            # 模式切换后尝试重新设置（窗口句柄此时已稳定）
            self._exclude_ok = _set_exclude_from_capture(self, True)
            if not self._capture_timer.isActive():
                interval = max(60, int(getattr(self.settings, "capture_interval_ms", 160)))
                self._capture_timer.start(interval)
        else:
            self._capture_timer.stop()
            self._pixmap = None
            self._capture_ok = False
            # 简易模式不需要抓屏，取消排除捕获，让用户正常截屏能看到遮罩
            if self._exclude_ok:
                _set_exclude_from_capture(self, False)
                self._exclude_ok = False

    def nativeEvent(self, eventType, message):
        """Windows 下让全屏置顶层对鼠标完全透明。

        WA_TransparentForMouseEvents 对 Qt 顶层窗口并不可靠（尤其带
        WindowStaysOnTopHint + Tool 时），会吞掉下层主界面的点击，导致
        "什么按钮都点不了"。这里直接处理原生 WM_NCHITTEST，返回
        HTTRANSPARENT，让系统把鼠标事件交给下面的窗口。
        """
        if _WinMSG is not None:
            try:
                etype = eventType
                if isinstance(etype, bytes):
                    is_generic = etype in (b"windows_generic_MSG", b"windows_dispatcher_MSG")
                else:
                    is_generic = str(etype) in ("windows_generic_MSG", "windows_dispatcher_MSG")
                if is_generic:
                    msg = ctypes.cast(int(message), ctypes.POINTER(_WinMSG)).contents
                    if msg.message == 0x0084:  # WM_NCHITTEST
                        return True, -1  # HTTRANSPARENT: 鼠标事件穿透到下层窗口
            except Exception:
                pass
        # 未处理的消息交给 Qt 默认流程。返回 False 表示"未消费"，
        # 不直接调 super().nativeEvent()，避免在无原生分发器的环境抛异常。
        return False, 0

    def set_risk(self, risk):
        """识别线程只更新目标值，视觉过渡由 _tick 完成。"""
        self._target_risk = float(np.clip(risk, 0.0, 1.0))

    def set_enabled(self, enabled):
        self.enabled = enabled
        if not enabled:
            self._target_risk = 0.0  # 平滑降到 0，淡出而非突然消失

    def _effective_target(self):
        if not self.enabled or not self.settings.protection_enabled:
            return 0.0
        return self._target_risk

    @staticmethod
    def _approach(current: float, target: float, rise: float, fall: float) -> float:
        diff = target - current
        if abs(diff) <= 1e-4:
            return target
        alpha = rise if diff > 0 else fall
        return current + diff * alpha

    def _tick(self):
        # 逻辑风险：非对称平滑（升快降慢），吸收检测抖动
        self._risk = self._approach(
            self._risk, self._effective_target(),
            self.settings.risk_rise_alpha, self.settings.risk_fall_alpha)

        # 迟滞：上升超过 show、下降低于 hide 才切换保护状态，
        # 避免真实模糊抓屏在临界点反复启停。
        show_th = self.settings.show_threshold
        hide_th = min(self.settings.hide_threshold, show_th - 0.01)
        if not self._logical_visible and self._risk >= show_th:
            self._logical_visible = True
            self._capture_ok = False  # 新一轮保护，等首帧就绪后再淡入
            # 进入保护：先把窗口显示出来（此时 visual_risk 接近 0，是全透明的），
            # 随后由 visual_risk 的淡入逐渐呈现，不会出现硬跳变。
            if not self.isVisible():
                self.show()
                self.raise_()
            self.protection_activated.emit()
        elif self._logical_visible and self._risk <= hide_th:
            self._logical_visible = False
            self._pixmap = None
            self._capture_ok = False
            self.protection_deactivated.emit()

        # 显示风险：决定画面透明度。真实模糊模式下，首帧抓到前目标按 0 处理，
        # 这样画面从无到有是平滑升起，而不是瞬间铺满。
        visual_target = self._risk
        if self.settings.effect_mode == "true_blur" and not self._capture_ok:
            visual_target = 0.0
        self._visual_risk = self._approach(
            self._visual_risk, visual_target,
            self.settings.risk_rise_alpha, self.settings.risk_fall_alpha)

        if self._visual_risk > 0.001:
            self.update()
        elif not self._logical_visible and self.isVisible():
            # 淡出已彻底完成且不在保护中：隐藏窗口。
            # 这样空闲时覆盖层完全不存在于屏幕上，绝不会拦截主界面的鼠标。
            self.hide()

    def _capture_blurred_screen(self):
        if self.settings.effect_mode != "true_blur":
            return
        if (not self._logical_visible or not self.enabled
                or self._risk <= self.settings.hide_threshold):
            return
        if self._capturing:
            return
        self._capturing = True

        # 若已排除捕获，窗口无需隐藏；否则回退到短暂隐藏（非 Windows）。
        hide_needed = not self._exclude_ok
        was_visible = self.isVisible()
        try:
            if hide_needed and was_visible:
                self.hide()
                sync = getattr(QApplication, "sync", None)
                if callable(sync):
                    sync()
                # 极短等待让窗口系统完成隐藏；仅在无排除捕获的平台发生
                import time
                time.sleep(0.004)

            with mss.mss() as sct:
                monitor = sct.monitors[1]  # 主显示器
                shot = np.asarray(sct.grab(monitor))

            bgr = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)

            # 先缩小 → 模糊 → 放大，降低计算量。
            h, w = bgr.shape[:2]
            scale = 4
            small = cv2.resize(bgr, (max(1, w // scale), max(1, h // scale)),
                               interpolation=cv2.INTER_AREA)

            # 固定核 + 连续 sigma：模糊强度平滑变化，不会因 kernel 取整而跳档。
            ksize = max(3, int(getattr(self.settings, "blur_kernel_size", 41)) | 1)
            strength = max(0.0, float(self.settings.max_blur) * self._risk)
            # max_blur 上限约 40，映射到 sigma；下限保证最小的柔化效果
            sigma = max(0.6, strength * 0.55)
            blurred = cv2.GaussianBlur(small, (ksize, ksize), sigma)
            blurred = cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(blurred, cv2.COLOR_BGR2RGB)
            image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
            self._pixmap = QPixmap.fromImage(image)
            self._capture_ok = True
        except Exception:
            # 抓屏失败时保留上一帧 pixmap 与成功标志，避免画面突然清空/变黑
            pass
        finally:
            if hide_needed and was_visible:
                self.show()
                self.raise_()
            self._capturing = False

    def paintEvent(self, event):
        if self._visual_risk <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.settings.effect_mode == "true_blur" and self._pixmap is not None:
            painter.drawPixmap(self.rect(), self._pixmap)
        # 两种模式都叠加暗色遮罩，强度随渐进后的显示风险连续变化
        alpha = int(255 * self.settings.max_dim * self._visual_risk)
        painter.fillRect(self.rect(), QColor(0, 0, 0, alpha))
        if self.settings.effect_mode == "simple":
            painter.fillRect(self.rect(), QColor(205, 218, 230, int(22 * self._visual_risk)))
        painter.end()
