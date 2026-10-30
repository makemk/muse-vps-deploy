#!/usr/bin/env bash
# Expect HTTP 401 from the bridge (wrong token) -> proves the whole
# proxy-CONNECT + TLS(SNI) + HTTP chain works without SSH.
set -u
export MUSE_API_TOKEN=wrong-token
exec python3 /tmp/museapi-proto/client.py
