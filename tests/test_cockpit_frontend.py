import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_frontend_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_initial_render_waits_for_sessions_data_and_uses_readonly_ttyd():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'let panesRenderedWithData=false' in html
    assert 'if(!panesRenderedWithData){renderPanes(); panesRenderedWithData=true;}' in html
    assert "renderPanes();setInterval(refresh" not in html
    assert 'r.ttyd_url||r.url' in html
    assert '/view/${r.task_id}' not in html


def test_cockpit_has_fixed_dimensions_to_avoid_fit_jitter():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'grid-template-rows:40px minmax(0,1fr)' in html
    assert 'grid-template-rows:32px minmax(0,1fr)' in html
    assert 'scrollbar-gutter:stable' in html
    assert 'flex-wrap:nowrap' in html


def test_cockpit_does_not_rewrite_dom_when_content_is_unchanged():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'let lastSideHtml' in html
    assert 'if(html===lastSideHtml)return' in html
    assert 'h.dataset.last!==next' in html
    assert 'document.title!==nextTitle' in html
