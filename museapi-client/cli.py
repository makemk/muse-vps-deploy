#!/usr/bin/env python3
"""musecli: drop-in HTTPS replacement for `musepc` (no SSH).

Usage mirrors musepc:
    musecli <command>                          Execute in Git Bash on the PC
    musecli --target HOST <command>            Route to a specific PC
    musecli --cwd <dir> <command>              Execute in a working directory
    musecli --timeout <sec> <command>          Command timeout (default 60)
    musecli --status                           PC connection status
    musecli --read <path>                      Read file from PC
    musecli --slice <path> <off> <count>       Read line slice
    musecli --write <path> <content>           Write string to file on PC
    musecli --edit <path> <old> <new>          Replace string in file
    musecli --glob <pattern> [path]            Glob search on PC
    musecli --grep <pattern> [path]            Grep search on PC
    musecli --ls <path>                        List directory on PC
    musecli --archive <path> [extra...]        7z archive on PC
    musecli --extract <path> [out_dir]         Extract archive on PC
    musecli --batch <c1> ;; <c2>               Batch commands (split on ;;)
    musecli --json <command>                   Print raw JSON response

Defaults: target from $MUSEPC_TARGET or "RD-928" (same as musepc wrapper).
Config: ~/.config/mused/config.json ; token: ~/.config/mused/token (600).

Exit codes: 0 ok, 1 remote command failed, 2 usage error, 3 auth/network error.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from museapi import (MuseAPIClient, MuseAPIError, AuthError, ForbiddenError,
                     NotFoundError, ConflictError, ServerError, NetworkError,
                     TimeoutError)

DEFAULT_TARGET = os.environ.get("MUSEPC_TARGET", "RD-928")

# musepc-style op flags, in the order they must be detected.
OP_FLAGS = ("--status", "--read", "--slice", "--write", "--edit", "--glob",
            "--grep", "--ls", "--archive", "--extract", "--batch")
GLOBAL_OPTS = ("--target", "--cwd", "--timeout", "--config", "--token-file")


def parse_musepc_args(argv):
    """Parse musepc-style args.

    Returns (globals_dict, op, op_args, command_tokens).
    - globals_dict: target/cwd/timeout/json/config/token_file
    - op: one of OP_FLAGS (without --), or "exec" for a bare command
    - op_args: tokens after the op flag
    - command_tokens: for "exec", the shell command tokens
    """
    g = {"target": DEFAULT_TARGET, "cwd": "", "timeout": 60,
         "json": False, "config": None, "token_file": None}
    op = None
    op_args = []
    cmd_tokens = []

    i = 0
    # Phase 1: globals and op flag, in order, until op flag or bare command.
    while i < len(argv):
        a = argv[i]
        if a in OP_FLAGS:
            op = a[2:]  # strip --
            op_args = argv[i + 1:]
            break
        if a in GLOBAL_OPTS:
            if i + 1 >= len(argv):
                raise ValueError(f"{a} needs a value")
            key = a[2:].replace("-", "_")
            val = argv[i + 1]
            g[key] = int(val) if key == "timeout" else val
            i += 2
            continue
        if a == "--json":
            g["json"] = True
            i += 1
            continue
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        # Anything else starts the bare command.
        op = "exec"
        cmd_tokens = argv[i:]
        break

    if op is None:
        raise ValueError("no command given")
    return g, op, op_args, cmd_tokens


def _print_result(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if isinstance(data, dict):
        if data.get("ok") is False:
            print(f"ERROR: {data.get('error', 'unknown error')}",
                  file=sys.stderr)
            return 1
        if "stdout" in data:  # command result
            out = data["stdout"] or ""
            if out:
                sys.stdout.write(out if out.endswith("\n") else out + "\n")
            err = data.get("stderr", "") or ""
            if err:
                sys.stderr.write(err if err.endswith("\n") else err + "\n")
            return 0 if data.get("exit_code", 0) == 0 else 1
        if "content" in data:  # file read
            sys.stdout.write(data["content"])
            return 0
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(data)
    return 0


def run(g, op, op_args, cmd_tokens):
    overrides = {}
    if g["token_file"]:
        overrides["token_file"] = g["token_file"]
    target = g["target"]
    timeout = g["timeout"]
    as_json = g["json"]

    try:
        client = MuseAPIClient(config_path=g["config"], **overrides)
    except AuthError as e:
        print(f"auth: {e}", file=sys.stderr)
        return 3

    with client:
        if op == "status":
            return _print_result(client.status(), as_json)
        if op == "exec":
            command = " ".join(cmd_tokens)
            data = client.cmd(target, command, cwd=g["cwd"], timeout=timeout)
            return _print_result(data, as_json)
        if op == "read":
            _need(op_args, 1, "path")
            return _print_result(
                client.read_file(target, op_args[0], timeout=timeout), as_json)
        if op == "slice":
            _need(op_args, 3, "path offset count")
            data = client.read_file(target, op_args[0], timeout=timeout)
            if isinstance(data, dict) and "content" in data:
                lines = data["content"].splitlines()
                off, cnt = int(op_args[1]), int(op_args[2])
                print("\n".join(lines[off:off + cnt]))
                return 0
            return _print_result(data, as_json)
        if op == "write":
            _need(op_args, 2, "path content")
            return _print_result(
                client.write_file(target, op_args[0], op_args[1],
                                  timeout=timeout), as_json)
        if op == "edit":
            _need(op_args, 3, "path old new")
            return _print_result(
                client.edit_file(target, op_args[0], op_args[1], op_args[2],
                                 timeout=timeout), as_json)
        if op == "glob":
            _need(op_args, 1, "pattern [path]")
            return _print_result(
                client.glob(target, op_args[0],
                            op_args[1] if len(op_args) > 1 else "",
                            timeout=timeout), as_json)
        if op == "grep":
            _need(op_args, 1, "pattern [path]")
            return _print_result(
                client.grep(target, op_args[0],
                            op_args[1] if len(op_args) > 1 else "",
                            timeout=timeout), as_json)
        if op == "ls":
            _need(op_args, 1, "path")
            return _print_result(
                client.ls(target, op_args[0], timeout=timeout), as_json)
        if op == "archive":
            _need(op_args, 1, "path [extra...]")
            return _print_result(
                client.archive(target, op_args[0], *op_args[1:],
                               timeout=timeout), as_json)
        if op == "extract":
            _need(op_args, 1, "path [out_dir]")
            return _print_result(
                client.extract(target, op_args[0],
                               op_args[1] if len(op_args) > 1 else "",
                               timeout=timeout), as_json)
        if op == "batch":
            raw = " ".join(op_args)
            cmds = [c.strip() for c in raw.split(";;") if c.strip()]
            if not cmds:
                raise ValueError("--batch needs at least one command")
            return _print_result(
                client.batch(target, cmds, timeout=timeout), as_json)
        raise ValueError(f"unknown op: {op}")


def _need(args, n, what):
    if len(args) < n:
        raise ValueError(f"need: {what}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        g, op, op_args, cmd_tokens = parse_musepc_args(argv)
    except ValueError as e:
        print(f"usage: {e}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    try:
        return run(g, op, op_args, cmd_tokens)
    except (AuthError, ForbiddenError) as e:
        print(f"auth: {e}", file=sys.stderr)
        return 3
    except NotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 1
    except ConflictError as e:
        print(f"conflict: {e}", file=sys.stderr)
        return 1
    except TimeoutError as e:
        print(f"timeout: {e}", file=sys.stderr)
        return 3
    except (NetworkError, ServerError, MuseAPIError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1 if isinstance(e, ServerError) else 3
    except ValueError as e:
        print(f"usage: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
