"""Git PR 工作流工具:commit → push → create pull request。

支持 GitHub / GitLab / Gitea 等主流平台(通过 AGENT_GIT_HOST 和 AGENT_GIT_TOKEN 配置)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.git_tool import _git, _find_repo
from app.tools.registry import tool


def _get_git_host() -> str:
    """获取 Git 平台类型(github/gitlab/gitea)。"""
    return (settings.agent_git_host or "github").lower()


def _get_git_token() -> Optional[str]:
    """获取 Git 平台 API Token。"""
    return settings.agent_git_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GITLAB_TOKEN")


def _api_headers() -> dict:
    host = _get_git_host()
    token = _get_git_token()
    if host == "github":
        return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"} if token else {}
    elif host == "gitlab":
        return {"PRIVATE-TOKEN": token} if token else {}
    return {}


def _api_url() -> str:
    host = _get_git_host()
    if host == "github":
        return "https://api.github.com"
    elif host == "gitlab":
        return (settings.agent_gitlab_url or "https://gitlab.com").rstrip("/")
    return ""


def _get_repo_info(repo: str) -> dict:
    """从 remote URL 解析 owner/repo。"""
    r = _git(repo, "remote", "get-url", "origin")
    url = (r.get("stdout") or "").strip()
    if not url:
        return {}
    # https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
    for pattern in (
        r"https://github\.com/([^/]+)/(.+)\.git$",
        r"git@github\.com:([^/]+)/(.+)\.git$",
        r"https://gitlab\.com/([^/]+)/(.+)\.git$",
        r"git@gitlab\.com:([^/]+)/(.+)\.git$",
    ):
        import re as _re
        m = _re.search(pattern, url)
        if m:
            return {"owner": m.group(1), "repo": m.group(2).rstrip(".git")}
    return {}


@tool(
    "git_create_branch",
    "创建新分支并切换到该分支。用于开始新功能开发时隔离工作。",
    timeout=15,
)
def git_create_branch(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    branch: str = Field(description="新分支名称"),
    base_branch: str = Field("main", description="基于哪个分支创建(默认 main)"),
) -> dict:
    try:
        repo = _find_repo(repo or os.getcwd())
    except ValueError as e:
        return {"error": str(e)}
    branch = (branch or "").strip()
    if not branch:
        return {"repo": repo, "error": "分支名称不能为空"}
    # 先切到 base
    base = _git(repo, "checkout", base_branch)
    if not base.get("ok"):
        return {"repo": repo, "error": f"无法切换到 {base_branch}: {(base.get('stderr') or base.get('error'))[:200]}"}
    # 创建并切换
    r = _git(repo, "checkout", "-b", branch)
    if not r.get("ok"):
        return {"repo": repo, "branch": branch, "error": r.get("stderr") or r.get("error")}
    return {"repo": repo, "branch": branch, "created_from": base_branch}


@tool(
    "git_push_and_create_pr",
    "将当前分支推送到远程并创建 Pull Request。需要配置 GITHUB_TOKEN 或 GITLAB_TOKEN。",
    timeout=60,
)
def git_push_and_create_pr(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    title: str = Field(description="PR 标题"),
    body: str = Field("", description="PR 描述(支持 Markdown)"),
    draft: bool = Field(False, description="是否为草稿 PR"),
) -> dict:
    try:
        repo = _find_repo(repo or os.getcwd())
    except ValueError as e:
        return {"error": str(e)}

    branch = _git(repo, "branch", "--show-current")
    current_branch = (branch.get("stdout") or "").strip()
    if not current_branch:
        return {"repo": repo, "error": "无法获取当前分支名"}

    # 推送
    push = _git(repo, "push", "-u", "origin", current_branch)
    if not push.get("ok"):
        return {"repo": repo, "branch": current_branch, "error": push.get("stderr") or push.get("error")}

    # 创建 PR
    token = _get_git_token()
    if not token:
        return {
            "repo": repo,
            "branch": current_branch,
            "pushed": True,
            "error": "未配置 Git Token(设置 AGENT_GIT_TOKEN 环境变量)",
            "note": "分支已推送,请手动创建 PR",
        }

    host = _get_git_host()
    headers = _api_headers()
    repo_info = _get_repo_info(repo)
    if not repo_info:
        return {"repo": repo, "error": "无法从 remote URL 解析仓库信息"}

    if host == "github":
        import httpx
        url = f"{_api_url()}/repos/{repo_info['owner']}/{repo_info['repo']}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": current_branch,
            "base": repo_info.get("base_branch", "main"),
            "draft": draft,
        }
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code == 201:
                data = r.json()
                return {
                    "repo": repo,
                    "branch": current_branch,
                    "pr_url": data.get("html_url", ""),
                    "pr_number": data.get("number"),
                    "title": title,
                }
            return {"repo": repo, "error": f"GitHub API 错误 {r.status_code}: {(r.text or '')[:200]}"}
        except Exception as e:
            return {"repo": repo, "error": f"创建 PR 失败: {e}"}

    elif host == "gitlab":
        import httpx
        project = f"{repo_info['owner']}/{repo_info['repo']}".replace("/", "%2F")
        url = f"{_api_url()}/api/v4/projects/{project}/merge_requests"
        payload = {
            "title": title,
            "description": body,
            "source_branch": current_branch,
            "target_branch": repo_info.get("base_branch", "main"),
            "draft": draft,
        }
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code in (200, 201):
                data = r.json()
                return {
                    "repo": repo,
                    "branch": current_branch,
                    "pr_url": data.get("web_url", ""),
                    "pr_iid": data.get("iid"),
                    "title": title,
                }
            return {"repo": repo, "error": f"GitLab API 错误 {r.status_code}: {(r.text or '')[:200]}"}
        except Exception as e:
            return {"repo": repo, "error": f"创建 MR 失败: {e}"}

    return {"repo": repo, "error": f"不支持的 Git 平台: {host}"}
