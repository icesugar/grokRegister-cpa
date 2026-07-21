"""邮箱提供商共用工具。"""

from __future__ import annotations

import re
import secrets
import string
from typing import Any, List, Optional


def generate_username(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(max(3, length)))


def pick_list_payload(data: Any) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return [item for item in data["results"] if isinstance(item, dict)]
        if isinstance(data.get("hydra:member"), list):
            return [item for item in data["hydra:member"] if isinstance(item, dict)]
        if isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
        if isinstance(data.get("messages"), list):
            return [item for item in data["messages"] if isinstance(item, dict)]
        if isinstance(data.get("data"), dict):
            nested = data.get("data") or {}
            if isinstance(nested.get("messages"), list):
                return [item for item in nested["messages"] if isinstance(item, dict)]
    return []


_XAI_CODE_RE = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", re.IGNORECASE)
# 邮件 HTML/CSS 常见误匹配：per-100 / max-600 / 2xx-4xx 等，不能当验证码
_FAKE_CODE_RE = re.compile(
    r"^(?:"
    r"per|max|min|pad|gap|top|left|right|em|rem|px|vh|vw|"
    r"rgb|hsl|css|div|span|img|src|alt|url|http|https|"
    r"\d{1,3}"
    r")-\d+$",
    re.IGNORECASE,
)


def _looks_like_xai_code(code: str) -> bool:
    """校验是否为 xAI 风格验证码（如 NBW-BQM），排除 CSS 噪声。"""
    c = str(code or "").strip()
    if not _XAI_CODE_RE.fullmatch(c):
        return False
    if _FAKE_CODE_RE.match(c):
        return False
    left, right = c.split("-", 1)
    # 纯数字两侧：123-456
    if left.isdigit() and right.isdigit():
        return False
    # CSS 占位 2xx-4xx / 100-px 一类（仅数字与 x）
    if re.fullmatch(r"[0-9xX]+", left) and re.fullmatch(r"[0-9xX]+", right):
        return False
    # 至少有一侧含字母（非纯数字码）
    if not re.search(r"[A-Za-z]", left + right):
        return False
    return True


def extract_verification_code(text: str, subject: str = "") -> Optional[str]:
    # 1) 主题优先：xAI / SpaceXAI confirmation code: XXX-XXX
    if subject:
        m = re.search(
            r"(?:confirmation\s+code|verification\s+code|code)[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
            subject,
            re.IGNORECASE,
        )
        if m and _looks_like_xai_code(m.group(1)):
            return m.group(1).upper()
        m = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if m and _looks_like_xai_code(m.group(1)):
            return m.group(1).upper()
        m = _XAI_CODE_RE.search(subject or "")
        if m and _looks_like_xai_code(m.group(1)):
            return m.group(1).upper()

    # 2) 正文中全部 XXX-XXX，过滤 CSS 噪声后取第一个合理值
    for m in _XAI_CODE_RE.finditer(text or ""):
        cand = m.group(1)
        if _looks_like_xai_code(cand):
            return cand.upper()

    # 3) 数字验证码兜底（非 xAI 主路径）
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "", re.IGNORECASE)
        if match:
            return match.group(1)
    return None
