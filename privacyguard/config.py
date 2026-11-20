from __future__ import annotations
import json
from pathlib import Path
from dataclasses import dataclass, asdict

APP_DIR = Path.home() / ".privacyguard-ai"
MODEL_DIR = APP_DIR / "models"
CONFIG_PATH = APP_DIR / "config.json"

@dataclass
class Settings:
    camera_index: int = 0
    trigger_distance_m: float = 1.5
    max_distance_m: float = 2.5
    camera_fov_degrees: float = 65.0
    assumed_face_width_m: float = 0.145
    effect_mode: str = "simple"  # simple | true_blur
    max_dim: float = 0.72        # 0..0.9
    max_blur: float = 24.0       # Gaussian kernel radius-like strength
    detection_confidence: float = 0.75
    recognition_threshold: float = 0.38
    protection_enabled: bool = True
    owner_embedding: list[float] | None = None

    # --- 显示平滑 / 防闪烁参数 ---
    # 风险上升快、下降慢：陌生人靠近时迅速保护，离开时缓慢淡出，避免边缘抖动
    risk_rise_alpha: float = 0.45   # 每帧向目标靠近的比例（上升）
    risk_fall_alpha: float = 0.12   # 每帧向目标靠近的比例（下降）
    show_threshold: float = 0.12    # 覆盖层开始出现的风险阈值
    hide_threshold: float = 0.05    # 覆盖层完全隐藏的风险阈值（低于 show，形成迟滞）
    stranger_miss_grace: float = 0.45  # 陌生人漏检后，风险维持的宽限秒数
    preview_collapsed: bool = False    # 摄像头预览画面是否收起（状态会被记住）

    # --- 真实模糊模式的画质 / 流畅度 ---
    capture_interval_ms: int = 160     # 抓屏间隔；越小越跟手（依赖排除捕获，不会闪烁）
    blur_kernel_size: int = 41         # 高斯核尺寸（固定），模糊强度靠连续 sigma 调节
    exclude_from_capture: bool = True  # Windows: 让覆盖层不进入屏幕捕获，从而无需隐藏窗口

    # --- 主人缺席保护（fail-closed：没看到主人就保护）---
    protect_when_owner_absent: bool = True   # 已录入主人后，画面中没有主人时也开启保护
    owner_absent_risk: float = 0.85          # 无主人且无陌生人时的保护强度(0..1)
    owner_absent_grace: float = 1.2          # 主人短暂消失的宽限秒数（低头/遮挡不误触发）

    # --- 两种保护手段的触发原因（多选）---
    # reason 取值：stranger=检测到陌生人；owner_absent=主人离开（已录入前提下）
    # 窗口模糊覆盖：是否启用 + 在哪些原因下触发
    overlay_enabled: bool = True
    overlay_triggers: list = None       # 默认 ["stranger", "owner_absent"]
    # 自动置顶：是否启用 + 在哪些原因下触发
    auto_raise_enabled: bool = False
    auto_raise_triggers: list = None    # 默认 ["stranger", "owner_absent"]
    auto_raise_programs: list = None    # 规则列表，每项 {"label","process_name","title_keyword"}

def load_settings() -> Settings:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            allowed = Settings.__dataclass_fields__.keys()
            # 只保留 dataclass 中存在的字段；缺失字段自动用默认值补齐，
            # 这样新增参数不会让旧配置文件失效。
            kwargs = {k: v for k, v in raw.items() if k in allowed}
            settings = Settings(**kwargs)
            _normalize(settings)
            return settings
        except Exception:
            pass
    settings = Settings()
    _normalize(settings)
    return settings


VALID_TRIGGERS = ("stranger", "owner_absent")
DEFAULT_TRIGGERS = ["stranger", "owner_absent"]


def _clean_triggers(value) -> list:
    """清洗触发原因列表：只保留合法值、去重；全空则回退到默认（两种都触发）。"""
    if not isinstance(value, list):
        return list(DEFAULT_TRIGGERS)
    cleaned = []
    for item in value:
        if isinstance(item, str) and item in VALID_TRIGGERS and item not in cleaned:
            cleaned.append(item)
    return cleaned if cleaned else list(DEFAULT_TRIGGERS)


def _normalize(settings: Settings) -> None:
    """修正反序列化后的字段类型，保证可变字段独立且类型正确。"""
    settings.overlay_triggers = _clean_triggers(settings.overlay_triggers)
    settings.auto_raise_triggers = _clean_triggers(settings.auto_raise_triggers)
    if not isinstance(settings.auto_raise_programs, list):
        settings.auto_raise_programs = []
    else:
        # 过滤掉格式非法的条目，保证每条至少有一个匹配字段
        cleaned = []
        for item in settings.auto_raise_programs:
            if not isinstance(item, dict):
                continue
            proc = str(item.get("process_name", "") or "").strip()
            title = str(item.get("title_keyword", "") or "").strip()
            if not proc and not title:
                continue
            cleaned.append({"label": str(item.get("label", "") or "").strip(),
                            "process_name": proc, "title_keyword": title})
        settings.auto_raise_programs = cleaned

def save_settings(settings: Settings) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
