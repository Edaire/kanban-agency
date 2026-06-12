import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_status_icons_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_status_icons_are_colored_and_status_text_is_not_inline():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.st-attention{color:#f59e0b}' in html
    assert '.st-blocked{color:#fb7185}' in html
    assert '.st-running{color:#38bdf8}' in html
    assert '.st-ready{color:#a78bfa}' in html
    assert '.st-review{color:#fbbf24}' in html
    assert '.st-done{color:#22c55e}' in html
    assert "function statusIcon(s,p)" in html
    assert "title=\"done\">✓</span>" in html
    assert "title=\"running\">●</span>" in html
    assert "title=\"blocked\">◆</span>" in html
    assert "title=\"review\">◐</span>" in html
    assert "function roleLabel(r){return `${esc(r.role||'session')}${paneRef(r.task_id)}`}" in html
    assert '<span class="root-state">${rootBadge(root)}</span>' in html
    assert '<span class="root-state">${esc(rootBadge(root))}</span>' not in html
    assert "${esc(displayStatus(r))}</span>${paneRef(r.task_id)}" not in html
