"""编辑器 MCP 配置片段（TASK-040 §"编辑器配置输出"）。

为什么单独成文件：这段文案会出现在**两个地方**（``nova-service local``/``serve`` 的就绪信息、
``nova-service mcp-config`` 单独输出），且是用户复制粘贴到编辑器配置里的东西——只有一份实现，
才谈得上"改了 URL 形态两处一起改"。

纪律：

- **不写 token**（本地单用户模式无鉴权，R34）；
- 只出现 ``127.0.0.1``/``localhost`` 形式的地址，不写用户的家目录或仓库绝对路径；
- 只承诺当前版本真有的传输形态（Streamable HTTP）；stdio 代理明确写成 M2c 的事（R38）。
"""

from __future__ import annotations

from nova_service.mcp import MCP_MOUNT_PATH

__all__ = [
    "CURSOR_LABEL",
    "HARNESS_LABEL",
    "editor_config_snippets",
    "format_snippets",
    "mcp_url",
]

#: 片段的两类接收方（键即输出里的标题）。
CURSOR_LABEL = "Cursor（项目内 .cursor/mcp.json 或全局 ~/.cursor/mcp.json）"
HARNESS_LABEL = "Claude Code / 其它支持 HTTP 传输的 harness"

#: stdio-only harness 的说明（不假装支持：R38 把 stdio 留给 M2c 的 Rust client）。
_STDIO_NOTE = (
    "只支持 stdio 的 harness 需要一个 stdio 代理（在本地把 stdio 转发到本 URL）——"
    "归 M2c 的 Rust client（TASK-040R），当前版本未提供。"
)


def mcp_url(port: int, *, host: str = "127.0.0.1") -> str:
    """MCP 端点 URL（编辑器里填这个）。"""
    return f"http://{host}:{port}{MCP_MOUNT_PATH}"


def editor_config_snippets(port: int, *, host: str = "127.0.0.1") -> dict[str, str]:
    """``{接收方标题: 可直接粘贴的片段}``（冻结入口）。"""
    url = mcp_url(port, host=host)
    cursor = f'{{ "mcpServers": {{ "nova": {{ "url": "{url}" }} }} }}'
    return {
        CURSOR_LABEL: cursor,
        HARNESS_LABEL: f"{cursor}\n（URL = {url}；若编辑器要求 JSON 文件，直接写上面的对象即可）"
        f"\n{_STDIO_NOTE}",
    }


def format_snippets(port: int, *, host: str = "127.0.0.1") -> str:
    """人类可读的多行文本（``nova-service mcp-config`` 与就绪信息共用）。"""
    blocks = [f"nova MCP 端点：{mcp_url(port, host=host)}（Streamable HTTP）"]
    blocks.extend(
        f"{label}：\n{snippet}"
        for label, snippet in editor_config_snippets(port, host=host).items()
    )
    return "\n\n".join(blocks)
