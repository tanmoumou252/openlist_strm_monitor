# 一、 架构设计与 A/B/C 三区模型

为了实现安全的文件操作与状态隔离，本系统提出了精密的 **A/B/C 三区划分体系** 与 **SQLite 状态持久化方案**。

> 📁 **项目目录结构**：核心代码位于 `src/` 目录，配置文件（`config.toml`、`*.txt`）位于项目根目录。启动脚本在根目录，分别为 `嵌入式启动.bat` 和 `环境变量启动.bat`。

---

## 1. 三区模型定义

### A 区（生肉区 / STRM 事实来源）
- **定义**：由 OpenList STRM 引擎更新模式收到 API 请求后直接输出。
- **特点**：不会被 OpenList 删除。
- **程序行为**：程序对 A 区执行对照映射，映射关系终结时，进行删除。它是 B 区文件的事实来源与指纹校验的基准。

### B 区（熟肉区 / 媒体库消费区）
- **定义**：Emby、Jellyfin、Plex 等刮削和播放的物理目录。
- **特点**：用户可以对其进行任意重命名、套娃式分类和删除整理。
- **程序行为**：程序对该区执行 7x24 小时全方位监控，过滤重复（去重）、修复损坏、并将用户的删除操作包装并转发为云端的 WebDAV 物理删除动作。

### C 区（幽灵收容所）
- **定义**：当某个引擎挂载点被下线，或网盘路径大改版时，相关的存量本地 STRM 不会被程序当场销毁，而是被“幽灵迁移”到 C 区。
- **特点**：程序不主动对其进行增减，作为失效路径的文件收容所，给强迫症用户提供人工核对的空间。

---

## 2. 存储映射系统 (StrmStorageMapping)

**这是 wiki 原版完全缺失的核心架构组件**

系统通过 Admin API 动态构建 `strm_storage_map`，实现三路映射：

```python
@dataclass
class StrmStorageMapping:
    engine_entry_paths: list[str]   # 引擎入口路径 (如 /strm, /测试a)
    cloud_path: str                 # 云端媒体根路径 (如 /天翼云盘/番剧)
    local_path: str                 # 本地 A 区输出路径 (如 C:/OpenList/strm/天翼云盘/番剧)
```

**映射构建流程**：
1. 读取 `strm_engine_paths.txt` 获取引擎入口路径列表
2. 调用 Admin API `/api/admin/setting/list` 获取所有存储配置
3. 筛选 `driver` 为 `Local` 且 `enable_sign` 为 true 的存储
4. 将存储的 `mount_path` (挂载点) 与 `SaveStrmLocalPath` (本地保存路径) 关联
5. 按引擎入口路径分组，构建 `engine_entry_paths -> cloud_path -> local_path` 映射

**用途**：
- A 区文件路径 → 反向解析云端真实路径
- 血统校验时的边界判定依据
- 主动刷新时的路径交叉校验

---

## 3. 数据库设计完整说明 (SQLite Schema - 11 张表)

程序依赖 `bridge.db` 作为核心状态机，启用 **WAL 模式** + **线程安全连接池**。

### 核心业务表

#### `a_strm_files` (A 区事实索引表)
| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | A 区本地物理路径 |
| `webdav_path` | TEXT NOT NULL | 解析后的云端真实路径 |
| `parent_webdav_path` | TEXT NOT NULL | 父级云端目录 |
| `fingerprint` | TEXT | 指纹哈希 |
| `updated_at` | REAL | 更新时间戳 |

#### `b_strm_files` (B 区实例表)
| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | B 区本地物理路径 |
| `webdav_path` | TEXT NOT NULL | 云端真实路径 |
| `parent_webdav_path` | TEXT NOT NULL | 父级云端目录 |
| `source_a_path` | TEXT | 对应的 A 区源路径 |
| `fingerprint` | TEXT | 指纹哈希 |
| `status` | TEXT | `valid`/`duplicate`/`quarantined` |
| `updated_at` | REAL | 更新时间戳 |

#### `strm_identity` (身份指纹全局主表)
用于在发生 B 区改名、重组或云端删除时，提供指纹级逆向映射。
| 字段 | 类型 | 说明 |
|------|------|------|
| `fingerprint` | TEXT PRIMARY KEY | 指纹哈希 |
| `webdav_path` | TEXT | 云端路径 |
| `source_a_path` | TEXT | A 区源路径 |
| `current_b_path` | TEXT | 当前服役的有效 B 区路径 |
| `updated_at` | REAL | 更新时间戳 |

#### `strm_media_boundary` (媒体边界映射表) - **wiki 原版缺失**
记录源媒体名与当前媒体名的映射，用于血统校验的边界判定。
| 字段 | 类型 | 说明 |
|------|------|------|
| `fingerprint` | TEXT PRIMARY KEY | 指纹哈希 |
| `source_media_name` | TEXT | 源媒体名 (A 区/云端原始名) |
| `current_media_name` | TEXT | 当前媒体名 (B 区用户改名后) |
| `engine_entry_path` | TEXT | 引擎入口路径 |
| `updated_at` | REAL | 更新时间戳 |

#### `subtitle_files` (字幕文件表) - **wiki 原版完全缺失**
| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | 字幕本地路径 |
| `associated_strm_path` | TEXT | 关联的 STRM 文件路径 |
| `language_code` | TEXT | 语言代码 (zho/eng/jpn 等) |
| `language_label` | TEXT | 中文标签 (简体/繁体/双语) |
| `is_forced` | INTEGER | 是否强制字幕 |
| `season` | INTEGER | 季号 |
| `episode` | INTEGER | 集号 |
| `updated_at` | REAL | 更新时间戳 |

### 保护与控制表

#### `ghost_protection` (幽灵保护表)
防止 B 区删除后，A 区因同步延迟又短暂生成同一 STRM 导致回灌。
| 字段 | 类型 | 说明 |
|------|------|------|
| `fingerprint` | TEXT PRIMARY KEY | 指纹哈希 |
| `expire_at` | REAL | 过期时间戳 |

#### `protected_roots` (保护根目录表)
记录需要保护的媒体根目录（防止误删整个剧集）。
| 字段 | 类型 | 说明 |
|------|------|------|
| `root_path` | TEXT PRIMARY KEY | 云端媒体根路径 |
| `engine_entry_path` | TEXT | 引擎入口路径 |
| `created_at` | REAL | 创建时间 |

#### `protected_roots_snapshot` (保护根目录快照表) - **wiki 原版缺失**
启动时快照保护根目录，用于对比检测大类路径是否从引擎移除。
| 字段 | 类型 | 说明 |
|------|------|------|
| `root_path` | TEXT PRIMARY KEY | 云端媒体根路径 |
| `engine_entry_path` | TEXT | 引擎入口路径 |
| `snapshot_at` | REAL | 快照时间 |

#### `sync_control` (同步控制表)
| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT PRIMARY KEY | 控制键 |
| `value` | TEXT | 控制值 |
| `updated_at` | REAL | 更新时间 |

#### `c_ghost_files` (C 区幽灵文件表)
| 字段 | 类型 | 说明 |
|------|------|------|
| `local_path` | TEXT PRIMARY KEY | C 区本地路径 |
| `original_b_path` | TEXT | 原 B 区路径 |
| `webdav_path` | TEXT | 云端路径 |
| `fingerprint` | TEXT | 指纹 |
| `migrated_at` | REAL | 迁移时间 |

#### `known_folders` (已知文件夹表)
记录 B 区已知的媒体文件夹，用于空目录清理判断。

---

### 关键索引设计
```sql
-- 指纹查找加速
CREATE INDEX idx_b_strm_fingerprint ON b_strm_files(fingerprint);
CREATE INDEX idx_a_strm_fingerprint ON a_strm_files(fingerprint);

-- 父目录查找加速
CREATE INDEX idx_b_strm_parent ON b_strm_files(parent_webdav_path);
CREATE INDEX idx_a_strm_parent ON a_strm_files(parent_webdav_path);

-- 状态过滤
CREATE INDEX idx_b_strm_status ON b_strm_files(status);

-- 边界映射查找
CREATE INDEX idx_boundary_source ON strm_media_boundary(source_media_name, engine_entry_path);
CREATE INDEX idx_boundary_current ON strm_media_boundary(current_media_name, engine_entry_path);

-- 幽灵保护过期清理
CREATE INDEX idx_ghost_expire ON ghost_protection(expire_at);
```

---

## 4. 系统启动自同步生命周期 (8步法)

为了防止时序冲突导致数据库锁死或崩溃，程序在启动时会严格按照以下顺序初始化：

1. **数据库构建与核验**：强制在最前方执行 `self.db.init_db()`，确保所有数据表及核心索引 100% 存在。
2. **B 区物理磁盘逆向自同步（血统大清洗）**：
   - 遍历扫描现有 B 区的所有物理 STRM 文件并计算指纹。
   - 检查磁盘有但数据库没有的文件：血统校验通过后，补充入库。
   - 检查数据库有但磁盘上连“同义改名文件”都没有的失效死记录：直接执行**“B 区自同步清理”**，将其从数据库中抹除。
3. **API 登录握手与令牌本地缓存** (`src/.admin_token.json`)。
4. **拉取云端配置**：构建物理-云端映射表 `strm_storage_map`。
5. **A 区增量扫描并建立 A 区事实索引**。
6. **A -> B 增量同步**：只对 B 区不存在指纹的文件执行复制。同时利用评分机制，避免 A 区的“烂名字原文件”覆盖用户精心修改的名字。
7. **B 区冗余死链、废弃隔离文件清理**。
8. **智能空目录回收**：遍历 B 区，只删除完全不包含任何物理文件的完完全全的空文件夹（内含海报、.nfo 文件的目录会被安全保留）。

---

## 5. 核心模块依赖关系

```
main.py (入口)
├── config.py (配置加载 + API动态映射)
├── database.py (SQLite WAL + 线程安全)
├── webdav_client.py (WebDAV + Admin API + JWT/TOTP)
├── utils.py (指纹/路径/文件操作)
├── area_watchers.py (A/B/C三区事件处理)
├── refresh_service.py (主动刷新独立线程)
├── media_renamer.py (媒体/字幕智能重命名)
└── logger_setup.py (日志轮转)
```

---

## 6. 运行时关键数据结构

### `AppService` 核心状态
```python
class AppService:
    # 配置与映射
    config: AppConfig
    strm_storage_map: dict[str, StrmStorageMapping]  # engine_entry -> mapping
    
    # 数据库
    db: Database
    
    # 运行时状态
    _a_fingerprint_index: dict[str, str]      # fingerprint -> a_local_path
    _b_fingerprint_index: dict[str, str]      # fingerprint -> b_local_path (valid)
    _protected_roots: set[str]                # 保护根目录集合
    _protected_roots_snapshot: set[str]       # 启动快照
    
    # 幽灵保护
    _ghost_protect_seconds: int
    
    # 监控器
    _a_observer, _b_observer, _c_observer
    
    # 刷新服务
    _refresh_service: RefreshService
```

---

## 7. 关键设计原则

| 原则 | 实现方式 |
|------|----------|
| **指纹优先** | 所有身份识别基于 SHA256 指纹，不依赖文件名/路径 |
| **血统校验** | A 区 → 存储映射 → 云端路径 → 边界映射 → B 区，全链路验证 |
| **熔断保护** | 云盘异常时立即停止所有清理操作，保护 B 区数据安全 |
| **幽灵保护** | 删除后 TTL 窗口内禁止同指纹回灌 |
| **只读刷新** | 非引擎监控路径仅刷新索引，不清理 B 区 |
| **双阶段清理** | 预检查刷新 → 确认延迟 → 最终清理，防误删 |
| **字幕同步** | 电影/番剧双模式，语言标准化命名，Season 目录归档 |