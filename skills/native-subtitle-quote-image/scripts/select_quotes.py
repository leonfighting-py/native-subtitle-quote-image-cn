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
    """读 .transcript.json / .json / .srt，返回 (segments, duration, meta)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".srt":
        parsed, inferred = parse_srt(text)
        return parsed, inferred, {"source": "srt"}

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        raise SystemExit(
            f"{path} 看起来不是 JSON 也不是 SRT。只有纯文本、没有时间戳的 .txt "
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


def parse_srt(text: str) -> tuple[list[dict], float | None]:
    """解析 SRT。SRT 没有总时长字段，用末段时间当兜底。"""
    segments = []
    pattern = re.compile(
        r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
    )
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = pattern.search(block)
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(value) for value in match.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        body = " ".join(
            line.strip() for line in lines if not pattern.search(line) and not line.strip().isdigit()
        ).strip()
        if body:
            segments.append({"start": start, "end": end, "text": body})
    if not segments:
        raise SystemExit("SRT 里没有解析出任何字幕块")
    return segments, max(seg["end"] for seg in segments)


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
                previous["text"] = (previous["text"] + text).strip()
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
                    "text": "".join(item["text"] for item in chunk),
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
    windows: list[dict], top: int, max_line_overlap: float = 0.34, min_line_gap: int = 3
) -> list[dict]:
    """按分数贪心选，避免几张图讲同一件事。

    两道去重：共享行数比例过高，或起始行挨得太近，都视为重复。
    实测只靠时间重叠不够——两个窗口可以时间上只压边，却共享大部分行。
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
            if abs(candidate["index"] - picked["index"]) < min_line_gap:
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
    """标题只做文件名和内部导航，不会被画进图片。"""
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", window["text"])
    return text[:limit] or "未命名"


def build_outputs(windows: list[dict], top: int) -> dict:
    """产出两套可直接渲染的文件。

    原生模式：render 的 --manifest 本身就接受多张图，所以汇总成一个 JSON。
    脚本模式：render-script 一次只画一张（--script 是单个 {"lines":[...]}），
    所以必须拆成一张一个文件，绝不能塞成数组——实测踩过，会直接报
    「台词脚本必须包含至少 2 项的 lines 数组」。
    """
    chosen = pick_windows(windows, top)
    images, scripts = [], []
    for index, window in enumerate(chosen, 1):
        times = choose_times(window["lines"])
        title = make_title(window)
        images.append({"title": title, "times": times})
        lines = [
            {"t": round((line["start"] + line["end"]) / 2, 2), "text": line["text"]}
            for line in window["lines"]
        ]
        lines = [item for item in lines if item["t"] > 0][:MAX_TIMES_PER_IMAGE]
        if len(lines) >= MIN_LINES:
            scripts.append(
                {"order": index, "title": title, "start": window["start"], "lines": lines}
            )
    return {"native_manifest": {"images": images}, "script_lines": scripts, "chosen": chosen}


def render_commands(scripts: list[dict], video: str | None) -> str:
    """生成可直接执行的渲染命令，省掉手工拼 60 条命令。"""
    target = video or "请填入源视频路径.mp4"
    out = [
        "#!/usr/bin/env bash",
        "# 由 select_quotes.py 生成：逐张渲染脚本字幕。",
        "# render-script 一次只画一张，所以这里必须是循环。",
        "set -euo pipefail",
        "",
        f'PYTHON="${{PYTHON:-python3}}"',
        f'VIDEO="${{VIDEO:-{target}}}"',
        f'OUT="${{OUT:-{target}.quotes}}"',
        'SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'mkdir -p "$OUT"',
        "",
    ]
    for item in scripts:
        safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
        out.append(
            f'"$PYTHON" "$SKILL_DIR/scripts/native_subtitle_stitch.py" render-script "$VIDEO" \\\n'
            f'  --script "scripts/{item["order"]:02d}_{safe}.json" \\\n'
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
) -> str:
    out = [f"# 候选审阅表：{source}", ""]
    out.append(f"- 总时长：{duration:.2f}s" if duration else "- 总时长：未知")
    if duration_source == "video":
        out.append(
            f"- 时长来源：视频实测（转写声明 {declared:.2f}s，偏差 {drift:+.2f}s）"
            if declared and drift is not None
            else "- 时长来源：视频实测"
        )
    else:
        out.append("- 时长来源：转写自带（没找到同名视频，时间点未经真实时长校正）")
    out.append(f"- 转写模型：{meta.get('model') or '未知'}")
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
    out.append("4. **不要跳过人工确认**：本表只按形式特征排序，没有判断语义是否真的成立；")
    out.append("   转写本身也有错字（实测如「约数条件」应为「约束条件」），脚本模式必须先校对。")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 命令


def probe_duration(video: Path) -> float | None:
    """只在 --video 给了的时候用，避免和渲染脚本重复实现取帧逻辑。"""
    import subprocess

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(video)],
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
    bundle = build_outputs(windows, top)
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
    (out_dir / "script_lines.json").write_text(
        json.dumps(bundle["script_lines"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 脚本字幕模式：一张图一个文件，因为 render-script 一次只画一张
    script_dir = out_dir / "scripts"
    script_dir.mkdir(exist_ok=True)
    for item in bundle["script_lines"]:
        safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
        (script_dir / f"{item['order']:02d}_{safe}.json").write_text(
            json.dumps({"lines": item["lines"]}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    shell = out_dir / "渲染脚本字幕.sh"
    shell.write_text(render_commands(bundle["script_lines"], str(video) if video else None), encoding="utf-8")
    shell.chmod(0o755)
    (out_dir / "审阅表.md").write_text(
        render_review(
            transcript_path.name, meta, duration, dropped, windows, chosen, top,
            duration_source, declared, drift,
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


def discover_transcripts(root: Path) -> list[Path]:
    found = sorted(root.rglob("*.transcript.json"))
    return found or sorted(root.rglob("*.json"))


def sibling_video(transcript: Path) -> Path | None:
    """找与文字稿同名的视频。

    douyin-asr-pipeline 这类流水线把 X.mp4 和 X.mp4.transcript.json 放在一起，
    所以批处理时不必手工指定视频路径——认得出兄弟文件就能直接出图。
    """
    for suffix in (".transcript.json", ".transcript.srt", ".json", ".srt"):
        if transcript.name.endswith(suffix):
            stem = transcript.name[: -len(suffix)]
            candidate = transcript.parent / stem
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
    """调用渲染器把选中的候选真正画成图。失败不抛异常，收集起来一并报告。"""
    import subprocess

    renderer = Path(__file__).resolve().parent / "native_subtitle_stitch.py"
    produced: list[Path] = []
    failures: list[str] = []
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    if options.render_mode == "native":
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

    for item in bundle["script_lines"]:
        safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", item["title"])
        target = images_dir / f"{item['order']:02d}_{safe}.jpg"
        command = [
            sys.executable, str(renderer), "render-script", str(video),
            "--script", str(out_dir / "scripts" / f"{item['order']:02d}_{safe}.json"),
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


def main() -> None:
    parser = argparse.ArgumentParser(description="从带时间戳文字稿里自动挑选值得传播的句子")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("pick", help="处理单个文字稿")
    one.add_argument("transcript", type=Path)
    one.add_argument("--video", type=Path)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--top", type=int, default=3, help="最多产出几张图，默认 3")
    add_render_options(one)

    many = sub.add_parser("batch", help="批量处理一个目录下的全部文字稿")
    many.add_argument("repo", type=Path)
    many.add_argument("--out", type=Path, required=True)
    many.add_argument("--top", type=int, default=3)
    many.add_argument("--per-category", type=int, default=None,
                      help="每个顶层分类最多处理几个视频，默认全部")
    many.add_argument("--limit", type=int, default=None, help="全局最多处理几个视频")
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
