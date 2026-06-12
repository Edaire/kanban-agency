import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_now_workset_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recent_workset_is_client_only_and_drag_only():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "function renderRecentWorkset()" in html
    assert "Recent <span class=\"small\">activated here</span>" in html
    assert "function touchRecent(task)" in html
    assert "function seedRecentFromPanes()" in html
    assert "for(const task of panes){if(task&&!recentTasks[task])" in html
    assert "sessions=await r.json();seedRecentFromPanes();const att=" in html
    assert "recentTasks[task]=Math.floor(Date.now()/1000)" in html
    assert "localStorage.setItem(storageKey,JSON.stringify({layout,sideMode,panes,active,expanded:[...expandedRoots],collapsedRoots:[...collapsedRoots],collapsedKanbans:[...collapsedKanbans],recentTasks}))" in html
    assert "Object.entries(recentTasks||{})" in html
    assert "byId[id]&&Number(ts||0)>=cutoff" in html
    assert "sort((a,b)=>Number(b[1]||0)-Number(a[1]||0))" in html
    assert "slice(0,8)" in html
    assert "touchRecent(task);saveState();setActive(i);renderSide()" in html
    # No Now-specific fetch/open/complete side-effect path; it is just the normal draggable data-task chip.
    assert "recent-workset" in html
    assert "data-task=\"${esc(r.task_id||'')}\"" in html
