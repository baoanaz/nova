"""ContextPack 组装（TASK-012 §A）：预算内、结构化、可引用、诚实标注缺口。

设计依据：``docs/design/Module/03-上下文组装.md`` 全篇（D-21 双层合同 / D-22 装填 / D-23 预算）：

```text
输入：候选（已 rerank） + flows + freshness + 索引信号
预算：hardCap（Fast 默认 10K，可配 8-12K；Deep 12K） − 框架开销 ≈500 token
装填：分数降序贪心；单文件 ≤25%；tier3 配额 ≤30%；spec 保底 ≥1；code 保底 ≥2（R21）；
      spec 份额 ≤docs_ratio × 内容预算（R21，仅当池中存在代码候选时生效）
去重三招：相邻区间合并（行距 ≤10）/ 同符号聚合 / skeleton 降级（>300 行且超预算）
tier 不作为排序键（D-17）：只做配额与资格线
```

编号：evidence 与 docs **共用 E 编号空间**（占用顺序 = 装填顺序），flows 用 F（D-21）。

实现口径（本卡冻结，记录于任务卡"执行记录"）：

- **内容来源**：``store`` 提供切片正文；``EvidenceItem.content`` 是**带行号原文**
  （``45 | def refresh(...)``，CF-03 定义），渲染层直接输出；
- **spec 保底与 code 保底（R21）**：spec 保底在贪心前**预占**最高分 spec 候选（存在相关 spec 时），
  保证不被预算挤掉；code 保底在贪心后按候选序**补入**（池中存在代码候选而包内代码证据不足
  `code_floor` 块时）。两条路径与贪心循环共用 `placed_ids`（**按 chunk_id** 判重，R15，
  不再靠 `_Slot` 值比较）；预占块若已被贪心循环装填，预留预算立即**归还**（后续候选恢复完整硬顶）。
  code 保底不用"预占收窄硬顶"——实测它会覆盖 tier3/聚合/降级规则并让最高分证据掉到包尾（见
  TASK-021 执行记录），故只做"规则优先的贪心后补入"。
- **docs_ratio（R21，TASK-021）**：池中存在代码候选时，spec 证据总量 ≤ `docs_ratio × (hardCap −
  framework_overhead)`；超上限的 spec 候选计入 `omittedCount`（并置 `truncated`，不静默丢弃），
  `missingEvidence.retrieval_truncated` 的 message 里如实说明其中多少条因份额上限让位。
  池中确实没有代码候选时该上限不生效（纯文档问题不受影响）；且**不约束保底块**——
  `spec_floor` 的硬要求优先于份额上限（否则一条文档密集查询会一块 spec 都不剩）。
- **tier3 配额**按 §4.1 字面实现：``tier3_used + est > tier3_ratio × used`` 即跳过；
- **skeleton 降级**只在"超单文件上限或超硬预算"时触发（>300 行是前置条件）；
- **"命中行"**：RRF 只给 chunk 粒度 → 取**切片起始行**（符号定义行，即该切片锚点行）起
  ``context_lines`` 行，其余计入 ``elidedLines``；
- **token 估算** = 按字符类别加权（CJK 1.5 字符/token、其余 4 字符/token）；不引入 tokenizer
  （Module/03 §2 要点 3）。TASK-096 §A-2 把旧的 ``ceil(chars/4)`` 改为分类计价——中文下旧口径
  低估约 2.7 倍；纯 ASCII 文本结果不变。
- **预算账 = 渲染账（TASK-096 §A-1）**：装填用 ``_Slot.tokens`` = :func:`estimate_render_tokens`
  （header + reason + 行号缩进正文），不再只算 ``content``。修复前只算 content，而 header/reason
  占渲染输出约 12%，导致 ``max_tokens`` 实测超出 27-32%。`evidence_markdown_lines` 与
  ``render.py`` 同为一份格式（因循环 import 不能共用函数，由测试锁死一致性）。
- **片段化存储（TASK-017 / R12）**：``_Slot`` 按 ``(start, end, 原文)`` 片段列表存正文，
  合并按行号**有序插入**并去重重叠行；出口按片段行号升序编号拼接，省略区间**就地标注**
  （``... （省略 N 行）``）；``elidedLines`` = 声明区间行数 − Σ片段行数（真实省略行数）。
"""

from __future__ import annotations

import math
import os
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from nova_core.parsing.markdown import classify_doctype
from nova_core.storage import Store
from nova_core.types import (
    Budget,
    Candidate,
    ChunkDef,
    ContextPack,
    EvidenceItem,
    Flow,
    Freshness,
    MissingEvidence,
)

__all__ = [
    "ADJACENT_GAP_LINES",
    "CONSENSUS_SCORE_RATIO",
    "CONTEXT_SCORE_RATIO",
    "DEEP_BUDGET",
    "FAST_BUDGET",
    "MIN_CONSENSUS_FILES",
    "MODE_DEEP",
    "MODE_FAST",
    "BudgetConfig",
    "IndexSignals",
    "assemble",
    "budget_for",
    "collect_index_signals",
    "elision_note",
    "estimate_render_tokens",
    "estimate_tokens",
    "evidence_markdown_lines",
    "has_elision_note",
    "numbered_lines",
    "to_json",
]

MODE_FAST = "fast"
MODE_DEEP = "deep"

#: 相邻区间合并的行距阈值（Module/03 §3：同文件两 chunk 行距 ≤10 → 合并）。
ADJACENT_GAP_LINES = 10

#: 低于此 ``hard_cap`` 就不为二轮补检预留预算（TASK-MCP-BACKFILL-RESERVE）。
#:
#: 为什么需要这条下限（实测 service ``test_missing_evidence_retrieval_truncated``）：
#: 该用例用 ``maxTokens=600`` 跑，此时按 ``backfill_ratio`` 预留会把首轮压到装不下
#: 任何内容，于是产出与“预算裁剪”无关的 ``no_context_match``，把
#: ``missingEvidence == ["retrieval_truncated"]`` 的断言打翻。
#: 1500 的取法：补检目标（实测 ``_chain_model_call_handlers`` 降级后 320 token、
#: 未降级 1262）在此以下的预算里本来就装不下，预留没有意义。
BACKFILL_RESERVE_MIN_HARD_CAP = 1_500

#: TASK-095（用户 2026-09-14 拍板）：相对 top-1 的分数阈值。
#: 分数 ≥ ``top1_score × 本值`` 的候选才进装填循环（相对而非绝对：实测同仓库不同查询的
#: top-1 在 1.2~5.8 之间波动，绝对阈值无法通用；"明显弱于最佳命中"才是噪音的判据）。
#:
#: TASK-108 修正：该闸门必须保留（实测否决了“降级排序”方案）——真实查询的分数是长尾的，
#: 闸门之后的区域含有大量**与问题无关**的候选：tier3 图扩展邻居恒为固定分（不随相关性变化）、
#: 以及 0 分/负分的边缘命中。改成“不丢候选、预算允许就装”的结果是包被填满 10K token，
#: 但后半全是噪音（实测 E29 之后为 0.00 / -0.24 这类分值）。“装的都有用”比“装满”重要。
#:
#: 默认值由 0.50 调到 0.40（同一实测：search 题在 0.50/0.40 下都是最低 1.30、无噪音；
#: ask 题在 0.50 下只装 3 条代码证据（目标符号 InvocationRegistry 被挡），0.40 下装 10 条
#: 且仍无 0.70 噪音组）——比 0.50 更能覆盖“需要多条证据”的 ask 类问题，又不至于放进无关块。
#: **不要**用它去拟合 `benches/golden` 的 smoke 集（R29/R30 冻结）。
#: 覆盖方式：``assemble(config=replace(...))`` 或环境变量 ``NOVA_CONTEXT_SCORE_RATIO``
#: （**不暴露给 MCP 工具参数**——CF-06 冻结）。
CONTEXT_SCORE_RATIO = 0.40

_AGGREGATION_NOTE = "同符号聚合"
_MERGE_NOTE = "相邻区间合并"

_CPP_SOURCE_SUFFIXES = frozenset({
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".ipp",
    ".tpp",
})
_CPP_FUNCTION_KINDS = frozenset({"function", "method"})


#: CJK 区段（汉字 / 假名 / CJK 标点 / 全角形式）——这些字符在 BPE 词表里通常 1-2 字符 1 token，
#: 与英文的 ~4 字符/token 差距极大，必须分类计价（TASK-096 §A-2）。
#: 用**编译好的字符类**而不是逐字符区间比较：后者在热路径上是 O(字符数×区间数)，
#: 实测使全量套件从 62s 涨到 96s；C 实现的 ``findall`` 快约 40 倍。
_CJK_CHARS_RE = re.compile(
    "[\u3000-\u303f"   # CJK 标点（。、；：（）
    "\u3040-\u30ff"    # 平假名 + 片假名
    "\u3400-\u4dbf"    # CJK 扩展 A
    "\u4e00-\u9fff"    # CJK 基本区（汉字）
    "\uf900-\ufaff"    # CJK 兼容汉字
    "\uff00-\uffef]"   # 全角 ASCII 形式（，！？＝）
)

#: CJK 字符的 chars/token 折算（TASK-096 §A-2 的标定值）。
#: 卡内骨架给 1.5；实测项目自用的 e5 分词器下纯中文约 1.4-1.7 chars/token、cl100k 下约 1.0-1.2。
#: 取 1.5 作为中位保守值：既让中文查询/注释的预算按真实量级收缩，又不至于把英文代码高估。
_CJK_CHARS_PER_TOKEN = 1.5


def estimate_tokens(text: str) -> int:
    """按字符类别加权的 token 估算（Module/03 §2 要点 3：无 tokenizer 依赖，标注 approximate）。

    **TASK-096 §A-2 语义变更（既有调用方需知）**：旧实现是 ``ceil(len(text)/4)``，对 CJK 严重低估
    （中文约 1.5 字符/token，旧口径按 4 算 → 低估约 2.7 倍）。新口径按类别加权：
    CJK 字符按 :data:`_CJK_CHARS_PER_TOKEN`（1.5 字符/token），其余按 4 字符/token。
    纯英文/纯 ASCII 文本的结果与旧口径**完全一致**（只是把 ``len`` 拆成 ``cjk + other``，
    ``cjk=0`` 时退化为 ``ceil(len/4)``）；受影响的是**含中文**的文本。

    仍然是估算不是计数：用途是预算控制与分布观察，不是计费。真要精确计数由 provider 侧
    ``usage`` 返回（V1 不依赖它，Module/03 §2 要点 3 明确不引入 tokenizer 依赖）。
    """
    if not text:
        return 0
    cjk = len(_CJK_CHARS_RE.findall(text))
    other = len(text) - cjk
    return max(1, math.ceil(cjk / _CJK_CHARS_PER_TOKEN + other / 4))


def numbered_lines(content: str, start_line: int) -> str:
    """切片正文 → 带行号原文（``45 | def refresh(self):``）。"""
    lines = content.splitlines()
    return "\n".join(f"{start_line + offset} | {line}" for offset, line in enumerate(lines))


#: 省略区间标注（证据块正文与渲染层共用同一措辞；Module/03 §6）。
_ELISION_TEMPLATE = "... （省略 {count} 行）"
_ELISION_PREFIX = "... （省略"


def elision_note(count: int) -> str:
    """省略标注文本（``... （省略 9 行）``）。"""
    return _ELISION_TEMPLATE.format(count=count)


def has_elision_note(content: str) -> bool:
    """正文里是否已**就地**标注了省略区间（合并区间的间隙 / 降级尾部）。"""
    return any(line.strip().startswith(_ELISION_PREFIX) for line in content.splitlines())


#: 渲染层每条证据的缩进（与 ``render.py`` 的 ``_evidence_lines`` 一致：5 个空格）。
_EVIDENCE_INDENT = "     "


def evidence_markdown_lines(item: EvidenceItem) -> list[str]:
    """一条证据的 Markdown 行（**与 ``render.py`` 的 ``_evidence_lines`` 同一口径**）。

    本函数是预算计量的输入格式：TASK-096 §A-1 选定方案 A——``_Slot.tokens`` 必须等于该条证据
    渲染后的**全部**开销（header + reason + 行号前缀 + 缩进），而不只是 ``content``。
    修复前只算 ``content``（真实靶场占渲染输出的 86.8%），header（6.0%）+ reason（5.9%）+
    节标题等（1.4%）全部不记账，导致 ``max_tokens`` 实测超出 27-32%。

    为什么在 assembly 里再写一份而不是 import ``render._evidence_lines``：``render.py`` 已
    ``from .assembly import ...``（同一层），反向 import 会成环。因此按同一格式本地实现，
    并由 ``test_render_accounting_matches_rendered_evidence`` 断言与真实渲染输出逐字节一致
    （防格式漂移）；本卡因此**不需要**改动 ``render.py``（TASK-095 的领地）。
    """
    if item.type == "spec":
        doctype = f"（{item.doctype}）" if item.doctype else ""
        heading = f" > {item.heading_path}" if item.heading_path else ""
        header = f"[{item.id}] {item.path}{heading}{doctype}"
    else:
        target = item.symbol or item.path
        span = ""
        if item.lines is not None:
            span = f":{item.lines[0]}-{item.lines[1]}"
        header = f"[{item.id}] {target} — {item.path}{span}"
    lines = [header, f"{_EVIDENCE_INDENT}reason: {item.reason}"]
    lines.extend(f"{_EVIDENCE_INDENT}{line}" for line in item.content.splitlines())
    if item.elided_lines > 0 and not has_elision_note(item.content):
        lines.append(f"{_EVIDENCE_INDENT}{elision_note(item.elided_lines)}")
    if item.stale_refs:
        lines.append(
            f"{_EVIDENCE_INDENT}⚠ 引用了已删除符号 {', '.join(item.stale_refs)}，文档可能过时"
        )
    return lines


def estimate_render_tokens(item: EvidenceItem) -> int:
    """一条证据的**完整渲染开销**（:func:`evidence_markdown_lines` 的 token 数）。

    装填预算（``_Slot.tokens``）用本函数；``pack.budget.used_tokens`` 的不变量因此变成
    ``framework_overhead + Σ estimate_render_tokens(item)``（不再只累加 ``content``）。
    """
    return estimate_tokens("\n".join(evidence_markdown_lines(item)))


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """装填预算与配额（Module/03 §4.1/§4.2；D-23）。"""

    hard_cap: int = 14_000               # Fast 默认 14K（TASK-MCP-BUDGET：10K→14K）
    framework_overhead: int = 500        # query/freshness/missing 等元数据
    # A2：单文件 ≤25% hardCap。
    #
    # **锚定绝对值**（TASK-MCP-BUDGET，2026-09-16）：预算 10K→14K 后，若仍按比例算，
    # 单文件上限会从 2500 抬到 3500——一个文件就能吃掉近整包的三分之一，包体分布随默认值漂移。
    # 实测（cockpit-agents-server / “完整链路”题）：预算 10K→12K 时 ``process_with_streaming``
    # 才从 184 行截断转为完整 199 行——真正需要的是“该文件多装”，但“多装”应当由装填层
    # 按证据价值决定，而不是由预算比例顺带放大。
    # 故生产预设在 :data:`FAST_BUDGET` / :data:`DEEP_BUDGET` 里显式设 ``single_file_tokens``，
    # 新增预算一余给“更多文件”而不是“同一个文件更多行”。
    # 字段默认 0 = “不设绝对上限，按比例算”——既保证未显式设置的 ``BudgetConfig``
    # （测试与参数扫描脚本）行为不变，也让“关掉单文件上限”（ratio=1.0）的旧写法继续有效。
    single_file_tokens: int = 0          # A2：单文件绝对上限（token）；0 = 只用 ratio
    single_file_ratio: float = 0.25      # A2：单文件 ≤25% hardCap
    tier3_ratio: float = 0.30            # A3：tier3 配额 ≤30%
    spec_floor: int = 1                  # 存在相关 spec 时至少装 1-2 块
    # R21（TASK-021）：存在代码候选时，至少装 N 块代码证据（镜像 spec 保底）。
    code_floor: int = 2
    # R21（TASK-021）：存在代码候选时，spec 证据总量 ≤ docs_ratio × 内容预算
    # （hard_cap − framework_overhead）。池中确实没有代码候选时本项不生效（纯文档问题不受影响）。
    # 默认 0.10 由 TASK-021 的基线对照实测选定（见 benches/results/phase1-baseline.md 修复后复测），
    # 属 TASK-015 校准项：0.25 仅 2 条 R21 用例转 pass，0.15 为 3 条，0.10 为 4 条；而 0.05 会
    # 把文档密集的 spec 用例（aibox-0001，期望 4 条 doc）挤出 top-10。
    # 仅在 ``docs_tokens == 0`` 时作为回退口径使用（同 ``single_file_tokens`` 的理由）。
    docs_ratio: float = 0.10
    # R21 的**绝对值**形式（TASK-MCP-BUDGET）：
    #
    # 与 ``single_file_tokens`` 同一类问题——实测（cockpit-agents-py golden，同一索引同一题目）：
    # 预算 10K→16K 时，按比例的文档份额从 950 涨到 1550 token，多出的名额让
    # ``docs/internal-design.md`` / ``benchmarks/llm/datasets/README.md`` 这类
    # **分数高但不是目标**的长文档切片插到代码/测试证据前面：``cockpit-0037`` 的首个命中
    # 从 rank 3 滑到 rank 6（recall@5 0.861→0.833），而目标代码并未变差。
    # 文档份额的用途是**限制**文档挤占代码，不是“预算大了就多给文档”；
    # 故生产预设施加绝对值（950 = 旧 10K 口径 ``(10_000−500)×0.10``）。
    docs_tokens: int = 0
    skeleton_line_threshold: int = 300   # 超过此行数且超预算 → skeleton 降级
    skeleton_context_lines: int = 15     # 降级保留的上下文行数（±15）
    # TASK-109（G1/G2 补检）：第二轮的**独立小预算**（占 hard_cap 比例）。
    #
    # 为什么不能用主闸门：实测目标方法分 0.30，而主闸门是 ``top1 × 0.40``（该题 top1=1.9
    # → 闸门 0.76）——真实目标**结构性低于闸门**（它们是类里较小的行为方法，而闸门由
    # 文档/高频同名符号抬高）。但也不能因此绕过闸门（TASK-108 教训：不丢候选、预算允许就装
    # 的后果是包被 0.00/-0.24 的噪音填满）。
    #
    # 故用独立配额：补检候选只花这一份额，且**必须有确定性缺口依据**（gap.py 判定）
    # 才可能进入这一份额。0.25 与 ``single_file_ratio`` 同量级，含义是"二轮补检不得超过
    # 整个包的四分之一"——防止一个容器的全部成员淹没首轮证据（实测 cockpit-0035 的
    # ``CapabilityGateway`` 有 19 个成员、全量入包需 6598 token）。
    backfill_ratio: float = 0.35
    #: 补检候选的 skeleton 降级行数阈值（低于常驻的 300）：100+ 行的成员方法不是
    #: "某个行为怎么实现"的答案，而是一个大调度器——实测 ``_invoke`` 149 行 / 1996 token
    #: 会独吞整个补检配额（其余 13 个成员只能分剩下的 500 token）。降级后它只留签名 +
    #: 15 行上下文，配额才能落到真正的目标方法上。
    backfill_skeleton_lines: int = 100
    # 单个补检候选可占用的配额上限（占 ``backfill_ratio × hard_cap`` 的比例）。
    #
    # 实测矛盾（cockpit-0035）：即使已 skeleton 降级，``_invoke`` 仍占约 400 token，
    # 按池序装填时它 + ``_cache_and_finish`` 就把 2500 token 的配额吃完，
    # 三个真正目标（``_reserve_idempotency`` / ``_should_retry`` / ``_normalize``，
    # 均在池序 40+）拿不到任何预算。0.25 = 单成员不得超补检配额的四分之一，
    # 等价于“保证至少四个成员能上场”——补检是补断层，不是补某一个方法。
    backfill_single_ratio: float = 0.25
    # TASK-095：相对分数阈值——低于 ``top1_score × score_ratio`` 的候选**不装填**。
    # 只在贪心主循环生效；code_floor / spec_floor 的保底装填**不受它约束**
    # （保底是"至少给这些"，与"最多给到哪"不冲突，否则纯文档查询可能被清空）。
    # ``0.0`` = 显式关闭闸门（回到"贪心填满"），供机制类测试与修改前/后对照测量使用。
    score_ratio: float = CONTEXT_SCORE_RATIO


FAST_BUDGET = BudgetConfig(hard_cap=14_000, single_file_tokens=2_500, docs_tokens=950)

#: Deep 16K 硬顶（Module/03 §4.2 裁决口径；TASK-MCP-BUDGET 将 12K→16K）。
#:
#: **此前是死配置**：``Engine.search_with_trace`` 在两次 ``assemble`` 调用里都硬编码
#: ``mode=MODE_FAST``（该参数与 ``config`` 二选一，传了 ``config`` 后 ``mode`` 就是死参），
#: 因此 Deep 预算从未生效——实测 ``ask_project`` 走的就是 10K Fast 装填。
#: TASK-MCP-BUDGET 把 ``mode`` 贯通到 assemble，使本配置真正被使用。
DEEP_BUDGET = BudgetConfig(hard_cap=16_000, single_file_tokens=2_500, docs_tokens=950)

#: TASK-095：``score_ratio`` 的环境变量覆盖名（不暴露给 MCP 工具参数——CF-06 冻结）。
SCORE_RATIO_ENV = "NOVA_CONTEXT_SCORE_RATIO"


def _score_ratio_from_env(source: Mapping[str, str]) -> float:
    """``NOVA_CONTEXT_SCORE_RATIO`` → ``(0, 1]`` 的比例；缺省/非法一律回落默认值。

    与 ``IndexScope.from_env`` 同一纪律：一个打错的调试环境变量不该让检索起不来。
    允许 0.0——那是"关掉闸门"的显式写法（回到旧的贪心填满行为，便于对照测量）。
    """
    raw = source.get(SCORE_RATIO_ENV)
    if not raw:
        return CONTEXT_SCORE_RATIO
    try:
        parsed = float(raw)
    except ValueError:
        return CONTEXT_SCORE_RATIO
    if 0.0 <= parsed <= 1.0:
        return parsed
    return CONTEXT_SCORE_RATIO

#: spec 候选的 ``Candidate.kind``（其余值 <code|test|fallback> 一律算代码侧证据，R21）。
_SPEC_KIND = "spec"
#: 被 spec 份额上限挡下的候选在 missingEvidence 里的统一措辞（R21 §C 观测要求）。
_DOCS_RATIO_NOTE = "spec 份额上限"

# R22（TASK-022）：answerable 判定收紧参数（均为 TASK-015 校准项，数据见任务卡执行记录）。
#: 双通道共识必须覆盖的最少文件数（堵“同一长文档切片放大造成的伪共识”）。
MIN_CONSENSUS_FILES = 2
#: 共识最高分相对候选池分数中位数的最小倍数（堵“文档天然双通道命中”）。
CONSENSUS_SCORE_RATIO = 2.15


def budget_for(mode: str) -> BudgetConfig:
    base = DEEP_BUDGET if mode == MODE_DEEP else None
    if base is None:
        if mode != MODE_FAST:
            raise ValueError(f"mode 必须是 'fast' 或 'deep'，收到 {mode!r}")
        base = FAST_BUDGET
    ratio = _score_ratio_from_env(os.environ)
    return base if ratio == base.score_ratio else replace(base, score_ratio=ratio)


def _docs_cap(active: BudgetConfig, content_budget: int) -> int:
    """spec（文档）份额上限（R21）。

    TASK-MCP-BUDGET：``docs_tokens > 0`` 时取**绝对值**，不再随 ``hard_cap`` 缩放——
    实测同一索引同一题目下 10K→16K 会把文档份额从 950 抬到 1550，多出的名额插到代码/测试
    证据之前（``cockpit-0037`` 首个命中 rank 3→6）。为 0 时回退 ``docs_ratio × content_budget``。
    """
    if active.docs_tokens > 0:
        return active.docs_tokens
    return int(content_budget * active.docs_ratio)


def _single_file_cap(active: BudgetConfig) -> int:
    """单文件 token 上限（A2）。

    ``single_file_tokens > 0`` 时取该**绝对值**，不再随 ``hard_cap`` 缩放（TASK-MCP-BUDGET：
    避免预算从 10K 提到 14K 时把单文件上限一并抬到 3500，让新增预算全部给“更多文件”）；
    为 0（字段默认值）时回退旧口径 ``single_file_ratio × hard_cap``——这样未显式设置
    该字段的 ``BudgetConfig``（测试、``param_sweep``）行为**逐字不变**，
    ``single_file_ratio=1.0``（显式关闸）也继续有效。
    """
    if active.single_file_tokens > 0:
        return active.single_file_tokens
    return int(active.hard_cap * active.single_file_ratio)


@dataclass(frozen=True, slots=True)
class IndexSignals:
    """组装需要的索引侧信号（缺失即如实不报，不猜不造；A5）。

    ``unresolved_symbols``（TASK-096 §B-1 新增，附加字段不破契约）：无法解析的引用名
    （取 ``unresolved_refs.name_tail`` 中**标识符形态**的那些，按 id 序去重）。
    它有两个用处：① ``unresolved_reference`` 的 message 里如实列出"哪些符号"；
    ② ``next_queries`` 由缺口出发时从中取具体符号——旧口径从 ``pool[:3]`` 取符号，
    会建议去查一个测试函数（真实靶场实测）。
    """

    stale_doc_refs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unresolved_count: int = 0
    unresolved_symbols: tuple[str, ...] = ()


#: 可作为"符号名"的标识符形态；unresolved 的 reference_name 常是表达式
#: （``getattr(self._policy, name)``）而非符号，这类**不进** ``unresolved_symbols``。
_PLAIN_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

#: message 里最多列出的未解析符号名（余下用计数带过，避免 message 自身膨胀）。
_MAX_LISTED_UNRESOLVED = 3


def collect_index_signals(store: Store, candidates: Sequence[Candidate]) -> IndexSignals:
    """从 Store 收集 Phase 1 可得的缺失证据信号（G4 stale + unresolved_refs）。"""
    stale: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        if candidate.kind != "spec":
            continue
        refs = store.spec_refs_for_spec(candidate.chunk_id)
        names = tuple(_symbol_name_from_id(ref.symbol_id) for ref in refs if ref.stale)
        if names:
            stale[candidate.chunk_id] = names
    unresolved = store.unresolved_refs(status="failed")
    symbols: list[str] = []
    for row in unresolved:
        name = row.name_tail or row.reference_name
        if not _PLAIN_IDENTIFIER_RE.fullmatch(name) or name in symbols:
            continue
        symbols.append(name)
    return IndexSignals(
        stale_doc_refs=stale,
        unresolved_count=len(unresolved),
        unresolved_symbols=tuple(symbols),
    )


def _symbol_name_from_id(symbol_id: str) -> str:
    """``{path}:{fqn}:{start_line}`` → ``fqn``（C++ 的 ``A::b`` 也能正确取回）。"""
    _head, sep, tail = symbol_id.partition(":")
    if not sep:
        return symbol_id
    fqn, _sep, _line = tail.rpartition(":")
    return fqn or symbol_id


# --------------------------------------------------------------------------- 装填


@dataclass(slots=True)
class _Segment:
    """证据块内的一个连续行区间：``text`` 为该区间的**原始**正文（渲染时加行号）。

    真实 chunk 的 ``content`` 行数与 ``[start_line, end_line]`` 等长；测试夹具允许二者不等
    （符号声明区间与正文长度不一致），此时按行号裁剪原文可能取到空文本（不渲染，只记行账）。
    """

    start: int
    end: int
    text: str

    @property
    def line_count(self) -> int:
        """该区间覆盖的行数（按行号算，elidedLines 的账）。"""
        return self.end - self.start + 1


@dataclass(slots=True)
class _Slot:
    """已装填的一项（片段列表 + 声明末行 + token 账，供合并/降级用）。"""

    candidate: Candidate
    item: EvidenceItem
    base_reason: str
    segments: list[_Segment]
    #: 声明覆盖的末行（≥ 末个片段 end）：skeleton 降级只保留前 N 行时，尾部省略记在这里。
    elision_upper: int
    prelude: str = ""  # skeleton 降级的签名（带行号），置于首个片段之前
    aggregated: int = 0

    @property
    def span(self) -> tuple[int, int]:
        """片段包围盒（首片段 start, 末片段 end）。"""
        return self.segments[0].start, self.segments[-1].end

    @property
    def tokens(self) -> int:
        """该条证据的**完整渲染开销**（header + reason + 行号正文，TASK-096 §A-1 方案 A）。

        修复前只算 ``item.content``：header/reason 合计占渲染输出约 12%（真实靶场实测），
        ``max_tokens`` 因此系统性超出。现在与 :func:`evidence_markdown_lines` 同一口径，
        即"预算账 = 渲染输出"。
        """
        return estimate_render_tokens(self.item)


def _is_cpp_definition_chunk(chunk: ChunkDef) -> bool:
    """判断 C/C++ 函数切片是否包含函数体，而不是只含声明签名。"""
    suffix = "." + chunk.file_path.replace("\\", "/").rsplit(".", 1)[-1].lower()
    content = chunk.content.rstrip()
    return (
        chunk.symbol_kind in _CPP_FUNCTION_KINDS
        and suffix in _CPP_SOURCE_SUFFIXES
        and "{" in content
        and content.endswith("}")
    )


def _prefer_definition_representatives(
    store: Store, candidates: Sequence[Candidate]
) -> tuple[list[Candidate], int]:
    """同 FQN 同时有声明与定义时，选择可读的 C/C++ 函数体作为代表。

    C/C++ extractor 为声明和类外定义保留同一个 ``symbol_fqn``，而它们的检索分数可能不同。
    组装层原本按候选到达顺序聚合，容易把更短的 header 声明留在包内。这里只改变这一结构
    冲突的代表选择；无函数体定义的符号完全保持原有分数优先行为。
    """
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.symbol_fqn:
            groups.setdefault(candidate.symbol_fqn, []).append(candidate)

    if not groups:
        return list(candidates), 0

    chunks = {
        chunk.id: chunk
        for chunk in store.chunks_by_ids([candidate.chunk_id for candidate in candidates])
    }
    replacements: dict[str, Candidate] = {}
    aggregation_counts: dict[str, int] = {}
    for symbol, group in groups.items():
        if len(group) < 2:
            continue
        definition = None
        for candidate in group:
            chunk = chunks.get(candidate.chunk_id)
            if chunk is not None and _is_cpp_definition_chunk(chunk):
                definition = candidate
                break
        if definition is None or definition is group[0]:
            continue
        replacements[symbol] = definition
        aggregation_counts[symbol] = len(group) - 1
        note = f"{_AGGREGATION_NOTE}×{aggregation_counts[symbol]}"
        if note not in definition.reasons:
            definition.reasons.append(note)

    if not replacements:
        return list(candidates), 0

    selected: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        symbol = candidate.symbol_fqn
        if symbol not in replacements:
            selected.append(candidate)
            continue
        if symbol in seen:
            continue
        selected.append(replacements[symbol])
        seen.add(symbol)

    selected.sort(
        key=lambda candidate: (-candidate.score, -candidate.rrf_score, candidate.chunk_id)
    )
    return selected, sum(aggregation_counts.values())


def assemble(
    store: Store,
    query: str,
    candidates: Sequence[Candidate],
    *,
    flows: Sequence[Flow] = (),
    freshness: Freshness | None = None,
    mode: str = MODE_FAST,
    config: BudgetConfig | None = None,
    signals: IndexSignals | None = None,
    structural_result: bool = False,
    graph_boundary: bool = False,
    backfill: Sequence[tuple[Candidate, str]] = (),
    reserve_backfill: bool = False,
) -> ContextPack:
    """候选（已 rerank）→ 预算内的 ContextPack（CF-03 字段）。

    ``backfill``（TASK-109）：二轮补检候选 + 来源标注 ``(candidate, reason)``，按**优先级
    顺序**给出（调用方按候选池序给）。它们在主装填之后用一个**独立小预算**
    （``config.backfill_ratio``）补入，并受与主装填**同一套**上限约束（单文件、tier3、
    docs_ratio、hard_cap）——不绕过任何闸门（TASK-108 的教训，见 ``BudgetConfig`` 注释）。
    ``reason`` 写进证据的 ``reason`` 行，让 Agent 能看出"这条是二轮补来的"。

    ``reserve_backfill``（TASK-MCP-BACKFILL-RESERVE）：**首轮**装配时为主预算收窄出
    ``backfill_ratio × hard_cap`` 的余量，保证二轮补检有额度可用。由 engine 在
    首轮装配时传入（它已知道后面会跑 gap 检查）；二轮自己传 False。
    """
    active = config or budget_for(mode)
    single_file_cap = _single_file_cap(active)
    index_signals = signals if signals is not None else IndexSignals()
    fresh = freshness if freshness is not None else store.freshness()
    pool = sorted(candidates, key=lambda c: (-c.score, -c.rrf_score, c.chunk_id))
    pool, pre_aggregated = _prefer_definition_representatives(store, pool)

    # R21：内容预算与 spec 份额上限。池里**存在代码候选**时 docs_ratio 才生效
    # （纯文档问题——如 spec 类查询——不应被本机制伤害）。
    content_budget = max(active.hard_cap - active.framework_overhead, 0)
    has_code = any(candidate.kind != _SPEC_KIND for candidate in pool)
    docs_cap = _docs_cap(active, content_budget) if has_code else content_budget

    # TASK-095 §A：相对分数阈值（自适应查询难度）。“明显弱于最佳命中”才是噪音的判据——
    # 绝对阈值无法通用（实测同仓库不同查询的 top-1 在 1.2~5.8 之间波动）。
    #
    # TASK-108：语义从“闸内才装填”改为“闸内优先装填”——低于阈值的候选**降级排序**，
    # 不再被丢弃（见下方 ``pool`` 重排）。阈值仍决定哪些证据优先占预算。
    #
    # 参考分 top1 = **非 spec 候选（含 test）的最高 rerank 分**，不是池总分。两个理由：
    # ① spec 走 docs_ratio 配额这条独立路径，且其分数被 "high-value doctype" 等特征加成抬高；
    # 用池总分会让代码侧阈值被文档抬到 3.25（实测把 Runtime 那题塔到只剩 4 条）。
    # ② 与 TASK-095 §A-1 的实测表一致：以非 spec 最高分为 100% 时，四个真实查询在
    # ≥70%/≥50%/≥30% 三列上的条数为 2/11/24、1/1/5、7/18/24、16/36/49，与卡内表 12/12 逐项吻合
    # （卡内“总数”列 = 修改前贪心填满的证据条数 30/32/24/55）。
    # 池里没有任何非 spec 候选（纯文档查询）时闸门**关闭**（不被本机制伤害）。
    non_spec_scores = [candidate.score for candidate in pool if candidate.kind != _SPEC_KIND]
    top1_score = max(non_spec_scores, default=0.0)
    # ``score_ratio <= 0`` 或池里没有非 spec 候选 → 闸门关闭（score_floor=None）。
    score_floor = (
        top1_score * active.score_ratio
        if top1_score > 0.0 and active.score_ratio > 0.0
        else None
    )
    below_floor = 0

    used = active.framework_overhead
    omitted = pre_aggregated
    capacity_cut = 0
    tier3_used = 0
    spec_used = 0
    docs_capped = 0
    file_usage: dict[str, int] = {}
    symbol_slots: dict[str, _Slot] = {}
    slots: list[_Slot] = []

    #: 已装填 chunk_id（R15）：同一 chunk 只装一次——保底分支、贪心循环与合并路径共用。
    #: 修复前保底分支用 `reserved not in slots` 做 `_Slot` **值**比较：预留候选由贪心循环装下后，
    #: 只要该 slot 被 `_try_merge`/`_degrade` 就地改动，值就不再相等，保底分支会把同一 chunk
    #: 的原始 span 再装一次（真实复现：E1(173-232) 合并块 + E30(175-197) 预留块）。
    placed_ids: set[str] = set()

    def _place(candidate: Candidate, slot: _Slot, tokens: int) -> None:
        nonlocal used, tier3_used, spec_used
        used += tokens
        if candidate.kind == _SPEC_KIND:
            spec_used += tokens
        key = candidate.path or ""
        file_usage[key] = file_usage.get(key, 0) + tokens
        if candidate.tier == 3:
            tier3_used += tokens
        slots.append(slot)
        placed_ids.add(candidate.chunk_id)
        if candidate.symbol_fqn:
            symbol_slots.setdefault(candidate.symbol_fqn, slot)

    # spec 保底：为最高分 spec 候选**预留预算**（存在相关 spec 时），贪心后再补入——
    # 保留"装填顺序 = score 降序"的语义（Module/03 §4.1 的 spec 保底配额）。
    reserved: _Slot | None = None
    if active.spec_floor > 0:
        for candidate in pool:
            if candidate.kind != _SPEC_KIND:
                continue
            slot = _build_slot(store, candidate, index_signals)
            if slot is None:
                continue
            reserved = slot
            break
    reserved_id = reserved.candidate.chunk_id if reserved is not None else None

    # 补检份额预留（TASK-MCP-BACKFILL-RESERVE）：首轮贪心不得吃掉二轮补检的配额。
    #
    # 为什么必须预留（实测 langchain LC-21，2026-09-17）：首轮贪心把预算吃到只剩
    # 384~447 token，而补检目标 `_chain_model_call_handlers` 要 1262 token、
    # `_chain_tool_call_wrappers` 要 549 —— **一个都放不下**，19 个补检候选全部
    # 因 "预算不足" 被 break 掉。于是出现最反直觉的现象：把 ``backfill_ratio``
    # 从 0.35 调到 1.0、``max_tokens`` 从 10K 加到 30K，**补检命中数一个不变**
    # ——因为卡点不在补检自己的份额，而在首轮把余量吃光了。
    #
    # 补检候选带**确定性结构依据**（gap.py 判定的容器成员 / 文档引用），
    # 且是“调用链断裂”的唯一答案；首轮的可比候选（同分 tier3 邻居、测试同名符号）
    # 没有这种依据。故从首轮收窄出 ``backfill_ratio × hard_cap`` 的余量，
    # 与 spec 保底预留同一套语义（预留而非切走：若无补检候选，预算照样全给首轮）。
    #
    # 注意“预留”与“独立预算”的区别：``backfill_ratio`` 一直存在，但它只管**上限**
    # （补检最多用这么多），不保证**有余额可用**——本处补的正是这一环。
    #
    # 但预留**不能把首轮挤空**（实测踩到）：极小预算（如 service 的
    # ``test_missing_evidence_retrieval_truncated`` 用 ``maxTokens=600``）下，
    # 按比例预留会把首轮压到无内容可装，于是产出与“预算裁剪”无关的
    # ``no_context_match``，把该用例的 ``missingEvidence`` 断言打翻。故加两道约束：
    #   ① 预留量不超 ``hard_cap / 2``（首轮至少留一半）；
    #   ② 预算太小（< :data:`BACKFILL_RESERVE_MIN_HARD_CAP`）就不预留——
    #      那种规模下补检候选（数百 token 起）本来也装不下。
    backfill_reserve = 0
    if reserve_backfill and active.hard_cap >= BACKFILL_RESERVE_MIN_HARD_CAP:
        backfill_reserve = min(
            int(active.hard_cap * active.backfill_ratio), active.hard_cap // 2
        )

    def _hard_limit() -> int:
        """预留期间收窄的硬顶；预留块被装填后即归还预留预算（R15，不再挤压其它候选）。"""
        pending = reserved_id is not None and reserved_id not in placed_ids
        reserve_tokens = reserved.tokens if pending and reserved is not None else 0
        return max(
            active.hard_cap - reserve_tokens - backfill_reserve,
            active.framework_overhead,
        )

    for candidate in pool:
        if candidate.chunk_id in placed_ids:
            omitted += 1  # 同一 chunk 只装一次（与 _place / 保底分支共用同一判重集合）
            continue
        # TASK-095 §A：低于相对分数阈值的候选不装填（池已按分数降序，越过阈值即可停）。
        # 先于 `_build_slot` 判定：被分数截掉的候选根本不需要读切片（省 IO，语义更清晰）。
        # 保底（code_floor / spec_floor）不走本闸门：保底是“至少给这些”，与“最多给到哪”不冲突。
        #
        # TASK-108 试过把本闸门改成“降级排序”（不丢候选、预算允许就装）——**实测否决**：
        # 真实查询里分数是长尾的，闸后剩下的区域含有大量**与问题无关**的候选
        # （tier3 图扩展邻居恒为固定分 0.70，以及 0 分/负分的边缘命中），
        # 结果是包被填满 10K token，但 E29 之后全是 0.00/-0.24 这类噪音。
        # “装不装满”不是目标，“装的都有用”才是；阈值的作用必须保留。
        if score_floor is not None and candidate.score < score_floor:
            below_floor += 1
            omitted += 1
            break
        slot = _build_slot(store, candidate, index_signals)
        if slot is None:
            omitted += 1
            continue

        existing = symbol_slots.get(candidate.symbol_fqn) if candidate.symbol_fqn else None
        if existing is not None and existing is not slot:
            omitted += 1  # 同符号聚合：留最高分，其余计入 omittedCount 并在 reason 注明
            existing.aggregated += 1
            # reason 文本变长（"+ 同符号聚合×N"）→ 渲染开销随之增加，必须补记进预算
            # （TASK-096 §A-1：账目跟的是渲染输出，不是 content）。
            before_tokens = existing.tokens
            existing.item.reason = (
                f"{existing.base_reason} + {_AGGREGATION_NOTE}×{existing.aggregated}"
            )
            delta = existing.tokens - before_tokens
            used += delta
            if existing.item.type == "spec":
                spec_used += delta
            key = existing.candidate.path or ""
            file_usage[key] = file_usage.get(key, 0) + delta
            continue

        tokens = slot.tokens
        file_key = candidate.path or ""
        hard_limit = _hard_limit()
        over_file_cap = file_usage.get(file_key, 0) + tokens > single_file_cap
        if over_file_cap or used + tokens > hard_limit:
            degraded = _degrade(candidate, slot, active, store)
            if degraded is not None:
                tokens = degraded.tokens

        if file_usage.get(file_key, 0) + tokens > single_file_cap:
            omitted += 1
            capacity_cut += 1
            continue
        if candidate.tier == 3 and tier3_used + tokens > active.tier3_ratio * used:
            omitted += 1
            capacity_cut += 1
            continue
        if (
            has_code
            and candidate.kind == _SPEC_KIND
            and candidate.chunk_id != reserved_id  # 保底块不受份额上限约束（§4.1 硬要求）
            and spec_used + tokens > docs_cap
        ):
            # R21：spec 份额超上限 → 让位（不静默丢弃：计 omittedCount 并置 truncated）。
            # 这里用 continue 而非 break——池里更靠后的代码候选仍应有机会装填。
            omitted += 1
            docs_capped += 1
            capacity_cut += 1
            continue
        if used + tokens > hard_limit:
            omitted += 1
            capacity_cut += 1
            break

        merged = _try_merge(slots, slot, tokens)
        if merged is not None:
            delta = merged
            used += delta
            if slot.item.type == "spec":
                spec_used += delta
            file_usage[file_key] = file_usage.get(file_key, 0) + delta
            placed_ids.add(candidate.chunk_id)  # 已并入既有块：同一 chunk 不再单独装填
            continue
        _place(candidate, slot, tokens)

    # 保底补入：仅在预留块**尚未**被装填时执行（按 chunk_id 判重，R15）。只受 hard_cap 约束——
    # 保底块优先于 docs_ratio（spec 保底是 Module/03 §4.1 的硬要求，否则文档密集查询会一块都不剩）。
    if (
        reserved is not None
        and reserved.candidate.chunk_id not in placed_ids
        and used + reserved.tokens <= active.hard_cap
    ):
        _place(reserved.candidate, reserved, reserved.tokens)

    # 代码保底补入（R21）：池中存在代码候选、但包内代码证据不足 code_floor 块时，用剩余预算
    # 按候选序补入。去重/同符号聚合/tier3 配额/单文件上限/硬预算规则优先，不为凑数覆盖它们。
    if has_code and active.code_floor > 0:
        code_placed = sum(1 for slot in slots if slot.item.type != "spec")
        for candidate in pool:
            if code_placed >= active.code_floor:
                break
            if candidate.kind == _SPEC_KIND or candidate.chunk_id in placed_ids:
                continue
            slot = _build_slot(store, candidate, index_signals)
            if slot is None:
                continue
            existing = symbol_slots.get(candidate.symbol_fqn) if candidate.symbol_fqn else None
            if existing is not None and existing is not slot:
                continue  # 同符号聚合优先（不覆盖 R15）
            tokens = slot.tokens
            file_key = candidate.path or ""
            over_file_cap = file_usage.get(file_key, 0) + tokens > single_file_cap
            # 用 ``_hard_limit()``（含补检预留）而不是 ``active.hard_cap``：
            # 用裸 hard_cap 会让保底分支吃掉为二轮补检留的余量——
            # 实测 LC-21：保底先把 used 冲到 11344，补检目标（320 token）
            # 因 ``over_budget`` 差 115 token 被丢弃。
            if over_file_cap or used + tokens > active.hard_cap:
                degraded = _degrade(candidate, slot, active, store)
                if degraded is not None:
                    tokens = degraded.tokens
            if file_usage.get(file_key, 0) + tokens > single_file_cap:
                continue
            if candidate.tier == 3 and tier3_used + tokens > active.tier3_ratio * used:
                continue
            if used + tokens > active.hard_cap:
                continue
            merged = _try_merge(slots, slot, tokens)
            if merged is not None:
                used += merged
                file_usage[file_key] = file_usage.get(file_key, 0) + merged
                placed_ids.add(candidate.chunk_id)
                code_placed += 1  # 已完成并入既有块：内容已在包内
                continue
            _place(candidate, slot, tokens)
            code_placed += 1

    # ---- 二轮补检装填（TASK-109）：独立小预算，不绕主闸门 --------------------------
    #
    # 位置：在首轮贪心与 code_floor 之后、``truncated`` 判定之前。用**同一套**上限
    # （单文件 / tier3 / docs_ratio / hard_cap），只额外受两个约束：
    #   ① 补检总额 ≤ ``backfill_ratio × hard_cap``（独立小预算，不挤压首轮证据的份额）；
    #   ② 补检候选 > ``backfill_skeleton_lines`` 行时按 skeleton 降级装填
    #      （大调度器不该独吞配额，见 BudgetConfig.backfill_skeleton_lines 注释）。
    #
    # **不绕过主闸门**的落法：这些候选本来就被相对分数闸门挡下（目标分 0.30 < 闸门 0.76），
    # 但它们带着 gap.py 的**确定性结构依据**（同容器成员 / 文档符号引用），
    # 与 TASK-108 否决的"不丢候选、预算允许就装"有本质区别——后者没有任何依据。
    backfill_used = 0
    backfill_placed = 0
    backfill_cap = int(active.hard_cap * active.backfill_ratio)
    backfill_single_cap = int(backfill_cap * active.backfill_single_ratio)
    base_flow_tokens = sum(
        estimate_tokens(node.symbol) + estimate_tokens(node.path)
        for flow in flows
        for node in flow.nodes
    )
    for candidate, reason in backfill:
        if base_flow_tokens + used + backfill_used >= active.hard_cap:
            break
        if backfill_used >= backfill_cap:
            break
        if not candidate.chunk_id or candidate.chunk_id in placed_ids:
            continue
        # 单成员公平上限（见 ``backfill_single_ratio`` 注释）：超了就跳过，让后面的成员有机会。
        # 因为补检候选已按池序给出，跳过大的调度器不会损失“最相关”的判定（池序不变）。
        #
        # **例外：本轮首个装得下的候选不受单成员上限约束**（TASK-MCP-BUDGET）。
        #
        # 为什么（实测 cockpit-agents-server，2026-09-16）：补检候选按优先级（池序）给出，
        # 排第一的就是最该补的那个。当它恰好是一个**长方法**（本次实测：
        # ``StreamingService.process_with_streaming`` 199 行 / 2016 token）时，
        # 单成员上限（16K × 0.35 × 0.25 = 1400 token）把它强制降级成“签名 + 前 15 行”——
        # 而它正是**能唯一回答该问题的那条证据**（完整正文含 memory_query / preference_updated /
        # complete 三个事件与知识检索调用点）。公平上限的目的是“防止一个大调度器独吞配额、
        # 让后面 13 个成员无处可放”（cockpit-0035 的 ``_invoke``），但那个场景里
        # 目标是**多个小成员**；当首要目标就是单个长方法时，让位换不来任何东西。
        # 故只对首个候选豁免：后面的候选仍按原来的公平上限排队（冲突时“最相关”优先）。
        first_backfill = backfill_placed == 0
        slot = _build_slot(store, candidate, index_signals)
        if slot is None:
            continue
        existing = symbol_slots.get(candidate.symbol_fqn) if candidate.symbol_fqn else None
        if existing is not None and existing is not slot:
            continue  # 同符号聚合：该符号已在包内，不必重复补
        if reason and reason not in slot.item.reason:
            slot.item.reason = f"{slot.item.reason} + {reason}" if slot.item.reason else reason
            slot.base_reason = slot.item.reason
        tokens = slot.tokens
        file_key = candidate.path or ""
        over_file_cap = file_usage.get(file_key, 0) + tokens > single_file_cap
        over_backfill = backfill_used + tokens > backfill_cap
        over_budget = base_flow_tokens + used + backfill_used + tokens > active.hard_cap
        over_single = tokens > backfill_single_cap and not first_backfill
        # **首位补检候选的降级豁免**（TASK-MCP-BUDGET）——但只在“完整正文真装得下”时生效。
        #
        # 豁免动机：补检池首位 = 优先级最高、最该补的那条。实测它是唯一能回答该问题的
        # 长方法（``process_with_streaming`` 199 行 / 2016 token）时，两道闸门会合力把它降级：
        #   ① 单成员公平上限（14K × 0.35 × 0.25 = 1225 token）；
        #   ② 单文件上限（2500 token）——同文件里已有 ``_build_memory_query_result`` /
        #      ``StreamingService`` 等条目，再加上它就超了。
        # 降级结果是“签名 + 前 15 行”，恰好丢掉 ``memory_query`` / ``preference_updated`` /
        # ``complete`` 三个事件与检索调用点——而那正是问题要问的东西。
        #
        # 为什么可以豁免（不是拆闸门）：首位候选仍受**自己的总额约束**
        # （``backfill_ratio`` = 35% of hard_cap）与硬预算约束，不可能失控；
        # 而两道配额闸门的原本目的是“防止大调度器挤掉**后面的多个小成员**”，
        # 当首位本身就是唯一答案、后面也没有同权重的小成员时，让位换不来任何东西。
        #
        # **但豁免不能变成丢弃**：若完整包真的装不下（破补检总额或硬预算），仍要降级求入包——
        # 实测踩到过：直接跳过降级会让这条证据**整个掉出包**（比截断更差）。
        exempt_first = first_backfill and not (over_backfill or over_budget)
        if (over_file_cap and not exempt_first) or over_backfill or over_budget or over_single:
            # 仅按**行数**降级不够（行数 ≠ token 数，见 ``_degrade`` 的 ``force`` 说明）：
            # 超过单成员 token 配额时一律强制降级，否则这些调用链目标会被直接跳过。
            degraded = _degrade(
                candidate,
                slot,
                active,
                store,
                backfill=True,
                force=over_single,
            )
            if degraded is not None:
                # reason 含降级签名，重写一次（_degrade 会重置 content，但不动 reason）
                if reason and reason not in slot.item.reason:
                    slot.item.reason = (
                        f"{slot.item.reason} + {reason}" if slot.item.reason else reason
                    )
                    slot.base_reason = slot.item.reason
                tokens = slot.tokens
        if not exempt_first and file_usage.get(file_key, 0) + tokens > single_file_cap:
            # 补检目标的单文件豁免（TASK-MCP-BACKFILL-RESERVE，实测驱动）。
            #
            # 为什么（实测 langchain LC-21，2026-09-17）：目标 ``_chain_model_call_handlers``
            # （降级后仅 320 token）被 ``single_file_cap``（2500）拦下，因为同文件的
            # ``create_agent``(474) + ``_build_commands``(585) + 已补入的
            # ``_make_model_to_model_edge`` / ``_chain_tool_call_wrappers`` 已占 2224。
            # 结果是：**同文件里先到的兄弟函数把唯一能回答该问题的实现挤出了包**。
            #
            # 为何可以豁免：单文件上限的目的（见 ``single_file_ratio``）是防止
            # “一个文件里的多个候选挤掉其它文件的证据”。补检是**最后一轮**，
            # 首轮证据已全部就位，不会再挤走任何东西；而且补检整体仍受
            # ``backfill_ratio`` 独立预算约束（``over_backfill`` 仍是硬闸），不会失控。
            # 仅在**已降级到单成员上限以内**时豁免（``tokens <= backfill_single_cap``）：
            # 完整正文仍会被拦，避免一个巨函数靠豁免独吞整个单文件配额。
            #
            # 注意用**降级后**的 ``tokens`` 重算 ``over_overfill``/``over_budget``：
            # 上面那两个变量是用降级**前**的 tokens 算的（用于判断要不要降级），
            # 降级后直接沿用会把已变小、已能装下的候选误判为超预算而丢弃。
            over_backfill_now = backfill_used + tokens > backfill_cap
            over_budget_now = (
                base_flow_tokens + used + backfill_used + tokens > active.hard_cap
            )
            if tokens > backfill_single_cap or over_backfill_now or over_budget_now:
                continue
        if tokens > backfill_single_cap and not first_backfill:
            continue  # 降级后仍超单成员上限 → 放弃（不与小成员抢配额）
        # **不重复受 tier3 配额约束**（TASK-109 的关键设计决定，实测驱动）：
        #
        # 补检目标几乎全是 tier=3——它们由图扩展发现（``CapabilityGateway._should_retry``
        # 是 ``._invoke`` 的 callee；``Runtime.admit`` 是 docs 锚点的 spec 引用）。
        # 而 tier3 配额（≤30%）在首轮就已被别的扩展邻居吃满，因此在补检循环里再卡一次
        # 会**恒真地**把目标挡下——实测 ``Runtime.admit`` / ``_should_retry`` / ``_normalize``
        # 三个目标全因此被跳过（它们已在计划里、已按顺序到达，就是装不进去）。
        #
        # 这不是“绕过闸门”（TASK-108 的教训）：tier3 配额管的是 *无依据的图扩散*，
        # 而补检候选带**确定性结构依据**（用户点名的容器的成员 / 包内文档引用的符号），
        # 且已经受自己的独立预算（``backfill_ratio``，默认 35% of hard_cap）约束——
        # 那就是这一类候选的配额。tier 值**不改**（仍是 3，如实反映来源），
        # 只是不再重复计一次配额。
        if (
            has_code
            and candidate.kind == _SPEC_KIND
            and spec_used + tokens > docs_cap
        ):
            continue
        if backfill_used + tokens > backfill_cap:
            continue
        if base_flow_tokens + used + backfill_used + tokens > active.hard_cap:
            continue
        merged = _try_merge(slots, slot, tokens)
        if merged is not None:
            used += merged
            backfill_used += merged
            if slot.item.type == "spec":
                spec_used += merged
            file_usage[file_key] = file_usage.get(file_key, 0) + merged
            placed_ids.add(candidate.chunk_id)
            backfill_placed += 1
            continue
        _place(candidate, slot, tokens)
        backfill_used += tokens
        backfill_placed += 1

    # ``truncated`` 只反映**真的被预算截断**（TASK-108：闸外候选已不再被丢弃，
    # 它们要么装进包、要么因预算耗尽被 capacity_cut 计数）。
    truncated = capacity_cut > 0
    flow_tokens = base_flow_tokens

    evidence: list[EvidenceItem] = []
    docs: list[EvidenceItem] = []
    for index, slot in enumerate(slots, start=1):
        slot.item.id = f"E{index}"
        (docs if slot.item.type == "spec" else evidence).append(slot.item)

    pack = ContextPack(
        query=query,
        mode=mode,
        answerable=False,
        confidence="low",
        freshness=fresh,
        evidence=evidence,
        docs=docs,
        flows=list(flows),
        missing_evidence=[],
        next_queries=[],
        budget=Budget(
            used_tokens=used + flow_tokens,
            hard_cap=active.hard_cap,
            truncated=truncated,
            omitted_count=omitted,
        ),
    )
    pack.answerable, pack.confidence = _assess(
        pool, evidence, docs, structural_result=structural_result, graph_boundary=graph_boundary
    )
    pack.missing_evidence = _missing_evidence(
        pack,
        fresh,
        index_signals,
        omitted=omitted,
        truncated=truncated,
        evidence_count=len(evidence) + len(docs),
        docs_capped=docs_capped,
        score_capped=below_floor,
        score_ratio=active.score_ratio,
    )
    pack.next_queries = _next_queries(
        pack.missing_evidence, evidence, docs, answerable=pack.answerable
    )
    return pack


def _build_slot(store: Store, candidate: Candidate, signals: IndexSignals) -> _Slot | None:
    chunk = store.chunk_by_id(candidate.chunk_id)
    if chunk is None:
        return None
    is_spec = candidate.kind == "spec"
    reason = " + ".join(candidate.reasons) or "rrf"
    item = EvidenceItem(
        id="E0",  # 装填顺序确定后统一编号
        type="spec" if is_spec else ("test" if candidate.kind == "test" else "code"),
        path=chunk.file_path,
        content="",  # 由 _sync 按片段列表统一生成（TASK-017）
        score=candidate.score,
        evidence_tier=candidate.tier,
        reason=reason,
        symbol=None if is_spec else chunk.symbol_fqn,
        heading_path=chunk.symbol_fqn if is_spec else None,
        doctype=classify_doctype(chunk.file_path) if is_spec else None,
        lines=(chunk.start_line, chunk.end_line),
        elided_lines=0,
        stale_refs=tuple(signals.stale_doc_refs.get(candidate.chunk_id, ())),
    )
    slot = _Slot(
        candidate=candidate,
        item=item,
        base_reason=reason,
        segments=[_Segment(chunk.start_line, chunk.end_line, chunk.content)],
        elision_upper=chunk.end_line,
    )
    _sync(slot)
    return slot


def _degrade(
    candidate: Candidate,
    slot: _Slot,
    config: BudgetConfig,
    store: Store,
    *,
    backfill: bool = False,
    force: bool = False,
) -> _Slot | None:
    """单 chunk > 行长阈值且超预算 → 签名 + 起始行起 N 行。

    ``backfill=True``（TASK-109）：用更低的 ``backfill_skeleton_lines`` 阈值。
    ``force=True``（TASK-109）：**跳过行长前置条件**，一律降级。

    为什么需要 ``force``（实测数据）：行数与 token 数并不成比例——
    ``Runtime.admit`` 仅 63 行但 971 token（密集的 docstring 与签名），
    ``CapabilityGateway._normalize`` 44 行 585 token。单成员配额（25% × 补检预算 ≈ 875 token）
    按行数判永远不触发，于是这些**必须入包的调用链目标**被 token 闸门直接跳过
    （实测：三个 G1/G2 目标全因此未进包）。调用方按 token 判定要不要降级，
    这里负责执行降级。
    """
    threshold = (
        config.backfill_skeleton_lines if backfill else config.skeleton_line_threshold
    )
    start, end = slot.span
    if not force and end - start + 1 <= threshold:
        return None
    chunk = store.chunk_by_id(candidate.chunk_id)
    if chunk is None:
        return None
    kept_end = min(start + config.skeleton_context_lines, end)
    head = chunk.content.splitlines()[: config.skeleton_context_lines + 1]
    slot.prelude = numbered_lines(chunk.signature, start) if chunk.signature else ""
    slot.segments = [_Segment(start, kept_end, "\n".join(head))]
    _sync(slot)
    return slot


def _try_merge(slots: list[_Slot], slot: _Slot, tokens: int) -> int | None:
    """相邻区间合并（去重第 1 招）：返回本次新增的 token 数，未合并返回 ``None``。

    合并后片段按行号升序排列（不再是“分数顺序拼接”），重叠行去重、相邻区间归并。
    """
    for existing in slots:
        if existing.item.path != slot.item.path or existing.item.lines is None:
            continue
        if (existing.item.type == "spec") != (slot.item.type == "spec"):
            continue
        if _segments_distance(existing.segments, slot.segments) > ADJACENT_GAP_LINES:
            continue
        before = existing.tokens
        _merge_segments(existing, slot.segments)
        existing.item.reason = f"{existing.base_reason} + {_MERGE_NOTE}"
        return existing.tokens - before
    return None


def _merge_segments(slot: _Slot, incoming: Sequence[_Segment]) -> None:
    """把 ``incoming`` 片段按行序并入 ``slot``（重叠行去重，相邻区间归并）。"""
    for segment in incoming:
        slot.segments.extend(_uncovered_pieces(segment, slot.segments))
    slot.segments.sort(key=lambda segment: segment.start)
    slot.segments = _coalesce(slot.segments)
    _sync(slot)


def _uncovered_pieces(segment: _Segment, existing: Sequence[_Segment]) -> list[_Segment]:
    """``segment`` 中未被 ``existing`` 覆盖的子区间（原文按行号裁剪，可能为空文本）。"""
    pieces = [(segment.start, segment.end)]
    for other in existing:
        remaining: list[tuple[int, int]] = []
        for start, end in pieces:
            if other.end < start or other.start > end:
                remaining.append((start, end))
                continue
            if other.start > start:
                remaining.append((start, other.start - 1))
            if other.end < end:
                remaining.append((other.end + 1, end))
        pieces = remaining
    return [_Segment(start, end, _slice_text(segment, start, end)) for start, end in pieces]


def _slice_text(segment: _Segment, start: int, end: int) -> str:
    """取 ``segment`` 原文中 ``[start, end]`` 行的子串（行号超出原文时返回已可用部分）。"""
    lines = segment.text.splitlines()
    low = max(start - segment.start, 0)
    return "\n".join(lines[low : end - segment.start + 1])


def _coalesce(segments: Sequence[_Segment]) -> list[_Segment]:
    """相邻（行距 0）片段归并为一个片段；区间合并语义不变（连续区间不拆块）。"""
    merged: list[_Segment] = []
    for segment in segments:
        if merged and segment.start <= merged[-1].end + 1:
            previous = merged[-1]
            text = "\n".join(part for part in (previous.text, segment.text) if part)
            merged[-1] = _Segment(previous.start, max(previous.end, segment.end), text)
            continue
        merged.append(segment)
    return merged


def _segments_distance(left: Sequence[_Segment], right: Sequence[_Segment]) -> int:
    """两组片段间的最小未覆盖行数（重叠/相邻 → 0）——合并阈值按最近片段算。"""
    return min(_gap_lines(a.start, a.end, b.start, b.end) for a in left for b in right)


def _sync(slot: _Slot) -> None:
    """片段列表 → ``item.content`` / ``item.lines`` / ``item.elided_lines``（唯一出口）。"""
    segments = slot.segments
    first, last = segments[0].start, segments[-1].end
    slot.elision_upper = max(slot.elision_upper, last)
    covered = sum(segment.line_count for segment in segments)
    slot.item.lines = (first, last)
    slot.item.elided_lines = max(slot.elision_upper - first + 1 - covered, 0)
    slot.item.content = _render_content(slot)


def _render_content(slot: _Slot) -> str:
    """按片段行号升序拼接带行号正文，省略区间**就地**标注（间隙与尾部）。"""
    parts: list[str] = []
    if slot.prelude:
        parts.append(slot.prelude)
    previous: _Segment | None = None
    for segment in slot.segments:
        if previous is not None:
            gap = segment.start - previous.end - 1
            if gap > 0:
                parts.append(elision_note(gap))
        if segment.text:
            parts.append(numbered_lines(segment.text, segment.start))
        previous = segment
    tail = slot.elision_upper - slot.segments[-1].end
    if tail > 0:
        parts.append(elision_note(tail))
    return "\n".join(parts)


def _gap_lines(old_start: int, old_end: int, start: int, end: int) -> int:
    """两个区间的未覆盖行数（重叠/相邻 → 0）。"""
    if start > old_end:
        return max(start - old_end - 1, 0)
    if old_start > end:
        return max(old_start - end - 1, 0)
    return 0


# --------------------------------------------------------------------------- 判定与缺口


def _is_explicit(candidate: Candidate) -> bool:
    return "exact" in candidate.channel_ranks or "explicit path" in candidate.reasons


def _is_inferred(candidate: Candidate) -> bool:
    """Inferred 符号命中（02 的 Exact-Inferred 通道，tier 1）。

    与 Explicit 同为**符号级**依据（查询词命中了仓库里真实存在的符号/类名），
    与"两份文档都含这个词"有本质区别，因此 R22/TASK-022 把它作为独立硬依据。
    """
    return "inferred" in candidate.channel_ranks or "inferred symbol" in candidate.reasons


def _consensus_count(candidates: Iterable[Candidate]) -> int:
    return sum(1 for candidate in candidates if len(candidate.channel_ranks) >= 2)


#: 可读的代码结构切片 kind（TASK-106）：有这些才说明索引里有真实实现证据。
#: ``Candidate.kind`` 已在 fusion 层按切片类型归一（CF-04 四级）：结构化代码切片
#: = ``code``（函数/方法/类/类型定义，含 class_skeleton），测试代码 = ``test``；
#: ``fallback``（文件前导/降级段）与 ``spec``（文档）只证明“仓库里出现过这个词”，
#: 不证明“这个能力存在”，因此**都不算**可读实现证据。
_STRUCTURED_CODE_KINDS = frozenset({"code", "test"})


def _has_structured_code(candidates: Iterable[Candidate]) -> bool:
    """共识候选里是否至少有一个结构化代码切片（TASK-106）。

    只看**双通道共识候选**：单通道命中不构成”被交叉印证“，不应作为可答依据。
    ``kind`` 已由 fusion 层按路径/符号判定写入，此处不重复推断。
    """
    return any(
        len(candidate.channel_ranks) >= 2 and candidate.kind in _STRUCTURED_CODE_KINDS
        for candidate in candidates
    )


def _consensus_files(candidates: Iterable[Candidate]) -> int:
    """双通道共识候选覆盖的**不同文件**数（R22/TASK-022）。

    同一份长文档切出的多个小节会在 BM25 与 Vector 上同时强命中（基线实测：6 个不同小节
    同时占据 top-8），于是"共识候选数"被切片数量放大——按文件去重后才能反映"多来源互相印证"。
    """
    return len(
        {
            candidate.path or candidate.chunk_id
            for candidate in candidates
            if len(candidate.channel_ranks) >= 2
        }
    )


def _consensus_peak(candidates: Iterable[Candidate]) -> float:
    """双通道共识候选里的最高 rerank 分（无共识候选 → 0.0）。"""
    return max(
        (candidate.score for candidate in candidates if len(candidate.channel_ranks) >= 2),
        default=0.0,
    )


def _is_corroborated_top(candidate: Candidate | None) -> bool:
    """池内最高分候选是否被 **≥20 通道**同时命中（"最强证据有交叉印证，不是单通道运气"）。"""
    return candidate is not None and len(candidate.channel_ranks) >= 2


def _assess(
    pool: Sequence[Candidate],
    evidence: Sequence[EvidenceItem],
    docs: Sequence[EvidenceItem],
    *,
    structural_result: bool,
    graph_boundary: bool,
) -> tuple[bool, str]:
    """answerable / confidence 确定性判定（Module/03 §4.4 + R22/TASK-022 收紧）。

    收紧口径（数据见 TASK-022 执行记录的候选规则对比表）：

    - **Explicit / Inferred 符号级命中仍是硬依据**（原规则的 Explicit + 新增 Inferred）；
    - `consensus >= 2` 改为三条**可交叉印证**的口径（任一成立即可）：
      ① 池内最高分候选被 ≥2 通道命中，且共识候选覆盖 ≥2 个不同文件（最强证据有交叉印证）；
      ② 共识候选覆盖 ≥2 个不同文件，且共识最高分 ≥ `CONSENSUS_SCORE_RATIO` × 候选池分数中位数
      （存在显著强于池中位的共识）——堵住"文档密集仓库里文档天然双通道命中"；
    - `answerable=False` 时 `confidence` 一律 `low`（不用中等把握掩盖不可回答）。
    - **共识必须含可读代码结构**（TASK-106）：双通道共识本身仍可能全部落在文档或
      文件前导段上。真实反例：问"仓库里 Qdrant 向量库的实现在哪"（该能力**不存在**），
      README 与 ``.env.example`` 同时被 BM25/Vector 命中 → 旧规则判可答。现在要求共识
      候选里至少有一个结构化代码切片（函数/方法/类等），把"只有文档和 import 前导段"堵住。
    """
    explicit_hits = sum(1 for candidate in pool if _is_explicit(candidate))
    inferred_hits = sum(1 for candidate in pool if _is_inferred(candidate))
    consensus = _consensus_count(pool)
    consensus_files = _consensus_files(pool)
    median = statistics.median([candidate.score for candidate in pool]) if pool else 0.0
    corroborated = consensus_files >= MIN_CONSENSUS_FILES and (
        _is_corroborated_top(pool[0] if pool else None)
        or (median > 0.0 and _consensus_peak(pool) >= CONSENSUS_SCORE_RATIO * median)
    )
    answerable = explicit_hits >= 1 or inferred_hits >= 1 or structural_result or corroborated
    if answerable and not structural_result and not _has_structured_code(pool):
        # 旧规则的全部依据都是“文本相似”，没有任何可读代码结构时不足以支撑回答
        # （文档 + import 前导段也会双通道共识）。显式符号命中仍视为强依据。
        answerable = explicit_hits >= 1 or inferred_hits >= 1 or structural_result

    spec_only = bool(docs) and not evidence
    if not answerable:
        return False, "low"
    if explicit_hits >= 1 and consensus >= 3 and not graph_boundary:
        confidence = "high"
    elif (consensus > 0 and explicit_hits == 0) or spec_only:
        confidence = "medium"
    else:
        confidence = "low"
    return answerable, confidence


def _missing_evidence(
    pack: ContextPack,
    freshness: Freshness,
    signals: IndexSignals,
    *,
    omitted: int,
    truncated: bool,
    evidence_count: int,
    docs_capped: int = 0,
    score_capped: int = 0,
    score_ratio: float = CONTEXT_SCORE_RATIO,
) -> list[MissingEvidence]:
    """Phase 1 可实现的缺失证据子集（Module/03 §5；graph_boundary/symbol_ambiguous 留接口）。"""
    missing: list[MissingEvidence] = []
    if freshness.stale_files:
        missing.append(
            MissingEvidence(
                code="index_stale",
                message=(
                    f"索引落后于工作区：{len(freshness.stale_files)} 个文件已变更但未重索引，"
                    "证据可能不是当前代码。"
                ),
            )
        )
    if freshness.indexing_files:
        missing.append(
            MissingEvidence(
                code="indexing_pending",
                message=(
                    f"{len(freshness.indexing_files)} 个文件仍在索引队列中，"
                    "这些文件暂时无法作为证据。"
                ),
            )
        )
    if signals.unresolved_count > 0:
        subject = signals.unresolved_symbols[0] if signals.unresolved_symbols else None
        missing.append(
            MissingEvidence(
                code="unresolved_reference",
                message=(
                    f"{signals.unresolved_count} 个符号引用无法解析"
                    f"（unresolved_refs status=failed："
                    f"{_listed_symbols(signals.unresolved_symbols)}），"
                    "涉及这些符号的调用关系可能缺失。"
                ),
                # symbol = 首个可用的未解析符号：既让 message 可读，也让 next_queries
                # 能"从缺口出发"取具体主体（TASK-096 §B-1）。
                symbol=subject,
            )
        )
    for item in pack.docs:
        if not item.stale_refs:
            continue
        names = ", ".join(item.stale_refs)
        missing.append(
            MissingEvidence(
                code="stale_doc_reference",
                message=(
                    f"文档 {item.path} 引用了已删除或改名的符号（{names}），该文档可能已过时；"
                    "需要以代码为准并更新文档。"
                ),
                symbol=item.stale_refs[0],
            )
        )
    if truncated or docs_capped or score_capped:
        # R21 §C + TASK-095 §A：份额上限 / 相对分数阈值挡下的候选在这里如实说明
        # （CF-03 无新增字段，message 为自由文本）。
        #
        # TASK-108：本节的触发条件**不能只跟 ``truncated``**——它现在只反映“真的被预算截断”，
        # 而闸门/份额挡下的候选另属一类。用 ``truncated or docs_capped or score_capped``
        # 才能保证两类缺口都如实报告（少任一项就是信息丢失，不是“干净”）。
        reasons: list[str] = []
        if docs_capped:
            reasons.append(f"{docs_capped} 个因 {_DOCS_RATIO_NOTE}让位给代码证据")
        if score_capped:
            reasons.append(
                f"{score_capped} 个因低于相对分数阈值（top1×{score_ratio:g}）未予装填"
            )
        share = f"（其中 {'；'.join(reasons)}）" if reasons else ""
        missing.append(
            MissingEvidence(
                code="retrieval_truncated",
                message=(
                    f"候选池被裁剪：省略 {omitted} 个候选{share}，"
                    "可能有相关但未展示的证据；可提高预算或收窄查询。"
                ),
            )
        )
    if evidence_count == 0:
        missing.append(
            MissingEvidence(
                code="no_context_match",
                message="没有命中任何可用证据：查询词与索引内容无共识匹配，建议换用具体符号名或路径。",
            )
        )
    return missing


def _listed_symbols(symbols: Sequence[str]) -> str:
    """message 里列出的未解析符号（``a, b, c 等``；无可用符号时退回计数说明）。"""
    if not symbols:
        return "引用名为表达式或变量，无稳定符号名"
    listed = ", ".join(symbols[:_MAX_LISTED_UNRESOLVED])
    if len(symbols) > _MAX_LISTED_UNRESOLVED:
        return f"{listed} 等"
    return listed


def _next_queries(
    missing: Sequence[MissingEvidence],
    evidence: Sequence[EvidenceItem],
    docs: Sequence[EvidenceItem],
    *,
    answerable: bool,
) -> list[str]:
    """确定性生成 ≤3 条自愈查询（禁止 LLM；Module/03 §5）。

    **TASK-096 §B 口径（用户拍板 2026-09-14）**：

    1. **从缺口出发**，不再从 ``pool[:3]`` 取符号。旧口径的生成源是池内前 3 条符号，
       于是 top-1 是测试函数时就会建议"查这个测试函数的调用方"（真实靶场实测）——
       建议查询与**实际缺什么**无关，是噪音；
    2. **只在 ``answerable=false`` 时生成**：证据已足够还建议"换个方式再问"同样是噪音，
       空列表时 ``render_markdown`` 自动不渲染该节（TASK-087 已实现，无需改 ``render.py``）。

    模板与缺口类型的对应见 :data:`_QUERY_TEMPLATES`；``retrieval_truncated`` 不生成
    （那是预算不够，改问也帮不上，该做的是提高预算）——它也是**唯一**会抑制兜底的缺口：
    只有预算裁剪时返回空列表（否则“又报 truncated、又建议查路径”就是旧行为的噪音）。
    缺口里取不到可用符号时的兜底保留原有"文件名/路径"路径。
    """
    if not answerable:
        queries: list[str] = []
        for item in missing:
            query = _query_for_gap(item)
            if query is not None and query not in queries:
                queries.append(query)
        if queries:
            return queries[:3]
        # 包被预算裁剪时**不**给兜底：此时正确的动作是提高预算，而不是换个问法（§B-1）。
        if any(item.code == "retrieval_truncated" for item in missing):
            return []
        # 缺口里构造不出具体符号（如只有 index_stale）：退回"用路径构造"的兜底。
        return _fallback_queries(evidence, docs)
    return []


#: 目标形态：``query = template.format(subject=...)``（缺口类型 → 自愈问法，Module/03 §5）。
_QUERY_TEMPLATES: Mapping[str, str] = {
    "unresolved_reference": "{subject} 的调用方有哪些",
    "stale_doc_reference": "{subject} 现在在哪里实现",
    "graph_boundary": "{subject} 的调用链完整路径",
    "symbol_ambiguous": "{subject} 到底指哪一个",
    "no_context_match": "{subject} 的实现细节",
}

#: ``symbol_ambiguous`` 的候选分隔符（``A 还是 B``）。
_AMBIGUOUS_SEPARATOR = " 还是 "

#: 可作为"符号主体"的标识符形态（用于剔除 ``getattr(x, y)`` 这类表达式形态的引用名）。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*")


def _query_for_gap(item: MissingEvidence) -> str | None:
    """缺口 → 一条自愈查询；无法构造（或多个候选）时返回 ``None``。"""
    template = _QUERY_TEMPLATES.get(item.code)
    if template is None:  # 含 retrieval_truncated / index_stale / indexing_pending
        return None
    symbols = _gap_symbols(item)
    if not symbols:
        return None
    if item.code == "symbol_ambiguous" and len(symbols) >= 2:
        subject = f"{symbols[0]}{_AMBIGUOUS_SEPARATOR}{symbols[1]}"
    else:
        subject = symbols[0]
    return template.format(subject=subject)


def _gap_symbols(item: MissingEvidence) -> list[str]:
    """从缺口里取出可用的符号名（确定、保序、去重）。

    - ``MissingEvidence.symbol`` 是结构化字段，优先：``stale_doc_reference``（stale 引用名）与
      ``unresolved_reference``（首个标识符形态的未解析引用）都走这条；
    - 仅**多义缺口**（``symbol_ambiguous``，Phase 1 未产出，两符号塞不进单值 ``symbol``）从
      message 的括号组里取 ``（A 还是 B）``——该格式由 :data:`_AMBIGUOUS_SEPARATOR` 定义；
    - 其余缺口（含未带符号的 ``unresolved_reference``）返回空 → 由调用方走路径兼底。
      不泛化解析 message：``unresolved_reference`` 的括号里还有 ``unresolved_refs`` 这类
      噪音词，泛解析会生成"去查 unresolved_refs 的调用方"这种垃圾查询。
    """
    if item.symbol:
        return [item.symbol]
    if item.code != "symbol_ambiguous":
        return []
    names: list[str] = []
    for group in re.findall(r"[\(（]([^\)）]*)[\)）]", item.message):
        for part in group.split(_AMBIGUOUS_SEPARATOR.strip()):
            for name in _IDENTIFIER_RE.findall(part):
                if name not in names:
                    names.append(name)
    return names


def _fallback_queries(
    evidence: Sequence[EvidenceItem], docs: Sequence[EvidenceItem]
) -> list[str]:
    """兜底：缺口里拿不到符号时，用包里已有证据的路径/标题构造（保留 TASK-087 前的行为）。"""
    queries: list[str] = []
    first_evidence = next(iter(evidence), None)
    if first_evidence is not None:
        queries.append(f"{first_evidence.path} 里还有哪些与查询相关的符号")
    spec = next((item for item in docs if item.heading_path), None)
    if spec is not None:
        queries.append(f"{spec.heading_path} 对应的实现代码在哪里")
    if not queries and evidence:
        queries.append(f"{evidence[0].path} 的实现细节")
    return queries[:3]


# --------------------------------------------------------------------------- 序列化


def to_json(pack: ContextPack) -> dict:
    """ContextPack → CF-03 JSON（键名与 ``docs/contracts/contextpack.schema.json`` 完全一致）。

    ``budget.usedTokens`` 的口径：**渲染账**——框架开销 + 各条证据的完整渲染开销
    （header + reason + 行号缩进正文，TASK-096 §A-1/§A-2）。它量的是"这个包实际占多少"，
    而不是"正文有多少"。消费方若自行复算，用 ``estimate_render_tokens``。
    """
    return {
        "query": pack.query,
        "mode": pack.mode,
        "answerable": pack.answerable,
        "confidence": pack.confidence,
        "freshness": _freshness_json(pack.freshness),
        "evidence": [_evidence_json(item) for item in pack.evidence],
        "docs": [_doc_json(item) for item in pack.docs],
        "flows": [_flow_json(flow) for flow in pack.flows],
        "missingEvidence": [
            {"code": item.code, "message": item.message, "symbol": item.symbol}
            for item in pack.missing_evidence
        ],
        "nextQueries": list(pack.next_queries),
        "budget": {
            "usedTokens": pack.budget.used_tokens if pack.budget else 0,
            "hardCap": pack.budget.hard_cap if pack.budget else 0,
            "truncated": pack.budget.truncated if pack.budget else False,
            "omittedCount": pack.budget.omitted_count if pack.budget else 0,
        },
    }


def _freshness_json(freshness: Freshness) -> dict:
    return {
        "indexedAt": freshness.indexed_at,
        "staleFiles": list(freshness.stale_files),
        "indexingFiles": list(freshness.indexing_files),
    }


def _evidence_json(item: EvidenceItem) -> dict:
    payload: dict = {
        "id": item.id,
        "type": item.type,
        "path": item.path,
        "content": item.content,
        "score": item.score,
        "evidenceTier": item.evidence_tier,
        "reason": item.reason,
        "elidedLines": item.elided_lines,
    }
    if item.symbol is not None:
        payload["symbol"] = item.symbol
    if item.lines is not None:
        payload["lines"] = [item.lines[0], item.lines[1]]
    return payload


def _doc_json(item: EvidenceItem) -> dict:
    payload: dict = {
        "id": item.id,
        "type": "spec",
        "path": item.path,
        "headingPath": item.heading_path or "",
        "doctype": item.doctype or classify_doctype(item.path),
        "content": item.content,
        "reason": item.reason,
    }
    if item.lines is not None:
        payload["lines"] = [item.lines[0], item.lines[1]]
    if item.stale_refs:
        payload["staleRefs"] = list(item.stale_refs)
    return payload


def _flow_json(flow: Flow) -> dict:
    return {
        "id": flow.id,
        "nodes": [
            {"symbol": node.symbol, "path": node.path, "line": node.line} for node in flow.nodes
        ],
        "truncated": flow.truncated,
    }
