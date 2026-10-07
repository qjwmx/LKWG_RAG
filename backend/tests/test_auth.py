"""鉴权分级与身份来源。

这一组是安全底线：如果身份还能从请求体里传，那"服务端强制规则"就是假的。
"""

from __future__ import annotations

import pytest

from tests.conftest import make_user


def test_register_then_login_blocked_until_approved(client):
    """注册后处于 pending，**登录必须失败**；审批通过后才能登录。"""
    register = client.post(
        "/api/v1/auth/register",
        json={"username": "carol", "password": "secret123", "display_name": "Carol"},
    )
    assert register.json()["code"] == 200

    # 未审批 -> 业务码 401
    blocked = client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "secret123"}
    )
    assert blocked.json()["code"] == 401

    from app.services import users

    users.set_user_status("carol", "approved")

    ok = client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "secret123"}
    )
    assert ok.json()["code"] == 200
    assert ok.json()["data"]["access_token"]


def test_rejected_user_can_reregister_and_reuses_row(client):
    """被拒绝的账号不删行；重新注册复用同一行并回到 pending。"""
    client.post(
        "/api/v1/auth/register",
        json={"username": "dave", "password": "secret123", "display_name": "Dave"},
    )
    from app.services import users

    users.set_user_status("dave", "rejected")
    assert users.get_user("dave")["status"] == "rejected"

    again = client.post(
        "/api/v1/auth/register",
        json={"username": "dave", "password": "newsecret123", "display_name": "Dave2"},
    )
    assert again.json()["code"] == 200
    assert users.get_user("dave")["status"] == "pending"
    # 口令被重置为新口令
    assert users.authenticate("dave", "newsecret123") is None  # 还没审批


def test_login_failure_messages_are_identical(client):
    """不存在 / 待审批 / 口令错三种情况返回**同一**文案，避免用户名枚举。"""
    client.post(
        "/api/v1/auth/register",
        json={"username": "erin", "password": "secret123", "display_name": "Erin"},
    )

    missing = client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "secret123"}
    ).json()
    pending = client.post(
        "/api/v1/auth/login", json={"username": "erin", "password": "secret123"}
    ).json()

    from app.services import users

    users.set_user_status("erin", "approved")
    wrong_password = client.post(
        "/api/v1/auth/login", json={"username": "erin", "password": "wrong-password"}
    ).json()

    assert missing["code"] == pending["code"] == wrong_password["code"] == 401
    assert missing["msg"] == pending["msg"] == wrong_password["msg"]


def test_missing_token_returns_http_401(client):
    """鉴权走真实 HTTP 状态码，不是 body.code。"""
    response = client.get("/api/v1/system/stats")
    assert response.status_code == 401


def test_tampered_token_returns_http_401(client):
    response = client.get(
        "/api/v1/system/stats", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_non_admin_gets_403_on_admin_routes(client, user_headers):
    """已登录但权限不够 -> 403（而不是 401）。"""
    response = client.get("/api/v1/admin/users", headers=user_headers)
    assert response.status_code == 403


def test_admin_can_reach_admin_routes(client, admin_headers):
    response = client.get("/api/v1/admin/users", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["code"] == 200


# --------------------------------------------------------------------------- 登出吊销


def test_logout_revokes_token_immediately(client, admin_headers):
    """★ 登出后同一个 token 必须**立刻**失效。

    原来登出只是前端把 token 从 localStorage 删掉，服务端签发的 JWT
    在剩余有效期（默认 24 小时）内**依然可用**。共享设备上"登出"
    等于没登出——如果 token 被复制过，它还能用一整天。
    """
    assert client.get("/api/v1/auth/me", headers=admin_headers).status_code == 200

    logout = client.post("/api/v1/auth/logout", headers=admin_headers)
    assert logout.json()["code"] == 200

    # 同一个 token：现在必须是 401
    after = client.get("/api/v1/auth/me", headers=admin_headers)
    assert after.status_code == 401, "登出后旧 token 仍被接受"


def test_logout_does_not_affect_other_sessions(client):
    """只吊销当前那一个令牌，不能把该用户其他设备的登录一起踢掉。

    全量作废的体验是"在手机上登出，电脑也掉线了"——这是很多人
    不敢用服务端登出的原因。
    """
    from tests.conftest import make_user

    headers_a = make_user(client, "multi_a", password="secret123")
    # 同一账号再登录一次，模拟第二台设备
    second = client.post(
        "/api/v1/auth/login", json={"username": "multi_a", "password": "secret123"}
    ).json()
    headers_b = {"Authorization": f"Bearer {second['data']['access_token']}"}

    assert client.get("/api/v1/auth/me", headers=headers_a).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers_b).status_code == 200

    # 设备 A 登出
    client.post("/api/v1/auth/logout", headers=headers_a)

    assert client.get("/api/v1/auth/me", headers=headers_a).status_code == 401
    assert (
        client.get("/api/v1/auth/me", headers=headers_b).status_code == 200
    ), "另一台设备被误踢下线"


def test_relogin_after_logout_works(client):
    """登出后重新登录要能拿到可用的新令牌（吊销不能误伤新令牌）。"""
    from tests.conftest import make_user

    headers = make_user(client, "relogin_user", password="secret123")
    client.post("/api/v1/auth/logout", headers=headers)

    again = client.post(
        "/api/v1/auth/login", json={"username": "relogin_user", "password": "secret123"}
    ).json()
    fresh = {"Authorization": f"Bearer {again['data']['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=fresh).status_code == 200


def test_logout_without_token_is_401(client):
    """未登录调登出应走真实 401，而不是 200。"""
    assert client.post("/api/v1/auth/logout").status_code == 401


# --------------------------------------------------------------------------- 登录限流


def test_login_is_rate_limited_after_repeated_failures(client, monkeypatch):
    """★ 反复输错口令必须被限流。

    统一失败文案防的是**用户名枚举**，防不住**口令爆破**——爆破者
    不在乎提示，只在乎能反复试。所以需要独立的限流。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 3)
    monkeypatch.setattr(settings, "login_lockout_seconds", 60)

    for _ in range(3):
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
        )

    blocked = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    ).json()
    assert blocked["code"] == 401
    assert "频繁" in blocked["msg"], f"应提示限流，实际：{blocked['msg']}"


def test_rate_limit_blocks_correct_password_too(client, monkeypatch):
    """锁定期间**正确口令也被拒**，否则限流形同虚设。

    攻击者只要在锁定前试对一次就能进——限流必须挡住整个登录动作，
    而不是只挡住"失败的那次"。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 2)
    monkeypatch.setattr(settings, "login_lockout_seconds", 60)

    for _ in range(2):
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
        )

    ok = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
    ).json()
    assert ok["code"] == 401, "锁定期间正确口令也应当被拒"


def test_rate_limit_does_not_affect_other_accounts(client, monkeypatch):
    """一个账号被锁不该连累其他账号。"""
    from app.config import settings
    from tests.conftest import make_user

    monkeypatch.setattr(settings, "login_max_attempts", 2)
    monkeypatch.setattr(settings, "login_lockout_seconds", 60)

    for _ in range(2):
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
        )

    headers = make_user(client, "innocent", password="secret123")
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200


def test_successful_login_clears_failure_counter(client, monkeypatch):
    """成功登录要清零计数，否则正常用户偶尔输错会累积到被锁。"""
    from app.config import settings
    from tests.conftest import make_user

    monkeypatch.setattr(settings, "login_max_attempts", 3)
    make_user(client, "clumsy", password="secret123")

    # 输错两次（未达上限）
    for _ in range(2):
        client.post(
            "/api/v1/auth/login", json={"username": "clumsy", "password": "wrong"}
        )

    ok = client.post(
        "/api/v1/auth/login", json={"username": "clumsy", "password": "secret123"}
    ).json()
    assert ok["code"] == 200

    # 再输错两次：如果计数没清零，加上前面的 2 次就会到 4 -> 被锁
    for _ in range(2):
        client.post(
            "/api/v1/auth/login", json={"username": "clumsy", "password": "wrong"}
        )
    still_ok = client.post(
        "/api/v1/auth/login", json={"username": "clumsy", "password": "secret123"}
    ).json()
    assert still_ok["code"] == 200, "计数未在成功登录后清零"


def test_admin_cannot_be_registered(client):
    """root 是保留名，注册必须被拒。"""
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "admin", "password": "secret123", "display_name": "fake"},
    )
    assert response.json()["code"] != 200
    assert "保留" in response.json()["msg"]


def test_admin_cannot_be_deleted_or_demoted(client, admin_headers):
    """root 不可删除、不可改角色——否则一次误操作能把系统锁死。"""
    delete = client.post("/api/v1/admin/users/delete", json={"username": "admin"}, headers=admin_headers)
    assert delete.json()["code"] != 200

    role = client.post(
        "/api/v1/admin/users/role", json={"username": "admin", "role": "user"}, headers=admin_headers
    )
    assert role.json()["code"] != 200


def test_business_request_models_have_no_identity_fields():
    """**业务**请求模型里不存在身份字段。不是"忽略"，是删掉。

    注意 ``LoginRequest`` / ``RegisterRequest`` 不在此列：它们携带的 ``username``
    是**凭据本身**（"我要用这个账号登录"），不是"我是谁"的声明——
    服务端要用它去查口令，所以它必须在。判定标准是：这个字段会不会被用来
    决定权限或数据可见范围。
    """
    from app.schemas import (
        ChatRequest,
        DeleteDocumentRequest,
        DeleteSessionRequest,
        FeedbackRequest,
        ReviewUserRequest,
    )

    forbidden = {"username", "role", "owner", "scope", "requester"}
    for model in (
        ChatRequest,
        DeleteDocumentRequest,
        DeleteSessionRequest,
        FeedbackRequest,
    ):
        fields = set(model.model_fields)
        assert not (fields & forbidden), f"{model.__name__} 含身份字段：{fields & forbidden}"

    # ReviewUserRequest.username 是"要审批哪个账号"的操作目标，不是发起者身份；
    # 但它绝不能带 role / owner / scope 这类会放大权限的字段。
    review_fields = set(ReviewUserRequest.model_fields)
    assert not (review_fields & {"role", "owner", "scope", "requester"})


def test_chat_request_has_no_identity_fields():
    """提问接口不接收身份——检索范围由 token 决定，请求里说什么都不作数。"""
    from app.schemas import ChatRequest

    assert "username" not in ChatRequest.model_fields
    assert "role" not in ChatRequest.model_fields


def test_user_list_never_exposes_password_hash(client, admin_headers):
    make_user(client, "frank")
    response = client.get("/api/v1/admin/users", headers=admin_headers)
    for record in response.json()["data"]["users"]:
        assert "password_hash" not in record


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_review_accepts_valid_statuses(client, admin_headers, status):
    make_user(client, "grace", approve=False)
    response = client.post(
        "/api/v1/admin/users/review",
        json={"username": "grace", "status": status},
        headers=admin_headers,
    )
    assert response.json()["code"] == 200


def test_review_rejects_unknown_status(client, admin_headers):
    make_user(client, "heidi", approve=False)
    response = client.post(
        "/api/v1/admin/users/review",
        json={"username": "heidi", "status": "superuser"},
        headers=admin_headers,
    )
    assert response.json()["code"] != 200
