#!/usr/bin/env python3
"""把「用文字稿选题」这一步自动化：清洗文字稿 -> 切候选窗口 -> 打分 -> 产出可渲染时间点。

这个脚本只做确定性工作。它不假装自己能判断「哪句话最精彩」——
它做的是把上千句里明显不值得看的全部去掉，把剩下几十条排好序摆到审阅表上，
让模型和人只需要在几十条里做取舍，而不是把整期视频看完。

用法：
    select_quotes.py pick TRANSCRIPT [--video V] --out DIR [--top 3]
    select_quotes.py batch REPO_DIR --out DIR [--top 3] [--per-category N]

产出（--out 目录下）：
    candidates.json        全部候选窗口，含六维打分，供模型复排
    审阅表.md               人类可读审阅表，确认后才进渲染
    native_manifest.json   原生字幕模式可直接喂给 native_subtitle_stitch.py render
    script_lines.json      脚本字幕模式可直接喂给 render-script

关于本语料的两个实测约束（faster-whisper large-v3-turbo 的中文长视频输出）：
  1. 56/60 文件完全没有标点，靠标点切句不可能；
  2. 段间停顿中位数 0.00s、停顿 >0.5s 的只有 1/648 处，靠停顿切句也不可能。
所以本脚本不切句：把 Whisper 的原始段直接当作「字幕行」（中位 14 字 / 2.52s，
正好是字幕的粒度），一句引文 = 连续 N 行。这也让取帧时间点天然等于行中点，
不需要再合成时间。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_TIMES_PER_IMAGE = 7
MIN_LINES = 2

# ---------------------------------------------------------------- 清洗规则

# 短视频尾部的固定套话。命中即丢，不参与任何候选。
NOISE_PATTERN = re.compile(
    r"(感谢观看|感谢收看|谢谢观看|谢谢大家|感谢大家"
    r"|点赞关注|关注点赞|点赞收藏|点个赞|点个关注|加个关注"
    r"|一键三连|素质三连|三连支持"
    r"|下期见|下期再见|我们下期|未完待续|敬请期待|下一期"
    r"|字幕由|字幕组|字幕志愿者|本视频由|视频来源|素材来源"
    r"|侵删|转载请注明|来源于网络"
    r"|记得关注|评论区见|欢迎评论|转发给|分享给)"
)

# 口语填充词。用于估信息密度，不用于删行。
FILLER_PATTERN = re.compile(r"(然后|就是|这个|那个|其实|对吧|你知道|怎么讲|嗯|啊|呢|啦|哈)")

# 开头是悬空指代或关联词时，脱离上下文就看不懂——扣「独立性」。
DANGLING_PATTERN = re.compile(
    r"^(但是|但|所以|然后|而且|而|因为|不过|那|这|它|他|她|他们|她们|还有|另外|等于"
    r"|也就是说|同时|因此|就是|关于|所以说|总之|这样的话|是因为|就是因为|只不过)"
)

# 话语推进标记。出现在窗口中部，说明窗口跨过了话题边界。
SHIFT_PATTERN = re.compile(r"^(首先|其次|再次|然后|那么|接下来|下一个|第二|第三|第四|最后|另一个|反过来|回到|说回|总结一下|所以呢)")

# 开场铺垫与寒暄。这些行信息量低，但不直接丢弃——有的视频第一句就是论点。
PLACEHOLDER_PATTERN = re.compile(
    r"(今天我们来|今天想跟|这期视频|本期视频|在这个视频里|希望大家|请耐心|耐心听完"
    r"|先把背景|先交代|做个铺垫|废话不多说|话不多说|简单介绍一下|先说说|先讲讲"
    r"|欢迎来到|大家好|哈喽|hello)"
)

# 「发布价值」的信号词。每类给一个权重，累加后归一化。
# 注意：本语料没有标点，所以这里的正则不能用「，。 ] 做边界，只能用字数上限。
VALUE_SIGNALS = (
    # 「数字」只认真正的数量：阿拉伯数字带单位、百分之X、中文数词带量级单位。
    # 绝不能把「第一」「第二个」算成数字——中文里无处不在，实测会把一半候选都误标成
    # 「数字」，直接毁掉排序。
    (re.compile(
        r"(\d+(?:\.\d+)?\s*(?:%|年|倍|万|亿|个|次|元|块|美元|人|天|小时|分钟|秒|个月)"
        r"|百分之[一二三四五六七八九十百千]+"
        r"|[一二三四五六七八九十百千]+(?:万|亿|倍|成))"
    ), 1.0, "数量"),
    (re.compile(r"(并不是|不是[^的]{0,14}而是|恰恰|反而|其实|真相|误区|错了|悖论)"), 1.3, "反常识"),
    (re.compile(r"(因为|所以|因此|导致|意味着|说明|结论是|根本原因)"), 0.9, "因果"),
    (re.compile(r"(归根结底|本质上|核心是|关键是|最重要的是|说到底|真正的问题|重点在于|真正的运作方式)"), 1.4, "结论"),
    (re.compile(r"(应该|必须|一定要|记住|建议|别忘|千万不要|最应该|你得|需要去)"), 1.1, "行动建议"),
    (re.compile(r"(指的是|什么叫|所谓|定义是|意味着什么)"), 0.7, "定义"),
)

# 六维权重。发布价值给最高，因为它就是用户要的「传播意义」。
WEIGHTS = {
    "独立性": 0.20,
    "连续性": 0.15,
    "信息密度": 0.15,
    "发布价值": 0.50,
}


# ---------------------------------------------------------------- 载入


def load_transcript(path: Path) -> tuple[list[dict], float | None, dict]:
    """读 .transcript.json / .json / .srt / .vtt，返回 (segments, duration, meta)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix in (".srt", ".vtt"):
        return parse_cue_subtitles(text, source=suffix.lstrip("."))

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        raise SystemExit(
            f"{path} 看起来不是 JSON、SRT 或 VTT。只有纯文本、没有时间戳的 .txt "
            "无法定位取帧时间，请先转写（见 references/yt-dlp-and-transcripts.md）。"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} JSON 解析失败（第 {exc.lineno} 行）: {exc.msg}") from None

    if isinstance(data, dict):
        raw = data.get("segments")
        duration = data.get("duration")
        meta = {
            "language": data.get("language"),
            "backend": data.get("backend"),
            "model": data.get("model"),
        }
    elif isinstance(data, list):
        raw, duration, meta = data, None, {}
    else:
        raise SystemExit(f"{path} 结构无法识别，期望对象或数组")

    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path} 里没有 segments")

    segments = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SystemExit(f"{path} segments[{index}] 不是对象")
        start, end, value = item.get("start"), item.get("end"), item.get("text")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise SystemExit(f"{path} segments[{index}] 缺少数值 start/end")
        if not isinstance(value, str):
            raise SystemExit(f"{path} segments[{index}] 缺少字符串 text")
        segments.append({"start": float(start), "end": float(end), "text": value})

    if not isinstance(duration, (int, float)):
        duration = None
    return segments, (float(duration) if duration else None), meta


TAG_PATTERN = re.compile(r"<[^>]*>")
STAMP_PATTERN = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)


def _clean_cue_lines(body_lines: list[str]) -> list[str]:
    """去掉 VTT 的行内标签，返回有内容的行为列表。

    yt-dlp 原生的 YouTube 自动字幕 VTT 里，正文其实长这样：
        Hear<00:00:19.720><c> that?</c>
    实测同一份字幕 315 个 cue 里有 113 个带这种标签。不剥掉的话，
    `<c>` 和词级时间戳会被当成台词画进图里。
    """
    cleaned = []
    for line in body_lines:
        text = TAG_PATTERN.sub("", line).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _rolling_tail(previous: list[str], current: list[str]) -> tuple[list[str], bool]:
    """从滚动字幕里取出「本次新增的行」，返回 (可用行, 是否命中滚动结构)。

    YouTube 自动字幕是滚动缓冲式的：画面始终显示「上一行 + 当前行」，
    于是每条 cue 都把上一行又抄一遍。实测 315 个 cue 里有一半是 10 毫秒的
    「翻转 cue」，正文只有被顶掉的那一行。原样读进来，每句话会出现 2–3 次。

    只认**严格变长且上一条是前缀**这一种形态。刻意不认下面两种：

      - `current == previous`：翻转 cue 确实长这样，但中文口语里「对」「嗯」连着重复
        也长这样。实测 60 份 Whisper SRT 里有 31 处命中，全是正常重复而非滚动。
        这类重复交给下游 `sanitize` 按「与前面的段完全重复」丢弃即可，代价为零
        （实测收紧后 VTT 与 SRT 两条路径的最终选句结果逐字相同）。
      - 「current 的行都在 previous 里出现过」：太松，会把正常字幕里重复出现的短句吃掉。

    人工字幕（每 cue 是完整的一两行、且比上一条长不出前缀关系）两条都不命中，原样保留。
    """
    if previous and len(current) > len(previous) and current[: len(previous)] == previous:
        return current[len(previous) :], True
    return current, False


BLOCK_DIRECTIVE = re.compile(r"^(NOTE|STYLE|WEBVTT|Kind\s*:|Language\s*:)", re.IGNORECASE)


def parse_cue_subtitles(text: str, source: str = "srt") -> tuple[list[dict], float | None, dict]:
    """解析 SRT / VTT 字幕，返回 (segments, duration, meta)。

    SRT 和 VTT 没有总时长字段，用末段时间当兜底。

    按行扫描而不是按空行切块：VTT 的 cue 正文里会夹着**只含空白的行**
    （滚动字幕换行留下的），空行切块会把一条 cue 劈成两半，
    时间戳那半成了孤块、正文那半被当成没有时间戳的块丢掉。
    实测 YouTube 原生 VTT 第一条就是这样丢掉「Hear that?」的。

    **必须裁剪相邻重叠**：YouTube 自动字幕实测 91% 的相邻 cue 互相重叠，
    cue[i].end 恰好多半等于 cue[i+2].start（中位差 0.000 秒、83% 在 0.05 秒内）。
    不裁的话时间轴会交叠，取帧点会落在两句话之间。
    把每条的 end 裁到下一条的 start，阅读顺序就恢复成正确的单调序列。
    对 Whisper 那种本来就不重叠的 SRT，这一步是空操作。
    """
    lines = text.splitlines()
    segments = []
    rolling_merged = 0
    previous_lines: list[str] = []
    index = 0
    while index < len(lines):
        match = STAMP_PATTERN.search(lines[index])
        if not match:
            index += 1
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(value) for value in match.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000

        body: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if STAMP_PATTERN.search(line):
                break
            if BLOCK_DIRECTIVE.match(line.strip()):
                break
            # 紧挨着时间戳的那一行是下一条 cue 的抬头，不是本条正文：
            # SRT 是序号行（`2`），VTT 是 cue 标识（`cue-00002`）。
            # WebVTT 与 SRT 都以空行分隔 cue，所以正文末行不会紧邻时间戳。
            if index + 1 < len(lines) and STAMP_PATTERN.search(lines[index + 1]):
                break
            body.append(line)
            index += 1

        # 标签只在「时间戳行之后」的正文里剥，避免动到 VTT 头部
        cleaned = _clean_cue_lines(body)
        if not cleaned:
            continue

        tail, rolled = _rolling_tail(previous_lines, cleaned)
        previous_lines = cleaned
        if rolled:
            rolling_merged += 1
        if not tail:
            continue
        segments.append({"start": start, "end": end, "text": join_parts(tail)})

    if not segments:
        raise SystemExit("字幕里没有解析出任何可用字幕块")

    segments.sort(key=lambda item: item["start"])
    clipped = 0
    for index, segment in enumerate(segments[:-1]):
        following = segments[index + 1]["start"]
        if segment["end"] > following:
            segment["end"] = following
            clipped += 1
    segments = [item for item in segments if item["end"] > item["start"]]
    if not segments:
        raise SystemExit("字幕重叠裁剪后没有剩余可用字幕")

    return (
        segments,
        max(seg["end"] for seg in segments),
        {
            "source": source,
            "clipped_overlaps": clipped,
            "rolling_cues_merged": rolling_merged,
        },
    )


# ---------------------------------------------------------------- 拼接

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def join_parts(parts: list[str]) -> str:
    """按书写系统决定拼接方式：相邻有中日韩字符就不加空格，否则加空格。

    字幕是一句一句切开的，拼回整段时必须还原词间空格。
    中文长视频转写没有标点也没有停顿，段与段直接相连（`这种逆差在正常情况下`
    接 `会导致货币贬值`），多一个空格就是错的；英语正好相反，
    `10% of your` 与 `conversations yeah` 之间少一个空格就成了 `yourconversations`
    ——这个错误会一路带进候选标题和 `全局精选.md` 的原文里。

    逐对判断而不是整段一刀切，混合内容（中文里夹英文品牌名）也能拼对。
    """
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not out:
            out = part
            continue
        if CJK.search(out[-1]) or CJK.search(part[0]):
            out += part
        else:
            out += " " + part
    return out


# ---------------------------------------------------------------- 清洗


def sanitize(
    segments: list[dict], duration: float | None, max_span: float = 20.0
) -> tuple[list[dict], list[dict]]:
    """丢掉幻觉段、套话段、空段。返回 (干净段, 丢弃记录)。

    三道独立的防线，任意一道命中都会拦掉尾部幻觉（实测 11/60 文件有这个问题：
    末段「感谢观看」的 end 会比视频总时长多出几十秒）：
      1. start 超出视频总时长 -> 整段丢弃
      2. end 超出总时长 -> 截到总时长（SRT 没有总时长时跳过）
      3. 单段跨度异常大（Whisper 在静音段循环）-> 丢弃
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()

    for seg in segments:
        text = seg["text"].strip()
        start, end = seg["start"], seg["end"]
        reason = None

        if duration is not None and start >= duration:
            reason = "超出视频总时长"
        elif not text:
            reason = "空文本"
        elif not re.search(r"[\w\u4e00-\u9fff]", text):
            reason = "无有效字符"
        elif NOISE_PATTERN.search(text):
            reason = "片尾套话"
        elif end - start > max_span:
            reason = f"跨度异常大({end - start:.1f}s)，疑似静音幻觉"
        elif text in seen:
            reason = "与前面的段完全重复"

        if reason:
            dropped.append({**seg, "reason": reason})
            continue

        if duration is not None and end > duration:
            end = duration
        if end <= start:
            dropped.append({**seg, "reason": "截断后无长度"})
            continue

        seen.add(text)
        kept.append({"start": start, "end": end, "text": text})

    kept.sort(key=lambda item: item["start"])
    return kept, dropped


# ---------------------------------------------------------------- 造行与切窗


def build_lines(
    segments: list[dict], min_chars: int = 6, max_chars: int = 34, max_span: float = 9.0
) -> list[dict]:
    """把 Whisper 的原始段整成「字幕行」。

    本语料里原始段中位 14 字 / 2.52s，本身就是合格的字幕行，所以默认不动它。
    只处理两种极端：过短的碎片（< min_chars）并进相邻行；过长的段（> max_chars）单独成行。
    """
    lines: list[dict] = []
    for seg in segments:
        text = seg["text"]
        if lines and len(lines[-1]["text"]) < min_chars:
            previous = lines[-1]
            if previous["end"] - previous["start"] + (seg["end"] - seg["start"]) <= max_span:
                previous["text"] = join_parts([previous["text"], text])
                previous["end"] = seg["end"]
                previous["merged"] = previous.get("merged", 1) + 1
                continue
        lines.append(
            {"start": seg["start"], "end": seg["end"], "text": text, "merged": 1}
        )
    return [line for line in lines if line["text"].strip()]


def build_windows(lines: list[dict], low: int = 4, high: int = 6) -> list[dict]:
    """滑动生成候选窗口。一个窗口 = 一张图的内容 = 连续 N 行。"""
    windows = []
    total = len(lines)
    for size in range(low, high + 1):
        for index in range(0, total - size + 1):
            chunk = lines[index : index + size]
            windows.append(
                {
                    "size": size,
                    "index": index,
                    "start": chunk[0]["start"],
                    "end": chunk[-1]["end"],
                    "text": join_parts([item["text"] for item in chunk]),
                    "lines": chunk,
                }
            )
    windows.sort(key=lambda item: (item["start"], item["size"]))
    return windows


def cohesion(lines: list[dict]) -> float:
    """相邻行共享的二字词比例，作为「这几行是不是在讲同一件事」的近似。

    没有标点和停顿可用时，词面重叠是唯一还剩的连贯性线索。
    """
    if len(lines) < 2:
        return 1.0
    scores = []
    for left, right in zip(lines, lines[1:]):
        a = {left["text"][i : i + 2] for i in range(len(left["text"]) - 1)}
        b = {right["text"][i : i + 2] for i in range(len(right["text"]) - 1)}
        if not a or not b:
            continue
        scores.append(len(a & b) / min(len(a), len(b)))
    return sum(scores) / len(scores) if scores else 0.0


# ---------------------------------------------------------------- 打分


def score_window(window: dict) -> dict:
    """按 skill 自带的六个维度打分。画面可用性无视频时标 unknown，不计入。"""
    chunk = window["lines"]
    text = window["text"]
    length = len(text) or 1

    # 独立性：起句不悬空（没有标点可用，只能看开头是不是挂在上一句上的指代/关联词）
    independence = 0.0
    independence += 0.0 if DANGLING_PATTERN.match(chunk[0]["text"]) else 0.5
    # 首行太短，多半是上一句的尾巴，不是能独立站住的开头
    independence += 0.2 if len(chunk[0]["text"]) >= 8 else 0.0
    # 除首行外出现话语推进标记，说明窗口跨过了话题边界或正好停在转折处
    closing_shift = any(SHIFT_PATTERN.match(item["text"]) for item in chunk[1:])
    independence += 0.0 if closing_shift else 0.3

    # 连贯性：词面重叠 + 时间跨度是否像一段连续表达
    link = cohesion(chunk)
    span = window["end"] - window["start"]
    continuity = {4: 0.8, 5: 1.0, 6: 0.85}.get(window["size"], 0.5)
    continuity *= 0.7 + 0.3 * min(1.0, link * 6.0)
    if 8.0 <= span <= 40.0:
        pass
    elif span < 8.0:
        continuity *= 0.75
    else:
        continuity *= 0.8

    # 信息密度：填充词越少、用词越不重复，密度越高
    filler_ratio = len(FILLER_PATTERN.findall(text)) / length
    density = max(0.0, 1.0 - filler_ratio * 5.0)
    bigrams = {text[i : i + 2] for i in range(len(text) - 1)}
    density = min(1.0, density * 0.65 + (len(bigrams) / max(1, len(text) - 1)) * 0.5)

    # 发布价值：信号词累加
    hits, raw_value = [], 0.0
    for pattern, weight, label in VALUE_SIGNALS:
        if pattern.search(text):
            raw_value += weight
            hits.append(label)
    value = min(1.0, raw_value / 2.6)

    # 开场铺垫与寒暄压掉密度和价值，但不归零——有的论点就长在开头
    placeholders = len(PLACEHOLDER_PATTERN.findall(text))
    if placeholders:
        damping = max(0.35, 1.0 - placeholders * 0.3)
        density *= damping
        value *= damping
        hits.append(f"铺垫×{placeholders}")

    total = (
        WEIGHTS["独立性"] * independence
        + WEIGHTS["连续性"] * continuity
        + WEIGHTS["信息密度"] * density
        + WEIGHTS["发布价值"] * value
    )

    return {
        "total": round(total, 4),
        "独立性": round(independence, 3),
        "连续性": round(continuity, 3),
        "信息密度": round(density, 3),
        "发布价值": round(value, 3),
        "连贯性": round(link, 3),
        "画面可用性": "unknown",
        "信号词": hits,
    }


def pick_windows(
    windows: list[dict], top: int, max_line_overlap: float = 0.34
) -> list[dict]:
    """按分数贪心选，避免几张图讲同一件事。

    两道去重，任一命中即视为重复：
    1. 共享行数比例过高——两个窗口可以时间上只压边，却共享大部分行；
    2. **新窗口的起点落在已选窗口内部**——窗口是逐行滑动造的，相邻窗口天然重叠，
       只按比例判会漏掉「第 2 张从第 1 张的第 4 行开始」这种嵌套。
       实测（119 份文字稿）换成这条嵌套规则后，选出的总张数不变、最高分窗口全部保留，
       但含重复行的期数从 71 降到 40，平均最大共享比从 0.147 降到 0.082。

    早先这条是 `abs(index 差) < 3` 的固定间隔，对中文 Whisper 段（中位 14 字）勉强够用，
    对 YouTube 自动字幕（中位 32 字、行数少而信息密）就太松：实测同一段 TED 演讲里
    第 1、2 组共享了「have for you all」这一行，因为 1/5 = 0.20 低于阈值、索引差又是 4。
    改成按窗口自身长度判定后，规则随窗口大小自适应，不再依赖某个魔数。
    """
    ranked = sorted(windows, key=lambda item: (-item["score"]["total"], item["start"]))
    chosen: list[dict] = []
    for candidate in ranked:
        if len(chosen) >= top:
            break
        candidate_lines = {id(line) for line in candidate["lines"]}
        too_close = False
        for picked in chosen:
            picked_lines = {id(line) for line in picked["lines"]}
            shared = len(candidate_lines & picked_lines)
            if shared / min(len(candidate_lines), len(picked_lines)) >= max_line_overlap:
                too_close = True
                break
            if picked["index"] <= candidate["index"] < picked["index"] + picked["size"]:
                too_close = True
                break
        if not too_close:
            chosen.append(candidate)
    chosen.sort(key=lambda item: item["start"])
    return chosen


# ---------------------------------------------------------------- 产出时间点


def choose_times(
    lines: list[dict], count: int | None = None, min_gap: float = 0.4
) -> list[float]:
    """取每行的中点——那是字幕稳定显示的瞬间，不是切换瞬间（见工作流阶段 5）。

    count 默认 None = 用窗口全部的行。绝不能为了凑 5 个而均匀抽稀：
    抽掉的若是中间行，图上论证链会断（实测踩过：6 行抽 5 个，
    恰好丢掉「这种逆差在正常情况下会导致货币贬值和经济困难」这一环）。
    窗口本身只有 4–6 行，都在 MAX_TIMES_PER_IMAGE 之内。
    """
    mids = [round((item["start"] + item["end"]) / 2, 2) for item in lines]
    mids = [value for value in mids if value > 0]
    wanted = len(mids) if count is None else count
    if len(mids) > wanted:
        step = (len(mids) - 1) / (wanted - 1) if wanted > 1 else 0
        mids = [mids[round(i * step)] for i in range(wanted)]

    result: list[float] = []
    for value in mids:
        if not result or value - result[-1] >= min_gap:
            result.append(value)
    while len(result) < MIN_LINES:
        result.append(round((result[-1] if result else 0.0) + min_gap, 2))
    return result[:MAX_TIMES_PER_IMAGE]


def make_title(window: dict, limit: int = 18) -> str:
    """标题只做文件名和内部导航，不会被画进图片。

    剥标点，但**保留词间空格**：英语若连空格一起剥掉，会粘成
    `brokenaboneSolookslike`，在审阅表标题和 `scripts_zh/` 的文件名里都不可读
    （英文标题同时是原文与译文两套产物的名字）。对纯中文没有影响。
    """
    text = re.sub(r"[^\w\u4e00-\u9fff\s]", "", window["text"])
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "未命名"


def parse_targets(value: str | None) -> list[str]:
    """`--translate zh-Hans,zh-Hant` → `['zh-Hans', 'zh-Hant']`；空值 = 不启用。"""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# 渲染器实测的单行容量（width 1080 与 1440 结果相同——字号 `out_width/18` 和
# 可用宽度 `out_width*0.92` 都随宽度等比缩放，所以**容量与 width 无关**）：
#
#   基础字号（1440 时为 80px）   约 16 个汉字 / 约 32 个拉丁字符
#   缩到可读下限（55%，44px）    29 个汉字 / 60 个拉丁字符
#
# 两组数一致地指向「1 个汉字 ≈ 2 个拉丁字符宽」。所以下面统一用**宽度单位**
# 记账：汉字 2 单位、拉丁 1 单位。
#
# 44 单位 ≈ 22 汉字 ≈ 44 拉丁字符：字号约降到 75%，仍然醒目，
# 又不会把行数顶破 MAX_TIMES_PER_IMAGE。
ROW_UNITS = 44
# 装不下就逐档放宽（字号随之变小）；56 单位已经接近 60 的硬上限，
# 再宽就会撞上「缩小到可读下限后仍放不下」而整张图渲染失败。
ROW_UNITS_MAX = 56

SENTENCE_BREAK = re.compile(r"(?<=[，。！？；：、,.!?;:])")
LEADING_PUNCT = re.compile(r"^[，。！？；：、,.!?;:]+")

# 东亚宽字符 / 全角字符（含 U+3002「。」这类 CJK 标点、U+FF0C 全角逗号）。
# 不能直接复用上面的 CJK——那个不含 U+3000–U+303F 的 CJK 标点段，会把
# 句末的「。」当半个宽字符算，行就排超了。
WIDE = re.compile(
    r"[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff"
    r"\ufe30-\ufe6b\uff00-\uff60\uffe0-\uffe6]"
)


def display_units(text: str) -> int:
    """把一行文本换算成「宽度单位」：汉字 2 单位、拉丁字符 1 单位。

    只按字符个数算是不够的：`全球8%的温室气体排放` 是 11 个字符，但宽度是 18 单位，
    差了一倍——真实的排版限制看的是宽度。
    """
    return sum(2 if WIDE.match(char) else 1 for char in text)


def unit_prefix_len(text: str, max_units: int) -> int:
    """宽度不超过 max_units 的最长前缀有**几个字符**。

    汉字是 2 单位，所以 44 单位的预算在纯汉字下是 22 个字符、纯拉丁下是 44 个字符。
    按字符数硬切会把汉字行切出两倍长。
    """
    used = 0
    for index, char in enumerate(text):
        used += 2 if WIDE.match(char) else 1
        if used > max_units:
            return max(index, 1)
    return len(text)


def split_point(text: str, max_units: int) -> int:
    """决定从第几个字符处切开：有空格的语言**优先落在词边界**上。

    纯按宽度硬切会切出 `entire cli` / `mate fight,` 这种半截单词——英文看着像坏掉了。
    中文没有空格，`rfind` 找不到，自然回落到按宽度切。
    """
    cut = unit_prefix_len(text, max_units)
    if cut >= len(text):
        return cut
    space = text.rfind(" ", 0, cut)
    return space if space > 0 else cut


def split_text_rows(text: str, max_units: int) -> list[str]:
    """按标点优先把一段文本切成每行不超过 max_units 宽度的若干行。

    中文没有空格，单个词组就超长时只能硬切——但硬切**不能把标点甩到下一行开头**
    （实测会切出 `…排放` / `。所以航运…` 这种，读起来像坏掉了）。
    所以硬切之后把开头的标点要回来贴到上一行末尾，代价是那一行可能略超上限。
    """
    if display_units(text) <= max_units:
        return [text.strip()]
    rows: list[str] = []
    current = ""
    for piece in SENTENCE_BREAK.split(text):
        if not piece:
            continue
        if current and display_units(current) + display_units(piece) > max_units:
            rows.append(current)
            current = piece
        else:
            current += piece
        while display_units(current) > max_units:
            cut = split_point(current, max_units)
            head, tail = current[:cut], current[cut:]
            keep = LEADING_PUNCT.match(tail)
            if keep:
                head, tail = head + keep.group(), tail[keep.end():]
            rows.append(head)
            current = tail
    if current:
        rows.append(current)
    # 断点常常正好落在空格上（英文按逗号断句时是常态），行首/行尾的空格要剥掉，
    # 否则渲染出来的行会莫名缩进一格。
    cleaned = [row.strip() for row in rows if row.strip()]
    return cleaned or [text]


def _rows_for(cues_and_text: list[tuple[dict, str]], max_units: int) -> list[dict]:
    """每条 cue 按 max_units 拆行，行的时间取该 cue 时间段的等分中点。

    一条 cue 只拆出一行时，时间正好是该 cue 的中点——和「一个 cue 一行」的老行为一致。
    """
    rows: list[dict] = []
    for cue, text in cues_and_text:
        pieces = split_text_rows(text, max_units)
        step = (cue["end"] - cue["start"]) / len(pieces)
        for index, piece in enumerate(pieces):
            rows.append({
                "t": round(cue["start"] + step * (index + 0.5), 2),
                "text": piece,
            })
    return rows


def enforce_increasing(rows: list[dict]) -> list[dict]:
    """把时间戳强行改成严格递增：渲染器会拒绝落在同一帧的两行。"""
    last = -1.0
    for row in rows:
        if row["t"] <= last:
            row["t"] = round(last + 0.1, 2)
        last = row["t"]
    return rows


def original_rows(window: dict, max_rows: int = MAX_TIMES_PER_IMAGE) -> list[dict]:
    """把窗口里的原文 cue 变成能画进字幕条的行。

    以前这里是「一个 cue 一行」，并在注释里断言「拉丁字符约 50 个，所以英文原声行天然
    放得下」。**实测这个前提不成立**：TED 自动字幕合并滚动 cue 之后，字幕句的中位数是
    44 个拉丁字符、p90 是 67、最长 78，而渲染器缩到可读下限也只放得下 60 个——
    40.3% 的 cue 超过 50 字符，21.6% 直接超过 60 必然报错
    （`台词过长，缩小到可读下限后仍放不下`），整张图渲染不出来。

    之前那个「英文放得下」的结论是在 **VTT 的滚动短行**上量的，不是合并后的整句；
    而流水线用的恰恰是合并后的整句。所以英文这一套也得拆行。
    """
    kept = [(line, line["text"]) for line in window["lines"]]
    kept = [
        (line, text) for line, text in kept
        if round((line["start"] + line["end"]) / 2, 2) > 0
    ][:MAX_TIMES_PER_IMAGE]
    rows = [
        {"t": round((line["start"] + line["end"]) / 2, 2), "text": text}
        for line, text in kept
    ]
    # 快路径：本来就放得下，一个 cue 一行，行为与改动前逐字节一致。
    if all(display_units(row["text"]) <= ROW_UNITS_MAX for row in rows):
        return rows
    rows = _rows_for(kept, ROW_UNITS)
    for max_units in range(ROW_UNITS + 4, ROW_UNITS_MAX + 1, 4):
        if len(rows) <= max_rows:
            break
        rows = _rows_for(kept, max_units)
    return enforce_increasing(rows)


def translated_rows(
    window: dict, translation: list[dict], max_rows: int = MAX_TIMES_PER_IMAGE
) -> list[dict]:
    """把译文挂到窗口上：取落在窗口时间范围内的译文 cue，拆成可直接画的行。

    刻意**不按「原文的行数」去切译文**。实测 YouTube 自动翻译轨与原声轨是逐条对齐的
    （134/134 条、起止时间完全一致），但它的**换行位置与原文不总相同**——
    出现过整行只有一个逗号（`['它是整个气候斗争中最大的隐藏杠杆之一', '，']`）的情况。
    照着原文的行数切会画出残句。

    也不能「一条 cue 一行」：中文一条 cue 往往是一整句（实测 15–29 字），
    而渲染器缩到可读下限也只放得下 29 个汉字，29 字那行会直接报
    「台词过长，缩小到可读下限后仍放不下」。所以按 ROW_UNITS 拆，装不下再放宽。

    译文轨里可能有「有时间戳、正文为空」的 cue（实测自动翻译轨 134 条里有 2 条，
    人工译文轨 358 条里有 26 条），直接跳过。
    """
    kept: list[tuple[dict, str]] = []
    for cue in translation:
        if cue["end"] <= window["start"] or cue["start"] >= window["end"]:
            continue
        text = join_parts(cue["text"].splitlines())
        if text:
            kept.append((cue, text))
    if not kept:
        return []

    rows = _rows_for(kept, ROW_UNITS)
    for max_units in range(ROW_UNITS + 4, ROW_UNITS_MAX + 1, 4):
        if len(rows) <= max_rows:
            break
        rows = _rows_for(kept, max_units)
    return enforce_increasing(rows)


def fit_window(window: dict) -> dict:
    """把窗口裁到渲染器接受的长度：拆行后不超过 MAX_TIMES_PER_IMAGE 行。

    `render-script` **硬性**只收 7 个时间点，多了直接报
    「台词脚本最多支持 7 个时间点；请拆成多张图」。而长句一旦拆行，
    4–6 条的窗口就可能涨到 8–12 行；宽到 ROW_UNITS_MAX 也压不下去时，
    只能**从尾部整条整条地退 cue**——退的是整句，不是半句话。

    必须在这里裁、而不是在某一种语言里裁：英文和中文是同一个窗口的两套输出，
    各裁各的会让两版图的内容对不上。

    两条 cue 最坏也只拆成 4 行，所以循环一定能收敛到 ≥ MIN_LINES 条，不会把窗口退空。
    """
    if len(original_rows(window)) <= MAX_TIMES_PER_IMAGE:
        return window
    lines = list(window["lines"])
    while len(lines) > MIN_LINES:
        candidate = {**window, "lines": lines}
        if len(original_rows(candidate)) <= MAX_TIMES_PER_IMAGE:
            break
        lines = lines[:-1]
    trimmed = {
        **window,
        "lines": lines,
        "start": lines[0]["start"],
        "end": lines[-1]["end"],
        "text": join_parts([line["text"] for line in lines]),
        "dropped_lines": len(window["lines"]) - len(lines),
    }
    return trimmed


def build_outputs(windows: list[dict], top: int, translation: list[dict] | None = None) -> dict:
    """产出两套可直接渲染的文件。

    原生模式：render 的 --manifest 本身就接受多张图，所以汇总成一个 JSON。
    脚本模式：render-script 一次只画一张（--script 是单个 {"lines":[...]}），
    所以必须拆成一张一个文件，绝不能塞成数组——实测踩过，会直接报
    「台词脚本必须包含至少 2 项的 lines 数组」。

    给了译文轨就额外产出 `script_lines_zh`：**原文一套、译文一套，互不覆盖**，
    因为一张图只能画一种语言，而两种语言都要发（英文发 TikTok）。
    """
    chosen = [fit_window(window) for window in pick_windows(windows, top)]
    images, scripts, scripts_zh = [], [], []
    skipped_zh: list[dict] = []
    trimmed: list[dict] = []
    for index, window in enumerate(chosen, 1):
        if window.get("dropped_lines"):
            trimmed.append({"order": index, "dropped_lines": window["dropped_lines"]})
        times = choose_times(window["lines"])
        title = make_title(window)
        images.append({"title": title, "times": times})
        lines = original_rows(window)
        if len(lines) >= MIN_LINES:
            scripts.append(
                {"order": index, "title": title, "start": window["start"], "lines": lines}
            )
        if translation is not None:
            zh_lines = translated_rows(window, translation)
            if MIN_LINES <= len(zh_lines) <= MAX_TIMES_PER_IMAGE:
                scripts_zh.append(
                    {"order": index, "title": title, "start": window["start"], "lines": zh_lines}
                )
            else:
                skipped_zh.append({"order": index, "title": title, "rows": len(zh_lines)})
    bundle = {"native_manifest": {"images": images}, "script_lines": scripts, "chosen": chosen}
    if trimmed:
        # 长句拆行之后超过渲染器的 7 行上限，从尾部退掉了若干条 cue
        bundle["trimmed"] = trimmed
    if translation is not None:
        bundle["script_lines_zh"] = scripts_zh
        bundle["translation_skipped"] = skipped_zh
    return bundle


def render_commands(
    scripts: list[dict], video: str | None, script_dir: str = "scripts", images_dir: str = "images"
) -> str:
    """生成可直接执行的渲染命令，省掉手工拼 60 条命令。"""
    target = video or "请填入源视频路径.mp4"
    # 原文那一套保持老路径不变（`X.mp4.quotes`），译文那一套才加子目录。
    default_out = f"{target}.quotes" if images_dir == "images" else f"{target}.quotes/{images_dir}"
    out = [
        "#!/usr/bin/env bash",
        "# 由 select_quotes.py 生成：逐张渲染脚本字幕。",
        "# render-script 一次只画一张，所以这里必须是循环。",
        "set -euo pipefail",
        "",
        f'PYTHON="${{PYTHON:-python3}}"',
        f'VIDEO="${{VIDEO:-{target}}}"',
        f'OUT="${{OUT:-{default_out}}}"',
        'SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'mkdir -p "$OUT"',
        "",
    ]
    for item in scripts:
        safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
        out.append(
            f'"$PYTHON" "$SKILL_DIR/scripts/native_subtitle_stitch.py" render-script "$VIDEO" \\\n'
            f'  --script "{script_dir}/{item["order"]:02d}_{safe}.json" \\\n'
            f'  --out "$OUT/{item["order"]:02d}_{safe}.jpg" \\\n'
            f'  --aspect 3:4 --width 1440'
        )
        out.append("")
    out.append('echo "完成：$OUT"')
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 审阅表


def render_review(
    source: str, meta: dict, duration: float | None, dropped: list[dict],
    windows: list[dict], chosen: list[dict], top: int,
    duration_source: str = "transcript", declared: float | None = None,
    drift: float | None = None,
    translation: list[dict] | None = None,
    translation_source: str | None = None,
) -> str:
    out = [f"# 候选审阅表：{source}", ""]
    out.append(f"- 总时长：{duration:.2f}s" if duration else "- 总时长：未知")
    if duration_source == "video":
        out.append(
            f"- 时长来源：视频实测（转写声明 {declared:.2f}s，偏差 {drift:+.2f}s）"
            if declared and drift is not None
            else "- 时长来源：视频实测"
        )
    elif duration_source == "transcript-no-ffmpeg":
        out.append(
            "- 时长来源：**转写自带** —— 找到了同名视频，但读不出时长"
            "（FFmpeg 不可用）。时间点未经真实时长校正，出图前请先装好 FFmpeg 或 imageio-ffmpeg。"
        )
    else:
        out.append("- 时长来源：转写自带（没找到同名视频，时间点未经真实时长校正）")
    out.append(f"- 转写模型：{meta.get('model') or '未知'}")
    if translation_source:
        out.append(f"- 译文轨：{Path(translation_source).name}（逐行译文本表下方一并列出，请核对）")
    out.append(f"- 清洗丢弃：{len(dropped)} 段")
    out.append(f"- 候选窗口：{len(windows)} 个，选出 {len(chosen)} 个")
    out.append("")
    if dropped:
        out.append("## 已丢弃的段落")
        out.append("")
        out.append("| 原因 | 时间 | 文本 |")
        out.append("|---|---|---|")
        for item in dropped[:15]:
            snippet = item["text"][:34].replace("|", "／")
            out.append(f"| {item['reason']} | {item['start']:.1f}s | {snippet} |")
        if len(dropped) > 15:
            out.append(f"| … | | 另有 {len(dropped) - 15} 段 |")
        out.append("")
    out.append("## 入选候选（按时间顺序，请逐条确认）")
    out.append("")
    rank = {
        id(item): position
        for position, item in enumerate(
            sorted(chosen, key=lambda w: -w["score"]["total"]), 1
        )
    }
    for order, window in enumerate(chosen, 1):
        times = choose_times(window["lines"])
        score = window["score"]
        out.append(f"### {order}. {make_title(window, 30)}")
        out.append("")
        out.append(
            f"- 起止：{window['start']:.1f}s – {window['end']:.1f}s"
            f"（{window['end'] - window['start']:.1f}s，{window['size']} 行）"
        )
        out.append(
            f"- 总分：{score['total']}（建议优先级 {rank[id(window)]}/{len(chosen)}）　"
            f"独立性 {score['独立性']}　连续性 {score['连续性']}　"
            f"密度 {score['信息密度']}　发布价值 {score['发布价值']}"
        )
        out.append(f"- 信号词：{'、'.join(score['信号词']) or '无'}")
        out.append(f"- 候选时间点：{', '.join(f'{value:.1f}' for value in times)}")
        out.append("- 逐行原文（脚本字幕模式下每行画一条）：")
        out.append("")
        for line in window["lines"]:
            out.append(f"  - `{(line['start'] + line['end']) / 2:7.1f}s`　{line['text']}")
        out.append("")
        if translation is not None:
            zh_rows = translated_rows(window, translation)
            out.append("- 译文（每行画一条；长句按标点拆开，因为基础字号一行只放得下 16 个汉字）：")
            out.append("")
            for row in zh_rows:
                out.append(f"  - `{row['t']:7.1f}s`　{row['text']}")
            if not MIN_LINES <= len(zh_rows) <= MAX_TIMES_PER_IMAGE:
                out.append(
                    f"  - ⚠ 译文 {len(zh_rows)} 行，超出渲染器接受的 "
                    f"{MIN_LINES}–{MAX_TIMES_PER_IMAGE} 行，**这一组不会产出中文图**；"
                    "可缩短窗口或手工拆句。"
                )
            out.append("")
    if len(chosen) < top:
        out.append(
            f"> 只选出 {len(chosen)} 组（目标 {top} 组）：候选之间重叠过高或总量不足，"
            "可用 --top 调整。"
        )
        out.append("")
    out.append("## 下一步")
    out.append("")
    out.append("1. 逐条确认或删除上面的候选，改写标题。")
    out.append("2. 原生字幕模式：`render VIDEO --manifest native_manifest.json --out-dir OUT`。")
    out.append("3. 脚本字幕模式：`render-script VIDEO --script script_lines.json --out OUT`。")
    if translation_source:
        out.append("4. 中文那一套在 `scripts_zh/`，出图到 `images_zh/`：")
        out.append("   `渲染脚本字幕_zh.sh`；图与英文版**同帧同时刻**，可直接对照。")
        out.append("   ⚠ 译文来自机器翻译，**发布前必须逐句核对**，尤其人名、数字、专有名词。")
        out.append("5. **不要跳过人工确认**：本表只按形式特征排序，没有判断语义是否真的成立；")
        out.append("   转写本身也有错字（实测如「约数条件」应为「约束条件」），脚本模式必须先校对。")
    else:
        out.append("4. **不要跳过人工确认**：本表只按形式特征排序，没有判断语义是否真的成立；")
        out.append("   转写本身也有错字（实测如「约数条件」应为「约束条件」），脚本模式必须先校对。")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 命令


_FFMPEG: str | None = None
_FFMPEG_TRIED = False


def resolve_ffmpeg() -> str | None:
    """解析 FFmpeg，顺序与 native_subtitle_stitch.py 保持一致。

    先试 imageio-ffmpeg 自带的二进制，再回落到 PATH。
    不能只写裸 "ffmpeg"：requirements.txt 走的就是 imageio-ffmpeg，
    在没装系统 ffmpeg 的机器上裸命令不存在，时长校正会静默失效——
    这正是 CI 抓到的问题（本地装了系统 ffmpeg 所以看不出来）。
    """
    global _FFMPEG, _FFMPEG_TRIED
    if _FFMPEG_TRIED:
        return _FFMPEG
    _FFMPEG_TRIED = True
    try:
        import imageio_ffmpeg

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 没装就回落到 PATH
        import shutil

        _FFMPEG = shutil.which("ffmpeg")
    return _FFMPEG


def probe_duration(video: Path) -> float | None:
    """读视频的真实时长。取不到就返回 None，由调用方决定怎么标注。"""
    import subprocess

    engine = resolve_ffmpeg()
    if not engine:
        return None
    try:
        result = subprocess.run(
            [engine, "-hide_banner", "-i", str(video)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d{2})", result.stderr)
    if not match:
        return None
    hours, minutes, seconds, centis = (int(value) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds + centis / 100


def process_one(
    transcript_path: Path, out_dir: Path, top: int, video: Path | None = None, options=None
) -> dict:
    segments, declared, meta = load_transcript(transcript_path)

    # 真实视频时长是取帧的唯一权威。转写自带的 duration 只是 Whisper 的判断，
    # 实测会与视频不符（59 期中 11 期末段越界），所以只要找得到视频就用它校正。
    target_video = video or sibling_video(transcript_path)
    duration = declared
    duration_source = "transcript"
    if target_video is not None:
        probed = probe_duration(target_video)
        if probed:
            duration = probed
            duration_source = "video"
        else:
            # 有视频却读不到时长：标注出来，不要静默当成转写时长可信
            duration_source = "transcript-no-ffmpeg"
    drift = None
    if declared and duration and duration > 0:
        drift = round(declared - duration, 2)

    clean, dropped = sanitize(segments, duration)
    if len(clean) < 4:
        raise SystemExit(f"清洗后只剩 {len(clean)} 段，不足以选句")
    lines = build_lines(clean)
    windows = build_windows(lines)
    if not windows:
        raise SystemExit(f"只造出 {len(lines)} 行，凑不满一个窗口")
    for window in windows:
        window["score"] = score_window(window)

    # 译文轨是可选的：没有就照旧只出原文一套，不报错。
    targets = parse_targets(getattr(options, "translate", None)) if options is not None else []
    translation_path = find_translation(transcript_path, targets) if targets else None
    translation = None
    if translation_path is not None:
        translation = load_transcript(translation_path)[0]

    bundle = build_outputs(windows, top, translation)
    chosen = bundle["chosen"]

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "candidates.json").write_text(
        json.dumps(
            {
                "source": str(transcript_path),
                "video": str(target_video) if target_video else None,
                "duration": duration,
                "duration_source": duration_source,
                "declared_duration": declared,
                "duration_drift": drift,
                "meta": meta,
                "lines": len(lines),
                "dropped": dropped,
                "candidates": [
                    {
                        "start": item["start"],
                        "end": item["end"],
                        "size": item["size"],
                        "text": item["text"],
                        "score": item["score"],
                        "selected": item in chosen,
                    }
                    for item in sorted(windows, key=lambda w: (-w["score"]["total"], w["start"]))
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "native_manifest.json").write_text(
        json.dumps(bundle["native_manifest"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 原文与译文各写一套：一张图只能画一种语言，两套互不覆盖。
    for suffix, items in (("", bundle["script_lines"]), ("_zh", bundle.get("script_lines_zh"))):
        if items is None:
            continue
        (out_dir / f"script_lines{suffix}.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 脚本字幕模式：一张图一个文件，因为 render-script 一次只画一张
        script_dir = out_dir / ("scripts_zh" if suffix else "scripts")
        script_dir.mkdir(exist_ok=True)
        for item in items:
            safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
            (script_dir / f"{item['order']:02d}_{safe}.json").write_text(
                json.dumps({"lines": item["lines"]}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        shell = out_dir / ("渲染脚本字幕_zh.sh" if suffix else "渲染脚本字幕.sh")
        shell.write_text(
            render_commands(
                items, str(video) if video else None,
                script_dir=script_dir.name,
                images_dir="images_zh" if suffix else "images",
            ),
            encoding="utf-8",
        )
        shell.chmod(0o755)
    (out_dir / "审阅表.md").write_text(
        render_review(
            transcript_path.name, meta, duration, dropped, windows, chosen, top,
            duration_source, declared, drift,
            translation=translation, translation_source=str(translation_path) if translation_path else None,
        ),
        encoding="utf-8",
    )

    rendered: list[Path] = []
    render_failures: list[str] = []
    if options is not None and getattr(options, "render", False):
        if target_video is None:
            render_failures.append(
                f"找不到与 {transcript_path.name} 同名的视频，跳过渲染"
            )
        else:
            rendered, render_failures = render_selected(bundle, target_video, out_dir, options)

    return {
        "chosen": len(chosen),
        "candidates": len(windows),
        "dropped": len(dropped),
        "lines": len(lines),
        "out": out_dir,
        "rendered": len(rendered),
        "render_failures": render_failures,
    }


def aggregate(out_root: Path, top: int) -> dict:
    """把所有视频的入选候选汇成一张跨视频精选榜。

    单看一个视频的排序没有意义——用户要的是「这批里哪几句最值得发」。
    """
    entries = []
    stats = {"videos": 0, "lines": 0, "candidates": 0, "selected": 0}
    for path in sorted(out_root.rglob("candidates.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        stats["videos"] += 1
        items = data.get("candidates") or []
        stats["candidates"] += len(items)
        stats["lines"] += data.get("lines") or 0
        category = path.relative_to(out_root).parts[0] if len(path.relative_to(out_root).parts) > 1 else "_root"
        source = Path(data.get("source", "")).name.replace(".mp4.transcript.json", "")
        for item in items:
            if not item.get("selected"):
                continue
            stats["selected"] += 1
            entries.append(
                {
                    "score": item["score"]["total"],
                    "category": category,
                    "source": source,
                    "start": item["start"],
                    "end": item["end"],
                    "text": item["text"],
                    "signals": item["score"]["信号词"],
                    "score_detail": item["score"],
                }
            )
    entries.sort(key=lambda item: (-item["score"], item["start"]))

    (out_root / "全局精选.json").write_text(
        json.dumps({"stats": stats, "top": entries[:top], "all": entries},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = ["# 全局精选榜", ""]
    lines.append(f"- 覆盖视频：{stats['videos']}")
    lines.append(f"- 候选窗口：{stats['candidates']}")
    lines.append(f"- 入选：{stats['selected']}")
    lines.append("")
    lines.append(f"## 总分最高的 {min(top, len(entries))} 条")
    lines.append("")
    for order, item in enumerate(entries[:top], 1):
        lines.append(f"### {order}. [{item['category']}] {item['text'][:30]}")
        lines.append("")
        lines.append(f"- 来源：{item['source'][:60]}")
        lines.append(f"- 时间：{item['start']:.1f}s – {item['end']:.1f}s")
        lines.append(f"- 总分：{item['score']}　信号词：{'、'.join(item['signals']) or '无'}")
        lines.append(f"- 原文：{item['text']}")
        lines.append("")
    (out_root / "全局精选.md").write_text("\n".join(lines), encoding="utf-8")
    return stats


TRANSCRIPT_SUFFIXES = (".transcript.json", ".transcript.srt", ".transcript.vtt",
                       ".json", ".srt", ".vtt")


def transcript_stem(name: str) -> str:
    """剥掉已知的字幕/文字稿后缀，得到不含格式名的茎。"""
    for suffix in TRANSCRIPT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def language_rank(stem: str) -> tuple[int, str]:
    """同一支视频的多条字幕轨里，哪条最接近「原声」。

    yt-dlp 一次 `--sub-langs "zh.*,en.*"` 会给同一支视频落下好几条轨：
    `en`、`en-orig`，甚至自动翻译出来的 `en-en`（实测与 `en` 逐字节相同）。
    全量喂进批处理，同一支视频会被处理三遍、全局精选里重复三次。

    排序（越小越优先）：
      0  没有语言码（Whisper 转写就是这种），或 `-orig` 原声轨
      1  单一语言码：`en` / `zh`
      2  带一个横线的变体：`zh-Hans` / `pt-BR`
      3  两个以上横线：自动翻译轨 `zh-Hans-en` / `en-en`
    """
    head, dot, tail = stem.rpartition(".")
    if not dot or not tail or not LANG_CODE.match(tail):
        return (0, "")
    if tail.endswith("orig"):
        return (0, tail)
    dashes = tail.count("-")
    return (1 if dashes == 0 else 2 if dashes == 1 else 3, tail)


def dedupe_by_video(paths: list[Path]) -> list[Path]:
    """同一支视频只留一条最贴近原声的字幕。

    分组键取「文件名去掉语言码」，所以 `标题_[id].en.srt` 与
    `标题_[id].en-orig.srt` 会归到同一组，而两支不同视频不会互相吞掉。
    """
    best: dict[tuple[str, str], tuple[tuple[int, str], Path]] = {}
    for path in paths:
        stem = transcript_stem(path.name)
        head, dot, tail = stem.rpartition(".")
        base = head if (dot and head and LANG_CODE.match(tail)) else stem
        key = (str(path.parent), base)
        rank = language_rank(stem)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, path)
    return [item[1] for item in best.values()]


def discover_transcripts(root: Path) -> list[Path]:
    """找待处理的文字稿。优先带时间戳的格式，纯 .json 放最后兜底。"""
    for pattern in ("*.transcript.json", "*.srt", "*.vtt", "*.json"):
        found = sorted(root.rglob(pattern))
        # 排除视频附带的其它 json（如 yt-dlp 的 .info.json 是元数据不是文字稿）
        found = [item for item in found if not item.name.endswith(".info.json")]
        if found:
            return sorted(dedupe_by_video(found))
    return []


def find_translation(original: Path, targets: list[str]) -> Path | None:
    """在同一目录里找原文轨对应的译文轨。

    yt-dlp 把 YouTube 的自动翻译轨命名成 `<目标语>-<源语>`，例如
    `zh-Hans-en` = 简体中文、从英语翻译；人工上传的译文轨则是裸语言码（如 `zh-Hant`）。
    两种都认，**优先源语与原文轨一致**的那条。

    同一支视频可能同时存在多条候选（实测 `zh-Hans-ar` / `zh-Hans-en` / `zh-Hans-fr`
    / `zh-Hans-ko` …，因为 YouTube 会按每种识别语言各生成一份），所以必须挑一条，
    不能全喂进去。

    注意：**不能用 glob 匹配文件名**——yt-dlp 的输出模板里带 `[id]`，
    方括号是 glob 的元字符，直接 glob 会一条都匹配不到。
    """
    if not targets:
        return None
    stem = transcript_stem(original.name)
    head, dot, tail = stem.rpartition(".")
    if not (dot and head and LANG_CODE.match(tail)):
        return None

    best: tuple[int, Path] | None = None
    for candidate in sorted(original.parent.iterdir()):
        if not candidate.is_file() or candidate == original:
            continue
        if not candidate.name.endswith((".srt", ".vtt", ".json")):
            continue
        if candidate.name.endswith(".info.json"):
            continue
        cstem = transcript_stem(candidate.name)
        chead, cdot, ctail = cstem.rpartition(".")
        if not (cdot and chead == head and LANG_CODE.match(ctail)):
            continue
        for order, target in enumerate(targets):
            if ctail == target:
                score = order                      # 裸语言码：人工译文，最优先
            elif ctail.startswith(f"{target}-"):
                source = ctail[len(target) + 1:]
                score = (10 if source == tail else 20) + order
            else:
                continue
            if best is None or score < best[0]:
                best = (score, candidate)
    return best[1] if best else None


VIDEO_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov", ".flv", ".m4v", ".avi", ".ts")

# yt-dlp 的语言码：`en`、`zh-Hans`、`en-orig`、`pt-BR`
LANG_CODE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")


def video_stem_candidates(stem: str) -> list[str]:
    """由「剥掉字幕后缀的茎」推出可能对应的视频茎，长的优先。

    只有两条规则：
      1. 末段像语言码（`en` / `zh-Hans` / `en-orig`）就摘掉——这是 yt-dlp 的命名；
      2. 退回到「第一个点之前」——这是老规则的兜底，保留是为了不破坏既有命名。

    **不能只靠规则 2**：取视频标题当文件名之后，标题里的点会把 `split(".")[0]` 截断
    （`20241105_A.I. and the future_xyz.en` → `20241105_A`），于是配不上视频。
    规则 1 按语言码的形状判断，不受标题里有多少个点影响。
    """
    candidates: list[str] = []
    if stem:
        candidates.append(stem)
    if "." in stem:
        head, _, tail = stem.rpartition(".")
        if head and LANG_CODE.match(tail):
            candidates.append(head)
        candidates.append(stem.split(".")[0])
    ordered: list[str] = []
    for item in candidates:
        if item and item not in ordered:
            ordered.append(item)
    return ordered


def sibling_video(transcript: Path) -> Path | None:
    """找与文字稿同名的视频。

    认两种命名：
      douyin-asr-pipeline：`X.mp4` 配 `X.mp4.transcript.json`
      yt-dlp：           `X.mp4` 配 `X.en.srt`（语言码夹在中间）
    """
    stem = transcript_stem(transcript.name)

    for base in video_stem_candidates(stem):
        for extension in VIDEO_SUFFIXES:
            candidate = transcript.parent / f"{base}{extension}"
            if candidate.is_file():
                return candidate
    return None


def renderer_flags(options, mode: str) -> list[str]:
    """把通用版式选项翻译成渲染器的参数。只做转发，不重新解释语义。"""
    flags: list[str] = []
    if options.aspect:
        flags += ["--aspect", options.aspect]
    if options.width:
        flags += ["--width", str(options.width)]
    if options.layout:
        flags += ["--layout", options.layout]
    if options.font:
        flags += ["--font", options.font]
    if mode == "native":
        if options.band_top is not None:
            flags += ["--band-top", str(options.band_top)]
        if options.band_bottom is not None:
            flags += ["--band-bottom", str(options.band_bottom)]
    else:
        if options.band_center is not None:
            flags += ["--band-center", str(options.band_center)]
        if options.frame_top is not None:
            flags += ["--frame-top", str(options.frame_top)]
        if options.frame_bottom is not None:
            flags += ["--frame-bottom", str(options.frame_bottom)]
    return flags


def render_selected(bundle: dict, video: Path, out_dir: Path, options) -> tuple[list[Path], list[str]]:
    """调用渲染器把选中的候选真正画成图。失败不抛异常，收集起来一并报告。

    有译文轨时**两套一起出**：原文进 `images/`、译文进 `images_zh/`，
    同一组分镜用同一批时间点，所以两种语言的图可以逐张对照。
    """
    import subprocess

    renderer = Path(__file__).resolve().parent / "native_subtitle_stitch.py"
    produced: list[Path] = []
    failures: list[str] = []

    if options.render_mode == "native":
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(renderer), "render", str(video),
            "--manifest", str(out_dir / "native_manifest.json"),
            "--out-dir", str(images_dir),
            *renderer_flags(options, "native"),
        ]
        if options.overwrite:
            command.append("--overwrite")
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            failures.append((result.stdout + result.stderr).strip()[:300])
        else:
            produced += sorted(images_dir.glob("*.jpg"))
        return produced, failures

    # 原生模式保留画面里已有的字幕，没有「换成中文」这回事，所以只有脚本模式出两套。
    sets = [(bundle.get("script_lines") or [], "scripts", "images")]
    if bundle.get("script_lines_zh") is not None:
        sets.append((bundle["script_lines_zh"], "scripts_zh", "images_zh"))

    for items, script_dirname, images_dirname in sets:
        images_dir = out_dir / images_dirname
        images_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
            target = images_dir / f"{item['order']:02d}_{safe}.jpg"
            command = [
                sys.executable, str(renderer), "render-script", str(video),
                "--script", str(out_dir / script_dirname / f"{item['order']:02d}_{safe}.json"),
                "--out", str(target),
                *renderer_flags(options, "script"),
            ]
            if options.overwrite:
                command.append("--overwrite")
            result = subprocess.run(command, capture_output=True, text=True)
            if target.is_file():
                produced.append(target)
            else:
                failures.append(f"{target.name}: {(result.stdout + result.stderr).strip()[:200]}")
    return produced, failures


def add_render_options(parser) -> None:
    """加出图相关的开关。默认不出图，只产出候选与时间点文件。"""
    parser.add_argument("--render", action="store_true",
                        help="选出候选后直接调用渲染器出图")
    parser.add_argument("--render-mode", choices=["script", "native"], default="script",
                        help="script: 把台词画到帧上；native: 保留画面已有字幕（默认 script）")
    parser.add_argument("--aspect", help="如 3:4 / 9:16 / 1:1")
    parser.add_argument("--width", type=int)
    parser.add_argument("--layout", choices=["fixed", "natural"])
    parser.add_argument("--font", help="中文字体文件；竖屏建议显式指定")
    parser.add_argument("--band-top", type=float)
    parser.add_argument("--band-bottom", type=float)
    parser.add_argument("--band-center", type=float)
    parser.add_argument("--frame-top", type=float)
    parser.add_argument("--frame-bottom", type=float)
    parser.add_argument("--overwrite", action="store_true")


def add_translate_option(parser) -> None:
    """译文轨：给了就额外产出一套译文脚本与图，原文那一套照旧保留。"""
    parser.add_argument(
        "--translate",
        help="要产出的译文语言，如 zh-Hans（可逗号分隔多个）。会在文字稿同目录里找 "
             "yt-dlp 的 `<目标语>-<源语>` 译文轨（如 zh-Hans-en）。不给就只出原文一套。",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="从带时间戳文字稿里自动挑选值得传播的句子")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("pick", help="处理单个文字稿")
    one.add_argument("transcript", type=Path)
    one.add_argument("--video", type=Path)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--top", type=int, default=3, help="最多产出几张图，默认 3")
    add_translate_option(one)
    add_render_options(one)

    many = sub.add_parser("batch", help="批量处理一个目录下的全部文字稿")
    many.add_argument("repo", type=Path)
    many.add_argument("--out", type=Path, required=True)
    many.add_argument("--top", type=int, default=3)
    many.add_argument("--per-category", type=int, default=None,
                      help="每个顶层分类最多处理几个视频，默认全部")
    many.add_argument("--limit", type=int, default=None, help="全局最多处理几个视频")
    add_translate_option(many)
    add_render_options(many)

    args = parser.parse_args()

    if args.command == "pick":
        if not args.transcript.exists():
            raise SystemExit(f"找不到文字稿：{args.transcript}")
        result = process_one(args.transcript, args.out, args.top, args.video, args)
        print(
            f"{result['lines']} 行 -> 候选 {result['candidates']} 个 -> "
            f"选出 {result['chosen']} 组；丢弃 {result['dropped']} 段；输出 {result['out']}"
        )
        if args.render:
            print(f"出图 {result['rendered']} 张")
            for message in result["render_failures"][:5]:
                print(f"  ⚠ {message}")
        return

    root = args.repo.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"不是目录：{root}")
    files = discover_transcripts(root)
    if not files:
        raise SystemExit(f"{root} 下没找到 .transcript.json")

    by_category: dict[str, list[Path]] = {}
    for path in files:
        relative = path.relative_to(root)
        category = relative.parts[0] if len(relative.parts) > 1 else "_root"
        by_category.setdefault(category, []).append(path)

    processed = 0
    rendered_total = 0
    failures: list[tuple[str, str]] = []
    for category in sorted(by_category):
        items = by_category[category]
        if args.per_category is not None:
            items = items[: args.per_category]
        for path in items:
            if args.limit is not None and processed >= args.limit:
                break
            target = args.out / category / path.name.replace(".transcript.json", "")
            try:
                result = process_one(path, target, args.top, None, args)
            except SystemExit as exc:
                failures.append((path.name[:50], str(exc)))
                continue
            processed += 1
            rendered_total += result["rendered"]
            note = f" / 出图 {result['rendered']}" if args.render else ""
            print(f"[{processed}] {category}/{path.name[:34]} -> {result['chosen']} 组 / {result['lines']} 行{note}")
            for message in result["render_failures"][:2]:
                failures.append((f"{path.name[:30]} 渲染", message))
        if args.limit is not None and processed >= args.limit:
            break

    print(f"\n处理 {processed} 个视频，输出到 {args.out}")
    if args.render:
        print(f"共出图 {rendered_total} 张")
    if failures:
        print(f"跳过/报错 {len(failures)} 条：")
        for name, reason in failures[:10]:
            print(f"  - {name}: {reason}")

    if processed:
        stats = aggregate(args.out, top=max(20, args.top * 6))
        print(f"\n全局精选：{stats['selected']} 条入选 / {stats['candidates']} 个候选窗口")
        print(f"  {args.out / '全局精选.md'}")


if __name__ == "__main__":
    main()
