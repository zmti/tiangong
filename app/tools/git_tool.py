"""Git 工具集:状态/日志/diff/暂存/提交/分支/切换,支持任意 git 仓库路径。

所有命令通过 `git -C <repo>` 执行,不依赖当前工作目录。
提交前若缺少 user.name/email 配置,自动设置仓库级默认值(Agent <agent@local>)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from pydantic import Field

from app.tools.project_context import resolve_cwd
from app.tools.registry import tool

MAX_DIFF_CHARS = 8000


def _git_exe() -> str | None:
    return shutil.which("git")


def _git(repo: str, *args: str, timeout: int = 60) -> dict:
    """执行 git 命令,返回 {ok, stdout, stderr, returncode}。"""
    exe = _git_exe()
    if not exe:
        return {"ok": False, "error": "未找到 git 可执行文件,请先安装 Git(https://git-scm.com)"}
    try:
        r = subprocess.run(
            [exe, "-C", repo, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"git 命令超时({timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"git 执行失败: {e}"}


def _find_repo(path: str) -> str:
    """从给定路径向上查找包含 .git 的仓库根目录。"""
    p = os.path.abspath(os.path.expanduser(path))
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            raise ValueError(f"未找到 git 仓库(从 {path} 向上没有 .git 目录)")
        p = parent


def _ensure_identity(repo: str) -> None:
    """提交前确保有 user.name/email,否则设置仓库级默认。"""
    for key, val in (("user.name", "Agent"), ("user.email", "agent@local")):
        check = _git(repo, "config", "--local", "--get", key)
        if not check.get("ok") or not (check.get("stdout") or "").strip():
            _git(repo, "config", "--local", key, val)


@tool(
    "git_status",
    "查看 git 仓库当前状态:所在分支、暂存/未暂存/未跟踪文件、与远程的领先落后。用于了解代码变更全貌",
    timeout=30,
)
def git_status(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    branch = _git(repo, "branch", "--show-current")
    short = _git(repo, "status", "--short")
    if not short.get("ok"):
        return {"repo": repo, "error": short.get("stderr") or short.get("error")}
    staged, unstaged, untracked = [], [], []
    for line in (short.get("stdout") or "").splitlines():
        line = line.rstrip("\n")
        if not line:
            continue
        flag, path = line[:2], line[3:]
        if flag == "??":
            untracked.append(path)
        else:
            # XY 格式: X=暂存区状态, Y=工作区状态
            x, y = flag[0], flag[1]
            if x != " ":
                staged.append(f"{flag}: {path}")
            if y != " ":
                unstaged.append(f"{flag}: {path}")
    return {
        "repo": repo,
        "branch": (branch.get("stdout") or "").strip() or "(detached)",
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "staged_count": len(staged),
        "unstaged_count": len(unstaged),
        "untracked_count": len(untracked),
    }


@tool(
    "git_log",
    "查看 git 提交历史(简洁一行式),可选统计文件变更。用于了解最近改动与提交记录",
    timeout=30,
)
def git_log(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    count: int = Field(15, description="返回条数,最大 50"),
    with_stats: bool = Field(False, description="True 时附带每个提交的文件变更统计"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    count = max(1, min(count, 50))
    if with_stats:
        r = _git(repo, "log", "-n", str(count), "--stat", "--decorate")
    else:
        r = _git(repo, "log", "-n", str(count), "--oneline", "--decorate")
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error"), "empty": "没有提交" in (r.get("stderr") or "")}
    lines = (r.get("stdout") or "").rstrip("\n")
    return {"repo": repo, "count": count, "log": lines}


@tool(
    "git_diff",
    "查看 git 工作区改动(默认未暂存差异;staged=True 查看已暂存差异;可指定文件)。返回 unified diff 文本",
    timeout=30,
)
def git_diff(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    path: str = Field("", description="只查看该文件的 diff,留空查看全部"),
    staged: bool = Field(False, description="True 查看已暂存(staged)差异,False 查看未暂存差异"),
    max_chars: int = Field(8000, description="返回最大字符数"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args.append("--")
        args.append(path)
    r = _git(repo, *args)
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error")}
    text = (r.get("stdout") or "").rstrip("\n") or "(无差异)"
    truncated = len(text) > max_chars
    return {"repo": repo, "staged": staged, "path": path or None, "diff": text[:max_chars], "truncated": truncated}


@tool(
    "git_add",
    "把文件改动加入暂存区(stage),准备提交。paths 为空则暂存全部改动",
    timeout=30,
)
def git_add(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    paths: str = Field("", description="要暂存的文件路径(逗号分隔);留空=暂存全部"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    args = ["add"]
    if paths.strip():
        args.extend(p.strip() for p in paths.split(",") if p.strip())
    else:
        args.append("-A")
    r = _git(repo, *args)
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error")}
    return {"repo": repo, "staged_paths": paths or "全部", "status": "ok"}


@tool(
    "git_commit",
    "提交当前暂存的改动,返回提交哈希。提交前会自动补仓库级 user.name/email 默认值。"
    "注意:先调用 git_add 暂存,再 commit;此工具不会 push 到远程",
    timeout=30,
)
def git_commit(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    message: str = Field(description="提交信息,建议简洁描述本次改动"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    msg = (message or "").strip()
    if not msg:
        return {"repo": repo, "error": "提交信息不能为空"}
    _ensure_identity(repo)
    r = _git(repo, "commit", "-m", msg)
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error")}
    output = (r.get("stdout") or "").rstrip()
    return {"repo": repo, "status": "committed", "output": output, "message": msg}


@tool(
    "git_branch",
    "列出 git 分支及当前所在分支,可选新建分支",
    timeout=30,
)
def git_branch(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    create: str = Field("", description="要新建的分支名(留空仅列出)"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    if create.strip():
        r = _git(repo, "checkout", "-b", create.strip())
        if not r.get("ok"):
            return {"repo": repo, "error": r.get("stderr") or r.get("error")}
        return {"repo": repo, "action": "created", "branch": create.strip()}
    r = _git(repo, "branch", "-a")
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error")}
    branches = [(b.strip().lstrip("* ")) for b in (r.get("stdout") or "").splitlines() if b.strip()]
    return {"repo": repo, "current": branches[0] if branches else "", "branches": branches}


@tool(
    "git_checkout",
    "切换 git 分支或恢复到指定提交(ref 可以是分支名、commit hash 或 tag)。危险改动前建议先看 git_status",
    timeout=30,
)
def git_checkout(
    repo: str = Field("", description="仓库路径,留空用当前工作目录"),
    ref: str = Field(description="要切换到的分支/提交/tag"),
) -> str:
    try:
        repo = _find_repo(repo or resolve_cwd())
    except ValueError as e:
        return {"error": str(e)}
    ref = (ref or "").strip()
    if not ref:
        return {"repo": repo, "error": "ref 不能为空"}
    r = _git(repo, "checkout", ref)
    if not r.get("ok"):
        return {"repo": repo, "error": r.get("stderr") or r.get("error")}
    return {"repo": repo, "checked_out": ref, "output": (r.get("stdout") or "").strip()}
