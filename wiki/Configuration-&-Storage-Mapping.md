# 六、 配置系统与存储映射架构

**wiki 原版完全缺失此章节** —— 配置系统是整个程序的骨架，采用 TOML + 多文本文件 + Admin API 动态加载的三层架构，支持多存储分组、引擎入口映射、云端/本地三路路径对应。

> 📁 **配置文件位置**：项目根目录 (`config.toml`, `a_folders.txt`, `refresh_paths.txt`, `strm_engine_paths.txt`)；核心实现 `src/config.py`。

---

## 1. 配置文件架构总览

```
项目根目录/
├── config.toml              # 主配置 (TOML 格式)
├── a_folders.txt            # A区监控目录列表
├── refresh_paths.txt        # 主动刷新路径列表
├── strm_engine_paths.txt    # STRM引擎入口路径列表
└── src/
    └── .admin_token.json    # JWT Token 缓存 (运行时生成)
```

### 三层配置加载机制

| 层级 | 来源 | 加载时机 | 优先级 |
|------|------|----------|--------|
| 1. 静态配置 | `config.toml` | 启动时 | 基础配置 |
| 2. 列表配置 | `*.txt` 文件 | 启动时 | 路径列表 |
| 3. 动态配置 | OpenList Admin API | 启动后/定期刷新 | 存储映射、引擎状态 |

---

## 2. config.toml 完整结构解析

```toml
# ==========================================
# 本地路径配置
# ==========================================
[local]
# A区根目录：STRM引擎生成文件的落盘目录
a_root = "C:/测试a"

# B区根目录：程序管理的媒体库目录 (TMM/Emby/Jellyfin 挂载)
b_root = "C:/测试b"

# C区根目录：幽灵/孤儿文件隔离区
c_root = "C:/测试c"

# ==========================================
# 路径列表文件 (相对项目根目录)
# ==========================================
[paths]
# A区监控的子目录列表文件
a_folders_file = "a_folders.txt"

# 主动刷新的云端路径列表文件
refresh_paths_file = "refresh_paths.txt"

# STRM引擎入口路径列表文件
strm_engine_paths_file = "strm_engine_paths.txt"

# ==========================================
# WebDAV / OpenList 连接配置
# ==========================================
[webdav]
# OpenList 服务地址
url = "http://localhost:5244"

# 管理员用户名
username = "admin"

# 管理员密码
password = "your_password"

# TOTP 密钥 (可选，启用 2FA 时)
totp_secret = ""

# 请求超时 (秒)
timeout = 30

# ==========================================
# 主动刷新服务配置
# ==========================================
[refresh]
# 是否启用主动刷新
enabled = true

# 刷新间隔 (分钟)
interval_minutes = 240

# WebDAV 递归深度
depth = 5

# ==========================================
# 行为控制配置
# ==========================================
[behavior]
# 删除动作: MOVE (移至回收站) / DELETE (永久删除)
action = "MOVE"

# 回收站目录名 (云端)
trash_dir_name = "strm_回收站_测试"

# 幽灵保护 TTL (秒) - 防止误删回灌
ghost_protect_seconds = 10

# A->B 同步等待延迟 (秒) - 给引擎留出落地时间
a_to_b_restore_delay_seconds = 30

# 重复文件清理间隔 (秒)
duplicate_cleanup_interval = 3600

# ==========================================
# 日志配置
# ==========================================
[log]
# 日志级别: DEBUG/INFO/WARNING/ERROR
level = "INFO"

# 日志文件路径 (相对项目根目录)
file = "activity.log"

# 控制台输出
console = true
```

---

## 3. 列表配置文件详解

### a_folders.txt - A区监控目录

```text
# 每行一个相对 A区根目录 的子目录
# 支持注释 (# 开头) 和空行
# 示例:
测试a
电影
番剧
纪录片
```

**用途**：
- `AreaWatcher` 监控范围
- 血统校验时的首级目录匹配依据
- 存储映射的键名来源

### refresh_paths.txt - 主动刷新路径

```text
# 每行一个云端路径 (OpenList 路径格式)
# 示例:
/测试a
/网盘Y/电影
/网盘Z/番剧
/天翼云盘/番剧
```

**用途**：
- `RefreshService` 定时刷新目标
- 与引擎管辖路径交叉校验 (决定 full/readonly 模式)

### strm_engine_paths.txt - STRM引擎入口

```text
# 每行一个引擎配置的云端路径
# 必须与 OpenList 后台 STRM 存储的 "挂载路径" 一致
# 示例:
/测试a
/天翼云盘/番剧
/阿里云盘/电影
```

**用途**：
- 定义引擎管辖范围白名单
- 血统校验的归属判定依据
- 存储映射的引擎入口键

---

## 4. 存储映射架构 - 核心数据结构

### StrmStorageMapping (config.py)

```python
@dataclass
class StrmStorageMapping:
    """STRM 存储三路映射"""
    engine_entry_path: str      # 引擎入口路径 (如: /测试a)
    cloud_path: str             # 云端完整路径 (如: /天翼云盘/番剧)
    local_path: str             # 本地 A区 完整路径 (如: C:/测试a/测试a)
    driver: str                 # 存储驱动类型 (Local/S3/Alist...)
    enable_sign: bool           # 是否启用签名
    work_mode: str              # 工作模式: work/update/readonly
    status: str                 # 存储状态: work/maintenance/error
```

### 三路映射关系图解

```
OpenList 后台配置                    程序内部映射
┌─────────────────────┐            ┌─────────────────────┐
│ 存储名称: 天翼云盘    │            │ engine_entry: /测试a │
│ 驱动: Local          │     ──▶    │ cloud_path: /天翼云盘/│
│ 挂载路径: /测试a      │            │                    │
│ SaveStrmLocalPath:   │            │ local_path: C:/测试a/│
│   C:/测试a/测试a      │            │   测试a             │
│ 工作模式: update     │            │ driver: Local       │
│ 状态: work           │            │ enable_sign: true   │
└─────────────────────┘            │ work_mode: update   │
                                   │ status: work        │
                                   └─────────────────────┘
```

### 映射加载流程 (`AppConfig.from_file`)

```python
@classmethod
def from_file(cls, config_path: str) -> "AppConfig":
    # 1. 解析 TOML
    with open(config_path, "rb") as f:
        toml_data = tomllib.load(f)
    
    # 2. 读取列表文件 (相对项目根目录)
    project_root = Path(config_path).parent
    a_folders = read_line_list(project_root / toml_data["paths"]["a_folders_file"])
    refresh_paths = read_line_list(project_root / toml_data["paths"]["refresh_paths_file"])
    engine_paths = read_line_list(project_root / toml_data["paths"]["strm_engine_paths_file"])
    
    # 3. 创建基础配置对象
    config = cls(...)
    
    # 4. 动态加载存储映射 (Admin API)
    config.strm_storage_map = cls._load_storage_mappings(
        config.webdav, 
        engine_paths,
        a_folders,
        config.local.a_root
    )
    
    return config

@classmethod
def _load_storage_mappings(cls, webdav_config, engine_paths, a_folders, a_root):
    """从 Admin API 加载存储映射"""
    admin = OpenListAdminClient(webdav_config)
    storages = admin.list_storages()
    
    mapping = {}
    for storage in storages:
        if storage.driver != "Local" or not storage.enable_sign:
            continue  # 仅处理启用签名的 Local 存储
        
        mount_path = storage.mount_path  # 如: /测试a
        if mount_path not in engine_paths:
            continue  # 不在引擎入口列表中，跳过
        
        # 找到对应的 A区本地路径
        # SaveStrmLocalPath 通常是绝对路径，需提取相对 a_root 部分
        save_path = storage.save_strm_local_path
        rel_path = Path(save_path).relative_to(a_root)
        local_full = Path(a_root) / rel_path
        
        mapping[mount_path] = StrmStorageMapping(
            engine_entry_path=mount_path,
            cloud_path=storage.mount_path,  # 云端路径
            local_path=str(local_full),
            driver=storage.driver,
            enable_sign=storage.enable_sign,
            work_mode=storage.work_mode,
            status=storage.status
        )
    
    return mapping
```

---

## 5. 多存储分组支持

程序支持**多个 STRM 存储同时工作**，每个存储独立管理：

### 多存储场景示例

```text
存储 1: 天翼云盘
  - 引擎入口: /测试a
  - 云端路径: /天翼云盘/番剧
  - 本地路径: C:/测试a/测试a
  - 模式: update

存储 2: 阿里云盘
  - 引擎入口: /电影库
  - 云端路径: /阿里云盘/电影
  - 本地路径: C:/测试a/电影库
  - 模式: work

存储 3: 115网盘
  - 引擎入口: /纪录片
  - 云端路径: /115网盘/纪录片
  - 本地路径: C:/测试a/纪录片
  - 模式: readonly
```

### 运行时隔离

- 每个存储独立的 A/B/C 区子目录
- 独立的血统校验范围
- 独立的主动刷新任务
- 独立的幽灵保护空间

---

## 6. 路径解析与映射工具函数

### A区 → B区 映射 (`_map_a_to_b`)

```python
def _map_a_to_b(self, a_path: str) -> str:
    """将 A区路径映射为 B区路径"""
    # 1. 找到所属引擎入口
    rel_a = Path(a_path).relative_to(self.config.local.a_root)
    engine_entry = rel_a.parts[0]  # 首级目录
    
    # 2. 获取存储映射
    mapping = self.strm_storage_map.get(engine_entry)
    if not mapping:
        # 兜底：直接替换根目录
        return str(Path(self.config.local.b_root) / rel_a)
    
    # 3. 计算相对映射路径
    rel_to_mapping = Path(a_path).relative_to(mapping.local_path)
    return str(Path(self.config.local.b_root) / engine_entry / rel_to_mapping)
```

### B区 → 云端路径解析 (`_parse_strm_to_webdav`)

```python
def _parse_strm_to_webdav(self, b_path: str) -> str | None:
    """解析 B区 STRM 文件，提取云端 WebDAV 路径"""
    try:
        with open(b_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        
        # STRM 格式: http://host:port/d/xxx/文件名?sign=xxx
        # 或: /mnt/xxx/文件名 (本地路径模式)
        
        # 提取签名 URL 中的路径部分
        if "?sign=" in content:
            # 签名模式：从 URL path 提取
            parsed = urlparse(content)
            path = parsed.path
            # /d/xxx/文件名 -> 去掉 /d/xxx 前缀
            if path.startswith("/d/"):
                parts = path.split("/")
                if len(parts) >= 4:
                    return "/" + "/".join(parts[3:])
        elif content.startswith("/"):
            # 本地路径模式
            return content
        
        return None
    except Exception:
        return None
```

### 云端路径 → 本地 B区路径 (`_map_webdav_to_b`)

```python
def _map_webdav_to_b(self, webdav_path: str) -> str | None:
    """将云端路径映射为 B区本地路径"""
    # 1. 找到所属存储映射
    for mapping in self.strm_storage_map.values():
        if webdav_path.startswith(mapping.cloud_path.rstrip("/") + "/"):
            # 2. 计算相对路径
            rel = webdav_path[len(mapping.cloud_path):].lstrip("/")
            # 3. 组合 B区路径
            return str(Path(self.config.local.b_root) / mapping.engine_entry_path / rel)
    
    return None
```

---

## 7. 配置热重载与运行时更新

### 存储映射定期刷新

```python
def refresh_storage_mappings(self):
    """定期从 Admin API 刷新存储映射"""
    new_map = AppConfig._load_storage_mappings(
        self.config.webdav,
        self.config.paths.strm_engine_paths,  # 已加载的列表
        self.config.paths.a_folders,
        self.config.local.a_root
    )
    
    # 检测变更
    added = set(new_map.keys()) - set(self.strm_storage_map.keys())
    removed = set(self.strm_storage_map.keys()) - set(new_map.keys())
    changed = {
        k for k in set(new_map.keys()) & set(self.strm_storage_map.keys())
        if new_map[k] != self.strm_storage_map[k]
    }
    
    if added or removed or changed:
        log.info(f"[存储映射更新] 新增: {added}, 移除: {removed}, 变更: {changed}")
        self.strm_storage_map = new_map
        # 触发保护根目录快照更新
        self.db.snapshot_protected_roots(list(new_map.values()))
```

---

## 8. JWT Token 缓存机制

### 缓存文件: `src/.admin_token.json`

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_at": 1700000000,
  "username": "admin"
}
```

### Token 管理 (`OpenListAdminClient`)

```python
class OpenListAdminClient:
    TOKEN_FILE = Path(__file__).parent / ".admin_token.json"
    TOKEN_TTL = 3600  # 1 小时
    
    def _load_cached_token(self) -> str | None:
        """加载缓存的 Token"""
        if not self.TOKEN_FILE.exists():
            return None
        
        try:
            with open(self.TOKEN_FILE) as f:
                data = json.load(f)
            
            if data.get("expires_at", 0) > time.time() + 60:  # 留 1 分钟缓冲
                return data["token"]
        except Exception:
            pass
        return None
    
    def _save_token(self, token: str, expires_in: int = 3600):
        """保存 Token 到缓存"""
        data = {
            "token": token,
            "expires_at": time.time() + expires_in - 60,
            "username": self.username
        }
        with open(self.TOKEN_FILE, "w") as f:
            json.dump(data, f)
    
    def get_token(self) -> str:
        """获取有效 Token (自动刷新)"""
        token = self._load_cached_token()
        if token:
            return token
        
        # 登录获取新 Token
        token = self._login()
        self._save_token(token)
        return token
```

---

## 9. 配置验证与启动检查

```python
def validate_config(self) -> list[str]:
    """验证配置完整性，返回错误列表"""
    errors = []
    
    # 1. 必需目录存在性
    for name, path in [("A区", self.local.a_root), ("B区", self.local.b_root), ("C区", self.local.c_root)]:
        if not Path(path).exists():
            errors.append(f"{name}根目录不存在: {path}")
    
    # 2. 列表文件存在性
    for name, file in [("A区列表", self.paths.a_folders_file), 
                       ("刷新列表", self.paths.refresh_paths_file),
                       ("引擎列表", self.paths.strm_engine_paths_file)]:
        if not (self.project_root / file).exists():
            errors.append(f"{name}文件不存在: {file}")
    
    # 3. WebDAV 连通性
    try:
        client = WebDAVClient(self.webdav)
        client.list("/")
    except Exception as e:
        errors.append(f"WebDAV 连接失败: {e}")
    
    # 4. 存储映射非空
    if not self.strm_storage_map:
        errors.append("未找到有效的 STRM 存储映射，请检查 OpenList 配置")
    
    return errors
```

---

## 10. 关键代码位置

| 功能 | 文件 | 函数/类 |
|------|------|---------|
| 配置加载入口 | `config.py` | `AppConfig.from_file()` |
| TOML 解析 | `config.py` | `AppConfig.__init__()` |
| 列表文件读取 | `config.py` | `read_line_list()` |
| 存储映射加载 | `config.py` | `_load_storage_mappings()` |
| 存储映射数据类 | `config.py` | `StrmStorageMapping` |
| A→B 路径映射 | `app_service.py` | `_map_a_to_b()` |
| STRM→WebDAV 解析 | `app_service.py` | `_parse_strm_to_webdav()` |
| WebDAV→B 区映射 | `app_service.py` | `_map_webdav_to_b()` |
| Admin API 客户端 | `webdav_client.py` | `OpenListAdminClient` |
| Token 缓存 | `webdav_client.py` | `_load_cached_token()`, `_save_token()` |
| 配置验证 | `config.py` | `validate_config()` |
| 映射热重载 | `app_service.py` | `refresh_storage_mappings()` |