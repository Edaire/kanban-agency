from pathlib import Path


def test_recent_workset_limit_is_ten_roots():
    source = Path(__file__).resolve().parents[1] / 'core.py'
    text = source.read_text()
    assert 'function renderRecentWorkset(){const maxRecent=10;' in text
    assert 'done.slice(0,Math.max(0,maxRecent-unfinished.length))' in text
    assert 'latest ${maxRecent} roots' in text
    assert '5-unfinished.length' not in text
    assert 'latest 5 roots' not in text
