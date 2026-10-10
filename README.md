# native-subtitle-quote-image-cn

[![Tests](https://github.com/leonfighting-py/native-subtitle-quote-image-cn/actions/workflows/tests.yml/badge.svg)](https://github.com/leonfighting-py/native-subtitle-quote-image-cn/actions/workflows/tests.yml)

把真实视频帧做成保留原字幕或绘制台词的长图。本仓库是
[chengyi-ai/native-subtitle-quote-image](https://github.com/chengyi-ai/native-subtitle-quote-image) **v2.3.0 的增强分支**，
补上中国大陆视频平台、竖屏素材、批量自动选句这三块上游未覆盖的场景，并修掉一个取帧边界 bug。

上游的完整能力（两种字幕模式、紧凑排版、逐张质检）全部保留，本分支只做增量。

## 相对上游改了什么

### 1. 新增 `references/cn-platforms.md`：中国大陆平台取源

上游的 URL 文档 [`yt-dlp-and-transcripts.md`](skills/native-subtitle-quote-image/references/yt-dlp-and-transcripts.md)
通篇以 YouTube 为前提，国内平台一条没写。新增文档覆盖：

- **12 个国内平台的 extractor 路由表**，并明确标注哪些做过实测、哪些没有；
- **B站完整可用流程**：元数据、`-F` 选格式、视频音频分离流的下载与合并、免费画质上限（1080P，4K 需大会员）；
- **B站 `danmaku` 陷阱**：`--list-subs` 会把弹幕列成一条“字幕”。它是弹幕，不是字幕轨，不能当文字稿、不能画进图；
- **B站 CC 字幕需要登录态**：由 `need_login_subtitle` 控制，须先取得用户授权再读浏览器 Cookie；
- **抖音当前不可用，且不是 Cookie 问题**：源码级结论——接口返回空 body，`DouyinIE._real_extract` 里写着
  `# TODO: Run verification challenge code to generate signature cookies`。文档列清了已经验证过、不必再试的做法，
  并给出正确绕法（本地下载后走本地模式）；
- **短链必须直连**：环境预置的 HTTP 代理会让 `v.douyin.com` 短链报 TLS 错误。

### 2. 新增 `references/portrait-and-vertical.md`：竖屏与方形素材

上游默认字幕带 `--band-top 0.78 --band-bottom 0.96` 与 `--band-center 0.88` 都是横屏假设，
套在竖屏素材上会“看起来跑通了、其实一个字都没圈到”。新增文档给出实测数据：

| 场景 | 实测结果 |
|---|---|
| 1080×1920 竖屏，字幕在 0.58–0.70，用默认 `0.78–0.96` | 红线落在 y=1497–1843，**完全错过字幕** |
| 同素材改用 `0.55–0.75` | y=1056–1440，完整包住字幕 |
| 同素材用 `--aspect 3:4 --width 1440` | 输出 1440×1920，但主画面只取源画面纵向中段 1180px，**裁掉 38.5%**，并可能把源字幕裁进主图造成重字 |
| 同素材用 `--aspect 9:16 --width 1080` | 1080×1920，无裁切 |
| 竖屏原生字幕 + 默认 natural 布局 | 1080×2208 |

文档还给出源画面自带字幕时用 `--frame-bottom` 避开重字的写法，以及竖屏的字幕条密度取舍。

### 3. 修复 `sample` 在低帧率素材上整段中止的 bug

**问题**：帧率极低的素材（延时、幻灯片式、合成视频）最后一帧的 PTS 比视频 `duration` 早整整一个帧间隔。
原实现把尾部留白写死为 0.5 秒，最后一个采样点会落在不存在的帧上，`grab_frame` 抛错并让**整个 `sample` 中止且不产出总览图**。

复现：1 fps / 30 秒素材 → `取帧失败 @ 29.50s`，无输出文件。

**修复**：`video_metadata()` 现在解析真实帧率，新增 `decodable_margin()` 按帧长至少回退两个帧间隔（上限为 duration 的一半）。
24/25/30/60 fps 素材的留白仍是原来的 0.5 秒，**正常视频行为完全不变**。

```text
demo.mp4      1280x720  dur=  30.00 fps=1.0   margin=2.0000
demo25.mp4    1280x720  dur=  30.00 fps=25.0  margin=0.5000
bili-demo.mp4 1280x720  dur= 130.82 fps=30.0  margin=0.5000
```

**测试**：新增 2 个用例（行为断言 + 留白回归），本分支测试数 37 → 39，全部通过。
新增用例做过反向验证——放回未修复的代码时会以 `AssertionError: 1 != 0 : 取帧失败 @ 29.50s` 失败。
（上游完整测试为 43 个，另含 6 个横幅生成测试，本分支未纳入。）

### 4. 新增 `scripts/select_quotes.py` + `references/auto-quote-selection.md`：批量自动选句

上游的「选题」是手工步骤，要求人先看完视频才能指出哪几句精彩。新增脚本把这一步自动化：
**输入带时间戳的文字稿，输出排好序的候选清单和可直接渲染的时间点文件。**

```bash
# 单期
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py pick \
  video.mp4.transcript.json --out out/ --top 5

# 整库
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py batch \
  transcripts/ --out out/ --top 5
```

产出 `审阅表.md`（人在几十条里做取舍）、`candidates.json`、`native_manifest.json`、
`scripts/NN_标题.json` 与 `渲染脚本字幕.sh`；批处理额外产出跨视频的 `全局精选.md`。

实测规模与耗时：

```text
覆盖视频     59 期（568 分钟）
候选窗口     50,557
入选         291
耗时         约 4 秒
```

设计上刻意**不让模型直接读整篇文字稿**——那是几十万字、批处理跑不动，也失去逐句回溯能力。
脚本先用确定性规则清洗、切窗、打分、去重，把工作量压到 174:1，模型只在最后判断语义。

实现过程中查清并处理的四个中文长视频特性：

| 发现 | 实测数据 | 处理方式 |
|---|---|---|
| 转写**没有标点** | 56/59 期零标点 | 不靠标点切句 |
| 转写段**几乎没有停顿** | 段间停顿中位 0.00s，>0.5s 只占 1/648 | 不靠停顿切句；原始段本身就是合格字幕行（中位 14 字 / 2.52s） |
| 末段**幻觉超出总时长** | 11/59 期，如 `[217.70→247.68] 感谢观看` 而总时长为 217.73s | 三条规则兜底清洗；否则会撞上渲染器的硬校验 |
| 序数词**冒充数字** | 「第一」「第二个」无处不在 | 信号词只认真正的数量；否则过半候选被误标、排序尽毁 |

末尾两项都补了回归测试并做反向验证。竖屏素材在脚本模式下的**字幕重影**问题也在文档中给出
实测解法（`--frame-bottom 0.55 --band-center 0.5`）。

### 版本号

`VERSION` 标为 `2.3.0-cn.2`。`check_update.py` 的更新检查仍指向上游仓库，
这是有意保留的：用它来发现上游新版本，便于合并。

## 安装

```bash
git clone https://github.com/leonfighting-py/native-subtitle-quote-image-cn.git
cp -R native-subtitle-quote-image-cn/skills/native-subtitle-quote-image ~/.workbuddy/skills/
```

Claude Code 用 `~/.claude/skills/`，Codex 用 `~/.codex/skills/`，路径按各自文档调整。

依赖：

```bash
python3 -m pip install -r skills/native-subtitle-quote-image/requirements.txt
python3 skills/native-subtitle-quote-image/scripts/check_environment.py
```

需要处理在线链接时另装 `yt-dlp`：

```bash
python3 -m pip install -U "yt-dlp[default]"
python3 skills/native-subtitle-quote-image/scripts/check_environment.py --url-mode
```

> 建议装进虚拟环境。若 `python3 -m pip install` 装到了别的解释器，
> 而运行时用的 `python3` 里没有 Pillow，脚本会直接报 `ModuleNotFoundError`。

## 快速开始

```text
使用 native-subtitle-quote-image，把这个视频做成原生字幕拼图。
```

两种模式、命令行用法、质检要求与上游一致，见
[SKILL.md](skills/native-subtitle-quote-image/SKILL.md)。

## 测试

```bash
python3 -m unittest discover -s tests
```

55 个用例，覆盖布局几何、字幕带识别、重复帧拦截、清洗去重、打分信号、CLI 集成与两处修复的回归。

CI（[`.github/workflows/tests.yml`](.github/workflows/tests.yml)）在每次 push 与 PR 时
于 Python 3.10 和 3.13 上跑全套测试，并检查 CLI 可用性。

## 边界与未验证项

- 抖音的不可用结论基于 yt-dlp 特定版本。上游实现签名挑战后即可恢复，处理前先更新 yt-dlp 再判断。
- 12 个平台里只有 **B站** 和 **抖音** 做过完整实测，其余（微博、小红书、AcFun、优酷、爱奇艺、西瓜、头条、喜马拉雅、虎牙、斗鱼）只确认了 extractor 存在。
- 竖屏文档里的字号、区间数值来自 1080×1920 合成素材的实测，真实平台素材的字幕位置差异较大，必须先跑 `band` 预览。
- `select_quotes.py` 的打分是**形式特征**（信号词、词面重叠、句数、跨度），认得出「像金句」的结构，
  认不出反讽、上下文依赖和圈内梗。它把候选压到几十条，**不判断语义是否真的成立**——
  `审阅表.md` 末尾明确要求人工确认，交付时也不得把自动排序当作用户已确认。
- 无视频时「画面可用性」标为 `unknown`，所以选出的时间点可能落在人物闭眼或转场帧上，必须 `sample` 回看。
- 实测数据（59 期 / 50,557 个候选）来自单一语料：抖音知识类口播视频、`faster-whisper large-v3-turbo` 转写。
  换语料（对话类、访谈类、有标点的转写）需要重新校准阈值。
- 本仓库不含上游的示例图库（`examples/`）、多语言 README 和横幅生成脚本；
  仓库校验脚本 `scripts/validate_repo.py` 与横幅脚本 `scripts/render_banners.py` 因绑定上游的图库与资产结构，未纳入本分支。

## 许可

MIT。原作品版权归 程意 所有，本分支的修改部分版权归 leonfighting-py。
详见 [LICENSE](LICENSE)。

上游项目：[chengyi-ai/native-subtitle-quote-image](https://github.com/chengyi-ai/native-subtitle-quote-image)
