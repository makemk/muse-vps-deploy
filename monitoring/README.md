# monitoring — 轻量监控告警

Python 标准库，cron 每分钟跑，无外部依赖。

## 文件

| 文件 | 说明 |
|------|------|
| `alert.py` | 告警库：防抖状态机、写 alerts.log、调 hook |
| `vm_monitor.py` | VM 端：mused 进程/socket/内存/延迟/错误率 |
| `vps_monitor.py` | VPS 端：bridge 进程/401/403 突增/PC 在线/堆积 |
| `v2_metrics.py` | **v2 组件指标**：连接池/FastAuth/微批/压缩/去重/WSS 调度；`--side vm|vps|local`，`--check-alerts` |
| `alert_hook.sh.example` | 告警 hook 示例（syslog + webhook 模板） |
| `health_endpoint_spec.md` | daemon `health`/`stats` + VPS `/muse-api/health` 实现规格 |

## 快速验证（不部署）

```bash
cd ~/workspace/muse-vps-deploy/monitoring
python3 -m py_compile alert.py vm_monitor.py vps_monitor.py && echo OK
# 跑 VM monitor（daemon 没起时会报 daemon_down，属预期）
python3 vm_monitor.py; echo "exit=$?"
cat ~/.local/share/mused/alerts.log 2>/dev/null | tail -3
```

## 部署

见 `../MONITORING.md` 第六节。

## 注意

- `vm_monitor.py` 在 daemon 未实现 `health`/`stats` op 时会自动降级（只做进程+socket 连通性检查），不报错。
- `vps_monitor.py` 同理，`/muse-api/health` 不存在时跳过 API 指标。
- 两个脚本互相独立，一边挂了不影响另一边。
