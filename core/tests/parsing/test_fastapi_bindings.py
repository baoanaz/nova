import pytest
from nova_core.parsing.registry import get_parser


def parse(source):
    return get_parser("python").parse("routes.py", source)


@pytest.mark.parametrize("imports,construction", [
    ("from fastapi import FastAPI as Factory", "app = Factory()"),
    ("from fastapi import APIRouter", "app = APIRouter()"),
    ("import fastapi as api", "app = api.APIRouter()"),
])
def test_explicit_factory_route_is_only_weak_registration(imports, construction):
    parsed = parse(imports + "\n" + construction
                   + '\n@app.get("/health")\ndef handler(): return 1\n')
    edges = [e for e in parsed.edges if e.kind == "registers"]
    assert len(edges) == 1
    assert edges[0].source_fqn == "routes.py"
    assert edges[0].target_name == "handler"
    assert edges[0].line == 2
    assert edges[0].provenance == "synthesized"


@pytest.mark.parametrize("override", [
    "app = Other()", "app, other = Other(), 1", "del app", "(app := Other())",
    "for app in values: pass", "with ctx() as app: pass", "app.get = other",
    "if condition:\n    app = Other()",
    "try: pass\nexcept Error as app: pass",
])
def test_shadowed_instance_is_not_connected(override):
    parsed = parse('from fastapi import FastAPI\napp = FastAPI()\n' + override
                   + '\n@app.get("/health")\ndef handler(): return 1\n')
    assert not parsed.fallback
    assert not [e for e in parsed.edges if e.kind == "registers"]


@pytest.mark.parametrize("source", [
    'app = Other()\n@app.get("/p")\ndef handler(): return 1',
    'from other import FastAPI\napp = FastAPI()\n@app.get("/p")\ndef handler(): return 1',
    'from fastapi import FastAPI\nFastAPI = Other\napp = FastAPI()\n'
    '@app.get("/p")\ndef handler(): return 1',
    'from fastapi import FastAPI\n(FastAPI := Other)\napp = FastAPI()\n'
    '@app.get("/p")\ndef handler(): return 1',
])
def test_same_names_do_not_prove_framework_binding(source):
    assert not [e for e in parse(source).edges if e.kind == "registers"]


@pytest.mark.parametrize("suffix", [
    "def handler(): return 2", "handler = other", "(handler := other)",
])
def test_duplicate_or_reassigned_handler_is_not_associated(suffix):
    parsed = parse('from fastapi import FastAPI\napp = FastAPI()\n'
                   '@app.get("/p")\ndef handler(): return 1\n' + suffix)
    assert not [e for e in parsed.edges if e.kind == "registers"]


def test_dynamic_path_reports_boundary_not_registration():
    parsed = parse('from fastapi import FastAPI\napp = FastAPI()\n'
                   '@app.get(get_path())\ndef handler(): return 1')
    assert not [e for e in parsed.edges if e.kind == "registers"]
    assert any("route binding" in r.name for r in parsed.unresolved)
