"""token-manager 生产级测试：生成 / 验证 / 轮换 / 吊销 / 安全属性。"""
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (check_format, is_entry_valid, load_db, save_db_atomic,
                    token_digest, verify_token)  # noqa: E402
from gen_token import build_entry  # noqa: E402


def fresh_db(tmp_path):
    p = str(tmp_path / 'controllers.json')
    save_db_atomic(p, {})
    return p


def add_entry(db_path, muse='muse-test', pcs=None, **kw):
    token, digest, entry = build_entry(muse, pcs or ['PC-1'], 'test')
    entry.update(kw)
    db = load_db(db_path)
    db[digest] = entry
    save_db_atomic(db_path, db)
    return token, digest, entry


# ---------- 生成 ----------

def test_token_format():
    token, _, _ = build_entry('muse-a', ['PC-1'], '')
    assert token.startswith('muse_')
    assert len(token) == len('muse_') + 43
    assert check_format(token) is None


def test_token_uniqueness():
    tokens = {build_entry('muse-a', ['PC-1'], '')[0] for _ in range(100)}
    assert len(tokens) == 100


def test_malformed_rejected():
    for bad in ['', 'muse_', 'muse_short', 'bearer muse_' + 'x' * 43,
                'muse_' + 'x' * 42, 'muse_' + 'x' * 44, None, 123]:
        assert check_format(bad) is not None


def test_db_permissions(tmp_path):
    p = fresh_db(tmp_path)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


# ---------- 验证 ----------

def test_verify_ok(tmp_path):
    p = fresh_db(tmp_path)
    token, _, _ = add_entry(p, muse='muse-rd928', pcs=['RD-928'])
    ok, payload = verify_token(token, load_db(p))
    assert ok and payload['muse'] == 'muse-rd928' and payload['pcs'] == ['RD-928']


def test_verify_wrong_token(tmp_path):
    p = fresh_db(tmp_path)
    add_entry(p)
    token, _, _ = build_entry('muse-other', ['PC-9'], '')
    ok, payload = verify_token(token, load_db(p))
    assert not ok and payload['error'] == 'invalid token'


def test_verify_disabled(tmp_path):
    p = fresh_db(tmp_path)
    token, _, _ = add_entry(p, enabled=False)
    ok, payload = verify_token(token, load_db(p))
    assert not ok and payload['error'] == 'token disabled'


def test_verify_missing_enabled_fail_closed(tmp_path):
    p = fresh_db(tmp_path)
    token, digest, entry = build_entry('muse-x', ['PC-1'], '')
    del entry['enabled']
    db = {digest: entry}
    ok, _ = verify_token(token, db)
    assert not ok  # fail-closed


# ---------- 宽限期 ----------

def test_grace_valid_during_window(tmp_path):
    p = fresh_db(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token, _, _ = add_entry(p, grace_until=future)
    ok, _ = verify_token(token, load_db(p))
    assert ok


def test_grace_expired(tmp_path):
    p = fresh_db(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    token, _, _ = add_entry(p, grace_until=past)
    ok, payload = verify_token(token, load_db(p))
    assert not ok and 'grace' in payload['error']


def test_grace_corrupt_fail_closed(tmp_path):
    p = fresh_db(tmp_path)
    token, _, _ = add_entry(p, grace_until='not-a-date')
    ok, _ = verify_token(token, load_db(p))
    assert not ok


# ---------- CLI：gen / verify ----------

def run_cli(script, *args, cwd=HERE, stdin_text=None):
    return subprocess.run([sys.executable, os.path.join(cwd, script), *args],
                          capture_output=True, text=True, cwd=cwd,
                          input=stdin_text)


def test_cli_gen_and_verify_roundtrip(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'muse-cli', '--pcs', 'PC-7',
                '--label', 'cli 测试', '--output', db)
    assert r.returncode == 0, r.stderr
    token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    r2 = run_cli('verify.py', '--token', token, '--db', db)
    assert r2.returncode == 0
    assert json.loads(r2.stdout)['ok'] is True


def test_cli_gen_duplicate_muse_rejected(tmp_path):
    db = str(tmp_path / 'c.json')
    run_cli('gen_token.py', '--muse', 'muse-dup', '--pcs', 'PC-1', '--output', db)
    r = run_cli('gen_token.py', '--muse', 'muse-dup', '--pcs', 'PC-2', '--output', db)
    assert r.returncode == 3 and '已存在' in r.stderr


def test_cli_gen_star_exclusive(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'super', '--pcs', '*,PC-1', '--output', db)
    assert r.returncode == 2


def test_cli_verify_bad_token(tmp_path):
    db = str(tmp_path / 'c.json')
    save_db_atomic(db, {})
    r = run_cli('verify.py', '--token', 'muse_' + 'z' * 43, '--db', db)
    assert r.returncode == 1
    assert json.loads(r.stdout)['error'] == 'invalid token'


# ---------- 轮换 ----------

def test_rotate_dual_active_during_grace(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'muse-rot', '--pcs', 'PC-1', '--output', db)
    old_token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    r = run_cli('rotate.py', '--muse', 'muse-rot', '--db', db, '--grace-hours', '24')
    assert r.returncode == 0, r.stderr
    new_token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    assert new_token != old_token
    data = load_db(db)
    assert len(data) == 2
    # 宽限期内新旧都有效
    assert verify_token(old_token, data)[0] is True
    assert verify_token(new_token, data)[0] is True
    # 旧条目有 grace_until + superseded_by，新条目有 rotated_from
    olds = [e for e in data.values() if e.get('superseded_by')]
    news = [e for e in data.values() if e.get('rotated_from')]
    assert len(olds) == 1 and len(news) == 1
    # 审计日志
    audit = db + '.audit.log'
    assert os.path.exists(audit)
    rec = json.loads(open(audit, encoding='utf-8').read().strip().splitlines()[-1])
    assert rec['action'] == 'rotate' and rec['muse'] == 'muse-rot'


def test_rotate_immediate(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'muse-imm', '--pcs', 'PC-1', '--output', db)
    old_token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    r = run_cli('rotate.py', '--muse', 'muse-imm', '--db', db, '--grace-hours', '0')
    assert r.returncode == 0
    data = load_db(db)
    assert verify_token(old_token, data)[0] is False  # 立即失效


def test_rotate_no_valid_token_fails(tmp_path):
    db = str(tmp_path / 'c.json')
    save_db_atomic(db, {})
    r = run_cli('rotate.py', '--muse', 'muse-ghost', '--db', db)
    assert r.returncode == 3


# ---------- 吊销 ----------

def test_revoke_by_muse(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'muse-rev', '--pcs', 'PC-1', '--output', db)
    token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    r = run_cli('revoke.py', '--muse', 'muse-rev', '--db', db,
                '--reason', '测试吊销', '--yes')
    assert r.returncode == 0, r.stderr
    data = load_db(db)
    ok, payload = verify_token(token, data)
    assert not ok and payload['error'] == 'token disabled'
    # digest 保留，可审计
    assert len(data) == 1
    entry = next(iter(data.values()))
    assert entry['revoke_reason'] == '测试吊销' and 'revoked_at' in entry
    # 审计日志
    rec = json.loads(open(db + '.audit.log', encoding='utf-8').read().strip().splitlines()[-1])
    assert rec['action'] == 'revoke' and rec['reason'] == '测试吊销'


def test_revoke_by_digest(tmp_path):
    db = str(tmp_path / 'c.json')
    r = run_cli('gen_token.py', '--muse', 'muse-a', '--pcs', 'PC-1', '--output', db)
    run_cli('gen_token.py', '--muse', 'muse-b', '--pcs', 'PC-2',
            '--output', db, '--force')
    data = load_db(db)
    target = next(d for d, e in data.items() if e['muse'] == 'muse-a')
    r = run_cli('revoke.py', '--digest', target[:16], '--db', db,
                '--reason', '精确吊销', '--yes')
    assert r.returncode == 0
    data = load_db(db)
    assert data[target]['enabled'] is False
    # muse-b 不受影响
    other = next(d for d, e in data.items() if e['muse'] == 'muse-b')
    assert data[other]['enabled'] is True


def test_revoke_requires_confirmation(tmp_path):
    db = str(tmp_path / 'c.json')
    run_cli('gen_token.py', '--muse', 'muse-c', '--pcs', 'PC-1', '--output', db)
    # 输入非 YES → 取消，returncode 4，条目保持有效
    r = run_cli('revoke.py', '--muse', 'muse-c', '--db', db, '--reason', 'x',
                stdin_text='no\n')
    assert r.returncode == 4
    data = load_db(db)
    assert next(iter(data.values()))['enabled'] is True
    # 输入 YES → 执行吊销
    r = run_cli('revoke.py', '--muse', 'muse-c', '--db', db, '--reason', 'x',
                stdin_text='YES\n')
    assert r.returncode == 0
    assert next(iter(load_db(db).values()))['enabled'] is False
