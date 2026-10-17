## Relevant Context
### Code
#### Core
[E1] TokenService.refresh — src/auth/token_service.py:45-46
     reason: exact match "TokenService.refresh" · multi-signal match
     45 | def refresh(self):
     46 |     return self.store.rotate()
#### Related
[E2] TokenStore.rotate — src/store.py:210-212
     reason: keyword + semantic match
     210 | def rotate(self):
     211 |     return 'rotated'
[E3] AuthMiddleware.verify — src/mw.py:88-89
     reason: keyword + semantic match
     88 | def verify(self, request):
     89 |     return refresh()
### Flow
[F1] AuthMiddleware.verify → TokenService.refresh
### Docs
[E4] docs/design/auth.md > 认证 > Token Refresh（design）
     reason: keyword match · key document
     30 | 刷新流程说明
     31 | 
     32 | 使用 refresh_token
     ⚠ 引用了已删除符号 LegacyToken.rotate，文档可能过时
### Missing Evidence
- [index_stale] 索引落后于工作区：1 个文件已变更但未重索引，证据可能不是当前代码。
- [unresolved_reference] 2 个符号引用无法解析（unresolved_refs status=failed：引用名为表达式或变量，无稳定符号名），涉及这些符号的调用关系可能缺失。
- [stale_doc_reference] (LegacyToken.rotate) 文档 docs/design/auth.md 引用了已删除或改名的符号（LegacyToken.rotate），该文档可能已过时；需要以代码为准并更新文档。
### Meta
confidence: high | index: stale (1 files) | budget: 705/10.0K
