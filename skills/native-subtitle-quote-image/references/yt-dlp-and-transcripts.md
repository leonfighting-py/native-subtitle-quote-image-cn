# URL 获取与时间轴

当输入是 YouTube 等在线视频链接，而不是本地视频时，读取本文件。`yt-dlp` 只负责获取用户有权处理的视频、元数据和字幕时间轴；最终图片仍由 `native_subtitle_stitch.py` 从真实视频帧生成。

不要因为第一次公开请求被 YouTube 拒绝，就告诉用户“这个 Skill 只能读取本地视频”。URL 获取本来就是此 Skill 的正式入口：先尝试无 Cookie 公开访问；若命中登录或机器人验证，再走本文的 Chrome Cookie 授权流程。

## 先区分两种“字幕”

| 内容 | 用途 | 原生字幕模式 | 脚本字幕模式 |
|---|---|---|---|
| 画面内烧录字幕 | 保留原字幕 | 必须从真实视频帧保留 | 如与后期文字重叠，应换帧或换模式 |
| YouTube 字幕轨、VTT/SRT、Whisper 文字稿 | 理解内容、选题、定位时间点 | 只做索引，不能画进成品 | 经复核后可写入 `lines[].text`，必须标记为后期台词 |

有字幕轨不等于视频画面有字幕。下载后必须实际取帧检查。若用户要求原生字幕，但画面没有烧录字幕，停止并说明；只有用户同意后，才使用本 Skill 的 `render-script` 模式，不要偷偷切换。

## 官方依据

- [yt-dlp 官方 README](https://github.com/yt-dlp/yt-dlp#readme)
- [安装说明](https://github.com/yt-dlp/yt-dlp/wiki/Installation)
- [External JavaScript 指南](https://github.com/yt-dlp/yt-dlp/wiki/EJS)

yt-dlp 当前支持 CPython 3.10+ 和 PyPy 3.11+。完整 YouTube 支持强烈建议 FFmpeg、`yt-dlp-ejs` 和 JavaScript runtime。官方优先推荐 Deno，并默认启用它；Node.js 或 QuickJS 需要通过 `--js-runtimes` 显式启用。Bun 目前仍可用，但官方已将其支持标记为弃用，不应作为新环境首选。

## 环境检查

先运行 Skill 自带的只读诊断：

```bash
python3 "<SKILL_DIR>/scripts/check_environment.py" --url-mode
```

如果要绘制中日韩台词，同时检查 CJK 字体：

```bash
python3 "<SKILL_DIR>/scripts/check_environment.py" --url-mode --script-mode
```

它不会安装软件，只报告：

- Python、Pillow、FFmpeg provider 是否可用；
- `yt-dlp` 版本；
- Deno 或其他 JavaScript runtime；
- 脚本模式所需的 CJK 字体（使用 `--script-mode` 时）；
- 可选的 Whisper/语音识别能力。

缺少组件时先向用户说明，再取得安装授权。不要擅自修改系统 Python、包管理器或 shell 配置。

### 安装与更新原则

- 核心渲染依赖：`python3 -m pip install -r "<SKILL_DIR>/requirements.txt"`。
- yt-dlp 可使用官方独立可执行文件，或安装 PyPI 的 `yt-dlp[default]`；`default` extra 会带上官方推荐的 Python 依赖。
- 官方文档当前建议普通用户使用 nightly；若稳定版遇到站点解析问题，先按官方说明更新 nightly，再判断是不是命令或权限问题。
- Deno 使用官方安装方式，当前最低支持 2.3.0；若环境已有 Node.js 22+，可保留 Node，并在每条 yt-dlp 命令中加入 `--js-runtimes node`。

示例（使用 pip 的环境）：

```bash
python3 -m pip install -U "yt-dlp[default]"
```

稳定版出现站点解析错误、且用户同意更新时：

```bash
python3 -m pip install -U --pre "yt-dlp[default]"
```

不要把“安装成功”和“能解析当前链接”混为一谈；安装后至少运行：

```bash
yt-dlp --version
yt-dlp --no-playlist --skip-download --print "%(id)s | %(title)s | %(duration_string)s" "URL"
```

## 获取顺序

### 1. 只读检查元数据

```bash
yt-dlp --no-playlist --skip-download \
  --print "%(id)s | %(title)s | %(channel)s | %(duration_string)s" \
  "URL"
```

使用 Node.js 时：

```bash
yt-dlp --js-runtimes node --no-playlist --skip-download \
  --print "%(id)s | %(title)s | %(channel)s | %(duration_string)s" \
  "URL"
```

先确认链接指向单个目标视频。默认添加 `--no-playlist`，避免一个播放列表被意外整批下载。

### 2. 列出字幕轨

```bash
yt-dlp --no-playlist --list-subs "URL"
```

记录可用语言标签，不要假设一定叫 `zh`、`en` 或 `en-orig`。

### 3. 只下载文字时间轴

优先人工字幕，同时允许自动字幕作为备选。**优先要 `srt`，不要 `vtt`**：

```bash
yt-dlp --no-playlist --skip-download \
  --write-subs --write-auto-subs \
  --sub-langs "zh.*,en.*" --sub-format "srt/vtt" \
  -o "source/%(id)s/%(id)s.%(ext)s" \
  "URL"
```

`--sub-format "srt/vtt"` 是「优先 srt，拿不到才回落到 vtt」。原因不是偏好，是实测差异：
YouTube 的自动字幕原生只有 VTT，而 VTT 是**滚动缓冲**格式——画面始终显示「上一行 + 当前行」，
于是每条 cue 都把上一行重抄一遍，还有大量 10 毫秒的切换 cue。实测同一份 TED 演讲：

| | 原生 VTT | yt-dlp 转出的 SRT |
|---|---|---|
| 带时间戳的 cue | 315 | 158 |
| 含行内 `<c>` / 词级时间标签的 cue | 113 | 0 |
| 正文样例 | `Hear<00:00:19.720><c> that?</c>` | `Hear that?` |

换成 srt 就等于让 yt-dlp 先替你做掉这层归一化。`select_quotes.py` 两条都能读
（它自己会剥标签、按滚动结构只取新增行、裁掉相邻 cue 的时间重叠），
但 srt 路径少一层转换，排查问题时更容易看懂。

字幕和视频始终分两条命令下载：字幕命令带 `--skip-download`，视频命令不带任何 `--write-*subs` 参数。这样字幕请求失败（例如 429）不会中止视频下载，视频格式出错也不会白白重复请求字幕。

`zh.*` 这类正则会同时匹配 YouTube 的自动翻译轨（标签形如 `zh-Hans-en`），这类轨道最容易触发 HTTP 429。先看 `--list-subs` 的真实标签，只请求需要的一两个，例如人工字幕 `zh-Hans` 和原声自动字幕 `en-orig`；请求多个语言时加 `--sleep-subtitles 5`。

反过来也要知道：`--sub-langs` 的每一项是**锚定的正则**，yt-dlp 用
`re.fullmatch` 去比对语言标签（`orderedSet_from_options(..., use_regex=True)`）。
所以裸写的 `zh-Hans` **只**匹配叫 `zh-Hans` 的那条轨，**不会**顺带匹配 `zh-Hans-en`。
想要译文轨就得把全名写出来——这既是「不要用通配」的理由，也是译文轨要单独请求的原因。
（加 `.` 才是通配：`zh` 和 `zh-Hans-en` 都是 `zh.*` 的匹配对象。）

字幕轨先用来建立“内容—时间点”索引。原生模式不把 VTT 画回图片，也不因为文字稿里有一句话，就假设该时刻画面内烧录字幕完全相同。脚本模式可以使用已复核的 VTT/SRT/Whisper 文字，但必须标明为后期绘制，并将人名、数字、专有名词和翻译含义单独核对。

### 3.5 拿到的字幕怎么接到选句

`--list-subs` 只告诉你有哪些轨；真正决定「截哪几句」的是下一步。
拿到 SRT/VTT 后直接交给 `select_quotes.py`，不必先看完整支视频：

```bash
python3 "<SKILL_DIR>/scripts/select_quotes.py" pick "source/<id>/<id>.en.srt" \
  --out work/<id> --top 5 --render --render-mode script --aspect 3:4 --width 1080
```

批处理某个频道时把下载目录整个交给它：

```bash
python3 "<SKILL_DIR>/scripts/select_quotes.py" batch source/ --out work/ --top 5
```

它按 `*.transcript.json` → `*.srt` → `*.vtt` → `*.json` 的顺序找文字稿，并自动跳过
yt-dlp 的 `*.info.json`（那是元数据、没有 `segments`）。
同名视频会被自动认出来当取帧源，包括 yt-dlp 的 `talk.en.srt` ↔ `talk.mp4` 这种命名。
**同一支视频的多条语言轨只处理一条**（优先 `-orig` 原声轨）——
否则一次 `--sub-langs "zh.*,en.*"` 就会让同一支视频在全局精选里重复三遍。
排序依据与局限见 [auto-quote-selection.md](auto-quote-selection.md)。

### 3.5.1 整个频道 / 播放列表：用 `fetch_transcripts.py`

不要手写循环去套上面的命令。脚本已经把「先取字幕、后补视频」的两段式做进去了：

```bash
# 1. 只取字幕（不下载视频），几分钟拿下一个频道
python3 "<SKILL_DIR>/scripts/fetch_transcripts.py" \
  "https://www.youtube.com/@频道/videos" --out source/ --limit 50

# 2. 选句，产出审阅表与全局精选
python3 "<SKILL_DIR>/scripts/select_quotes.py" batch source/ --out work/ --top 5

# 3. 人看过审阅表后，只给要出图的那几条补视频
python3 "<SKILL_DIR>/scripts/fetch_transcripts.py" \
  --ids "8S0FDjFBj8o,dQw4w9WgXcQ" --out source/ --videos

# 4. 出图（视频已在手，批处理自动配对）
python3 "<SKILL_DIR>/scripts/select_quotes.py" batch source/ --out work/ \
  --render --render-mode script --aspect 3:4 --width 1080
```

**为什么必须先选后下**：挑句子只需要文字稿。一个 200 条的频道全量下载是几十 GB 和几小时；
只取字幕是几分钟。反过来做，你会把绝大多数带宽花在永远用不上的素材上。

几个参数的含义：

| 参数 | 说明 |
|---|---|
| `--limit N` | 只处理前 N 条；枚举阶段也带 `--playlist-end`，不会为了拿 3 条而列完整个频道 |
| `--ids A,B` | 只处理这些 id 或链接。**给了它就不再碰整个频道**，适合第 3 步补视频 |
| `--videos` | 同时下载视频（`bv*[height<=1080]+ba`，1080p 足够做社交图） |
| `--dry-run` | 只列出将要处理的条目，不下载 |
| `--cookies-from-browser` / `--proxy` | 同上面的 Cookie 与代理规则 |

重跑是增量的：内置了一份 `--download-archive`（`.fetched.txt`），已取过的条目直接跳过，
所以可以定期对新更的频道重跑同一条命令。

跑完会写一份 `sources.json`，逐个条目记下 id、标题、时长、拿到了哪些字幕、
以及**哪些条目根本没有字幕轨**——最后一类必须改走本地 Whisper 转写，不要静默漏掉。

### 3.6 英文/非中文素材的两点注意

1. **发布价值这一维在非中文素材上恒为 0**。打分信号词表是按中文写的
   （「并不是…而是」「归根结底」「必须」…），英文台词一个都不命中。
   于是英文素材实际只按独立性、连续性、信息密度三维排序——**排的是「结构完整」，
   不是「值得传播」**。英文素材请把 `--top` 调大、人工过筛的比例留高一些。
2. **画面下缘常有台标或角标**。TED 这类视频在底部烧了 `NOVEMBER2014 NEWYORKNEWYORK`
   一类的字标，而字幕条默认取的是画面 0.78–0.96 这一段（这是个横向画幅假设），
   于是字标会透出来形成重影。实测改这两个参数即可压掉：

   ```bash
   --frame-bottom 0.82 --band-center 0.96
   ```

   先出 1 张看效果，再批量。

### 3.7 用 YouTube 自带译文轨产出中文版

YouTube 给绝大多数视频免费生成**翻译字幕**，无需 API key、无需登录，`yt-dlp --list-subs`
就能看到。标签形如 `<目标语>-<源语>`，含义是「从某源语翻译到某目标语」：

```
zh-Hans-en      Chinese (Simplified) from English
zh-Hant-en      Chinese (Traditional) from English
zh-Hans-ko      Chinese (Simplified) from Korean
```

实测一支 TED 演讲有 **156 条**这类译文轨：一个目标语会按每个源语**各生成一份**
（`zh-Hans-ar`、`zh-Hans-en`、`zh-Hans-fr`、`zh-Hans-ko`、`zh-Hans-pt-BR`…）。
所以「要中文」不是一条轨，而是一组候选，得逐个写全名去要。

正确用法（`fetch_transcripts.py` 会替你展开）：

```bash
# 原声轨照常取，同时把中文译文轨一起拉下来；英文版保留，两套并存
python3 "<SKILL_DIR>/scripts/fetch_transcripts.py" \
  "https://www.youtube.com/@频道/videos" --out source/ \
  --lang "en-orig,en" --translate zh-Hans
```

**为什么是「按源语各请求一遍」**：`--sub-langs` 用的是锚定匹配（见上文），
`zh-Hans` 不会匹配 `zh-Hans-en`。脚本把 `--lang` 里的每个原声标签都拼成
`zh-Hans-<源语>` 一起请求；某个源语不存在时只是拿不到，不会报错。
顺带也会请求裸的 `zh-Hans`（那是人工译本，有就直接用）。

#### 译文轨能不能直接当生产输入：分情况

| 情形 | 实测对齐情况 | 能否按行号映射 |
|---|---|---|
| **自动译文轨**（`zh-Hans-en`） | 134/134 条，**起止时间与原文逐条相同**，仅 2 条正文为空 | 可以用，但仍建议按时间取 |
| **人工译本**（裸 `zh-Hant` 这类） | 358/358 条时间戳完全相同，但**正文整体错位**，且有 26 条「有时间戳、正文为空」 | **绝对不行** |

错位的具体样子：原文第 66 条是 `ignore.`，同一序号的中文是 `忽視。  這一切`，
而第 67 条中文是空的。产品侧的解析器会丢掉空 body 的 cue，于是索引从 358 掉到 332——
**按行号映射会静默错位并丢句，而且看起来「成功」**。

所以规则是：

1. **按时间范围取译文，永远不要按行号/索引取。**
2. 跳过「有时间戳、正文为空」的 cue。
3. 只取语言码与原文轨一致的那一条（原文是 `en`，就取 `zh-Hans-en`；裸语言码的人工轨亦可）。
   同目录里可能同时存在 `zh-Hans-en` / `zh-Hans-ko` / `zh-Hans-pt-BR` 等多条，必须挑一条。

`select_quotes.py --translate zh-Hans` 已按这三条实现（`find_translation` 挑轨、
`translated_rows` 按时间取行）。它**不会**把译文按原文行数切——实测译文换行位置与原文不同，
按原文行数切会切出一条只有一个逗号的「行」。

#### 译文轨很容易撞 429

这是译文轨区别于普通字幕轨最明显的运维特征。第一次请求经常直接 429，
但**它是瞬时的**，退避几十秒后重试通常第一次就成功。
`fetch_transcripts.py` 内置退避重试（见「字幕下载报 HTTP 429」一节）。
手工试的时候不要用退出码判成败——`--ignore-errors` 会把 429 变成「退出码 0」。

#### 译文长度与渲染器的硬限制

中文译文是整句（实测 15–29 字），而渲染器**缩到可读下限也只放得下 29 个汉字**
（基础字号下约 16 个）。字号只会从基础值缩到 55%（`max(12, round(font*0.55))`，步长 2），
所以 29 字那行会直接失败，报错形如：

```
台词过长，缩小到可读下限后仍放不下: '或者越来越多地燃烧液化天然气，这会释放二氧化碳和甲烷，甲烷是'
```

**别以为只有中文需要拆行**。原先这里写着「英文原句通常 ≤ 40 个拉丁字符，天然放得下」，
2026-10-12 被证伪：合并滚动 cue 之后的英文句子中位 44、p90 67、最长 78 个字符，
而拉丁字符的上限是 60——**21.6% 的英文 cue 必然报错**。原文那一套同样要拆。
统一按「宽度单位」记账（汉字 2、拉丁 1），实测基础字号 16 汉字 ≈ 32 拉丁、
可读下限 29 汉字 ≈ 60 拉丁，两组数一致。详见
[auto-quote-selection.md](auto-quote-selection.md) 的「脚本字幕：两张硬约束」。

### 4. 下载视频

通常不需要 4K。1080p 足以做 1440×1920 的社交图；输出尺寸可能放大，但不应宣称提升了真实清晰度。

```bash
yt-dlp --no-playlist \
  -f "bv*[height<=1080]+ba/b[height<=1080]" \
  -o "source/%(id)s/%(id)s.%(ext)s" \
  "URL"
```

`/` 只在格式**不存在**时生效；某个格式存在但下载时返回 HTTP 403，yt-dlp 不会自动改用下一项。遇到 403 时先更新 yt-dlp 并重试一次，仍失败就按下表逐级换一条 `-f` 重新下载，每级只试一次：

| 级别 | `-f` 参数 | 说明 |
| --- | --- | --- |
| 1 | `bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]` | 1080p H.264 + AAC，兼容性最好 |
| 2 | `bv*[height<=1080]+ba` | 1080p 任意编码（VP9、AV1） |
| 3 | `bv*[height<=720]+ba` | 降到 720p 的分离流 |
| 4 | `b[height<=1080]` | 已合并的单文件；YouTube 上通常只剩 360p |
| 5 | `b` | 任意可用的单文件，最后手段 |

不确定有哪些格式时先运行 `yt-dlp --no-playlist -F "URL"`。降级后要向用户说明实际分辨率；第 5 级仍失败就停止并报告错误，不要继续尝试 extractor 参数。

分离的视频和音频需要 FFmpeg 合并。原生字幕取帧不依赖音频，但保留带音频的完整源文件方便复核内容。重复执行时复用已有文件，不要无理由重新下载。

## 没有可用字幕轨

按以下顺序处理：

1. 检查视频描述、章节和人工字幕轨；
2. 检查自动字幕轨；
3. 如果画面本身有烧录字幕，可先用候选帧总览人工选句；
4. 用户确实需要长视频的语义选段时，再询问是否允许使用本地 Whisper 或环境中已有的语音转写 Skill。

Whisper 是可选上游，用来生成带时间戳文字稿。它的识别文本不能替代画面内原生字幕；脚本模式需要使用它时，也必须先校对文本，并回到视频帧验证时间点。

## 登录、Cookies 与访问限制

- 只处理用户有权访问、下载和再利用的内容。
- 先执行一次不带 Cookie 的元数据检查。不要把 Cookie 作为所有公开视频的默认参数。
- 遇到 `Sign in to confirm you’re not a bot`、登录、年龄验证或用户自己的非公开视频时，不要退回“只能处理本地视频”；先说明原因并取得授权，再使用 `--cookies-from-browser chrome`。
- 不要求用户粘贴账号密码，不导出或提交 Cookie 文件，不把浏览器配置、访问令牌或绝对路径写入仓库。
- 不绕过 DRM、付费墙、地区限制或其他访问控制。
- Cookie 命令只用于当前来源获取，不得显示 Cookie 内容，也不得把 Cookie 写入日志、来源记录或交付物。
- Cookies 失败时停止并报告，不要遍历或反复尝试多个浏览器账户。

### Chrome Cookie 授权流程

只在无 Cookie 请求明确被访问验证拦截后执行：

1. 向用户复述真实错误，例如 YouTube 要求“登录以确认不是机器人”。
2. 询问：`是否允许我让 yt-dlp 临时读取 Chrome 中已登录的 YouTube Cookie？它只用于获取这个链接的视频和字幕，不会导出、保存或上传 Cookie。`
3. 用户明确同意后，把 `--cookies-from-browser chrome` 加到这个 URL 后续所有 yt-dlp 命令，而不是只加在第一次探测命令上。
4. 默认只读 Chrome 当前默认配置。若机器上有多个 Chrome 配置且默认配置无效，停止并询问用户要使用哪个配置；不要自行扫描所有配置。
5. 若用户拒绝，提供两种清晰选择：让用户自行下载视频后按本地模式继续，或结束 URL 任务。不要把这种受限后的备选说成 Skill 的固有限制。

授权后的元数据检查：

```bash
yt-dlp --cookies-from-browser chrome --js-runtimes node \
  --no-playlist --skip-download \
  --print "%(id)s | %(title)s | %(channel)s | %(duration_string)s" \
  "URL"
```

授权后的字幕轨检查与下载：

```bash
yt-dlp --cookies-from-browser chrome --js-runtimes node \
  --no-playlist --list-subs "URL"

yt-dlp --cookies-from-browser chrome --js-runtimes node \
  --no-playlist --skip-download \
  --write-subs --write-auto-subs \
  --sub-langs "zh.*,en.*" --sub-format vtt \
  -o "source/%(id)s/%(id)s.%(ext)s" \
  "URL"
```

授权后的视频下载：

```bash
yt-dlp --cookies-from-browser chrome --js-runtimes node \
  --no-playlist \
  -f "bv*[height<=1080]+ba/b[height<=1080]" \
  -o "source/%(id)s/%(id)s.%(ext)s" \
  "URL"
```

如果环境使用 Deno，删除 `--js-runtimes node` 即可，Cookie 规则不变。若默认 Chrome 配置不含有效 YouTube 会话，可在用户明确指定配置后使用 yt-dlp 支持的 `chrome:PROFILE` 形式；不要猜测配置名。读取失败、系统钥匙串拒绝访问或站点仍拒绝请求时，只重试一次经过用户确认的同一配置，然后停止并报告具体错误。

## 常见故障

### `yt-dlp` 有版本但 YouTube 格式不完整

先看诊断里是否存在 JavaScript runtime。Deno 默认启用；Node.js 环境必须显式添加：

```bash
yt-dlp --js-runtimes node ...
```

随后更新 yt-dlp。不要先改一串实验性 extractor 参数，因为这会让工作流难以复现。

### YouTube 要求登录以确认不是机器人

这是来源访问验证，不代表 Skill 只能处理本地视频。按“Chrome Cookie 授权流程”先取得用户授权，再让同一 URL 的元数据、字幕和视频命令都带上 `--cookies-from-browser chrome`。用户没有授权时才停止联网获取，并给出本地文件备选。

### 字幕语言下载不到

先运行 `--list-subs`，按真实语言标签修改 `--sub-langs`。不要把自动翻译字幕当成人工原字幕。

### 视频下载报 HTTP 403

403 通常只针对某个具体格式，或说明 yt-dlp 版本已落后于站点改动。先更新 yt-dlp 并确认 JavaScript runtime 生效，再按“下载视频”一节的格式回退表逐级重试。若所有格式都返回 403，且无 Cookie 的元数据请求也被拒，转到“登录、Cookies 与访问限制”处理，不要循环重试。

### 字幕下载报 HTTP 429

429 表示请求过于频繁，几乎只出现在自动翻译字幕轨上（普通字幕轨罕有）。**429 是瞬时的，
退避重试是有效手段，不要一看到就放弃**：实测同一次运行里主字幕 `en` 正常下完，
紧接着的 `zh-Hans-en` 直接 429；换 `--sub-format`（ttml/srv3/json3）、加
`--sleep-subtitles 8`、冷却 90 秒后重跑，**全部仍然 429**；而换用带退避的重试脚本
（0/45/90/180/300 秒）在**第一次重试**就成功了。

`fetch_transcripts.py` 已经把这条退避逻辑内置（`--retry-delays`，默认 `0,45,120,300`
= 最多试 4 次），所以正常路径上不需要手工处理。要手工排查时按这个顺序：

1. 确认字幕命令与视频命令是分开的，视频下载不受影响。
2. **看输出文本，不要看退出码**。命令里带了 `--ignore-errors`，而 yt-dlp 的
   `trouble()` 在忽略错误时**跳过** `_download_retcode = 1`，于是 429 会以
   「**退出码 0** + stderr 里一行 `ERROR: ... HTTP Error 429`」的形式静悄悄过去。
   用 `grep 429` 判成败，别用 `$?`。
3. 改用 `--list-subs` 中的具体标签，把一次请求的语言数压下来——译文轨按源语各有一份，
   请求 6 个源语就是 6 次 timedtext 请求。
4. 退避几十秒后重试；`--no-overwrites` + `--download-archive` 保证重试只补缺的那些。
5. 仍然拿不到时，用已拿到的其他语言字幕定位时间点，或在用户同意后用 Whisper 转写。

### 视频下载成功，但图片里没有字幕

这不是取帧失败，而是原生字幕条件不成立。说明该视频只有独立字幕轨；若用户同意后期绘制，转用 `render-script`，否则停止。

### 下载很慢或中断

保留工作目录和分片，重试同一条命令以复用缓存。连续失败后报告网络或站点限制，不要无限重试。
