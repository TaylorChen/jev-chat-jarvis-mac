#!/usr/bin/env python3
"""history_analyze.py — 历史会话分析：解密库直读，jev 判断 + LLM 分析（不走 OCR）。

把 jev-chat-jarvis 的两层能力（本地 decider-2b 意图/风险判断、LLM 生成）从
「看屏幕一瞬」扩展到「整个聊天历史」。数据源是 wcdb-key-tool 解密出的本地库，
凭据与生成层共用一套（OPENAI_*/ANTHROPIC_*，或 Ollama 全本地）。

用法：
  uv run --with zstandard python src/history_analyze.py list [--top 15]
  uv run --with zstandard python src/history_analyze.py summary "张三" [--days 30] [--last 200]
  uv run --with zstandard python src/history_analyze.py intent  "张三" [--days 90] [--sample 50]
  uv run --with zstandard python src/history_analyze.py reply   "张三"

  summary  议题/结论/承诺与待办/风险 + 建议下一步（LLM，走生成层凭据）
  intent   对对方消息抽样跑本地意图/风险判断，输出分布（完全本地，不出网）
  reply    找对方最后一条未回复消息，按当前话术生成候选回复（同悬浮窗能力）
目标可以是备注/昵称/群名/username；撞名时会列出候选，用 --pick 序号选择。
隐私：intent 全本地；summary/reply 的对话文本会发给生成层配置的 LLM 服务。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from history import HistoryProvider  # noqa: E402

TRANSCRIPT_CHAR_CAP = 14000


def _provider(args) -> HistoryProvider:
    return HistoryProvider(args.db_dir, args.self_wxid)


def _resolve(p: HistoryProvider, target: str, pick: int) -> str | None:
    """显示名/username → username；撞名列出候选。"""
    if not target:
        return None
    if p._tables_for(target):          # 直接是 username
        return target
    cands = p._candidates_by_title(target)
    if not cands:
        print(f"找不到「{target}」（试备注/昵称/群名，或 username）")
        return None
    if len(cands) == 1 or pick:
        idx = (pick or 1) - 1
        if idx >= len(cands):
            print(f"--pick {pick} 超出范围（共 {len(cands)} 个候选）")
            return None
        return cands[idx]
    print(f"「{target}」有 {len(cands)} 个同名候选：")
    for i, u in enumerate(cands, 1):
        print(f"  [{i}] {u}  显示名: {p.display_name(u)}")
    print("用 --pick 序号选择。")
    return None


def cmd_list(p: HistoryProvider, args) -> int:
    # with_counts 对 live 是逐会话跑 sqlcipher，几十个会话并发数约一两秒；CLI 付得起，
    # 而会话列表没有条数就没法判断哪个会话值得分析。快照库忽略该参数（本来就计数）。
    convs = p.conversations(args.top, with_counts=True)
    print(f"{'#':>3}  {'消息数':>6}  {'最后活跃':<17}  会话")
    for i, c in enumerate(convs, 1):
        ts = datetime.fromtimestamp(c["sort_ts"]).strftime("%m-%d %H:%M") if c["sort_ts"] else "?"
        print(f"{i:>3}  {c['msg_count']:>6}  {ts:<17}  {c['display']}"
              + (f"  ── {c['summary']}" if c["summary"] else ""))
    print("\n用显示名或 username 跑 summary / intent / reply。")
    return 0


def _transcript(p: HistoryProvider, username: str, days: int | None, last: int) -> str:
    msgs = p.messages(username, days=days, limit=last)
    if not msgs:
        return ""
    parts, total = [], 0
    for m in msgs:
        line = f"{m['who']}({m['name']}) {datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}: {m['text']}"
        parts.append(line)
        total += len(line)
        if total > TRANSCRIPT_CHAR_CAP:
            parts.append("……（更早内容已截断）")
            break
    return "\n".join(parts)


def cmd_summary(p: HistoryProvider, g, args) -> int:
    username = _resolve(p, args.target, args.pick)
    if not username:
        return 2
    tr = _transcript(p, username, args.days, args.last)
    if not tr:
        print("该会话没有可分析的文本消息。")
        return 1
    print(f"分析 {p.display_name(username)} 的最近对话（{tr.count(chr(10)) + 1} 条），LLM 分析中…")
    prompt = (
        "你是聊天记录分析助手。以下是某人与我的最近对话（时间正序，已标注 我/对方）：\n\n"
        f"{tr}\n\n请用 Markdown 输出：\n"
        "## 议题概览（3-5 条）\n## 已达成结论/决定\n"
        "## 承诺与待办（谁欠谁什么，尽量引用原话）\n"
        "## 风险或需要留意的话\n## 建议的下一步\n")
    t0 = time.perf_counter()
    out = g._call(prompt)
    print(out or "(LLM 返回为空)")
    print(f"\n[{time.perf_counter() - t0:.1f}s]")
    return 0


def cmd_intent(p: HistoryProvider, args) -> int:
    from judge import make_judge
    username = _resolve(p, args.target, args.pick)
    if not username:
        return 2
    all_msgs = p.messages(username, days=args.days, limit=2000)
    msgs = [m for m in all_msgs if m["who"] == "对方"]
    if not msgs:
        print(f"该会话共 {len(all_msgs)} 条消息，其中对方文本消息 0 条"
              f"（其余为图片/语音/系统消息，或均为自己发送）。")
        return 1
    step = max(1, len(msgs) // args.sample)
    sample = msgs[::step][:args.sample]
    judge = make_judge()
    cloud = type(judge).__name__ == "FallbackJudge"   # Jev 云端；否则本地 decider-2b
    print(f"对 {p.display_name(username)} 的对方消息抽样 {len(sample)}/{len(msgs)} 条，"
          f"{'Jev 云端判断中…' if cloud else '本地模型判断中（首次加载模型要一会儿）…'}")
    judge.warm()
    dist: dict[str, int] = {}
    risks = []
    for i, m in enumerate(sample, 1):
        try:
            v = judge.judge(m["text"])
        except Exception as e:
            print(f"\n判断失败: {e}")
            return 1
        dist[v["intent"]] = dist.get(v["intent"], 0) + 1
        risks.append((v["risk"], m))
        print(f"\r  {i}/{len(sample)}", end="", flush=True)
    print()
    n = len(sample)
    print(f"\n意图分布（{p.display_name(username)} → 我，抽样 {n} 条）：")
    for intent, c in sorted(dist.items(), key=lambda kv: -kv[1]):
        bar = "█" * round(c / n * 30)
        print(f"  {intent:<12} {c:>4}  {c / n * 100:5.1f}%  {bar}")
    risks.sort(key=lambda kv: -kv[0])
    print("\n风险最高的 3 条（仅提示，不展示原文）：")
    for risk, m in risks[:3]:
        when = datetime.fromtimestamp(m["ts"]).strftime("%m-%d")
        print(f"  风险 {risk:>4}  {when}  {len(m['text'])} 字")
    print("（判断引擎: " + ("Jev 云端 — 对方消息已发送到 Typesafe 接口分析"
                           if cloud else "本地 decider-2b，消息未出网") + "）")
    return 0


def cmd_reply(p: HistoryProvider, args) -> int:
    from generate import Generator
    from judge import make_judge
    username = _resolve(p, args.target, args.pick)
    if not username:
        return 2
    msgs = p.messages(username, days=args.days, limit=500)
    last_idx, last_their = None, None
    for i, m in enumerate(msgs):
        if m["who"] == "对方":
            last_idx, last_their = i, m
    if not last_their:
        print("没找到对方的消息。")
        return 1
    pending = all(m["who"] != "我" for m in msgs[last_idx + 1:])
    when = datetime.fromtimestamp(last_their["ts"]).strftime("%m-%d %H:%M")
    print(f"对方最后一条消息：{when}（{'尚未回复' if pending else '之后你已回复'}，"
          f"{len(last_their['text'])} 字）")
    context = "\n".join(f"{m['who']}({m['name']}): {m['text']}" for m in msgs[-10:])
    intent = ""
    try:
        verdict = make_judge().judge(last_their["text"])
        intent = verdict["intent"]
        print(f"本地判断: 意图={intent} 置信={verdict['confidence']:.2f} 风险={verdict['risk']}")
    except Exception as e:
        print(f"本地判断跳过（{e}），直接生成")
    print("生成候选回复…")
    gen = Generator().generate(last_their["text"], intent, None, context)
    for grp in gen.get("groups", []):
        print(f"\n【{grp.get('tone', '?')}】")
        for t in grp.get("texts", []):
            print(f"  · {t}")
    if gen.get("error"):
        print(f"生成失败: {gen['error']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="历史会话分析（解密库直读 + jev/LLM）")
    ap.add_argument("--db-dir", default=None, help="解密快照目录（--live 关闭时使用）")
    ap.add_argument("--live", action="store_true", help="直读微信 live 加密库（实时数据）")
    ap.add_argument("--self", dest="self_wxid", default=None,
                    help="自己的 wxid（不填则从解密目录名自动推断）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="最近活跃会话")
    lp.add_argument("--top", type=int, default=15)

    def target_parser(sp, days_default=None):
        sp.add_argument("target", help="备注/昵称/群名/username")
        sp.add_argument("--pick", type=int, default=0, help="撞名时选第几个候选")
        sp.add_argument("--days", type=int, default=days_default, help="只看最近 N 天")

    sp = sub.add_parser("summary", help="LLM 会话总结（议题/结论/承诺待办/风险）")
    target_parser(sp)
    sp.add_argument("--last", type=int, default=200, help="最多取最近多少条")

    sp = sub.add_parser("intent", help="对方消息意图分布（本地判断）")
    target_parser(sp)
    sp.add_argument("--sample", type=int, default=50, help="抽样条数")

    sp = sub.add_parser("reply", help="对方最后未回复消息 → 候选回复")
    target_parser(sp, days_default=30)

    args = ap.parse_args()
    if args.live:
        from live_db import LiveProvider
        p = LiveProvider()
    else:
        p = _provider(args)
    if args.cmd == "list":
        return cmd_list(p, args)
    if args.cmd == "summary":
        from generate import Generator
        return cmd_summary(p, Generator(), args)
    if args.cmd == "intent":
        return cmd_intent(p, args)
    if args.cmd == "reply":
        return cmd_reply(p, args)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
