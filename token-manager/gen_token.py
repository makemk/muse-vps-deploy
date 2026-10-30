#!/usr/bin/env python3
"""生成 controller token 并（可选）直接写入 controllers.json。

Token 模型：
  - 格式: ``muse_`` + ``secrets.token_urlsafe(32)``（256-bit 熵）
  - VPS 只存 SHA-256 hex，明文永不落盘、永不可恢复
  - 明文 token 只在 stderr 显示一次 —— 立即分发，过期不候

用法:
    # 只打印 JSON 条目（stdout）+ 明文 token（stderr）
    ./gen_token.py --muse muse-rd928 --pcs RD-928 --label "RD-928 绑定"

    # 超管 token 绑定源 IP（逗号分隔的 CIDR，单 IP 自动视为 /32）
    ./gen_token.py --muse super --pcs "*" --allowed-ips "203.0.113.10/32,198.51.100.0/24"

    # SSH 回退专用 token：只允许本机回环调用
    ./gen_token.py --muse ssh_fallback_admin --pcs "*" --allowed-ips "127.0.0.1/32" \\
        --output /etc/muse-bridge/controllers.json
"""
import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_db, save_db_atomic, TOKEN_PREFIX, parse_allowed_ips  # noqa: E402


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)  # 256-bit entropy


def build_entry(muse_id: str, pcs: list, label: str,
                allowed_ips: list | None = None) -> tuple:
    token = generate_token()
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    entry = {
        'muse': muse_id,
        'pcs': pcs,
        'created': datetime.now(timezone.utc).isoformat(),
        'label': label,
        'enabled': True,
    }
    if allowed_ips:
        entry['allowed_ips'] = allowed_ips
    return token, digest, entry


def main() -> int:
    p = argparse.ArgumentParser(description='生成 muse controller token')
    p.add_argument('--muse', required=True, help='muse id, e.g. muse-rd928')
    p.add_argument('--pcs', required=True,
                   help='逗号分隔的 PC 列表, e.g. "RD-928"；超管用 "*"')
    p.add_argument('--label', default='', help='人类可读标签')
    p.add_argument('--output', default='',
                   help='controllers.json 路径；给出则直接追加写入（flock 保护）')
    p.add_argument('--allowed-ips', default='',
                   help='逗号分隔的 CIDR 列表, e.g. "203.0.113.10/32,198.51.100.0/24"；'
                        '单 IP 自动视为 /32；缺省=不限制')
    p.add_argument('--force', action='store_true',
                   help='允许同名 muse 重复添加（默认拒绝重名）')
    args = p.parse_args()

    pcs = [x.strip() for x in args.pcs.split(',') if x.strip()]
    if not pcs:
        print('error: --pcs 不能为空', file=sys.stderr)
        return 2
    if '*' in pcs and len(pcs) > 1:
        print('error: "*" 不能与其他 PC 混用（超管独占）', file=sys.stderr)
        return 2

    allowed_ips = [x.strip() for x in args.allowed_ips.split(',') if x.strip()]
    try:
        parse_allowed_ips(allowed_ips)  # 生成时即校验 CIDR 格式，非法直接报错
    except ValueError as e:
        print(f'error: --allowed-ips 格式非法: {e}', file=sys.stderr)
        return 2

    token, digest, entry = build_entry(args.muse, pcs, args.label, allowed_ips)

    if args.output:
        # flock 保护的读-改-写，避免并发生成互相覆盖
        with open(args.output, 'a+', encoding='utf-8') as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                db = load_db(args.output)
                if not args.force:
                    for _d, e in db.items():
                        if e.get('muse') == args.muse:
                            print(f"error: muse '{args.muse}' 已存在（digest {_d[:12]}…），"
                                  '用 --force 强制添加', file=sys.stderr)
                            return 3
                db[digest] = entry
                save_db_atomic(args.output, db)
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        print(f'已写入 {args.output}（digest {digest[:16]}…）', file=sys.stderr)
    else:
        print(json.dumps({digest: entry}, indent=2, ensure_ascii=False))

    print('', file=sys.stderr)
    print('>>> 明文 TOKEN（只显示一次，立即分发并删除本地副本，永不可恢复）:',
          file=sys.stderr)
    print(f'>>> {token}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
