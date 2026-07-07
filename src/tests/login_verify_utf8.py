# -*- coding: utf-8 -*-
"""验证 OpenListAdminClient.login() 在各种场景下的返回结构。
使用 mock 替换 session.post，模拟后端响应。
"""
import sys
import os
import json
from pathlib import Path

# 确保 src/ 在 sys.path 最前面（文件位于 src/scripts/，parent.parent 是 src/）
_SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC_DIR))

from unittest.mock import MagicMock, patch


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text="", raise_on_json=False):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._raise_on_json = raise_on_json
    def json(self):
        if self._raise_on_json:
            raise json.JSONDecodeError("Expecting value", text_doc, 0)
        return self._json or {}


def run_login_test(name, fake_resp, totp=""):
    from webdav_client import OpenListAdminClient
    client = OpenListAdminClient("http://127.0.0.1:5244", "admin", "pw", totp_secret=totp)
    client.token = None  # 强制联网
    with patch.object(client.session, "post", return_value=fake_resp):
        result = client.login()
    print(f"[{name}] -> success={result}, error_type={client.last_error_type}")


text_doc = ""

# 1. 成功
run_login_test("success", FakeResp(200, {"code": 200, "data": {"token": "tok123"}}))

# 2. 密码错误 (HTTP 200 + message)
run_login_test("wrong_password_200", FakeResp(200, {"code": 401, "message": "username or password is wrong"}))

# 3. 密码错误 (HTTP 403 + message)
run_login_test("wrong_password_403", FakeResp(403, {"message": "username or password is wrong"}))

# 4. 2FA 错误（使用有效的 base32 编码 secret）
run_login_test("wrong_2fa", FakeResp(403, {"message": "otp code is wrong"}), totp="JBSWY3DPEHPK3PXP")

# 5. 账号不存在
run_login_test("account_not_found", FakeResp(403, {"message": "user not found"}))

# 6. 路由不存在 404 + JSON message
run_login_test("not_found_json", FakeResp(404, {"message": "not found"}))

# 7. 路由不存在 404 + HTML (反代 404 页面)
run_login_test("not_found_html", FakeResp(404, None, text="<html>404</html>", raise_on_json=True))

# 8. 200 但无 token 无 message
run_login_test("200_no_token", FakeResp(200, {"code": 500}))

# 9. 500 服务端错误
run_login_test("server_500", FakeResp(500, {"message": "internal error"}))

print("All scenarios completed.")
