# -*- coding: utf-8 -*-
"""代理池：加载、连通探测、一代理一账号分配（线程安全）。

- 线程本地 `_tls.proxy`：当前 worker/账号生效的代理
- 全局游标 + 锁：多 worker 共享池，避免重复占用
"""

from __future__ import annotations

import os
import threading
from typing import Callable, List, Optional, Tuple

from curl_cffi import requests

LogFn = Optional[Callable[[str], None]]
CancelFn = Optional[Callable[[], bool]]

_tls = threading.local()
_alloc_lock = threading.Lock()
_pool: List[str] = []
_cursor = 0

# 探测 URL：任一成功即视为可用；禁止走「代理失败改直连」回退
PROXY_PROBE_URLS = (
    "https://accounts.x.ai/",
    "https://cloudflare.com/cdn-cgi/trace",
)


def normalize_proxy_url(raw) -> str:
    """规范化代理地址，支持 host:port / http(s):// / socks5://。"""
    s = str(raw or "").strip()
    if not s or s.startswith("#"):
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def load_proxy_pool(path, base_dir: str = "") -> List[str]:
    """从 txt 加载代理池，一行一个，忽略空行与 # 注释。"""
    path = str(path or "").strip()
    if not path:
        return []
    if not os.path.isabs(path):
        root = base_dir or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(root, path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"代理池文件不存在: {path}")
    proxies: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            proxy = normalize_proxy_url(line)
            if proxy:
                proxies.append(proxy)
    return proxies


def reset_allocator(proxies: Optional[List[str]] = None) -> None:
    """重置全局池与游标；传 None 清空。"""
    global _pool, _cursor
    with _alloc_lock:
        _pool = list(proxies or [])
        _cursor = 0


def pool_size() -> int:
    with _alloc_lock:
        return len(_pool)


def set_thread_proxy(proxy) -> None:
    """设置当前线程代理；None 表示回退 config.proxy。"""
    if proxy is None:
        _tls.proxy = None
    else:
        _tls.proxy = normalize_proxy_url(proxy)


def clear_thread_proxy() -> None:
    _tls.proxy = None


def get_thread_proxy() -> Optional[str]:
    """线程本地代理；未设置时返回 None（注意与空串区分）。"""
    if not hasattr(_tls, "proxy"):
        return None
    return _tls.proxy


def get_active_proxy(config_proxy: str = "") -> str:
    """优先线程本地代理，否则 config.proxy。"""
    local = get_thread_proxy()
    if local is not None:
        return local
    return normalize_proxy_url(config_proxy)


def get_proxies_dict(config_proxy: str = "") -> dict:
    proxy = get_active_proxy(config_proxy)
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def test_proxy_connectivity(
    proxy: str = "",
    timeout: int = 10,
    log_callback: LogFn = None,
) -> Tuple[bool, str]:
    """探测代理是否可出网。返回 (ok, message)。"""
    proxy = normalize_proxy_url(proxy)
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    label = proxy or "直连"
    last_err = "无响应"
    for url in PROXY_PROBE_URLS:
        try:
            if log_callback:
                log_callback(f"[*] 探测代理: {label} -> {url}")
            resp = requests.get(
                url,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome120",
                allow_redirects=True,
            )
            # 2xx/3xx/4xx 均说明链路通
            if resp.status_code < 500:
                msg = f"HTTP {resp.status_code}"
                if log_callback:
                    log_callback(f"[+] 代理可用: {label} ({msg})")
                return True, msg
            last_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)
            continue
    if log_callback:
        log_callback(f"[-] 代理不可用: {label} | {last_err}")
    return False, last_err


def apply_proxy_for_index(
    index: int,
    proxy_pool: List[str],
    log_callback: LogFn = None,
    config_proxy: str = "",
) -> bool:
    """按序号应用代理（不做连通测试）。池用尽返回 False。"""
    if proxy_pool:
        if index >= len(proxy_pool):
            if log_callback:
                log_callback(f"[!] 代理池已用尽（共 {len(proxy_pool)} 个），停止注册")
            return False
        proxy = proxy_pool[index]
        set_thread_proxy(proxy)
        if log_callback:
            log_callback(f"[*] 使用代理 [{index + 1}/{len(proxy_pool)}]: {proxy}")
        return True
    set_thread_proxy(None)
    proxy = get_active_proxy(config_proxy)
    if proxy and log_callback:
        log_callback(f"[*] 使用代理: {proxy}")
    return True


def acquire_proxy_for_slot(
    proxy_pool: List[str],
    cursor: int = 0,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    do_test: bool = True,
    timeout: int = 10,
    config_proxy: str = "",
    raise_if_cancelled: Optional[Callable[[CancelFn], None]] = None,
) -> Tuple[bool, int]:
    """从 cursor 起挑选可用代理并 set_thread_proxy。

    返回 (ok, next_cursor)。
    """
    if proxy_pool:
        idx = max(int(cursor or 0), 0)
        while idx < len(proxy_pool):
            if raise_if_cancelled:
                raise_if_cancelled(cancel_callback)
            proxy = proxy_pool[idx]
            set_thread_proxy(proxy)
            if log_callback:
                log_callback(f"[*] 候选代理 [{idx + 1}/{len(proxy_pool)}]: {proxy}")
            if not do_test:
                return True, idx + 1
            ok, _msg = test_proxy_connectivity(
                proxy, timeout=timeout, log_callback=log_callback
            )
            if ok:
                return True, idx + 1
            if log_callback:
                log_callback(f"[!] 跳过不可用代理 [{idx + 1}/{len(proxy_pool)}]: {proxy}")
            idx += 1
        if log_callback:
            log_callback(
                f"[!] 代理池无可用代理（从第 {max(int(cursor or 0), 0) + 1} 个起均失败或已用尽）"
            )
        return False, idx

    # 单代理 / 直连
    set_thread_proxy(None)
    proxy = get_active_proxy(config_proxy)
    if proxy:
        if log_callback:
            log_callback(f"[*] 使用单代理: {proxy}")
        if do_test:
            ok, msg = test_proxy_connectivity(
                proxy, timeout=timeout, log_callback=log_callback
            )
            if not ok:
                if log_callback:
                    log_callback(f"[!] 单代理不可用，中止注册: {msg}")
                return False, cursor
    elif log_callback:
        log_callback("[*] 未配置代理，将直连")
    return True, cursor


def acquire_next_from_global(
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    do_test: bool = True,
    timeout: int = 10,
    config_proxy: str = "",
    raise_if_cancelled: Optional[Callable[[CancelFn], None]] = None,
) -> bool:
    """从全局池游标分配下一个可用代理（多 worker 共享）。

    无池时回退 config.proxy。成功则已 set_thread_proxy。
    """
    global _cursor
    with _alloc_lock:
        pool = list(_pool)
        start = _cursor

    if not pool:
        ok, _ = acquire_proxy_for_slot(
            [],
            cursor=0,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            do_test=do_test,
            timeout=timeout,
            config_proxy=config_proxy,
            raise_if_cancelled=raise_if_cancelled,
        )
        return ok

    # 在锁外探测，避免长时间阻塞其它 worker；用 CAS 推进游标
    idx = start
    while idx < len(pool):
        if raise_if_cancelled:
            raise_if_cancelled(cancel_callback)
        proxy = pool[idx]
        if log_callback:
            log_callback(f"[*] 候选代理 [{idx + 1}/{len(pool)}]: {proxy}")
        if do_test:
            ok, _msg = test_proxy_connectivity(
                proxy, timeout=timeout, log_callback=log_callback
            )
        else:
            ok = True
        if ok:
            with _alloc_lock:
                # 若其它线程已推进超过 idx，则重试该位置之后
                if _cursor > idx:
                    idx = _cursor
                    continue
                _cursor = idx + 1
            set_thread_proxy(proxy)
            if log_callback:
                log_callback(f"[*] 使用代理 [{idx + 1}/{len(pool)}]: {proxy}")
            return True
        if log_callback:
            log_callback(f"[!] 跳过不可用代理 [{idx + 1}/{len(pool)}]: {proxy}")
        with _alloc_lock:
            if _cursor <= idx:
                _cursor = idx + 1
            idx = max(idx + 1, _cursor)
    if log_callback:
        log_callback(f"[!] 代理池无可用代理（共 {len(pool)} 个）")
    return False
