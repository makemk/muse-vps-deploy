#!/usr/bin/env python3
"""Dispatch authorization patch for muse-bridge.py HTTP handlers.

This is a PATCH FILE - do not run directly. It describes the modifications
to handle_connection() in /usr/local/bin/muse-bridge.py.

It also declares the integration points for the v2 modules
(fast_auth.py / dedup.py / wss_scheduler.py) — see the "V2 MODULES" section
before MERGE INSTRUCTIONS. Those three deploy as whole files, not merges.

Current structure (lines 454-460):
    token = (headers.get("x-bridge-token") or ...)
    ...
    if not check_auth(token):
        await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
        return

Each endpoint (/cmd, /batch, /file) then does:
    target = body_json.get("target", "") or None
    ... pick_client(target) or dispatch_request_to_pc(req with target) ...

PATCH STRATEGY: Two-tier auth with graceful migration.
- Tier 1 (new): controller token from controllers.json -> (muse_id, allowed_pcs)
- Tier 2 (legacy): global CONFIGURED_TOKEN -> full access (for transition period)
- WSS handler (line 392) is UNTOUCHED - PC clients keep using global token.

SECURITY HARDENING (2026-10-19 review, see SECURITY_REVIEW.md):
- H1:  fail-closed check at the top of the auth block. If controllers.json
       exists but is unreadable / has unsafe permissions / is corrupt with no
       valid cache, deny EVERYTHING with 503 (controller AND legacy).
- H7:  Bearer parsing is prefix-only + case-insensitive via
       extract_bearer_token(); non-string token values are coerced to ""
       so a JSON body like {"token": 123} can never raise AttributeError.
- H5:  target must be str/None/"" else 400; 403 messages are generic.
- H6:  audit log lines use _log_safe() (no CR/LF injection, truncated).

401 vs 403 semantics (enforced):
- 401 = no/invalid token (authentication failed)
- 403 = valid token but target not permitted (authorization failed)
- 503 = auth subsystem misconfigured (fail-closed, operator must fix)
"""

# ============================================================================
# PATCH 1: Replace the HTTP API auth block (lines 454-460)
# ============================================================================
# OLD (lines 454-460):
"""
        token = (
            headers.get("x-bridge-token")
            or headers.get("authorization", "").replace("Bearer ", "").strip()
            or body_json.get("token", "")
            or query.get("token", [""])[0]
        )
        ...
        if not check_auth(token):
            await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
            return
"""

# NEW:
"""
        # H1: fail-closed - auth subsystem misconfigured -> deny everything.
        # load_controllers() refreshes the flag; missing file (pre-migration)
        # does NOT set it, so legacy-only operation keeps working.
        load_controllers()
        if controllers_fail_closed():
            await send_json({"ok": False, "error": "Authentication service unavailable"}, 503)
            return

        # H7: hardened token extraction. Bearer scheme is prefix-only and
        # case-insensitive; a non-string token value can never be valid.
        token = (
            headers.get("x-bridge-token")
            or extract_bearer_token(headers.get("authorization", ""))
            or body_json.get("token", "")
            or query.get("token", [""])[0]
        )
        if not isinstance(token, str):
            token = ""

        # Two-tier auth: controller token (new) -> legacy global token (transition)
        # controller_auth = (muse_id, allowed_pcs) or None (legacy full access)
        controller_auth = verify_controller_token(token)
        if controller_auth is None:
            if not check_auth(token):
                await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
                return
            # Legacy global token: full access during migration (log for audit)
            controller_auth = ("legacy", ["*"])
            print(f"[bridge] Legacy global token used for "
                  f"{_log_safe(method, 16)} {_log_safe(path, 64)}", file=sys.stderr)
"""

# ============================================================================
# PATCH 2: Add target permission check in /cmd handler (after line 477)
# ============================================================================
# In the /cmd handler, after:
#     target = body_json.get("target", "") or None
# INSERT:
"""
            # Controller authorization: check target permission (H5/H6)
            if controller_auth[0] != "legacy":
                muse_id, allowed_pcs = controller_auth
                if target is not None and not isinstance(target, str):
                    await send_json({"ok": False, "error": "Bad request: target must be a string"}, 400)
                    return
                allowed, err = check_target_permission(allowed_pcs, target)
                if not allowed:
                    await send_json({"ok": False, "error": f"Forbidden: {err}"}, 403)
                    return
                target = resolve_target(allowed_pcs, target)
                print(f"[bridge] {_log_safe(muse_id, 40)} -> {_log_safe(target or '(auto)', 40)}: "
                      f"cmd {_log_safe(cmd)}", file=sys.stderr)
"""
# This goes in BOTH branches (wait=true via dispatch_request_to_pc, and
# wait=false via pick_client). Place it right after `target = ...` line,
# before `req = {...}` so both paths are covered.

# ============================================================================
# PATCH 3: Add target permission check in /batch handler (after line 507)
# ============================================================================
# In the /batch handler, after:
#     target = body_json.get("target", "") or None
# INSERT:
"""
            if controller_auth[0] != "legacy":
                muse_id, allowed_pcs = controller_auth
                if target is not None and not isinstance(target, str):
                    await send_json({"ok": False, "error": "Bad request: target must be a string"}, 400)
                    return
                allowed, err = check_target_permission(allowed_pcs, target)
                if not allowed:
                    await send_json({"ok": False, "error": f"Forbidden: {err}"}, 403)
                    return
                target = resolve_target(allowed_pcs, target)
                print(f"[bridge] {_log_safe(muse_id, 40)} -> {_log_safe(target or '(auto)', 40)}: "
                      f"batch {len(commands) if isinstance(commands, list) else '?'} cmds",
                      file=sys.stderr)
"""

# ============================================================================
# PATCH 4: Add target permission check in /file handler (after line ~545)
# ============================================================================
# In the /file handler, after:
#     target = body_json.get("target", "") or None
# INSERT:
"""
            if controller_auth[0] != "legacy":
                muse_id, allowed_pcs = controller_auth
                if target is not None and not isinstance(target, str):
                    await send_json({"ok": False, "error": "Bad request: target must be a string"}, 400)
                    return
                allowed, err = check_target_permission(allowed_pcs, target)
                if not allowed:
                    await send_json({"ok": False, "error": f"Forbidden: {err}"}, 403)
                    return
                target = resolve_target(allowed_pcs, target)
                print(f"[bridge] {_log_safe(muse_id, 40)} -> {_log_safe(target or '(auto)', 40)}: "
                      f"file {_log_safe(op, 32)}", file=sys.stderr)
"""
# Note: /file handler gets target from body_json (line ~545: target = body_json.get(...)).
# The check must come after that line but before dispatch_request_to_pc(req, ...).

# ============================================================================
# PATCH 5 (defense in depth): pick_client wrapper (optional but recommended)
# ============================================================================
# Add an optional allowed_pcs parameter to pick_client for defense-in-depth.
# This is zero-cost (in-process) and protects against future bugs in the
# handler-level checks.
#
# Change signature from:
#     def pick_client(hostname=None):
# To:
#     def pick_client(hostname=None, allowed_pcs=None):
# And add at the top of the function body:
#     if allowed_pcs is not None and "*" not in allowed_pcs:
#         # Defense in depth: double-check target is in allowed list
#         if hostname:
#             h_lower = hostname.lower()
#             if not any(isinstance(pc, str) and pc.lower() == h_lower for pc in allowed_pcs):
#                 return None, "forbidden"
#         # If no hostname and multiple PCs, the handler should have resolved it.
#         # If we get here with no hostname, let the existing "multiple" logic handle it.
#
# Then in dispatch_request_to_pc, add handling for "forbidden":
#     if reason == "forbidden":
#         return {"ok": False, "error": "Target not authorized"}
#
# This is OPTIONAL - the handler-level checks (patches 2-4) are the authoritative
# enforcement point. Only add this if you want belt-and-suspenders.

# ============================================================================
# V2 MODULES: fast_auth.py / dedup.py / wss_scheduler.py — 集成点声明
# ============================================================================
# 部署方式：与 auth_patch.py 的"精确锚点代码合并"不同，这三个 v2 模块以
# **整文件拷贝**部署到 VPS（与 muse-bridge.py 同目录），由 muse-bridge.py
# 直接 import。原因：它们是独立的纯逻辑模块（无行号锚点依赖），整文件
# 部署避免了大段代码合并的行号漂移风险；热更新只需替换单个文件。
#
# VPS 目标路径（与 muse-bridge.py 同目录）：
#   /usr/local/bin/fast_auth.py
#   /usr/local/bin/dedup.py
#   /usr/local/bin/wss_scheduler.py
#
# 文件分发（在 deploy-auth-patch.sh 的 scp 步骤中追加，或走 PATCH_DIR）：
#   scp ~/workspace/muse-vps-deploy/vps-auth-patch/{fast_auth.py,dedup.py,wss_scheduler.py} \
#       root@vps:/usr/local/bin/
#
# ----------------------------------------------------------------------------
# V2-A: fast_auth.py — FastAuthEngine（高性能鉴权引擎）
# ----------------------------------------------------------------------------
# 集成锚点 1（import）：muse-bridge.py 顶部 import 区追加：
#     from fast_auth import get_engine, engine_fail_closed
# get_engine() 返回进程级单例：内部 200ms 节流 mtime 检查 + RCU 不可变
# 字典切换，controllers.json 变更自动热加载（写入方必须 temp +
# os.replace() 原子替换）。
#
# 集成锚点 2（鉴权块）：替换 PATCH 1 中
#     controller_auth = verify_controller_token(token)
# 的调用，改用引擎的复合决策（(blake2b_tag, ip, target) -> resolved）：
#     engine = get_engine()  # 缺省 /etc/muse-bridge/controllers.json
#     if engine.fail_closed():
#         await send_json({"ok": False, "error": "Authentication service unavailable"}, 503)
#         return
#     http_status, payload = engine.authenticate_and_authorize(
#         raw_token=token, client_ip=client_ip, req_target=target)
# 返回语义：200 -> payload 为解析后的 target_pc；
#           401 -> token 无效/未知/吊销（payload 为错误信息）；
#           403 -> IP 不允许 / target 越权 / 超管未显式指定 target；
#           503 -> fail-closed（controllers.json 损坏/权限不安全）。
# 注意：
#   - client_ip 取法：nginx 转发到 127.0.0.1:8471 后，Python 侧看到的对端
#     IP 恒为 127.0.0.1；真实客户端 IP 必须从 X-Forwarded-For / X-Real-IP
#     头取（nginx 须配 proxy_set_header X-Forwarded-For $remote_addr）。
#     取不到时传 "" —— 但注意：若该 token 配了 allowed_ips（非空），空
#     client_ip 会判 403 "Invalid client IP"。所以 IP 白名单 token 必须
#     保证 nginx 透传了真实 IP，否则合法请求也会被 403。
#   - allowed_ips 为空列表的条目 = 不限制来源 IP（向后兼容旧 token）。
#   - 超管（pcs=["*"]）必须显式指定 target，否则 403。
#   - 引擎只缓存 200 决策（401/403 永不进缓存，防缓存投毒）。
#   - 503 语义与 PATCH 1 的 controllers_fail_closed() 一致：二选一保留，
#     不要双重 fail-closed 检查。
#
# ----------------------------------------------------------------------------
# V2-B: dedup.py — DedupManager（request_id 去重状态机）
# ----------------------------------------------------------------------------
# 集成锚点 1（import）：muse-bridge.py 顶部追加：
#     from dedup import DedupManager, IN_FLIGHT, COMPLETED, UNCERTAIN, OverloadedError
# 并在模块级初始化单例：dedup = DedupManager()  # 单事件循环内调用即原子
#
# 集成锚点 2（/cmd 处理器）：放在 PATCH 2 的 target 权限检查之后、
# dispatch_request_to_pc(req, ...) 调用之前。request_id 取自请求体
# body_json.get("request_id")（VM daemon 每调用生成 UUIDv4；老客户端不带
# 则服务端用 uuid.uuid4() 代生——无去重收益但不破坏兼容）。
# 完整 ~20 行接入模板见 dedup.py 模块 docstring
# （"Integration pattern for the /cmd handler"），语义摘要：
#   - get_or_create() 抛 OverloadedError（IN_FLIGHT 超 10000 上限）
#     -> 回 503（可重试，永不 500）
#   - 非新请求且 COMPLETED -> 直接重放缓存 (http_status, response_body)
#     （>256KB 的包体只缓存状态不缓存正文）
#   - 非新请求且 UNCERTAIN -> 回 504 {"error":"EXECUTION_UNCERTAIN"}
#   - 非新请求且 IN_FLIGHT -> wait_for(timeout=300) 挂载等待同一次执行；
#     超时抛 asyncio.TimeoutError -> 504。严禁自动换新 request_id 重发
#     （at-most-once）。
#   - 新请求：执行 -> complete(request_id, http_status, body)；执行期抛
#     异常（可能已送达 PC）-> mark_uncertain(request_id) -> 504。
#   - TTL 300s 从完成时刻起算；IN_FLIGHT 记录永不被 TTL/LRU 淘汰。
#
# 集成锚点 3（WSS 断开）：PC 的 WSConnection 关闭回调中调用：
#     dedup.mark_uncertain_for_target(pc_id)
# 将该 PC 所有 IN_FLIGHT 请求转 UNCERTAIN（同 request_id 重试持续 504
# 直到 TTL 过期，绝不自动重发）。
# 注意：dedup 是 asyncio 模型（wait_for 用 asyncio.Event）；
# muse-bridge.py 的 handle_connection 已是 async，可直接 await。
#
# ----------------------------------------------------------------------------
# V2-C: wss_scheduler.py — WSSScheduler（WSS 双优先级队列 + 64KB 分块）
# ----------------------------------------------------------------------------
# 集成锚点 1（import）：muse-bridge.py 顶部追加：
#     from wss_scheduler import WSSScheduler, QueueFullError
# 并在模块级初始化单例：wss_sched = WSSScheduler()
#
# 集成锚点 2（PC 上/下线）：WSConnection 建立时
#     wss_sched.register_pc(pc_id)
# 关闭时
#     wss_sched.unregister_pc(pc_id)  # 返回丢弃的排队消息数，建议记日志
#
# 集成锚点 3（发送路径）：所有发往 PC 的 WSS 消息不再直发，
# 改为 wss_sched.enqueue(pc_id, msg_type, payload)，msg_type 按双优先级划分
# （Gemini-4 §5.3）：/cmd、/batch 的请求与响应走高优先级（零等待插队）；
# /file 的 64KB 分片走低优先级（同一 PC 最多 1 个分片在途）。
# 每 PC 一个发送泵 task 循环：
#     nxt = wss_sched.get_next_message(pc_id)  # -> (msg_type, payload) | None
#     if nxt is None: 等待新消息事件
#     await ws.send(serialize(nxt)); wss_sched.on_response(pc_id, msg_type)
# 64KB 应用层分块由调用方在 enqueue 前完成（分块 + 最后一片标记的约定见
# wss_scheduler.py docstring）；调度器只保证"单 PC 单 /file 分片在途"。
#
# 集成锚点 4（背压）：enqueue() 队列深度超上限时抛 QueueFullError
# （防攻击者塞爆内存，见安全审计 S1）-> 对 /cmd 回 503（可重试）；
# /file 分片可丢弃并记日志（上层有 md5/重传语义）或同样回 503，
# 由调用方决定。/file 在持续 /cmd 洪流下可能饿死（审计 S2），必要时
# 可升级为加权公平队列（可选优化）。
#
# ----------------------------------------------------------------------------
# V2 部署步骤（追加到 MERGE INSTRUCTIONS 之后执行）
# ----------------------------------------------------------------------------
# Step A: scp 三个模块文件到 VPS（与 muse-bridge.py 同目录）：
#   scp ~/workspace/muse-vps-deploy/vps-auth-patch/{fast_auth.py,dedup.py,wss_scheduler.py} \
#       root@vps:/usr/local/bin/
#   （PATCH_DIR 模式：也可先拷到 /tmp/bridge-auth-impl，再 cp 到 /usr/local/bin/）
# Step B: 在 muse-bridge.py 顶部 import 区追加三行 import（锚点 V2-A/B/C-1）。
# Step C: 按 V2-A-2 替换 PATCH 1 的 verify_controller_token 调用
#         （或保留旧调用仅新增引擎——二选一，不要双重鉴权）。
# Step D: 按 V2-B-2 在 /cmd 处理器插入去重逻辑；按 V2-B-3 在 WSS 关闭
#         回调插入 mark_uncertain_for_target。
# Step E: 按 V2-C-2/3/4 接入 WSS 发送泵与背压处理。
# Step F: 语法检查：
#   python3 -m py_compile /usr/local/bin/muse-bridge.py \
#       /usr/local/bin/fast_auth.py /usr/local/bin/dedup.py \
#       /usr/local/bin/wss_scheduler.py
# Step G: systemctl restart muse-bridge（或等效），验证：
#         冒烟 1) /cmd 正常 200；
#         2) 同 request_id 重发命中 COMPLETED 缓存（第二次返回极快）；
#         3) 断开某 PC 的 WSS 后其 IN_FLIGHT 请求转 504 UNCERTAIN；
#         4) /health 的 "v2" 块（如已实现）含 fastauth/dedup/wss 计数器。
# 回滚：删掉三个文件 + 还原 muse-bridge.py 备份
#   （deploy-auth-patch.sh --rollback）。
# 注意：deploy-auth-patch.sh 当前只合并 auth_patch.py + dispatch_patch.py；
# V2 的 B–E 步为手工锚点操作（或后续扩展脚本支持），不要误以为跑一遍
# 脚本就自动带上了 v2 模块。

# ============================================================================
# MERGE INSTRUCTIONS - EXACT LINE NUMBERS
# ============================================================================
"""
File: /usr/local/bin/muse-bridge.py (637 lines)

Step 1: Apply auth_patch.py first (globals + 8 functions).

Step 2: In handle_connection(), find this block (~line 454-460):
        token = (
            headers.get("x-bridge-token")
            or headers.get("authorization", "").replace("Bearer ", "").strip()
            or body_json.get("token", [""])[0]
        )
        ...
        if not check_auth(token):
            await send_json({"ok": False, "error": "Unauthorized: invalid bridge token"}, 401)
            return

        Replace with PATCH 1 above.

Step 3: In /cmd handler (~line 477), find:
            target = body_json.get("target", "") or None
        Insert PATCH 2 immediately after.

Step 4: In /batch handler (~line 507), find:
            target = body_json.get("target", "") or None
        Insert PATCH 3 immediately after.

Step 5: In /file handler (~line 545), find:
            target = body_json.get("target", "") or None
        Insert PATCH 4 immediately after.

Step 6 (optional): Apply PATCH 5 for defense-in-depth.

Step 7: Syntax check: python3 -m py_compile /usr/local/bin/muse-bridge.py

Step 8: Create /etc/muse-bridge/controllers.json (600 root:root, dir 700),
         then: systemctl restart muse-bridge (or equivalent)

Step 9: Verify: wrong token -> 401, wrong target -> 403, corrupt
         controllers.json -> 503 for everything (fail-closed).

ROLLBACK: Keep a backup of the original file. The patches are purely additive
(new functions + new checks). Removing the controller_auth checks reverts to
legacy behavior (global token only).
"""
