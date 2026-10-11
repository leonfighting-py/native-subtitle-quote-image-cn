"""`fetch_transcripts.py` 的测试。

脚本本身只做「拼 yt-dlp 命令 + 整理清单」这两件事，所以测试也分两层：
  - 纯函数：解析 JSONL、按 --limit/--ids 收窄、拼命令。不起子进程。
  - 端到端：**用一个假的 yt-dlp 可执行文件**跑完整流程，验证清单和落盘文件名。
    假脚本放在临时目录并前置到 PATH，所以不依赖网络、也不碰真实 YouTube。
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "native-subtitle-quote-image" / "scripts" / "fetch_transcripts.py"
SPEC = importlib.util.spec_from_file_location("fetch_transcripts", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


FAKE_YTDLP = '''#!{python}
"""假 yt-dlp：只实现本脚本用到的三个分支，不联网。

真 yt-dlp 拿到播放列表链接会**一次把所有条目都下下来**，
所以这里也要按播放列表逐条造文件，否则测不出「一个视频一条字幕」的行为。
"""
import json
import os
import re
import sys
from pathlib import Path

argv = sys.argv[1:]

if "--version" in argv:
    print("9999.01.01")
    sys.exit(0)

entries = [json.loads(line) for line in
           Path({playlist!r}).read_text(encoding="utf-8").splitlines() if line.strip()]

if "--dump-json" in argv:
    for entry in entries:
        print(json.dumps(entry))
    sys.exit(0)

template = argv[argv.index("-o") + 1]
url = argv[-1]

if "list=" in url or "/@" in url:
    video_ids = [entry["id"] for entry in entries]
    if "--playlist-end" in argv:
        end = int(argv[argv.index("--playlist-end") + 1])
        video_ids = video_ids[:end]
else:
    found = re.search(r"[?&]v=([\\w-]+)", url) or re.search(r"youtu\\.be/([\\w-]+)", url)
    video_ids = [found.group(1) if found else "unknown"]

skip = set(filter(None, os.environ.get("FAKE_SKIP_IDS", "").split(",")))

for video_id in video_ids:
    if video_id in skip:
        print("WARNING: no subtitles for", video_id)
        continue
    name = (template
            .replace("%(upload_date>%Y-%m-%d)s", "2024-01-01")
            .replace("%(title).80s", "Fake Title")
            .replace("%(id)s", video_id)
            .replace("%(ext)s", "en.srt"))
    target = Path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("1\\n00:00:00,000 --> 00:00:02,000\\nHello world\\n", encoding="utf-8")
    print("fake-downloaded", target.name)
'''

PLAYLIST = "\n".join(
    [
        json.dumps({"id": "aaaaaaaaaaa", "title": "First talk", "duration": 600}),
        json.dumps({"id": "bbbbbbbbbbb", "title": "Second talk", "duration": 900}),
        json.dumps({"id": "ccccccccccc", "title": "Third talk", "duration": 300}),
    ]
)


class FakeYtDlp:
    """把假 yt-dlp 放进临时目录，前置到 PATH，用完清理。"""

    def __init__(self, folder: Path, skip_ids: tuple[str, ...] = ()):
        self.skip_ids = skip_ids
        self.bindir = folder / "bin"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.playlist = folder / "playlist.jsonl"
        self.playlist.write_text(PLAYLIST, encoding="utf-8")
        target = self.bindir / "yt-dlp"
        target.write_text(
            FAKE_YTDLP.format(python=sys.executable, playlist=str(self.playlist)),
            encoding="utf-8",
        )
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def env(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        if self.skip_ids:
            env["FAKE_SKIP_IDS"] = ",".join(self.skip_ids)
        return env


class ParsePlaylistTests(unittest.TestCase):
    def test_parses_jsonl(self):
        entries = MODULE.parse_flat_playlist(PLAYLIST)
        self.assertEqual([e["id"] for e in entries],
                         ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
        self.assertEqual(entries[0]["title"], "First talk")
        self.assertEqual(entries[1]["duration"], 900)

    def test_skips_lines_that_are_not_json(self):
        """yt-dlp 会在 stdout 混进警告和进度行，不能整段 json.loads。"""
        noisy = (
            "WARNING: [youtube] something happened\n"
            '{"id": "aaaaaaaaaaa", "title": "Ok"}\n'
            "[download] 12.3% of 4.00MiB\n"
            "not json at all\n"
            '{"broken": \n'
            '{"id": "bbbbbbbbbbb", "title": "Also ok"}\n'
        )
        entries = MODULE.parse_flat_playlist(noisy)
        self.assertEqual([e["id"] for e in entries], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_entries_without_an_id_are_dropped(self):
        self.assertEqual(MODULE.parse_flat_playlist('{"title": "nameless"}'), [])


class SelectEntriesTests(unittest.TestCase):
    def setUp(self):
        self.entries = MODULE.parse_flat_playlist(PLAYLIST)

    def test_limit_truncates_keeping_order(self):
        picked = MODULE.select_entries(self.entries, 2, [])
        self.assertEqual([e["id"] for e in picked], ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    def test_none_limit_means_everything(self):
        self.assertEqual(len(MODULE.select_entries(self.entries, None, [])), 3)

    def test_ids_filter_and_preserve_the_given_order(self):
        picked = MODULE.select_entries(self.entries, None, ["ccccccccccc", "aaaaaaaaaaa"])
        self.assertEqual([e["id"] for e in picked], ["ccccccccccc", "aaaaaaaaaaa"])

    def test_ids_accept_a_full_url(self):
        picked = MODULE.select_entries(
            self.entries, None, ["https://www.youtube.com/watch?v=bbbbbbbbbbb"]
        )
        self.assertEqual(picked[0]["id"], "bbbbbbbbbbb")
        self.assertEqual(picked[0]["title"], "Second talk")

    def test_unknown_id_is_still_processed(self):
        """--ids 给的条目可能不在枚举结果里（例如枚举被 --limit 截断过）。"""
        picked = MODULE.select_entries(self.entries, None, ["zzzzzzzzzzz"])
        self.assertEqual(picked[0]["id"], "zzzzzzzzzzz")


class CommandBuildingTests(unittest.TestCase):
    def make_args(self, **overrides):
        parser = MODULE.build_parser()
        args = parser.parse_args(["URL", "--out", "/tmp/whatever"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_subtitle_mode_does_not_download_video(self):
        command = MODULE.build_command(["yt-dlp"], "URL", Path("/tmp/o"),
                                       self.make_args(), videos=False)
        self.assertIn("--skip-download", command)
        self.assertIn("--write-auto-subs", command)
        self.assertNotIn("-f", command)
        # 内置清单：重跑只补新的，不会把整个频道重新下一遍
        self.assertIn("--download-archive", command)
        self.assertIn("--no-overwrites", command)

    def test_video_mode_drops_subtitle_flags(self):
        """字幕和视频分开两次跑：字幕撞 429 不该连累视频，反之亦然。"""
        command = MODULE.build_command(["yt-dlp"], "URL", Path("/tmp/o"),
                                       self.make_args(), videos=True)
        self.assertNotIn("--skip-download", command)
        self.assertNotIn("--write-auto-subs", command)
        self.assertIn("-f", command)

    def test_wildcards_are_not_the_default(self):
        """`zh.*,en.*` 会把自动翻译轨一起拉下来，一个频道能落下几十个同义文件。"""
        self.assertNotIn("*", MODULE.DEFAULT_LANGS)
        for code in ("zh-Hans", "en-orig", "en"):
            self.assertIn(code, MODULE.DEFAULT_LANGS.split(","))

    def test_playlist_end_is_passed_through_when_limit_is_set(self):
        command = MODULE.build_command(["yt-dlp"], "URL", Path("/tmp/o"),
                                       self.make_args(limit=5), videos=False)
        self.assertIn("--playlist-end", command)
        self.assertEqual(command[command.index("--playlist-end") + 1], "5")

    def test_proxy_is_forwarded(self):
        command = MODULE.build_command(["yt-dlp"], "URL", Path("/tmp/o"),
                                       self.make_args(proxy="http://127.0.0.1:7892"),
                                       videos=False)
        self.assertEqual(command[command.index("--proxy") + 1], "http://127.0.0.1:7892")

    def test_targets_built_from_ids(self):
        args = self.make_args()
        targets = MODULE.targets_for(args, ["aaaaaaaaaaa"])
        self.assertEqual(targets, ["https://www.youtube.com/watch?v=aaaaaaaaaaa"])

    def test_targets_fall_back_to_the_url(self):
        args = self.make_args(url="https://www.youtube.com/@someone/videos")
        self.assertEqual(MODULE.targets_for(args, []), ["https://www.youtube.com/@someone/videos"])


class FileMatchingTests(unittest.TestCase):
    def test_subtitles_are_matched_by_the_id_marker(self):
        """文件名由模板生成（日期_标题_[id].en.srt），所以按 [id] 找而不是猜全名。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            wanted = folder / "2024-01-01_A talk_[aaaaaaaaaaa].en.srt"
            wanted.write_text("x", encoding="utf-8")
            (folder / "2024-01-01_Other_[bbbbbbbbbbb].en.srt").write_text("x", encoding="utf-8")
            self.assertEqual(MODULE.matching_subtitles(folder, "aaaaaaaaaaa"), [wanted])
            self.assertEqual(MODULE.matching_subtitles(folder, "ccccccccccc"), [])


class EndToEndTests(unittest.TestCase):
    """用假 yt-dlp 跑完整流程，验证落盘文件名和清单。"""

    def test_dry_run_downloads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "2", "--dry-run"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dry-run", result.stdout)
            self.assertIn("aaaaaaaaaaa", result.stdout)
            self.assertFalse((out / "sources.json").exists())

    def test_full_run_writes_subtitles_and_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "2"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            produced = sorted(p.name for p in out.glob("*.srt"))
            self.assertEqual(len(produced), 2, produced)
            # 模板里的日期与 [id] 标记都要落地：出图时靠 [id] 找兄弟视频
            self.assertTrue(all("[aaaa" in n or "[bbbb" in n for n in produced), produced)
            self.assertTrue(all(n.startswith("2024-01-01_") for n in produced), produced)

            manifest = json.loads((out / "sources.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["entries"]), 2)
            for entry in manifest["entries"]:
                self.assertTrue(entry["subtitles"], entry)
            self.assertEqual(manifest["without_subtitles"], [])
            self.assertFalse(manifest["videos_downloaded"])

    def test_entries_without_subtitles_are_reported_not_silently_dropped(self):
        """没有字幕轨的条目必须列出来——那些只能走本地 Whisper 转写。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder, skip_ids=("bbbbbbbbbbb", "ccccccccccc"))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "3"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "sources.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["without_subtitles"], ["bbbbbbbbbbb", "ccccccccccc"])
            self.assertIn("拿到字幕 1/3 条", result.stdout)
            self.assertIn("bbbbbbbbbbb", result.stdout)

    def test_missing_yt_dlp_gives_an_actionable_error(self):
        """找不到 yt-dlp 时要告诉用户怎么装，而不是抛栈。"""
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            pass
        else:
            # 装了 yt_dlp 包的话，`python -m yt_dlp` 这条回落分支必然可用，
            # 就把 PATH 清空也测不出「找不到」的情形。如实跳过，不伪造成通过。
            self.skipTest("本机装了 yt_dlp 包，回落分支必然可用，测不到找不到的情形")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["PATH"] = tmp  # 空目录，既没有 yt-dlp 也没有别的
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "URL", "--out", str(Path(tmp) / "o")],
                capture_output=True, text=True, env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            combined = result.stdout + result.stderr
            self.assertIn("yt-dlp", combined)
            self.assertNotIn("Traceback", combined)

    def test_no_url_and_no_ids_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--out", tmp],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--ids", result.stdout + result.stderr)

    def test_help_works_without_any_dependency(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--videos", result.stdout)


if __name__ == "__main__":
    unittest.main()
