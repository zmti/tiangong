"""工作流 API 测试 - 同步版本。"""
import pytest


def test_list_workflows():
    """测试列出工作流。"""
    import httpx
    with httpx.Client(base_url="http://127.0.0.1:8517") as client:
        resp = client.get("/v1/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert "workflows" in data
        assert isinstance(data["workflows"], list)


def test_create_workflow():
    """测试创建工作流。"""
    import httpx
    with httpx.Client(base_url="http://127.0.0.1:8517") as client:
        workflow_data = {
            "name": "测试工作流",
            "description": "用于测试的工作流",
            "nodes": [
                {"id": "start", "type": "start", "name": "开始"},
                {"id": "agent", "type": "agent", "name": "Agent", "config": {"input": "test"}},
                {"id": "end", "type": "end", "name": "结束"}
            ],
            "edges": [
                {"source": "start", "target": "agent"},
                {"source": "agent", "target": "end"}
            ],
            "entry_node": "start"
        }
        
        resp = client.post("/v1/workflows", json=workflow_data)
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data


def test_get_workflow():
    """测试获取工作流详情。"""
    import httpx
    with httpx.Client(base_url="http://127.0.0.1:8517") as client:
        # 先创建
        create_resp = client.post("/v1/workflows", json={
            "name": "获取测试",
            "nodes": [
                {"id": "start", "type": "start", "name": "开始"},
                {"id": "end", "type": "end", "name": "结束"}
            ],
            "edges": [{"source": "start", "target": "end"}],
            "entry_node": "start"
        })
        
        workflow_id = create_resp.json()["id"]
        
        # 获取详情
        resp = client.get(f"/v1/workflows/{workflow_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == workflow_id
        assert data["name"] == "获取测试"


def test_delete_workflow():
    """测试删除工作流。"""
    import httpx
    with httpx.Client(base_url="http://127.0.0.1:8517") as client:
        # 先创建
        create_resp = client.post("/v1/workflows", json={
            "name": "删除测试",
            "nodes": [
                {"id": "start", "type": "start", "name": "开始"},
                {"id": "end", "type": "end", "name": "结束"}
            ],
            "edges": [{"source": "start", "target": "end"}],
            "entry_node": "start"
        })
        
        workflow_id = create_resp.json()["id"]
        
        # 删除
        resp = client.delete(f"/v1/workflows/{workflow_id}")
        assert resp.status_code == 200


def test_workflow_not_found():
    """测试不存在的 worklow。"""
    import httpx
    with httpx.Client(base_url="http://127.0.0.1:8517") as client:
        resp = client.get("/v1/workflows/nonexistent")
        assert resp.status_code == 404
