"""
WebUI 独立集成测试（需要真实服务器运行）。

用法:
  python src/tests/test_webui_standalone.py [password]

测试范围:
  - 登录认证
  - TMDB 操作日志 API (/api/tmdb/logs)
  - 主程序日志 API (/api/logs)
  - TMDB 操作日志下载 (/api/tmdb/logs/download)
  - 主程序日志下载 (/api/logs/download)
  - 配置 API (/api/config)
  - 操作码覆盖验证（确保前端 opLabel 覆盖后端所有 op code）

注意: 此脚本需要 WebUI 服务器已在 8579 端口运行，
      或者通过 --start 参数自动启动。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error

# 切换到 src 目录
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.chdir(SRC_DIR)

# 前端 opLabel 映射（与 logs.js 保持一致）
OP_LABEL = {
    'sync': '同步', 'sync_start': '同步启动', 'sync_done': '同步完成',
    'sync_error': '同步失败', 'match_refresh_start': '收录刷新启动',
    'match_refresh': '收录刷新', 'match_refresh_done': '收录刷新完成',
    'match_refresh_error': '收录刷新失败', 'match_override': '收录覆盖',
    'match': '收录匹配', 'match_done': '收录匹配完成',
    'match_error': '收录匹配失败', 'configure': '配置保存',
    'config_save': '配置保存', 'config_update': '配置更新', 'restart': '重启',
    'webui_restart': 'WebUI 重启', 'login': '登录', 'logout': '登出',
    'add': '新增', 'update': '更新', 'delete': '删除', 'fetch': '拉取',
    'search': '搜索', 'cache_clear': '清理缓存', 'cache_hit': '缓存命中',
    'cache_miss': '缓存未命中', 'api_call': 'API 调用', 'api_error': 'API 错误',
    'rate_limit': '速率限制', 'auth': '认证', 'token_refresh': '令牌刷新',
    'watchlist_sync': '待看列表同步', 'watchlist_refresh': '待看列表刷新',
    'info': '信息', 'warn': '警告', 'error': '错误', 'success': '成功',
}

BASE_URL = "http://127.0.0.1:8579"
PASSWD = "admin123"  # 默认密码，可通过命令行参数覆盖


def _api_get(path, token):
    """发送带认证的 GET 请求。"""
    req = urllib.request.Request(
        BASE_URL + path,
        headers={'X-Session-Token': token},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _api_get_raw(path, token):
    """发送带认证的 GET 请求，返回原始响应对象。"""
    req = urllib.request.Request(
        BASE_URL + path,
        headers={'X-Session-Token': token},
    )
    return urllib.request.urlopen(req, timeout=10)


def _login(password):
    """登录并返回 session token。"""
    body = json.dumps({'password': password}).encode()
    req = urllib.request.Request(
        BASE_URL + '/api/login',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    token = data.get('token', '')
    if not token:
        raise RuntimeError(f"登录失败: {data}")
    return token


def _server_running():
    """检查 WebUI 服务器是否在运行。"""
    sock = socket.socket()
    sock.settimeout(2)
    try:
        return sock.connect_ex(('127.0.0.1', 8579)) == 0
    finally:
        sock.close()


def _start_server():
    """启动 WebUI 服务器（仅启动 WebUI 模式）。"""
    proc = subprocess.Popen(
        [sys.executable, '-c',
         'import sys; sys.path.insert(0,"."); from webui.server import main; main()'],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    proc.stdin.write(b'2\n')
    proc.stdin.flush()
    for i in range(10):
        time.sleep(1)
        if _server_running():
            return proc
    proc.terminate()
    raise RuntimeError("WebUI 服务器启动失败")


def _check_login(token):
    """验证登录成功。"""
    assert token, "登录未返回 token"
    print(f"  [PASS] 登录成功 (token={token[:16]}...)")


def _check_tmdb_logs(token):
    """测试 TMDB 操作日志 API。"""
    data = _api_get('/api/tmdb/logs?limit=5', token)
    logs = data.get('logs', [])
    assert isinstance(logs, list), "logs 应为列表"
    assert 'count' in data, "应包含 count 字段"
    print(f"  [PASS] TMDB 操作日志: {len(logs)} 条")
    if logs:
        op = logs[0].get('op', '?')
        msg = logs[0].get('msg', '')[:60]
        print(f"         首条: [{op}] {msg}")
    return logs


def _check_main_logs(token):
    """测试主程序日志 API。"""
    data = _api_get('/api/logs?lines=5', token)
    lines = data.get('lines', [])
    assert isinstance(lines, list), "lines 应为列表"
    assert 'count' in data, "应包含 count 字段"
    print(f"  [PASS] 主程序日志: {len(lines)} 行")


def _check_tmdb_logs_download(token):
    """测试 TMDB 操作日志下载端点。"""
    resp = _api_get_raw('/api/tmdb/logs/download', token)
    assert resp.status == 200, f"应返回 200，实际 {resp.status}"
    content_type = resp.headers.get('Content-Type', '')
    assert 'text/plain' in content_type, f"Content-Type 应为 text/plain: {content_type}"
    content = resp.read().decode('utf-8', errors='replace')
    assert len(content) > 0, "下载内容不应为空"
    print(f"  [PASS] TMDB 日志下载: {len(content)} 字符")
    print(f"         首行: {content.split(chr(10))[0][:100]}")


def _check_main_logs_download(token):
    """测试主程序日志下载端点。"""
    try:
        resp = _api_get_raw('/api/logs/download', token)
        content = resp.read().decode('utf-8', errors='replace')
        print(f"  [PASS] 主程序日志下载: {len(content)} 字符")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  [SKIP] 主程序日志下载: 日志文件不存在 (404)")
        else:
            raise


def _check_config_api(token):
    """测试配置 API。"""
    data = _api_get('/api/config', token)
    assert isinstance(data, dict), "配置应为字典"
    assert len(data) > 0, "配置不应为空"
    print(f"  [PASS] 配置 API: {len(data)} 个字段")


def _check_op_label_coverage(token):
    """验证前端 opLabel 覆盖后端所有操作码。"""
    data = _api_get('/api/tmdb/logs?limit=100', token)
    logs = data.get('logs', [])
    ops = set(log.get('op', '') for log in logs if log.get('op'))
    missing = ops - set(OP_LABEL.keys())
    if missing:
        print(f"  [FAIL] opLabel 缺失: {missing}")
        return False
    print(f"  [PASS] opLabel 覆盖: {len(ops)} 个操作码全部有中文映射")
    return True


def main():
    # 解析命令行参数
    password = PASSWD
    auto_start = False
    for arg in sys.argv[1:]:
        if arg == '--start':
            auto_start = True
        elif not arg.startswith('-'):
            password = arg

    print("=" * 50)
    print("  WebUI 独立集成测试")
    print("=" * 50)

    # 确保服务器在运行
    if not _server_running():
        if auto_start:
            print("\n自动启动 WebUI 服务器...")
            _start_server()
            time.sleep(2)
        else:
            print("\n错误: WebUI 服务器未运行在 8579 端口")
            print("用法: python test_webui_standalone.py --start [password]")
            sys.exit(1)

    # 登录
    print("\n1. 登录认证")
    token = _login(password)
    _check_login(token)

    # 测试各项 API
    print("\n2. TMDB 操作日志")
    logs = _check_tmdb_logs(token)

    print("\n3. 主程序日志")
    _check_main_logs(token)

    print("\n4. TMDB 日志下载")
    _check_tmdb_logs_download(token)

    print("\n5. 主程序日志下载")
    _check_main_logs_download(token)

    print("\n6. 配置 API")
    _check_config_api(token)

    print("\n7. 操作码覆盖验证")
    _check_op_label_coverage(token)

    print("\n" + "=" * 50)
    print("  全部测试完成")
    print("=" * 50)


if __name__ == "__main__":
    main()