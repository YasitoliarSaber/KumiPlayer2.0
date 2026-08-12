#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档 Proma 已完成委派子会话脚本。

用法（项目根目录执行）：
    python scripts/archive_delegations.py                 # 归档当前父会话下所有已完成委派
    python scripts/archive_delegations.py --task 任务B     # 只归档标题以"任务B"开头的已完成委派
    python scripts/archive_delegations.py --dry-run        # 只预览要归档哪些，不实际修改

安全规则：
- 本脚本是主线程工作流「验收通过 → 自动归档」闭环的收尾工具，由主线程在验收无误后调用；不需要用户手动触发。
- 只处理当前父会话（parentSessionId == 本脚本内 PARENT_SESSION）下的委派子会话。
- 只处理 delegationStatus == completed 的子会话；运行中/失败/其他状态一律跳过。
- 不处理非委派主会话（没有 sourceDelegationId 的会话不动）。
- 修改前自动备份原文件到 agent-sessions.json.bak，可随时恢复。
- 只把 archived 置为 true（归档），不删除任何记录；归档后可复原。

注意：归档是修改 Proma 的会话状态文件；若界面未立即刷新，请重启 Proma 或等待界面重载。
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

AGENT_SESSIONS_PATH = Path.home() / ".proma" / "agent-sessions.json"
# 当前主会话：只归档该会话委派出去的子代理，绝不归档主会话本身或其他会话
PARENT_SESSION = "a1d0a324-e961-4674-b4d5-235acac0aeac"


def load_sessions():
    if not AGENT_SESSIONS_PATH.is_file():
        sys.exit(f"未找到会话文件: {AGENT_SESSIONS_PATH}")
    with open(AGENT_SESSIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_sessions(data, backup=True):
    if backup:
        bak = AGENT_SESSIONS_PATH.with_suffix(".json.bak")
        shutil.copy2(AGENT_SESSIONS_PATH, bak)
        print(f"[备份] 已备份原文件到: {bak}")
    with open(AGENT_SESSIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[写入] 已更新: {AGENT_SESSIONS_PATH}")


def main():
    parser = argparse.ArgumentParser(description="归档 Proma 已完成委派子会话")
    parser.add_argument("--task", default="", help="只归档标题以该前缀开头的已完成委派（如 任务B）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改文件")
    args = parser.parse_args()

    data = load_sessions()
    sessions = data.get("sessions", [])

    candidates = []
    skipped_running = []
    skipped_other_parent = []
    skipped_not_delegation = []

    for s in sessions:
        is_delegation = bool(s.get("sourceDelegationId"))
        if not is_delegation:
            skipped_not_delegation.append(s.get("title", ""))
            continue
        if s.get("parentSessionId") != PARENT_SESSION:
            skipped_other_parent.append(s.get("title", ""))
            continue
        status = s.get("delegationStatus")
        if status != "completed":
            skipped_running.append((s.get("title", ""), status))
            continue
        title = s.get("title", "")
        if args.task and not title.startswith(args.task):
            continue
        if s.get("archived"):
            continue  # 已归档，跳过
        candidates.append(s)

    print(f"[扫描] 当前父会话下的委派子会话共 {len([s for s in sessions if s.get('sourceDelegationId') and s.get('parentSessionId') == PARENT_SESSION])} 个")
    print(f"[匹配] 本次将归档 {len(candidates)} 个已完成委派")
    if candidates:
        for s in candidates:
            print(f"  - {s.get('title', '')} (status={s.get('delegationStatus')})")
    if skipped_running:
        print(f"[跳过] 未完成委派 {len(skipped_running)} 个（绝不动）:")
        for title, status in skipped_running:
            print(f"  - {title} (status={status})")
    if skipped_other_parent:
        print(f"[跳过] 其他父会话委派 {len(skipped_other_parent)} 个:")
        for t in skipped_other_parent:
            print(f"  - {t}")
    if skipped_not_delegation:
        print(f"[跳过] 非委派会话 {len(skipped_not_delegation)} 个（主会话/普通会话不动）")

    if args.dry_run:
        print("\n[预览] dry-run 模式，未做任何修改。")
        return

    if not candidates:
        print("[结果] 没有需要归档的委派，未做修改。")
        return

    for s in candidates:
        s["archived"] = True
        s["archivedAt"] = datetime.now().isoformat(timespec="seconds")

    save_sessions(data, backup=True)
    print(f"[完成] 已归档 {len(candidates)} 个已完成委派。")


if __name__ == "__main__":
    main()
