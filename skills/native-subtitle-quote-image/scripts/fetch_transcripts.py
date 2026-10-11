#!/usr/bin/env python3
"""给频道 / 播放列表批量取文字稿——**默认只取字幕，不下载视频**。

为什么默认不下载视频：挑句子只需要文字稿。一个 200 条视频的频道，
全量下载是几十 GB 和几小时；只取字幕是几分钟。
正确顺序是**先选句、再下载被选中的那几条**：

    # 1. 只取字幕（快）
    fetch_transcripts.py "https://www.youtube.com/@频道/videos" --out source/

    # 2. 选句，产出审阅表与全局精选
    select_quotes.py batch source/ --out work/ --top 5

    # 3. 人看过审阅表后，只给要出图的那几条补视频
    fetch_transcripts.py --ids "8S0FDjFBj8o,dQw4w9WgXcQ" --out source/ --videos

    # 4. 出图（视频已在手，批处理会自动配对）
    select_quotes.py batch source/ --out work/ --render --render-mode script

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


def subtitle_args(languages: str, sub_format: str) -> list[str]:
    return [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", languages,
        "--sub-format", sub_format,
    ]


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
    parts += subtitle_args(args.lang, args.sub_format) if not videos else video_args()
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


def run(command: list[str]) -> int:
    printable = " ".join(command)
    print(f"  $ {printable[:160]}{'…' if len(printable) > 160 else ''}")
    result = subprocess.run(command, text=True, encoding="utf-8", errors="replace")
    return result.returncode


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

    targets = targets_for(args, ids)
    print(f"\n[2/2] 拉取{'字幕与视频' if args.videos else '字幕'}（{len(targets)} 次调用）…")
    for target in targets:
        run(build_command(command, target, out_dir, args, videos=args.videos))

    if args.videos:
        print("\n已同时拉取视频，可直接出图：")
        print(f"  select_quotes.py batch {out_dir} --out work/ --render --render-mode script")
    else:
        print("\n下一步（挑句子，不需要视频）：")
        print(f"  select_quotes.py batch {out_dir} --out work/ --top 5")

    manifest = {
        "source_url": args.url,
        "yt_dlp": yt_dlp_version(command),
        "languages": args.lang,
        "sub_format": args.sub_format,
        "videos_downloaded": bool(args.videos),
        "entries": [
            {
                **entry,
                "subtitles": [str(p) for p in matching_subtitles(out_dir, entry["id"])],
                "videos": [str(p) for p in matching_videos(out_dir, entry["id"])],
            }
            for entry in selected
        ],
    }
    missing = [item for item in manifest["entries"] if not item["subtitles"]]
    manifest["without_subtitles"] = [item["id"] for item in missing]
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
