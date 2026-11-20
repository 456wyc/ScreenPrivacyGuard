from __future__ import annotations
import sys
from dataclasses import dataclass

"""
跨平台窗口控制：枚举当前窗口、按规则匹配、置顶与恢复。

目前 Windows 通过 ctypes 调用 Win32 API 完整实现；其它平台优雅降级
（枚举返回空、置顶为空操作），保证应用不因平台差异而崩溃。

置顶语义：
- “置顶”指把窗口设为 HWND_TOPMOST，使其位于所有普通窗口之上。
- 恢复时会还原窗口原本是否置顶（WS_EX_TOPMOST），避免破坏用户自己
  设置的置顶状态。
"""

IS_WINDOWS = sys.platform.startswith("win")


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    pid: int


def is_supported() -> bool:
    """当前平台是否支持自动置顶。"""
    return IS_WINDOWS


def list_windows() -> list[WindowInfo]:
    """列出当前可见、有标题的顶层窗口。非 Windows 返回空列表。"""
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    result: list[WindowInfo] = []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _process_name(pid: int) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if ok:
                import os
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = _process_name(pid.value) if pid.value else ""
        result.append(WindowInfo(int(hwnd), title, name, int(pid.value)))
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return result


def _matches(info: WindowInfo, rule: dict) -> bool:
    """判断窗口是否命中一条置顶规则（进程名与标题关键字为「与」关系）。

    规则字段：process_name（进程名，如 chrome.exe）、title_keyword（标题关键字）。
    两者可只填其一；都为空视为不匹配，避免误伤所有窗口。
    """
    proc = (rule.get("process_name") or "").strip().lower()
    title_kw = (rule.get("title_keyword") or "").strip().lower()
    if not proc and not title_kw:
        return False
    if proc and proc not in (info.process_name or "").lower():
        return False
    if title_kw and title_kw not in (info.title or "").lower():
        return False
    return True


def _configure_user32(user32):
    """声明 Win32 函数的参数/返回类型。

    关键：HWND 在 64 位 Windows 上是 64 位值。若用默认 ctypes 调用并直接传
    Python int -1（HWND_TOPMOST），会被当作 32 位 0xFFFFFFFF 传入，导致
    SetWindowPos 静默失败（返回 0、GetLastError 也是 0），置顶永远不生效。
    声明 argtypes 后，ctypes 会按指针宽度正确转换特殊句柄。
    """
    from ctypes import wintypes
    import ctypes as _ctypes
    # HWND 用 c_void_p（随指针宽度自动 32/64 位）；特殊值 HWND_TOPMOST=-1 /
    # HWND_NOTOPMOST=-2 经 c_void_p 转换后分别变成 0xFFFF...FFFF / ...FFFE。
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                    _ctypes.c_int, _ctypes.c_int,
                                    _ctypes.c_int, _ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, _ctypes.c_int]
    user32.GetWindowLongW.restype = _ctypes.c_long
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL


def raise_matching(rules: list[dict]) -> list[dict]:
    """把所有命中任一规则的窗口置顶，返回已置顶窗口的快照（供恢复使用）。

    每个快照包含 hwnd 与 was_topmost（原本是否已置顶）。
    """
    if not IS_WINDOWS or not rules:
        return []
    import ctypes

    user32 = ctypes.windll.user32
    _configure_user32(user32)
    HWND_TOPMOST = -1  # 经 argtypes(HWND=c_void_p) 正确转换为 64 位
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008

    raised: list[dict] = []
    seen: set[int] = set()
    for info in list_windows():
        if not any(_matches(info, rule) for rule in rules):
            continue
        if info.hwnd in seen:
            continue
        seen.add(info.hwnd)
        try:
            ex_style = user32.GetWindowLongW(info.hwnd, GWL_EXSTYLE)
            was_topmost = bool(ex_style & WS_EX_TOPMOST)
            ok = user32.SetWindowPos(info.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if not ok:
                # 置顶失败不记入快照，避免误还原其它窗口
                continue
            raised.append({"hwnd": info.hwnd, "was_topmost": was_topmost,
                           "title": info.title, "process_name": info.process_name})
        except Exception:
            # 单个窗口失败不影响其它窗口
            continue
    return raised


def restore(snapshot: list[dict]) -> None:
    """根据 raise_matching 返回的快照还原窗口的置顶状态。"""
    if not IS_WINDOWS or not snapshot:
        return
    import ctypes

    user32 = ctypes.windll.user32
    _configure_user32(user32)
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010

    for item in snapshot:
        hwnd = item.get("hwnd")
        if not hwnd:
            continue
        try:
            if not user32.IsWindow(hwnd):
                continue  # 窗口已关闭
            insert_after = HWND_TOPMOST if item.get("was_topmost") else HWND_NOTOPMOST
            user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            continue
