# -*- coding: utf-8 -*-
"""用真实 TXT 目录树文件名跑识别器，验证当前识别链路是否正确。

覆盖两个 TXT 的典型文件名/目录名模式。
filename = 纯文件名（不含目录前缀）
relative_path = 完整相对路径（含目录前缀）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.recognition.media import recognize_media


def _sample(filename, relative_path, source="pan115"):
    return (filename, relative_path, source)


# ===== 文件1: 01动画目录（标准 SxxExx + 中文）=====
file1_samples = [
    _sample("石纪元 - S01E01 - 石之世界.mkv", "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Season 1/石纪元 - S01E01 - 石之世界.mkv"),
    _sample("石纪元 - S03E22 - 跨越新世界.mkv", "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Season 3/石纪元 - S03E22 - 跨越新世界.mkv"),
    _sample("石纪元 - S04E01 - 龙水 VS 千空.mkv", "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Season 4/石纪元 - S04E01 - 龙水 VS 千空.mkv"),
]

# ===== 文件2: 根目录树（【】日文 + 多季子目录）=====
file2_samples = [
    # 飞跃巅峰
    _sample("【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv", "动画/飞跃巅峰 内封中字/S1/【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv"),
    _sample("【Top o Nerae2! DieBuster】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv", "动画/飞跃巅峰 内封中字/S2/【Top o Nerae2! DieBuster】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv"),
    _sample("【Top o Nerae2! DieBuster】【NCED】【BDrip】【HEVC 3840x2160p FLAC】.mkv", "动画/飞跃巅峰 内封中字/S2/【Top o Nerae2! DieBuster】【NCED】【BDrip】【HEVC 3840x2160p FLAC】.mkv"),
    _sample("【Top o Nerae2! DieBuster】【NCOP】【BDrip】【HEVC 3840x2160p FLAC】.mkv", "动画/飞跃巅峰 内封中字/S2/【Top o Nerae2! DieBuster】【NCOP】【BDrip】【HEVC 3840x2160p FLAC】.mkv"),
    # 谭雅战记
    _sample("【Youjo Senki】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv", "动画/谭雅战记 内封中字/【Youjo Senki】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv"),
    _sample("【Youjo Senki】【12】【BDrip】【HEVC 3840x2160p FLAC】.mkv", "动画/谭雅战记 内封中字/【Youjo Senki】【12】【BDrip】【HEVC 3840x2160p FLAC】.mkv"),
    # 君主·埃尔梅罗二世事件簿
    _sample("【君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note-】【01】【BDrip】【HEVC 3840x2160p PCM】.mkv", "动画/君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note/【君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note-】【01】【BDrip】【HEVC 3840x2160p PCM】.mkv"),
    _sample("【君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note-】【00】【BDrip】【HEVC 3840x2160p PCM】.mkv", "动画/君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note/【君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note-】【00】【BDrip】【HEVC 3840x2160p PCM】.mkv"),
    # 幻想嘉年华
    _sample("【Carnival Phantasm】【01】【BDrip】【HEVC 3840x2160p FLAC_AC3】.mkv", "动画/幻想嘉年华/【Carnival Phantasm】【01】【BDrip】【HEVC 3840x2160p FLAC_AC3】.mkv"),
    _sample("【Carnival Phantasm】【12】【BDrip】【HEVC 3840x2160p FLAC_AC3】.mkv", "动画/幻想嘉年华/【Carnival Phantasm】【12】【BDrip】【HEVC 3840x2160p FLAC_AC3】.mkv"),
    # 鬼灭之刃系列
    _sample("[MAI] Kimetsu no Yaiba [01][Ma10p_2160p][x265_flac_aac_ass].mkv", "动画/鬼灭之刃系列/1.立志篇.[S1].2019/[MAI] Kimetsu no Yaiba [01][Ma10p_2160p][x265_flac_aac_ass].mkv"),
    _sample("[MAI] Kimetsu no Yaiba [NCED][Ma10p_2160p][x265_flac].mkv", "动画/鬼灭之刃系列/1.立志篇.[S1].2019/[MAI] Kimetsu no Yaiba [NCED][Ma10p_2160p][x265_flac].mkv"),
    _sample("[MAI] Kimetsu no Yaiba [27][Ma10p_2160p][x265_flac_aac_sup].mkv", "动画/鬼灭之刃系列/2.无限列车篇.[S1.1].2020/TV版/[MAI] Kimetsu no Yaiba [27][Ma10p_2160p][x265_flac_aac_sup].mkv"),
    _sample("[MAI] Kimetsu no Yaiba Mugen Ressha-hen [Ma10p_2160p][x265_DTS-HDMA5.1_sup].mkv", "动画/鬼灭之刃系列/2.无限列车篇.[S1.1].2020/剧场版/[MAI] Kimetsu no Yaiba Mugen Ressha-hen [Ma10p_2160p][x265_DTS-HDMA5.1_sup].mkv"),
    _sample("[MAI] Kimetsu No Yaiba [34][Ma10p_2160P][x265_flac_sup].mkv", "动画/鬼灭之刃系列/3.游郭篇(花街篇).[S2].2021/[MAI] Kimetsu No Yaiba [34][Ma10p_2160P][x265_flac_sup].mkv"),
    _sample("[MAI] Kimetsu no Yaiba Katanakaji no Sato Hen [01][Ma10p_2160p][x265_flac_ass].mkv", "动画/鬼灭之刃系列/4.锻刀村篇.[S3].2023/[MAI] Kimetsu no Yaiba Katanakaji no Sato Hen [01][Ma10p_2160p][x265_flac_ass].mkv"),
    _sample("[MAI] Kimetsu no Yaiba S5 [01][Webrip_2160p][x265_DDp_ass].mkv", "动画/鬼灭之刃系列/5.柱训练篇.[S4].2024/[MAI] Kimetsu no Yaiba S5 [01][Webrip_2160p][x265_DDp_ass].mkv"),
    # 紫罗兰永恒花园
    _sample("[MAI] Violet Evergarden - 01 [Ma10p_2160p][x265_flac_ass].mkv", "动画/紫罗兰永恒花园.TV版+外传+剧场版/1.紫罗兰永恒花园.2018/[MAI] Violet Evergarden - 01 [Ma10p_2160p][x265_flac_ass].mkv"),
    _sample("[MAI] Violet Evergarden - 04.5(Extra Episode) [Ma10p_2160p][x265_flac_ass].mkv", "动画/紫罗兰永恒花园.TV版+外传+剧场版/1.紫罗兰永恒花园.2018/[MAI] Violet Evergarden - 04.5(Extra Episode) [Ma10p_2160p][x265_flac_ass].mkv"),
    _sample("[MAI] Violet Evergarden Side Story Gaiden Eien to Jidou Shuki Ningyou [Ma10p_1608p][x265_TureHD5.1_ass].mkv", "动画/紫罗兰永恒花园.TV版+外传+剧场版/2.外传：永远与自动手记人偶.2020/[MAI] Violet Evergarden Side Story Gaiden Eien to Jidou Shuki Ningyou [Ma10p_1608p][x265_TureHD5.1_ass].mkv"),
    _sample("[MAI] Gekijouban Violet Evergarden [Ma10p_1608p_SDR][x265_flac7.1_ass].mkv", "动画/紫罗兰永恒花园.TV版+外传+剧场版/3.剧场版.2021/[MAI] Gekijouban Violet Evergarden [Ma10p_1608p_SDR][x265_flac7.1_ass].mkv"),
    # 月色真美
    _sample("[Ygm] Tsuki ga Kirei [01][Ma10p_2160p][x265_flac_dts] .mkv", "动画/月色真美.2017/[Ygm] Tsuki ga Kirei [01][Ma10p_2160p][x265_flac_dts] .mkv"),
    _sample("[Ygm] Tsuki ga Kirei [NCED][Ma10p_2160p][x265_flac].mkv", "动画/月色真美.2017/[Ygm] Tsuki ga Kirei [NCED][Ma10p_2160p][x265_flac].mkv"),
    # 在下坂本
    _sample("[MAI] Sakamoto Desu ga [01][Ma10p_2160p][x265_flac_ass].mkv", "动画/在下坂本，有何贵干？.2016/[MAI] Sakamoto Desu ga [01][Ma10p_2160p][x265_flac_ass].mkv"),
    # 伪恋
    _sample("[01].mkv", "动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S1].2013/[01].mkv"),
    _sample("[OAD01].mkv", "动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S1].2013/[OAD01].mkv"),
    _sample("S2 [01] .mkv", "动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S2].2014/S2 [01] .mkv"),
    _sample("[NCED01].mkv", "动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S1].2013/SPs/[NCED01].mkv"),
    # 中二病也要谈恋爱
    _sample("[MAI] Chuunibyou demo Koi ga Shitai! [01][Ma10p_2160p][x265_flac_ass].mkv", "动画/中二病也要谈恋爱.S1-S2+剧场版/1.中二病也要谈恋爱.[S1].2012/[MAI] Chuunibyou demo Koi ga Shitai! [01][Ma10p_2160p][x265_flac_ass].mkv"),
    _sample("[MAI] Gekijouban Chuunibyou demo Koi ga Shitai! [Ma10p_2160p][x265_2flac_2ac3_ass].mkv", "动画/中二病也要谈恋爱.S1-S2+剧场版/2.剧场版：小鸟游六花 改.2013/[MAI] Gekijouban Chuunibyou demo Koi ga Shitai! [Ma10p_2160p][x265_2flac_2ac3_ass].mkv"),
    _sample("[MAI] Chuunibyou demo Koi ga Shitai! Ren [01][Ma10p_2160p][x265_flac_ass].mkv", "动画/中二病也要谈恋爱.S1-S2+剧场版/3.中二病也要谈恋爱.[S2].2014/[MAI] Chuunibyou demo Koi ga Shitai! Ren [01][Ma10p_2160p][x265_flac_ass].mkv"),
    _sample("[MAI] Chuunibyou demo Koi ga Shitai!  -Take On Me- [Ma10p_2160p][x265_3flac_aac_ass].mkv", "动画/中二病也要谈恋爱.S1-S2+剧场版/4.剧场版：Take On Me.2018/[MAI] Chuunibyou demo Koi ga Shitai!  -Take On Me- [Ma10p_2160p][x265_3flac_aac_ass].mkv"),
    # 辉夜大小姐
    _sample("[YE] Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen -  [01][Ma10p_2160p][x265_flac_ass].mkv", "动画/辉夜大小姐想让我告白.S1-S4+剧场版（将更新）/1.辉夜大小姐想让我告白：天才们的恋爱头脑战.[S1].2019/[YE] Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen -  [01][Ma10p_2160p][x265_flac_ass].mkv"),
]


def run_recognition(samples, label):
    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    issues = []
    for filename, relative_path, source in samples:
        guess = recognize_media(filename, relative_path=relative_path, source=source)
        title = guess.work_title or "(空)"
        season = guess.season_number
        episode = guess.episode_number
        card = guess.card_type
        group = guess.group_type
        review = guess.needs_review
        print(f"  {filename[:55]:<55} → title={title[:25]:<25} S{season}E{episode} group={group:<10} card={card} review={review}")
        if group not in ("ignored", "auxiliary") and review and ".mkv" in filename.lower():
            issues.append((filename, "mkv needs_review（非附属）"))
    return issues


all_issues = []
all_issues += run_recognition(file1_samples, "文件1: 01动画目录 (标准 SxxExx + 中文)")
all_issues += run_recognition(file2_samples, "文件2: 根目录树 (【】日文 + 多季子目录)")

print(f"\n{'='*80}")
print(f"  问题汇总 ({len(all_issues)} 个)")
print(f"{'='*80}")
for filename, issue in all_issues:
    print(f"  {issue}: {filename[:70]}")
