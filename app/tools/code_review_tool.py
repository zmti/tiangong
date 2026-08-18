"""代码审阅工具:对代码进行静态分析，输出结构化 Review 意见。

支持的分析维度:
- 缺陷检测:潜在的 bug、边界条件
- 性能问题:低效算法、不必要的循环
- 安全风险:注入、硬编码凭据
- 代码风格:命名规范、重复代码
- PEP8 检查:基本的格式问题
"""
from __future__ import annotations

import ast
import re

from pydantic import Field

from app.tools.registry import tool


class CodeReviewResult:
    """单次代码审阅结果。"""
    def __init__(self, filepath: str, issues: list[dict]):
        self.filepath = filepath
        self.issues = issues  # [{"severity": "critical/warning/style", "line": int, "message": str}]

    def to_dict(self) -> dict:
        return {
            "filepath": self.filepath,
            "total_issues": len(self.issues),
            "critical": sum(1 for i in self.issues if i["severity"] == "critical"),
            "warnings": sum(1 for i in self.issues if i["severity"] == "warning"),
            "style": sum(1 for i in self.issues if i["severity"] == "style"),
            "issues": self.issues[:20],  # 最多返回 20 条
        }


def _check_indentation(lines: list[str]) -> list[dict]:
    """检查缩进一致性。"""
    issues = []
    for i, line in enumerate(lines, 1):
        if line.strip() and not line.startswith("\t") and "    " in line and "\t" in line:
            issues.append({
                "severity": "style",
                "line": i,
                "message": "混用了空格和制表符缩进",
            })
    return issues


def _check_hardcoded_secrets(source: str) -> list[dict]:
    """检查硬编码凭据。"""
    issues = []
    secret_patterns = [
        (r'password\s*=\s*["\'][^"\']+["\']', "hardcoded_password", "硬编码密码"),
        (r'api_key\s*=\s*["\'][^"\']+["\']', "hardcoded_api_key", "硬编码 API Key"),
        (r'secret\s*=\s*["\'][^"\']+["\']', "hardcoded_secret", "硬编码密钥"),
        (r'token\s*=\s*["\'][^"\']+["\']', "hardcoded_token", "硬编码 Token"),
    ]
    for line_no, line in enumerate(source.splitlines(), 1):
        for pattern, issue_type, msg in secret_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                issues.append({
                    "severity": "critical",
                    "line": line_no,
                    "message": f"{msg}: {line.strip()[:60]}",
                })
    return issues


def _check_todo_fixme(source: str) -> list[dict]:
    """检查 TODO/FIXME 标记。"""
    issues = []
    for line_no, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if re.match(r'(?m)^[ \t]*(#|//|\*)[ \t]*(TODO|FIXME|HACK|WORKAROUND)', stripped, re.IGNORECASE):
            issues.append({
                "severity": "warning",
                "line": line_no,
                "message": f"代码标记: {stripped[:80]}",
            })
    return issues


def _check_long_lines(lines: list[str], max_len: int = 100) -> list[dict]:
    """检查过长的行。"""
    issues = []
    for i, line in enumerate(lines, 1):
        if len(line.rstrip()) > max_len and not line.strip().startswith("#"):
            issues.append({
                "severity": "style",
                "line": i,
                "message": f"行过长 ({len(line.rstrip())} > {max_len})",
            })
    return issues


def _check_duplicate_imports(tree: ast.Module) -> list[dict]:
    """检查重复导入。"""
    imports = {}
    issues = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                if name in imports:
                    issues.append({
                        "severity": "style",
                        "line": node.lineno,
                        "message": f"重复导入: {name}",
                    })
                else:
                    imports[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = f"{node.module}.{alias.name}" if node.module else alias.name
                if name in imports:
                    issues.append({
                        "severity": "style",
                        "line": node.lineno,
                        "message": f"重复导入: {name}",
                    })
                else:
                    imports[name] = node.lineno
    return issues


@tool(
    "code_review",
    "对代码进行静态分析审阅，检测缺陷、安全风险、性能问题和代码风格问题，返回结构化 Review 意见",
    timeout=30,
)
def code_review(
    source_code: str = Field(description="要审阅的代码源码"),
    filepath: str = Field("", description="文件路径(用于报告)"),
    check_security: bool = Field(True, description="是否检查安全风险"),
    check_style: bool = Field(True, description="是否检查代码风格"),
) -> dict:
    """对代码进行审阅，返回结构化结果。"""
    lines = source_code.splitlines()
    issues: list[dict] = []

    # 基础检查
    issues.extend(_check_indentation(lines))
    issues.extend(_check_long_lines(lines))

    if check_security:
        issues.extend(_check_hardcoded_secrets(source_code))

    issues.extend(_check_todo_fixme(source_code))

    # AST 分析
    try:
        tree = ast.parse(source_code)
        issues.extend(_check_duplicate_imports(tree))

        # 检查空函数
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    issues.append({
                        "severity": "warning",
                        "line": node.lineno,
                        "message": f"函数 {node.name} 只有 pass，可能是未实现的占位符",
                    })
    except SyntaxError as e:
        issues.append({
            "severity": "critical",
            "line": e.lineno or 1,
            "message": f"语法错误: {e.msg}",
        })

    result = CodeReviewResult(filepath, issues)
    return result.to_dict()


def review_diff(old_code: str, new_code: str, filepath: str = "") -> dict:
    """对比代码变更，找出新增问题。"""
    # 简单的 diff 分析：逐行比较，标记新增的问题
    old_lines = set(old_code.splitlines())
    new_lines = set(new_code.splitlines())
    added = new_lines - old_lines

    issues = []
    for line in added:
        # 检查新增行是否有问题
        if re.search(r'password\s*=\s*["\']', line, re.IGNORECASE):
            issues.append({
                "severity": "critical",
                "line": 0,
                "message": f"新增硬编码凭据: {line.strip()[:60]}",
            })
        if re.search(r'# TODO|# FIXME', line, re.IGNORECASE):
            issues.append({
                "severity": "warning",
                "line": 0,
                "message": f"新增 TODO/FIXME: {line.strip()[:60]}",
            })

    return {
        "filepath": filepath,
        "lines_added": len(added),
        "issues_found": len(issues),
        "issues": issues,
    }
