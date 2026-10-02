"""登录流程验证：模拟 OpenList 登录的各种场景，验证 login() 返回结构和错误分类。

场景覆盖：
1. 成功登录（HTTP 200 + 有效 token）
2. 密码错误（HTTP 200 + business code 错误，message 含 "password"）
3. 2FA 错误（message 含 "otp"）
4. 账号不存在（message 含 "user not found"）
5. HTTP 404 Not Found（OpenList 路由不存在 / 反代 404）
6. 网络错误（连接超时）
7. HTTP 200 但无 token（畸形响应）
"""
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# 文件位于 src/scripts/，parent.parent 是 src/
_current = Path(__file__).parent.resolve().parent
sys.path.insert(0, str(_current))

import logging
logging.basicConfig(level=logging.CRITICAL)  # 屏蔽错误日志输出

import webdav_client
from webdav_client import OpenListAdminClient


def make_client():
    """创建一个不读 token 缓存的 client。"""
    with patch.object(OpenListAdminClient, '_load_token_from_cache', return_value=None):
        c = OpenListAdminClient("http://127.0.0.1:5244", "admin", "pass")
    c.token = None  # 强制不使用缓存
    return c


def fake_response(status_code, json_body=None, raise_json=False):
    res = MagicMock()
    res.status_code = status_code
    if json_body is None:
        res.json.side_effect = Exception("not json")
    else:
        res.json.return_value = json_body
    return res


results = []

def scenario(name, status_code, json_body, expect_success, expect_error_type):
    client = make_client()
    with patch.object(client.session, 'post', return_value=fake_response(status_code, json_body)):
        r = client.login(force=True)
    ok = (r == expect_success and client.last_error_type == expect_error_type)
    results.append((name, ok, {"success": r, "error_type": client.last_error_type, "error_message": client.last_error_message}))
    return ok


# 场景 1: 成功
scenario("成功登录", 200,
         {"data": {"token": "tok-123"}},
         expect_success=True, expect_error_type=None)

# 场景 2: 密码错误 (业务 message)
scenario("密码错误 (200+msg)", 200,
         {"data": {}, "message": "username or password is wrong"},
         expect_success=False, expect_error_type="wrong_password")

# 场景 3: 2FA 错误
scenario("2FA 错误 (200+msg)", 200,
         {"data": {}, "message": "otp code is wrong"},
         expect_success=False, expect_error_type="wrong_2fa")

# 场景 4: 账号不存在
scenario("账号不存在 (200+msg)", 200,
         {"data": {}, "message": "user not found"},
         expect_success=False, expect_error_type="account_not_found")

# 场景 5: HTTP 404 + JSON 错误体
scenario("HTTP 404 (JSON)", 404,
         {"message": "Not Found"},
         expect_success=False, expect_error_type="unknown")

# 场景 6: HTTP 404 + HTML (json 解析失败)
scenario("HTTP 404 (HTML)", 404, None,
         expect_success=False, expect_error_type="unknown")

# 场景 7: HTTP 200 无 token 无 message
scenario("200 无token 无msg", 200,
         {"data": {}},
         expect_success=False, expect_error_type="unknown")

# 场景 8: 网络错误
client = make_client()
import requests as req_mod
with patch.object(client.session, 'post', side_effect=req_mod.exceptions.ConnectionError("refused")):
    r = client.login(force=True)
ok = (r is False and client.last_error_type == "network_error")
results.append(("网络错误", ok, {"success": r, "error_type": client.last_error_type, "error_message": client.last_error_message}))

# 输出结果
print("=" * 70)
print("登录流程验证结果")
print("=" * 70)
all_ok = True
for name, ok, r in results:
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_ok = False
    detail = json.dumps(r, ensure_ascii=False)
    print(f"[{status}] {name:30s} -> {detail}")

print("=" * 70)
print(f"总计: {sum(1 for _, ok, _ in results if ok)}/{len(results)} 通过")
if all_ok:
    print("结论: 所有登录场景分类正确，无未处理异常")
else:
    print("结论: 存在失败场景，需检查")
sys.exit(0 if all_ok else 1)
