# -*- coding: utf-8 -*-
"""idinging/freemail 后端（X-Admin-Token 鉴权）。

与 cloudflare_temp_email 不同：无 JWT，创建走 GET /api/generate，
收信按 mailbox 查询 /api/emails。

所有 HTTP 请求必须走 get_proxies（代理池/单代理），禁止直连 workers.dev。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from curl_cffi import requests

from email_providers.common import extract_verification_code, _looks_like_xai_code

LogFn = Optional[Callable[[str], None]]
CancelFn = Optional[Callable[[], bool]]
RaiseIfCancelled = Callable[[CancelFn], None]
SleepWithCancel = Callable[[float, CancelFn], None]
GetProxies = Optional[Callable[[], Dict[str, str]]]


def _looks_like_code(raw: str) -> bool:
    """freemail verification_code 字段也可能塞噪声，统一走 xAI 码校验。"""
    return _looks_like_xai_code(str(raw or "").strip())


def _pick_list(data: Any) -> List[Any]:
    """兼容 list / {results|data|messages|hydra:member}；允许元素为 str 或 dict。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "hydra:member", "data", "messages", "domains"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("results", "messages", "domains"):
                val = nested.get(key)
                if isinstance(val, list):
                    return val
    return []


def _domain_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("domain") or item.get("name") or "").strip()
    return ""


class FreemailClient:
    """带 X-Admin-Token 的 freemail API 客户端。"""

    def __init__(
        self,
        api_base: str,
        admin_token: str,
        get_proxies: GetProxies = None,
        timeout: int = 20,
    ):
        base = str(api_base or "").rstrip("/")
        token = str(admin_token or "").strip()
        if not base:
            raise Exception("freemail API Base 未配置（cloudflare_api_base）")
        if not token:
            raise Exception("freemail Admin Token 未配置（请填写 cloudflare_api_key）")
        self.api_base = base
        self._admin_token = token
        self._get_proxies = get_proxies
        self.timeout = max(5, int(timeout or 20))
        # 不在 Session 上固定 proxies：代理池按线程切换，每次请求现取
        self.session = requests.Session()
        self.session.headers.update({"X-Admin-Token": token})

    def _request_kwargs(self, **kwargs) -> dict:
        """组装请求参数：注入当前代理 + 默认超时。"""
        req = dict(kwargs)
        req.setdefault("timeout", self.timeout)
        # 调用方显式传 proxies= 时优先；否则走 get_proxies 回调
        if "proxies" not in req and self._get_proxies is not None:
            try:
                proxies = self._get_proxies() or {}
            except Exception:
                proxies = {}
            if proxies:
                req["proxies"] = proxies
        return req

    def _get(self, url: str, **kwargs):
        return self.session.get(url, **self._request_kwargs(**kwargs))

    def get_domains(self) -> List[Any]:
        resp = self._get(f"{self.api_base}/api/domains")
        resp.raise_for_status()
        return _pick_list(resp.json())

    def create_temp_address(
        self,
        preferred_domain: str = "",
        length: int = 10,
    ) -> Tuple[str, str]:
        """GET /api/generate → (email, "")；无 JWT，token 固定空串。"""
        params: dict = {"length": max(4, int(length or 10))}
        domain = str(preferred_domain or "").strip()
        if domain:
            try:
                domains = self.get_domains()
                for idx, item in enumerate(domains):
                    if _domain_name(item) == domain:
                        params["domainIndex"] = idx
                        break
            except Exception:
                # domains 失败不阻塞创建，仍可让服务端用默认域
                pass
        resp = self._get(
            f"{self.api_base}/api/generate",
            params=params,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            raise Exception(f"freemail /api/generate 返回非JSON: {resp.text[:300]}")
        address = data.get("email") if isinstance(data, dict) else None
        if not address:
            raise Exception(f"freemail /api/generate 缺少 email 字段: {data}")
        return str(address), ""

    def get_messages(self, email: str, limit: int = 50) -> List[dict]:
        mailbox = str(email or "").strip()
        if not mailbox:
            raise Exception("freemail 拉取邮件需要邮箱地址")
        resp = self._get(
            f"{self.api_base}/api/emails",
            params={"mailbox": mailbox, "limit": limit},
        )
        resp.raise_for_status()
        items = _pick_list(resp.json())
        return [x for x in items if isinstance(x, dict)]

    def get_message_detail(self, message_id: str) -> dict:
        mid = str(message_id or "").strip()
        if not mid:
            raise Exception("freemail message_id 为空")
        candidates = [
            f"{self.api_base}/api/email/{mid}",
            f"{self.api_base}/api/emails/{mid}",
        ]
        last_err: Optional[Exception] = None
        for url in candidates:
            try:
                resp = self._get(url)
                if resp.status_code >= 400:
                    last_err = Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                data = resp.json()
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                last_err = exc
                continue
        raise Exception(f"freemail 获取邮件详情失败: {last_err}")


def wait_for_code(
    client: FreemailClient,
    email: str,
    *,
    timeout: int = 180,
    poll_interval: int = 3,
    raise_if_cancelled: RaiseIfCancelled,
    sleep_with_cancel: SleepWithCancel,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询 freemail 收件箱，提取 xAI 验证码。"""
    deadline = time.time() + timeout
    seen_attempts: dict = {}
    next_resend_at = time.time() + 35
    list_fields = (
        "preview",
        "text",
        "raw",
        "content",
        "intro",
        "body",
        "snippet",
        "verification_code",
    )
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = client.get_messages(email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] freemail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] freemail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1

            # 列表里可能直接带 verification_code（需像真实 xAI 码）
            vc = msg.get("verification_code")
            if isinstance(vc, str) and _looks_like_code(vc):
                code = vc.strip()
                if log_callback:
                    log_callback(f"[*] freemail 从邮件列表直接提取验证码: {code}")
                return code

            # freemail 主题常含 SpaceXAI confirmation code: XXX-XXX，优先于正文 CSS
            subject = str(msg.get("subject", "") or "")
            code = extract_verification_code("", subject)
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 从邮件主题提取验证码: {code}")
                return code

            parts: List[str] = []
            for field in list_fields:
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or msg.get("html_content") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", str(h)))
            combined = "\n".join(parts)

            try:
                detail = client.get_message_detail(str(msg_id))
                detail_vc = detail.get("verification_code")
                if isinstance(detail_vc, str) and _looks_like_code(detail_vc):
                    code = detail_vc.strip()
                    if log_callback:
                        log_callback(f"[*] freemail 从邮件详情直接提取验证码: {code}")
                    return code
                detail_subject = str(detail.get("subject", "") or "")
                if detail_subject:
                    subject = detail_subject
                    code = extract_verification_code("", subject)
                    if code:
                        if log_callback:
                            log_callback(f"[*] freemail 从详情主题提取验证码: {code}")
                        return code
                for field in list_fields:
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or detail.get("html_content") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", str(h))
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] freemail detail 失败，改用列表内容: {exc}")

            if log_callback:
                log_callback(f"[Debug] freemail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 从邮件中提取到验证码: {code}")
                return code
            if log_callback:
                log_callback(
                    f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}"
                )
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"freemail 在 {timeout}s 内未收到验证码邮件")
