from pathlib import Path


def test_prompt_paste_uses_raw_lf_not_carriage_return_submit_per_line():
    source = Path(__file__).resolve().parents[1] / 'core.py'
    text = source.read_text()
    assert '"tmux", "paste-buffer", "-r", "-t"' in text
    assert '"tmux", "paste-buffer", "-t", str(tmux_name)' not in text
    assert '"tmux", "paste-buffer", "-t", tmux_name' not in text
