"""测试项目菜单优化功能。"""
import pytest
import os
import sys
import json
import tempfile

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestProjectMenu:
    """项目菜单功能测试。"""

    @pytest.fixture
    def project_history_file(self, tmp_path):
        """创建临时项目历史文件。"""
        return str(tmp_path / "project_history.json")

    @pytest.fixture
    def mock_project_module(self, project_history_file):
        """mock 项目模块使用临时文件，每次测试前清空历史。"""
        import app.api.project as project_module
        original_func = project_module._history_file

        def mock_history_file():
            return project_history_file

        project_module._history_file = mock_history_file
        # 每次测试前清空历史文件，确保状态隔离
        with open(project_history_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        yield project_module
        project_module._history_file = original_func

    @pytest.fixture
    def temp_projects(self, tmp_path):
        """创建临时测试项目目录。"""
        proj1 = tmp_path / "project1"
        proj2 = tmp_path / "project2"
        proj3 = tmp_path / "test_project"
        proj1.mkdir(parents=True, exist_ok=True)
        proj2.mkdir(parents=True, exist_ok=True)
        proj3.mkdir(parents=True, exist_ok=True)
        return {"project1": str(proj1), "project2": str(proj2), "test_project": str(proj3)}

    def test_project_history_structure(self, mock_project_module, project_history_file):
        """测试项目历史记录的结构。"""
        from app.api.project import _load_history, _save_history

        # 保存测试数据
        test_projects = [
            {"path": "/test/project1", "name": "Project1", "selected_at": 1000.0},
            {"path": "/test/project2", "name": "Project2", "selected_at": 2000.0},
        ]
        
        _save_history(test_projects)
        loaded = _load_history()
        
        assert len(loaded) == 2
        assert loaded[0]["name"] == "Project1"
        assert loaded[1]["name"] == "Project2"

    def test_project_switch(self, mock_project_module, temp_projects):
        """测试项目切换功能。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 切换项目
        response = client.post("/v1/project/switch", json={"path": temp_projects["test_project"]})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["path"] == temp_projects["test_project"]

    def test_project_remove(self, mock_project_module, temp_projects):
        """测试项目删除功能。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 先添加项目
        client.post("/v1/project/switch", json={"path": temp_projects["test_project"]})
        
        # 删除项目
        response = client.post("/v1/project/remove", json={"path": temp_projects["test_project"]})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        
        # 验证已删除
        response = client.get("/v1/project/history")
        assert len(response.json()["projects"]) == 0

    def test_project_list(self, mock_project_module, temp_projects):
        """测试项目列表功能。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 添加几个项目
        client.post("/v1/project/switch", json={"path": temp_projects["project1"]})
        client.post("/v1/project/switch", json={"path": temp_projects["project2"]})
        
        # 获取列表
        response = client.get("/v1/project/list")
        assert response.status_code == 200
        data = response.json()
        assert len(data["projects"]) == 2

    def test_project_browse(self, mock_project_module):
        """测试项目浏览功能。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 浏览当前目录
        response = client.get("/v1/project/browse")
        assert response.status_code == 200
        data = response.json()
        assert "path" in data
        assert "name" in data
        assert data["is_default"] is True

    def test_project_remove_nonexistent(self, mock_project_module):
        """测试删除不存在的项目。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 删除不存在的项目
        response = client.post("/v1/project/remove", json={"path": "/nonexistent"})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

    def test_project_switch_invalid_path(self, mock_project_module):
        """测试切换到无效路径。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.project import router
        
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        
        # 切换到不存在的目录
        response = client.post("/v1/project/switch", json={"path": "/nonexistent/path"})
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "目录不存在" in data.get("error", "")
