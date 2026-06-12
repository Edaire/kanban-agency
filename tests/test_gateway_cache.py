import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_gateway_cache_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_gateway_serves_cockpit_at_root_and_strict_no_cache():
    core = load_core()
    core.codex_web_gateway_stop()
    out = core.codex_web_gateway_start(port=0)
    script = Path(out['state']['script']).read_text()
    core.codex_web_gateway_stop()
    assert "if path == '/':" in script
    assert "core._cockpit_html('__all__', embed=False)" in script
    assert "if path == '/healthz':" in script
    assert "no-store, no-cache, must-revalidate, max-age=0" in script
    assert "self.send_header('pragma','no-cache')" in script
    assert "def _send_html" in script
