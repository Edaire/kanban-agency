#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _default_hermes_repo() -> Path:
    spec = importlib.util.find_spec('hermes_cli')
    if spec and spec.origin:
        return Path(spec.origin).resolve().parents[1]
    return Path.cwd()


REPO_ROOT = Path(os.environ.get('HERMES_AGENT_REPO', str(_default_hermes_repo())))

CODE = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path("__PLUGIN_DIR__")))
sys.path.insert(0, str(Path("__REPO_ROOT__")))
from hermes_cli import kanban_db as kb
from core import scan, start, run, continue_comments, sync, _parse_role_body, _parse_role_rules, _codex_prompt
import core

home = Path(os.environ['HERMES_KANBAN_HOME'])
roles_path = home / 'roles.yaml'
workdir = home / 'workdir'
(workdir / '.ai/rules').mkdir(parents=True)
(workdir / '.ai/rules/developer.md').write_text('# dev', encoding='utf-8')
(workdir / '.ai/rules/tester.md').write_text('# tester', encoding='utf-8')
roles_path.write_text('''
roles:
  default:
    provider: hermes
    aliases: [默认, default]
  developer:
    provider: codex
    rules: [.ai/rules/developer.md]
    aliases: [开发工程师, developer]
  tester:
    provider: codex
    rules: [.ai/rules/tester.md]
    aliases: [测试工程师, tester]
''', encoding='utf-8')
kb.write_board_metadata('agency_test', default_workdir=str(workdir))
conn = kb.connect(board='agency_test')
try:
    empty = scan('agency_test', roles_path)
    assert empty['errors'] == []
    assert empty['roots'] == []

    dev_id = kb.create_task(conn, title='实现登录功能', body='@kanban-agency\n请让开发工程师处理', initial_status='running')
    title_marker = kb.create_task(conn, title='@kanban-agency 测试标题', body='没有marker', initial_status='running')
    child = kb.create_task(conn, title='[agency] developer: 实现登录功能', body='@kanban-agency\n开发', initial_status='running')
    multi_id = kb.create_task(conn, title='开发并测试支付功能', body='@kanban-agency\n需要 developer 和 tester', initial_status='running')
    done_id = kb.create_task(conn, title='开发已完成', body='@kanban-agency\n开发', initial_status='running')
    kb.complete_task(conn, done_id, summary='done')
finally:
    conn.close()

out = scan('agency_test', roles_path)
assert out['errors'] == [], out
ids = [r['root_id'] for r in out['roots']]
assert dev_id in ids, ids
assert title_marker not in ids, ids
assert child not in ids, ids
assert done_id not in ids, ids
by_id = {r['root_id']: r for r in out['roots']}
assert by_id[dev_id]['route_to'] == 'developer', by_id[dev_id]
assert by_id[dev_id]['would_start_provider'] == 'codex'
assert by_id[dev_id]['workdir'] == str(workdir)
assert by_id[multi_id]['route_to'] == 'default', by_id[multi_id]
assert by_id[multi_id]['matched_roles'] == ['developer', 'tester'], by_id[multi_id]

conn = kb.connect(board='agency_test')
try:
    invalid = kb.create_task(conn, title='开发另一个', body='@kanban-agency\nworkdir: relative/path\n开发', initial_status='running')
finally:
    conn.close()
out2 = scan('agency_test', roles_path)
assert any('invalid non-absolute workdir' in w for w in {r['root_id']: r for r in out2['roots']}[invalid]['warnings'])

bad = home / 'bad.yaml'
bad.write_text('roles:\n  default:\n    provider: nope\n', encoding='utf-8')
assert scan('agency_test', bad)['errors']
assert scan('missing_board_xyz', roles_path)['errors']

# start creates one role card, force-promotes it, and second run reuses it
started = start('agency_test', roles_path)
assert not started['errors'], started
assert started['created'], started
created_by_role = {(x['root_id'], x['role']): x for x in started['created']}
assert (dev_id, 'developer') in created_by_role, started
role_task_id = created_by_role[(dev_id, 'developer')]['task_id']
conn = kb.connect(board='agency_test')
try:
    role_task = kb.get_task(conn, role_task_id)
    assert role_task is not None
    assert role_task.title.startswith('[agency] developer:'), role_task.title
    assert role_task.status == 'ready', role_task.status
    links = conn.execute('SELECT parent_id FROM task_links WHERE child_id=?', (role_task_id,)).fetchall()
    assert [r[0] for r in links] == [dev_id]
finally:
    conn.close()
started_again = start('agency_test', roles_path)
assert not started_again['errors'], started_again
assert any(x['task_id'] == role_task_id for x in started_again['reused']), started_again
parsed = _parse_role_body(role_task.body)
assert parsed['provider'] == 'codex'
assert parsed['role'] == 'developer'
assert _parse_role_rules(role_task.body) == ['.ai/rules/developer.md']
prompt = _codex_prompt(role_task, parsed)
assert '<role_rules>' in prompt
assert '# dev' in prompt
assert '.ai/rules/developer.md' in prompt
dry = run('agency_test', dry_run=True)
assert not dry['errors'], dry
assert any(x['task_id'] == role_task_id and x['dry_run'] for x in dry['started']), dry
# unsupported provider skip
conn = kb.connect(board='agency_test')
try:
    human = kb.create_task(conn, title='[agency] human: deploy', body='@kanban-agency-role\nroot_id: t_fake\nrole: human\nprovider: human\n', initial_status='running')
finally:
    conn.close()
dry2 = run('agency_test', dry_run=True)
assert any(x['task_id'] == human and x['reason'] == 'unsupported provider in MVP' for x in dry2['skipped']), dry2
# continue dry-run forwards only when an existing codex thread and new human comment exist
class FakeRunner:
    def _read_bridge_state(self, task_id):
        return {'thread_id': 'thread-test', 'last_human_comment_id': 0, 'state': 'awaiting_review'}
    def codex_kanban_appserver_task_status(self, args):
        return '{"state":{"state":"awaiting_review","thread_id":"thread-test"},"final_tail":"fake final for review"}'
core._load_codex_runner = lambda: FakeRunner()
conn = kb.connect(board='agency_test')
try:
    cid = kb.add_comment(conn, role_task_id, author='tester-human', body='继续同一个会话回答：这是什么？')
finally:
    conn.close()
cont = continue_comments('agency_test', dry_run=True)
assert not cont['errors'], cont
assert any(x['task_id'] == role_task_id and x['comment_id'] == cid and x['dry_run'] for x in cont['continued']), cont
synced = sync('agency_test')
assert not synced['errors'], synced
assert any(x['task_id'] == role_task_id and x['action'] == 'synced_review' for x in synced['synced']), synced
conn = kb.connect(board='agency_test')
try:
    synced_task = kb.get_task(conn, role_task_id)
    assert synced_task.status == 'review', synced_task.status
    assert 'fake final for review' in (synced_task.result or '')
    comments = kb.list_comments(conn, role_task_id)
    assert any(c.author == 'kanban-agency' and 'fake final for review' in c.body for c in comments)
finally:
    conn.close()
print('ALL_TESTS_PASSED')
"""

def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env['HERMES_KANBAN_HOME'] = td
        env.pop('HERMES_KANBAN_BOARD', None)
        env.pop('HERMES_KANBAN_DB', None)
        env.pop('HERMES_KANBAN_WORKSPACES_ROOT', None)
        code = CODE.replace('__PLUGIN_DIR__', str(PLUGIN_DIR)).replace('__REPO_ROOT__', str(REPO_ROOT))
        proc = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, env=env, timeout=60)
        print(proc.stdout, end='')
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
        return proc.returncode

if __name__ == '__main__':
    raise SystemExit(main())
