"""`fetch_transcripts.py` 的测试。

脚本本身只做「拼 yt-dlp 命令 + 整理清单」这两件事，所以测试也分两层：
  - 纯函数：解析 JSONL、按 --limit/--ids 收窄、拼命令。不起子进程。
  - 端到端：**用一个假的 yt-dlp 可执行文件**跑完整流程，验证清单和落盘文件名。
    假脚本放在临时目录并前置到 PATH，所以不依赖网络、也不碰真实 YouTube。
"""

import contextlib
import importlib.util
import io
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

`--sub-langs` 里带 `-` 的标签（`zh-Hans-en`）各自落一个文件，用来验证
「原声轨与译文轨并存」。`FAKE_429_ONCE=1` 时第一次下载调用会打印 429 并
**以退出码 0 结束**——这正是 `--ignore-errors` 下的真实形态。
"""
import json
import os
import re
import sys
from pathlib import Path

counter = Path({counter!r})
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

# 每次「下载调用」都计数（--dump-json 那种枚举调用不算），
# 这样测试才能分辨「重试了」和「没重试」。
seen = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(seen + 1))

if os.environ.get("FAKE_429_ONCE") == "1" and seen == 0:
    print("ERROR: unable to download video subtitles: HTTP Error 429: Too Many Requests",
          file=sys.stderr)
    sys.exit(0)

# 哪些标签是「译文轨」由测试显式告知：译文轨是 `<目标语>-<源语>`，
# 不能靠「有没有 `-`」判断——`zh-Hans`、`en-orig` 自己就带横杠。
target = os.environ.get("FAKE_TRANSLATE", "")
langs = ["en"]
if target and "--sub-langs" in argv:
    requested = argv[argv.index("--sub-langs") + 1].split(",")
    langs += [item for item in requested if item.startswith(target + "-")]

skip = set(filter(None, os.environ.get("FAKE_SKIP_IDS", "").split(",")))

for video_id in video_ids:
    if video_id in skip:
        print("WARNING: no subtitles for", video_id)
        continue
    for lang in langs:
        name = (template
                .replace("%(upload_date>%Y-%m-%d)s", "2024-01-01")
                .replace("%(title).80s", "Fake Title")
                .replace("%(id)s", video_id)
                .replace("%(ext)s", lang + ".srt"))
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

    def __init__(self, folder: Path, skip_ids: tuple[str, ...] = (),
                 rate_limit_once: bool = False, translate: str = ""):
        self.skip_ids = skip_ids
        self.rate_limit_once = rate_limit_once
        self.translate = translate
        self.bindir = folder / "bin"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.playlist = folder / "playlist.jsonl"
        self.playlist.write_text(PLAYLIST, encoding="utf-8")
        self.counter = folder / "download-calls.txt"
        target = self.bindir / "yt-dlp"
        target.write_text(
            FAKE_YTDLP.format(python=sys.executable, playlist=str(self.playlist),
                              counter=str(self.counter)),
            encoding="utf-8",
        )
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def download_calls(self) -> int:
        return int(self.counter.read_text()) if self.counter.exists() else 0

    def env(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        if self.skip_ids:
            env["FAKE_SKIP_IDS"] = ",".join(self.skip_ids)
        if self.rate_limit_once:
            env["FAKE_429_ONCE"] = "1"
        if self.translate:
            env["FAKE_TRANSLATE"] = self.translate
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


def make_args(**overrides):
    """按真实 parser 造一份参数，再按需覆盖——避免测试里手写默认值。"""
    parser = MODULE.build_parser()
    args = parser.parse_args(["URL", "--out", "/tmp/whatever"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class CommandBuildingTests(unittest.TestCase):
    make_args = staticmethod(make_args)

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


class TranslationTrackTests(unittest.TestCase):
    """`--translate` 要展开成 YouTube 认得的译文轨标签。"""

    def test_each_target_is_expanded_over_every_source(self):
        """同一目标语在 YouTube 上按源语各有一份，所以每个源语都得请求一遍。"""
        self.assertEqual(
            MODULE.translation_tracks("zh-Hans", "en-orig,en"),
            ["zh-Hans", "zh-Hans-en-orig", "zh-Hans-en"],
        )

    def test_bare_target_is_requested_for_a_manual_translation(self):
        """裸标签是人工译本，存在就直接用；实测并非所有视频都有。"""
        self.assertEqual(MODULE.translation_tracks("zh-Hans", "en")[0], "zh-Hans")

    def test_no_flag_means_no_extra_tracks(self):
        self.assertEqual(MODULE.translation_tracks("", "en,zh-Hant"), [])
        self.assertEqual(MODULE.translation_tracks(None, "en"), [])

    def test_several_targets_and_no_duplicates(self):
        self.assertEqual(
            MODULE.translation_tracks("zh-Hans,zh-Hant", "en"),
            ["zh-Hans", "zh-Hans-en", "zh-Hant", "zh-Hant-en"],
        )

    def test_a_source_equal_to_the_target_is_skipped(self):
        """`zh-Hans-zh-Hans` 不是真实的轨道名，别去请求它。"""
        self.assertNotIn("zh-Hans-zh-Hans", MODULE.translation_tracks("zh-Hans", "zh-Hans,en"))

    def test_subtitle_args_keep_the_original_languages(self):
        """英文原轨必须原样保留——用户要把英文版发到 TikTok。"""
        args = MODULE.subtitle_args("en-orig,en", "srt/vtt", "zh-Hans")
        langs = args[args.index("--sub-langs") + 1].split(",")
        for code in ("en-orig", "en", "zh-Hans-en"):
            self.assertIn(code, langs)

    def test_subtitle_args_without_translate_are_unchanged(self):
        args = MODULE.subtitle_args("en-orig,en", "srt/vtt")
        self.assertEqual(args[args.index("--sub-langs") + 1], "en-orig,en")

    def test_a_dashed_language_in_the_passed_list_is_not_duplicated(self):
        args = MODULE.subtitle_args("en,zh-Hans-en", "srt/vtt", "zh-Hans")
        langs = args[args.index("--sub-langs") + 1].split(",")
        self.assertEqual(langs.count("zh-Hans-en"), 1)

    def test_video_mode_ignores_translate(self):
        """视频那一趟不碰字幕，别把译文轨塞进去。"""
        args = make_args(translate="zh-Hans")
        command = MODULE.build_command(["yt-dlp"], "URL", Path("/tmp/o"), args, videos=True)
        self.assertNotIn("--sub-langs", command)


class TranslationFileTests(unittest.TestCase):
    def test_picks_only_the_translation_tracks(self):
        paths = [
            Path("2024-01-01_X_[aaaaaaaaaaa].en.srt"),
            Path("2024-01-01_X_[aaaaaaaaaaa].en-orig.srt"),
            Path("2024-01-01_X_[aaaaaaaaaaa].zh-Hans-en.srt"),
            Path("2024-01-01_X_[aaaaaaaaaaa].zh-Hans-en-orig.srt"),
        ]
        picked = [p.name for p in MODULE.translation_files(paths, "zh-Hans")]
        self.assertEqual(picked,
                         ["2024-01-01_X_[aaaaaaaaaaa].zh-Hans-en.srt",
                          "2024-01-01_X_[aaaaaaaaaaa].zh-Hans-en-orig.srt"])

    def test_the_bare_manual_track_counts_as_a_translation(self):
        picked = MODULE.translation_files([Path("a.zh-Hans.srt")], "zh-Hans")
        self.assertEqual(len(picked), 1)

    def test_nothing_is_picked_without_a_target(self):
        paths = [Path("a.zh-Hans-en.srt")]
        self.assertEqual(MODULE.translation_files(paths, ""), [])

    def test_a_chinese_original_is_not_mistaken_for_a_translation(self):
        """`zh-Hant` 是原文轨时要留在原声那一侧，不能被算成 zh-Hans 的译文。"""
        self.assertEqual(MODULE.translation_files([Path("a.zh-Hant.srt")], "zh-Hans"), [])


class RetryDelayTests(unittest.TestCase):
    def test_parses_a_comma_separated_list(self):
        self.assertEqual(MODULE.parse_delays("0,45,120"), (0.0, 45.0, 120.0))

    def test_empty_means_a_single_attempt(self):
        self.assertEqual(MODULE.parse_delays(""), (0.0,))
        self.assertEqual(MODULE.parse_delays(None), (0.0,))
        self.assertEqual(MODULE.parse_delays("0"), (0.0,))

    def test_junk_is_rejected_with_a_clear_message(self):
        with self.assertRaises(SystemExit) as caught:
            MODULE.parse_delays("0,soon")
        self.assertIn("soon", str(caught.exception))

    def test_negative_values_are_clamped(self):
        self.assertEqual(MODULE.parse_delays("-5"), (0.0,))

    def test_default_is_a_retry_ladder(self):
        self.assertGreater(len(MODULE.parse_delays(MODULE.DEFAULT_RETRY_DELAYS)), 1)


class RateLimitTests(unittest.TestCase):
    """429 是瞬时限流，必须退避重试；其它错误不要浪费时间重试。"""

    # 退避值取「非零但极小」：既能触发那句「撞限流」提示，又不会真的等下去。
    NUDGE = (0.0, 0.01, 0.01)

    def flaky(self, counter: Path, *, fail_times: int, message: str) -> list[str]:
        """前 `fail_times` 次打印 `message` 并**以退出码 0 结束**，之后正常退出。

        退出码 0 是有意的：`--ignore-errors` 下 yt-dlp 撞 429 就是这个形态。
        """
        code = (
            "import pathlib, sys\n"
            f"p = pathlib.Path({str(counter)!r})\n"
            "n = int(p.read_text()) if p.exists() else 0\n"
            "p.write_text(str(n + 1))\n"
            f"if n < {fail_times}:\n"
            f"    print({message!r})\n"
            "    sys.exit(0)\n"
            "print('all good')\n"
        )
        return [sys.executable, "-c", code]

    def capture(self, command, delays):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = MODULE.run(command, delays)
        return code, buffer.getvalue()

    def test_429_is_retried_and_then_succeeds(self):
        """实测：同一进程里主字幕正常、译文轨直接 429，退避后重试第一次就成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=1,
                                 message="ERROR: HTTP Error 429: Too Many Requests")
            code, output = self.capture(command, self.NUDGE)
            self.assertEqual(code, 0)
            self.assertEqual(counter.read_text(), "2")
            self.assertIn("撞限流", output)
            self.assertIn("all good", output)

    def test_a_429_is_retried_even_when_the_exit_code_is_zero(self):
        """`--ignore-errors` 下 429 是「退出码 0 + 一行 ERROR」，只能认文本。"""
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=1,
                                 message="ERROR: unable to download video subtitles: "
                                         "HTTP Error 429: Too Many Requests")
            code, _ = self.capture(command, self.NUDGE)
            self.assertEqual(code, 0)
            self.assertEqual(counter.read_text(), "2", "退出码 0 的 429 被当成成功了")

    def test_a_hard_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=99,
                                 message="ERROR: Video unavailable")
            _, output = self.capture(command, self.NUDGE)
            self.assertEqual(counter.read_text(), "1")
            self.assertNotIn("撞限流", output)

    def test_attempts_are_capped_by_the_delay_ladder(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=99,
                                 message="ERROR: HTTP Error 429: Too Many Requests")
            self.capture(command, self.NUDGE)
            self.assertEqual(counter.read_text(), "3")

    def test_a_single_delay_means_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=99,
                                 message="ERROR: HTTP Error 429: Too Many Requests")
            self.capture(command, (0.0,))
            self.assertEqual(counter.read_text(), "1")

    def test_output_is_relayed_so_nothing_is_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n.txt"
            command = self.flaky(counter, fail_times=0, message="unused")
            _, output = self.capture(command, (0.0,))
            self.assertIn("all good", output)


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

    def test_translate_keeps_both_languages_and_records_them(self):
        """英文原轨与中文译轨并存——英文版还要发 TikTok，不能被替换掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder, translate="zh-Hans")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "2", "--lang", "en-orig,en",
                 "--translate", "zh-Hans"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            names = sorted(p.name for p in out.glob("*.srt"))
            self.assertEqual(len(names), 6, names)  # 每视频 3 条：en + 两条 zh-Hans-*
            self.assertTrue(any(n.endswith(".en.srt") for n in names), names)
            self.assertTrue(any(n.endswith(".zh-Hans-en.srt") for n in names), names)

            manifest = json.loads((out / "sources.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["translate"], "zh-Hans")
            self.assertEqual(manifest["without_translations"], [])
            for entry in manifest["entries"]:
                self.assertEqual(len(entry["subtitles"]), 3, entry)
                self.assertEqual(len(entry["translations"]), 2, entry)

    def test_an_entry_without_a_translation_track_is_reported(self):
        """有原声、没译文：还能出英文图，但要让人知道没中文版。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder, translate="zh-Hant")  # 实际只请求 zh-Hans
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "1", "--lang", "en-orig,en",
                 "--translate", "zh-Hans"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / "sources.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["without_translations"], ["aaaaaaaaaaa"])
            self.assertIn("没拿到译文轨", result.stdout)

    def test_translate_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder)
            subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "1"],
                capture_output=True, text=True, env=fake.env(),
            )
            manifest = json.loads((out / "sources.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["translate"], "")
            self.assertNotIn("without_translations", manifest)

    def test_the_next_step_hint_carries_the_translate_flag(self):
        """选句时漏了 --translate，第二套中文就不会产出——提示里必须带上。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = FakeYtDlp(folder)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(folder / "source"), "--limit", "1", "--translate", "zh-Hans"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertIn("--translate zh-Hans", result.stdout)

    def test_a_rate_limited_run_is_retried_by_the_cli(self):
        """端到端验证退避重试真的接在了主流程上（这里把等待压成 0）。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / "source"
            fake = FakeYtDlp(folder, rate_limit_once=True, translate="zh-Hans")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(out), "--limit", "2", "--lang", "en-orig,en",
                 "--translate", "zh-Hans", "--retry-delays", "0,0.01"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fake.download_calls(), 2, "第一次 429 之后没有重试")
            self.assertIn("撞限流", result.stdout)
            self.assertEqual(len(list(out.glob("*.srt"))), 6)

    def test_a_non_rate_limit_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fake = FakeYtDlp(folder, skip_ids=("aaaaaaaaaaa",))
            subprocess.run(
                [sys.executable, str(SCRIPT), "https://www.youtube.com/playlist?list=X",
                 "--out", str(folder / "source"), "--limit", "1", "--retry-delays", "0,0,0"],
                capture_output=True, text=True, env=fake.env(),
            )
            self.assertEqual(fake.download_calls(), 1, "没有字幕不是限流，不该重试")

    def test_help_works_without_any_dependency(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--videos", result.stdout)
        self.assertIn("--translate", result.stdout)
        self.assertIn("--retry-delays", result.stdout)


if __name__ == "__main__":
    unittest.main()
