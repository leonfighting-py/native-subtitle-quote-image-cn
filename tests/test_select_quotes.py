import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "native-subtitle-quote-image"
    / "scripts"
    / "select_quotes.py"
)
SPEC = importlib.util.spec_from_file_location("select_quotes", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_segments(lines, start=0.0, step=3.0, span=2.6):
    segments = []
    for index, text in enumerate(lines):
        begin = round(start + index * step, 2)
        segments.append({"start": begin, "end": round(begin + span, 2), "text": text})
    return segments


class SanitizeTests(unittest.TestCase):
    def test_tail_hallucination_with_start_inside_duration_is_dropped(self):
        """实测 11/60 个转写文件的末段会超出总时长（Whisper 在尾部静音上幻觉）。

        注意它的 start 往往刚好卡在总时长以内（217.70 < 217.73），
        所以拦住它的是套话规则或跨度规则，而不是「start 越界」那一条。
        多条规则互为兜底，正是为了这种错位。
        """
        segments = make_segments(["甲", "乙", "丙"])
        segments.append({"start": 217.70, "end": 247.68, "text": "感谢观看"})
        clean, dropped = MODULE.sanitize(segments, duration=217.73)
        self.assertEqual([item["text"] for item in clean], ["甲", "乙", "丙"])
        self.assertEqual(len(dropped), 1)
        self.assertIn(dropped[0]["reason"], {"超出视频总时长", "片尾套话"})

    def test_segment_starting_beyond_duration_is_dropped(self):
        segments = make_segments(["甲", "乙", "丙"])
        segments.append({"start": 250.0, "end": 252.0, "text": "正文之外的残留"})
        clean, dropped = MODULE.sanitize(segments, duration=217.73)
        self.assertEqual(len(clean), 3)
        self.assertEqual(dropped[0]["reason"], "超出视频总时长")

    def test_end_beyond_duration_is_clamped_when_start_is_inside(self):
        segments = [{"start": 0.0, "end": 2.0, "text": "甲"},
                    {"start": 2.0, "end": 12.0, "text": "乙"}]
        clean, _ = MODULE.sanitize(segments, duration=10.0)
        self.assertEqual(len(clean), 2)
        self.assertEqual(clean[1]["end"], 10.0)

    def test_boilerplate_is_dropped(self):
        segments = make_segments(["正文一", "感谢观看", "点赞关注", "正文二"])
        clean, dropped = MODULE.sanitize(segments, duration=20.0)
        self.assertEqual([item["text"] for item in clean], ["正文一", "正文二"])
        self.assertTrue(all(item["reason"] == "片尾套话" for item in dropped))

    def test_abnormally_long_span_is_dropped(self):
        segments = [{"start": 0.0, "end": 2.0, "text": "甲"},
                    {"start": 5.0, "end": 40.0, "text": "疑似静音循环"}]
        clean, dropped = MODULE.sanitize(segments, duration=60.0)
        self.assertEqual(len(clean), 1)
        self.assertIn("疑似静音幻觉", dropped[0]["reason"])


class LineTests(unittest.TestCase):
    def test_short_fragments_merge_into_neighbour(self):
        segments = make_segments(["很短", "一个正常长度的句子在这里"])
        lines = MODULE.build_lines(segments, min_chars=6)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "很短一个正常长度的句子在这里")

    def test_normal_length_lines_stay_untouched(self):
        """本语料原始段中位 14 字，本身就是合格字幕行，不该被合并。"""
        segments = make_segments(["这是第一句正常长度的话", "这是第二句正常长度的话",
                                  "这是第三句正常长度的话"])
        lines = MODULE.build_lines(segments, min_chars=6)
        self.assertEqual(len(lines), 3)


class WindowTests(unittest.TestCase):
    def test_window_count_matches_sliding_sizes(self):
        lines = [{"start": i * 3.0, "end": i * 3.0 + 2.6, "text": f"第{index}句内容"} for index, i in enumerate(range(10))]
        windows = MODULE.build_windows(lines, low=4, high=6)
        # 4 行: 7 个, 5 行: 6 个, 6 行: 5 个
        self.assertEqual(len(windows), 7 + 6 + 5)

    def test_times_match_line_count_and_increase_strictly(self):
        """绝不能为了凑固定数量而抽稀中间行——会抽断论证链（实测踩过）。"""
        lines = [{"start": i * 3.0, "end": i * 3.0 + 2.6, "text": f"第{i}句"} for i in range(6)]
        times = MODULE.choose_times(lines)
        self.assertEqual(len(times), 6)
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))

    def test_all_lines_are_kept_even_when_more_than_five(self):
        lines = [{"start": i * 3.0, "end": i * 3.0 + 2.6, "text": f"第{i}句"} for i in range(6)]
        times = MODULE.choose_times(lines)
        self.assertEqual(times, [1.3, 4.3, 7.3, 10.3, 13.3, 16.3])

    def test_deeply_overlapping_windows_are_not_both_selected(self):
        """实测踩过：两个窗口时间上只压边、却共享大部分行，会被同时选中。"""
        lines = [{"start": i * 3.0, "end": i * 3.0 + 2.6, "text": f"第{i}句内容"} for i in range(10)]
        windows = MODULE.build_windows(lines, low=5, high=5)
        for window in windows:
            window["score"] = {"total": 0.5}
        chosen = MODULE.pick_windows(windows, top=5)
        for left, right in zip(chosen, chosen[1:]):
            shared = len({id(line) for line in left["lines"]} & {id(line) for line in right["lines"]})
            self.assertLess(shared / min(len(left["lines"]), len(right["lines"])), 0.34)


class SignalTests(unittest.TestCase):
    def test_ordinals_are_not_counted_as_quantity(self):
        """「第一」「第二个」在中文里无处不在，算成数字会把一半候选误标、毁掉排序。"""
        ordinals = "第一个原因是我们先看第一步第二步然后第三个就是重点"
        hits = [
            label
            for pattern, _, label in MODULE.VALUE_SIGNALS
            if pattern.search(ordinals)
        ]
        self.assertNotIn("数量", hits)

    def test_real_quantities_are_counted(self):
        for text in ["上线一个月被罚了500万", "能提升百分之三十", "涨了三倍", "2023年退出中国市场"]:
            hits = [label for pattern, _, label in MODULE.VALUE_SIGNALS if pattern.search(text)]
            self.assertIn("数量", hits, text)

    def test_placeholder_damping_lowers_score(self):
        plain = {"size": 5, "start": 0.0, "end": 14.0,
                 "lines": [{"text": t} for t in ["政策影响了市场结构", "企业需要重新评估", "这个判断有依据", "数据支持这个结论", "所以结论成立"]],
                 "text": "政策影响了市场结构企业需要重新评估这个判断有依据数据支持这个结论所以结论成立"}
        weak = {"size": 5, "start": 0.0, "end": 14.0,
                "lines": [{"text": t} for t in ["今天我们来聊聊", "希望大家耐心听完", "先把背景交代清楚", "这个话题比较复杂", "我们慢慢说"]],
                "text": "今天我们来聊聊希望大家耐心听完先把背景交代清楚这个话题比较复杂我们慢慢说"}
        self.assertLess(MODULE.score_window(weak)["total"], MODULE.score_window(plain)["total"])


class CliTests(unittest.TestCase):
    def test_pick_writes_renderable_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lines = [
                "今天我们来讨论一个话题", "希望大家能够耐心听完", "先交代一下背景情况",
                "政策影响了整个市场结构", "企业必须重新评估自己的定位",
                "因为成本上升会挤压利润空间", "所以结论是需要提前布局",
                "而不是等到变化发生之后", "这是最关键的一个判断",
            ]
            transcript = {
                "text": "".join(lines),
                "language": "zh",
                "duration": 40.0,
                "backend": "test",
                "model": "test",
                "segments": make_segments(lines),
            }
            source = tmp_path / "sample.mp4.transcript.json"
            source.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")

            out = tmp_path / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(source), "--out", str(out), "--top", "2"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ["candidates.json", "native_manifest.json", "script_lines.json", "审阅表.md"]:
                self.assertTrue((out / name).is_file(), name)

            # 原生 manifest 必须能被渲染器直接接受：times 严格递增且落在时长内
            manifest = json.loads((out / "native_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["images"])
            for image in manifest["images"]:
                times = image["times"]
                self.assertGreaterEqual(len(times), 2)
                self.assertLessEqual(len(times), MODULE.MAX_TIMES_PER_IMAGE)
                self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
                self.assertLess(times[-1], transcript["duration"])

            # 脚本模式：一张图一个文件，且键是 lines——
            # render-script 一次只画一张，塞成数组会直接报「必须包含至少 2 项的 lines 数组」
            scripts = sorted((out / "scripts").glob("*.json"))
            self.assertEqual(len(scripts), len(manifest["images"]))
            for path in scripts:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("lines", payload)
                self.assertIsInstance(payload["lines"], list)
                self.assertGreaterEqual(len(payload["lines"]), 2)
                for line in payload["lines"]:
                    self.assertIsInstance(line["t"], (int, float))
                    self.assertIsInstance(line["text"], str)

    def test_batch_aggregates_global_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "corpus"
            for index, category in enumerate(["甲类", "乙类"]):
                folder = root / category
                folder.mkdir(parents=True)
                lines = [
                    "今天我们来讨论一个话题", "希望大家能够耐心听完", "先交代一下背景情况",
                    "政策影响了整个市场结构", "企业必须重新评估自己的定位",
                    "因为成本上升会挤压利润空间", "所以结论是需要提前布局",
                    "而不是等到变化发生之后", "这是最关键的一个判断",
                ]
                transcript = {
                    "text": "".join(lines), "duration": 40.0,
                    "backend": "test", "model": "test",
                    "segments": make_segments(lines),
                }
                (folder / f"v{index}.mp4.transcript.json").write_text(
                    json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
                )
            out = tmp_path / "batch"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "batch", str(root), "--out", str(out), "--top", "2"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "全局精选.md").is_file())
            ranking = json.loads((out / "全局精选.json").read_text(encoding="utf-8"))
            self.assertEqual(ranking["stats"]["videos"], 2)
            self.assertGreater(ranking["stats"]["selected"], 0)


class SiblingVideoTests(unittest.TestCase):
    def test_finds_video_next_to_transcript(self):
        """流水线把 X.mp4 和 X.mp4.transcript.json 放在一起，批处理要认得出兄弟文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "X.mp4"
            video.write_bytes(b"stub")
            transcript = folder / "X.mp4.transcript.json"
            transcript.write_text("{}", encoding="utf-8")
            self.assertEqual(MODULE.sibling_video(transcript), video)

    def test_returns_none_when_video_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "lonely.mp4.transcript.json"
            transcript.write_text("{}", encoding="utf-8")
            self.assertIsNone(MODULE.sibling_video(transcript))

    def test_finds_video_when_yt_dlp_inserts_language_code(self):
        """yt-dlp 把语言码夹在中间：talk.mp4 配 talk.en.srt。

        只剥已知后缀是不够的——剥完还剩 "talk.en"，加扩展名找不到 talk.mp4。
        """
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "talk.mp4"
            video.write_bytes(b"stub")
            subtitle = folder / "talk.en.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            self.assertEqual(MODULE.sibling_video(subtitle), video)

    def test_does_not_confuse_a_similarly_named_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "talk-2.mp4").write_bytes(b"stub")
            subtitle = folder / "talk.en.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            self.assertIsNone(MODULE.sibling_video(subtitle))


class TranscriptDiscoveryTests(unittest.TestCase):
    """批处理要先找得到文字稿。仓库里同时有 json / srt / txt，还混着 yt-dlp 的元数据。"""

    def _touch(self, folder, name, body="{}"):
        path = folder / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_prefers_transcript_json_over_other_formats(self):
        """三种格式并存时只用最完整的那份，否则同一期视频会被处理三遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            wanted = self._touch(folder, "a.mp4.transcript.json")
            self._touch(folder, "a.mp4.transcript.srt")
            self._touch(folder, "a.mp4.transcript.txt")
            found = MODULE.discover_transcripts(folder)
            self.assertEqual(found, [wanted])

    def test_falls_back_to_subtitle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            srt = self._touch(folder, "b.srt")
            self._touch(folder, "b.txt")
            self.assertEqual(MODULE.discover_transcripts(folder), [srt])

    def test_yt_dlp_info_json_is_not_treated_as_a_transcript(self):
        """`--write-info-json` 产出的 .info.json 是元数据，没有 segments，喂进去只会报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            subtitle = self._touch(folder, "talk.info.srt")
            self._touch(folder, "talk.info.json", "{\"id\": \"talk\"}")
            found = MODULE.discover_transcripts(folder)
            self.assertIn(subtitle, found)
            self.assertNotIn(folder / "talk.info.json", found)


class SubtitleParsingTests(unittest.TestCase):
    """SRT / VTT 是 YouTube 与 Whisper 两条链路交回来的格式，也是最容易出错的一环。"""

    # 结构逐字取自 yt-dlp 对同一个 TED 演讲的原生自动字幕 VTT（8S0FDjFBj8o）
    ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:19.560 --> 00:00:21.950 align:start position:0%
 
Hear<00:00:19.720><c> that?</c>

00:00:21.950 --> 00:00:21.960 align:start position:0%
Hear that?
 

00:00:21.960 --> 00:00:24.830 align:start position:0%
Hear that?
That's<00:00:22.960><c> nothing.</c>
"""

    def test_inline_word_timing_tags_never_reach_the_text(self):
        """VTT 正文长这样：`Hear<00:00:19.720><c> that?</c>`。

        实测同一份字幕 315 个 cue 里 113 个带标签。不剥掉，`<c>` 会被当台词画进图里。
        """
        segments, _, _ = MODULE.parse_cue_subtitles(self.ROLLING_VTT, source="vtt")
        for segment in segments:
            self.assertNotIn("<", segment["text"], segment["text"])
            self.assertNotIn(">", segment["text"], segment["text"])
        self.assertEqual(segments[0]["text"], "Hear that?")

    def test_first_cue_survives_a_whitespace_only_line(self):
        """滚动字幕的 cue 正文里夹着只含空白的行。

        按空行切块会把它当成块边界，于是「时间戳」成为孤块、正文成为无时间戳的块被丢掉，
        结果是 YouTube 自动字幕的第一句话永远丢失。
        """
        segments, _, _ = MODULE.parse_cue_subtitles(self.ROLLING_VTT, source="vtt")
        self.assertEqual(segments[0]["start"], 19.56)
        self.assertEqual(segments[0]["text"], "Hear that?")

    def test_rolling_cue_yields_only_the_new_line(self):
        """滚动缓冲的第二条 cue 把上一条又抄了一遍，只该取新增的那行。"""
        segments, _, meta = MODULE.parse_cue_subtitles(self.ROLLING_VTT, source="vtt")
        self.assertEqual(segments[2]["text"], "That's nothing.")
        self.assertEqual(meta["rolling_cues_merged"], 1)

    def test_whisper_style_srt_is_left_alone(self):
        """Whisper SRT 每条是完整的独立段，不是滚动结构，一个字都不能动。

        早先的滚轮判定用「上一行是前缀」+「都出现过」两条，太松：
        实测 60 份中文 Whisper SRT 里有 31 处被误判成滚动而被吞掉（如「对」「嗯」连着重复）。
        """
        srt = (
            "1\n00:00:00,000 --> 00:00:02,520\n怎么让自己一天专注至少8个小时\n\n"
            "2\n00:00:02,520 --> 00:00:04,300\n不废话直接开始\n\n"
            "3\n00:00:04,300 --> 00:00:06,100\n第一遍看不建议拉进度条\n"
        )
        segments, _, meta = MODULE.parse_cue_subtitles(srt, source="srt")
        self.assertEqual([s["text"] for s in segments],
                         ["怎么让自己一天专注至少8个小时", "不废话直接开始", "第一遍看不建议拉进度条"])
        self.assertEqual(meta["rolling_cues_merged"], 0)

    def test_srt_index_numbers_are_not_part_of_the_text(self):
        srt = "1\n00:00:00,000 --> 00:00:02,000\n正文内容\n"
        segments, _, _ = MODULE.parse_cue_subtitles(srt, source="srt")
        self.assertEqual(segments[0]["text"], "正文内容")

    def test_vtt_cue_identifiers_and_header_are_skipped(self):
        vtt = (
            "WEBVTT\nKind: captions\nLanguage: en\n\n"
            "cue-00001\n00:00:01.000 --> 00:00:03.000\nHello there\n\n"
            "NOTE 这一行是注释\n\n"
            "cue-00002\n00:00:03.000 --> 00:00:05.000\nSecond line\n"
        )
        segments, _, _ = MODULE.parse_cue_subtitles(vtt, source="vtt")
        self.assertEqual([s["text"] for s in segments], ["Hello there", "Second line"])

    def test_overlapping_cues_are_clipped_to_keep_the_timeline_monotonic(self):
        """YouTube 自动字幕实测 91% 的相邻 cue 互相重叠，不裁取帧点会落在两句之间。"""
        srt = (
            "1\n00:00:00,000 --> 00:00:05,000\n第一句\n\n"
            "2\n00:00:03,000 --> 00:00:08,000\n第二句\n\n"
            "3\n00:00:07,000 --> 00:00:10,000\n第三句\n"
        )
        segments, _, meta = MODULE.parse_cue_subtitles(srt, source="srt")
        self.assertEqual(meta["clipped_overlaps"], 2)
        self.assertEqual([(s["start"], s["end"]) for s in segments],
                         [(0.0, 3.0), (3.0, 7.0), (7.0, 10.0)])
        for left, right in zip(segments, segments[1:]):
            self.assertLessEqual(left["end"], right["start"])

    def test_degenerate_cues_are_dropped_after_clipping(self):
        """裁剪后 end 不再大于 start 的 cue 没有可用时长，必须丢掉而不是留个空档。"""
        srt = (
            "1\n00:00:00,000 --> 00:00:05,000\n有内容的一句\n\n"
            "2\n00:00:00,000 --> 00:00:00,500\n夹在里面的碎块\n"
        )
        segments, _, _ = MODULE.parse_cue_subtitles(srt, source="srt")
        self.assertTrue(all(s["end"] > s["start"] for s in segments))

    def test_load_transcript_accepts_vtt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.en.vtt"
            path.write_text(self.ROLLING_VTT, encoding="utf-8")
            segments, duration, meta = MODULE.load_transcript(path)
            self.assertTrue(segments)
            self.assertEqual(meta["source"], "vtt")
            self.assertAlmostEqual(duration, 24.83, places=2)

    def test_load_transcript_rejects_timestamp_free_text(self):
        """没有时间戳的纯文本定位不到取帧点，必须明确报错并指路，不能猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.txt"
            path.write_text("这是一段没有时间戳的转写文本", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                MODULE.load_transcript(path)
            self.assertIn("时间戳", str(caught.exception))


class DedupeTests(unittest.TestCase):
    """挑出的每一组都要出成一张图，所以两组之间不能共用同一行。"""

    def _windows(self, count=10, size=5, score=0.5):
        lines = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": f"第{index}句内容"}
            for index in range(count)
        ]
        windows = MODULE.build_windows(lines, low=size, high=size)
        for window in windows:
            window["score"] = {"total": score}
        return windows

    def test_new_window_may_not_start_inside_an_already_chosen_one(self):
        """窗口是逐行滑出来的，嵌套重叠是最常见的重复形态。

        这里是旧规则漏掉的样例：窗口 0 被选中后，窗口 4 共享 1/5 行（0.2 < 0.34 阈值），
        索引又差 4（旧阈值为 3），于是两道检查都放过——但它整段压在窗口 0 里面。
        """
        windows = self._windows()
        chosen = MODULE.pick_windows(windows, top=5)
        for picked in chosen:
            for other in chosen:
                if other is picked:
                    continue
                inside = picked["index"] <= other["index"] < picked["index"] + picked["size"]
                self.assertFalse(
                    inside,
                    f"窗口 {other['index']} 落在已选窗口 {picked['index']}"
                    f"（{picked['index']}–{picked['index'] + picked['size'] - 1}）内部",
                )

    def test_no_line_is_shared_by_two_selected_windows(self):
        """12 行、窗口 5 行时，互不共享的窗口最多只能放下 2 个（0–4 与 5–9）。

        这里同时是「去重不能严到选不出东西」的下界守卫：两个都必须选出来。
        """
        windows = self._windows(count=12)
        chosen = MODULE.pick_windows(windows, top=5)
        self.assertEqual(len(chosen), 2, "去重过严或过松")
        for index, left in enumerate(chosen):
            for right in chosen[index + 1:]:
                shared = set(id(line) for line in left["lines"]) & set(
                    id(line) for line in right["lines"]
                )
                self.assertFalse(shared, f"共享了 {len(shared)} 行")

    def test_short_video_still_yields_at_least_one_window(self):
        """只有 4 行的小样本也必须能选出东西，不能因为去重规则空手而归。"""
        windows = self._windows(count=4, size=4)
        self.assertEqual(len(MODULE.pick_windows(windows, top=5)), 1)

    def test_dedupe_never_drops_the_best_window(self):
        """去重只在重复之间取舍，绝不能把分数最高的那组挤掉。"""
        windows = self._windows(count=12)
        winner = windows[0]
        winner["score"] = {"total": 0.99}
        chosen = MODULE.pick_windows(windows, top=5)
        self.assertIn(winner, chosen)


class JoinTests(unittest.TestCase):
    """字幕是一句一句切开的，拼回整段时要按书写系统还原空格。"""

    def test_cjk_parts_are_glued_without_space(self):
        """中文转写没有标点也没有停顿，段与段直接相连，多一个空格就是错的。"""
        self.assertEqual(
            MODULE.join_parts(["这种逆差在正常情况下", "会导致货币贬值和经济困难"]),
            "这种逆差在正常情况下会导致货币贬值和经济困难",
        )

    def test_latin_parts_are_joined_with_a_space(self):
        """英语少一个空格就粘连成 `yourconversations`，而且会一路带进交付物。"""
        self.assertEqual(
            MODULE.join_parts(["looks like 10% of your", "conversations yeah I thought"]),
            "looks like 10% of your conversations yeah I thought",
        )

    def test_mixed_script_switches_at_the_boundary(self):
        """中文里夹英文术语要按相邻字符判断，不能整段一刀切。"""
        self.assertEqual(MODULE.join_parts(["这是 ChatGPT", "的最新版本"]), "这是 ChatGPT的最新版本")
        self.assertEqual(MODULE.join_parts(["we use GPT", "models here"]), "we use GPT models here")

    def test_empty_parts_are_skipped(self):
        self.assertEqual(MODULE.join_parts(["甲", "", "   ", "乙"]), "甲乙")
        self.assertEqual(MODULE.join_parts([]), "")

    def test_window_text_keeps_word_boundaries(self):
        """窗口 text 会被当成候选标题和全局精选的原文，必须可直接阅读。"""
        lines = [
            {"start": 0.0, "end": 2.0, "text": "looks like 10% of your"},
            {"start": 2.0, "end": 4.0, "text": "conversations yeah I thought"},
            {"start": 4.0, "end": 6.0, "text": "Twitter like love is blind"},
            {"start": 6.0, "end": 8.0, "text": "drinking game like take a shot"},
        ]
        window = MODULE.build_windows(lines, low=4, high=4)[0]
        self.assertIn("your conversations", window["text"])
        self.assertNotIn("yourconversations", window["text"])


class LanguageTrackTests(unittest.TestCase):
    """一次 `--sub-langs "zh.*,en.*"` 会给同一支视频落下好几条轨。"""

    def test_original_track_outranks_the_rest(self):
        ranks = {
            name: MODULE.language_rank(name)
            for name in ["T_[id].en-orig", "T_[id].en", "T_[id].zh-Hans",
                         "T_[id].en-en", "T_[id].zh-Hans-en", "X.mp4"]
        }
        self.assertLess(ranks["T_[id].en-orig"], ranks["T_[id].en"])
        self.assertLess(ranks["T_[id].en"], ranks["T_[id].en-en"])
        self.assertLess(ranks["T_[id].zh-Hans"], ranks["T_[id].zh-Hans-en"])
        # 没有语言码的（Whisper 转写）不受影响
        self.assertEqual(ranks["X.mp4"][0], 0)

    def test_one_subtitle_per_video_is_kept(self):
        """实测一个英文视频会落下 en / en-orig / en-en 三份，其中 en-en 与 en 逐字节相同。

        不归并的话同一支视频会被处理三遍，全局精选里重复三次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ["T_[a].en.srt", "T_[a].en-orig.srt", "T_[a].en-en.srt", "T_[b].en.srt"]
            for name in names:
                (folder / name).write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            found = MODULE.discover_transcripts(folder)
            self.assertEqual(len(found), 2, [p.name for p in found])
            self.assertEqual({p.name for p in found},
                             {"T_[a].en-orig.srt", "T_[b].en.srt"})

    def test_different_videos_are_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ["first_[a].en.srt", "second_[b].en.srt"]:
                (folder / name).write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            self.assertEqual(len(MODULE.discover_transcripts(folder)), 2)

    def test_whisper_transcript_json_is_untouched(self):
        """抖音流水线的命名没有语言码，归并规则不能误伤它。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ["X.mp4.transcript.json", "Y.mp4.transcript.json"]:
                (folder / name).write_text("{}", encoding="utf-8")
            found = MODULE.discover_transcripts(folder)
            self.assertEqual({p.name for p in found},
                             {"X.mp4.transcript.json", "Y.mp4.transcript.json"})


class VideoStemTests(unittest.TestCase):
    def test_language_code_is_stripped(self):
        self.assertEqual(MODULE.video_stem_candidates("talk.en"), ["talk.en", "talk"])

    def test_script_variant_and_orig_codes_are_stripped(self):
        self.assertEqual(MODULE.video_stem_candidates("talk.zh-Hans"), ["talk.zh-Hans", "talk"])
        self.assertEqual(MODULE.video_stem_candidates("talk.en-orig"), ["talk.en-orig", "talk"])

    def test_dots_inside_the_title_do_not_break_lookup(self):
        """取标题当文件名之后，标题里的点会把 `split(".")[0]` 截断。"""
        candidates = MODULE.video_stem_candidates("2024-01-01_A.I. and the future_xyz.en")
        self.assertIn("2024-01-01_A.I. and the future_xyz", candidates)

    def test_video_is_found_when_the_title_contains_dots(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "2024-01-01_A.I. and the future_xyz.mp4"
            video.write_bytes(b"stub")
            subtitle = folder / "2024-01-01_A.I. and the future_xyz.en.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            self.assertEqual(MODULE.sibling_video(subtitle), video)


class MakeTitleTests(unittest.TestCase):
    """标题不画进图片，但它同时是 `scripts/` 与 `scripts_zh/` 的文件名和审阅表的小标题。

    英文若连空格一起剥掉会粘成 `brokenaboneSolookslike`，在审阅表里完全没法看。
    """

    def test_english_keeps_word_separators(self):
        title = MODULE.make_title(
            {"text": "broken a bone. So, looks like about 40% of the room, "}, limit=30
        )
        self.assertIn("broken a bone", title)
        self.assertNotIn("brokenabone", title)

    def test_chinese_stays_unspaced_and_punctuation_is_dropped(self):
        self.assertEqual(
            MODULE.make_title({"text": "你好，世界。这是测试"}, limit=30), "你好世界这是测试"
        )

    def test_never_invents_a_space(self):
        """实测 50557 条真实候选：新标题里出现空格的前提是原文里本来就有空白。

        断言取最严格的形式——**原文一个空白都没有时，标题里绝不能有空格**。
        """
        for text in ["工具的调用记忆路由评测权限", "AI来了", "第3季度营收", "你好，世界。这是测试"]:
            self.assertNotIn(" ", MODULE.make_title({"text": text}, limit=30), text)

    def test_existing_whitespace_is_preserved(self):
        """转写里本来就带空格的地方要留住：实测 `记忆 路由` 曾被粘成 `记忆路由`。"""
        self.assertEqual(
            MODULE.make_title({"text": "工具的调用记忆 路由 评测"}, limit=30),
            "工具的调用记忆 路由 评测",
        )

    def test_blank_title_falls_back(self):
        self.assertEqual(MODULE.make_title({"text": "！！"}, limit=30), "未命名")


def make_srt(lines, step=3.0, span=2.6):
    """把若干行文本拼成一份最小可用的 SRT（时间轴与 make_segments 对齐）。"""

    def stamp(seconds):
        hours = int(seconds // 3600)
        minutes = int(seconds % 3600 // 60)
        secs = int(seconds % 60)
        millis = round((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    blocks = []
    for index, text in enumerate(lines, 1):
        begin = (index - 1) * step
        blocks.append(f"{index}\n{stamp(begin)} --> {stamp(begin + span)}\n{text}\n")
    return "\n".join(blocks)


EN_LINES = [
    "Today we are going to talk about shipping",
    "Please listen carefully until the end",
    "Let me first set up the background",
    "Shipping is not a side issue in the climate crisis",
    "It is one of the biggest hidden levers",
    "Steel production is responsible for eight percent",
    "So the conclusion is to move early",
    "Rather than waiting for the change to happen",
    "This is the most important judgement",
]

ZH_LINES = [
    "今天我们要聊聊航运这件事",
    "请耐心听到最后",
    "我先交代一下背景",
    "航运并非气候危机中的次要问题",
    "它是最大的隐藏杠杆之一",
    "钢铁生产占了百分之八",
    "所以结论是要提前布局",
    "而不是等到变化发生之后",
    "这是最关键的一个判断",
]


class RowWidthTests(unittest.TestCase):
    """行宽用「宽度单位」记账：汉字 2 单位、拉丁 1 单位。

    实测渲染器单行容量（width 1080 与 1440 结果相同，因为字号 `out_width/18`
    和可用宽度 `out_width*0.92` 都随宽度等比缩放）：
      基础字号  约 16 个汉字 / 约 32 个拉丁字符
      可读下限  29 个汉字    / 60 个拉丁字符
    两组数一致指向「1 汉字 ≈ 2 拉丁字符」，所以按单位算才对。
    """

    def test_cjk_counts_double(self):
        self.assertEqual(MODULE.display_units("甲乙丙"), 6)
        self.assertEqual(MODULE.display_units("abc"), 3)
        self.assertEqual(MODULE.display_units("a甲b"), 4)
        self.assertEqual(MODULE.display_units(""), 0)

    def test_cjk_punctuation_counts_as_wide(self):
        """`。`(U+3002) 和全角逗号 (U+FF0C) 都是宽字符。

        复用脚本里已有的 CJK 正则会把 U+3002 漏掉（那段是 U+3400 起步的），
        于是句末的「。」被当成半宽，行就排超了——所以另开了 WIDE。
        """
        self.assertEqual(MODULE.display_units("。"), 2)
        self.assertEqual(MODULE.display_units("，"), 2)
        self.assertEqual(MODULE.display_units("、"), 2)

    def test_digits_and_latin_are_one_unit(self):
        """`全球8%` 按字符数是 4、按宽度是 6——差的就是这个。"""
        self.assertEqual(MODULE.display_units("全球8%"), 6)

    def test_prefix_len_is_measured_in_units_not_characters(self):
        """44 单位的预算在纯汉字下是 22 个字符、纯拉丁下是 44 个字符。"""
        self.assertEqual(MODULE.unit_prefix_len("甲乙丙丁", 4), 2)
        self.assertEqual(MODULE.unit_prefix_len("abcdef", 4), 4)
        self.assertEqual(MODULE.unit_prefix_len("甲乙丙", 100), 3)

    def test_prefix_len_always_makes_progress(self):
        """预算小于一个汉字时至少要切 1 个字符，否则硬切会死循环。"""
        self.assertEqual(MODULE.unit_prefix_len("甲乙丙", 1), 1)

    def test_english_breaks_at_word_boundaries(self):
        """英文硬切要落在空格上。

        实测纯按宽度切出过 `entire cli` / `mate fight,`——半截单词，读起来像坏掉了。
        """
        text = "It is one of the biggest hidden levers in the entire climate fight,"
        rows = MODULE.split_text_rows(text, MODULE.ROW_UNITS)
        self.assertGreater(len(rows), 1, rows)
        for row in rows:
            self.assertEqual(row, row.strip(), row)
        # 词序列必须完全一致：中途劈开单词的话，" ".join(rows).split() 会多出一个碎词
        self.assertEqual(" ".join(rows).split(), text.split(), rows)

    def test_chinese_still_splits_by_width_without_spaces(self):
        """中文没有空格可依，必须回落到按宽度切，而且不能因此不切。"""
        text = "这是一句没有任何空格的中文长句子用来确认它仍然会被按宽度硬切开"
        rows = MODULE.split_text_rows(text, MODULE.ROW_UNITS)
        self.assertEqual("".join(rows), text)
        self.assertGreater(len(rows), 1)

    def test_a_long_single_word_falls_back_to_a_width_cut(self):
        """没有空格的超长单词（URL 之类）不能因为找不到词边界就不切。"""
        text = "x" * 120
        rows = MODULE.split_text_rows(text, MODULE.ROW_UNITS)
        self.assertGreater(len(rows), 1, rows)
        self.assertEqual("".join(rows), text)

    def test_hard_split_respects_the_budget(self):
        text = "这是一句没有任何标点的中文长句子用来测试按宽度硬切的行为是否正确"
        rows = MODULE.split_text_rows(text, MODULE.ROW_UNITS)
        self.assertEqual("".join(rows), text)
        for row in rows[:-1]:
            self.assertLessEqual(MODULE.display_units(row), MODULE.ROW_UNITS)


class OriginalRowTests(unittest.TestCase):
    """原文那一套的行同样要拆。

    实测 TED 自动字幕合并滚动 cue 之后，字幕句长度：中位 44、p90 67、最长 78 个
    拉丁字符，而渲染器缩到可读下限也只放得下 60 个——40.3% 的 cue 超过 50 字符，
    21.6% 直接超过 60，必然报「台词过长」把整张图搞失败。
    之前「英文一行放得下」的结论是在 VTT 的滚动短行上量的，不是合并后的整句。
    """

    @staticmethod
    def window(texts, step=3.0):
        return {
            "start": 0.0,
            "end": step * len(texts),
            "lines": [
                {"start": index * step, "end": index * step + 2.6, "text": text}
                for index, text in enumerate(texts)
            ],
        }

    def test_short_lines_are_left_exactly_as_they_were(self):
        """放得下时一个 cue 一行、时间取 cue 中点——与改动前完全一致（走快路径）。"""
        window = self.window(["Today we are going to talk", "Please listen carefully"])
        rows = MODULE.original_rows(window)
        self.assertEqual([row["text"] for row in rows],
                         ["Today we are going to talk", "Please listen carefully"])
        self.assertEqual([row["t"] for row in rows], [1.3, 4.3])

    def test_a_long_english_cue_is_split_instead_of_failing_the_render(self):
        """这句 61 个拉丁字符，渲染器缩到下限也放不下——实测它让整张图渲染失败。"""
        cue = "or increasingly, LNG, which releases carbon dioxide and methane,"
        rows = MODULE.original_rows(self.window([cue, "Short one"]))
        self.assertGreater(len(rows), 2, rows)
        for row in rows:
            self.assertLessEqual(MODULE.display_units(row["text"]), MODULE.ROW_UNITS_MAX)
        # 拆行只能加断点，不能丢字符（断点处的空格会被剥掉，所以按非空白比对）
        squash = lambda text: "".join(text.split())  # noqa: E731
        self.assertEqual(squash("".join(row["text"] for row in rows)),
                         squash(cue + "Short one"))

    def test_no_row_starts_or_ends_with_whitespace(self):
        """断点常落在空格上——实测切出过行首带空格的 ` which releases…`，会莫名缩进一格。"""
        cue = "or increasingly, LNG, which releases carbon dioxide and methane,"
        rows = MODULE.original_rows(self.window([cue, "Short one"]))
        for row in rows:
            self.assertEqual(row["text"], row["text"].strip(), row["text"])

    def test_times_stay_strictly_increasing(self):
        texts = ["The technologies around these ships are monopolized by a handful of companies."] * 3
        rows = MODULE.original_rows(self.window(texts, step=0.05))
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(rows, rows[1:])), rows)

    def test_cues_at_or_before_zero_are_dropped(self):
        window = self.window(["First", "Second"])
        window["lines"][0]["start"] = -2.0
        window["lines"][0]["end"] = 0.0
        rows = MODULE.original_rows(window)
        self.assertEqual([row["text"] for row in rows], ["Second"])

    def test_a_window_too_long_for_seven_rows_is_trimmed_from_the_tail(self):
        """拆行后超过 7 行时从尾部整条退 cue——退的是整句，不是半句话。

        实测 `render-script` 会直接报「台词脚本最多支持 7 个时间点；请拆成多张图」。
        """
        window = self.window(
            ["The technologies around these ships are monopolized by a handful of companies."] * 6,
            step=4.0)
        fitted = MODULE.fit_window(window)
        self.assertLess(len(fitted["lines"]), 6)
        self.assertGreaterEqual(len(fitted["lines"]), MODULE.MIN_LINES)
        self.assertEqual(fitted["dropped_lines"], 6 - len(fitted["lines"]))
        self.assertLessEqual(len(MODULE.original_rows(fitted)), MODULE.MAX_TIMES_PER_IMAGE)
        self.assertEqual(fitted["end"], fitted["lines"][-1]["end"])

    def test_a_window_that_already_fits_is_returned_untouched(self):
        """放得下就别动它——不必要地改窗口会让老输出整体位移。"""
        window = self.window(["Short one", "Short two", "Short three"])
        self.assertIs(MODULE.fit_window(window), window)

    def test_trimming_shrinks_the_span_so_the_translation_follows(self):
        """裁窗口必须连 end 一起收，否则译文那套还会覆盖已经被退掉的那几条 cue。"""
        fitted = MODULE.fit_window(self.window(
            ["The technologies around these ships are monopolized by a handful of companies."] * 6,
            step=4.0))
        translation = [
            {"start": index * 4.0, "end": index * 4.0 + 2.6, "text": f"译文{index}"}
            for index in range(6)
        ]
        rows = MODULE.translated_rows(fitted, translation)
        self.assertLessEqual(len(rows), MODULE.MAX_TIMES_PER_IMAGE)
        self.assertNotIn("译文5", [row["text"] for row in rows])

    def test_build_outputs_reports_what_was_trimmed(self):
        """裁掉内容必须留下痕迹，不能悄悄少几条。"""
        lines = [
            {"start": index * 4.0, "end": index * 4.0 + 2.6,
             "text": "The technologies around these ships are monopolized by a handful of companies."}
            for index in range(10)
        ]
        windows = MODULE.build_windows(lines)
        for window in windows:
            window["score"] = {"total": 0.5}
        bundle = MODULE.build_outputs(windows, top=1)
        self.assertIn("trimmed", bundle)
        self.assertTrue(bundle["trimmed"][0]["dropped_lines"] > 0)
        for script in bundle["script_lines"]:
            self.assertLessEqual(len(script["lines"]), MODULE.MAX_TIMES_PER_IMAGE)

    def test_build_outputs_never_emits_a_row_the_renderer_would_reject(self):
        """底线：产出的每一行都必须渲得出来，否则等于没产出。

        用**真实测到的长句**（61–78 个拉丁字符）跑完整条 build_outputs——
        这正是 2026-10-12 端到端演示里让英文那两张图全部失败的输入。
        """
        texts = [
            "The technologies around these ships are monopolized by a handful of companies.",
            "a dirty superpollutant that accelerates climate change and air pollution.",
            "This is what it looks like to move a hidden engine of the global economy",
            "The technology for clean steel or green shipping fuels is already here.",
            "Our push for finance, regulations and infrastructure will change this.",
            "or increasingly, LNG, which releases carbon dioxide and methane,",
        ]
        lines = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": text}
            for index, text in enumerate(texts * 2)
        ]
        windows = MODULE.build_windows(lines)
        for window in windows:
            window["score"] = {"total": 0.5}

        bundle = MODULE.build_outputs(windows, top=2)
        self.assertTrue(bundle["script_lines"], "长句窗口一个都没产出")
        for script in bundle["script_lines"]:
            self.assertGreaterEqual(len(script["lines"]), MODULE.MIN_LINES)
            for row in script["lines"]:
                self.assertLessEqual(
                    MODULE.display_units(row["text"]), MODULE.ROW_UNITS_MAX, row["text"])


class TranslationTests(unittest.TestCase):
    """YouTube 把自动翻译轨命名成 `<目标语>-<源语>`，如 `zh-Hans-en`。"""

    def test_parse_targets(self):
        self.assertEqual(MODULE.parse_targets(None), [])
        self.assertEqual(MODULE.parse_targets(""), [])
        self.assertEqual(MODULE.parse_targets("zh-Hans"), ["zh-Hans"])
        self.assertEqual(MODULE.parse_targets("zh-Hans, zh-Hant "), ["zh-Hans", "zh-Hant"])

    def test_source_matching_the_original_is_preferred(self):
        """同一支视频会按每种识别语言各生成一份译文（实测 ar/en/fr/ko/my/pt-BR）。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "T_[id].en.srt").write_text("x", encoding="utf-8")
            for name in ["T_[id].zh-Hans-ar.srt", "T_[id].zh-Hans-en.srt",
                         "T_[id].zh-Hans-fr.srt", "T_[id].zh-Hant-en.srt"]:
                (folder / name).write_text("x", encoding="utf-8")
            found = MODULE.find_translation(folder / "T_[id].en.srt", ["zh-Hans"])
            self.assertEqual(found.name, "T_[id].zh-Hans-en.srt")

    def test_bare_language_code_wins_over_generated_track(self):
        """裸语言码是人工上传的译文，优先于机器翻译轨。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "T_[id].en.srt").write_text("x", encoding="utf-8")
            (folder / "T_[id].zh-Hans-en.srt").write_text("x", encoding="utf-8")
            (folder / "T_[id].zh-Hans.srt").write_text("x", encoding="utf-8")
            found = MODULE.find_translation(folder / "T_[id].en.srt", ["zh-Hans"])
            self.assertEqual(found.name, "T_[id].zh-Hans.srt")

    def test_bracketed_video_id_does_not_break_lookup(self):
        """yt-dlp 的输出模板带 `[id]`，方括号是 glob 元字符——用 glob 会一条都匹配不到。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = folder / "2026-10-09_Title ｜ Name_[wsstlwuE5XY].en.vtt"
            original.write_text("x", encoding="utf-8")
            (folder / "2026-10-09_Title ｜ Name_[wsstlwuE5XY].zh-Hans-en.srt").write_text(
                "x", encoding="utf-8"
            )
            found = MODULE.find_translation(original, ["zh-Hans"])
            self.assertIsNotNone(found)
            self.assertTrue(found.name.endswith("zh-Hans-en.srt"))

    def test_no_targets_or_no_match_gives_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "T_[id].en.srt").write_text("x", encoding="utf-8")
            original = folder / "T_[id].en.srt"
            self.assertIsNone(MODULE.find_translation(original, []))
            self.assertIsNone(MODULE.find_translation(original, ["ja"]))

    def test_video_file_is_not_mistaken_for_a_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "T_[id].en.srt").write_text("x", encoding="utf-8")
            (folder / "T_[id].mkv").write_bytes(b"stub")
            self.assertIsNone(MODULE.find_translation(folder / "T_[id].en.srt", ["zh-Hans"]))

    def test_rows_follow_time_and_skip_blank_cues(self):
        """实测译文轨里有「有时间戳、正文为空」的 cue（自动轨 2 条、人工轨 26 条）。"""
        window = {"start": 0.0, "end": 20.0}
        translation = [
            {"start": 0.0, "end": 5.0, "text": "甲"},
            {"start": 5.0, "end": 10.0, "text": ""},
            {"start": 10.0, "end": 15.0, "text": "乙"},
            {"start": 30.0, "end": 35.0, "text": "丙"},
        ]
        rows = MODULE.translated_rows(window, translation)
        self.assertEqual([row["text"] for row in rows], ["甲", "乙"])
        self.assertEqual([row["t"] for row in rows], [2.5, 12.5])

    def test_wrapped_lines_are_joined_correctly(self):
        """一条 cue 内部可以换行；中文直接接起来，英文要补回空格。"""
        window = {"start": 0.0, "end": 20.0}
        translation = [
            {"start": 0.0, "end": 5.0, "text": "甲\n乙"},
            {"start": 5.0, "end": 10.0, "text": "hello\nworld"},
        ]
        rows = MODULE.translated_rows(window, translation)
        self.assertEqual([row["text"] for row in rows], ["甲乙", "hello world"])

    def test_long_chinese_cue_is_split_to_fit_one_line(self):
        """实测硬约束：渲染器缩到可读下限也只放得下 29 个汉字。

        29 字那行会直接报「台词过长，缩小到可读下限后仍放不下」，必须拆。
        这里顺带守住「拆开的行都不超过可渲染宽度」和「时间仍然严格递增」。
        """
        window = {"start": 0.0, "end": 30.0}
        long_text = "或者越来越多地燃烧液化天然气，这会释放二氧化碳和甲烷，甲烷是"
        rows = MODULE.translated_rows(window, [{"start": 0.0, "end": 4.0, "text": long_text}])
        self.assertGreater(len(rows), 1, rows)
        for row in rows:
            self.assertLessEqual(MODULE.display_units(row["text"]), MODULE.ROW_UNITS_MAX)
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(rows, rows[1:])), rows)
        self.assertEqual("".join(row["text"] for row in rows), long_text)

    def test_rows_break_at_punctuation(self):
        """优先在标点处断句，且**标点不能落到下一行开头**（实测切出过 `…排放` / `。所以…`）。"""
        text = "钢铁生产造成了全球百分之八的温室气体排放。所以航运不是次要问题。"
        rows = MODULE.split_text_rows(text, 40)  # 40 单位 = 20 个汉字
        self.assertEqual("".join(rows), text)
        self.assertEqual(rows, ["钢铁生产造成了全球百分之八的温室气体排放。", "所以航运不是次要问题。"])
        for row in rows:
            self.assertFalse(MODULE.LEADING_PUNCT.match(row), row)

    def test_relaxation_never_exceeds_the_renderable_width(self):
        """放宽每行宽度的**硬上限是 56 单位**（≈28 汉字），再宽整张图就渲不出来。

        老版本一路放宽到 34 个汉字/行（= 68 单位），行数确实回到 7 行以内，
        但那一行渲不出来——当时的测试只数了行数、没量宽度，所以是绿的。
        宁可多画几行，也不能画出一行渲不出的字。
        """
        window = {"start": 0.0, "end": 60.0}
        translation = [
            {"start": index * 10.0, "end": index * 10.0 + 9.0,
             "text": "这是一句用来测试拆行逻辑的中文长句大约三十来个字左右的样子"}
            for index in range(6)
        ]
        rows = MODULE.translated_rows(window, translation)
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(
                MODULE.display_units(row["text"]), MODULE.ROW_UNITS_MAX, row["text"])

    def test_a_window_that_cannot_be_squeezed_is_the_callers_problem(self):
        """压不进 7 行就如实返回多行，由 build_outputs 决定丢弃——不要偷偷切掉句子。"""
        window = {"start": 0.0, "end": 60.0}
        translation = [
            {"start": index * 10.0, "end": index * 10.0 + 9.0,
             "text": "这是一句用来测试拆行逻辑的中文长句大约三十来个字左右的样子"}
            for index in range(6)
        ]
        rows = MODULE.translated_rows(window, translation)
        self.assertGreater(len(rows), MODULE.MAX_TIMES_PER_IMAGE)
        joined = "".join(row["text"] for row in rows)
        for cue in translation:
            self.assertIn(cue["text"], joined)

    def test_build_outputs_skips_a_window_whose_translation_cannot_fit(self):
        """译文行数落在渲染器接受范围之外时，这一组不产出中文图，但要有据可查。"""
        lines = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": text}
            for index, text in enumerate(EN_LINES)
        ]
        windows = MODULE.build_windows(lines)
        for window in windows:
            window["score"] = {"total": 0.5}
        # 每条 cue 只有 1 个字，行数永远降不到 7 行以内
        translation = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": f"字{index}"}
            for index in range(len(EN_LINES))
        ]
        bundle = MODULE.build_outputs(windows, top=2, translation=translation)
        for item in bundle["script_lines_zh"]:
            self.assertLessEqual(len(item["lines"]), MODULE.MAX_TIMES_PER_IMAGE)
        self.assertIn("translation_skipped", bundle)

    def test_build_outputs_keeps_order_in_step_across_languages(self):
        lines = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": text}
            for index, text in enumerate(EN_LINES)
        ]
        windows = MODULE.build_windows(lines)
        for window in windows:
            window["score"] = {"total": 0.5}
        translation = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": text}
            for index, text in enumerate(ZH_LINES)
        ]
        bundle = MODULE.build_outputs(windows, top=2, translation=translation)
        self.assertIn("script_lines_zh", bundle)
        self.assertEqual(
            [item["order"] for item in bundle["script_lines_zh"]],
            [item["order"] for item in bundle["script_lines"]],
        )
        for item in bundle["script_lines_zh"]:
            self.assertGreaterEqual(len(item["lines"]), MODULE.MIN_LINES, item)
            for line in item["lines"]:
                self.assertNotIn("\n", line["text"])

    def test_without_translation_no_second_set_is_produced(self):
        lines = [
            {"start": index * 3.0, "end": index * 3.0 + 2.6, "text": text}
            for index, text in enumerate(EN_LINES)
        ]
        windows = MODULE.build_windows(lines)
        for window in windows:
            window["score"] = {"total": 0.5}
        bundle = MODULE.build_outputs(windows, top=2)
        self.assertNotIn("script_lines_zh", bundle)

    def test_pick_with_translate_writes_both_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = folder / "talk.en.srt"
            original.write_text(make_srt(EN_LINES), encoding="utf-8")
            (folder / "talk.zh-Hans-en.srt").write_text(make_srt(ZH_LINES), encoding="utf-8")
            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(original), "--out", str(out),
                 "--top", "2", "--translate", "zh-Hans"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ["script_lines.json", "script_lines_zh.json", "审阅表.md"]:
                self.assertTrue((out / name).is_file(), name)
            self.assertTrue((out / "scripts").is_dir())
            self.assertTrue((out / "scripts_zh").is_dir())

            original_set = json.loads((out / "script_lines.json").read_text(encoding="utf-8"))
            translated = json.loads((out / "script_lines_zh.json").read_text(encoding="utf-8"))
            self.assertTrue(original_set)
            self.assertEqual(
                [item["order"] for item in translated],
                [item["order"] for item in original_set],
            )
            for item in translated:
                self.assertGreaterEqual(len(item["lines"]), 2)
            review = (out / "审阅表.md").read_text(encoding="utf-8")
            self.assertIn("译文", review)

    def test_pick_without_a_translation_file_still_succeeds(self):
        """要了译文却找不到轨：照旧出原文一套，不报错、不静默出半套。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original = folder / "talk.en.srt"
            original.write_text(make_srt(EN_LINES), encoding="utf-8")
            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(original), "--out", str(out),
                 "--top", "2", "--translate", "zh-Hans"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "script_lines.json").is_file())
            self.assertFalse((out / "script_lines_zh.json").exists())
            self.assertFalse((out / "scripts_zh").exists())


def ffmpeg_exe():
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 探测不到就跳过渲染类测试
        return None


def cjk_font():
    for path in [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]:
        if Path(path).exists():
            return path
    return None


class FfmpegResolutionTests(unittest.TestCase):
    """时长校正依赖能不能解析到 FFmpeg。CI 在这里抓到过一个真缺陷。"""

    SAMPLE_LINES = [
        "先交代一下背景情况", "政策影响了整个市场结构",
        "企业必须重新评估自己的定位", "因为成本上升会挤压利润空间",
        "所以结论是需要提前布局", "而不是等到变化发生之后",
    ]

    def test_resolve_ffmpeg_falls_back_to_path(self):
        """没有 imageio-ffmpeg 时要能回落到 PATH 上的 ffmpeg。

        原来的实现只写裸 "ffmpeg"，等于既没试 imageio-ffmpeg 也没做回落判断；
        本机装了系统 ffmpeg 所以看不出来，CI 上一跑就暴露。
        """
        import builtins

        saved = (MODULE._FFMPEG, MODULE._FFMPEG_TRIED)
        MODULE._FFMPEG, MODULE._FFMPEG_TRIED = None, False
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "imageio_ffmpeg":
                raise ImportError("模拟未安装 imageio-ffmpeg")
            return real_import(name, *args, **kwargs)

        try:
            with mock.patch.object(builtins, "__import__", side_effect=fake_import):
                with mock.patch("shutil.which", return_value="/fake/bin/ffmpeg"):
                    self.assertEqual(MODULE.resolve_ffmpeg(), "/fake/bin/ffmpeg")
        finally:
            MODULE._FFMPEG, MODULE._FFMPEG_TRIED = saved

    def test_resolve_ffmpeg_finds_something_in_this_environment(self):
        """顺序与 native_subtitle_stitch.py 一致：先 imageio-ffmpeg，再 PATH。"""
        saved = (MODULE._FFMPEG, MODULE._FFMPEG_TRIED)
        MODULE._FFMPEG, MODULE._FFMPEG_TRIED = None, False
        try:
            self.assertTrue(MODULE.resolve_ffmpeg(), "解析不到任何 FFmpeg，环境不完整")
        finally:
            MODULE._FFMPEG, MODULE._FFMPEG_TRIED = saved

    def test_unreadable_video_duration_is_flagged_not_silently_ignored(self):
        """找到了视频却读不出时长，必须标注出来，不能静默当成转写时长可信。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "clip.mp4").write_bytes(b"stub")
            lines = self.SAMPLE_LINES
            transcript = {
                "text": "".join(lines), "duration": 40.0,
                "backend": "test", "model": "test",
                "segments": make_segments(lines),
            }
            source = folder / "clip.mp4.transcript.json"
            source.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")

            out = folder / "out"
            with mock.patch.object(MODULE, "probe_duration", return_value=None):
                MODULE.process_one(source, out, 1)

            payload = json.loads((out / "candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["duration_source"], "transcript-no-ffmpeg")
            review = (out / "审阅表.md").read_text(encoding="utf-8")
            self.assertIn("未经真实时长校正", review)


class RenderTests(unittest.TestCase):
    """--render 会真的调起 FFmpeg 和 Pillow，跑通它才算端到端可用。

    只在环境确实缺东西时跳过（没有 FFmpeg / 没有中日韩字体）。
    除此之外一律必须通过——否则真实回归会被伪装成跳过。
    """

    # 无信号词的平淡句，放在前 10 行
    BLAND = [
        "会议室的灯忽明忽暗", "窗外刚好下起小雨", "桌上的水杯已经凉透",
        "墙上挂着一幅旧地图", "椅子发出轻微的声响", "远处传来一阵脚步声",
        "空调的风向换了两次", "笔记本翻到了空白页", "走廊里的灯依次亮起",
        "时间一分一秒过去",
    ]
    # 带信号词的强句，放在后面——故意让它们落在视频时长之外
    STRONG = [
        "真正的核心是这个系统没有闭环", "而不是把责任推给一线同事",
        "因为成本上升会挤压利润空间", "所以必须提前布局",
        "很多人以为这是效率问题其实是分配问题", "关键在于把指标拆到可执行",
        "上线一个月被罚了500万", "能提升百分之三十的转化",
        "千万不要等到出事才补救", "本质上这是一场认知升级",
        "什么叫真正的护城河", "应该先定标准再谈执行",
        "真相是多数团队都搞反了", "归根结底还是要看现金流",
    ]

    def make_fixture(self, folder, seconds=30):
        engine = ffmpeg_exe()
        if engine is None:
            self.skipTest("没有可用的 FFmpeg")
        video = folder / "clip.mp4"
        subprocess.run(
            [engine, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10",
             "-t", str(seconds), "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)],
            check=True, capture_output=True,
        )
        return video

    def make_calibration_transcript(self, folder, declared=90.0):
        """转写声明 90 秒，视频只有 30 秒，且强句全部落在 30 秒之后。

        这个错位是刻意的：只有用视频实测时长校正，才会丢掉 30 秒之后的段，
        时间点才落在视频内。沿用转写声明值就会选出 57 秒处的时间点，
        被渲染器以「必须小于视频时长」拒绝——这正是实测踩到的那个 bug。
        """
        lines = self.BLAND + self.STRONG
        transcript = {
            "text": "".join(lines), "duration": declared,
            "backend": "test", "model": "test",
            "segments": make_segments(lines, step=2.5, span=2.2),
        }
        source = folder / "clip.mp4.transcript.json"
        source.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        return source

    def test_render_script_mode_produces_images_from_sibling_video(self):
        font = cjk_font()
        if font is None:
            self.skipTest("没有可用的中日韩字体")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.make_fixture(folder)
            source = self.make_calibration_transcript(folder)
            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(source), "--out", str(out),
                 "--top", "2", "--render", "--render-mode", "script",
                 "--aspect", "9:16", "--width", "540", "--font", font],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            images = sorted((out / "images").glob("*.jpg"))
            self.assertTrue(images, result.stdout + result.stderr)
            self.assertIn("出图", result.stdout)
            self.assertNotIn("⚠", result.stdout, result.stdout)

    def test_render_native_mode_produces_images(self):
        if cjk_font() is None:
            self.skipTest("没有可用的中日韩字体")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.make_fixture(folder)
            source = self.make_calibration_transcript(folder)
            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(source), "--out", str(out),
                 "--top", "2", "--render", "--render-mode", "native",
                 "--band-top", "0.70", "--band-bottom", "0.95"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(sorted((out / "images").glob("*.jpg")), result.stdout + result.stderr)

    def test_real_video_duration_overrides_transcript_duration(self):
        """转写声明的时长会与视频不符；取帧必须以视频实测时长为准。"""
        engine = ffmpeg_exe()
        if engine is None:
            self.skipTest("没有可用的 FFmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.make_fixture(folder, seconds=30)
            source = self.make_calibration_transcript(folder, declared=90.0)

            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(source), "--out", str(out), "--top", "2"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads((out / "candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["duration_source"], "video")
            self.assertAlmostEqual(payload["duration"], 30.0, delta=0.6)
            self.assertAlmostEqual(payload["duration_drift"], 60.0, delta=0.6)
            manifest = json.loads((out / "native_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["images"])
            for image in manifest["images"]:
                self.assertLess(image["times"][-1], 30.0)

    def test_render_reports_missing_sibling_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            lines = ["先交代一下背景情况", "政策影响了整个市场结构",
                     "企业必须重新评估自己的定位", "因为成本上升会挤压利润空间",
                     "所以结论是需要提前布局", "而不是等到变化发生之后"]
            transcript = {
                "text": "".join(lines), "duration": 20.0,
                "backend": "test", "model": "test",
                "segments": make_segments(lines),
            }
            source = folder / "orphan.mp4.transcript.json"
            source.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
            out = folder / "out"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "pick", str(source), "--out", str(out),
                 "--top", "2", "--render"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("找不到与", result.stdout)
            # 出图失败不影响候选与时间点产出
            self.assertTrue((out / "native_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
