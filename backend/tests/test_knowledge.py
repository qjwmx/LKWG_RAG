"""文档入库、去重、删除级联、可见范围。"""

from __future__ import annotations

import io

from tests.conftest import make_user


def upload(client, headers, name: str, content: bytes, category: str = "阵容攻略", version: str = "v1.0"):
    return client.post(
        "/api/v1/knowledge_base/upload_docs",
        files=[("files", (name, io.BytesIO(content), "application/octet-stream"))],
        data={"category": category, "version": version, "title": ""},
        headers=headers,
    )


# --------------------------------------------------------------------------- .md 支持


def test_markdown_upload_is_supported(client, admin_headers, sample_text):
    """`.md` 是本次新增支持的类型，必须能解析入库。"""
    response = upload(client, admin_headers, "制度.md", sample_text.encode("utf-8"))
    body = response.json()
    assert body["code"] == 200
    item = body["data"]["uploaded"][0]
    assert item["status"] == "success", item["message"]


def test_markdown_long_extension_is_supported(client, admin_headers, sample_text):
    """`.markdown` 同样支持（两个扩展名走同一条纯文本路径）。"""
    response = upload(client, admin_headers, "说明.markdown", sample_text.encode("utf-8"))
    assert response.json()["data"]["uploaded"][0]["status"] == "success"


def test_markdown_syntax_is_preserved(client, admin_headers):
    """Markdown 语法刻意保留不清洗：标题层级是天然的切分边界。"""
    from app.services import storage
    from app.services.documents import ingest_bytes

    text = "# 大标题\n\n## 小节\n\n- 条目一\n- 条目二\n\n正文内容。"
    result = ingest_bytes(
        file_name="语法.md",
        file_bytes=text.encode("utf-8"),
        category="未分类",
        owner="admin",
        scope="public",
    )
    assert result["status"] == "success"
    document_id = result["document"]["id"]

    from app.services.vector_store import get_chunks

    chunks = get_chunks(document_id)
    joined = "\n".join(c["content"] for c in chunks)
    # `#` 与 `-` 都应原样保留
    assert "# 大标题" in joined
    assert "- 条目一" in joined
    assert storage.get_document(document_id) is not None


def test_unsupported_extension_is_rejected(client, admin_headers):
    response = upload(client, admin_headers, "恶意.exe", b"MZ\x90\x00binary")
    body = response.json()
    # 全部失败 -> 非 200 的业务码，并给出可读原因
    assert body["code"] != 200
    assert "不支持" in str(body["data"])


def test_batch_upload_partial_failure_does_not_block(client, admin_headers, sample_text):
    """一批里有一个坏文件，好的那个仍要成功入库。"""
    response = client.post(
        "/api/v1/knowledge_base/upload_docs",
        files=[
            ("files", ("好文件.md", io.BytesIO(sample_text.encode("utf-8")), "text/markdown")),
            ("files", ("坏文件.exe", io.BytesIO(b"binary"), "application/octet-stream")),
        ],
        data={"category": "阵容攻略", "version": "v1.0", "title": ""},
        headers=admin_headers,
    )
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["uploaded"][0]["status"] == "success"
    assert "坏文件.exe" in body["data"]["failed_files"]


# --------------------------------------------------------------------------- 去重


def test_same_content_is_deduplicated(client, admin_headers, sample_text):
    first = upload(client, admin_headers, "制度.md", sample_text.encode("utf-8"))
    assert first.json()["data"]["uploaded"][0]["status"] == "success"

    # 同内容、不同文件名 -> duplicate
    second = upload(client, admin_headers, "制度副本.md", sample_text.encode("utf-8"))
    assert second.json()["data"]["uploaded"][0]["status"] == "duplicate"

    from app.services import storage

    assert len(storage.list_documents(include_all=True)) == 1


# --------------------------------------------------------------------------- 删除级联


def test_deleting_document_cascades_to_chunks(client, admin_headers, sample_text):
    """删文档后切片必须清零（靠 ON DELETE CASCADE，不是应用层删）。"""
    created = upload(client, admin_headers, "制度.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    from app.services.vector_store import get_chunks

    assert len(get_chunks(document_id)) > 0

    deleted = client.post(
        "/api/v1/knowledge_base/delete_docs",
        json={"document_id": document_id},
        headers=admin_headers,
    )
    assert deleted.json()["code"] == 200

    # 级联生效 -> 切片为空
    assert get_chunks(document_id) == []


def test_sqlite_foreign_keys_are_enabled():
    """SQLite 默认不强制外键，不开 PRAGMA 的话级联删除形同虚设。"""
    from sqlalchemy import text

    from app.db.session import transaction

    with transaction() as session:
        value = session.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1


# --------------------------------------------------------------------------- 可见范围


def test_private_document_isolated_between_users(client, user_headers, other_user_headers, sample_text):
    """普通用户之间互相看不到对方上传的私有文档。"""
    upload(client, user_headers, "我的私有.md", sample_text.encode("utf-8"))

    mine = client.get("/api/v1/knowledge_base/list_files", headers=user_headers).json()
    theirs = client.get("/api/v1/knowledge_base/list_files", headers=other_user_headers).json()

    assert len(mine["data"]) == 1
    assert theirs["data"] == []


def test_user_upload_lands_in_private_scope(client, user_headers, sample_text):
    upload(client, user_headers, "私有.md", sample_text.encode("utf-8"))
    listed = client.get("/api/v1/knowledge_base/list_files", headers=user_headers).json()
    record = listed["data"][0]
    assert record["scope"] == "private"
    assert record["owner"] == "alice"


def test_admin_upload_lands_in_public_scope(client, admin_headers, sample_text):
    upload(client, admin_headers, "公共.md", sample_text.encode("utf-8"))
    listed = client.get("/api/v1/knowledge_base/list_files", headers=admin_headers).json()
    record = listed["data"][0]
    assert record["scope"] == "public"


def test_public_document_visible_to_everyone(client, admin_headers, user_headers, sample_text):
    upload(client, admin_headers, "公共.md", sample_text.encode("utf-8"))
    listed = client.get("/api/v1/knowledge_base/list_files", headers=user_headers).json()
    assert len(listed["data"]) == 1


def test_admin_sees_other_users_private_documents(client, admin_headers, user_headers, sample_text):
    """root 权限：能看到并管理所有人的文档，含他人私有。"""
    upload(client, user_headers, "alice私有.md", sample_text.encode("utf-8"))

    listed = client.get("/api/v1/knowledge_base/list_files", headers=admin_headers).json()
    assert len(listed["data"]) == 1
    assert listed["data"][0]["owner"] == "alice"
    assert listed["data"][0]["scope"] == "private"


def test_user_cannot_delete_others_private_document(client, admin_headers, user_headers, other_user_headers, sample_text):
    created = upload(client, user_headers, "alice私有.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    # bob 不知道 id 也能试（这里刻意直接拿到 id，验证服务端拦截）
    denied = client.post(
        "/api/v1/knowledge_base/delete_docs",
        json={"document_id": document_id},
        headers=other_user_headers,
    )
    assert denied.json()["code"] == 403

    # 文件还在
    assert len(client.get("/api/v1/knowledge_base/list_files", headers=user_headers).json()["data"]) == 1


def test_user_cannot_delete_public_document(client, admin_headers, user_headers, sample_text):
    """公共库归 root 维护，普通用户不能删。"""
    created = upload(client, admin_headers, "公共.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    denied = client.post(
        "/api/v1/knowledge_base/delete_docs",
        json={"document_id": document_id},
        headers=user_headers,
    )
    assert denied.json()["code"] == 403


def test_admin_can_delete_any_document(client, admin_headers, user_headers, sample_text):
    created = upload(client, user_headers, "alice私有.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    deleted = client.post(
        "/api/v1/knowledge_base/delete_docs",
        json={"document_id": document_id},
        headers=admin_headers,
    )
    assert deleted.json()["code"] == 200


def test_user_cannot_read_others_chunks(client, user_headers, other_user_headers, sample_text):
    """切片接口也要挡：否则 document_id 可被枚举出他人文档的全文。"""
    created = upload(client, user_headers, "私有.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    denied = client.get(
        "/api/v1/knowledge_base/chunks", params={"document_id": document_id}, headers=other_user_headers
    )
    assert denied.json()["code"] == 403

    allowed = client.get(
        "/api/v1/knowledge_base/chunks", params={"document_id": document_id}, headers=user_headers
    )
    assert allowed.json()["code"] == 200
    assert len(allowed.json()["data"]["chunks"]) > 0


# --------------------------------------------------------------------------- 补偿清理


def test_failed_ingest_cleans_up_chunks_and_keeps_original_error(
    client, admin_headers, monkeypatch, sample_text
):
    """回写 status 失败时，已落库的切片要被清掉，且**原始异常不能被吞**。

    只让"回写 success"这一步失败：新文档的 processing 状态是 ``add_document``
    写的，不经过 ``update_document``，所以按调用次数计数会误判。
    """
    from app.services import documents, storage, vector_store

    original_update = storage.update_document

    def flaky_update(document_id, patch):
        if patch.get("status") == "success":
            raise RuntimeError("模拟回写失败")
        return original_update(document_id, patch)

    monkeypatch.setattr(documents.storage, "update_document", flaky_update)

    result = documents.ingest_bytes(
        file_name="会失败的.md",
        file_bytes=sample_text.encode("utf-8"),
        category="阵容攻略",
        owner="admin",
        scope="public",
    )

    assert result["status"] == "failed"
    # 原始异常信息必须保留，而不是被清理逻辑盖掉
    assert "模拟回写失败" in result["message"]

    document_id = result["document"]["id"] if result["document"] else None
    assert document_id, "失败也要返回文档记录，否则前端无从展示失败原因"

    # 补偿清理生效：切片被清空，chunk_count 归零
    assert vector_store.get_chunks(document_id) == []
    assert storage.get_document(document_id)["chunk_count"] == 0


def test_categories_endpoint_lists_markdown(client, admin_headers):
    """前端的上传校验读这里，`.md` 必须出现在支持列表里。"""
    data = client.get("/api/v1/system/categories", headers=admin_headers).json()["data"]
    assert "md" in data["supported_file_types"]
    assert "markdown" in data["supported_file_types"]


def test_chunk_index_is_returned_for_citation_highlighting(client, admin_headers, sample_text):
    """切片要带 chunk_index，前端靠它定位并高亮。"""
    created = upload(client, admin_headers, "制度.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    data = client.get(
        "/api/v1/knowledge_base/chunks", params={"document_id": document_id}, headers=admin_headers
    ).json()["data"]
    assert data["chunks"]
    for chunk in data["chunks"]:
        assert "chunk_index" in chunk
        assert isinstance(chunk["chunk_index"], int)
    # 切片接口不该把向量带出去（白占带宽）
    assert "embedding" not in data["chunks"][0]


# --------------------------------------------------------------------------- 缓存失效（集成）


def test_upload_invalidates_search_cache(client, admin_headers, sample_text):
    """★ 上传文档后，检索缓存必须立即失效。

    这是缓存最容易出错的地方：只用「查询文本」做键的话，用户上传新文档后
    同一个问题在 TTL 内仍命中旧结果——表现为"上传成功但搜不到新内容"，
    而且**不报错**，最难排查。这里走真实的 HTTP 上传路径，验证
    ``ingest_bytes`` 一路把语料版本号推上去了。
    """
    from app.cache import corpus_version
    from app.services import vector_store

    before = corpus_version()

    # 先做一次检索，把结果灌进缓存
    vector_store.search("年假", category="全部", requester="admin", include_all=True)
    cached = vector_store.search("年假", category="全部", requester="admin", include_all=True)
    assert cached is not None  # 第二次应是缓存命中（不抛错即通过）

    # 上传一份内容相关的新文档
    response = upload(client, admin_headers, "年假制度.md", sample_text.encode("utf-8"))
    assert response.json()["data"]["uploaded"][0]["status"] == "success"

    after = corpus_version()
    assert after != before, "上传后语料版本号没变 -> 检索缓存不会失效"


def test_delete_invalidates_search_cache(client, admin_headers, sample_text):
    """★ 删除文档后缓存也要失效。

    删除走的是 ``ON DELETE CASCADE``，**不经过** ``vector_store.delete_chunks``。
    如果失效逻辑只写在 delete_chunks 里，这条路径就漏了——用户删了私有文档，
    却还能从缓存里检索到它。
    """
    from app.cache import corpus_version

    created = upload(client, admin_headers, "待删.md", sample_text.encode("utf-8")).json()
    document_id = created["data"]["uploaded"][0]["document_id"]

    before = corpus_version()
    deleted = client.post(
        "/api/v1/knowledge_base/delete_docs",
        json={"document_id": document_id},
        headers=admin_headers,
    )
    assert deleted.json()["code"] == 200

    assert corpus_version() != before, "删除后语料版本号没变 -> 被删内容仍可能从缓存返回"
