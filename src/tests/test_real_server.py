"""
真实服务器安全测试脚本。
连接到运行中的 WebUI (http://127.0.0.1:8579) 执行安全验证。
输出 JSON 日志到 <项目根>/test_logs/real_server_test_*.json
"""
import json, urllib.request, urllib.error, time, os
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8579"
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "test_logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"real_server_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

results = []

def req(method, path, body=None, headers=None, timeout=5):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except:
            return e.code, raw
    except Exception as e:
        return 0, str(e)

def log(name, cat, ep, method, status, expected, passed, body=None, notes=""):
    r = {
        "test": name, "category": cat, "endpoint": ep, "method": method,
        "status": status, "expected": expected, "passed": passed,
        "response": body, "notes": notes, "time": datetime.now().isoformat()
    }
    results.append(r)
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {name}: {method} {ep} -> {status} (expected {expected})")
    return r

def save_log():
    summary = {
        "suite": "Real Server Security Test",
        "server": BASE,
        "timestamp": datetime.now().isoformat(),
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n日志已保存: {LOG_FILE}")

# ============================================================
print("=" * 60)
print("真实服务器安全测试")
print(f"目标: {BASE}")
print("=" * 60)

# 1. 基础连通性
print("\n[1] 基础连通性")
s, b = req("GET", "/api/admin/status")
log("server_alive", "connectivity", "/api/admin/status", "GET",
    s, 200, s == 200, b, "服务器是否在线")

has_password = b.get("has_password", False) if isinstance(b, dict) else False
log("has_password", "connectivity", "/api/admin/status", "GET",
    s, "bool", isinstance(has_password, bool), b, f"has_password={has_password}")

# 2. 敏感数据泄露检测（核心测试）
print("\n[2] 敏感数据泄露检测 - /api/config")
s, b = req("GET", "/api/config")
log("config_reachable", "data_leak", "/api/config", "GET",
    s, 200, s == 200, b, "配置端点可达")

if isinstance(b, dict):
    # TMDB API Key
    api_key = b.get("tmdb_api_key")
    is_bool = isinstance(api_key, bool)
    log("tmdb_api_key_not_leaked", "data_leak", "/api/config", "GET",
        s, "bool", is_bool,
        {"tmdb_api_key": api_key, "type": type(api_key).__name__},
        "TMDB API Key 应为 bool，不应泄露明文")

    # TMDB Token Preview
    token_preview = b.get("tmdb_token_preview")
    no_preview = token_preview is None
    log("tmdb_token_preview_removed", "data_leak", "/api/config", "GET",
        s, "absent", no_preview,
        {"tmdb_token_preview": token_preview},
        "tmdb_token_preview 应已移除")

    # WebDAV Password
    webdav_pwd = b.get("webdav_password")
    pwd_is_bool = isinstance(webdav_pwd, bool)
    log("webdav_password_not_leaked", "data_leak", "/api/config", "GET",
        s, "bool", pwd_is_bool,
        {"webdav_password": webdav_pwd, "type": type(webdav_pwd).__name__},
        "WebDAV 密码应为 bool，不应泄露明文")

    # WebDAV TOTP Secret
    webdav_totp = b.get("webdav_totp_secret")
    totp_is_bool = isinstance(webdav_totp, bool)
    log("webdav_totp_not_leaked", "data_leak", "/api/config", "GET",
        s, "bool", totp_is_bool,
        {"webdav_totp_secret": webdav_totp, "type": type(webdav_totp).__name__},
        "WebDAV TOTP 密钥应为 bool，不应泄露明文")

    # WebDAV User（允许明文，记录实际值用于确认）
    webdav_user = b.get("webdav_user", "")
    log("webdav_user_value", "data_leak", "/api/config", "GET",
        s, "info", True,
        {"webdav_user": webdav_user},
        "WebDAV 用户名（允许明文，仅记录）")

    # TMDB Host（记录实际值）
    tmdb_host = b.get("tmdb_host", "")
    log("tmdb_host_value", "data_leak", "/api/config", "GET",
        s, "info", True,
        {"tmdb_host": tmdb_host},
        "TMDB Host（记录实际配置值）")

    # 列出所有字段，检查有无遗漏的敏感字段
    sensitive_keywords = ["password", "secret", "token", "api_key", "key"]
    leaked_fields = []
    for k, v in b.items():
        if isinstance(v, str) and len(v) > 8:
            for kw in sensitive_keywords:
                if kw in k.lower():
                    leaked_fields.append(f"{k}={v[:20]}...")
    log("no_other_sensitive_fields", "data_leak", "/api/config", "GET",
        s, "clean", len(leaked_fields) == 0,
        {"leaked_fields": leaked_fields, "all_keys": list(b.keys())},
        "检查是否有其他未脱敏的敏感字段")

# 3. 鉴权验证
print("\n[3] 鉴权验证 - 受保护端点")
protected = [
    ("/api/dashboard", "GET"),
    ("/api/area/a", "GET"),
    ("/api/area/b", "GET"),
    ("/api/area/c", "GET"),
    ("/api/records", "GET"),
    ("/api/logs", "GET"),
    ("/api/logs/download", "GET"),
    ("/api/webui/config/openlist", "GET"),
    ("/api/webui/config/tmdb", "GET"),
    ("/api/tmdb/status", "GET"),
    ("/api/tmdb/watchlist", "GET"),
    ("/api/main/status", "GET"),
    ("/api/main/start", "POST"),
    ("/api/main/stop", "POST"),
    ("/api/restart-webui", "POST"),
    ("/api/openlist/test-connection", "POST"),
    ("/api/tmdb/configure", "POST"),
    ("/api/tmdb/watchlist/sync", "POST"),
]
for ep, method in protected:
    s, b = req(method, ep, body={} if method == "POST" else None)
    log(f"auth_{ep.replace('/', '_')}", "auth", ep, method,
        s, 401, s == 401, b, "无 token 应返回 401")

# 4. 白名单端点
print("\n[4] 白名单端点验证")
whitelist = [
    ("/api/config", "GET"),
    ("/api/webui/config/ui", "GET"),
    ("/api/tmdb/avatar?hash=abc", "GET"),
    ("/api/tmdb/poster?path=/test.jpg", "GET"),
    ("/api/openlist/status", "GET"),
    ("/api/openlist/ping", "GET"),
    ("/api/admin/status", "GET"),
]
for ep, method in whitelist:
    s, b = req(method, ep)
    log(f"whitelist_{ep.split('?')[0].replace('/', '_')}", "whitelist", ep, method,
        s, "not_401", s != 401, b, "白名单端点不应返回 401")

# 5. Token 验证
print("\n[5] Token 验证")
s, b = req("GET", "/api/dashboard", headers={"X-Session-Token": "fake_token_12345"})
log("invalid_token_rejected", "token", "/api/dashboard", "GET",
    s, 401, s == 401, b, "无效 token 应返回 401")

# 6. 登录测试
print("\n[6] 登录测试")
s, b = req("POST", "/api/login", body={"password": ""})
log("login_empty_password", "login", "/api/login", "POST",
    s, "400/401", s in (400, 401), b, "空密码应被拒绝")

s, b = req("POST", "/api/login", body={"password": "wrong_password_12345"})
log("login_wrong_password", "login", "/api/login", "POST",
    s, 401, s == 401, b, "错误密码应返回 401")

# 7. 请求体大小限制
print("\n[7] 请求体大小限制 (B-5)")
# 构造一个超过 10MB 的请求
# 注意：单线程服务器在发送 413 响应后会关闭连接，客户端可能无法读取响应
# 因此连接重置（WinError 10053）也视为通过
try:
    big_body = json.dumps({"data": "x" * (11 * 1024 * 1024)}).encode()
    r = urllib.request.Request(f"{BASE}/api/login", data=big_body, method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=10) as resp:
        s = resp.status
        b = json.loads(resp.read().decode())
except urllib.error.HTTPError as e:
    s = e.code
    try:
        b = json.loads(e.read().decode())
    except:
        b = "read error"
except Exception as e:
    s = 0
    b = str(e)
# 413 或连接重置都视为通过（服务器正确拒绝了超大请求）
passed = s == 413 or (s == 0 and "中止" in str(b))
log("max_content_length_11mb", "request_limit", "/api/login", "POST",
    s, "413/connection_reset", passed, b, "11MB 请求体应返回 413 或关闭连接")

# 8. OpenList 配置端点（需要认证才能获取明文凭据）
print("\n[8] OpenList 配置端点鉴权")
s, b = req("GET", "/api/webui/config/openlist")
log("openlist_config_no_auth", "auth", "/api/webui/config/openlist", "GET",
    s, 401, s == 401, b, "无认证不应返回 OpenList 配置")

# 9. TMDB 配置端点鉴权
print("\n[9] TMDB 配置端点鉴权")
s, b = req("POST", "/api/tmdb/configure", body={"anime_max_season_diff": 0.5, "anime_min_season_ratio": 0.5})
log("tmdb_configure_no_auth", "auth", "/api/tmdb/configure", "POST",
    s, 401, s == 401, b, "无认证不应允许配置 TMDB")

# 10. TMDB 海报代理端点鉴权
print("\n[10] TMDB 海报代理端点鉴权")
s, b = req("GET", "/api/tmdb/poster?path=/test.jpg")
log("tmdb_poster_no_auth", "auth", "/api/tmdb/poster?path=/test.jpg", "GET",
    s, "not_401", s != 401, b, "白名单端点不应返回 401")

# 11. 日志下载端点鉴权（/api/logs/download）
print("\n[11] 日志下载端点鉴权")
# 无 token 应返回 401
s, b = req("GET", "/api/logs/download")
log("logs_download_no_auth", "auth", "/api/logs/download", "GET",
    s, 401, s == 401, b, "无 token 应返回 401")

# 无效 token 应返回 401
s, b = req("GET", "/api/logs/download", headers={"X-Session-Token": "fake_token_xyz"})
log("logs_download_invalid_token", "auth", "/api/logs/download", "GET",
    s, 401, s == 401, b, "无效 token 应返回 401")

# 保存日志
save_log()

# 打印摘要
print("\n" + "=" * 60)
print(f"测试完成: {len(results)} 项")
print(f"  通过: {sum(1 for r in results if r['passed'])}")
print(f"  失败: {sum(1 for r in results if not r['passed'])}")
print(f"日志: {LOG_FILE}")
print("=" * 60)
