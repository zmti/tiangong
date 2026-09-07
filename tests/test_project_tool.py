"""测试项目管理工具:scaffold/analyze/testgen/docs/build。"""
import os
import tempfile


class TestProjectScaffold:
    """测试 project_scaffold。"""

    def test_scaffold_python_library(self):
        from app.tools.project_tool import project_scaffold

        with tempfile.TemporaryDirectory() as tmpdir:
            result = project_scaffold.func(
                name="test_lib",
                description="A test library",
                template="python-library",
                output_dir=tmpdir,
            )
            assert result["ok"]
            assert result["file_count"] >= 4
            project_root = result["project_root"]
            assert os.path.isdir(project_root)
            # 核心文件应存在
            assert os.path.isfile(os.path.join(project_root, "pyproject.toml"))
            assert os.path.isfile(os.path.join(project_root, "README.md"))
            assert os.path.isfile(os.path.join(project_root, "src", "test_lib", "__init__.py"))
            assert os.path.isfile(os.path.join(project_root, "tests", "test_basic.py"))

    def test_scaffold_node_webapp(self):
        from app.tools.project_tool import project_scaffold

        with tempfile.TemporaryDirectory() as tmpdir:
            result = project_scaffold.func(
                name="my-app",
                description="A test web app",
                template="node-webapp",
                output_dir=tmpdir,
            )
            assert result["ok"]
            assert os.path.isfile(os.path.join(result["project_root"], "package.json"))
            assert os.path.isfile(os.path.join(result["project_root"], "README.md"))
            assert os.path.isfile(os.path.join(result["project_root"], "src", "index.js"))

    def test_scaffold_unknown_template(self):
        from app.tools.project_tool import project_scaffold

        result = project_scaffold.func(name="x", template="nonexistent")
        assert result["ok"] is False
        assert "未知模板" in result.get("error", "")


class TestProjectAnalyze:
    """测试 project_analyze。"""

    def test_analyze_existing_project(self):
        from app.tools.project_tool import project_analyze

        # 分析 TianGong 项目本身（确保路径存在）
        result = project_analyze.func(path="E:/dev/TianGong", max_files=50)
        assert result["ok"]
        assert result["total_files"] > 0
        assert isinstance(result["build_system"], str) or result["build_system"] is None

    def test_analyze_nonexistent_dir(self):
        from app.tools.project_tool import project_analyze

        result = project_analyze.func(path="/definitely/not/a/real/dir")
        assert result["ok"] is False
        assert "目录不存在" in result.get("error", "")


class TestProjectTestgen:
    """测试 project_testgen。"""

    def test_testgen_python_module(self):
        from app.tools.project_tool import project_testgen

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个简单的 Python 模块文件
            with open(os.path.join(tmpdir, "mymodule.py"), "w", encoding="utf-8") as f:
                f.write('def add(a, b):\n    return a + b\n\nclass Calculator:\n    def multiply(self, a, b):\n        return a * b\n')
            with open(os.path.join(tmpdir, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write('[project]\nname = "mylib"\n')

            result = project_testgen.func(target=os.path.join(tmpdir, "mymodule.py"), path=tmpdir)
            assert result["ok"], result.get("error")
            assert os.path.isfile(result["test_file"])
            content = open(result["test_file"], encoding="utf-8").read()
            assert "test_add" in content
            assert "TestCalculator" in content

    def test_testgen_nonexistent_target(self):
        from app.tools.project_tool import project_testgen

        with tempfile.TemporaryDirectory() as tmpdir:
            result = project_testgen.func(target="nonexistent_module", path=tmpdir)
            assert result["ok"] is False


class TestProjectDocs:
    """测试 project_docs。"""

    def test_docs_generates_readme(self):
        from app.tools.project_tool import project_docs

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个简单的 Python 项目
            pkg_dir = os.path.join(tmpdir, "src", "demo")
            os.makedirs(pkg_dir)
            with open(os.path.join(pkg_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write('def greet(name):\n    return f"Hello {name}"\n')
            with open(os.path.join(tmpdir, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write('[project]\nname = "demo"\n')

            result = project_docs.func(path=tmpdir)
            assert result["ok"]
            readme_path = os.path.join(tmpdir, "README.md")
            assert os.path.isfile(readme_path)
            content = open(readme_path, encoding="utf-8").read()
            assert "greet" in content

    def test_docs_appends_to_existing_readme(self):
        from app.tools.project_tool import project_docs

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "src", "app")
            os.makedirs(pkg_dir)
            with open(os.path.join(pkg_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write('def hello():\n    return "hi"\n')
            with open(os.path.join(tmpdir, "README.md"), "w", encoding="utf-8") as f:
                f.write("# My App\n\nSome existing content.\n")
            with open(os.path.join(tmpdir, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write('[project]\nname = "app"\n')

            result = project_docs.func(path=tmpdir)
            assert result["ok"]
            content = open(os.path.join(tmpdir, "README.md"), encoding="utf-8").read()
            assert "Some existing content." in content
            assert "hello" in content


class TestProjectBuild:
    """测试 project_build。"""

    def test_build_no_deps_no_tests(self):
        from app.tools.project_tool import project_build

        with tempfile.TemporaryDirectory() as tmpdir:
            # 空项目 - 没有依赖和测试
            result = project_build.func(path=tmpdir, install_deps=False, run_tests=False)
            assert result["ok"]
            assert len(result["steps"]) >= 1

    def test_build_detects_python_project(self):
        from app.tools.project_tool import project_build

        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "test"\n')
            # 没有 requirements.txt，跳过依赖安装
            result = project_build.func(path=tmpdir, install_deps=False, run_tests=False)
            assert result["ok"]
