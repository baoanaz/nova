"""Rust 抽取器：符号 / fqn / 属性与文档并入 / impl 归属 / 边。"""

from __future__ import annotations

import pytest
from nova_core.parsing.rust import RustParser

SOURCE = '''use std::sync::Arc;

/// 熔断器配置。
#[derive(Debug, Clone)]
pub struct Config {
    pub threshold: u32,
}

pub trait Router: Send {
    fn route(&self, name: &str) -> bool;
}

pub struct Breaker {
    config: Config,
}

impl Breaker {
    /// 新建。
    pub fn new(config: Config) -> Self {
        Self { config }
    }

    fn check(&self) -> bool {
        helper(self.config.threshold) && self.config.ready()
    }
}

impl Router for Breaker {
    fn route(&self, name: &str) -> bool {
        util::lookup(name)
    }
}

fn helper(n: u32) -> bool {
    n > 0
}

mod tests {
    #[test]
    fn it_works() {
        assert!(super::helper(1));
    }
}
'''


@pytest.fixture(scope="module")
def parsed():
    return RustParser().parse("src/lib.rs", SOURCE)


def _by_fqn(parsed):
    return {symbol.fqn: symbol for symbol in parsed.symbols}


def test_symbols_and_kinds(parsed) -> None:
    symbols = _by_fqn(parsed)
    assert symbols["Config"].kind == "struct" and symbols["Config"].is_exported
    assert symbols["Router"].kind == "trait"
    assert symbols["Router::route"].kind == "method"
    assert symbols["Breaker::new"].kind == "method"
    assert symbols["Breaker::check"].kind == "method"
    assert symbols["helper"].kind == "function" and not symbols["helper"].is_exported
    assert symbols["tests"].kind == "namespace"
    assert symbols["tests::it_works"].kind == "function"


def test_impl_blocks_attach_methods_to_the_implementing_type(parsed) -> None:
    symbols = _by_fqn(parsed)
    # ``impl Router for Breaker`` 的方法归 Breaker，而不是 Router。
    assert "Breaker::route" in symbols
    assert "Breaker" in symbols and symbols["Breaker"].kind == "struct"


def test_attributes_and_doc_comments_are_part_of_the_symbol(parsed) -> None:
    symbols = _by_fqn(parsed)
    lines = SOURCE.splitlines()
    start = symbols["Config"].start_line
    assert lines[start - 1].startswith("/// 熔断器配置")
    assert symbols["Breaker::new"].start_line == start_of(lines, "/// 新建。")


def start_of(lines: list[str], text: str) -> int:
    return next(i for i, line in enumerate(lines, 1) if text in line)


def test_edges(parsed) -> None:
    edges = {(e.source_fqn, e.target_name, e.kind) for e in parsed.edges}
    assert ("src/lib.rs", "std::sync::Arc", "imports") in edges
    assert ("Breaker", "Router", "implements") in edges
    assert ("Breaker::check", "helper", "calls") in edges
    assert ("Breaker::check", "ready", "calls") in edges
    assert ("Breaker::route", "util::lookup", "calls") in edges
    assert ("Router", "Send", "extends") in edges


def test_syntax_error_does_not_raise() -> None:
    result = RustParser().parse("bad.rs", "fn ok() {}\nfn broken( {\n")
    assert result.language == "rust"
