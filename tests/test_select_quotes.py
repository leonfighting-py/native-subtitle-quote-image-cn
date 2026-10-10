import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
