# 七、 数据库结构参考

这页按当前代码里的实际 schema 记录 `bridge.db` 的结构。程序使用 SQLite + WAL 模式，所有写操作都经过线程安全连接上下文。

> 📁 **实现位置**：`src/database.py`

---

## 1. 连接与事务策略

`Database.connection()` 在进入上下文时会做三件事：

1. 确保数据库文件所在目录存在。
2. 打开 SQLite 连接并设置 `PRAGMA journal_mode=WAL`。
3. 通过创建和删除临时表 `_write_test` 验证连接真的具备写权限。

如果遇到只读错误，会自动重试几次再抛出异常。这个设计是为了在 Windows 环境里尽量躲开文件锁、权限波动和外部进程干扰。

---

## 2. 实际表结构

当前代码里初始化的表一共 9 张，不是旧 wiki 里写的那一版 11 张。

### `a_strm_files`
A 区事实索引表。

| 字段 | 说明 |
|------|------|
| `local_path` | A 区本地路径，主键 |
| `webdav_path` | 解析后的云端路径 |
| `parent_webdav_path` | 父级云端目录 |
| `updated_at` | 更新时间戳 |

### `b_strm_files`
B 区实例表。

| 字段 | 说明 |
|------|------|
| `local_path` | B 区本地路径，主键 |
| `webdav_path` | 解析后的云端路径 |
| `parent_webdav_path` | 父级云端目录 |
| `source_a_path` | 对应的 A 区源路径 |
| `fingerprint` | 指纹哈希，后加字段 |
| `status` | 状态，默认 `valid` |
| `updated_at` | 更新时间戳 |

### `strm_identity`
全局身份表，用于用指纹把 A 区、云端和 B 区串起来。

| 字段 | 说明 |
|------|------|
| `fingerprint` | 主键 |
| `webdav_path` | 云端路径 |
| `source_a_path` | A 区源路径 |
| `current_b_path` | 当前有效 B 区路径 |
| `updated_at` | 更新时间戳 |

### `c_ghost_files`
C 区幽灵收容表。

| 字段 | 说明 |
|------|------|
| `local_path` | C 区本地路径，主键 |
| `webdav_path` | 云端路径 |
| `original_b_path` | 原 B 区路径 |
| `ghost_root` | 幽灵根目录 |
| `moved_at` | 迁移时间 |

### `ghost_protection`
幽灵保护表，用来给刚删除的路径留一个短 TTL 的保护窗。

| 字段 | 说明 |
|------|------|
| `webdav_path` | 主键 |
| `expire_time` | 过期时间 |
| `reason` | 保护原因 |

### `known_folders`
记录已知文件夹，供空目录清理和目录边界判断使用。

| 字段 | 说明 |
|------|------|
| `folder_path` | 文件夹路径，主键 |
| `source` | 来源标记 |
| `updated_at` | 更新时间戳 |

### `protected_roots`
受保护的媒体根目录表。

| 字段 | 说明 |
|------|------|
| `root_path` | 主键 |
| `trash_path` | 对应回收站路径 |
| `active` | 是否启用 |
| `updated_at` | 更新时间戳 |

### `protected_roots_snapshot`
受保护根目录的启动快照。

| 字段 | 说明 |
|------|------|
| `root_path` | 主键 |
| `trash_path` | 回收站路径 |
| `updated_at` | 快照时间 |

### `sync_control`
同步控制表。

| 字段 | 说明 |
|------|------|
| `control_key` | 主键 |
| `control_value` | 控制值 |
| `updated_at` | 更新时间戳 |

### `strm_media_boundary`
媒体边界映射表，血统校验和 Season 归档都用它。

| 字段 | 说明 |
|------|------|
| `fingerprint` | 主键 |
| `source_media_name` | 源媒体名 |
| `current_media_name` | 当前媒体名 |
| `engine_entry_path` | 引擎入口路径 |
| `updated_at` | 更新时间戳 |

---

## 3. 索引设计

代码里实际创建的索引如下：

```sql
CREATE INDEX idx_a_strm_webdav_path ON a_strm_files(webdav_path);
CREATE INDEX idx_b_strm_webdav_path ON b_strm_files(webdav_path);
CREATE INDEX idx_b_strm_fingerprint ON b_strm_files(fingerprint);
CREATE INDEX idx_b_strm_status ON b_strm_files(status);
CREATE INDEX idx_identity_webdav_path ON strm_identity(webdav_path);
CREATE INDEX idx_identity_current_b_path ON strm_identity(current_b_path);
CREATE INDEX idx_boundary_source_name ON strm_media_boundary(source_media_name);
CREATE INDEX idx_boundary_current_name ON strm_media_boundary(current_media_name);
```

补充说明：代码没有把 `a_strm_files.fingerprint`、`ghost_protection.expire_time`、`protected_roots_snapshot` 这些索引写进当前版本的初始化语句，所以旧 wiki 里那部分不算现状。

---

## 4. 关键 CRUD 形态

### A 区写入
`upsert_a(local_path, webdav_path, parent_webdav_path)` 会覆盖写入 A 区事实索引。

### B 区写入
`upsert_b(local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status)` 会写入 B 区记录，并支持指纹和状态。

### C 区写入
`upsert_c(local_path, webdav_path, original_b_path, ghost_root)` 用来登记幽灵迁移。

### 删除
代码提供了按本地路径删除 A/B/C 记录的接口：
`delete_a_by_local()`、`delete_b_by_local()`、`delete_c_by_local()`。

### 查询
现有接口包括：
`get_a_by_local()`、`get_b_by_local()`、`get_a_by_webdav()`、`get_b_by_webdav()`、`get_all_a()`、`get_all_b()`、`get_all_c()`。

### 已知文件夹
`save_known_folder()` 会忽略空路径和根路径，`remove_known_folder_prefix()` 可以清理整个前缀树。

### 幽灵保护
`set_ghost_protection()` 和 `remove_expired_ghost_protection()` 负责 TTL 生命周期，`is_ghost_protected()` 用来做防误删判断。

### 媒体边界
`upsert_media_boundary()`、`get_media_boundary_by_fingerprint()`、`get_media_boundary_by_source_name()`、`get_media_boundary_by_current_name()` 支撑血统校验和改名追踪。

---

## 5. 启动时的初始化顺序

数据库初始化发生在 `Database.__init__()` 里，顺序很明确：

1. 创建基础表。
2. 对 `b_strm_files` 补 `fingerprint` 和 `status` 字段。
3. 创建索引。
4. 建立 `strm_media_boundary`。
5. 提交事务并记录日志。

`init_db()` 只是兼容旧调用的空壳方法，真正的建表动作已经在构造时完成。

---

## 6. 代码与表的对应关系

| 功能 | 表 |
|------|----|
| A 区事实索引 | `a_strm_files` |
| B 区有效/重复状态 | `b_strm_files` |
| 指纹身份总表 | `strm_identity` |
| C 区幽灵收容 | `c_ghost_files` |
| 幽灵 TTL | `ghost_protection` |
| 已知文件夹 | `known_folders` |
| 保护根目录 | `protected_roots` |
| 保护根目录快照 | `protected_roots_snapshot` |
| 同步控制 | `sync_control` |
| 媒体边界映射 | `strm_media_boundary` |

---

## 7. 关键代码位置

| 功能 | 文件 | 入口 |
|------|------|------|
| 数据库连接 | `database.py` | `connection()` |
| 初始化 | `database.py` | `__init__()` |
| A 区写入 | `database.py` | `upsert_a()` |
| B 区写入 | `database.py` | `upsert_b()` |
| C 区写入 | `database.py` | `upsert_c()` |
| 幽灵保护 | `database.py` | `set_ghost_protection()` / `is_ghost_protected()` |
| 媒体边界 | `database.py` | `upsert_media_boundary()` 系列 |
| 已知文件夹 | `database.py` | `save_known_folder()` |
| 只读兼容入口 | `database.py` | `init_db()` |