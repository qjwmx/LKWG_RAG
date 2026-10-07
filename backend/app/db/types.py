"""SQLAlchemy 基类与通用列类型。

向量以 BLOB 存 numpy float32 原始字节，而不是依赖 pgvector。理由：
免去数据库扩展依赖，让项目在 SQLite 上开箱可跑；万级切片内应用层精确余弦检索
（精确 KNN）的召回优于近似索引，完全够本场景。
"""

from __future__ import annotations

import numpy as np
from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator


class VectorBlob(TypeDecorator):
    """float32 向量 <-> BLOB。

    ``dtype=np.float32`` 必须固定：用 float64 存会让体积翻倍，且与检索时的
    矩阵乘法精度不一致，导致同一向量在不同路径下算出不同的相似度。
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        array = np.asarray(value, dtype=np.float32).ravel()
        return array.tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return np.frombuffer(value, dtype=np.float32)
