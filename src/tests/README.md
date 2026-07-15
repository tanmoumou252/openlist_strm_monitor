# 测试脚本说明

本目录包含 `openlist_strm_bridge` 项目的所有测试文件。

## 测试分类

### 单元测试

针对单个函数或模块的测试：

- `test_fts5_escape_and_tmdb_search.py` - FTS5 查询转义函数和 TMDB 搜索路由测试
- `test_database.py` - 数据库操作测试
- `test_config.py` - 配置加载和验证测试
- `test_media_renamer.py` - 媒体重命名逻辑测试
- `test_subtitle_handler.py` - 字幕处理测试

### 集成测试

测试多个模块之间的交互：

- `test_webui_routes.py` - WebUI API 路由测试
- `test_tmdb_integration.py` - TMDB API 集成测试
- `test_openlist_integration.py` - OpenList API 集成测试

### 端到端测试

模拟完整用户流程的测试：

- `test_onboarding_e2e.py` - 新手引导流程端到端测试
- `test_e2e_full_flow.py` - 完整业务流程端到端测试（需求 24-28）

## 运行测试

### 运行所有测试

```bash
cd src
python -m pytest tests/ -v
```

### 运行特定测试文件

```bash
cd src
python -m pytest tests/test_e2e_full_flow.py -v
```

### 运行特定测试类

```bash
cd src
python -m pytest tests/test_fts5_escape_and_tmdb_search.py::TestEscapeFts5Query -v
```

### 运行特定测试方法

```bash
cd src
python -m pytest tests/test_fts5_escape_and_tmdb_search.py::TestEscapeFts5Query::test_escape_star -v
```

### 运行测试并生成覆盖率报告

```bash
cd src
python -m pytest tests/ --cov=. --cov-report=html
```

## 测试依赖

运行测试需要以下依赖：

```bash
pip install pytest pytest-cov
```

## 测试环境

- Python 3.11+
- SQLite（内置）
- 无需外部服务（所有外部依赖已 mock）

## 测试文件说明

### test_e2e_full_flow.py

**覆盖场景：**

1. **成功路径**
   - 完整新用户流程：登录 → 配置 TMDB → 配置 OpenList → 查看 A/B 区 → 验证配置状态
   - 引导步骤完成流程

2. **失败场景**
   - 不可达 OpenList 地址仍可保存配置
   - OpenList 未配置时预检失败
   - 非法 scope 被拒绝
   - 包含空 engine 的 strm_engines 被拒绝

**运行方式：**

```bash
cd src
python -m pytest tests/test_e2e_full_flow.py -v
```

**预期输出：** 6 passed

### test_fts5_escape_and_tmdb_search.py

**覆盖场景：**

1. **FTS5 查询清理函数测试**
   - 特殊字符移除（* + " ^ ~ :）
   - 括号替换为空格
   - 连字符处理（词中间保留，首尾移除）
   - 反斜杠替换为空格
   - 中文混合特殊字符
   - 多个空白合并

2. **TMDB 搜索路由测试**
   - 缺少 query 参数返回 400
   - 空 query 返回 400
   - 正常搜索返回电影和电视剧
   - 搜索结果限制为前 10 条
   - 无匹配结果返回空列表
   - URL 解码和空白去除
   - TMDB 客户端异常返回 500

**运行方式：**

```bash
cd src
python -m pytest tests/test_fts5_escape_and_tmdb_search.py -v
```

**预期输出：** 18 passed（FTS5 转义）+ 9 passed（TMDB 搜索）= 27 passed

### test_onboarding_e2e.py

**覆盖场景：**

1. **引导流程**
   - 初始状态所有步骤未完成
   - 标记 view_ab 步骤完成
   - 标记 tmdb_refresh 步骤完成
   - 标记 tmdb_match 步骤完成
   - 完成所有步骤

2. **启动预检**
   - OpenList 未配置时预检失败
   - OpenList 已配置但不可达时预检失败
   - 所有配置完成时预检通过

3. **完整旅程**
   - 从登录到完成引导的完整流程

**运行方式：**

```bash
cd src
python -m pytest tests/test_onboarding_e2e.py -v
```

## 测试策略

### Mock 策略

所有测试使用 mock 避免真实网络调用和外部依赖：

- `WebUIServer` 使用真实实例，但 mock 数据库和配置
- `Database` 使用 mock，避免真实 SQLite 操作
- `AppConfig` 使用 mock，提供最小化配置
- TMDB 客户端使用 mock，避免真实 API 调用
- OpenList 客户端使用 mock，避免真实 WebDAV 调用

### 测试隔离

每个测试使用独立的 `tmp_path`，确保：

- 数据库文件隔离
- 配置文件隔离
- 日志文件隔离

### 测试数据

测试使用最小化数据集：

- 空数据库（0 条记录）
- 最小化配置（仅必要字段）
- 固定测试密码（`test_password_123`）

## 常见问题

### Q: 测试失败提示 "ModuleNotFoundError"

**A:** 确保在 `src/` 目录下运行测试：

```bash
cd src
python -m pytest tests/
```

### Q: 测试失败提示 "Port already in use"

**A:** 测试使用随机端口，通常不会冲突。如遇到，等待几秒后重试。

### Q: 测试运行缓慢

**A:** 端到端测试需要启动真实 WebUIServer，可能需要 10-20 秒。单元测试通常在 1 秒内完成。

### Q: 如何添加新测试

**A:** 

1. 在 `tests/` 目录下创建新文件 `test_xxx.py`
2. 使用 `pytest` 标准语法编写测试
3. 如需 WebUIServer，参考 `test_e2e_full_flow.py` 的 fixture
4. 运行测试验证

## 测试覆盖率

当前测试覆盖率：

- 核心模块：~80%
- WebUI 路由：~70%
- 端到端流程：~60%

目标覆盖率：80%+

## 持续集成

测试已集成到 CI/CD 流程：

- 每次提交自动运行测试
- 测试失败阻止合并
- 覆盖率报告自动生成

## 相关文档

- [项目开发计划](../.kilo/plans/1784068382552-requirements-24-28-implementation.md)
- [API 文档](../docs/)
- [项目说明](../README.md)
