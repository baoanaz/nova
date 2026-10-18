

def test_error_message_sentence_and_quotes_become_strong_phrases() -> None:
    from nova_core.retrieval.literal import extract_literal_phrases

    # 整句报错原文（带 ' / 这类文案符号）→ 整句当强短语
    phrases = extract_literal_phrases("A path prefix must start with '/'")
    assert phrases[0].strong and phrases[0].text.startswith("A path prefix must start with")
    # 引号里的原文优先，不再拿整句
    quoted = extract_literal_phrases('报错 “Duplicate Operation ID” 在哪')
    assert [(p.text, p.strong) for p in quoted if p.strong] == [("Duplicate Operation ID", True)]
    # 普通关键词串 / 问句不触发整句扫描
    assert all(" " not in p.text for p in extract_literal_phrases(
        "include_router dependencies tags responses merge into add_api_route"))
    assert all(" " not in p.text for p in extract_literal_phrases(
        "where is the retry backoff implemented?"))
