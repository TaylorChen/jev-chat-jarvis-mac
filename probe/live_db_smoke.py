#!/usr/bin/env python3
"""probe/live_db_smoke.py — live 直读数据源的一键验证（全部自动判定，退出码即结论）

需要微信在跑、已用 wcdb-key-tool 提取密钥（见 src/live_db.py 顶部）。只读打开
加密库，不写任何微信文件。覆盖：库定位/密钥/会话列表/实时性/比快照新/
消息结构（我+对方）/快照路径不回归/判断引擎就绪。

Run: python -B probe/live_db_smoke.py     （仓库根目录）
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from live_db import LiveProvider       # noqa: E402
from history import HistoryProvider    # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))


p = LiveProvider()
snap = HistoryProvider()

# 1) live 库定位
check("1. live db_storage 定位", p.live_dir is not None and p.live_dir.exists(),
      str(p.live_dir))

# 2) 密钥加载
check("2. 密钥加载（all_keys.json）", len(p.keys) >= 20, f"{len(p.keys)} 个库")

# 3) 会话列表（live）
convs = p.conversations(30)
check("3. 实时会话列表", len(convs) >= 20, f"{len(convs)} 个会话")

# 4) 实时性：live 读数必须比昨天的快照新（这才是「直读实时库」的不变量）
#    「最新消息距现在 < 10 分钟」曾经过严：账号安静半小时就假失败（实测踩到）。
newest, newest_c = 0, ""
for c in convs[:15]:
    for m in p.messages(c["username"], limit=1):
        if m["ts"] > newest:
            newest, newest_c = m["ts"], c["display"]
snap_newest, snap_conv_name = 0, ""
for c in snap.conversations(30):
    for m in snap.messages(c["username"], limit=1):
        if m["ts"] > snap_newest:
            snap_newest, snap_conv_name = m["ts"], c["display"]
age_min = (time.time() - newest) / 60
fresh = newest > snap_newest
note = (f"最近 {age_min:.1f} 分钟内有新消息" if age_min < 10
        else f"最近 {age_min:.1f} 分钟没有新消息（账号安静，不代表读取有问题）")
check("4. 实时性（比昨天的快照新）", fresh,
      f"「{newest_c}」最新 {datetime.fromtimestamp(newest):%m-%d %H:%M} · {note}；"
      f"快照最新 {datetime.fromtimestamp(snap_newest):%m-%d %H:%M}"
      + (f"（{snap_conv_name}）" if snap_conv_name else ""))

# 5) 实时 vs 快照：live 计数应 ≥ 快照计数（微信在持续收消息）
# live 计数逐会话跑 sqlcipher 子进程，只对最近 15 个会话算（会话列表本身不传 with_counts）
top15 = p.conversations(15, with_counts=True)
counted = {c["username"]: c["msg_count"] for c in top15}
snap_conv = {c["username"]: c["msg_count"] for c in snap.conversations(60)}
grew = [(u, snap_conv[u], counted[u])
        for u in snap_conv if counted.get(u) is not None and counted[u] > snap_conv[u]]
check("5. live 数据比昨天的快照新（有会话消息增长）", len(grew) > 0,
      f"{len(grew)} 个会话有增长" +
      (f"，例: {grew[0][0][:16]}… {grew[0][1]}→{grew[0][2]}" if grew else ""))

# 6) 消息结构完整性：找一个「我」和「对方」都发言过的会话验证方向与时间序。
#    候选来自**快照**：快照是静态的，谁是「双方都说过话的会话」不会随当前活跃度变化；
#    早先按 live 最活跃的 15 个会话取样，热门群一换就找不到自己发过言的会话（假失败）。
found6, detail6 = False, "快照里没找到双方都发言过的会话"
target = None
for c in snap.conversations(80):
    if c["category"] == "公众号" or c["msg_count"] < 20:
        continue
    sample = snap.messages(c["username"], limit=200)
    if sample and {m["who"] for m in sample} == {"我", "对方"}:
        target = c
        break
if target:
    m6 = p.messages(target["username"], limit=200)
    ts_ok = all(a["ts"] <= b["ts"] for a, b in zip(m6, m6[1:]))
    who = {m["who"] for m in m6}
    found6 = bool(m6) and ts_ok and who == {"我", "对方"}
    detail6 = (f"「{target['display']}」快照双方都发言 → live {len(m6)} 条："
               f"方向 {sorted(who)}、时间递增 {ts_ok}")
    if not m6:
        detail6 += "（live 分片里读不到该会话：可能已被微信归档出当前分片）"
check("6. 消息结构（找双方都发言的会话）", found6, detail6)

# 7) 快照路径不回归：HistoryProvider 仍能读（动态选一个有消息的会话）
snap_msgs = []
for c in snap.conversations(60):
    snap_msgs = snap.messages(c["username"], limit=50)
    if snap_msgs:
        break
check("7. 快照路径（HistoryProvider）不回归", len(snap_msgs) > 0,
      f"快照消息 {len(snap_msgs)} 条")

# 8) Intent 引擎自动选择（不实际调用 API）
import judge as J
j = J.make_judge()
cloud = type(j).__name__ == "FallbackJudge"
check("8. 判断引擎就绪", True, f"{'Jev 云端（TYPESAFE key）' if cloud else '本地 decider-2b'}")

print()
fails = sum(1 for _, ok, _ in results if not ok)
print(f"共 {len(results)} 项，{'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(0 if fails == 0 else 1)
