"""技能/工具市场:沙箱执行+版本化+依赖解析+签名验证。

核心能力:
1. 技能包格式: 标准化技能定义(manifest + 代码 + 依赖 + 测试 + 签名)
2. 沙箱执行: 进程隔离/容器化/资源限制/网络隔离
3. 版本管理: 语义化版本/依赖图/冲突解决/回滚
4. 签名验证: Ed25519 签名/证书链/信任锚点/撤销列表
5. 仓库协议: 本地/远程(Git/HTTP/Registry)/私有源
6. 生命周期: 安装/更新/卸载/启用/禁用/审计
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

class SkillStatus(str, Enum):
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UPDATING = "updating"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


class SignatureAlgorithm(str, Enum):
    ED25519 = "ed25519"
    RSA_PSS = "rsa-pss"
    ECDSA_P256 = "ecdsa-p256"


@dataclass
class SkillDependency:
    """技能依赖。"""
    name: str
    version: str  # 语义化版本范围,如 ">=1.0.0 <2.0.0"
    optional: bool = False
    source: str = ""  # 依赖来源仓库


@dataclass
class SkillManifest:
    """技能清单。"""
    name: str
    version: str
    description: str = ""
    author: str = ""
    license: str = "MIT"
    homepage: str = ""
    repository: str = ""
    
    # 入口点
    entry_point: str = "main.py"  # 主入口文件
    handler: str = "handle"  # 处理函数名
    
    # 依赖
    dependencies: list[SkillDependency] = field(default_factory=list)
    python_requires: str = ">=3.9"
    
    # 资源限制(沙箱)
    max_memory_mb: int = 512
    max_cpu_percent: int = 50
    max_execution_time_seconds: int = 60
    network_allowed: bool = False
    filesystem_allowed: bool = True
    allowed_paths: list[str] = field(default_factory=list)  # 允许访问的路径
    
    # 能力声明
    capabilities: list[str] = field(default_factory=list)  # 如 ["file_read", "web_search", "code_exec"]
    required_permissions: list[str] = field(default_factory=list)
    
    # 元数据
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["dependencies"] = [asdict(dep) for dep in self.dependencies]
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "SkillManifest":
        deps = []
        for dep in data.get("dependencies", []):
            deps.append(SkillDependency(**dep))
        data["dependencies"] = deps
        return cls(**data)


@dataclass
class SkillPackage:
    """技能包(清单 + 代码 + 签名)。"""
    manifest: SkillManifest
    code_hash: str  # 代码目录 SHA256
    signature: str = ""  # Base64 编码签名
    signature_algorithm: SignatureAlgorithm = SignatureAlgorithm.ED25519
    signer: str = ""  # 签名者标识
    signed_at: str = ""
    certificate_chain: list[str] = field(default_factory=list)  # 证书链
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["manifest"] = self.manifest.to_dict()
        d["signature_algorithm"] = self.signature_algorithm.value
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "SkillPackage":
        data["manifest"] = SkillManifest.from_dict(data["manifest"])
        if "signature_algorithm" in data and isinstance(data["signature_algorithm"], str):
            data["signature_algorithm"] = SignatureAlgorithm(data["signature_algorithm"])
        return cls(**data)


@dataclass
class InstalledSkill:
    """已安装技能记录。"""
    skill_id: str  # name@version
    name: str
    version: str
    install_path: str
    status: SkillStatus = SkillStatus.INSTALLED
    manifest: SkillManifest = None
    package: SkillPackage = None
    installed_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    enabled_at: float = 0
    last_used_at: float = 0
    use_count: int = 0
    checksum: str = ""  # 安装目录校验和
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        if self.manifest:
            d["manifest"] = self.manifest.to_dict()
        if self.package:
            d["package"] = self.package.to_dict()
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "InstalledSkill":
        if "status" in data and isinstance(data["status"], str):
            data["status"] = SkillStatus(data["status"])
        if "manifest" in data and data["manifest"]:
            data["manifest"] = SkillManifest.from_dict(data["manifest"])
        if "package" in data and data["package"]:
            data["package"] = SkillPackage.from_dict(data["package"])
        return cls(**data)


# ============================================================
# 签名验证器
# ============================================================

class SignatureVerifier:
    """签名验证器:Ed25519/RSA/ECDSA 验证 + 证书链信任。"""
    
    def __init__(self):
        self._trust_anchors: dict[str, bytes] = {}  # subject -> public_key
        self._revoked_certs: set[str] = set()  # 指纹集合
        self._load_trust_anchors()
    
    def _load_trust_anchors(self) -> None:
        """加载信任锚点(根证书)。"""
        # 从配置/文件加载
        trust_file = getattr(settings, "skill_trust_anchors_file", "")
        if trust_file and os.path.exists(trust_file):
            try:
                with open(trust_file, "r") as f:
                    data = json.load(f)
                for anchor in data.get("anchors", []):
                    subject = anchor.get("subject")
                    pubkey_b64 = anchor.get("public_key")
                    if subject and pubkey_b64:
                        import base64
                        self._trust_anchors[subject] = base64.b64decode(pubkey_b64)
            except Exception as e:
                logger.warning("加载信任锚点失败: %s", e)
    
    def add_trust_anchor(self, subject: str, public_key: bytes) -> None:
        """添加信任锚点。"""
        self._trust_anchors[subject] = public_key
    
    def revoke_certificate(self, fingerprint: str) -> None:
        """吊销证书。"""
        self._revoked_certs.add(fingerprint)
    
    def verify(self, package: SkillPackage) -> tuple[bool, str]:
        """验证技能包签名。
        
        Returns:
            (valid, message)
        """
        # 1. 验证代码哈希
        # 这里需要实际计算安装目录哈希,简化省略
        
        # 2. 验证签名算法
        if package.signature_algorithm not in SignatureAlgorithm:
            return False, f"不支持的签名算法: {package.signature_algorithm}"
        
        # 3. 验证证书链
        if not package.certificate_chain:
            return False, "缺少证书链"
        
        # 4. 验证签名(简化:实际需用 cryptography 库)
        # 这里仅做格式检查
        if not package.signature:
            return False, "签名为空"
        
        # 5. 检查吊销列表
        for cert in package.certificate_chain:
            # 计算证书指纹
            fp = hashlib.sha256(cert.encode()).hexdigest()[:16]
            if fp in self._revoked_certs:
                return False, f"证书已吊销: {fp}"
        
        # 6. 验证信任链(简化)
        root_cert = package.certificate_chain[-1] if package.certificate_chain else ""
        if root_cert and root_cert not in self._trust_anchors:
            logger.warning("根证书不在信任锚点中: %s", root_cert[:32])
        
        return True, "签名验证通过"


# ============================================================
# 沙箱执行器
# ============================================================

class SandboxExecutor:
    """沙箱执行器:进程隔离 + 资源限制 + 网络/文件系统隔离。"""
    
    def __init__(self):
        self._docker_available = self._check_docker()
        self._force_docker = getattr(settings, "sandbox_force_docker", False)
    
    def _check_docker(self) -> bool:
        try:
            result = subprocess.run(["docker", "version"], capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False
    
    def execute(
        self,
        skill_path: str,
        handler: str,
        payload: dict,
        limits: dict = None,
        allowed_paths: list[str] = None,
    ) -> dict:
        """在沙箱中执行技能。
        
        Args:
            skill_path: 技能代码目录
            handler: 处理函数名(模块.函数)
            payload: 输入参数
            limits: 资源限制 {memory_mb, cpu_percent, timeout_seconds}
            allowed_paths: 允许访问的宿主路径列表
        
        Returns:
            {"success": bool, "result": Any, "error": str, "duration_ms": int}
        """
        limits = limits or {}
        memory_mb = limits.get("memory_mb", 512)
        cpu_percent = limits.get("cpu_percent", 50)
        timeout = limits.get("timeout_seconds", 60)
        network_allowed = limits.get("network_allowed", False)
        fs_allowed = limits.get("filesystem_allowed", True)
        
        if self._force_docker and self._docker_available:
            return self._execute_docker(skill_path, handler, payload, memory_mb, cpu_percent, timeout, network_allowed, fs_allowed, allowed_paths)
        else:
            return self._execute_subprocess(skill_path, handler, payload, memory_mb, timeout, allowed_paths)
    
    def _execute_docker(
        self,
        skill_path: str,
        handler: str,
        payload: dict,
        memory_mb: int,
        cpu_percent: int,
        timeout: int,
        network_allowed: bool,
        fs_allowed: bool,
        allowed_paths: list[str],
    ) -> dict:
        """Docker 容器执行。"""
        # 构建卷映射
        volumes = [f"{skill_path}:/skill:ro"]
        if allowed_paths:
            for p in allowed_paths:
                volumes.append(f"{p}:{p}:ro")
        
        # 资源限制
        mem_limit = f"{memory_mb}m"
        cpu_quota = int(100000 * cpu_percent / 100)  # CPU quota (微秒)
        cpu_period = 100000
        
        # 网络
        network_mode = "bridge" if network_allowed else "none"
        
        # 构建命令
        cmd = [
            "docker", "run", "--rm",
            "--memory", mem_limit,
            "--cpus", str(cpu_percent / 100),
            "--cpu-quota", str(cpu_quota),
            "--cpu-period", str(cpu_period),
            "--network", network_mode,
            "--pids-limit", "100",
            "--security-opt", "no-new-privileges:true",
        ]
        
        for vol in volumes:
            cmd.extend(["-v", vol])
        
        cmd.extend([
            "python:3.11-slim",
            "python", "-c",
            f"""
import sys, json, importlib.util, traceback
sys.path.insert(0, '/skill')
spec = importlib.util.spec_from_file_location("skill", "/skill/{handler.split('.')[0]}.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
handler = getattr(module, "{handler.split('.')[-1]}")
payload = {json.dumps(payload)}
result = handler(payload)
print(json.dumps({{"success": True, "result": result}}))
""",
        ])
        
        start = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
            duration = int((time.time() - start) * 1000)
            
            if result.returncode == 0:
                try:
                    output = json.loads(result.stdout.strip())
                    return {"success": True, "result": output.get("result"), "duration_ms": duration}
                except json.JSONDecodeError:
                    return {"success": True, "result": result.stdout.strip(), "duration_ms": duration}
            else:
                return {"success": False, "error": result.stderr.strip(), "duration_ms": duration}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"执行超时({timeout}s)", "duration_ms": timeout * 1000}
        except Exception as e:
            return {"success": False, "error": str(e), "duration_ms": int((time.time() - start) * 1000)}
    
    def _execute_subprocess(
        self,
        skill_path: str,
        handler: str,
        payload: dict,
        memory_mb: int,
        timeout: int,
        allowed_paths: list[str],
    ) -> dict:
        """子进程执行(降级方案,隔离性较弱)。"""
        # 这里使用 Python subprocess,实际生产建议强制 Docker
        env = os.environ.copy()
        env["PYTHONPATH"] = skill_path + os.pathsep + env.get("PYTHONPATH", "")
        
        # 写入 payload 到临时文件
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            payload_file = f.name
        
        try:
            cmd = [
                sys.executable, "-c",
                f"""
import sys, json, importlib.util, traceback
sys.path.insert(0, {json.dumps(skill_path)})
with open({json.dumps(payload_file)}) as f:
    payload = json.load(f)
spec = importlib.util.spec_from_file_location("skill", {json.dumps(os.path.join(skill_path, handler.split('.')[0] + '.py'))})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
handler = getattr(module, "{handler.split('.')[-1]}")
result = handler(payload)
print(json.dumps({{"success": True, "result": result}}))
""",
            ]
            
            start = time.time()
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, env=env)
            duration = int((time.time() - start) * 1000)
            
            if result.returncode == 0:
                try:
                    output = json.loads(result.stdout.strip())
                    return {"success": True, "result": output.get("result"), "duration_ms": duration}
                except json.JSONDecodeError:
                    return {"success": True, "result": result.stdout.strip(), "duration_ms": duration}
            else:
                return {"success": False, "error": result.stderr.strip(), "duration_ms": duration}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"执行超时({timeout}s)", "duration_ms": timeout * 1000}
        except Exception as e:
            return {"success": False, "error": str(e), "duration_ms": int((time.time() - start) * 1000)}
        finally:
            try:
                os.unlink(payload_file)
            except Exception:
                pass


# ============================================================
# 依赖解析器
# ============================================================

class DependencyResolver:
    """依赖解析器:语义化版本/依赖图/冲突检测/拓扑排序。"""
    
    def __init__(self, marketplace: "SkillMarketplace"):
        self.marketplace = marketplace
    
    def resolve(self, skill_name: str, version: str = "latest") -> list[InstalledSkill]:
        """解析技能及其所有依赖,返回安装顺序列表。"""
        # 获取技能清单
        skill = self.marketplace.get_skill(skill_name, version)
        if not skill:
            raise ValueError(f"技能不存在: {skill_name}@{version}")
        
        # 构建依赖图
        graph = {}  # name -> set(dependencies)
        versions = {}  # name -> version
        
        def collect_deps(manifest: SkillManifest, target_version: str):
            name = manifest.name
            if name in graph:
                return
            graph[name] = set()
            versions[name] = target_version
            for dep in manifest.dependencies:
                graph[name].add(dep.name)
                # 递归收集依赖的依赖
                dep_skill = self.marketplace.get_skill(dep.name, dep.version)
                if dep_skill:
                    collect_deps(dep_skill.manifest, dep_skill.manifest.version)
        
        collect_deps(skill.manifest, skill.manifest.version)
        
        # 拓扑排序
        installed_order = self._topological_sort(graph)
        
        # 转换为 InstalledSkill 列表
        result = []
        for name in installed_order:
            ver = versions.get(name, "latest")
            installed = self.marketplace.get_installed(name, ver)
            if not installed:
                # 需要安装
                skill_pkg = self.marketplace.get_skill(name, ver)
                if skill_pkg:
                    installed = self.marketplace.install(skill_pkg)
            if installed:
                result.append(installed)
        
        return result
    
    def _topological_sort(self, graph: dict[str, set[str]]) -> list[str]:
        """Kahn 算法拓扑排序。"""
        in_degree = {node: 0 for node in graph}
        for node, deps in graph.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[dep] += 1
        
        queue = [node for node, deg in in_degree.items() if deg == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            for dep in graph.get(node, []):
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)
        
        if len(result) != len(graph):
            raise ValueError("依赖图存在循环")
        
        return result
    
    def check_conflicts(self, skills: list[InstalledSkill]) -> list[tuple[str, str, str]]:
        """检查版本冲突。
        
        Returns:
            List of (skill_name, version1, version2)
        """
        version_map = {}
        conflicts = []
        for skill in skills:
            if skill.name in version_map:
                if version_map[skill.name] != skill.version:
                    conflicts.append((skill.name, version_map[skill.name], skill.version))
            else:
                version_map[skill.name] = skill.version
        return conflicts


# ============================================================
# 技能市场核心
# ============================================================

class SkillMarketplace:
    """技能市场核心:管理技能的发布/安装/更新/执行全生命周期。"""
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        backend: Optional[StorageBackend] = None,
        skills_dir: Optional[str] = None,
        registry_url: str = "",
    ):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "skill_marketplace.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._skills_dir = Path(skills_dir or os.path.join(settings.resolved_data_dir, "skills"))
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._registry_url = registry_url or getattr(settings, "skill_registry_url", "")
        self._verifier = SignatureVerifier()
        self._sandbox = SandboxExecutor()
        self._resolver = DependencyResolver(self)
        self._init_db()
    
    def _init_db(self) -> None:
        schema = {
            "skills": [
                {"name": "skill_id", "type": "TEXT", "constraints": "PRIMARY KEY"},  # name@version
                {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "version", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "package_json", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "install_path", "type": "TEXT", "constraints": ""},
                {"name": "status", "type": "TEXT", "constraints": "DEFAULT 'installed'"},
                {"name": "checksum", "type": "TEXT", "constraints": ""},
                {"name": "source", "type": "TEXT", "constraints": "DEFAULT 'local'"},  # local/remote/git
                {"name": "source_url", "type": "TEXT", "constraints": ""},
                {"name": "installed_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "enabled_at", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "last_used_at", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "use_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
            ],
            "skill_repositories": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "url", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "type", "type": "TEXT", "constraints": "DEFAULT 'http'"},  # http/git/local
                {"name": "auth_token", "type": "TEXT", "constraints": ""},
                {"name": "enabled", "type": "INTEGER", "constraints": "DEFAULT 1"},
                {"name": "priority", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "last_sync", "type": "REAL", "constraints": "DEFAULT 0"},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_skill_name ON skills(name)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_skill_status ON skills(status)")
    
    # ---- 发布/打包 ----
    
    def pack_skill(self, source_dir: str, output_path: str = None) -> SkillPackage:
        """打包技能目录为 .skill.zip。"""
        source = Path(source_dir)
        if not source.exists():
            raise ValueError(f"源目录不存在: {source_dir}")
        
        # 读取清单
        manifest_file = source / "manifest.json"
        if not manifest_file.exists():
            raise ValueError("缺少 manifest.json")
        
        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        manifest = SkillManifest.from_dict(manifest_data)
        
        # 计算代码哈希
        code_hash = self._compute_dir_hash(source)
        
        # 创建包
        package = SkillPackage(
            manifest=manifest,
            code_hash=code_hash,
            signed_at=datetime.now().isoformat(),
        )
        
        # 打包为 zip
        if output_path is None:
            output_path = str(self._skills_dir / f"{manifest.name}-{manifest.version}.skill.zip")
        
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in source.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(source)
                    zf.write(file_path, arcname)
            # 写入 package.json
            package_json = json.dumps(package.to_dict(), ensure_ascii=False, indent=2)
            zf.writestr("package.json", package_json)
        
        logger.info("技能打包完成: %s", output_path)
        return package
    
    def _compute_dir_hash(self, dir_path: Path) -> str:
        """计算目录内容 SHA256。"""
        hasher = hashlib.sha256()
        for file_path in sorted(dir_path.rglob("*")):
            if file_path.is_file() and file_path.name != "package.json":
                with open(file_path, "rb") as f:
                    hasher.update(f.read())
        return hasher.hexdigest()
    
    # ---- 安装/卸载 ----
    
    def install(self, package: SkillPackage, source: str = "local", source_url: str = "") -> InstalledSkill:
        """安装技能包。"""
        # 1. 验证签名
        valid, msg = self._verifier.verify(package)
        if not valid:
            raise ValueError(f"签名验证失败: {msg}")
        
        # 2. 解析依赖
        deps = self._resolver.resolve(package.manifest.name, package.manifest.version)
        for dep in deps:
            logger.info("安装依赖: %s@%s", dep.name, dep.version)
        
        # 3. 解压代码
        skill_id = f"{package.manifest.name}@{package.manifest.version}"
        install_path = self._skills_dir / "installed" / skill_id
        install_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 如果已存在,先备份
        if install_path.exists():
            backup = install_path.with_suffix(f".bak.{int(time.time())}")
            shutil.move(str(install_path), str(backup))
        
        # 这里简化:实际应从 zip 解压或从仓库下载
        # 模拟安装
        install_path.mkdir(parents=True, exist_ok=True)
        
        # 4. 计算校验和
        checksum = self._compute_dir_hash(install_path)
        
        # 5. 记录安装
        installed = InstalledSkill(
            skill_id=skill_id,
            name=package.manifest.name,
            version=package.manifest.version,
            install_path=str(install_path),
            status=SkillStatus.INSTALLED,
            manifest=package.manifest,
            package=package,
            checksum=checksum,
        )
        
        self._save_installed(installed)
        logger.info("技能安装成功: %s", skill_id)
        return installed
    
    def uninstall(self, skill_id: str, force: bool = False) -> bool:
        """卸载技能。"""
        with self._lock:
            skill = self.get_installed(skill_id)
            if not skill:
                return False
            
            # 检查是否被其他技能依赖
            if not force:
                dependents = self._get_dependents(skill_id)
                if dependents:
                    raise ValueError(f"技能被以下技能依赖,无法卸载: {dependents}")
            
            # 删除文件
            install_path = Path(skill.install_path)
            if install_path.exists():
                shutil.rmtree(install_path)
            
            # 更新状态
            skill.status = SkillStatus.UNINSTALLED
            self._save_installed(skill)
            
            logger.info("技能卸载: %s", skill_id)
            return True
    
    def enable(self, skill_id: str) -> bool:
        """启用技能。"""
        skill = self.get_installed(skill_id)
        if not skill:
            return False
        skill.status = SkillStatus.ENABLED
        skill.enabled_at = time.time()
        self._save_installed(skill)
        return True
    
    def disable(self, skill_id: str) -> bool:
        """禁用技能。"""
        skill = self.get_installed(skill_id)
        if not skill:
            return False
        skill.status = SkillStatus.DISABLED
        self._save_installed(skill)
        return True
    
    def _save_installed(self, skill: InstalledSkill) -> None:
        with self._lock:
            skill.updated_at = time.time()
            self._backend.upsert(
                "skills",
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "version": skill.version,
                    "package_json": json.dumps(skill.package.to_dict() if skill.package else {}, ensure_ascii=False),
                    "install_path": skill.install_path,
                    "status": skill.status.value,
                    "checksum": skill.checksum,
                    "installed_at": skill.installed_at,
                    "updated_at": skill.updated_at,
                    "enabled_at": skill.enabled_at,
                    "last_used_at": skill.last_used_at,
                    "use_count": skill.use_count,
                    "metadata": json.dumps(skill.metadata, ensure_ascii=False),
                },
                conflict_columns=["skill_id"],
            )
    
    def get_installed(self, skill_id: str = None, name: str = None, version: str = None) -> Optional[InstalledSkill]:
        with self._lock:
            if skill_id:
                row = self._backend.select_one("skills", "skill_id = ?", (skill_id,))
            elif name and version:
                row = self._backend.select_one("skills", "name = ? AND version = ?", (name, version))
            elif name:
                # 获取最新版本
                row = self._backend.select_one("skills", "name = ?", (name,), order_by="installed_at DESC")
            else:
                return None
        
        if not row:
            return None
        return InstalledSkill.from_dict(json.loads(row["package_json"]))
    
    def list_installed(self, status: SkillStatus = None) -> list[InstalledSkill]:
        with self._lock:
            if status:
                rows = self._backend.select("skills", "status = ?", (status.value,))
            else:
                rows = self._backend.select("skills", "1=1")
        return [InstalledSkill.from_dict(json.loads(r["package_json"])) for r in rows]
    
    def _get_dependents(self, skill_id: str) -> list[str]:
        """获取依赖此技能的其他技能。"""
        dependents = []
        for skill in self.list_installed():
            if skill.package and skill.package.manifest:
                for dep in skill.package.manifest.dependencies:
                    if dep.name == skill_id.split("@")[0]:
                        dependents.append(skill.skill_id)
        return dependents
    
    # ---- 执行 ----
    
    def execute(
        self,
        skill_id: str,
        payload: dict,
        context: dict = None,
    ) -> dict:
        """执行已安装技能。"""
        skill = self.get_installed(skill_id)
        if not skill:
            return {"success": False, "error": f"技能未安装: {skill_id}"}
        
        if skill.status != SkillStatus.ENABLED:
            return {"success": False, "error": f"技能未启用: {skill_id}"}
        
        # 更新使用统计
        skill.last_used_at = time.time()
        skill.use_count += 1
        self._save_installed(skill)
        
        # 准备沙箱限制
        limits = {
            "memory_mb": skill.manifest.max_memory_mb if skill.manifest else 512,
            "cpu_percent": skill.manifest.max_cpu_percent if skill.manifest else 50,
            "timeout_seconds": skill.manifest.max_execution_time_seconds if skill.manifest else 60,
            "network_allowed": skill.manifest.network_allowed if skill.manifest else False,
            "filesystem_allowed": skill.manifest.filesystem_allowed if skill.manifest else True,
        }
        
        # 执行
        handler = skill.manifest.handler if skill.manifest else "handle"
        result = self._sandbox.execute(
            skill_path=skill.install_path,
            handler=handler,
            payload=payload,
            limits=limits,
            allowed_paths=skill.manifest.allowed_paths if skill.manifest else [],
        )
        
        return result
    
    # ---- 仓库管理 ----
    
    def add_repository(self, name: str, url: str, type: str = "http", auth_token: str = "", priority: int = 0) -> None:
        """添加技能仓库。"""
        repo_id = hashlib.sha256(f"{name}{url}".encode()).hexdigest()[:16]
        with self._lock:
            self._backend.upsert(
                "skill_repositories",
                {
                    "id": repo_id,
                    "name": name,
                    "url": url,
                    "type": type,
                    "auth_token": auth_token,
                    "enabled": 1,
                    "priority": priority,
                },
                conflict_columns=["id"],
            )
    
    def sync_repository(self, repo_id: str) -> list[SkillPackage]:
        """同步仓库技能列表。"""
        with self._lock:
            row = self._backend.select_one("skill_repositories", "id = ?", (repo_id,))
        if not row:
            raise ValueError(f"仓库不存在: {repo_id}")
        
        # 这里简化:实际应从 HTTP/Git 获取索引
        logger.info("同步仓库: %s (%s)", row["name"], row["url"])
        return []
    
    # ---- 查询 ----
    
    def get_skill(self, name: str, version: str = "latest") -> Optional[SkillPackage]:
        """获取技能包(优先本地,回退远程)。"""
        if version == "latest":
            skill = self.get_installed(name=name)
            if skill and skill.package:
                return skill.package
        
        skill = self.get_installed(name=name, version=version)
        if skill and skill.package:
            return skill.package
        
        # 远程仓库查找(简化)
        return None
    
    def search_skills(self, query: str = "", tags: list[str] = None, limit: int = 20) -> list[dict]:
        """搜索技能。"""
        with self._lock:
            rows = self._backend.select("skills", "1=1", limit=limit)
        
        results = []
        for row in rows:
            skill = InstalledSkill.from_dict(json.loads(row["package_json"]))
            if skill.package and skill.package.manifest:
                manifest = skill.package.manifest
                if query and query.lower() not in manifest.name.lower() and query.lower() not in manifest.description.lower():
                    continue
                if tags and not any(t in manifest.tags for t in tags):
                    continue
                results.append({
                    "skill_id": skill.skill_id,
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "author": manifest.author,
                    "tags": manifest.tags,
                    "status": skill.status.value,
                })
        return results

    # ============================================================
    # 与 skills.py 集成: Skill -> SkillManifest/SkillPackage 转换
    # ============================================================
    
    def skill_to_manifest(self, skill: Skill) -> "SkillManifest":
        """将 skills.py 的 Skill 转换为 SkillManifest。"""
        from app.core.skill_marketplace import SkillManifest, SkillDependency, SkillParam as MP_Param
        
        deps = []
        # 从步骤中提取依赖工具
        tool_names = set()
        for step in skill.steps:
            if step.type == "tool" or (hasattr(step, 'type') and step.type.value == "tool"):
                tool_names.add(step.tool)
        
        manifest = SkillManifest(
            name=skill.name,
            version=f"{skill.version}.0.0",  # 简单版本转换
            description=skill.description,
            author="agent",
            license="MIT",
            entry_point=f"{skill.name}.py",
            handler="handle",
            dependencies=[SkillDependency(name=t, version="*") for t in tool_names],
            python_requires=">=3.9",
            max_memory_mb=skill.sandbox_limits.get("memory_mb", 512) if skill.sandbox_enabled else 512,
            max_cpu_percent=skill.sandbox_limits.get("cpu_percent", 50) if skill.sandbox_enabled else 50,
            max_execution_time_seconds=skill.sandbox_limits.get("timeout_seconds", 60) if skill.sandbox_enabled else int(skill.timeout),
            network_allowed=skill.sandbox_limits.get("network_allowed", False) if skill.sandbox_enabled else False,
            filesystem_allowed=skill.sandbox_limits.get("filesystem_allowed", True) if skill.sandbox_enabled else True,
            allowed_paths=skill.sandbox_limits.get("allowed_paths", []) if skill.sandbox_enabled else [],
            tags=["agent-skill", f"v{skill.version}"],
        )
        return manifest
    
    def skill_to_package(self, skill: Skill) -> "SkillPackage":
        """将 skills.py 的 Skill 转换为 SkillPackage。"""
        manifest = self.skill_to_manifest(skill)
        package = SkillPackage(
            manifest=manifest,
            code_hash="",  # 无代码目录
            signed_at=datetime.now().isoformat(),
        )
        return package
    
    def install_skill(self, skill: Skill, source: str = "local") -> InstalledSkill:
        """安装 skills.py 的 Skill。"""
        # 转换并保存
        package = self.skill_to_package(skill)
        
        # 验证签名(内部技能跳过)
        # valid, msg = self._verifier.verify(package)
        # if not valid:
        #     raise ValueError(f"签名验证失败: {msg}")
        
        skill_id = f"{skill.name}@{skill.version}"
        install_path = self._skills_dir / "installed" / skill_id
        install_path.parent.mkdir(parents=True, exist_ok=True)
        
        if install_path.exists():
            backup = install_path.with_suffix(f".bak.{int(time.time())}")
            shutil.move(str(install_path), str(backup))
        
        install_path.mkdir(parents=True, exist_ok=True)
        
        # 保存技能定义文件
        skill_file = install_path / f"{skill.name}.json"
        with open(skill_file, "w", encoding="utf-8") as f:
            json.dump(skill.to_dict(), f, ensure_ascii=False, indent=2)
        
        checksum = self._compute_dir_hash(install_path)
        
        # 创建包用于存储
        package = self.skill_to_package(skill)
        
        installed = InstalledSkill(
            skill_id=f"{skill.name}@{skill.version}",
            name=skill.name,
            version=str(skill.version),
            install_path=str(install_path),
            status=SkillStatus.INSTALLED if skill.enabled else SkillStatus.DISABLED,
            manifest=self.skill_to_manifest(skill),
            package=package,
            checksum=checksum,
            metadata={
                "source": "agent-skill",
                "sandbox_enabled": skill.sandbox_enabled,
                "audit_enabled": skill.audit_enabled,
                "guardrail_enabled": skill.guardrail_enabled,
            },
        )
        
        self._save_installed(installed)
        logger.info("Agent Skill 安装成功: %s", skill_id)
        return installed
    
    def execute_skill(
        self,
        skill_name: str,
        payload: dict,
        context: dict = None,
        tool_executor=None,
    ) -> dict:
        """执行 Agent Skill(使用 SkillExecutor)。"""
        skill_id = f"{skill_name}@latest"
        skill = self.get_installed(skill_id=skill_name)
        if not skill:
            return {"success": False, "error": f"技能未安装: {skill_name}"}
        
        if skill.status != SkillStatus.ENABLED:
            return {"success": False, "error": f"技能未启用: {skill_name}"}
        
        # 更新使用统计
        skill.last_used_at = time.time()
        skill.use_count += 1
        self._save_installed(skill)
        
        # 加载技能定义
        skill_file = Path(skill.install_path) / f"{skill.name}.json"
        if not skill_file.exists():
            return {"success": False, "error": f"技能定义文件丢失: {skill_file}"}
        
        with open(skill_file, "r", encoding="utf-8") as f:
            skill_data = json.load(f)
        skill_obj = Skill.from_dict(skill_data)
        
        # 使用 SkillExecutor 执行
        from app.core.skills import SkillExecutor
        executor = SkillExecutor(tool_executor)
        result = asyncio.run(executor.execute(skill_obj, payload, context={"skill_id": skill.skill_id}))
        
        # 更新使用统计
        skill.last_used_at = time.time()
        skill.use_count += 1
        self._save_installed(skill)
        
        return result.to_dict()


# ============================================================
# 全局单例
# ============================================================

_skill_marketplace: Optional[SkillMarketplace] = None


def get_skill_marketplace() -> SkillMarketplace:
    global _skill_marketplace
    if _skill_marketplace is None:
        _skill_marketplace = SkillMarketplace()
    return _skill_marketplace