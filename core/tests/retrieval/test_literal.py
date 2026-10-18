

def test_error_message_sentence_and_quotes_become_strong_phrases() -> None:
    from nova_core.retrieval.literal import extract_literal_phrases

    # 整句报错原文（带 ' / 这类文案符号）→ 整句当强短语
    phrases = extract_literal_phrases("A path prefix must start with '/'")
    assert phrases[0].strong and phrases[0].text.startswith("A path prefix must start with")
    # 引号里的原文优先，不再拿整句
    quoted = extract_literal_phrases('报错 “Duplicate Operation ID” 在哪')
    assert [(p.text, p.strong) for p in quoted if p.strong] == [("Duplicate Operation ID", True)]
    # 不带标点的整句原文同样当强短语（走 FTS 短语查询，见 recall_literal）
    plain = extract_literal_phrases("Cannot include the same APIRouter instance into itself")
    assert plain[0].strong and plain[0].text.startswith("Cannot include")
    # 两三个词的短语不当整句
    assert all(" " not in p.text for p in extract_literal_phrases("retry backoff policy"))
