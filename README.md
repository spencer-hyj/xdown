# xdown

> 下载 X (Twitter) 视频，并可选转成 GIF —— 一条命令搞定。

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Deps](https://img.shields.io/badge/pip%20deps-none-success)](xdown.py)
[![Powered by ffmpeg](https://img.shields.io/badge/powered%20by-ffmpeg-orange)](https://ffmpeg.org/)


- 单文件 Python 脚本，无第三方 pip 依赖（只用标准库 + 系统里的 `yt-dlp` / `ffmpeg`）
- 优先走 X 的公开 **syndication 接口**拿视频直链，快且**无需登录**
- 拿不到直链时自动回退 `yt-dlp`（需要登录的内容可配浏览器 cookies）
- GIF 使用 `palettegen` + `paletteuse` 两遍调色板，画质好、体积小
- 支持裁剪时间段、限帧率、限宽度、**限体积自动压缩**、跳重复帧

## 安装

依赖：

```bash
# yt-dlp（回退下载器）
uv tool install yt-dlp        # 或: brew install yt-dlp
# ffmpeg / ffprobe（GIF 转换）
brew install ffmpeg           # 或: sudo port install ffmpeg
```

安装 `xdown` 命令（任选其一）：

```bash
# 推荐：uv（装成独立的命令行工具，可 --editable 便于改源码）
uv tool install git+https://github.com/spencer-hyj/xdown.git

# 或者 pipx
pipx install git+https://github.com/spencer-hyj/xdown.git

# 或者 clone 后本地安装（--editable 让改源码即时生效）
git clone https://github.com/spencer-hyj/xdown.git
uv tool install --editable ./xdown

xdown --help
```

也可以完全不安装，直接把脚本跑起来（只有一个文件，无 pip 依赖）：

```bash
python3 xdown.py <url> --gif
```

## 用法

**不指定 `-o` 时默认下载到 `~/Downloads`**，跟浏览器下载一个习惯：

```bash
xdown <url>              # 视频 -> ~/Downloads
xdown <url> --gif        # GIF  -> ~/Downloads
```

换目录：

```bash
xdown <url> --gif -o ~/Desktop          # 指定任意目录
xdown <url> --gif -o .                  # 当前目录
export XDOWN_OUTPUT=~/Desktop           # 改长期默认，写进 ~/.zshrc

xdown <url> --gif --to-clipboard        # 顺便放进剪贴板，可直接 Cmd+V 粘贴
```

其他常用组合：

```bash
# 裁剪 3~6 秒、15fps、宽 480
xdown <url> --gif --start 3 --end 6 --fps 15 --width 480

# 限制 GIF 不超过 5MB（自动逐步降 fps/尺寸重试）
xdown <url> --gif --max-size 5

# 画面静止较多的视频，丢重复帧可大幅减小体积
xdown <url> --gif --mpdecimate

# 从剪贴板读链接（macOS / Linux）
xdown -c --gif

# 批量（多个产物时最后一个进剪贴板）
xdown <url1> <url2> <url3> -o ~/Downloads

# 纯 tweet ID 也行
xdown 1234567890
```

### 参数速查

| 参数 | 说明 |
| --- | --- |
| `-o, --output DIR` | 输出目录。**不指定则默认 `~/Downloads`** |
| `--to-clipboard` | 额外把产物放进系统剪贴板（可粘贴到聊天窗口） |
| `-f, --force` | 覆盖已存在文件 |
| `--reveal` | 完成后在 Finder / 文件管理器里定位输出文件 |
| `-c, --clipboard` | 从剪贴板读取**链接**（方向与上面相反） |
| `--gif` | 下载后转 GIF |
| `--fps N` | GIF 帧率（默认 12） |
| `--width N` | GIF 宽度像素，高度自适应（默认 480） |
| `--start / --end` | 裁剪起止时间（秒） |
| `--quality low\|medium\|high` | 调色板颜色数 128/192/256 |
| `--dither bayer\|none` | 抖动方式；`bayer` 体积小，`none` 更干净 |
| `--loop N` | GIF 循环次数，`0` = 无限 |
| `--max-size MB` | GIF 体积上限，超出自动降 fps/尺寸 |
| `--mpdecimate` | 丢弃几乎未变的重复帧 |
| `--rm-video` | 转 GIF 后删除原视频 |
| `--video-only` | 只下载 video，忽略 animated_gif 媒体 |
| `--no-metadata` | 不写 `.info.json` |
| `--cookies-from-browser BROWSER` | yt-dlp 回退时使用浏览器 cookies |
| `--dry-run` | 只显示将要做什么 |

## 工作原理

1. 用 `token = (id / 1e15 * π).toString(36)`（源自 `react-tweet`）请求
   `https://cdn.syndication.twimg.com/tweet-result?id=<id>&token=<token>`，
   从 `mediaDetails[].video_info.variants` 里挑码率最高的 mp4 直链。
2. 直链下载到 `<用户名>_<推文ID>.mp4`（失败会重试，再失败回退 `yt-dlp`）。
3. 若 `--gif`，用 ffmpeg 两遍处理：
   - 第一遍 `fps → scale → palettegen` 生成调色板
   - 第二遍 `paletteuse` 套用调色板输出 GIF（`bayer` 抖动 + `diff_mode=rectangle`）

## 输出示例

```
▶ 处理 https://x.com/TsarSylveon/status/2110476568543850930  (id=2110476568543850930)
直链下载: https://video.twimg.com/amplify_video/.../O0zVQij7u2ohRB5p.mp4
转 GIF: TsarSylveon_2110476568543850930.mp4 (29.8s)  12fps, w=400, quality=medium, dither=bayer, 上限 4.0MB
体积超限,重试: 9fps / 400px
体积超限,重试: 7fps / 400px
体积超限,重试: 6fps / 340px

✔ 完成,输出到 /tmp/xd4:
  TsarSylveon_2110476568543850930.info.json  (473B)
  TsarSylveon_2110476568543850930.mp4  (4.7MB)
  TsarSylveon_2110476568543850930.gif  (3.4MB)
```

## 结果去哪了

按这个优先级决定输出目录：

| 优先级 | 来源 | 例如 |
| --- | --- | --- |
| 1 | `-o/--output` | `xdown <url> -o ~/Desktop` |
| 2 | `XDOWN_OUTPUT` 环境变量 | `export XDOWN_OUTPUT=~/Desktop` |
| 3 | `XDG_DOWNLOAD_DIR`（Linux 惯例） | 桌面环境自动设置的下载目录 |
| 4 | **`~/Downloads`** | 默认 |
| 5 | 当前工作目录 | 前面都不存在时的兜底（如无 HOME 的容器） |

```bash
xdown <url> --gif --reveal    # 完成后直接在 Finder 里定位到该文件
```

文件名格式：`<用户名>_<推文ID>.mp4` / `.gif` / `.info.json`；
一条推文里有多个视频时依次加 `_1`、`_2` 后缀。
结束时会把**绝对路径**打出来。

不想要 `.info.json` 就加 `--no-metadata`；转完 GIF 想丢掉原视频就加 `--rm-video`。

## 剪贴板（可选，`--to-clipboard`）

```bash
xdown <url> --gif --to-clipboard    # 存到 ~/Downloads 的同时放进剪贴板
```

- 放进去的是**文件引用**（macOS 上是 `«class furl»`），不是路径字符串
  - 贴进微信 / Slack / Discord → 直接发送该文件
  - 贴进 Finder → 复制/移动文件
  - 贴进终端 → 大多数终端不会粘出路径，这是正常的
- 多个链接时，**最后一个**产物进剪贴板（剪贴板本来就会被后续操作覆盖）
- 各平台实现：macOS `osascript` / Windows PowerShell `Set-Clipboard` / Linux `wl-copy` → `xclip` → `xsel`
- 系统没有剪贴板工具时只打印提示，文件照常落地，不会丢

## 相似的开源实现

这工具不是从零发明，很多环节都有成熟开源方案。常见的：

| 项目 | 语言 | 说明 |
| --- | --- | --- |
| **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** | Python | 事实标准。`yt-dlp <url>` 已能直接下 X 视频，本工具的下载回退就是它 |
| **[gallery-dl](https://github.com/mikf/gallery-dl)** | Python | 专注图/视频批量抓取，Twitter 支持完善，适合整号归档 |
| **[cobalt](https://github.com/imputnet/cobalt)** | TS | 最流行的开源自托管下载服务，带 Web UI，支持 X 等一堆站点 |
| **[gifski](https://github.com/ImageOptim/gifski)** | Rust | 高质量 GIF 编码器（时间抖动，画质优于 ffmpeg 调色板），`brew install gifski` |
| **[lux](https://github.com/iawia002/lux)** | Go | 多站点下载器；X 提取器已失效（本机装的 lux 实测报错） |
| **[media-harvest](https://github.com/aidistan/twitter-web-exporter)** | JS | 开源浏览器扩展，推文页直接出下载/导出按钮 |
| **[react-tweet](https://github.com/vercel/react-tweet)** | TS | 本工具 `token` 算法的来源（syndication 接口用法参考） |
| **ffmpeg 两遍调色板** | C | `palettegen`+`paletteuse` 是视频转 GIF 的通用标准做法 |

**那为什么还写这个？** 因为现成方案各缺一块：

- `yt-dlp` 下视频很强，但**不转 GIF**，你得自己再拼 ffmpeg 命令
- `ffmpeg` 一条命令也能转 GIF，但**默认质量差、体积巨大**，要手写两遍调色板
- `cobalt` 是 Web 服务，**没有 GIF 输出**，也不适合命令行批量
- 体积控制基本没人做——GIF 动辄几十 MB，本工具的 `--max-size` 自动降参重试是刚需

所以 `xdown` 的定位就是**把「X 链接 → 可用 GIF」这一步合成一条命令**，
并且零 pip 依赖、自动限体积、失败自动回退 yt-dlp。

如果你只要下视频，`yt-dlp` 就够了：

```bash
yt-dlp "https://x.com/user/status/123"
```

要转 GIF 但想更好画质，可以配合 gifski：

```bash
xdown <url>                                  # 先拿 mp4
gifski -o out.gif --fps 15 -W 480 in.mp4     # 再用 gifski 编码
```

## 注意

- 仅用于个人合理使用，请遵守 X 的服务条款与内容版权。
- 受保护 / 已删除的推文 syndication 接口会失败，此时用 `--cookies-from-browser chrome`
  让 `yt-dlp` 带登录态重试。
- 接口对 token 校验较宽松，但仍按算法正确生成，以防将来收紧。

## License

[MIT](LICENSE)

仅供个人学习与合理使用。下载内容的版权归原作者所有，请勿用于商业传播。
