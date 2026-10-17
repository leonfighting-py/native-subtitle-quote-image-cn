import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageOps


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "native-subtitle-quote-image"
    / "scripts"
    / "native_subtitle_stitch.py"
)
ENV_SCRIPT = (
    ROOT
    / "skills"
    / "native-subtitle-quote-image"
    / "scripts"
    / "check_environment.py"
)
SPEC = importlib.util.spec_from_file_location("native_subtitle_stitch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HelperTests(unittest.TestCase):
    def test_parse_aspect_and_safe_title(self):
        self.assertEqual(MODULE.parse_aspect("3:4"), (3.0, 4.0))
        self.assertEqual(MODULE.safe_title('a/b:c*?"<>|'), "a_b_c")

    def test_default_sample_times_cover_range_with_24_frames(self):
        times = MODULE.build_sample_times(0, 230, None, 48)
        self.assertEqual(len(times), 24)
        self.assertEqual(times[0], 0)
        self.assertAlmostEqual(times[-1], 230)

    def test_sample_times_enforce_frame_cap(self):
        with self.assertRaisesRegex(SystemExit, "超过上限"):
            MODULE.build_sample_times(0, 100, 1, 48)

    def test_focus_times_add_before_middle_and_after(self):
        times = MODULE.build_focus_times([1, 3], 0.5, 5, 48)
        self.assertEqual(times, [0.5, 1.0, 1.5, 2.5, 3.0, 3.5])

    def test_focus_times_clip_and_deduplicate_edges(self):
        times = MODULE.build_focus_times([0, 2.7], 0.5, 3, 48)
        self.assertEqual(times, [0.0, 0.5, 2.2, 2.7])

    def test_auto_layout_keeps_subtitle_strips_compact(self):
        self.assertEqual(MODULE.choose_hero_fraction(4), 0.7)
        self.assertEqual(MODULE.choose_hero_fraction(3), 0.775)
        self.assertEqual(MODULE.choose_hero_fraction(2), 0.82)
        self.assertEqual(MODULE.choose_hero_fraction(7), 0.48)
        self.assertEqual(MODULE.choose_hero_fraction(4, 0.6), 0.6)

    def test_script_lines_require_increasing_timestamps_and_text(self):
        lines = MODULE.normalize_script_lines(
            {
                "lines": [
                    {"t": 1, "text": "First"},
                    {"t": 2, "text": "Second"},
                ]
            },
            3,
        )
        self.assertEqual(lines[1]["text"], "Second")
        with self.assertRaisesRegex(SystemExit, "严格递增"):
            MODULE.normalize_script_lines(
                {
                    "lines": [
                        {"t": 2, "text": "First"},
                        {"t": 1, "text": "Second"},
                    ]
                },
                3,
            )
        with self.assertRaisesRegex(SystemExit, "最多支持 7"):
            MODULE.normalize_script_lines(
                {
                    "lines": [
                        {"t": index / 10, "text": f"Line {index}"}
                        for index in range(8)
                    ]
                },
                3,
            )

    def test_native_times_share_script_mode_cap(self):
        times = MODULE.normalize_times([index / 10 for index in range(7)])
        self.assertEqual(len(times), 7)
        with self.assertRaisesRegex(SystemExit, "最多支持 7"):
            MODULE.normalize_times([index / 10 for index in range(8)])

    def test_cjk_detection_covers_chinese_japanese_and_korean(self):
        self.assertTrue(MODULE.contains_cjk("中文"))
        self.assertTrue(MODULE.contains_cjk("かな"))
        self.assertTrue(MODULE.contains_cjk("한글"))
        self.assertFalse(MODULE.contains_cjk("English"))

    def test_environment_check_local_mode_is_machine_readable(self):
        proc = subprocess.run(
            [sys.executable, str(ENV_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["mode"], "local")
        self.assertTrue(payload["ok"])
        components = {item["component"] for item in payload["components"]}
        self.assertIn("Python 3.10+", components)
        self.assertIn("yt-dlp", components)
        self.assertIn("CJK font", components)

    def test_environment_check_url_mode_guides_cookie_recovery(self):
        proc = subprocess.run(
            [sys.executable, str(ENV_SCRIPT), "--url-mode"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("不要退回本地模式", proc.stdout)
        self.assertIn("--cookies-from-browser chrome", proc.stdout)

    def test_render_one_has_requested_dimensions(self):
        frame = Image.new("RGB", (640, 360), "#336699")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MODULE, "grab_frame", return_value=frame
        ):
            out = Path(tmp) / "render.jpg"
            MODULE.render_one(
                "unused.mp4",
                [0, 1, 2, 3, 4],
                out,
                (3, 4),
                300,
                0.68,
                0.96,
                0.42,
                layout="fixed",
            )
            with Image.open(out) as rendered:
                self.assertEqual(rendered.size, (300, 400))

    def test_render_one_default_keeps_source_geometry_not_seventy_percent(self):
        def fake_frame(_video, seconds):
            color = "#cc0000" if seconds == 0 else "#0033cc"
            return Image.new("RGB", (640, 360), color)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MODULE, "grab_frame", side_effect=fake_frame
        ):
            out = Path(tmp) / "auto-layout.jpg"
            MODULE.render_one(
                "unused.mp4",
                [0, 1, 2, 3, 4],
                out,
                (3, 4),
                300,
                0.78,
                0.96,
                None,
            )
            with Image.open(out) as rendered:
                self.assertEqual(rendered.size, (300, 284))
                self.assertGreater(rendered.getpixel((10, 160))[0], 180)
                self.assertGreater(rendered.getpixel((10, 164))[2], 150)

    def test_missing_input_is_readable_without_traceback(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "render",
                "/no/such/video.mp4",
                "--manifest",
                "/no/such/manifest.json",
                "--out-dir",
                "/tmp/unused-native-subtitle-output",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("视频不存在或不是文件", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class NaturalGeometryTests(unittest.TestCase):
    def test_native_subtitles_have_same_size_in_hero_and_every_strip(self):
        for frame_size in [(1280, 720), (720, 1280)]:
            for layout, fit in [("natural", "crop"), ("fixed", "pad"), ("fixed", "crop")]:
                with self.subTest(frame_size=frame_size, layout=layout, fit=fit):
                    w, h = frame_size
                    frame = Image.new("RGB", frame_size, "black")
                    # 宽度覆盖近全帧，模拟长字幕，能同时抓出裁字和字号不一致。
                    ImageDraw.Draw(frame).rectangle(
                        (round(w * 0.03), round(h * 0.88), round(w * 0.97), round(h * 0.91)),
                        fill="white",
                    )
                    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                        MODULE, "grab_frame", return_value=frame
                    ):
                        out = Path(tmp) / "uniform.jpg"
                        MODULE.render_one("unused", [0, 1, 2, 3, 4], out, (3, 4), 1080,
                                          0.78, 0.96, None, layout=layout, fit=fit)
                        with Image.open(out) as opened:
                            mask = opened.convert("L").point(lambda p: 255 if p > 127 else 0)
                            runs = []
                            for y in range(mask.height):
                                bounds = mask.crop((0, y, mask.width, y + 1)).getbbox()
                                if bounds is not None:
                                    if not runs or runs[-1][1] != y:
                                        runs.append([y, y + 1, bounds[0], bounds[2]])
                                    else:
                                        runs[-1][1] = y + 1
                                        runs[-1][2] = min(runs[-1][2], bounds[0])
                                        runs[-1][3] = max(runs[-1][3], bounds[2])
                            self.assertEqual(len(runs), 5)
                            heights = [r[1] - r[0] for r in runs]
                            widths = [r[3] - r[2] for r in runs]
                            self.assertLessEqual(max(heights) - min(heights), 1)
                            self.assertLessEqual(max(widths) - min(widths), 1)
                            if layout == "fixed":
                                self.assertEqual(opened.size, (1080, 1440))
                                source_height = int(h * 0.96) + 4 * (int(h * 0.96) - int(h * 0.78))
                                scale = min(1080 / w, 1440 / source_height)
                                expected_width = (round(w * 0.97) - round(w * 0.03) + 1) * scale
                                if fit == "pad":
                                    self.assertLessEqual(abs(min(widths) - expected_width), 2)
                                else:
                                    # 长字幕只允许裁到安全边界：不小于留边版，也不碰画布边缘。
                                    self.assertGreaterEqual(min(widths), expected_width - 2)
                                    self.assertGreater(min(r[2] for r in runs), 0)
                                    self.assertLess(max(r[3] for r in runs), opened.width)
                            else:
                                self.assertGreater(min(widths), 1080 * 0.9)

    def test_crop_and_same_width_keep_exact_source_pixels(self):
        frame = Image.new("RGB", (1280, 720), "black")
        ImageDraw.Draw(frame).ellipse((100, 100, 300, 300), fill="white")
        cropped, y0, y1 = MODULE.crop_band(frame, 0, 0.72)
        result = MODULE.scale_to_width(cropped, 1280)
        self.assertEqual((y0, y1), (0, 518))
        self.assertEqual(result.size, (1280, 518))
        self.assertEqual(result.tobytes(), frame.crop((0, 0, 1280, 518)).tobytes())

    def test_scaling_preserves_circle_geometry(self):
        frame = Image.new("RGB", (1280, 518), "black")
        ImageDraw.Draw(frame).ellipse((100, 100, 300, 300), fill="white")
        result = MODULE.scale_to_width(frame, 640)
        bounds = result.convert("L").point(lambda p: 255 if p > 127 else 0).getbbox()
        self.assertEqual(result.size, (640, 259))
        self.assertLessEqual(abs((bounds[2] - bounds[0]) - (bounds[3] - bounds[1])), 1)

    def test_native_natural_retains_full_width_and_band_height(self):
        frame = Image.new("RGB", (640, 360), "#336699")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MODULE, "grab_frame", return_value=frame
        ):
            out = Path(tmp) / "native.jpg"
            MODULE.render_one("unused", [0, 1, 2, 3, 4], out, (3, 4), None,
                              0.78, 0.96, None, layout="natural")
            with Image.open(out) as rendered:
                # 主图 345 + 四条 (345 - 280)，没有填满固定画布。
                self.assertEqual(rendered.size, (640, 605))

    def test_scripted_crop_does_not_stretch_hero_back_to_source_height(self):
        frame = Image.new("RGB", (1280, 720), "black")
        ImageDraw.Draw(frame).ellipse((100, 100, 300, 300), fill="white")
        lines = [{"t": index, "text": f"Line {index}"} for index in range(5)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            MODULE, "grab_frame", return_value=frame
        ), mock.patch.object(MODULE, "draw_scripted_subtitle") as draw_text:
            out = Path(tmp) / "script.jpg"
            MODULE.scripted_render_one("unused", lines, out, (3, 4), None,
                                       0.88, None, None, None,
                                       layout="natural", frame_bottom=0.72)
            with Image.open(out) as rendered:
                self.assertEqual(rendered.size, (1280, 806))
                bounds = rendered.crop((0, 0, 1280, 518)).convert("L").point(
                    lambda p: 255 if p > 127 else 0
                ).getbbox()
                self.assertEqual(bounds[2] - bounds[0], bounds[3] - bounds[1])
            self.assertEqual([call.args[1] for call in draw_text.call_args_list],
                             [line["text"] for line in lines])

    def test_cli_rejects_conflicting_layout_and_invalid_crop_before_io(self):
        for options, message in [
            (["--layout", "natural", "--aspect", "3:4"], "不接受"),
            (["--layout", "natural", "--hero-fraction", "0.7"], "不接受"),
            (["--frame-top", "0.8", "--frame-bottom", "0.2"], "裁切必须满足"),
        ]:
            with self.subTest(options=options):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "render-script", "unused.mp4",
                     "--script", "unused.json", "--out", "unused.jpg", *options],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(message, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_cli_rejects_invalid_hero_search_before_io(self):
        for options, message in [
            (["--hero-search", "-1"], "大于等于 0"),
            (["--hero-search", "20"], "不能大于 10"),
            (["--layout", "natural", "--hero-search", "1"], "不接受 --hero-search"),
        ]:
            with self.subTest(options=options):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "render-script", "unused.mp4",
                     "--script", "unused.json", "--out", "unused.jpg", *options],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(message, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_cli_rejects_invalid_native_fit_options_before_io(self):
        for options, message in [
            (["--layout", "natural", "--fit", "crop"], "不接受 --fit"),
            (["--layout", "natural", "--crop-center", "0.4"], "不接受 --fit"),
            (["--crop-center", "1.5"], "0–1"),
            (["--fit", "pad", "--crop-center", "0.4"], "只用于 --fit crop"),
        ]:
            with self.subTest(options=options):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), "render", "unused.mp4",
                     "--manifest", "unused.json", "--out-dir", "unused", *options],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(message, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)


def subtitle_band(width, height, left, right, background="#5a5a5a"):
    """灰底上画白色黑描边的竖笔画，模拟烧录字幕。"""
    band = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(band)
    for x in range(left, right - 3, 9):
        draw.rectangle((x, height * 0.3, x + 3, height * 0.7), fill="white",
                       outline="black", width=1)
    return band


SKIN = (200, 150, 120)


def person_frame(size=(640, 360), coverage=0.09):
    """黑底正中盖一块肤色椭圆，模拟「画面里有人」；居中以免被出图裁切切掉。"""
    frame = Image.new("RGB", size, "black")
    side = int(2 * (coverage * size[0] * size[1] / 3.14159265) ** 0.5)
    left, top = (size[0] - side) // 2, (size[1] - side) // 2
    ImageDraw.Draw(frame).ellipse((left, top, left + side, top + side), fill=SKIN)
    return frame


def slide_frame(size=(640, 360)):
    """白底黑条，模拟纯色/近白的幻灯片。"""
    frame = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(frame)
    for index in range(6):
        y = round(size[1] / 6) + index * round(size[1] / 12)
        draw.rectangle((round(size[0] * 0.12), y, round(size[0] * 0.88), y + 14),
                       fill=(40, 40, 40))
    return frame


VARIED_COLORS = [
    (20, 40, 220), (0, 120, 255), (10, 180, 200), (0, 90, 90),
    (30, 30, 160), (0, 200, 120), (90, 0, 190), (150, 20, 200),
    (20, 60, 100), (0, 150, 255), (60, 0, 120), (10, 110, 60),
    (100, 100, 255), (0, 70, 180), (70, 20, 90), (25, 130, 190),
]


def varied_frame(size=(640, 360)):
    """16 条冷色竖条：颜色够多、不是近白，模拟真实但无人的画面。"""
    frame = Image.new("RGB", size, "black")
    draw = ImageDraw.Draw(frame)
    step = size[0] // len(VARIED_COLORS)
    for index, color in enumerate(VARIED_COLORS):
        draw.rectangle((index * step, 0, (index + 1) * step, size[1]), fill=color)
    return frame


def video_text_frame(size=(854, 480), center=0.5, thickness=0.14):
    """深色底 + 一条像大字的竖笔画，模拟画面自带的标题/Logo。"""
    frame = Image.new("RGB", size, (22, 26, 34))
    draw = ImageDraw.Draw(frame)
    band = thickness * size[1]
    top = center * size[1] - band / 2
    for x in range(int(size[0] * 0.06), int(size[0] * 0.94), 13):
        draw.rectangle((x, top + band * 0.18, x + 6, top + band * 0.82), fill="white")
    return frame


def clean_video_frame(size=(854, 480)):
    """同一底色、只有极低对比度的大色块：没有自带文字。"""
    frame = Image.new("RGB", size, (22, 26, 34))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, size[0], int(size[1] * 0.10)), fill=(27, 31, 40))
    draw.rectangle((0, int(size[1] * 0.72), size[0], size[1]), fill=(26, 30, 38))
    return frame


def striped_frame(size=(854, 480)):
    """满屏等距横纹：任何带位都差不多脏，用来守「别为了几个百分点瞎挪」。"""
    frame = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(frame)
    for y in range(0, size[1], 8):
        draw.rectangle((0, y, size[0], y + 4), fill="black")
    return frame


class SideCropTests(unittest.TestCase):
    def assertSubtitlesInside(self, canvas):
        """白色笔画的左右边界都不能贴到画布边缘，否则说明字被裁了。"""
        bright = canvas.convert("L").point(lambda p: 255 if p > 200 else 0)
        bounds = bright.getbbox()
        self.assertIsNotNone(bounds)
        self.assertGreater(bounds[0], 0)
        self.assertLess(bounds[2], canvas.width)

    def test_subtitle_extent_finds_text_and_ignores_plain_band(self):
        extent = MODULE.subtitle_extent(subtitle_band(640, 40, 200, 440))
        self.assertIsNotNone(extent)
        self.assertLessEqual(abs(extent[0] - 200), 3)
        self.assertLessEqual(abs(extent[1] - 436), 6)
        self.assertIsNone(MODULE.subtitle_extent(Image.new("RGB", (640, 40), "#5a5a5a")))

    def stack_with_subtitles(self, left, right, strips=4):
        hero = Image.new("RGB", (640, 300), "#336699")
        hero.paste(subtitle_band(640, 40, left, right), (0, 260))
        bands = [subtitle_band(640, 40, left, right) for _ in range(strips)]
        stack = MODULE.stack_parts([hero, *bands])
        extents = [MODULE.subtitle_extent(b) for b in [hero.crop((0, 260, 640, 300)), *bands]]
        return stack, extents

    def test_narrow_subtitles_fill_canvas_without_black_bars(self):
        stack, extents = self.stack_with_subtitles(260, 380)
        canvas, note = MODULE.fit_fixed_canvas(stack, extents, (300, 400))
        self.assertEqual(canvas.size, (300, 400))
        self.assertIn("无黑边", note)
        for y in (2, 397):
            self.assertGreater(max(canvas.getpixel((150, y))), 40)

    def test_wide_subtitles_limit_crop_and_keep_text(self):
        stack, extents = self.stack_with_subtitles(60, 580)
        canvas, note = MODULE.fit_fixed_canvas(stack, extents, (300, 400))
        self.assertIn("只裁去", note)
        padded = ImageOps.pad(stack, (300, 400), color="black")
        # 裁得比留边版少黑边，但所有字幕边界都在画布内。
        self.assertLess(canvas.convert("L").getbbox()[1], padded.convert("L").getbbox()[1])
        self.assertSubtitlesInside(canvas)

    def test_unknown_subtitle_or_pad_mode_falls_back_to_padding(self):
        stack, extents = self.stack_with_subtitles(260, 380)
        expected = ImageOps.pad(stack, (300, 400), method=Image.Resampling.LANCZOS, color="black")
        for fit, items, message in [
            ("crop", [extents[0], None, *extents[2:]], "第 2 句"),
            ("pad", extents, "--fit pad"),
        ]:
            with self.subTest(fit=fit):
                canvas, note = MODULE.fit_fixed_canvas(stack, items, (300, 400), fit)
                self.assertIn(message, note)
                self.assertEqual(canvas.tobytes(), expected.tobytes())

    def test_crop_center_moves_window_but_never_cuts_subtitles(self):
        hero = Image.new("RGB", (640, 300), "#336699")
        ImageDraw.Draw(hero).rectangle((0, 0, 100, 259), fill="#ff0000")
        stack = MODULE.stack_parts([hero, *[subtitle_band(640, 40, 260, 380)] * 4])
        extents = [(260, 380)] * 5
        left, _ = MODULE.fit_fixed_canvas(stack, extents, (300, 400), crop_center=0.0)
        centered, _ = MODULE.fit_fixed_canvas(stack, extents, (300, 400), crop_center=0.5)
        # 期望窗口靠左时会尽量左移，但仍须容纳字幕左右安全边界。
        self.assertLess(left.getpixel((5, 100))[2], centered.getpixel((5, 100))[2])
        self.assertSubtitlesInside(left)


class DuplicateFrameTests(unittest.TestCase):
    @staticmethod
    def frame(offset):
        image = Image.new("RGB", (320, 180), (40, 40, 40))
        ImageDraw.Draw(image).rectangle((offset, 40, offset + 60, 120), fill=(230, 200, 60))
        return image

    def test_identical_frames_are_rejected(self):
        frames = [self.frame(20)] * 4
        with self.assertRaises(SystemExit) as raised:
            MODULE.ensure_distinct_frames(frames, [1, 2, 3, 4])
        self.assertIn("静态封面", str(raised.exception))

    def test_moving_frames_pass(self):
        frames = [self.frame(20 + 50 * index) for index in range(4)]
        MODULE.ensure_distinct_frames(frames, [1, 2, 3, 4])

    def test_repeated_native_subtitle_band_is_rejected(self):
        frames = [self.frame(20 + 50 * index) for index in range(3)]
        bands = [self.frame(20), self.frame(120), self.frame(120)]
        with self.assertRaises(SystemExit) as raised:
            MODULE.ensure_distinct_frames(frames, [1, 2, 3], bands)
        self.assertIn("2.00s 与 3.00s", str(raised.exception))

    def test_cli_stops_on_static_video_unless_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            video = tmp_path / "static.mp4"
            subprocess.run(
                [
                    MODULE.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=10",
                    "-t", "3", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video),
                ],
                check=True,
                capture_output=True,
            )
            manifest = tmp_path / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"title": "static", "times": [0.5, 1.5, 2.5]}]}),
                encoding="utf-8",
            )
            command = [
                sys.executable, str(SCRIPT), "render", str(video),
                "--manifest", str(manifest), "--out-dir", str(tmp_path / "out"),
            ]
            blocked = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("静态封面", blocked.stderr)
            self.assertFalse((tmp_path / "out" / "01_static.jpg").exists())

            allowed = subprocess.run(
                [*command, "--allow-duplicate-frames"], capture_output=True, text=True
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertTrue((tmp_path / "out" / "01_static.jpg").is_file())


class HeroFrameTests(unittest.TestCase):
    def test_candidate_offsets_are_symmetric_and_nearest_first(self):
        self.assertEqual(
            MODULE.hero_candidate_offsets(1.5), [0.0, -0.75, 0.75, -1.5, 1.5]
        )
        self.assertEqual(MODULE.hero_candidate_offsets(0), [0.0])

    def test_person_and_slide_windows_are_separable(self):
        self.assertGreater(
            MODULE.skin_ratio(person_frame()), MODULE.HERO_SKIN_MIN
        )
        self.assertLess(MODULE.skin_ratio(slide_frame()), MODULE.HERO_SKIN_MIN)
        self.assertLess(MODULE.skin_ratio(varied_frame()), MODULE.HERO_SKIN_MIN)

    def test_only_plain_cards_count_as_flat(self):
        self.assertTrue(MODULE.is_flat_card(slide_frame()))
        self.assertFalse(MODULE.is_flat_card(varied_frame()))
        self.assertFalse(MODULE.is_flat_card(person_frame()))

    def test_hero_keeps_first_frame_when_it_already_has_a_person(self):
        first = person_frame()
        with mock.patch.object(MODULE, "grab_frame", return_value=person_frame()):
            frame, note = MODULE.pick_hero_frame(
                "unused", 4.13, 1.5, 0.0, 1.0, first
            )
        self.assertIs(frame, first)
        self.assertIn("画面里有人", note)

    def test_hero_moves_to_the_nearest_frame_with_a_person(self):
        first = slide_frame()

        def fake(video, when):
            return person_frame() if round(when, 2) == 5.63 else slide_frame()

        with mock.patch.object(MODULE, "grab_frame", side_effect=fake):
            frame, note = MODULE.pick_hero_frame(
                "unused", 4.13, 1.5, 0.0, 1.0, first
            )
        self.assertIsNot(frame, first)
        self.assertGreater(MODULE.skin_ratio(frame), MODULE.HERO_SKIN_MIN)
        self.assertIn("5.63s", note)
        self.assertIn("向后找了 1.50s", note)

    def test_hero_still_avoids_a_blank_card_when_nobody_is_in_frame(self):
        first = slide_frame()
        with mock.patch.object(MODULE, "grab_frame", return_value=varied_frame()):
            frame, note = MODULE.pick_hero_frame(
                "unused", 4.13, 1.5, 0.0, 1.0, first
            )
        self.assertFalse(MODULE.is_flat_card(frame))
        self.assertIn("纯色/近白画面", note)
        self.assertIn("向前找了 0.75s", note)

    def test_hero_falls_back_to_first_frame_when_nothing_qualifies(self):
        first = varied_frame()
        with mock.patch.object(MODULE, "grab_frame", return_value=varied_frame()):
            frame, note = MODULE.pick_hero_frame(
                "unused", 4.13, 1.5, 0.0, 1.0, first
            )
        self.assertIs(frame, first)
        self.assertEqual(note, "沿用首句 4.13s 的那一帧")

    def test_zero_window_keeps_first_frame_without_extra_decoding(self):
        first = slide_frame()
        with mock.patch.object(MODULE, "grab_frame") as grabby:
            frame, note = MODULE.pick_hero_frame("unused", 4.13, 0, 0.0, 1.0, first)
        self.assertIs(frame, first)
        self.assertEqual(note, "沿用首句 4.13s 的那一帧")
        grabby.assert_not_called()

    def test_a_candidate_that_fails_to_decode_is_dropped(self):
        first = slide_frame()

        def fake(video, when):
            if round(when, 2) == 3.38:
                raise SystemExit("取帧失败 @ 3.38s")
            return person_frame() if round(when, 2) == 5.63 else slide_frame()

        with mock.patch.object(MODULE, "grab_frame", side_effect=fake):
            frame, note = MODULE.pick_hero_frame(
                "unused", 4.13, 1.5, 0.0, 1.0, first
            )
        self.assertGreater(MODULE.skin_ratio(frame), MODULE.HERO_SKIN_MIN)
        self.assertIn("5.63s", note)

    def render(self, hero_search, tmp, lines=None):
        lines = lines or [{"t": 4.13, "text": "First"}, {"t": 8.0, "text": "Second"}]

        def fake(video, when):
            return person_frame((1280, 720)) if when >= 5.0 else slide_frame((1280, 720))

        with mock.patch.object(MODULE, "grab_frame", side_effect=fake), mock.patch.object(
            MODULE, "draw_scripted_subtitle"
        ):
            out = Path(tmp) / f"hero-{hero_search}.jpg"
            MODULE.scripted_render_one(
                "unused", lines, out, (3, 4), None, 0.88, None, None, None,
                hero_search=hero_search,
            )
        with Image.open(out) as rendered:
            top = round(rendered.height * 0.8)
            return rendered.crop((0, 0, rendered.width, top))

    def test_scripted_render_swaps_a_slide_hero_for_a_person_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            hero = self.render(None, tmp)
        # 固定画布下默认就会选帧：首句是幻灯片，+1.5s 有人，主画面应当是那个人的画面。
        self.assertGreater(MODULE.skin_ratio(hero), MODULE.HERO_SKIN_MIN)

    def test_scripted_render_keeps_the_slide_hero_when_search_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            hero = self.render(0, tmp)
        # 反向验证：关掉选帧后主画面必须还是那张白底幻灯片。
        ratio, _ = MODULE.frame_stats(hero)
        self.assertGreaterEqual(ratio, MODULE.HERO_WHITE_MAX)
        self.assertLess(MODULE.skin_ratio(hero), MODULE.HERO_SKIN_MIN)


class StripBandTests(unittest.TestCase):
    # 1440x1920 画布、7 行字幕时，每条字幕带在 854x480 源画面上的高度是 144 像素。
    BAND = 144

    def test_row_profile_is_far_shorter_than_the_frame(self):
        frame = video_text_frame()
        profile = MODULE.row_edge_energy(frame)
        # 160 宽缩略图 → 90 行，和 480 像素行不是一回事：带窗口必须换算。
        self.assertEqual(len(profile), 90)
        self.assertNotEqual(len(profile), frame.height)
        self.assertGreater(max(profile), 0.4)
        # 第 0 行会被 FIND_EDGES 当成画面边界，取画面内部的干净行做对照。
        self.assertLess(profile[10], 0.05)

    def test_band_window_maps_pixel_rows_into_profile_rows(self):
        self.assertEqual(MODULE.band_window(0.96, 480, 144, 90), (63, 27))
        self.assertEqual(MODULE.band_window(0.50, 480, 144, 90), (32, 27))
        # 轮廓行数少于带的行数时，至少保留一行，不能切片成空。
        self.assertEqual(MODULE.band_window(0.96, 480, 144, 6), (4, 2))
        self.assertEqual(MODULE.band_window(0.50, 480, 144, 480), (168, 144))

    def test_band_window_never_leaves_the_profile(self):
        for rows in (6, 45, 90, 481):
            for center in (0.0, 0.1, 0.37, 0.5, 0.9, 1.0):
                top, span = MODULE.band_window(center, 480, 144, rows)
                with self.subTest(rows=rows, center=center):
                    self.assertGreaterEqual(span, 1)
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(top + span, rows)

    def test_energy_tracks_the_painted_text_band(self):
        middle = video_text_frame(center=0.5)
        profile = MODULE.row_edge_energy(middle)
        on_text = MODULE.band_energy([profile], middle.height, self.BAND, 0.50)
        off_text = MODULE.band_energy([profile], middle.height, self.BAND, 0.20)
        self.assertGreater(on_text, MODULE.STRIP_BAND_TEXT_FLOOR)
        self.assertLess(off_text, MODULE.STRIP_BAND_TEXT_FLOOR / 4)
        # 同一套代码在「字在底部」的画面上必须把底部判得更脏，不能只会认中间。
        bottom = video_text_frame(center=0.96)
        bottom_profile = MODULE.row_edge_energy(bottom)
        self.assertGreater(
            MODULE.band_energy([bottom_profile], bottom.height, self.BAND, 0.96),
            MODULE.band_energy([bottom_profile], bottom.height, self.BAND, 0.30),
        )

    def test_clean_requested_position_is_kept(self):
        center, note = MODULE.choose_strip_center([clean_video_frame()], self.BAND, 0.96)
        self.assertEqual(center, 0.96)
        self.assertIsNone(note)

    def test_no_frames_keeps_the_requested_position(self):
        self.assertEqual(MODULE.choose_strip_center([], self.BAND, 0.96), (0.96, None))

    def test_band_moves_off_the_painted_text(self):
        frame = video_text_frame(center=0.96)
        center, note = MODULE.choose_strip_center([frame], self.BAND, 0.96)
        profile = MODULE.row_edge_energy(frame)
        before = MODULE.band_energy([profile], frame.height, self.BAND, 0.96)
        after = MODULE.band_energy([profile], frame.height, self.BAND, center)
        self.assertNotEqual(center, 0.96)
        self.assertIn("挪到", note)
        self.assertIn(f"{before:.3f}", note)
        self.assertLess(after, before * MODULE.STRIP_BAND_GAIN)
        self.assertLess(after, MODULE.STRIP_BAND_TEXT_FLOOR)
        self.assertGreaterEqual(center, MODULE.STRIP_BAND_MIN)
        self.assertLessEqual(center, MODULE.STRIP_BAND_MAX)

    def test_uniform_texture_is_not_worth_moving(self):
        frame = striped_frame()
        profile = MODULE.row_edge_energy(frame)
        # 前提：这个画面确实脏过地板，否则这里测到的只是「本来就干净」。
        self.assertGreater(
            MODULE.band_energy([profile], frame.height, self.BAND, 0.50),
            MODULE.STRIP_BAND_TEXT_FLOOR,
        )
        center, note = MODULE.choose_strip_center([frame], self.BAND, 0.50)
        self.assertEqual(center, 0.50)
        self.assertIsNone(note)

    def render(self, tmp, name, floor=None):
        """渲染一张图，只留下字幕条区域；返回 (逐行边缘强度均值, 打印内容)。"""
        lines = [
            {"t": 1.0, "text": "First"},
            {"t": 2.0, "text": "Second"},
            {"t": 3.0, "text": "Third"},
        ]
        frame = video_text_frame((1280, 720), center=0.90, thickness=0.20)
        out = Path(tmp) / f"{name}.jpg"
        log = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "grab_frame", return_value=frame))
            stack.enter_context(mock.patch.object(MODULE, "draw_scripted_subtitle"))
            if floor is not None:
                stack.enter_context(
                    mock.patch.object(MODULE, "STRIP_BAND_TEXT_FLOOR", floor)
                )
            with contextlib.redirect_stdout(log):
                MODULE.scripted_render_one(
                    "unused", lines, out, (3, 4), 1440, 0.90, None, None, None,
                    hero_search=0,
                )
        with Image.open(out) as rendered:
            # 3 句 → 主画面 82%，下面两条就是纯源画面像素（字幕由 mock 关掉了）。
            area = rendered.crop(
                (0, round(rendered.height * 0.82), rendered.width, rendered.height)
            )
        profile = MODULE.row_edge_energy(area)
        return sum(profile) / len(profile), log.getvalue()

    def test_scripted_render_moves_the_strips_off_the_video_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            energy, log = self.render(tmp, "avoid")
        self.assertLess(energy, MODULE.STRIP_BAND_TEXT_FLOOR)
        self.assertIn("字幕带: 带位从 0.90 挪到", log)

    def test_scripted_render_keeps_the_text_when_avoidance_is_off(self):
        # 反向验证：把地板抬到永远不满足＝退回改之前的行为，
        # 字幕条必须重新压在画面自带的文字上，且不再打印带位说明。
        with tempfile.TemporaryDirectory() as tmp:
            energy, log = self.render(tmp, "keep", floor=99)
        self.assertGreater(energy, MODULE.STRIP_BAND_TEXT_FLOOR)
        self.assertNotIn("字幕带:", log)


class CliIntegrationTests(unittest.TestCase):
    def test_sample_band_and_render_with_synthetic_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            video = tmp_path / "synthetic.mp4"
            subprocess.run(
                [
                    MODULE.FFMPEG,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=10",
                    "-t",
                    "3",
                    "-c:v",
                    "mpeg4",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
                capture_output=True,
            )

            sample = tmp_path / "candidate.jpg"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "sample",
                    str(video),
                    "--start",
                    "0.5",
                    "--end",
                    "2.5",
                    "--interval",
                    "1",
                    "--out",
                    str(sample),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(sample.is_file())

            default_sample = tmp_path / "default-candidate.jpg"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "sample",
                    str(video),
                    "--out",
                    str(default_sample),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(default_sample.is_file())

            focused_sample = tmp_path / "focused-candidate.jpg"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "sample",
                    str(video),
                    "-t",
                    "1",
                    "-t",
                    "2",
                    "--around",
                    "0.2",
                    "--out",
                    str(focused_sample),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(focused_sample.is_file())

            band = tmp_path / "band.jpg"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "band",
                    str(video),
                    "-t",
                    "1",
                    "--out",
                    str(band),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(band.is_file())

            manifest = tmp_path / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {"images": [{"title": "合成测试", "times": [0.5, 1, 1.5, 2, 2.5]}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            out_dir = tmp_path / "output"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "render",
                    str(video),
                    "--manifest",
                    str(manifest),
                    "--out-dir",
                    str(out_dir),
                    "--width",
                    "300",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            output = out_dir / "01_合成测试.jpg"
            self.assertTrue(output.is_file())
            self.assertTrue((out_dir / "final_contact_sheet.jpg").is_file())
            self.assertTrue((out_dir / "原生字幕时间点.json").is_file())
            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (300, 284))

            script = tmp_path / "script.json"
            script.write_text(
                json.dumps(
                    {
                        "lines": [
                            {"t": 0.5, "text": "First point"},
                            {"t": 1.0, "text": "Second point"},
                            {"t": 1.5, "text": "Third point"},
                            {"t": 2.0, "text": "Fourth point"},
                            {"t": 2.5, "text": "Fifth point"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scripted_output = tmp_path / "scripted.jpg"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "render-script",
                    str(video),
                    "--script",
                    str(script),
                    "--out",
                    str(scripted_output),
                    "--width",
                    "300",
                    "--font-size",
                    "18",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            with Image.open(scripted_output) as rendered:
                self.assertEqual(rendered.size, (300, 400))

            for command, inputs, expected in [
                ("render", ["--manifest", str(manifest), "--out-dir", str(tmp_path / "natural")], (640, 605)),
                ("render-script", ["--script", str(script), "--out", str(tmp_path / "natural-script.jpg"),
                                   "--frame-bottom", "0.72"], (640, 403)),
            ]:
                subprocess.run(
                    [sys.executable, str(SCRIPT), command, str(video), *inputs, "--layout", "natural"],
                    check=True, capture_output=True, text=True,
                )
                natural_path = (tmp_path / "natural" / "01_合成测试.jpg" if command == "render"
                                else tmp_path / "natural-script.jpg")
                with Image.open(natural_path) as rendered:
                    self.assertEqual(rendered.size, expected)

            fixed_dir = tmp_path / "explicit-fixed"
            subprocess.run(
                [sys.executable, str(SCRIPT), "render", str(video),
                 "--manifest", str(manifest), "--out-dir", str(fixed_dir),
                 "--aspect", "3:4", "--width", "300"],
                check=True, capture_output=True, text=True,
            )
            with Image.open(fixed_dir / "01_合成测试.jpg") as rendered:
                self.assertEqual(rendered.size, (300, 400))

            repeated = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "render",
                    str(video),
                    "--manifest",
                    str(manifest),
                    "--out-dir",
                    str(out_dir),
                    "--width",
                    "300",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("--overwrite", repeated.stderr)

    def test_low_frame_rate_tail_does_not_abort_sample(self):
        """1 fps 素材的最后一帧 PTS 早于 duration，sample 不能因此整段中止。

        回归用例，只断言可观察行为（退出码、产物、输出的时间点），不依赖
        video_metadata 的返回签名：修复前尾部留白固定为 0.5 秒，最后一个采样点
        落在不存在的帧上，grab_frame 抛错并让整个 sample 中止且不产出总览图。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            video = tmp_path / "slow.mp4"
            subprocess.run(
                [
                    MODULE.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=1",
                    "-t", "30", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video),
                ],
                check=True, capture_output=True,
            )

            sample = tmp_path / "candidate.jpg"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "sample", str(video), "--out", str(sample)],
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(sample.is_file(), completed.stderr)

            # 1 fps / 30 秒素材的真实末帧 PTS 是 29s，采样点不能越过它
            reported = re.search(r"时间点:\s*(.+)", completed.stdout)
            self.assertIsNotNone(reported, completed.stdout)
            last = float(reported.group(1).split(",")[-1])
            self.assertLessEqual(last, 29.0 + 1e-6)

    def test_decodable_margin_keeps_normal_frame_rates_unchanged(self):
        """24/25/30 fps 素材沿用原来的 0.5 秒尾部留白。"""
        for fps in (24, 25, 30, 60):
            self.assertAlmostEqual(MODULE.decodable_margin(120, fps), 0.5)
        # 帧率未知时退回原行为
        self.assertAlmostEqual(MODULE.decodable_margin(120, 0.0), 0.5)
        self.assertAlmostEqual(MODULE.decodable_margin(3, 0.0), 0.3)
        # 低帧率按帧长回退
        self.assertAlmostEqual(MODULE.decodable_margin(30, 1.0), 2.0)
        # 极端低帧率不会把可请求范围压到 0
        self.assertLessEqual(MODULE.decodable_margin(10, 0.5), 5.0)


if __name__ == "__main__":
    unittest.main()
