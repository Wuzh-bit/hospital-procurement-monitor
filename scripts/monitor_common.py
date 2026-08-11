"""Shared, standard-library helpers for the public-notice monitor."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

USER_AGENT = "HospitalProcurementMonitor/1.0 (+public-notice-monitoring)"

# HTTP 头未声明 charset 时，从文档头部嗅探 <meta charset>/encoding= 声明。
META_CHARSET_RE = re.compile(rb'(?:charset|encoding)\s*=\s*["\']?\s*([A-Za-z0-9_-]+)', re.I)


def choose_charset(raw: bytes, header_charset: str | None) -> str:
    """Pick a decode charset: HTTP header wins, then meta sniff, then utf-8/gb18030 trial."""
    if header_charset:
        return header_charset
    match = META_CHARSET_RE.search(raw[:4096])
    if match:
        charset = match.group(1).decode("ascii", "ignore").lower()
        if charset in {"gb2312", "gbk"}:  # gb18030 是 GBK 的超集，解码更稳。
            return "gb18030"
        return charset
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: str | Path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, data) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(target)
    return target


def canonical_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, ""))


def validate_public_http_url(raw_url: str) -> str:
    """Reject non-web and local/private targets before making a request."""
    url = encode_non_ascii_url(canonical_url(raw_url))
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许带主机名的 http/https 公开 URL")
    host = parsed.hostname
    if host.lower() == "localhost":
        raise ValueError("不允许访问 localhost")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as error:
        raise ValueError(f"无法解析域名 {host}: {error}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"不允许访问非公网地址: {address}")
    return url


def encode_non_ascii_url(raw_url: str) -> str:
    """对 URL 中的非 ASCII 字符做百分号编码（中文查询参数常见，urllib 不会自动处理）。"""
    if all(ord(ch) < 128 for ch in raw_url):
        return raw_url
    parsed = urlparse(raw_url)
    safe = "/%:@&=+$,;~!*'()"
    path = quote(parsed.path, safe=safe)
    query = quote(parsed.query, safe=safe + "?")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, parsed.fragment))


# 可用于 JS 页面无头渲染的系统浏览器（优先 Chromium 内核：Chrome/Edge 参数一致；
# Firefox 为最后兜底）。覆盖 Windows / macOS / Linux 常见安装路径。
# 环境变量 RENDER_BROWSER 可显式指定浏览器可执行文件路径；EDGE_PATH 兼容旧配置。
RENDER_BROWSER_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("firefox", "/Applications/Firefox.app/Contents/MacOS/firefox"),
    ("chrome", "/usr/bin/google-chrome"),
    ("chromium", "/usr/bin/chromium"),
    ("chromium", "/usr/bin/chromium-browser"),
    ("firefox", "/usr/bin/firefox"),
)


def find_browser() -> tuple[str, str]:
    """探测可用于无头渲染的系统浏览器，返回 (name, path)。

    优先级：环境变量 RENDER_BROWSER（或旧名 EDGE_PATH）→ Chrome → Edge → Firefox。
    Safari 不支持命令行无头渲染，无法使用本机制；未检测到可用浏览器时抛 RuntimeError。
    """
    configured = os.environ.get("RENDER_BROWSER", "").strip().strip('"') or os.environ.get("EDGE_PATH", "").strip().strip('"')
    if configured and Path(configured).is_file():
        stem = Path(configured).stem.lower()
        name = "firefox" if "firefox" in stem else ("chrome" if "chrome" in stem else "edge")
        return name, configured
    for name, candidate in RENDER_BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return name, candidate
    raise RuntimeError("未检测到可用于无头渲染的浏览器（Chrome/Edge/Firefox）。Safari 不支持命令行渲染，"
                       "请安装 Chrome（或设置 RENDER_BROWSER 指向浏览器可执行文件），或改用静态来源")


RENDER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def render_public_url(raw_url: str, timeout: int = 50,
                      profile_dir: str | None = None) -> tuple[str, str, str]:
    """用系统浏览器无头模式渲染公开 URL，返回 (final_url, content_type, body)。

    仅用于公开、无需登录/验证码的 JS 渲染页面（如政采平台 SPA 列表页）；不填表、
    不点验证码、不绕过访问控制，限速仍由调用方控制，用完即关。
    渲染策略（渐进增强）：优先 Playwright（若已安装，能处理 TLS 握手异常/JS SPA 更稳），
    未安装或失败时退回系统浏览器 dump-dom。Playwright 为可选依赖（pip install playwright，
    复用系统 Chrome/Edge，无需下载浏览器）。
    """
    url = validate_public_http_url(raw_url)
    try:
        return _render_playwright(url, timeout)
    except Exception as pw_error:
        try:
            return _render_dumpdom(url, timeout, profile_dir)
        except Exception as dd_error:
            raise RuntimeError(f"浏览器渲染失败（Playwright: {str(pw_error)[:80]}; "
                               f"dump-dom: {str(dd_error)[:80]}）") from dd_error


def _render_playwright(url: str, timeout: int) -> tuple[str, str, str]:
    """Playwright 渲染（按需启动、用完即关）。未安装 playwright 时抛错由上层兜底。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("未安装 playwright（可选依赖，pip install playwright 后可用）") from exc
    name, browser_path = find_browser()
    with sync_playwright() as p:
        if name == "firefox":
            browser = p.firefox.launch(executable_path=browser_path, headless=True)
        else:
            try:  # Chromium 系优先用 channel 复用系统浏览器，失败则用可执行文件路径。
                channel = "msedge" if name == "edge" else "chrome"
                browser = p.chromium.launch(channel=channel, headless=True)
            except Exception:
                browser = p.chromium.launch(executable_path=browser_path, headless=True)
        try:
            page = browser.new_page(user_agent=RENDER_UA, viewport={"width": 1400, "height": 900})
            html = ""
            for _attempt in range(2):
                try:
                    # 用 domcontentloaded（不易超时）+ 固定等待，给 SPA 异步数据完成时间；
                    # 部分站点 load/networkidle 永不满足（持续轮询），不可作为等待条件。
                    page.goto(url, wait_until="domcontentloaded", timeout=min(timeout, 45000))
                except Exception:
                    pass
                page.wait_for_timeout(8000)
                html = page.content()
                if html.strip():
                    break
            if not html.strip():
                raise RuntimeError("Playwright 渲染结果为空（页面可能不可访问或需要登录）")
            return url, "text/html", html
        finally:
            browser.close()


def _render_dumpdom(url: str, timeout: int, profile_dir: str | None) -> tuple[str, str, str]:
    """系统浏览器 --dump-dom 兜底（零依赖）。"""
    name, browser = find_browser()
    profile = profile_dir or (Path(tempfile.gettempdir()) / "hpm-render-profile")
    profile.mkdir(parents=True, exist_ok=True)
    if name in ("chrome", "edge", "chromium"):
        command = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
                   "--disable-extensions", "--virtual-time-budget=8000",
                   f"--user-data-dir={profile}", "--dump-dom", url]
    else:  # firefox
        command = [browser, "--headless", "--dump-dom", url]
    process = subprocess.run(command, capture_output=True, timeout=timeout)
    raw = process.stdout
    if not raw.strip():
        raise RuntimeError("浏览器渲染结果为空（页面可能不可访问或需要登录）")
    charset = choose_charset(raw[:4096], None)
    return url, "text/html", raw.decode(charset, errors="replace")


def fetch_public_url(raw_url: str, timeout: int = 20, retries: int = 2) -> tuple[str, str, str]:
    """抓取公开 URL；对偶发 502/503/429 做最多 retries 次退避重试（429 尊重 Retry-After）。"""
    url = validate_public_http_url(raw_url)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/xml;q=0.9,*/*;q=0.1"})
    class PublicRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_public_http_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(PublicRedirectHandler())
    final_url, content_type, body = "", "", ""
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:  # nosec B310: initial and redirect URLs are validated.
                final_url = canonical_url(response.geturl())
                validate_public_http_url(final_url)
                content_type = response.headers.get_content_type()
                raw = response.read(2_000_000)
                charset = choose_charset(raw, response.headers.get_content_charset())
                body = raw.decode(charset, errors="replace")
            break
        except HTTPError as error:
            if error.code in (502, 503, 429) and attempt < retries:
                retry_after = (error.headers.get("Retry-After") or "").strip()
                wait = float(retry_after) if retry_after.isdigit() else attempt + 1
                time.sleep(min(wait, 5.0))
                continue
            raise
        except ssl.SSLError as error:
            raise ValueError(f"TLS/证书问题（{ssl_hint(error)}）无法访问该来源；"
                             f"请改配 http 入口，或将该来源标注为“人工关注”") from error
        except URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, ssl.SSLError):
                raise ValueError(f"TLS/证书问题（{ssl_hint(reason)}）无法访问该来源；"
                                 f"请改配 http 入口，或将该来源标注为“人工关注”") from error
            raise
    return final_url, content_type, body


def ssl_hint(error: ssl.SSLError) -> str:
    """把常见 SSL 错误转成可读的中文原因，便于 AI/用户判断对策。"""
    message = str(error)
    if "certificate has expired" in message:
        return "证书已过期"
    if "Hostname mismatch" in message or "hostname" in message and "doesn't match" in message:
        return "证书域名与访问地址不匹配"
    if "UNEXPECTED_EOF" in message or "EOF occurred" in message:
        return "TLS 握手中断（站点协议/中间设备异常）"
    if "CERTIFICATE_VERIFY_FAILED" in message:
        return "证书校验失败"
    return message[:120]


def make_notice_id(source_id: str, url: str, title: str, published_text: str = "") -> str:
    material = "\x1f".join([source_id, canonical_url(url), title.strip(), published_text.strip()])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def ensure_data_dir(raw: str | Path) -> Path:
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


# 台账与跳过记录的表结构只在 monitor_common 维护一份，
# prepare_candidates 与 record_leads 共用，避免建表语句分叉。
def create_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS leads (
        notice_id TEXT PRIMARY KEY, hospital_name TEXT, title TEXT, notice_type TEXT, project_type TEXT,
        matched_keywords TEXT, ai_reason TEXT, published_text TEXT, collected_at TEXT,
        source_name TEXT, source_type TEXT, url TEXT, budget TEXT, deadline TEXT, status TEXT,
        notes TEXT, recorded_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS skipped_notices (
        notice_id TEXT PRIMARY KEY, url TEXT, title TEXT, source_name TEXT,
        skip_reason TEXT, ai_reason TEXT, skipped_at TEXT NOT NULL)""")
