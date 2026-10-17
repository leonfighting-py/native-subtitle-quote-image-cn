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
# 只选句，产出候选清单与时间点文件
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py pick \
  video.mp4.transcript.json --out out/ --top 5

# 一条命令选句 + 出图（视频与转写同名同目录时自动配对）
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py pick \
  video.mp4.transcript.json --out out/ --top 5 \
  --render --render-mode script --aspect 9:16 --width 1080 \
  --frame-bottom 0.55 --band-center 0.5

# 整库批处理
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py batch \
  transcripts/ --out out/ --top 5 --render --render-mode native \
  --band-top 0.76 --band-bottom 0.90
```

产出 `审阅表.md`（人在几十条里做取舍）、`candidates.json`、`native_manifest.json`、
`scripts/NN_标题.json` 与 `渲染脚本字幕.sh`；加 `--render` 另有 `images/`；
批处理额外产出跨视频的 `全局精选.md`。

实测规模与耗时：

```text
覆盖视频     59 期（568 分钟）
候选窗口     50,560
入选         291
耗时         约 4 秒（不含出图）
```

设计上刻意**不让模型直接读整篇文字稿**——那是几十万字、批处理跑不动，也失去逐句回溯能力。
脚本先用确定性规则清洗、切窗、打分、去重，把工作量压到 174:1，模型只在最后判断语义。

实现过程中查清并处理的九个素材特性：

| 发现 | 实测数据 | 处理方式 |
|---|---|---|
| 转写**没有标点** | 56/59 期零标点 | 不靠标点切句 |
| 转写段**几乎没有停顿** | 段间停顿中位 0.00s，>0.5s 只占 1/648 | 不靠停顿切句；原始段本身就是合格字幕行（中位 14 字 / 2.52s） |
| 末段**幻觉超出总时长** | 11/59 期，如 `[217.70→247.68] 感谢观看` 而总时长为 217.73s | 三条规则兜底清洗；否则会撞上渲染器的硬校验 |
| 转写**声明的时长本身可能是错的** | 实测踩到声明 20s / 视频 3s | 一律以视频实测时长校正；否则时间点越界、一张图都出不来 |
| 序数词**冒充数字** | 「第一」「第二个」无处不在 | 信号词只认真正的数量；否则过半候选被误标、排序尽毁 |
| YouTube 字幕是**滚动缓冲**格式 | 315 个 cue 里 113 个带 `<c>`/词级时间标签、一半是 10 毫秒翻转 cue | 剥行内标签 + 只取新增行 + 裁相邻重叠；改完 VTT 与 SRT 两条路径产出逐字相同 |
| 字幕**不能按空行切块** | 滚动 cue 正文里夹着只含空白的行 | 改为逐行扫描；否则首句会被劈丢（实测 YouTube 第一条永远丢失） |
| 一次 `--sub-langs "zh.*,en.*"` 会落下**同一支视频的多条轨** | 实测一个英文视频落了 `en` / `en-orig` / `en-en`，其中 `en-en` 与 `en` 逐字节相同 | 消费端按「文件名去掉语言码」分组，只留最贴近原声的那条；否则同一支视频在全局精选里重复三遍 |
| 字幕是按句切开的，**拼回整段要还原词间空格** | 英语会粘连成 `yourconversations`，并一路带进候选标题与 `全局精选.md` 的原文 | 按书写系统逐对判断：相邻有中日韩字符就不加空格，否则加空格 |

后七项都补了回归测试并做反向验证（13 条断言在改动前的代码上全部失败）。竖屏素材在脚本模式下的**字幕重影**问题也在文档中给出实测解法
（`--frame-bottom 0.55 --band-center 0.5`）。

时长校正还暴露了一个只在 CI 上才现形的问题：`probe_duration` 起初只写裸 `"ffmpeg"` 命令，
而 `requirements.txt` 走的是 `imageio-ffmpeg`。开发机上装了系统 ffmpeg 所以一直是绿的，
CI 上没有系统 ffmpeg，时长校正**静默失效**。现已与 `native_subtitle_stitch.py` 统一解析顺序
（先 `imageio-ffmpeg`，再 PATH），并且在「找到视频却读不出时长」时明确标注
`transcript-no-ffmpeg`，不再静默当成转写时长可信。

### 5. 打通「链接 → 文字稿 → 选句 → 出图」的 URL 链路

上游的 URL 入口止于「拿到文字时间轴」，选句仍要人工。现在整条链路都补齐了，
并且**按「先取字幕、再选句、最后才下视频」的三段式组织**：

```bash
# 1. 整个频道/播放列表只取字幕（不下载视频，几分钟）
python3 skills/native-subtitle-quote-image/scripts/fetch_transcripts.py \
  "https://www.youtube.com/@频道/videos" --out source/ --limit 50

# 2. 选句，产出审阅表与全局精选
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py \
  batch source/ --out work/ --top 5

# 3. 人看过审阅表后，只给要出图的那几条补视频
python3 skills/native-subtitle-quote-image/scripts/fetch_transcripts.py \
  --ids "8S0FDjFBj8o,dQw4w9WgXcQ" --out source/ --videos

# 4. 出图（视频已在手，批处理自动配对）
python3 skills/native-subtitle-quote-image/scripts/select_quotes.py \
  batch source/ --out work/ --render --render-mode script --aspect 3:4 --width 1080
```

为什么要分成三段：**挑句子只需要文字稿**。一个 200 条的频道全量下载是几十 GB 和几小时，
只取字幕是几分钟。反过来做，绝大多数带宽会花在永远用不上的素材上。
`fetch_transcripts.py` 内置增量清单（`.fetched.txt`），重跑只补新更的条目。

实测（TED 演讲 `8S0FDjFBj8o`，355 秒）：146 行 → 426 个候选窗口 → 5 组 → 5 张图，
`--sub-format srt` 和 `vtt` **两条路径的最终选句逐字相同**。

顺带说明三件容易踩的事：

- **`--sub-format` 优先 `srt`。**YouTube 的自动字幕原生只有 VTT，而 VTT 是滚动缓冲格式，
  每条 cue 都把上一行重抄一遍。srt 等于让 yt-dlp 先替你归一化。
- **`--lang` 不要写通配符。**`zh.*,en.*` 会把自动翻译轨（`zh-Hans-en`、`en-en`）一起拉下来，
  一个中文频道能落下几十个同义文件。脚本默认用具体标签。
  消费端也会兜底：同一支视频的多条语言轨只处理一条（优先 `-orig` 原声轨）。
- **非中文素材的「发布价值」恒为 0。**信号词表是按中文写的，实测那份英文演讲
  426 个候选窗口的发布价值全是 0.0，实际只按结构三维排序。
  **英文素材排出来的是「结构完整的段落」，不是「值得传播的金句」**，
  请把 `--top` 调大、多留人工过筛。
- **画面下缘有台标/角标时要挪字幕带。**TED 这类视频在底部烧了字标，
  默认 `--band-center 0.88` 会把它透出来形成重影，实测
  `--frame-bottom 0.82 --band-center 0.96` 可压掉。

### 6. 这条链路**不包含**发布

从「拿到链接」到「产出图片」已经跑通，但**送到社交平台这一步在本仓库里完全不存在**——
流水线止于 `images/` 目录。这是有意的设计边界：各平台开放接口都需要企业资质或应用审核，
还牵扯定时任务与平台对自动化发布的服务条款约束。仓库存放「出图工具」这一个职责，
不预置、也不假装支持发布。需要发布时另起一层，不要塞进这个 skill。

### 7. 接 YouTube 自带译文轨：一套原文、一套译文

英文版发 TikTok、中文版发国内平台，同一条素材要出两套图。译文不自己翻，直接用 YouTube
已经存在的自动翻译轨：

```bash
python3 "<SKILL_DIR>/scripts/fetch_transcripts.py" "https://www.youtube.com/watch?v=..." \
  --translate zh-Hans --sub-format srt --out-dir sub
```

实测（TED `8S0FDjFBj8o`）：YouTube 给出 **156 条 `<目标语>-<源语>` 翻译轨**（如 `zh-Hans-en`），
免费、不需要 key、不需要登录。三件容易踩的事：

- **`--sub-langs` 是整串锚定的。**yt-dlp 用 `fullmatch` 匹配，写 `zh-Hans` 匹配不上文件里的
  `zh-Hans-en`，必须写完整标签。
- **翻译轨必须按时间范围对齐，不能按下标对齐。**自动译文轨 134/134 条时间戳与原文一致；
  人工译文轨虽然 358/358 条时间戳相同，正文却有偏移，还夹着 26 条空正文 cue，
  解析时丢弃空 cue 就会让下标错位（358 → 332）。按时间区间取，两种轨都安全。
- **429 是暂时的，退避重试有效。**但要**看输出文本、不要看退出码**：`--ignore-errors` 会让
  yt-dlp 跳过 `_download_retcode = 1`，限流表现为「退出码 0 + 一行 ERROR」，只看退出码会把
  限流当成成功。`fetch_transcripts.py` 已按输出文本判断，并按 `--retry-delays 0,45,120,300` 退避。

原文一套、译文一套用同一组时间点和同一套版式，只换 `text`，分别落到 `images/` 与 `images_zh/`。

### 8. 脚本模式主画面不再死取第一帧

`render-script` 原来固定用第一句时间点那一帧当主画面。第一句正好落在图表或幻灯片上时，
主画面就是一张写着大字的白色幻灯片，发布价值基本为零。现在改为：

- 第一句那一帧里本来就有人 → 原样保留；
- 否则在**第一句前后 `--hero-search` 秒（默认 1.5 秒）**内就近换成有人的一帧；
- 窗口内都没人、第一句又是纯色 / 近白卡片 → 取最近的「不是卡片」的一帧；
- 都不满足 → 回退到第一句那一帧。

「有人」用肤色像素占比判断，「纯色卡片」用近白占比 / 量化颜色数判断，**两套阈值都来自实测**：
TED 类访谈里人像镜头肤色占比 3.6%–5.3%、近白占比 0.000%、颜色数 35–41；白底幻灯片肤色占比
0.000%、近白占比 44%–58%、颜色数 11。不需要模型、不需要联网，也不读内容语义。

实测同一份 40 秒片段：首句 4.13s 落在 FUELS 幻灯片上的那张图，主画面自动换到 4.88s 的演讲者帧；
首句 20.38s 本身有人的那张，主画面原样保留。选帧只动主画面，字幕条仍然取各自时间点的原始画面；
`--hero-search 0` 关闭；`--layout natural` 不接受该选项（原比例布局的帧自带烧录字幕，必须与时间点对齐）。

### 9. 字幕带不再压在画面自带的字上

`--band-center` 原来是个死参数：定在 0.96，六条字幕带就都从画面 96% 处横切。哪一句正好落在
幻灯片上，那条带子就把幻灯片的大字一起切进来——字压字，前半段几乎读不出来，看上去像「又冒出来
一段别的字幕」。

现在 `--band-center` 只是**起点**。出图前会按批次算逐行边缘强度（每条字幕帧缩到 160 像素宽、
`FIND_EDGES` 后按行取均值），在 `0.10–0.90` 之间每 `0.02` 试一遍，**整张图统一挑一条最干净的横带**：

- 请求位边缘强度 `< 0.04` → 本来就干净，原样沿用；
- 只有请求位压着东西、且存在比它**干净 40% 以上**的带位时才挪，并打印
  `字幕带: 带位从 0.96 挪到 0.46（原位置压在画面自带的文字/图形上，边缘强度 0.103 → 0.015）`。

整张图统一一条带，不逐条各挑各的（各条带位不同会让版面上下跳动）。

实测（854×480 片段，带高占画面 21.6%）：压在幻灯片自带的 `STEEL` 大字上 `0.1031`、最干净带位
`0.0152`、人像镜头的腿 `0.0190`——所以干净素材不会触发挪动，同一份片段的另一张图六条带子原地不动。

它只保证**不压画面自带的字**，保证不了「有干净背景」：整帧就是幻灯片时，实测任何带位的亮度离散度
都是 `0.99`（横切必然同时切到白底和黑块），那几条仍会是幻灯片的一块。`--layout natural` 不适用
（原比例布局的帧自带烧录字幕，带位必须与时间点严格对齐）。

### 版本号

`VERSION` 标为 `2.3.0-cn.7`。`check_update.py` 的更新检查仍指向上游仓库，
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

211 个用例，覆盖布局几何、字幕带识别、字幕带避让（逐行边缘轮廓、像素行↔轮廓行换算、压字帧与
干净帧的带位决策）、主画面选帧（肤色/近白阈值的可分性、四级顺序）、重复帧拦截、清洗去重、
打分信号、时长校正与 FFmpeg 解析、
SRT/VTT 解析（行内标签、滚动字幕、相邻重叠、cue 抬头）、语言轨归并与视频配对、
跨语言拼接的空格、频道批量取字幕（用假 yt-dlp 跑端到端）、
两个字幕模式的端到端出图，以及各处修复的回归。

CI（[`.github/workflows/tests.yml`](.github/workflows/tests.yml)）在每次 push 与 PR 时
于 Python 3.10 和 3.13 上跑全套测试，并检查 CLI 可用性。
缺少中日韩字体的环境下，脚本字幕的渲染用例会明确跳过而不是误报失败。

## 边界与未验证项

- **不包含发布环节。**流水线止于 `images/`，没有任何推送到社交平台的能力。
- 抖音的不可用结论基于 yt-dlp 特定版本。上游实现签名挑战后即可恢复，处理前先更新 yt-dlp 再判断。
- 12 个平台里只有 **B站** 和 **抖音** 做过完整实测，其余（微博、小红书、AcFun、优酷、爱奇艺、西瓜、头条、喜马拉雅、虎牙、斗鱼）只确认了 extractor 存在。
- YouTube 链路只在 **1 支视频**（TED `8S0FDjFBj8o`）上端到端验证过，包括自动字幕下载、VTT/SRT 解析与出图；
  多语言字幕轨（`zh-Hans-en` 一类自动翻译轨）的 429 已在同一支视频上实测为**暂时性限流、退避重试可恢复**，
  但样本只有 1 次触发，不足以给出稳定的重试次数建议。
- `fetch_transcripts.py` 的**真实站点**行为只在一个公开播放列表（限 2 条）上验过一次；
  CI 里跑的是**假的 yt-dlp**（覆盖播放列表枚举、多语言轨落盘、`--playlist-end`、无字幕条目上报），
  不联网。频道私密性、Cookie 授权、403/429 的实测仍以 [yt-dlp-and-transcripts.md](skills/native-subtitle-quote-image/references/yt-dlp-and-transcripts.md) 的手工流程为准。
- 语言轨归并按**文件名形状**判断（末段是否像语言码），不是读字幕内容里的语言标签。
  文件名被人工改过时可能归错组。
- 竖屏文档里的字号、区间数值来自 1080×1920 合成素材的实测，真实平台素材的字幕位置差异较大，必须先跑 `band` 预览。
- `select_quotes.py` 的打分是**形式特征**（信号词、词面重叠、句数、跨度），认得出「像金句」的结构，
  认不出反讽、上下文依赖和圈内梗。它把候选压到几十条，**不判断语义是否真的成立**——
  `审阅表.md` 末尾明确要求人工确认，交付时也不得把自动排序当作用户已确认。
- **打分信号词只有中文**，非中文素材的「发布价值」恒为 0，实际只按结构三维排序。
- 无视频时「画面可用性」标为 `unknown`，所以选出的时间点可能落在人物闭眼或转场帧上，必须 `sample` 回看。
  脚本固定布局的主画面另有自动兜底（见上文第 8 节），但它只保证「有人在画面里」，
  不判断表情、口型、闭眼和转场，字幕条也不受保护。
- 实测数据（59 期 / 50,560 个候选）来自单一语料：抖音知识类口播视频、`faster-whisper large-v3-turbo` 转写。
  换语料（对话类、访谈类、有标点的转写）需要重新校准阈值。
- 本仓库不含上游的示例图库（`examples/`）、多语言 README 和横幅生成脚本；
  仓库校验脚本 `scripts/validate_repo.py` 与横幅脚本 `scripts/render_banners.py` 因绑定上游的图库与资产结构，未纳入本分支。

## 许可

MIT。原作品版权归 程意 所有，本分支的修改部分版权归 leonfighting-py。
详见 [LICENSE](LICENSE)。

上游项目：[chengyi-ai/native-subtitle-quote-image](https://github.com/chengyi-ai/native-subtitle-quote-image)
