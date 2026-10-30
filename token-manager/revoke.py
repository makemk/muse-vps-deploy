#!/usr/bin/env python3
"""吊销 token（立即失效）。

- 按 --muse 吊销该 muse 的全部有效条目，或按 --digest 精确吊销一条。
- 条目设 ``enabled: false`` + ``revoked_at`` + ``revoke_reason``；
  digest 保留在库中（可审计、可追溯），不再接受鉴权。
- 审计日志追加 revoke 记录（含操作者 --by，便于追责）。
- 这是破坏性操作：吊销后对应客户端立即 401，需二次确认（--yes 跳过）。

用法:
    ./revoke.py --muse muse-rd928 --db controllers.json --reason "PC 报废"
    ./revoke.py --digest a1b2c3... --db controllers.json --reason "疑似泄露" --yes
"""
import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (append_audit, find_by_muse, is_entry_valid, load_db,
                    save_db_atomic, utcnow_iso)  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description='吊销 token（立即失效）')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--muse', help='吊销该 muse 的全部有效条目')
    g.add_argument('--digest', help='按 digest 前缀精确吊销一条')
    p.add_argument('--db', required=True, help='controllers.json 路径')
    p.add_argument('--reason', required=True, help='吊销原因（写入审计日志）')
    p.add_argument('--by', default=os.environ.get('USER', 'operator'),
                   help='操作者（默认 $USER）')
    p.add_argument('--audit', default='', help='审计日志路径（默认 <db>.audit.log）')
    p.add_argument('--yes', action='store_true', help='跳过二次确认')
    args = p.parse_args()

    audit_path = args.audit or (args.db + '.audit.log')
    now = datetime.now(timezone.utc)

    with open(args.db, 'a+', encoding='utf-8') as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            db = load_db(args.db)
            if args.digest:
                targets = [(d, e) for d, e in db.items()
                           if d.startswith(args.digest)]
                if len(targets) != 1:
                    print(f"error: --digest '{args.digest}' 匹配到 {len(targets)} 条",
                          file=sys.stderr)
                    return 3
            else:
                targets = [(d, e) for d, e in find_by_muse(db, args.muse)
                           if is_entry_valid(e, now)]
                if not targets:
                    print(f"error: muse '{args.muse}' 没有有效 token 可吊销",
                          file=sys.stderr)
                    return 3

            if not args.yes:
                print('即将吊销以下 token（立即失效，不可撤销）：', file=sys.stderr)
                for d, e in targets:
                    print(f"  - {e.get('muse')} / {d[:16]}… / pcs={e.get('pcs')} "
                          f"/ label={e.get('label', '')}", file=sys.stderr)
                ans = input('确认吊销？输入 YES 继续: ').strip()
                if ans != 'YES':
                    print('已取消', file=sys.stderr)
                    return 4

            revoked = []
            for d, e in targets:
                e['enabled'] = False
                e['revoked_at'] = utcnow_iso()
                e['revoke_reason'] = args.reason
                e.pop('grace_until', None)  # 吊销覆盖宽限期
                revoked.append((d, e))
            save_db_atomic(args.db, db)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    for d, e in revoked:
        append_audit(audit_path, {
            'action': 'revoke',
            'muse': e.get('muse'),
            'digest': d[:16] + '…',
            'reason': args.reason,
            'by': args.by,
        })

    print(json.dumps({
        'revoked': [{'muse': e.get('muse'), 'digest': d[:16] + '…'}
                    for d, e in revoked],
        'reason': args.reason,
        'by': args.by,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
