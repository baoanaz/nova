"""CJK 预分词模块（D-20 / D-45，Module/02 §4.2-b）。

索引与查询必须调用同一 ``segment`` 函数，保证 FTS5 两侧 token 空间一致。
"""

from nova_core.text.segmenter import segment

__all__ = ["segment"]
