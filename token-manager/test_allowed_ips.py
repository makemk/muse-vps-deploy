"""allowed_ips 源 IP 白名单测试（Gemini 4 架构评审 §7.1）。

覆盖：CIDR 匹配、单 IP（自动 /32）、多网段、空列表/缺失=不限制、
非法格式 → 条目失效（fail-closed）、IPv6、非法源 IP、CLI 端到端。
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (is_entry_valid, ip_allowed, parse_allowed_ips,
                    verify_token)  # noqa: E402
from gen_token import build_entry  # noqa: E402


def make_db(token, digest, entry):
    return {digest: entry}


# ---------- parse_allowed_ips ----------

def test_parse_cidr_list():
    nets = parse_allowed_ips(['203.0.113.10/32', '198.51.100.0/24'])
    assert len(nets) == 2
    assert str(nets[0]) == '203.0.113.10/32'
    assert str(nets[1]) == '198.51.100.0/24'


def test_parse_single_ip_becomes_32():
    nets = parse_allowed_ips(['203.0.113.5'])
    assert str(nets[0]) == '203.0.113.5/32'


def test_parse_empty_and_none():
    assert parse_allowed_ips([]) == []
    assert parse_allowed_ips(None) == []


def test_parse_rejects_bad_formats():
    for bad in ['not-an-ip', '300.1.2.3/24', '203.0.113.0/33', '', '10.0.0.0/-1']:
        with pytest.raises(ValueError):
            parse_allowed_ips([bad])
    with pytest.raises(ValueError):
        parse_allowed_ips('203.0.113.0/24')  # 不是列表
    with pytest.raises(ValueError):
        parse_allowed_ips([123])  # 非字符串元素
    with pytest.raises(ValueError):
        parse_allowed_ips(['203.0.113.0/24', None])


def test_parse_ipv6():
    nets = parse_allowed_ips(['::1', '2001:db8::/32'])
    assert str(nets[0]) == '::1/128'
    assert str(nets[1]) == '2001:db8::/32'


# ---------- is_entry_valid（格式损坏 fail-closed） ----------

def test_entry_valid_with_good_allowed_ips():
    e = {'muse': 'm', 'pcs': ['PC-1'], 'enabled': True,
         'allowed_ips': ['10.0.0.0/8', '192.168.1.5']}
    assert is_entry_valid(e) is True


def test_entry_invalid_with_bad_allowed_ips():
    for bad in [['not-an-ip'], 'plain-string', [123], ['10.0.0.0/33']]:
        e = {'muse': 'm', 'pcs': ['PC-1'], 'enabled': True, 'allowed_ips': bad}
        assert is_entry_valid(e) is False, f'should reject {bad}'


def test_entry_valid_without_allowed_ips():
    e = {'muse': 'm', 'pcs': ['PC-1'], 'enabled': True}
    assert is_entry_valid(e) is True


# ---------- ip_allowed ----------

def test_ip_allowed_cidr():
    e = {'allowed_ips': ['203.0.113.0/24']}
    assert ip_allowed(e, '203.0.113.10') is True
    assert ip_allowed(e, '203.0.113.255') is True
    assert ip_allowed(e, '198.51.100.5') is False


def test_ip_allowed_single_ip():
    e = {'allowed_ips': ['203.0.113.5']}
    assert ip_allowed(e, '203.0.113.5') is True
    assert ip_allowed(e, '203.0.113.6') is False


def test_ip_allowed_multiple_cidrs():
    e = {'allowed_ips': ['203.0.113.10/32', '198.51.100.0/24']}
    assert ip_allowed(e, '203.0.113.10') is True
    assert ip_allowed(e, '198.51.100.99') is True
    assert ip_allowed(e, '203.0.113.11') is False


def test_ip_allowed_empty_or_missing():
    assert ip_allowed({}, '1.2.3.4') is True
    assert ip_allowed({'allowed_ips': []}, '1.2.3.4') is True


def test_ip_allowed_ipv6():
    e = {'allowed_ips': ['2001:db8::/32']}
    assert ip_allowed(e, '2001:db8::1') is True
    assert ip_allowed(e, '2001:db9::1') is False


def test_ip_allowed_malformed_client_ip():
    e = {'allowed_ips': ['203.0.113.0/24']}
    assert ip_allowed(e, 'not-an-ip') is False
    assert ip_allowed(e, '') is False


# ---------- verify_token + client_ip ----------

def test_verify_token_with_ip_ok():
    token, digest, entry = build_entry('muse-x', ['PC-1'], '',
                                       allowed_ips=['203.0.113.0/24'])
    db = make_db(token, digest, entry)
    ok, payload = verify_token(token, db, client_ip='203.0.113.10')
    assert ok is True and payload['muse'] == 'muse-x'


def test_verify_token_with_ip_denied():
    token, digest, entry = build_entry('muse-x', ['PC-1'], '',
                                       allowed_ips=['203.0.113.0/24'])
    db = make_db(token, digest, entry)
    ok, payload = verify_token(token, db, client_ip='198.51.100.5')
    assert ok is False
    assert payload['error'] == 'source IP not allowed'
    assert payload['muse'] == 'muse-x'


def test_verify_token_no_ip_arg_skips_check():
    token, digest, entry = build_entry('muse-x', ['PC-1'], '',
                                       allowed_ips=['203.0.113.0/24'])
    db = make_db(token, digest, entry)
    ok, _ = verify_token(token, db)  # client_ip 未传 → 不校验
    assert ok is True


def test_verify_token_no_whitelist_any_ip():
    token, digest, entry = build_entry('muse-x', ['PC-1'], '')
    db = make_db(token, digest, entry)
    ok, _ = verify_token(token, db, client_ip='1.2.3.4')
    assert ok is True


def test_verify_token_ip_check_after_grace_check():
    # 宽限期失效的条目即使 IP 匹配也应拒绝（顺序：先条目有效性）
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token, digest, entry = build_entry('muse-x', ['PC-1'], '',
                                       allowed_ips=['203.0.113.0/24'])
    entry['grace_until'] = past
    db = make_db(token, digest, entry)
    ok, payload = verify_token(token, db, client_ip='203.0.113.10')
    assert ok is False and 'grace' in payload['error']


# ---------- gen_token CLI ----------

def test_gen_token_cli_allowed_ips(tmp_path):
    db_path = str(tmp_path / 'controllers.json')
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'gen_token.py'),
         '--muse', 'muse-cli-ip', '--pcs', 'PC-1',
         '--allowed-ips', '203.0.113.10/32, 198.51.100.0/24',
         '--output', db_path],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    db = json.load(open(db_path, encoding='utf-8'))
    entry = next(iter(db.values()))
    assert entry['allowed_ips'] == ['203.0.113.10/32', '198.51.100.0/24']


def test_gen_token_cli_rejects_bad_cidr(tmp_path):
    db_path = str(tmp_path / 'controllers.json')
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'gen_token.py'),
         '--muse', 'muse-cli-bad', '--pcs', 'PC-1',
         '--allowed-ips', 'not-an-ip',
         '--output', db_path],
        capture_output=True, text=True)
    assert r.returncode == 2
    assert 'allowed-ips' in r.stderr


# ---------- verify.py CLI ----------

def _cli_db_with_ip_entry(tmp_path):
    """生成一个带 allowed_ips 的条目，返回 (db_path, plaintext_token)。"""
    db_path = str(tmp_path / 'controllers.json')
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'gen_token.py'),
         '--muse', 'muse-cli-v', '--pcs', 'PC-1',
         '--allowed-ips', '127.0.0.1/32',
         '--output', db_path],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 明文 token 在 stderr 的 >>> 行
    token = [l for l in r.stderr.splitlines() if l.startswith('>>> muse_')][0][4:]
    return db_path, token


def test_verify_cli_ip_allowed(tmp_path):
    db_path, token = _cli_db_with_ip_entry(tmp_path)
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'verify.py'),
         '--token', token, '--db', db_path, '--ip', '127.0.0.1'],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout)['ok'] is True


def test_verify_cli_ip_denied(tmp_path):
    db_path, token = _cli_db_with_ip_entry(tmp_path)
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'verify.py'),
         '--token', token, '--db', db_path, '--ip', '10.9.9.9'],
        capture_output=True, text=True)
    assert r.returncode == 1
    payload = json.loads(r.stdout)
    assert payload['ok'] is False
    assert payload['error'] == 'source IP not allowed'


def test_verify_cli_no_ip_flag_backward_compat(tmp_path):
    db_path, token = _cli_db_with_ip_entry(tmp_path)
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, 'verify.py'),
         '--token', token, '--db', db_path],
        capture_output=True, text=True)
    assert r.returncode == 0  # 不传 --ip 时不校验白名单
