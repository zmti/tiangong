"""H5/网页自动测试工具:检测前端项目 → 后台拉起 dev server → 浏览器打开 →
冒烟检查(console 错误/页面异常/失败请求/关键元素)→ 结构化报告。

依赖:playwright(`pip install playwright && playwright install chromium`)。
与 browser_tool 的关系:复用其 BrowserSession 的错误捕获能力,这里只做"编排"。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

from pydantic import Field

from app.tools.registry import tool
from app.tools.project_context import detect_project_type


# 各框架默认端口(用于等待服务起来时探测)
_FRAMEWORK_PORTS = {
    "vite": [5173, 5174],
    "vue": [8080, 8081],
    "react": [5173, 5174],
    "next": [3000],
    "nuxt": [3000],
    "angular": [4200],
    "svelte": [5173, 5174],
    "uni-app": [5173, 8080, 5174],
    "web-generic": [5173, 8080, 3000, 4000, 8081],
    "static": [8000, 8080, 5173],
}


def _pick_dev_command(pkg: dict | None, framework: str | None, project_path: str) -> tuple[str, list[int]]:
    """选择 dev 启动命令与待探测端口列表。

    优先 package.json scripts(dev > serve > start);uni-app 用 dev:h5;
    静态站点用 python -m http.server。
    """
    ports = _FRAMEWORK_PORTS.get(framework or "web-generic", [5173, 8080, 3000])
    if framework == "static":
        port = ports[0]
        return f'python -m http.server {port}', ports
    if pkg:
        scripts = pkg.get("scripts") or {}
        if framework == "uni-app" and "dev:h5" in scripts:
            return "npm run dev:h5", ports
        for key in ("dev", "serve", "start"):
            if key in scripts:
                return f"npm run {key}", ports
    # 兜底
    return "npm run dev", ports


def _wait_for_server(urls: list[str], timeout: int) -> Optional[str]:
    """轮询候选 URL,返回首个可达(HTTP<400)的地址;超时返回 None。"""
    import urllib.request

    deadline = time.time() + max(5, timeout)
    while time.time() < deadline:
        for url in urls:
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if getattr(resp, "status", 200) < 400:
                        return url
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1.0)
    return None


def _spawn_dev_server(command: str, project_path: str) -> tuple[Optional[subprocess.Popen], str]:
    """后台拉起 dev server(不阻塞),返回 (proc, log_path)。失败返回 (None, reason)。"""
    try:
        log_fd, log_path = tempfile.mkstemp(prefix="devserver_", suffix=".log")
        import os as _os
        log_file = _os.fdopen(log_fd, "w")
        argv = ["cmd.exe", "/c", command] if sys.platform == "win32" else ["/bin/sh", "-lc", command]
        proc = subprocess.Popen(
            argv,
            cwd=project_path,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            # Windows: 独立于父进程,避免被杀;创建无窗口
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
        return proc, log_path
    except Exception as e:  # noqa: BLE001
        return None, f"启动 dev server 失败: {e}"


def _build_candidate_urls(ports: list[int], host: str = "127.0.0.1") -> list[str]:
    urls = []
    for p in ports:
        urls.append(f"http://{host}:{p}/")
        urls.append(f"http://localhost:{p}/")
    return urls


def _assemble_report(
    project: dict, url: str | None, steps: list[str],
    capture: dict, title: str, has_content: bool,
    selectors_present: list[str] | None, error: str | None = None,
) -> dict:
    console_errors = capture.get("console_errors", [])
    page_errors = capture.get("page_errors", [])
    failed = capture.get("failed_requests", [])
    checks = {
        "page_loaded": bool(url and not error),
        "has_content": has_content,
        "no_console_errors": len(console_errors) == 0,
        "no_page_errors": len(page_errors) == 0,
        "no_failed_requests": len(failed) == 0,
    }
    if selectors_present is not None:
        checks["selectors_present"] = selectors_present
    passed = bool(
        url and not error and has_content
        and not console_errors and not page_errors and not failed
        and (selectors_present is None or all(selectors_present))
    )
    return {
        "status": "ok" if not error else "error",
        "pass": passed,
        "project": project,
        "url": url,
        "title": title,
        "checks": checks,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failed_requests": failed,
        "steps": steps,
        "error": error,
    }


@tool(
    "web_auto_test",
    "自动测试当前 H5/网页项目:检测框架→后台拉起 dev server→浏览器打开→冒烟检查"
    "(console 错误/页面异常/失败请求/关键元素)→返回结构化报告。适合:完成前端改动后验证页面能正常加载、无报错。",
    timeout=300,
)
def web_auto_test(
    project_path: str = Field("", description="项目目录,留空用当前项目路径"),
    url: str = Field("", description="已运行的 dev server 地址;留空则自动检测并拉起"),
    start_server: bool = Field(True, description="url 为空时是否自动拉起 dev server"),
    keep_server: bool = Field(False, description="测试结束后是否保留 dev server 运行"),
    smoke_selectors: str = Field("", description="可选,JSON 数组的 CSS 选择器,要求全部存在才算通过"),
    timeout: int = Field(120, description="等待 dev server 就绪的超时秒数"),
) -> dict:
    """对 H5/网页项目做一键冒烟测试。"""
    steps: list[str] = []
    project_dir = os.path.abspath(project_path) if project_path else detect_project_type()["path"]
    info = detect_project_type(project_dir)
    project = {"path": project_dir, "framework": info.get("framework"),
               "is_web": info.get("is_web"), "is_static": info.get("is_static")}
    steps.append(f"识别项目: framework={info.get('framework')}, is_web={info.get('is_web')}")

    if not info.get("is_web"):
        return _assemble_report(project, None, steps, {}, "", False, None,
                                error="当前目录不是 H5/网页项目(未检测到前端框架或 index.html),跳过自动测试")

    # 1) 解析 dev 命令 / 端口
    pkg = None
    pkg_path = os.path.join(project_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                pkg = json.load(f)
        except Exception:  # noqa: BLE001
            pkg = None
    dev_cmd, ports = _pick_dev_command(pkg, info.get("framework"), project_dir)
    project["dev_command"] = dev_cmd
    steps.append(f"dev 命令: {dev_cmd}, 探测端口: {ports}")

    proc = None
    log_path = None
    try:
        # 2) 确定目标 URL(已给则用,否则拉起并等待)
        target = url.strip() if url else None
        if not target:
            if not start_server:
                return _assemble_report(project, None, steps, {}, "", False, None,
                                        error="未提供 url 且 start_server=False,无法测试")
            proc, log_path = _spawn_dev_server(dev_cmd, project_dir)
            if proc is None:
                return _assemble_report(project, None, steps, {}, "", False, None,
                                        error=f"无法启动 dev server: {log_path}")
            steps.append("已后台拉起 dev server,等待就绪…")
            candidates = _build_candidate_urls(ports)
            target = _wait_for_server(candidates, timeout)
            if not target:
                return _assemble_report(project, None, steps, {}, "", False, None,
                                        error=f"dev server 在 {timeout}s 内未就绪(端口 {ports} 无响应);"
                                              f"日志: {log_path}")
            steps.append(f"dev server 就绪: {target}")

        # 3) 浏览器打开 + 冒烟检查
        from app.tools.browser_tool import BrowserSession
        session = BrowserSession()
        if not asyncio.get_event_loop().run_until_complete(session.launch(headless=True)):
            return _assemble_report(project, target, steps, {}, "", False, None,
                                    error="无法启动浏览器(请确认已安装 playwright: pip install playwright && playwright install chromium)")
        try:
            session.reset_capture()
            nav = asyncio.get_event_loop().run_until_complete(session.goto(target, wait_until="networkidle", timeout=30000))
            if "error" in nav:
                return _assemble_report(project, target, steps, session.get_capture(), nav.get("title", ""), False, None,
                                        error=f"打开页面失败: {nav['error']}")
            title = nav.get("title", "")
            # 内容检查
            text = asyncio.get_event_loop().run_until_complete(session.get_text())
            has_content = bool(text.get("text", "").strip()) if isinstance(text, dict) else False
            steps.append(f"页面已加载: title={title!r}, 有内容={has_content}")
            # 关键元素检查
            selectors_present = None
            if smoke_selectors:
                try:
                    sels = json.loads(smoke_selectors) if isinstance(smoke_selectors, str) else smoke_selectors
                except json.JSONDecodeError:
                    sels = []
                selectors_present = []
                for sel in (sels or []):
                    w = asyncio.get_event_loop().run_until_complete(session.wait_for(sel, timeout=5000))
                    selectors_present.append("error" not in w)
                steps.append(f"关键元素检查: {selectors_present}")
            # 截图留证
            shot = asyncio.get_event_loop().run_until_complete(session.screenshot(full_page=False))
            screenshot_path = ""
            if isinstance(shot, dict) and "image_base64" in shot:
                import base64
                sp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, prefix="webtest_")
                with open(sp.name, "wb") as f:
                    f.write(base64.b64decode(shot["image_base64"]))
                screenshot_path = sp.name
            capture = session.get_capture()
            project["screenshot"] = screenshot_path
            return _assemble_report(project, target, steps, capture, title, has_content, selectors_present)
        finally:
            asyncio.get_event_loop().run_until_complete(session.close())
    finally:
        if proc is not None and not keep_server:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
