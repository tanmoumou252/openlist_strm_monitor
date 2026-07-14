# 测试脚本说明

本目录包含 `openlist_strm_bridge` 项目的所有测试脚本。

## 测试框架

- **框架**: pytest
- **Python 版本**: 3.11+
- **运行方式**: `pytest src/tests/`

## 测试分类

### 核心功能测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_app_service_core.py` | 核心同步引擎 | `app_service_core.py` |
| `test_sync_service.py` | 同步服务 | `sync_service.py` |
| `test_refresh_service.py` | 刷新服务 | `refresh_service.py` |
| `test_refresh_media.py` | 媒体刷新 | `routes.py` |
| `test_media_renamer.py` | 媒体重命名 | `media_renamer.py` |
| `test_subtitle_handler.py` | 字幕处理 | `subtitle_handler.py` |

### 数据库测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_boundary_conditions.py` | 边界条件 | `database.py` |
| `test_fts5_search.py` | FTS5 中文分词搜索（含降级路径、孤儿清理、删除同步） | `database.py` |
| `test_fts5_escape_and_tmdb_search.py` | FTS5 特殊字符转义 + TMDB 综合搜索 API | `routes.py` |
| `test_watchlist_match_state.py` | 待看列表匹配状态 | `tmdb_watchlist_db.py` |

### WebUI 测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_webui_http.py` | WebUI HTTP 请求（含新手引导 API） | `webui/routes.py`, `webui/server.py` |
| `test_webui_standalone.py` | WebUI 独立运行 | `webui/server.py` |
| `test_password_security.py` | 密码安全 | `webui/server.py` |
| `test_integration_security.py` | 集成安全 | 多模块 |

### OpenList 集成测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_openlist_admin_api.py` | OpenList Admin API | `webdav_client.py` |
| `test_openlist_hotreload.py` | OpenList 热重载 | `config.py` |
| `test_webdav_client.py` | WebDAV 客户端 | `webdav_client.py` |

### TMDB 集成测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_tmdb_api.py` | TMDB API | `tmdb_client.py` |
| `test_tmdb_client.py` | TMDB 客户端 | `tmdb_client.py` |
| `test_watchlist_match.py` | 待看列表匹配 | `watchlist_match.py` |

### 工具函数测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_utils.py` | 工具函数 | `utils/*.py` |
| `test_strm_engines_validation.py` | STRM 引擎验证 | `config.py` |
| `test_logging_system.py` | 日志系统 | `logger_setup.py` |

### 并发与性能测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_concurrency.py` | 并发测试 | 多模块 |
| `test_call_coverage.py` | 调用覆盖 | 多模块 |

### 集成测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_integration.py` | 集成测试 | 多模块 |
| `test_real_server.py` | 真实服务器测试 | `webui/server.py` |
| `test_secret_manager.py` | 密钥管理 | `secret_manager.py` |
| `test_migrate_encryption.py` | 加密迁移 | `secret_manager.py` |

### 其他测试

| 测试文件 | 测试内容 | 覆盖模块 |
|---------|---------|---------|
| `test_area_watchers.py` | 区域监控 | `area_watchers.py` |
| `test_error_translator.py` | 错误翻译 | `error_translator.py` |
| `test_fts_orphan_cleanup.py` | FTS 孤儿记录清理 | `tmdb_watchlist_db.py` |
| `test_onboarding_e2e.py` | 新手引导端到端测试 | `routes.py`, `server.py` |

## 运行测试

### 运行所有测试

```bash
pytest src/tests/
```

### 运行特定测试文件

```bash
pytest src/tests/test_fts5_search.py
```

### 运行特定测试类

```bash
pytest src/tests/test_fts5_search.py::TestChineseSearch
```

### 运行特定测试方法

```bash
pytest src/tests/test_fts5_search.py::TestChineseSearch::test_search_chinese_keyword
```

### 显示详细输出

```bash
pytest src/tests/ -v
```

### 生成覆盖率报告

```bash
pytest src/tests/ --cov=src --cov-report=html
```

## 测试配置

### conftest.py

全局测试配置和 fixtures：
- 临时目录创建
- 数据库初始化
- Mock 对象配置

### _test_helpers.py

测试辅助函数：
- 数据生成
- 断言辅助
- 清理函数

## 新增测试指南

### 1. 创建测试文件

```python
# src/tests/test_new_feature.py
"""
新功能测试。

测试内容描述。
"""

from __future__ import annotations

import pytest
from pathlib import Path

class TestNewFeature:
    """新功能测试类"""
    
    def test_basic_functionality(self):
        """测试基本功能"""
        # 测试代码
        assert True
```

### 2. 遵循命名规范

- 测试文件: `test_<feature>.py`
- 测试类: `Test<Feature>`
- 测试方法: `test_<description>`

### 3. 使用 fixtures

```python
@pytest.fixture
def temp_db(tmp_path):
    """创建临时数据库"""
    db_path = tmp_path / "test.db"
    return Database(str(db_path))

def test_with_fixture(temp_db):
    """使用 fixture 的测试"""
    assert temp_db is not None
```

### 4. 添加文档字符串

每个测试方法都应该有清晰的文档字符串，说明：
- 测试目的
- 测试场景
- 预期结果

## 测试数据

### 测试数据库

测试使用临时数据库，位于 `tmp_path` 目录，测试完成后自动清理。

### 测试文件

测试文件使用 `tmp_path` fixture 创建，避免污染工作目录。

## 常见问题

### Q: 测试失败怎么办？

A: 
1. 检查错误信息
2. 运行单个测试定位问题
3. 检查测试数据是否正确
4. 查看日志输出

### Q: 如何跳过特定测试？

A: 使用 `@pytest.mark.skip` 装饰器：

```python
@pytest.mark.skip(reason="暂时跳过")
def test_skip_me():
    pass
```

### Q: 如何标记预期失败的测试？

A: 使用 `@pytest.mark.xfail` 装饰器：

```python
@pytest.mark.xfail(reason="已知问题")
def test_known_issue():
    assert False
```

## 持续集成

测试在以下环境运行：
- Python 3.11
- Windows 10/11
- SQLite 3.35+

## 测试覆盖率目标

- 核心模块: >80%
- WebUI: >70%
- 工具函数: >90%

## 相关文档

- [pytest 文档](https://docs.pytest.org/)
- [项目架构文档](../wiki/Architecture-Overview.md)
- [开发指南](../wiki/Development-Guide.md)
