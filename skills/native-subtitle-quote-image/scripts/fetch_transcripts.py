#!/usr/bin/env python3
"""给频道 / 播放列表批量取文字稿——**默认只取字幕，不下载视频**。

为什么默认不下载视频：挑句子只需要文字稿。一个 200 条视频的频道，
全量下载是几十 GB 和几小时；只取字幕是几分钟。
正确顺序是**先选句、再下载被选中的那几条**：

    # 1. 只取字幕（快）；--translate 额外拉 YouTube 自带译文轨
    fetch_transcripts.py "https://www.youtube.com/@频道/videos" --out source/ --translate zh-Hans

    # 2. 选句，产出审阅表与全局精选（原文、译文两套都在同一份审阅表里）
    select_quotes.py batch source/ --out work/ --top 5 --translate zh-Hans

    # 3. 人看过审阅表后，只给要出图的那几条补视频
    fetch_transcripts.py --ids "8S0FDjFBj8o,dQw4w9WgXcQ" --out source/ --videos

    # 4. 出图（视频已在手，批处理会自动配对；原文一套、译文一套）
    select_quotes.py batch source/ --out work/ --render --render-mode script --translate zh-Hans

只依赖 `yt-dlp` 命令行，不 import 它的 Python 包——这样 yt-dlp 既可以是
官方独立可执行文件，也可以是 pip 装的，和这个 skill 其它脚本的假设保持一致。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 文件名里带上日期和标题便于人看，id 放最后保证唯一。
# 标题里可能有 `/`、`:` 等字符，yt-dlp 会自行清理；长度截断防止超长。
OUTPUT_TEMPLATE = "%(upload_date>%Y-%m-%d)s_%(title).80s_[%(id)s].%(ext)s"

SUBTITLE_SUFFIXES = (".srt", ".vtt")
ARCHIVE_NAME = ".fetched.txt"

# **不用通配符**。`zh.*,en.*` 会连 YouTube 的自动翻译轨一起匹配
# （标签形如 `zh-Hans-en`），一个中文频道能落下几十个同义文件；
# 实测一个英文频道也被拉下 `en` / `en-orig` / `en-en` 三份，其中 `en-en` 与 `en` 逐字节相同。
# 这里只列「最可能是原声」的具体标签，按语言分组，用户可按频道语言调整。
DEFAULT_LANGS = "zh-Hans,zh-Hant,zh-TW,zh,en-orig,en"

# 译文轨撞 429 是**瞬时**的：同一个进程里主字幕下得好好的，紧接着的
# `zh-Hans-en` 直接 429，换格式、加 --sleep-subtitles 都没用，
# 但退避几十秒后重试**第一次就成功**。所以值得重试，而且是脚本内置行为，
# 不要指望用户自己再跑一遍（--download-archive 保证重试不会重复下载）。
DEFAULT_RETRY_DELAYS = "0,45,120,300"

# 429 是实测到的；403 在视频下载时出现，同属 IP 限流，一起退避。
RETRYABLE = re.compile(r"HTTP Error (?:429|403)|Too Many Requests", re.I)

# 文件名尾部的语言标签：`...[id].zh-Hans-en.srt` → `zh-Hans-en`
SUBTITLE_TAG = re.compile(r"\.([A-Za-z][A-Za-z0-9-]*)\.(?:srt|vtt)$", re.I)


# ---------------------------------------------------------------- 前置检查


def resolve_yt_dlp() -> list[str] | None:
    """返回调用 yt-dlp 的命令前缀，找不到返回 None。

    优先 PATH 上的可执行文件（官方独立版就是这种），退回到
    `python -m yt_dlp`（pip 装的情况）。
    """
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode == 0:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def yt_dlp_version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command + ["--version"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


# ---------------------------------------------------------------- 枚举条目


def parse_flat_playlist(text: str) -> list[dict]:
    """解析 `--flat-playlist --dump-json` 的 JSONL 输出。

    yt-dlp 在出错时会往 stdout 混进非 JSON 的行，所以**逐行解析、跳过解析不了的行**，
    不能整段 `json.loads`。
    """
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        video_id = payload.get("id")
        if not video_id:
            continue
        entries.append(
            {
                "id": str(video_id),
                "title": payload.get("title") or "",
                "duration": payload.get("duration"),
                "url": payload.get("url") or payload.get("webpage_url") or "",
            }
        )
    return entries


def select_entries(entries: list[dict], limit: int | None, ids: list[str]) -> list[dict]:
    """按 --limit / --ids 收窄待处理范围，保持原顺序。"""
    selected = entries
    if ids:
        wanted = [item.strip() for item in ids if item.strip()]
        index = {entry["id"]: entry for entry in entries}
        selected = []
        for key in wanted:
            # 允许直接给 id，也允许给完整链接
            video_id = key
            match = re.search(r"[?&]v=([\w-]{6,})", key) or re.search(r"youtu\.be/([\w-]{6,})", key)
            if match:
                video_id = match.group(1)
            entry = index.get(video_id)
            if entry is None:
                # 枚举结果里没有（例如 --limit 截断过），按 id 现造一条
                entry = {"id": video_id, "title": "", "duration": None, "url": key}
            selected.append(entry)
    if limit is not None:
        selected = selected[:limit]
    return selected


def enumerate_entries(
    command: list[str], url: str, extra: list[str], limit: int | None = None
) -> list[dict]:
    """列出频道 / 播放列表里的条目（只读，不下载）。

    带 `--playlist-end`：一个几千条的频道全量枚举要好几分钟，
    而我们这次最多只要前 `limit` 条，没必要列完。
    """
    args = ["--flat-playlist", "--dump-json", "--ignore-errors"]
    if limit:
        args += ["--playlist-end", str(limit)]
    result = subprocess.run(
        command + args + [*extra, url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise SystemExit(
            "yt-dlp 没能列出条目。原始输出：\n"
            + (result.stderr or "").strip()[-1500:]
        )
    return parse_flat_playlist(result.stdout)


# ---------------------------------------------------------------- 取字幕


def split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def translation_tracks(translate: str | None, sources: str | None) -> list[str]:
    """把 `--translate zh-Hans` 展开成 yt-dlp 认得的译文轨标签。

    YouTube 的译文轨**按源语各自生成一份**（`zh-Hans-en`、`zh-Hans-ko`、
    `zh-Hans-fr`…），目标语只有一个、轨道却有一大把。所以要把每个
    「可能的源语」都请求一遍——源语就是 `--lang` 里那些原声标签。
    顺便请求裸的 `zh-Hans`：那是人工译本，有就直接用，没有也无妨。

    注意 `--sub-langs` 用的是 **fullmatch**（见 yt-dlp 的
    `orderedSet_from_options(..., use_regex=True)`），所以裸的 `zh-Hans`
    **不会**顺带匹配 `zh-Hans-en`——想要译文轨就得把全名写出来。
    """
    tracks: list[str] = []
    for target in split_csv(translate):
        if target not in tracks:
            tracks.append(target)
        for source in split_csv(sources):
            if source == target:
                continue
            track = f"{target}-{source}"
            if track not in tracks:
                tracks.append(track)
    return tracks


def subtitle_args(languages: str, sub_format: str, translate: str = "") -> list[str]:
    langs = split_csv(languages)
    for track in translation_tracks(translate, languages):
        if track not in langs:
            langs.append(track)
    return [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", ",".join(langs),
        "--sub-format", sub_format,
    ]


def translation_files(paths: list, translate: str | None) -> list:
    """从已落地的字幕里挑出译文轨，供清单标注。

    译文轨的标签一定等于目标语、或以 `目标语-` 开头（`zh-Hans-en`）。
    """
    targets = split_csv(translate)
    if not targets:
        return []
    picked = []
    for path in paths:
        match = SUBTITLE_TAG.search(path.name)
        tag = match.group(1) if match else ""
        if any(tag == target or tag.startswith(f"{target}-") for target in targets):
            picked.append(path)
    return picked


def video_args() -> list[str]:
    """下载视频。1080p 足够出社交图，见 yt-dlp-and-transcripts.md 的回退表。"""
    return ["-f", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4"]


def matching_subtitles(root: Path, video_id: str) -> list[Path]:
    """找出某个 id 已落地的字幕文件。

    文件名由模板生成（`日期_标题_[id].en.srt`），所以按 `[id]` 标记匹配而不是
    去猜完整的文件名。
    """
    marker = f"[{video_id}]"
    found = [
        path
        for path in root.rglob("*")
        if path.suffix.lower() in SUBTITLE_SUFFIXES and marker in path.name
    ]
    return sorted(found)


def matching_videos(root: Path, video_id: str) -> list[Path]:
    marker = f"[{video_id}]"
    return sorted(
        path
        for path in root.rglob("*")
        if path.suffix.lower() in (".mp4", ".mkv", ".webm") and marker in path.name
    )


def build_command(
    command: list[str], target: str, out_dir: Path, args, *, videos: bool
) -> list[str]:
    """拼出一次 yt-dlp 调用。字幕和视频**分开两次跑**。

    字幕请求容易撞 429，视频下载容易撞 403；合成一条命令的话，
    字幕失败会连累视频、视频失败会让字幕白下。见 yt-dlp-and-transcripts.md。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = command + [
        "--ignore-errors",
        "--no-overwrites",
        "--download-archive", str(out_dir / ARCHIVE_NAME),
        "--sleep-subtitles", str(args.sleep),
        "--sleep-requests", "1",
        "-o", str(out_dir / OUTPUT_TEMPLATE),
    ]
    if args.cookies_from_browser:
        parts += ["--cookies-from-browser", args.cookies_from_browser]
    if args.proxy:
        parts += ["--proxy", args.proxy]
    if not videos and isinstance(args.limit, int) and args.limit > 0:
        parts += ["--playlist-end", str(args.limit)]
    if videos:
        parts += video_args()
    else:
        parts += subtitle_args(
            args.lang, args.sub_format, getattr(args, "translate", "") or ""
        )
    parts.append(target)
    return parts


def targets_for(args, ids: list[str]) -> list[str]:
    """决定这次要喂给 yt-dlp 的目标。

    - 给了 `--ids`：一条一个目标，**顺手只下这几条**，不去碰整个频道；
    - 只给了 url：整体一次调用，靠 `--download-archive` 做增量（重跑只补新的）。

    非 YouTube 的来源不做 URL 拼接——拼错了不如报错让人自己填。
    """
    if ids:
        built = []
        for item in ids:
            if item.startswith(("http://", "https://")):
                built.append(item)
            elif re.fullmatch(r"[\w-]{6,}", item):
                built.append(f"https://www.youtube.com/watch?v={item}")
            else:
                raise SystemExit(f"--ids 里这一项既不像链接也不像 id：{item}")
        return built
    return [args.url]


def parse_delays(value: str | None) -> tuple[float, ...]:
    """`"0,45,120"` → `(0.0, 45.0, 120.0)`。

    第一项是「首次尝试前的等待」，所以默认的 `0,45,120,300` 表示
    **最多试 4 次**。`--retry-delays ""` 或 `"0"` 就是只试一次、不重试。
    """
    delays: list[float] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            delays.append(max(0.0, float(item)))
        except ValueError:
            raise SystemExit(f"--retry-delays 里这一项不是数字：{item}")
    return tuple(delays) or (0.0,)


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines())


def run(command: list[str], delays: tuple[float, ...] = (0.0,)) -> int:
    """跑一次 yt-dlp；输出**捕获后原样转印**，这样能识别限流并退避重试。

    直接 `subprocess.run(...)` 让子进程自己往终端写是不行的——那样就拿不到
    「这次是 429」这个信息。所以这里改成捕获再打印，代价是进度条不再是
    实时的（一次性吐出），可以接受。

    **判断限流只能认输出文本，不能看退出码**：命令里带了 `--ignore-errors`，
    而 yt-dlp 的 `trouble()` 在 ignoreerrors 下**跳过** `_download_retcode = 1`，
    所以一个 429 会以「退出码 0 + stderr 里一行 ERROR」的形式静悄悄过去。
    早先那版 `run()` 不看输出，等于把这些失败全吞了。

    重试是安全的：命令里带 `--no-overwrites` 和 `--download-archive`，
    已经下到的文件不会重下，重试只补缺的那些。
    """
    printable = " ".join(command)
    print(f"  $ {printable[:160]}{'…' if len(printable) > 160 else ''}")
    result = None
    for attempt, delay in enumerate(delays):
        if delay:
            print(f"    撞限流，等 {delay:.0f}s 后重试（第 {attempt + 1} 次尝试）…")
            time.sleep(delay)
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if output:
            print(_indent(output))
        if not RETRYABLE.search(output):
            return result.returncode
    return result.returncode if result is not None else 1


# ---------------------------------------------------------------- 主流程


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="给频道 / 播放列表批量取文字稿（默认只取字幕，不下载视频）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型顺序：\n"
            "  1) fetch_transcripts.py 频道URL --out source/      只取字幕\n"
            "  2) select_quotes.py batch source/ --out work/      选句，产出审阅表\n"
            "  3) fetch_transcripts.py --ids A,B --out source/ --videos   只补选中的视频\n"
            "  4) select_quotes.py batch source/ --out work/ --render     出图\n"
        ),
    )
    parser.add_argument("url", nargs="?", help="频道 / 播放列表 / 单条视频的链接")
    parser.add_argument("--out", required=True, help="输出目录（字幕与视频都放这里）")
    parser.add_argument("--ids", default="",
                        help="只处理这些 id 或链接（逗号分隔）。给了就不需要 url")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理多少条（默认不限）")
    parser.add_argument("--lang", default=DEFAULT_LANGS,
                        help=f'字幕语言，默认 "{DEFAULT_LANGS}"。'
                             '**不要写 `zh.*,en.*` 这种通配**——那会把自动翻译轨'
                             '（`zh-Hans-en`、`en-en`）一起拉下来，'
                             '一个中文频道能给你几十个同义文件')
    parser.add_argument("--sub-format", default="srt/vtt",
                        help='字幕格式，默认 "srt/vtt"（优先 srt）')
    parser.add_argument("--translate", default="",
                        help='顺带拉 YouTube 自带译文轨，如 "zh-Hans"（逗号分隔可给多个）。'
                             '会按 `--lang` 里的每个原声标签展开成 `zh-Hans-en` 这类'
                             '轨道——同一目标语在 YouTube 上按源语各有一份，'
                             '所以要把可能的源语都请求一遍。**原声轨照常保留**，'
                             '两套并存。译文轨经常撞 429，脚本会自动退避重试')
    parser.add_argument("--retry-delays", default=DEFAULT_RETRY_DELAYS,
                        help=f'撞限流后的退避秒数，默认 "{DEFAULT_RETRY_DELAYS}"'
                             '（= 最多试 4 次）。给 "0" 就是不重试')
    parser.add_argument("--videos", action="store_true",
                        help="同时下载视频（默认只取字幕）")
    parser.add_argument("--cookies-from-browser", default=None,
                        help="需要登录时指定浏览器，如 chrome。默认不使用 Cookie")
    parser.add_argument("--proxy", default=None, help="http(s):// 代理地址")
    parser.add_argument("--sleep", type=float, default=5.0,
                        help="字幕请求间隔秒数，默认 5")
    parser.add_argument("--manifest", default="sources.json",
                        help="清单文件名，默认 sources.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出将要处理的条目，不下载")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    out_dir = Path(args.out)

    ids = [item for item in args.ids.split(",") if item.strip()]
    if not args.url and not ids:
        raise SystemExit("必须给一个 url，或者用 --ids 指定要处理的条目")

    command = resolve_yt_dlp()
    if command is None:
        raise SystemExit(
            "找不到 yt-dlp。安装方式见 references/yt-dlp-and-transcripts.md：\n"
            '  python3 -m pip install -U "yt-dlp[default]"\n'
            "或使用官方独立可执行文件。"
        )
    print(f"yt-dlp: {yt_dlp_version(command)}")
    print(f"输出目录: {out_dir}")

    if args.url:
        print("\n[1/2] 列出条目…")
        entries = enumerate_entries(command, args.url, proxy_args(args), args.limit)
    else:
        entries = [{"id": item, "title": "", "duration": None, "url": item} for item in ids]

    selected = select_entries(entries, args.limit, ids)
    if not selected:
        raise SystemExit("没有匹配到任何条目")
    total_seconds = sum(
        entry["duration"] for entry in selected if isinstance(entry["duration"], (int, float))
    )
    print(f"  共 {len(entries)} 条，本次处理 {len(selected)} 条"
          + (f"，合计 {total_seconds / 60:.0f} 分钟" if total_seconds else ""))
    for entry in selected:
        print(f"    - {entry['id']}  {entry['title'][:60]}")

    if args.dry_run:
        print("\n--dry-run：未下载任何内容。")
        return

    delays = parse_delays(args.retry_delays)
    targets = targets_for(args, ids)
    tracks = translation_tracks(getattr(args, "translate", ""), args.lang)
    if tracks and not args.videos:
        print(f"  译文轨: {', '.join(tracks)}")
    print(f"\n[2/2] 拉取{'字幕与视频' if args.videos else '字幕'}（{len(targets)} 次调用）…")
    for target in targets:
        run(build_command(command, target, out_dir, args, videos=args.videos), delays)

    # 选句时把 --translate 一起带上，否则第二套（中文）不会产出
    hint = f" --translate {args.translate}" if args.translate and not args.videos else ""
    if args.videos:
        print("\n已同时拉取视频，可直接出图：")
        print(f"  select_quotes.py batch {out_dir} --out work/ --render"
              f" --render-mode script{hint}")
    else:
        print("\n下一步（挑句子，不需要视频）：")
        print(f"  select_quotes.py batch {out_dir} --out work/ --top 5{hint}")

    def with_files(entry: dict) -> dict:
        subs = matching_subtitles(out_dir, entry["id"])
        return {
            **entry,
            "subtitles": [str(p) for p in subs],
            "translations": [str(p) for p in translation_files(subs, args.translate)],
            "videos": [str(p) for p in matching_videos(out_dir, entry["id"])],
        }

    manifest = {
        "source_url": args.url,
        "yt_dlp": yt_dlp_version(command),
        "languages": args.lang,
        "translate": args.translate,
        "sub_format": args.sub_format,
        "videos_downloaded": bool(args.videos),
        "entries": [with_files(entry) for entry in selected],
    }
    missing = [item for item in manifest["entries"] if not item["subtitles"]]
    manifest["without_subtitles"] = [item["id"] for item in missing]
    if args.translate:
        # 有原声、没译文的条目单独报：它们照样能出英文图，只是没有中文版
        bare = [item["id"] for item in manifest["entries"]
                if item["subtitles"] and not item["translations"]]
        manifest["without_translations"] = bare
    (out_dir / args.manifest).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n清单: {out_dir / args.manifest}")
    got = len(selected) - len(missing)
    print(f"拿到字幕 {got}/{len(selected)} 条")
    if missing:
        print("以下条目没有可用字幕（这些必须走本地 Whisper 转写）：")
        for item in missing:
            print(f"  - {item['id']}  {item['title'][:60]}")
    if args.translate and manifest.get("without_translations"):
        print(f"以下条目只有原声、没拿到译文轨（仍可出图，只是没有"
              f"{args.translate} 版；可稍后单独重跑 --ids 补）：")
        for video_id in manifest["without_translations"]:
            print(f"  - {video_id}")


def proxy_args(args) -> list[str]:
    """枚举阶段也要带上 Cookie / 代理，否则频道是私有的就列不出来。"""
    extra: list[str] = []
    if args.cookies_from_browser:
        extra += ["--cookies-from-browser", args.cookies_from_browser]
    if args.proxy:
        extra += ["--proxy", args.proxy]
    return extra


if __name__ == "__main__":
    main()
