"""项目管理工具:从零搭建完整项目。

核心能力:
1. project_scaffold - 根据描述生成项目骨架(模板+文件)
2. project_analyze - 分析现有项目的完整结构
3. project_testgen - 为现有代码生成测试骨架
4. project_docs - 自动生成 README / API 文档
5. project_build - 端到端构建验证整个项目
"""
from __future__ import annotations

import json
import os
import re
import textwrap
from typing import Optional

from pydantic import Field

from app.tools.project_context import resolve_cwd
from app.tools.registry import tool


# ==============================================================================
# 项目模板定义
# ==============================================================================

PROJECT_TEMPLATES: dict[str, dict] = {
    "python-library": {
        "description": "Python 库项目",
        "files": {
            "pyproject.toml": textwrap.dedent('''\
                [build-system]
                requires = ["setuptools>=68.0", "wheel"]
                build-backend = "setuptools.backends._legacy:_Backend"

                [project]
                name = "{{name}}"
                version = "0.1.0"
                description = "{{description}}"
                requires-python = ">=3.9"
                dependencies = []

                [project.optional-dependencies]
                dev = ["pytest>=8.0", "pytest-cov>=4.0"]

                [tool.setuptools.packages.find]
                where = ["src"]
            '''),
            "README.md": textwrap.dedent('''\
                # {{name}}

                {{description}}

                ## 安装

                ```bash
                pip install -e ".[dev]"
                ```

                ## 使用

                ```python
                from {{pkg_name}} import hello

                print(hello())
                ```

                ## 测试

                ```bash
                pytest
                ```
            '''),
            "src/{{pkg_name}}/__init__.py": textwrap.dedent('''\
                """{{name}} - {{description}}"""

                __version__ = "0.1.0"


                def hello() -> str:
                    """返回问候语。"""
                    return "Hello from {{name}}!"
            '''),
            "tests/test_basic.py": textwrap.dedent('''\
                """基础测试。"""
                import pytest

                def test_hello():
                    from {{pkg_name}} import hello
                    assert hello() == "Hello from {{name}}!"
            '''),
            ".gitignore": textwrap.dedent('''\
                __pycache__/
                *.pyc
                *.pyo
                .venv/
                dist/
                build/
                *.egg-info/
            '''),
        },
    },
    "python-webapp": {
        "description": "FastAPI Web 应用",
        "files": {
            "pyproject.toml": textwrap.dedent('''\
                [build-system]
                requires = ["setuptools>=68.0", "wheel"]
                build-backend = "setuptools.backends._legacy:_Backend"

                [project]
                name = "{{name}}"
                version = "0.1.0"
                description = "{{description}}"
                requires-python = ">=3.9"
                dependencies = ["fastapi>=0.111.0", "uvicorn>=0.29.0", "pydantic>=2.0"]

                [project.optional-dependencies]
                dev = ["pytest>=8.0", "httpx>=0.27.0"]

                [tool.setuptools.packages.find]
                where = ["src"]
            '''),
            "README.md": textwrap.dedent('''\
                # {{name}}

                {{description}}

                ## 启动

                ```bash
                uvicorn src.{{pkg_name}}.main:app --reload
                ```

                ## API 文档

                启动后访问 http://localhost:8000/docs
            '''),
            "src/{{pkg_name}}/__init__.py": '"""{{name}} package."""\n',
            "src/{{pkg_name}}/main.py": textwrap.dedent('''\
                """FastAPI 应用入口。"""
                from fastapi import FastAPI

                app = FastAPI(title="{{name}}", version="0.1.0")


                @app.get("/")
                async def root():
                    return {"message": "Hello World", "service": "{{name}}"}


                @app.get("/health")
                async def health():
                    return {"status": "ok"}
            '''),
            "tests/test_api.py": textwrap.dedent('''\
                """API 基础测试。"""
                from fastapi.testclient import TestClient
                from src.{{pkg_name}}.main import app

                client = TestClient(app)


                def test_root():
                    r = client.get("/")
                    assert r.status_code == 200
                    assert r.json()["service"] == "{{name}}"

                def test_health():
                    r = client.get("/health")
                    assert r.json()["status"] == "ok"
            '''),
            ".gitignore": textwrap.dedent('''\
                __pycache__/
                *.pyc
                .venv/
                dist/
                build/
            '''),
        },
    },
    "node-webapp": {
        "description": "Node.js Web 应用",
        "files": {
            "package.json": textwrap.dedent('''\
                {
                  "name": "{{name}}",
                  "version": "0.1.0",
                  "description": "{{description}}",
                  "main": "src/index.js",
                  "scripts": {
                    "start": "node src/index.js",
                    "dev": "node --watch src/index.js",
                    "test": "jest"
                  },
                  "dependencies": {
                    "express": "^4.18.2"
                  },
                  "devDependencies": {
                    "jest": "^29.7.0"
                  }
                }
            '''),
            "README.md": textwrap.dedent('''\
                # {{name}}

                {{description}}

                ## 启动

                ```bash
                npm install
                npm start
                ```

                ## 测试

                ```bash
                npm test
                ```
            '''),
            "src/index.js": textwrap.dedent('''\
                const express = require('express');
                const app = express();
                const PORT = process.env.PORT || 3000;

                app.get('/', (req, res) => {
                  res.json({ message: 'Hello World', service: '{{name}}' });
                });

                app.get('/health', (req, res) => {
                  res.json({ status: 'ok' });
                });

                app.listen(PORT, () => {
                  console.log(`{{name}} listening on port ${PORT}`);
                });

                module.exports = app;
            '''),
            "tests/test_api.test.js": textwrap.dedent('''\
                const request = require('supertest');
                const app = require('../src/index');

                describe('{{name}} API', () => {
                  test('GET / returns greeting', async () => {
                    const res = await request(app).get('/');
                    expect(res.status).toBe(200);
                    expect(res.body.service).toBe('{{name}}');
                  });

                  test('GET /health returns ok', async () => {
                    const res = await request(app).get('/health');
                    expect(res.body.status).toBe('ok');
                  });
                });
            '''),
            ".gitignore": "node_modules/\n",
        },
    },
    "python-data": {
        "description": "Python 数据处理项目",
        "files": {
            "pyproject.toml": textwrap.dedent('''\
                [build-system]
                requires = ["setuptools>=68.0", "wheel"]
                build-backend = "setuptools.backends._legacy:_Backend"

                [project]
                name = "{{name}}"
                version = "0.1.0"
                description = "{{description}}"
                requires-python = ">=3.9"
                dependencies = ["pandas>=2.0", "numpy>=1.24"]

                [project.optional-dependencies]
                dev = ["pytest>=8.0", "pytest-cov>=4.0"]
            '''),
            "README.md": "# {{name}}\n\n{{description}}\n",
            "src/{{pkg_name}}/__init__.py": '"""{{name}} package."""\n',
            "tests/test_data.py": '',
        },
    },
}


def _render_template(template: str, context: dict) -> str:
    """渲染 Jinja2-like 模板(仅支持 {{key}} 插值)。"""
    result = template
    for k, v in context.items():
        result = result.replace("{{" + k + "}}", str(v))
    return result


def _resolve_file_name(filename: str, context: dict) -> str:
    """解析文件名中的模板变量。"""
    return _render_template(filename, context)


# ==============================================================================
# 工具实现
# ==============================================================================


@tool(
    "project_scaffold",
    "根据项目描述生成完整的项目骨架(目录结构 + 配置文件 + 示例代码 + 测试)。"
    "支持 Python 库/WebApp、Node.js WebApp 等模板。"
    "适合从零开始创建一个新项目。",
    timeout=60,
)
def project_scaffold(
    name: str = Field(description="项目名称"),
    description: str = Field("", description="项目描述"),
    template: str = Field("python-library", description="项目模板: python-library / python-webapp / node-webapp / python-data"),
    output_dir: str = Field("", description="输出目录,留空用当前目录"),
) -> dict:
    """根据模板生成项目骨架。"""
    tpl = PROJECT_TEMPLATES.get(template)
    if not tpl:
        return {"ok": False, "error": f"未知模板: {template}。可用: {', '.join(PROJECT_TEMPLATES.keys())}"}

    pkg_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name.lower()).strip('_')
    context = {
        "name": name,
        "description": description or f"{name} 项目",
        "pkg_name": pkg_name,
    }

    root = os.path.abspath(os.path.expanduser(output_dir or resolve_cwd()))
    project_root = os.path.join(root, name)
    os.makedirs(project_root, exist_ok=True)

    created = []
    for filename, content_tmpl in tpl["files"].items():
        target = _resolve_file_name(filename, context)
        full_path = os.path.join(project_root, target)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        content = _render_template(content_tmpl, context)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        created.append(target)

    return {
        "ok": True,
        "project_root": project_root,
        "template": template,
        "files_created": created,
        "file_count": len(created),
        "next_steps": [
            f"cd {name}",
            "运行构建/安装依赖 (run_build)",
            "运行测试 (run_test)",
            "根据需求修改 src/ 下的代码",
        ],
    }


@tool(
    "project_analyze",
    "分析现有项目的完整结构:文件树、模块依赖、符号大纲、构建系统。"
    "返回项目的全景视图，帮助 Agent 理解整体架构后再进行修改。",
    timeout=60,
)
def project_analyze(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    max_files: int = Field(200, description="最大分析的文件数"),
) -> dict:
    """分析项目结构。"""
    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))
    if not os.path.isdir(root):
        return {"path": root, "ok": False, "error": f"目录不存在: {root}"}

    # 1. 文件树
    tree: list[dict] = []
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if file_count >= max_files:
                break
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            try:
                size = os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                size = 0
            tree.append({"path": rel, "size": size})
            file_count += 1
        if file_count >= max_files:
            break

    # 2. 构建系统
    files_set = set(os.listdir(root))
    build_system = None
    if "pyproject.toml" in files_set or "requirements.txt" in files_set:
        build_system = "python-pip"
    elif "package.json" in files_set:
        build_system = "node-npm"
    elif "pom.xml" in files_set:
        build_system = "java-maven"
    elif "go.mod" in files_set:
        build_system = "go"
    elif "Cargo.toml" in files_set:
        build_system = "rust-cargo"

    # 3. 语言统计
    lang_counts: dict[str, int] = {}
    for entry in tree:
        ext = os.path.splitext(entry["path"])[1].lower()
        lang_counts[ext or "(no ext)"] = lang_counts.get(ext or "(no ext)", 0) + 1

    # 4. 顶层入口文件
    entry_points = []
    for entry in tree:
        p = entry["path"]
        if p in ("main.py", "index.js", "app.py", "__init__.py", "src/main.py"):
            entry_points.append(p)

    return {
        "ok": True,
        "path": root,
        "total_files": file_count,
        "build_system": build_system,
        "entry_points": entry_points,
        "language_stats": dict(sorted(lang_counts.items(), key=lambda x: -x[1])),
        "top_files": tree[:30],
    }


@tool(
    "project_testgen",
    "为指定模块/文件生成测试骨架。扫描代码中的函数/类，自动生成对应的 pytest/jest 测试模板。",
    timeout=60,
)
def project_testgen(
    target: str = Field(description="要生成测试的目标文件路径(相对于项目根)或模块名"),
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
) -> dict:
    """为代码生成测试骨架。"""
    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))
    target_full = os.path.join(root, target) if not os.path.isabs(target) else target

    # 检测测试框架
    files = set(os.listdir(root))
    has_pytest = "pytest.ini" in files or "pyproject.toml" in files or "setup.cfg" in files
    has_jest = "jest.config.js" in files or ("jest" in files and any("jest" in f for f in files))
    has_package_json = "package.json" in files

    # 读取目标文件
    if not os.path.isfile(target_full):
        # 尝试作为模块名
        module_path = os.path.join(root, "src", target.replace(".", os.sep) + ".py")
        if os.path.isfile(module_path):
            target_full = module_path
        else:
            return {"ok": False, "error": f"找不到目标: {target}"}

    try:
        with open(target_full, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError as e:
        return {"ok": False, "error": f"读取文件失败: {e}"}

    ext = os.path.splitext(target_full)[1].lower()
    test_cases = []

    if ext == ".py":
        import ast
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {"ok": False, "error": "源码有语法错误"}

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                params = [arg.arg for arg in node.args.args if arg.arg != "self"]
                test_cases.append({
                    "type": "function",
                    "name": node.name,
                    "params": params,
                    "line": node.lineno,
                })
            elif isinstance(node, ast.ClassDef):
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                test_cases.append({
                    "type": "class",
                    "name": node.name,
                    "methods": methods,
                    "line": node.lineno,
                })
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        # 简单正则提取函数/类
        func_pat = re.compile(r'(?:async\s+)?function\s+(\w+)\s*\(')
        class_pat = re.compile(r'class\s+(\w+)')
        for m in func_pat.finditer(source):
            test_cases.append({"type": "function", "name": m.group(1), "line": source[:m.start()].count("\n") + 1})
        for m in class_pat.finditer(source):
            test_cases.append({"type": "class", "name": m.group(1), "line": source[:m.start()].count("\n") + 1})

    # 生成测试文件路径
    base_name = os.path.splitext(os.path.basename(target_full))[0]
    if ext == ".py":
        test_dir = os.path.join(root, "tests")
        os.makedirs(test_dir, exist_ok=True)
        test_file = os.path.join(test_dir, f"test_{base_name}.py")
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        test_dir = os.path.join(root, "tests")
        os.makedirs(test_dir, exist_ok=True)
        test_file = os.path.join(test_dir, f"{base_name}.test.js")

    # 生成测试内容
    if ext == ".py":
        lines = [f'"""测试 {base_name}。"""']
        lines.append("import pytest\n")
        for tc in test_cases:
            if tc["type"] == "function":
                lines.append(f"\n\ndef test_{tc['name']}():")
                lines.append(f'    # TODO: 测试 {tc["name"]}({", ".join(tc["params"])})')
                lines.append("    pass")
            elif tc["type"] == "class":
                lines.append(f"\n\nclass Test{tc['name']}:")
                for method in tc.get("methods", []):
                    lines.append(f"    def test_{method}(self):")
                    lines.append(f'        # TODO: 测试 {method}')
                    lines.append("        pass")
        test_content = "\n".join(lines)
    else:
        func_names = [tc["name"] for tc in test_cases if tc["type"] == "function" and tc["name"] not in ("main", "App")]
        import_line = f"const {{ {', '.join(func_names)} }} = require('../src/{base_name}');"
        lines = [import_line + "\n"]
        lines.append("describe('{}', () => {{".format(base_name))
        for tc in test_cases:
            if tc["type"] == "function":
                lines.append(f"  test('should test {tc['name']}', () => {{")
                lines.append(f"    // TODO: implement test for {tc['name']}")
                lines.append("  });")
        lines.append("});")
        test_content = "\n".join(lines)

    with open(test_file, "w", encoding="utf-8") as f:
        f.write(test_content)

    return {
        "ok": True,
        "target": target_full,
        "test_file": test_file,
        "test_cases_generated": len(test_cases),
        "test_framework": "pytest" if ext == ".py" else "jest",
        "preview": test_content[:2000],
    }


@tool(
    "project_docs",
    "为项目生成文档:README 更新、API 文档(基于代码符号)、CHANGELOG 草稿。",
    timeout=60,
)
def project_docs(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    format: str = Field("markdown", description="输出格式: markdown / openapi"),
) -> dict:
    """生成项目文档。"""
    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))
    if not os.path.isdir(root):
        return {"path": root, "ok": False, "error": f"目录不存在: {root}"}

    # 收集所有符号
    all_symbols = []
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".py", ".js", ".ts", ".tsx", ".jsx"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    src = f.read()
            except OSError:
                continue

            if ext == ".py":
                try:
                    import ast
                    tree = ast.parse(src)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            all_symbols.append({
                                "file": rel,
                                "line": node.lineno,
                                "name": node.name,
                                "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                            })
                except SyntaxError:
                    pass
            elif ext in (".js", ".ts", ".jsx", ".tsx"):
                for m in re.finditer(r'(?:async\s+)?function\s+(\w+)|class\s+(\w+)', src):
                    name = m.group(1) or m.group(2)
                    line = src[:m.start()].count("\n") + 1
                    kind = "function" if m.group(1) else "class"
                    all_symbols.append({"file": rel, "line": line, "name": name, "kind": kind})

    # 生成 Markdown 文档
    pkg_name = os.path.basename(root)
    docs_lines = [
        f"# {pkg_name}",
        "",
        "## API 参考",
        "",
        f"共发现 {len(all_symbols)} 个符号定义:",
        "",
    ]

    # 按文件分组
    by_file: dict[str, list[dict]] = {}
    for s in all_symbols:
        by_file.setdefault(s["file"], []).append(s)

    for fname, syms in sorted(by_file.items()):
        docs_lines.append(f"### `{fname}`")
        docs_lines.append("")
        docs_lines.append("| 类型 | 名称 | 行号 |")
        docs_lines.append("|------|------|------|")
        for s in sorted(syms, key=lambda x: x["line"]):
            kind_icon = "📁" if s["kind"] == "class" else "⚡"
            docs_lines.append(f"| {kind_icon} {s['kind']} | `{s['name']}` | L{s['line']} |")
        docs_lines.append("")

    docs_content = "\n".join(docs_lines)

    # 写入 README
    readme_path = os.path.join(root, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if "## API 参考" not in existing:
            with open(readme_path, "a", encoding="utf-8") as f:
                f.write("\n" + docs_content)
    else:
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(f"# {pkg_name}\n\n{docs_content}")

    return {
        "ok": True,
        "path": root,
        "symbols_found": len(all_symbols),
        "readme_updated": not os.path.exists(readme_path) or "## API 参考" not in open(readme_path, encoding="utf-8").read() if os.path.exists(readme_path) else True,
        "docs_preview": docs_content[:2000],
    }


@tool(
    "project_build",
    "端到端构建验证:自动检测构建系统 → 安装依赖 → 运行测试 → 生成报告。"
    "适合在项目修改完成后验证整个项目是否正常工作。",
    timeout=300,
)
def project_build(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    install_deps: bool = Field(True, description="是否先安装依赖"),
    run_tests: bool = Field(True, description="是否运行测试"),
    lint: bool = Field(False, description="是否运行 linter"),
) -> dict:
    """端到端构建验证。"""
    import subprocess
    import shutil

    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))
    if not os.path.isdir(root):
        return {"path": root, "ok": False, "error": f"目录不存在: {root}"}

    report: dict = {"path": root, "steps": []}

    # 步骤 1: 安装依赖
    if install_deps:
        files = set(os.listdir(root))
        deps_ok = False
        if "requirements.txt" in files and shutil.which("pip"):
            r = subprocess.run(["pip", "install", "-r", "requirements.txt"],
                             capture_output=True, text=True, timeout=120,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deps_ok = r.returncode == 0
            report["steps"].append({"step": "install_deps", "ok": deps_ok, "output": (r.stdout or r.stderr or "")[:500]})
        elif "package.json" in files and shutil.which("npm"):
            r = subprocess.run(["npm", "install"], capture_output=True, text=True, timeout=120,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deps_ok = r.returncode == 0
            report["steps"].append({"step": "install_deps", "ok": deps_ok, "output": (r.stdout or r.stderr or "")[:500]})
        else:
            report["steps"].append({"step": "install_deps", "ok": True, "output": "无需安装依赖"})

    # 步骤 2: 运行测试
    if run_tests:
        files = set(os.listdir(root))
        test_ok = False
        if "pytest.ini" in files or "pyproject.toml" in files:
            r = subprocess.run(["pytest", "-q"], capture_output=True, text=True, timeout=180,
                             cwd=root,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            test_ok = r.returncode == 0
            report["steps"].append({"step": "run_tests", "ok": test_ok, "output": (r.stdout or "")[:1000]})
        elif "package.json" in files:
            import json
            try:
                pkg = json.loads(open(os.path.join(root, "package.json"), encoding="utf-8").read())
                test_script = pkg.get("scripts", {}).get("test", "")
                if test_script:
                    cmd = test_script.split()
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                     cwd=root,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    test_ok = r.returncode == 0
                    report["steps"].append({"step": "run_tests", "ok": test_ok, "output": (r.stdout or "")[:1000]})
            except Exception:
                pass
        else:
            report["steps"].append({"step": "run_tests", "ok": True, "output": "未找到测试配置"})

    # 步骤 3: Lint (可选)
    if lint:
        files = set(os.listdir(root))
        lint_ok = False
        if "pyproject.toml" in files and shutil.which("ruff"):
            r = subprocess.run(["ruff", "check", "."], capture_output=True, text=True, timeout=60,
                             cwd=root,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            lint_ok = r.returncode == 0
            report["steps"].append({"step": "lint", "ok": lint_ok, "output": (r.stdout or r.stderr or "")[:500]})
        else:
            report["steps"].append({"step": "lint", "ok": True, "output": "跳过 lint (未配置)"})

    report["ok"] = all(s.get("ok", True) for s in report["steps"])
    return report
