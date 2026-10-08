# 中国大陆视频平台的取源与字幕

读取时机：链接不是 YouTube，而是 B站、抖音、小红书、微博等中国大陆平台时。

[yt-dlp-and-transcripts.md](yt-dlp-and-transcripts.md) 的默认前提是 YouTube（`--js-runtimes`、`en-orig` 自动字幕轨、429 翻译轨、"confirm you're not a bot"）。国内平台的风控方式、字幕形态和画质门槛都不同，按本文路由，不要直接套用那份文档的命令。

## 平台路由表

| 平台 | yt-dlp extractor | 免登录可用 | 实测结论 |
|---|---|---|---|
| B站 | `BiliBili` 系列（20+ 个） | ✅ 可以 | 元数据、格式列表、下载全部可用。免费画质到 1080P |
| 抖音 | `Douyin` | ❌ 不可以 | **当前完全不可用，原因在 yt-dlp 侧，不是 Cookie 问题** |
| 微博 | `Weibo` / `WeiboVideo` / `WeiboUser` | 未实测 | 有 extractor |
| 小红书 | `XiaoHongShu` | 未实测 | 有 extractor |
| AcFun | `AcFunVideo` / `AcFunBangumi` | 未实测 | 有 extractor |
| 优酷 | `youku` / `youku:show` | 未实测 | 有 extractor |
| 爱奇艺 | `iqiyi` | 未实测 | 有 extractor |
| 西瓜视频 | `Ixigua` | 未实测 | 有 extractor |
| 今日头条 | `toutiao` | 未实测 | 有 extractor |
| 喜马拉雅 | `ximalaya` / `ximalaya:album` | 未实测 | 有 extractor |
| 虎牙 | `huya:live` / `huya:video` | 未实测 | 有 extractor |
| 斗鱼 | `DouyuTV` / `DouyuShow` | 未实测 | 有 extractor |

先确认环境有没有对应 extractor：

```bash
yt-dlp --list-extractors | grep -i bili
```

“有 extractor”不等于“能用”。上表里只有 B站 和抖音做过完整实测，其余平台第一次处理时必须先跑元数据检查，不要假设成功。

## B站

### 元数据与格式

```bash
yt-dlp --no-playlist --skip-download \
  --print "%(extractor)s | %(id)s | %(duration)ss | %(resolution)s | %(title)s" \
  "https://www.bilibili.com/video/BVxxxxxxxxxx"
```

多数视频免登录即可解析。列清晰度：

```bash
yt-dlp --no-playlist -F "URL"
```

免费账号的常见上限是 1080P。4K、1080P 高码率等格式会提示需要大会员，yt-dlp 会打印类似：

```text
Format(s) 4K 超高清, 1080P 高码率 are missing; you have to become a premium member to download them.
```

这不是解析失败。**不要为了拿更高画质就去读 Cookie**；1440×1920 的成品用 1080P 源已经足够，且放大不会提升真实清晰度。

### 下载

B站把视频和音频拆成两条流，需要 FFmpeg 合并：

```bash
yt-dlp --no-playlist -f "<视频格式ID>+<音频格式ID>" --merge-output-format mp4 \
  -o "source/%(id)s/%(id)s.%(ext)s" "URL"
```

**格式 ID 必须从 `-F` 的实际输出里取**，不要照抄示例。B站常见的是 `30016/30032/30064/30080`（视频）配 `30216/30232/30280`（音频），但以 `-F` 输出为准。

只用单条流（`-f 30064`）会得到无声视频。原生字幕取帧不依赖音频，但仍建议保留带音频的完整源文件，方便复核内容。

### 弹幕不是字幕轨（重要）

B站的 `--list-subs` 会把弹幕当成一条“字幕”列出来：

```text
Language Formats
danmaku  xml
```

这是**弹幕**，不是字幕轨。它有三个后果：

1. 看到 `danmaku` **不能**得出“这个视频有字幕轨”的结论；
2. 不能把弹幕当文字稿去选题——弹幕是观众评论，不是视频内容；
3. 更不能把弹幕画进成品图。

真正的字幕轨是 B站的 CC 字幕（UP 主上传或 AI 生成），在 `--list-subs` 里表现为语言标签（如 `zh-Hans`），而不是 `danmaku`。

### CC 字幕需要登录态

yt-dlp 的 B站 extractor 支持把 CC 字幕转成 SRT，但受 `data.need_login_subtitle` 控制：**多数视频的 CC 字幕必须带登录态 Cookie 才取得到**。

按 [yt-dlp-and-transcripts.md](yt-dlp-and-transcripts.md) 的“Chrome Cookie 授权流程”先取得用户授权，再执行：

```bash
yt-dlp --cookies-from-browser chrome --no-playlist --skip-download \
  --write-subs --sub-langs "zh-Hans" --sub-format srt \
  -o "source/%(id)s/%(id)s.%(ext)s" "URL"
```

先跑一次 `--list-subs` 拿到真实语言标签。若授权后仍只有 `danmaku`，说明该视频没有 CC 字幕，按“没有可用字幕轨”处理。

### B站画面常见干扰

- 搬运和切片视频的烧录字幕是**非官方**的，画质和位置都可能变化，同一支视频要抽查开头、中段、结尾；
- 左上角常有平台水印或 UP 主 ID，按 [visual-style.md](visual-style.md) 保持原样，不自行抹除；
- 番剧和影视切片可能带片头、角标、进度条，取帧时避开。

## 抖音

### 结论：当前不可用，别在 Cookie 上找原因

三种地址形态都试过，报错完全相同：

- `https://www.douyin.com/video/<数字ID>`
- `https://v.douyin.com/<短码>/`
- `https://www.iesdouyin.com/share/video/<数字ID>/`

```text
ERROR: [Douyin] <id>: Fresh cookies (not necessarily logged in) are needed
```

**这句报错有误导性。** 它的真实含义是接口返回了空 body，而不是 Cookie 不对。

原因在 yt-dlp 的 `DouyinIE._real_extract`：它请求 `https://www.douyin.com/aweme/v1/web/aweme/detail/`，抖音对没有签名的请求返回 HTTP 200 + 空 body，于是 `aweme_detail` 为空，extractor 抛出上面那句错误。源码注释写得很明确：

```python
if not detail:
    # TODO: Run verification challenge code to generate signature cookies
    raise ExtractorError(
        'Fresh cookies (not necessarily logged in) are needed', ...)
```

即 **yt-dlp 还没实现抖音的签名挑战（a_bogus）**。这是 upstream 的 TODO，不是你的环境问题。

已经验证过、**不必再试**的做法：

- 换不同的抖音链接（用当天热榜的真实 aweme_id 试过，结果一样）；
- 只带匿名 Cookie（只能拿到 `__ac_nonce`，不够）；
- `--cookies-from-browser chrome` 读完整登录态（能成功提取上千条 Cookie，含 `ttwid`、`sid_guard`、`odin_tt`，仍然失败）；
- 反复重试——接口返回的是空 body，重试无用。

### 正确做法

按顺序处理：

1. 先更新 yt-dlp 到 nightly，再试一次。若 upstream 已实现签名，这条就通了；失败就停在这里，不要继续调参数。
2. 让用户自己把视频下载到本地（App 的“保存到相册”，或网页端另存），然后走本 Skill 的**本地视频模式**。pipeline 从 `sample` 开始完全一样。
3. 若用户已有本地的抖音下载 / 转写流水线（例如已配置好 Cookie 的第三方下载器 + 本地 ASR），复用它产出的视频和带时间戳文字稿，本 Skill 只负责取帧、拼图和质检。

不要为了绕过风控去写签名算法、自动化验证码或调用第三方解析 API——那超出本 Skill 的授权边界，也会把用户的账号和内容送进不受控的第三方。

### 抖音的短链必须直连

`v.douyin.com` 短链在 HTTP 代理下可能直接报 TLS 错误：

```text
SSL: UNEXPECTED_EOF_WHILE_READING
```

国内平台不需要走境外代理。若环境预置了 `HTTP_PROXY` / `HTTPS_PROXY` 且指向内网或不可达的代理，处理国内链接前先清掉：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
    -u ALL_PROXY -u all_proxy yt-dlp ... "URL"
```

清理代理后短链能正常 302 跳转。反过来说，若你确实需要代理才能出网，也要确认该代理能访问国内站点。

## 其他平台

微博、小红书、AcFun、优酷、爱奇艺、西瓜、头条、喜马拉雅、虎牙、斗鱼都有 extractor，但**本 Skill 未做实测**。第一次处理时：

1. 先跑元数据打印，确认 `extractor` 不是 `generic`（落到 `generic` 说明没被识别）；
2. 再列格式；
3. 需要登录时按上面 B站的 Cookie 授权流程处理，并明确告诉用户正在读取哪个站点的 Cookie。

这些平台的风控和抖音类似，出现空 body 或验证页时要如实报告“该平台当前不可用”，而不是反复重试或退回“只能处理本地视频”。

## 交付时要说清的事

处理国内平台时，交付说明里至少要写：

- 真实使用的平台和链接，以及 extractor 实际匹配到的 ID；
- 是否有 CC 字幕轨，还是只有弹幕——两者不能混为一谈；
- 字幕模式（原生 / 脚本）以及理由；
- 实际下载到的分辨率，以及是否因权限限制降级；
- 源素材里平台水印、UP 主标识保持原样这一事实。
