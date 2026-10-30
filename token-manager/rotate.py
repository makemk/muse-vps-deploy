#!/usr/bin/env python3
"""轮换某个 muse 的 token（零停机）。

流程（flock 保护的读-改-写，原子落盘）:
  1. 找到该 muse 当前有效的条目（enabled 且宽限期未过）；
     若有多个有效条目，必须用 --digest 精确指定旧条目。
  2. 生成新 token + 新条目（``rotated_from`` 指向上一个 digest）。
  3. 旧条目：保持 ``enabled: true``，但加上 ``grace_until``（默认 24h 后）
     和 ``superseded_by``（新 digest）。宽限期内新旧 token 都可用，
     超时后旧 token 自动失效 —— 客户端有充足时间切换。
  4. 审计日志追加一条 rotate 记录。

注意：VPS 端鉴权必须理解 ``grace_until``（见 common.is_entry_valid；
bridge-auth-impl 的 verify_controller_token 需同步该语义）。

用法:
    ./rotate.py --muse muse-rd928 --db /etc/muse-bridge/controllers.json
    ./rotate.py --muse muse-rd928 --db controllers.json --grace-hours 48
    ./rotate.py --muse muse-rd928 --db controllers.json --digest <old-digest> --grace-hours 0
"""
import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (append_audit, find_by_muse, is_entry_valid, load_db,
                    save_db_atomic, utcnow_iso)  # noqa: E402
from gen_token import build_entry  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description='轮换 muse token（零停机，宽限期双活）')
    p.add_argument('--muse', required=True, help='muse id')
    p.add_argument('--db', required=True, help='controllers.json 路径')
    p.add_argument('--digest', default='',
                   help='旧条目的 digest 前缀；多条有效时必填')
    p.add_argument('--grace-hours', type=float, default=24,
                   help='旧 token 宽限期（小时），默认 24；0 = 立即失效')
    p.add_argument('--label', default='', help='新 token 的人类可读标签（默认沿用旧标签）')
    p.add_argument('--audit', default='', help='审计日志路径（默认 <db>.audit.log）')
    args = p.parse_args()

    if args.grace_hours < 0:
        print('error: --grace-hours 不能为负', file=sys.stderr)
        return 2

    audit_path = args.audit or (args.db + '.audit.log')
    now = datetime.now(timezone.utc)

    with open(args.db, 'a+', encoding='utf-8') as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            db = load_db(args.db)
            valid = [(d, e) for d, e in find_by_muse(db, args.muse)
                     if is_entry_valid(e, now)]
            if not valid:
                print(f"error: muse '{args.muse}' 没有有效 token 可轮换", file=sys.stderr)
                return 3
            if len(valid) > 1 and not args.digest:
                ds = ', '.join(d[:12] for d, _ in valid)
                print(f"error: muse '{args.muse}' 有 {len(valid)} 个有效 token "
                      f'({ds}…)，请用 --digest 指定', file=sys.stderr)
                return 3
            if args.digest:
                cands = [(d, e) for d, e in valid if d.startswith(args.digest)]
                if len(cands) != 1:
                    print(f"error: --digest '{args.digest}' 匹配到 {len(cands)} 条",
                          file=sys.stderr)
                    return 3
                old_digest, old_entry = cands[0]
            else:
                old_digest, old_entry = valid[-1]  # 最新的有效条目

            new_token, new_digest, new_entry = build_entry(
                args.muse, list(old_entry['pcs']), args.label or old_entry.get('label', ''),
                allowed_ips=list(old_entry.get('allowed_ips', [])) or None)
            new_entry['rotated_from'] = old_digest

            grace_until = now + timedelta(hours=args.grace_hours)
            old_entry['grace_until'] = grace_until.isoformat()
            old_entry['superseded_by'] = new_digest
            if args.grace_hours == 0:
                old_entry['enabled'] = False
                old_entry['revoked_at'] = utcnow_iso()
                old_entry['revoke_reason'] = 'rotated (immediate)'

            db[new_digest] = new_entry
            save_db_atomic(args.db, db)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    append_audit(audit_path, {
        'action': 'rotate',
        'muse': args.muse,
        'old_digest': old_digest[:16] + '…',
        'new_digest': new_digest[:16] + '…',
        'grace_until': old_entry.get('grace_until'),
        'immediate': args.grace_hours == 0,
    })

    print(json.dumps({
        'muse': args.muse,
        'old_digest': old_digest[:16] + '…',
        'new_digest': new_digest[:16] + '…',
        'grace_until': old_entry.get('grace_until'),
    }, ensure_ascii=False))
    print('', file=sys.stderr)
    print('>>> 新明文 TOKEN（只显示一次，立即分发给该 muse 的客户端）:', file=sys.stderr)
    print(f'>>> {new_token}', file=sys.stderr)
    if args.grace_hours > 0:
        print(f'>>> 旧 token 宽限期至 {old_entry["grace_until"]}，之后自动失效',
              file=sys.stderr)
    else:
        print('>>> 旧 token 已立即失效', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
