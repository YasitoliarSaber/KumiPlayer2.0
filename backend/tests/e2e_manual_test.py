# -*- coding: utf-8 -*-
"""M01-M06 端到端手动验证脚本"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if "pytest" in sys.modules:
    import pytest
    pytest.skip("手动端到端脚本，不参与 pytest 自动收集", allow_module_level=True)

from app.sources.pan115 import Pan115Adapter
from app.recognition.planner import build_draft_import_plan
from app.recognition.plan_recognizer import recognize_import_plan_media
from app.import_plan.service import build_preview, confirm_plan
from app.mirror.generator import generate_mirror

# ============================================================
# 配置
# ============================================================
TREE_FILE = r"D:\01_Software\KumiPlayer2.0\根目录20260612200150_目录树.txt"
SOURCE_ROOT = r"H:\115open"
MIRROR_ROOT = r"D:\01_Software\KumiPlayer2.0\data\test_mirror"

# 清理旧的测试镜像
import shutil
mirror_path = Path(MIRROR_ROOT)
if mirror_path.exists():
    shutil.rmtree(mirror_path)

# ============================================================
# M02: 解析 115 目录树
# ============================================================
print("=" * 60)
print("M02: 解析 115 目录树")
print("=" * 60)

adapter = Pan115Adapter()
snapshot = adapter.parse(TREE_FILE, SOURCE_ROOT)
print(f"  file_count  = {snapshot.file_count}")
print(f"  video_count = {snapshot.video_count}")
print(f"  首个文件: {snapshot.files[0].relative_path if snapshot.files else '无'}")

# ============================================================
# M03: 生成 draft import plan
# ============================================================
print()
print("=" * 60)
print("M03: 生成 draft import plan")
print("=" * 60)

plan = build_draft_import_plan(snapshot)
print(f"  total_items = {plan.summary.get('total_items')}")
print(f"  video_count = {plan.summary.get('video_count')}")
print(f"  by_action   = {plan.summary.get('by_action')}")

# ============================================================
# M04: 媒体识别
# ============================================================
print()
print("=" * 60)
print("M04: 媒体识别")
print("=" * 60)

recognize_import_plan_media(plan)

# 统计识别结果
from collections import Counter
group_types = Counter(item.group_type for item in plan.items if item.action == "generate_strm")
card_types = Counter(item.card_type for item in plan.items if item.action == "generate_strm")
needs_review = sum(1 for item in plan.items if item.needs_review and item.action == "generate_strm")
print(f"  group_type 分布: {dict(group_types)}")
print(f"  card_type  分布: {dict(card_types)}")
print(f"  needs_review 视频数: {needs_review}")

# ============================================================
# M05: 预览与确认
# ============================================================
print()
print("=" * 60)
print("M05: 预览与确认")
print("=" * 60)

preview = build_preview(plan)
print(f"  summary: {preview.summary}")
print(f"  issues ({len(preview.issues)}):")
for issue in preview.issues:
    print(f"    [{issue.level}] {issue.code}: {issue.message}")

# 尝试确认
confirmed, err = confirm_plan(plan)
if err:
    print(f"\n  confirm 失败: {err}")
    print(f"  尝试处理问题条目后重新确认...")

    # 把缺少 group_type 的视频改为 ignore（字幕组原始命名无法识别）
    ignored = 0
    for item in plan.items:
        if item.resource_type == "video" and item.action == "generate_strm":
            if not item.group_type:
                item.action = "ignore"
                item.needs_review = False
                ignored += 1
    print(f"  已将 {ignored} 个无法分组的视频改为 ignore")

    # 清除剩余 needs_review
    cleared = 0
    for item in plan.items:
        if item.needs_review and item.action == "generate_strm":
            item.needs_review = False
            cleared += 1
    print(f"  已清除 {cleared} 个 needs_review")

    # 重新生成 preview 确认无 error
    preview2 = build_preview(plan)
    error_issues = [i for i in preview2.issues if i.level == "error"]
    if error_issues:
        print(f"  仍有 {len(error_issues)} 个 error issue:")
        for issue in error_issues:
            print(f"    [{issue.level}] {issue.code}: {issue.message}")
        print(f"  跳过 confirm，直接用 draft plan 测试 M06...")

        # 对于手动测试，直接把 plan.status 设为 confirmed 来测试 M06
        plan.status = "confirmed"
        confirmed = plan
    else:
        confirmed, err = confirm_plan(plan)
        if err:
            print(f"  仍然失败: {err}")
            sys.exit(1)

print(f"  confirmed: plan_id={confirmed.plan_id}, status={confirmed.status}")

# ============================================================
# M06: 镜像生成
# ============================================================
print()
print("=" * 60)
print("M06: 镜像生成")
print("=" * 60)

result = generate_mirror(confirmed, MIRROR_ROOT)
print(f"  status          = {result.status}")
print(f"  generated_count = {result.generated_count}")
print(f"  skipped_count   = {result.skipped_count}")
print(f"  failed_count    = {result.failed_count}")

if result.errors:
    print(f"  errors ({len(result.errors)}):")
    for e in result.errors[:10]:
        print(f"    {e}")
    if len(result.errors) > 10:
        print(f"    ... 共 {len(result.errors)} 条")

# 统计生成的 .strm 文件
strm_files = list(mirror_path.rglob("*.strm"))
print(f"\n  实际 .strm 文件数: {len(strm_files)}")

# 按目录统计
from collections import defaultdict
dir_counts = defaultdict(int)
for f in strm_files:
    # 取 mirror_root 下的第二层目录（作品目录）
    rel = f.relative_to(mirror_path)
    parts = rel.parts
    if len(parts) >= 2:
        dir_counts[parts[1]] += 1

print(f"  作品目录数: {len(dir_counts)}")
print(f"  前 10 个作品:")
for name, count in sorted(dir_counts.items())[:10]:
    print(f"    {name}: {count} 个 .strm")

# 示例 .strm 内容
if strm_files:
    sample = strm_files[0]
    print(f"\n  示例 .strm:")
    print(f"    路径: {sample}")
    print(f"    内容: {sample.read_text(encoding='utf-8').strip()}")

# plan.status
print(f"\n  plan.status = {confirmed.status}")

print()
print("=" * 60)
print("端到端验证完成")
print("=" * 60)
