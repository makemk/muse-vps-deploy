#!/usr/bin/env python3
"""验证明文 controller token。

语义见 common.verify_token：
  - 先做格式校验（前缀 ``muse_`` + 43 个 urlsafe 字符），格式不对直接拒绝
  - SHA-256 计算 digest，``hmac.compare_digest`` 防时序攻击比对
  - ``enabled=false`` 拒绝；轮换宽限期（grace_until）超时后拒绝
- 成功输出 ``{"ok": true, "muse": ..., "pcs": [...]}``（exit 0）
- 失败输出 ``{"ok": false, "error": ...}``（exit 1）
- ``--ip`` 给出时校验 ``allowed_ips`` 白名单，不在范围内 → 失败

用法:
    ./verify.py --token 'muse_...' --db /etc/muse-bridge/controllers.json
    ./verify.py --token 'muse_...' --db controllers.json --ip 203.0.113.10
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_db, verify_token  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description='验证 muse controller token')
    p.add_argument('--token', required=True, help='明文 token（muse_...）')
    p.add_argument('--db', required=True, help='controllers.json 路径')
    p.add_argument('--ip', default=None, help='源 IP，用于校验 allowed_ips 白名单')
    args = p.parse_args()

    try:
        db = load_db(args.db)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({'ok': False, 'error': f'db load failed: {e}'}))
        return 1

    ok, payload = verify_token(args.token, db, client_ip=args.ip)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
