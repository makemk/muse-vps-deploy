"""token-manager 共享逻辑：纯标准库。

VPS 端鉴权 patch（bridge-auth-impl）可直接复用本模块的
``verify_token`` / ``is_entry_valid``，保持语义一致。
"""
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone

TOKEN_PREFIX = 'muse_'
TOKEN_RE = re.compile(r'^muse_[A-Za-z0-9_-]{43}$')  # token_urlsafe(32) == 43 chars


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_format(token) -> str | None:
    """格式校验；返回错误信息，无错误返回 None。"""
    if not isinstance(token, str):
        return 'token must be a string'
    if not TOKEN_RE.match(token):
        return 'malformed token (expect muse_ + 43 urlsafe chars)'
    return None


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def parse_allowed_ips(allowed_ips) -> list:
    """验证并预编译 allowed_ips CIDR 列表。

    格式定义（Gemini 4 架构评审 §7.1）：
      - ``allowed_ips`` 为字符串数组，元素为 IPv4/IPv6 单 IP 或 CIDR 网段
      - 单 IP 自动识别为 /32（/128），``ip_network('203.0.113.5')``
        即 ``203.0.113.5/32``
      - 缺省 / 空列表 ``[]`` → 不限制 IP
    格式错误抛 ``ValueError``（调用方做 fail-closed 处理）。
    """
    if not allowed_ips:
        return []
    if not isinstance(allowed_ips, list) or not all(
            isinstance(x, str) for x in allowed_ips):
        raise ValueError('allowed_ips must be a list of strings')
    nets = []
    for cidr in allowed_ips:
        nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
    return nets


def ip_allowed(entry: dict, client_ip: str) -> bool:
    """``client_ip`` 是否在 ``entry['allowed_ips']`` 范围内。

    空列表 / 缺失 → 不限制（允许）。IP 格式非法 → 拒绝（fail-closed）。
    """
    nets = parse_allowed_ips(entry.get('allowed_ips'))
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def is_entry_valid(entry: dict, now: datetime | None = None) -> bool:
    """条目是否有效。

    规则：
      1. ``enabled`` 必须为 true；
      2. 若有 ``grace_until``（轮换宽限期），超时后即失效 —— 即使 enabled 仍为 true；
      3. 若有 ``allowed_ips`` 且格式损坏 → 失效（fail-closed）。
    """
    if not entry.get('enabled', False):
        return False
    grace = entry.get('grace_until')
    if grace:
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            until = datetime.fromisoformat(grace)
        except (ValueError, TypeError):
            return False  # 宽限期字段损坏 → fail-closed
        if now > until:
            return False
    if 'allowed_ips' in entry:
        try:
            parse_allowed_ips(entry['allowed_ips'])
        except ValueError:
            return False  # IP 白名单字段损坏 → fail-closed
    return True


def verify_token(token: str, db: dict, now: datetime | None = None,
                client_ip: str | None = None) -> tuple:
    """返回 (ok: bool, payload: dict)。纯函数，便于测试与 VPS 复用。

    ``client_ip`` 为可选的源 IP：给出时校验 ``allowed_ips`` 白名单，
    不在范围内 → ``ok=False, error='source IP not allowed'``。
    缺省 / 空列表 = 不限制。
    """
    err = check_format(token)
    if err:
        return False, {'ok': False, 'error': err}
    digest = token_digest(token)
    matched = None
    for k, v in db.items():
        if hmac.compare_digest(k, digest):
            matched = v
            break
    if matched is None:
        return False, {'ok': False, 'error': 'invalid token'}
    if not is_entry_valid(matched, now):
        reason = 'token disabled'
        if matched.get('grace_until'):
            reason = 'token expired (grace period over)'
        return False, {'ok': False, 'error': reason, 'muse': matched.get('muse')}
    if client_ip is not None and not ip_allowed(matched, client_ip):
        return False, {'ok': False, 'error': 'source IP not allowed',
                       'muse': matched.get('muse')}
    return True, {'ok': True, 'muse': matched['muse'], 'pcs': matched['pcs']}


def load_db(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        raw = f.read().strip()
    if not raw:
        return {}  # 空文件视为 {}（'a+' 建锁文件时会产生）
    return json.loads(raw)


def save_db_atomic(path: str, db: dict):
    """原子写入：临时文件 + fsync + rename，权限强制 600。"""
    d = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.controllers.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) != 0o600:
        os.chmod(path, 0o600)


def find_by_muse(db: dict, muse_id: str) -> list:
    """返回 [(digest, entry)]，按创建时间排序。"""
    hits = [(d, e) for d, e in db.items() if e.get('muse') == muse_id]
    hits.sort(key=lambda kv: kv[1].get('created', ''))
    return hits


def append_audit(audit_path: str, record: dict):
    """审计日志：JSONL 追加，flock 保护。"""
    record = dict(record)
    record.setdefault('at', utcnow_iso())
    line = json.dumps(record, ensure_ascii=False) + '\n'
    with open(audit_path, 'a', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
