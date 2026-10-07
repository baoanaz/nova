"""本地 / API embedding 模型注册表（TASK-008 §B/§C）。

设计要点：
- ``dim`` 与 ``max_input_tokens`` 只在这里定义，实现（local.py / api.py）不得写死，
  这样 TASK-015 bake-off 调整默认模型或截断值时只需改本文件；
- 本注册表也是 TASK-015 的候选清单来源与 ``EmbeddingProfile`` 的唯一构造依据；
- ``model_id`` 命名按 TASK-008 §A：``local:<model_slug>`` / ``api:<model_name>``，
  写入 ``index_config`` 后参与 D-07 二级失效判定（TASK-007/009 消费）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nova_core.embedding.base import EmbeddingConfigError

#: 池化方式：``mean`` = 注意力掩码加权平均（e5 系列）；``cls`` = 取首 token（bge / arctic）。
Pooling = Literal["mean", "cls"]

#: 暂定默认本地模型；最终由 TASK-015 bake-off 钉死（D-44）。
DEFAULT_LOCAL_SLUG = "multilingual-e5-small"


@dataclass(frozen=True, slots=True)
class ApiTransportSpec:
    """一个 API 来源（厂商）的**传输特征**（TASK-049 §3.1）。

    本类描述“怎么跟这家说话”，与“这个模型是什么”（维度/上下文/前缀，见 ``ApiModelSpec``）分离：

    - 认证头与 scheme（OpenAI-compatible 家族是 ``Bearer``，但并非所有厂商）；
    - **批上限**（条数与总 token）：用于校验与默认值选择。实测：硅基流动 800 条、
      Voyage 1000 条且总 token ≤ ~320K（见 TASK-049 §8）；
    - 响应体量约束：实测 dim=1024 下 1000 条会得到 12.7 MB 响应，**对端会在传输中途断连**
      （``RemoteProtocolError``），因此安全批大小需明显小于条数上限（默认取 500）；
    - 错误详情字段名：硅基流动用 ``message``、Voyage 用 ``detail``（仅影响可读性）；
    - 是否支持 ``input_type``（非对称嵌入）：Voyage 支持，实测区分度差异不大，默认不发。
    """

    provider: str
    #: 该厂商默认 base_url（env 可覆盖）。``None`` 表示必须由用户显式提供。
    base_url: str | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer "
    #: 单请求条数上限（校验用）。
    max_batch_items: int = 500
    #: 单请求累计 token 上限（校验用）。
    max_batch_tokens: int = 300_000
    #: **安全**批大小：受响应体量与带宽约束，默认明显小于 ``max_batch_items``（TASK-049 §8.2）。
    safe_batch_items: int = 500
    #: 是否支持 ``input_type=document|query``（Voyage）。默认不发。
    supports_input_type: bool = False
    #: 错误响应里的详情字段名（可读性用）。
    error_detail_field: str = "message"
    #: 是否请求 ``encoding_format=base64``（float32 小端字节）。只对实测/文档确认的厂商开启：
    #: 不认识该参数的兼容服务可能报错。
    supports_base64: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class LocalModelSpec:
    """本地 ONNX 模型条目。"""

    slug: str
    repo_id: str
    onnx_file: str
    dim: int
    pooling: Pooling
    max_input_tokens: int = 512
    query_prefix: str = ""
    passage_prefix: str = ""
    tokenizer_file: str = "tokenizer.json"
    notes: str = ""

    @property
    def model_id(self) -> str:
        """指纹 id（卡内 §A）：``local:<model_slug>``。"""
        return f"local:{self.slug}"

    @property
    def repo_file(self) -> str:
        """人类可读的模型文件坐标（错误信息用）。"""
        return f"{self.repo_id}/{self.onnx_file}"


@dataclass(frozen=True, slots=True)
class ApiModelSpec:
    """OpenAI-compatible API 模型条目。

    未登记的模型名允许通过 ``dim`` 显式声明（见 factory），但必须显式给维度：
    猜维度会让 ``profile.dim`` 失真，进而让 D-07 失效判定失效。
    """

    name: str
    dim: int
    max_input_tokens: int = 2048
    query_prefix: str = ""
    passage_prefix: str = ""
    notes: str = ""
    #: 请求体里发给 API 的 model 字段。默认与 ``name`` 相同；调用了厂商前缀做 key 时
    #: （如 key ``bge-m3`` / ``request_name`` ``BAAI/bge-m3``），这里写 provider 认的名字。
    #: **别名替换必须发生在这里**（发请求前），见 TASK-046 §A。
    request_name: str | None = None
    #: 提供该模型 tokenizer.json 的 HF 仓库 id（可选）。设了它，API 侧就能**精确**按 token
    #: 截断（TASK-046 §D）；拿不到时回落 UTF-8 字节数估计（安全上界）。
    tokenizer_repo_id: str | None = None
    #: （TASK-049）该模型所属的传输配置 id（见 ``API_TRANSPORTS``）。
    #: ``None`` 时按 ``base_url`` / 模型名推断，推断不出则用 ``generic``。
    transport: str | None = None
    #: （TASK-049）模型级批参数推荐值（三级回落的中间层：env > 模型 > 厂商 > 全局）。
    #: 实测依据见 TASK-049 §8.3；``None`` 表示不覆盖下层。
    batch_size: int | None = None
    batch_token_budget: int | None = None
    #: （TASK-049）并发度推荐值。``None`` → 用厂商级；免费档建议 1。
    concurrency: int | None = None
    #: （TASK-049）输出维度（Matryoshka 模型支持降维，如 Voyage 的 256/512/2048）。
    #: 降维能显著降低响应体量（实测提速 ~2×，见 TASK-049 §8.1），但会改变向量空间。
    output_dimension: int | None = None

    @property
    def api_model(self) -> str:
        """实际发给 API 的 model 字段（未设 ``request_name`` 时回落 ``name``）。"""
        return self.request_name or self.name

    @property
    def model_id(self) -> str:
        """指纹 id（卡内 §A）：``api:<model_name>``。

        刻意用**注册表 key**（``name``）而不是 ``request_name``：key 是「哪家的哪个模型」的
        稳定标识，``index_config`` 指纹据此区分 provider——同名模型换 provider 会改 key，
        从而触发 D-07 二级失效（重嵌），而改别名（同一模型的不同写法）不会无谓重嵌。
        """
        return f"api:{self.name}"


# 维度 / 截断值的来源核实（2026-09-10 查 HuggingFace config.json 与 tokenizer_config.json）：
# - intfloat/multilingual-e5-small：hidden_size=384，max_position_embeddings=512，
#   model_max_length=512
# - Snowflake/snowflake-arctic-embed-xs：hidden_size=384，max_position_embeddings=512
# - Xenova/bge-small-zh-v1.5：hidden_size=512，max_position_embeddings=512
# nova 侧 2048 token 假设见 Module/01 §2.4；本地候选的实际原生上限是 512，最终由 TASK-015 校准。
_LOCAL_CANDIDATES: tuple[LocalModelSpec, ...] = (
    LocalModelSpec(
        slug="multilingual-e5-small",
        repo_id="intfloat/multilingual-e5-small",
        onnx_file="onnx/model.onnx",
        dim=384,
        pooling="mean",
        query_prefix="query: ",
        passage_prefix="passage: ",
        notes="暂定默认；e5 系列必须带 query:/passage: 前缀，否则质量明显下降（卡内 §B）",
    ),
    LocalModelSpec(
        slug="bge-small-zh-v1.5",
        repo_id="Xenova/bge-small-zh-v1.5",
        onnx_file="onnx/model.onnx",
        dim=512,
        pooling="cls",
        notes="官方 BAAI 仓库未发布 ONNX 导出，这里指 transformers.js 镜像（含 tokenizer.json）",
    ),
    LocalModelSpec(
        slug="arctic-embed-xs",
        repo_id="Snowflake/snowflake-arctic-embed-xs",
        onnx_file="onnx/model.onnx",
        dim=384,
        pooling="cls",
        notes="英文对照基线（Background/04 §3：GitNexus 的本地 ONNX 先例，22M 参数 / 90MB）",
    ),
)

# HuggingFace 上另存的量化版本，供 TASK-015 对比耗时/显存时替换 onnx_file：
# - intfloat/multilingual-e5-small: onnx/model_qint8_avx512_vnni.onnx
# - Snowflake/snowflake-arctic-embed-xs: onnx/model_quantized.onnx
LOCAL_MODELS: dict[str, LocalModelSpec] = {spec.slug: spec for spec in _LOCAL_CANDIDATES}

#: 注册表 key → 别名集合。别名是「同一模型在别处（如官方文档）的写法」，
#: 解析后落到同一条目，因此**不改变 ``model_id``、也不触发重嵌**（TASK-046 §A）。
#: 硅基流动官方文档的 model 名带厂商前缀（``BAAI/bge-m3``），用户照抄即可命中这里。
API_MODEL_ALIASES: dict[str, str] = {
    "BAAI/bge-m3": "bge-m3",
    "Pro/BAAI/bge-m3": "bge-m3-pro",
}

#: 厂商传输表（TASK-049 §3.1）。数字全部来自本机实测，出处见 ``docs/tasks/TASK-049`` §8。
API_TRANSPORTS: dict[str, ApiTransportSpec] = {
    spec.provider: spec
    for spec in (
        ApiTransportSpec(
            provider="siliconflow",
            base_url="https://api.siliconflow.cn",
            max_batch_items=800,
            max_batch_tokens=75_000,
            safe_batch_items=256,
            error_detail_field="message",
            notes=(
                "实测：760 条成功 / 800 条 HTTP 500（条数硬上限）；"
                "总 token 上限实测 72K 可用，取 75K 作校验线；"
                "安全批大小按响应体量选 256（约 3.4 MB）；免费档并发建议 1"
            ),
        ),
        ApiTransportSpec(
            provider="voyage",
            base_url="https://api.voyageai.com",
            max_batch_items=1000,
            max_batch_tokens=320_000,
            safe_batch_items=500,
            supports_input_type=True,
            error_detail_field="detail",
            supports_base64=True,
            notes=(
                "实测：条数上限 1000；总 token 实测 1000×300 成功、×2000 失败；"
                "**安全批 500**：dim=1024 下 1000 条会得到 12.7 MB 响应，"
                "对端在传输中途断连（RemoteProtocolError）；"
                "并发实测 8 路饱和（~2.2 M tok/min），瓶颈是本地链路带宽而非配额"
            ),
        ),
        ApiTransportSpec(
            provider="openai",
            base_url="https://api.openai.com",
            max_batch_items=2048,
            max_batch_tokens=300_000,
            safe_batch_items=512,
            notes="未在本机实测（无 key）；保守取值，用户可用 env 覆盖",
        ),
        ApiTransportSpec(
            provider="generic",
            max_batch_items=256,
            max_batch_tokens=64_000,
            safe_batch_items=128,
            notes="未登记厂商的保守默认（宁小勿大：超限会直接报错或断连）",
        ),
    )
}

#: 默认厂商（未指定且无法推断时）。
DEFAULT_TRANSPORT = "generic"

API_MODELS: dict[str, ApiModelSpec] = {
    spec.name: spec
    for spec in (
        ApiModelSpec(
            name="bge-m3",
            dim=1024,
            max_input_tokens=8192,
            request_name="BAAI/bge-m3",
            tokenizer_repo_id="BAAI/bge-m3",
            transport="siliconflow",
            notes=(
                "硅基流动上的 model 名是厂商前缀全名 BAAI/bge-m3；裸名会被 API 拒绝"
                "（20012 Model does not exist）——API 侧统一发 request_name（TASK-046 §A）；"
                "上限经本机实测校准：8192 token 成功、8193 被拒（20015）"
            ),
        ),
        ApiModelSpec(
            name="bge-m3-pro",
            dim=1024,
            max_input_tokens=8192,
            request_name="Pro/BAAI/bge-m3",
            tokenizer_repo_id="BAAI/bge-m3",
            transport="siliconflow",
            notes=(
                "硅基流动付费档。实测与免费档**返回同一模型**（余弦 0.9999+），"
                "差别仅在配额（TPM 100 万起，按 VIP 递增）——"
                "但两档 model_id 不同，切换会触发 D-07 重嵌（TASK-049 §3 待解决）"
            ),
        ),
        ApiModelSpec(
            name="voyage-4-lite",
            dim=1024,
            max_input_tokens=32_000,
            transport="voyage",
            batch_size=500,
            batch_token_budget=300_000,
            concurrency=8,
            notes=(
                "Voyage 4 系列（lite/4/large/nano 共享同一向量空间，可混用不同档位）："
                "200M 免费 token，32K 上下文，$0.02/M token；"
                "实测冷启动 89.2s（vs bge-m3 230.9s），golden recall@5 0.690（vs 0.655）；"
                "context 长度与服务端自动截断均已实测（超 32K 输入不会 400）"
            ),
        ),
        ApiModelSpec(
            name="voyage-code-4",
            dim=1024,
            max_input_tokens=32_000,
            transport="voyage",
            batch_size=500,
            batch_token_budget=300_000,
            concurrency=8,
            notes="代码检索专用档（未实测；与 voyage-4-lite 共享向量空间）",
        ),
        ApiModelSpec(
            name="text-embedding-3-small", dim=1536, max_input_tokens=8191, transport="openai"
        ),
        ApiModelSpec(
            name="text-embedding-3-large", dim=3072, max_input_tokens=8191, transport="openai"
        ),
    )
}


def get_local_spec(slug: str) -> LocalModelSpec:
    """按 slug 取本地模型条目；未登记时给出可用候选清单（可读错误）。"""
    try:
        return LOCAL_MODELS[slug]
    except KeyError:
        known = ", ".join(sorted(LOCAL_MODELS))
        raise EmbeddingConfigError(f"未登记的本地模型 slug: {slug!r}；可用候选：{known}") from None


def find_api_spec(name: str) -> ApiModelSpec | None:
    """按模型名取 API 条目；未登记返回 ``None``（由 factory 决定是否要求显式 dim）。

    先查注册表 key，再查别名表（如 ``BAAI/bge-m3`` → ``bge-m3``），两者命中同一条目，
    因此换写法（别名 vs 裸名）不会改变 ``model_id``、不会触发无谓重嵌。
    """
    spec = API_MODELS.get(name)
    if spec is not None:
        return spec
    canonical = API_MODEL_ALIASES.get(name)
    return API_MODELS.get(canonical) if canonical else None


def get_transport(provider: str) -> ApiTransportSpec:
    """按 provider 名取传输配置；未登记时报可读错误并列出可用项（TASK-049）。"""
    try:
        return API_TRANSPORTS[provider]
    except KeyError:
        known = ", ".join(sorted(API_TRANSPORTS))
        raise EmbeddingConfigError(
            f"未登记的 embedding 厂商 {provider!r}；可用：{known}"
        ) from None


def resolve_transport(
    spec: ApiModelSpec | None, *, base_url: str | None = None, provider: str | None = None
) -> ApiTransportSpec:
    """确定生效的传输配置（TASK-049 §3.1）。

    优先级：显式 ``provider``（env ``EMBED_PROVIDER``）> 模型条目的 ``transport``
    > 按 ``base_url`` 推断 > ``generic``。

    按 ``base_url`` 推断是为了“用户只填了一个自建/新厂商地址”时仍能得到合理默认：
    域名里含 ``siliconflow`` / ``voyageai`` 时命中对应厂商，否则回落 ``generic``。
    """
    if provider:
        return get_transport(provider)
    if spec is not None and spec.transport:
        return get_transport(spec.transport)
    if base_url:
        lowered = base_url.lower()
        for name, transport in API_TRANSPORTS.items():
            if name != DEFAULT_TRANSPORT and transport.base_url and name in lowered:
                return transport
    return get_transport(DEFAULT_TRANSPORT)
