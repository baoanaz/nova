# langchain 基准题库（20 题）

> 靶场：`langchain` @ `41d3572`｜projectId `ca2050db0db5b1e2`｜索引：`/root/.nova/bench/voyage-4-lite-d1024`（复用，不重建）
> ⚠️ 出题时避开了**未进索引**的文件：`libs/core/langchain_core/runnables/base.py` 在索引时被跳过（体积/解析策略），
> 所以 Runnable/LCEL 相关题目只落在 `branch.py` / `fallbacks.py` / `retry.py` 等已索引文件上。
> 每题给出建议工具（`search` / `ask`）、人工核实的参考答案与依据路径；机器可跑版本见同目录 `langchain.jsonl`。

| ID | 建议工具 | 类别 | 问题 | 参考答案 | 依据 |
|---|---|---|---|---|---|
| LC-01 | `search` | path | In which file is the parser class that converts LLM output into a Pydantic model defined? | libs/core/langchain_core/output_parsers/pydantic.py 的 PydanticOutputParser（继承 JsonOutputParser，用模型的 JSON schema 生成格式说明并校验）。本题只问文件位置，因此同文件的类成员切片也应算文件级命中。 | `libs/core/langchain_core/output_parsers/pydantic.py` |
| LC-02 | `search` | symbol | RecursiveCharacterTextSplitter 默认按什么顺序尝试分隔符？ | 默认 separators = ["\n\n", "\n", " ", ""]，keep_separator=True，可选 is_separator_regex 把分隔符当正则；递归逐级尝试直到片段够小。 | `libs/text-splitters/langchain_text_splitters/character.py#RecursiveCharacterTextSplitter` |
| LC-03 | `search` | symbol | 按 token budget 裁剪 message list 的 public function 在哪个文件？ | libs/core/langchain_core/messages/utils.py 的 trim_messages。 | `libs/core/langchain_core/messages/utils.py#trim_messages` |
| LC-04 | `search` | symbol | In which file is the `ChatPromptTemplate.from_messages` class method implemented? | libs/core/langchain_core/prompts/chat.py 的 ChatPromptTemplate.from_messages。 | `libs/core/langchain_core/prompts/chat.py#ChatPromptTemplate` |
| LC-05 | `search` | symbol | 所有工具类的基类 BaseTool 定义在哪，它是什么类型的 Runnable？ | libs/core/langchain_core/tools/base.py 的 BaseTool，是 RunnableSerializable[str | dict | ToolCall, Any]（另见 BaseToolkit）。 | `libs/core/langchain_core/tools/base.py#BaseTool` |
| LC-06 | `search` | path | 把普通 function decorate 成 tool 的 `@tool` implementation 在哪个文件？ | libs/core/langchain_core/tools/convert.py（多个重载的 tool(...)，负责推断参数 schema 与 docstring 描述）。 | `libs/core/langchain_core/tools/convert.py#tool` |
| LC-07 | `ask` | behavior | 自己实现一个向量库（VectorStore 子类）时，抽象契约要求哪些方法？add_texts 和 add_documents 都要写吗？ | 抽象方法只有 `similarity_search` 与 classmethod `from_texts`，实例化子类必须实现它们。写入路径上 `add_texts` 与 `add_documents` 的基类实现会互相适配：子类至少覆盖其中一个才能实际写入，不必两个都写；两者都不覆盖时会抛 NotImplementedError。 | `libs/core/langchain_core/vectorstores/base.py#VectorStore` |
| LC-08 | `search` | path | langchain_core 自带的进程内向量库实现是哪个文件、哪个类？ | libs/core/langchain_core/vectorstores/in_memory.py 的 InMemoryVectorStore。 | `libs/core/langchain_core/vectorstores/in_memory.py#InMemoryVectorStore` |
| LC-09 | `search` | symbol | Which file defines the abstract LLM-response cache and its in-memory implementation? | libs/core/langchain_core/caches.py：BaseCache（抽象）与 InMemoryCache（内存实现）。InMemoryCache 可通过 `set_llm_cache` 配置，但不是自动启用的“默认缓存”。 | `libs/core/langchain_core/caches.py#BaseCache` |
| LC-10 | `ask` | behavior | `BaseRetriever` 和 `VectorStoreRetriever` 是什么 relationship？vector store 如何 convert 成 retriever？ | VectorStoreRetriever 是 BaseRetriever 的子类，内部持有 vectorstore 与 search_kwargs；向量库通过 VectorStore.as_retriever(**kwargs) 生成它。BaseRetriever 要求实现 _get_relevant_documents，可选提供原生异步的 _aget_relevant_documents。 | `libs/core/langchain_core/retrievers.py#BaseRetriever`<br>`libs/core/langchain_core/vectorstores/base.py#VectorStoreRetriever` |
| LC-11 | `search` | symbol | embedding 模型的抽象基类叫什么、在哪个文件？ | Embeddings（ABC），定义在 libs/core/langchain_core/embeddings/embeddings.py，要求实现 embed_documents / embed_query。 | `libs/core/langchain_core/embeddings/embeddings.py#Embeddings` |
| LC-12 | `search` | symbol | Document 类定义在哪，它继承自什么？ | libs/core/langchain_core/documents/base.py 的 Document(BaseMedia)（pdf/图片等内容可通过 BaseMedia 承载）。 | `libs/core/langchain_core/documents/base.py#Document` |
| LC-13 | `search` | symbol | 聊天模型的基类 BaseChatModel 在哪个文件，它的基类是什么？ | libs/core/langchain_core/language_models/chat_models.py 的 BaseChatModel(BaseLanguageModel[AIMessage], ABC)。 | `libs/core/langchain_core/language_models/chat_models.py#BaseChatModel` |
| LC-14 | `ask` | behavior | langchain_core.indexing 的 index() 是怎么判断「该跳过 / 该更新 / 该删除」的？cleanup 有哪几种模式？ | 文档内容与 metadata 经 key_encoder（默认 sha1）生成 id；record_manager.exists(id) 为真且 force_update=False 时跳过并刷新时间戳，为真且 force_update=True 时更新，否则新增。删除依据本轮开始时间：incremental 按本批 source id 边写边删旧记录；full 在全量结束后删所有旧记录；scoped_full 在结束后只删本轮出现过的 source id 范围内旧记录；None 不删。时间戳用于刷新与识别旧记录，不决定内容是否更新。 | `libs/core/langchain_core/indexing/api.py#index` |
| LC-15 | `search` | symbol | Where is `BaseTracer` defined, and which classes does it inherit from? | libs/core/langchain_core/tracers/base.py 的 BaseTracer(_TracerCore, BaseCallbackHandler, ABC)。 | `libs/core/langchain_core/tracers/base.py#BaseTracer` |
| LC-16 | `ask` | behavior | `RunnableWithFallbacks`、`RunnableRetry`、`RunnableBranch` 分别 solve 什么 problem？ | fallbacks.py：主 Runnable 抛指定异常时按顺序尝试备用（常用于换 provider）；retry.py：对失败的 Runnable 做重试，可配置异常类型、最大次数与指数退避；branch.py：按顺序测试条件，执行第一个为真的分支，否则走默认分支。 | `libs/core/langchain_core/runnables/fallbacks.py#RunnableWithFallbacks`<br>`libs/core/langchain_core/runnables/retry.py#RunnableRetry`<br>`libs/core/langchain_core/runnables/branch.py#RunnableBranch` |
| LC-17 | `search` | behavior | TextSplitter 的 split_text 和 create_documents 是什么关系？ | split_text 是抽象方法（子类实现真正的切分），create_documents 是基类默认实现：先对每段文本调用 split_text，再把结果包成 Document（可带 metadatas）。 | `libs/text-splitters/langchain_text_splitters/base.py#TextSplitter` |
| LC-18 | `ask` | behavior | ChatPromptTemplate.from_messages 接受哪些 message representation，如何放入单条模板变量或整段消息历史？ | 支持 BaseMessagePromptTemplate / BaseChatPromptTemplate、BaseMessage 实例、("role", "template") 元组、(message class, template) 元组，以及纯字符串（仅是 human message 的简写）。字符串模板里的 `{var}` 是单条消息内容变量；整段历史用 `MessagesPlaceholder("history")`，也可写 `("placeholder", "{history}")`，该简写创建 optional placeholder。 | `libs/core/langchain_core/prompts/chat.py#ChatPromptTemplate`<br>`libs/core/langchain_core/prompts/chat.py#MessagesPlaceholder` |
| LC-19 | `search` | path | 基于 `rank_bm25` 的 retriever implementation 放在哪个 file？ | libs/langchain/langchain_classic/retrievers/bm25.py（同目录另有 elastic_search_bm25.py）。 | `libs/langchain/langchain_classic/retrievers/bm25.py` |
| LC-20 | `search` | negative | langchain_core 内置的 SQLite FTS5 全文检索实现在哪个文件？ | 不存在：langchain_core 没有 SQLite/FTS5 全文检索实现（核心只提供向量库抽象与 BM25 等第三方后端集成）。 | （无：负例） |

## 统计

- 共 20 题：`search` 14 题、`ask` 5 题、负例 1 题；语言：中文 11｜英文 4｜中英混合 5。
