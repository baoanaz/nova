"""Kotlin 抽取器：符号 / 嵌套 / 伴生对象 / 继承与接口边 / 导入。"""

from __future__ import annotations

import pytest
from nova_core.parsing.kotlin import KotlinParser

SOURCE = '''package a.b

import x.y.Z

/** 服务。 */
@Singleton
class Foo(val a: Int) : Base(), Iface {
    private val x = 1

    companion object {
        const val K = 2
        fun make() = Foo(1)
    }

    fun bar(q: String): Int {
        return baz(q) + this.qux() + a.b.c(1)
    }
}

interface Iface : Parent {
    fun m()
}

object Single {
    fun f() = 1
}

enum class E {
    A, B;
    fun e() {}
}

fun top(a: Int) = a
val tv: Int = 3
private fun hidden() {}
typealias T = List<Int>
fun String.ext() = 1
'''


@pytest.fixture(scope="module")
def parsed():
    return KotlinParser().parse("src/Foo.kt", SOURCE)


def _by_fqn(parsed):
    return {symbol.fqn: symbol for symbol in parsed.symbols}


def test_symbols_and_kinds(parsed) -> None:
    symbols = _by_fqn(parsed)
    assert symbols["Foo"].kind == "class" and symbols["Foo"].is_exported
    assert symbols["Iface"].kind == "interface"
    assert symbols["Single"].kind == "class"
    assert symbols["E"].kind == "enum"
    assert symbols["Foo.bar"].kind == "method" and not symbols["Foo.bar"].is_exported
    assert symbols["E.e"].kind == "method"
    assert symbols["top"].kind == "function" and symbols["top"].is_exported
    assert symbols["tv"].kind == "variable"
    assert symbols["T"].kind == "typedef"
    assert "ext" in symbols
    assert not symbols["hidden"].is_exported


def test_companion_members_belong_to_the_outer_class(parsed) -> None:
    symbols = _by_fqn(parsed)
    assert "Foo.make" in symbols and "Foo.K" in symbols
    assert "x" not in symbols and "Foo.x" not in symbols  # 类内普通属性不成符号


def test_kdoc_and_annotations_are_part_of_the_symbol(parsed) -> None:
    lines = SOURCE.splitlines()
    start = _by_fqn(parsed)["Foo"].start_line
    assert lines[start - 1].startswith("/** 服务。 */")


def test_edges(parsed) -> None:
    edges = {(e.source_fqn, e.target_name, e.kind) for e in parsed.edges}
    assert ("src/Foo.kt", "x.y.Z", "imports") in edges
    assert ("Foo", "Base", "extends") in edges
    assert ("Foo", "Iface", "implements") in edges
    assert ("Iface", "Parent", "implements") in edges
    assert ("Foo.bar", "baz", "calls") in edges
    assert ("Foo.bar", "qux", "calls") in edges
    assert ("Foo.bar", "a.b.c", "calls") in edges


def test_syntax_error_does_not_raise() -> None:
    result = KotlinParser().parse("Bad.kt", "fun ok() {}\nclass Broken( {\n")
    assert result.language == "kotlin"
