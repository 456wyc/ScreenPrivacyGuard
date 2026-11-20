from __future__ import annotations
import time, urllib.request
from pathlib import Path
import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from .config import APP_DIR, MODEL_DIR, Settings

YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
SFACE_URL = "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
YUNET_FILE = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_FILE = MODEL_DIR / "face_recognition_sface_2021dec.onnx"

def _download(url: str, path: Path):
    if path.exists() and path.stat().st_size > 100_000:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "PrivacyGuardAI/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as out:
        out.write(response.read())
    tmp.replace(path)

def ensure_models():
    _download(YUNET_URL, YUNET_FILE)
    _download(SFACE_URL, SFACE_FILE)

class VisionWorker(QThread):
    frame_ready = Signal(object)
    state_changed = Signal(str)
    # risk 0..1, nearest distance, stranger count, reason: 0=陌生人 1=主人缺席
    risk_changed = Signal(float, float, int, int)
    enrollment_progress = Signal(int, int)
    enrollment_done = Signal(object)
    error = Signal(str)

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self._running = True
        self._enroll_target = 0
        self._enroll_features = []
        self._owner = np.asarray(settings.owner_embedding, dtype=np.float32) if settings.owner_embedding else None
        self._last_distance = None
        self._last_emit = 0.0
        self._last_preview_emit = 0.0
        self._last_seen_ts = 0.0    # 最近一次检测到陌生人的时间（用于漏检宽限）
        self._last_owner_ts = 0.0   # 最近一次看到主人的时间（用于主人缺席宽限）

    def stop(self):
        self._running = False
        self.wait(2500)

    def start_enrollment(self, count=20):
        self._enroll_features = []
        self._enroll_target = count
        self.state_changed.emit("正在录入机主人脸：请正对摄像头，保持自然")
        self.enrollment_progress.emit(0, count)

    def update_settings(self, settings: Settings):
        self.settings = settings
        if settings.owner_embedding:
            self._owner = np.asarray(settings.owner_embedding, dtype=np.float32)

    def _decide_risk(self, stranger_distances, owner_present, now):
        """根据当前帧的陌生人/主人状态决定保护风险。

        返回 (risk, nearest, reason)，reason: 0=陌生人触发, 1=主人缺席触发。
        抽成独立方法便于单测，核心策略：
          - 有陌生人 → 按距离计算（最精确）
          - 无陌生人 + 已录入主人 + 主人缺席超过宽限 → fail-closed 触发
          - 其余 → 原有的漏检宽限，避免闪烁
        """
        # 关键护栏：未注册主人时完全不保护。
        # 否则未注册阶段画面里的任何人（包括主人本人）都会被当成陌生人 →
        # 启动即全屏保护 → 遮住"注册人脸"按钮 → 永远无法注册（死锁）。
        # 录入过程中 _owner 也是 None，此期间不保护符合预期；
        # 录入完成 _owner 被赋值后护栏自动解除。
        if self._owner is None:
            return 0.0, 99.0, 0

        # 看到主人就刷新时间戳，作为判断"是否已离开"的基准
        if owner_present:
            self._last_owner_ts = now

        trigger = max(0.3, self.settings.trigger_distance_m)
        outer = max(trigger + 0.1, self.settings.max_distance_m)

        if stranger_distances:
            self._last_seen_ts = now
            nearest = min(stranger_distances)
            risk = float(np.clip((outer - nearest) / (outer - trigger), 0.0, 1.0))
            if nearest <= trigger:
                risk = 1.0
            # 距离单帧抖动做轻度平滑
            if self._last_distance is None:
                self._last_distance = nearest
            else:
                self._last_distance += 0.35 * (nearest - self._last_distance)
            return risk, nearest, 0

        # 没有陌生人：判断是否主人缺席（fail-closed 核心）
        owner_registered = self._owner is not None
        protect_absent = (getattr(self.settings, "protect_when_owner_absent", True)
                          and owner_registered)
        absent_grace = getattr(self.settings, "owner_absent_grace", 1.2)
        owner_absent = (protect_absent and not owner_present
                        and (not self._last_owner_ts
                             or (now - self._last_owner_ts) > absent_grace))

        if owner_absent:
            # 主人离开座位：即使画面里没人也保持保护
            self._last_distance = None
            risk = float(np.clip(getattr(self.settings, "owner_absent_risk", 0.85), 0.0, 1.0))
            return risk, 0.0, 1

        # 未缺席：原有陌生人漏检宽限，丢帧时不立刻清零，避免闪烁
        grace = getattr(self.settings, "stranger_miss_grace", 0.45)
        if self._last_seen_ts and (now - self._last_seen_ts) <= grace:
            nearest = self._last_distance if self._last_distance is not None else 99.0
            risk = float(np.clip((outer - nearest) / (outer - trigger), 0.0, 1.0))
            if nearest <= trigger:
                risk = 1.0
        else:
            nearest, risk = 99.0, 0.0
            self._last_distance = None
        return risk, nearest, 0

    def run(self):
        cap = None
        try:
            ensure_models()
            detector = cv2.FaceDetectorYN.create(str(YUNET_FILE), "", (320, 320),
                                                 self.settings.detection_confidence, 0.3, 5000)
            recognizer = cv2.FaceRecognizerSF.create(str(SFACE_FILE), "")
            cap = cv2.VideoCapture(self.settings.camera_index)
            if not cap.isOpened():
                raise RuntimeError("无法打开摄像头。请检查摄像头连接、系统权限或 camera_index。")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.state_changed.emit("摄像头已启动")
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                h, w = frame.shape[:2]
                detector.setInputSize((w, h))
                _, faces = detector.detect(frame)
                stranger_distances = []
                stranger_count = 0
                owner_present = False
                if faces is not None:
                    for face in faces:
                        x, y, fw, fh = [float(v) for v in face[:4]]
                        aligned = recognizer.alignCrop(frame, face)
                        feature = recognizer.feature(aligned).flatten().astype(np.float32)
                        norm = np.linalg.norm(feature)
                        if norm:
                            feature /= norm
                        if self._enroll_target > 0:
                            self._enroll_features.append(feature.copy())
                            n = len(self._enroll_features)
                            self.enrollment_progress.emit(n, self._enroll_target)
                            if n >= self._enroll_target:
                                avg = np.mean(np.stack(self._enroll_features), axis=0)
                                avg /= max(np.linalg.norm(avg), 1e-8)
                                self._owner = avg.astype(np.float32)
                                self._enroll_target = 0
                                # 录入完成瞬间主人在镜头前，刷新时间戳避免误判缺席
                                self._last_owner_ts = time.monotonic()
                                self.enrollment_done.emit(self._owner.tolist())
                                self.state_changed.emit("机主人脸录入完成")
                        is_owner = False
                        if self._owner is not None:
                            score = float(np.dot(self._owner, feature))
                            is_owner = score >= self.settings.recognition_threshold
                        if is_owner:
                            owner_present = True
                        else:
                            stranger_count += 1
                            face_width = max(fw, 1.0)
                            fov = np.deg2rad(np.clip(self.settings.camera_fov_degrees, 35, 110))
                            focal_px = w / (2.0 * np.tan(fov / 2.0))
                            distance = self.settings.assumed_face_width_m * focal_px / face_width
                            stranger_distances.append(float(np.clip(distance, 0.2, 8.0)))
                        # Draw a subtle local preview box
                        color = (70, 210, 90) if is_owner else (40, 150, 245)
                        cv2.rectangle(frame, (int(x), int(y)), (int(x+fw), int(y+fh)), color, 2)
                        cv2.putText(frame, "OWNER" if is_owner else "VISITOR",
                                    (int(x), max(18, int(y)-8)), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.55, color, 2)
                now = time.monotonic()
                risk, nearest, reason = self._decide_risk(stranger_distances, owner_present, now)

                # 按固定时间间隔发射，频率稳定，避免时序抖动
                if now - self._last_emit > 0.08:
                    self.risk_changed.emit(risk, nearest, stranger_count, reason)
                    self._last_emit = now
                # 预览帧按时间节流（约 8fps），保持 CPU 占用适度
                if now - self._last_preview_emit > 0.125:
                    rgb = cv2.cvtColor(cv2.resize(frame, (480, 360)), cv2.COLOR_BGR2RGB)
                    self.frame_ready.emit(rgb)
                    self._last_preview_emit = now
                time.sleep(0.015)
        except Exception as exc:
            self.error.emit(str(exc))
            self.state_changed.emit("摄像头/模型启动失败")
        finally:
            if cap is not None:
                cap.release()
