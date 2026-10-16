#!/usr/bin/env python3
"""
xdown - 下载 X (Twitter) 视频，并可选转成 GIF。

依赖:
  - yt-dlp  (仅在主路径失败时作为回退)
  - ffmpeg  (仅转 GIF 时需要，用 palettegen/paletteuse 两遍调色板)

用法示例:
  xdown <url>                           # 下视频，默认存到 ~/Downloads
  xdown <url> --gif                     # 转成 GIF，默认存到 ~/Downloads
  xdown <url> --gif -o ~/Desktop        # 指定输出目录
  xdown <url> --gif --to-clipboard      # 顺便放进剪贴板，可直接粘贴
  xdown <url> --gif --fps 15 --width 480 --start 2.5 --end 7
  xdown <url> --gif --max-size 8 --mpdecimate   # 自动压到 8MB 以内
  xdown -c --gif                        # 从剪贴板读取链接
  xdown <url1> <url2> -o ~/Downloads    # 批量

外部工具路径可用环境变量覆盖（供 npm 等包装器注入自带二进制）:
  XDOWN_PYTHON / XDOWN_YTDLP / XDOWN_FFMPEG / XDOWN_FFPROBE
"""

from __future__ import annotations

__version__ = "0.1.0"

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 常量 / 工具函数
# --------------------------------------------------------------------------

STATUS_RE = re.compile(
    r"(?:twitter\.com|x\.com|mobile\.twitter\.com|mobile\.x\.com)/"
    r"(?P<user>[A-Za-z0-9_]+)/status(?:es)?/(?P<id>\d+)",
    re.IGNORECASE,
)
BARE_ID_RE = re.compile(r"^\d{1,25}$")
SYNDICATION_API = "https://cdn.syndication.twimg.com/tweet-result"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

C_RED = "\033[31m"
C_YEL = "\033[33m"
C_GRN = "\033[32m"
C_DIM = "\033[2m"
C_END = "\033[0m"


class XdownError(Exception):
    """单个链接处理失败（可继续处理下一个）。"""


def _use_color() -> bool:
    return sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def log(msg: str, color: str = "") -> None:
    if color and _use_color():
        print(f"{color}{msg}{C_END}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    log(f"错误: {msg}", C_RED)
    sys.exit(code)


def resolve_tool(env_var: str, name: str, hint: str) -> str:
    """定位外部可执行文件。优先取环境变量覆盖，便于被 npm 等包装器注入自带二进制。"""
    override = os.environ.get(env_var)
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        die(f"{env_var}={override!r} 不是可执行文件")
    path = shutil.which(name)
    if not path:
        die(f"找不到 `{name}`。{hint}\n        也可以用环境变量 {env_var} 指定绝对路径。")
    return path


def which_or_die(name: str, hint: str) -> str:
    return resolve_tool(f"XDOWN_{name.upper().replace('-', '_')}", name, hint)


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


# --------------------------------------------------------------------------
# URL 解析
# --------------------------------------------------------------------------


def parse_target(raw: str) -> tuple[str, str | None]:
    """返回 (status_id, username)。支持 status URL 或纯数字 ID。"""
    raw = raw.strip().strip("<>").strip()
    m = STATUS_RE.search(raw)
    if m:
        return m.group("id"), m.group("user")
    if BARE_ID_RE.match(raw):
        return raw, None
    raise XdownError(
        f"无法识别的链接或 ID: {raw!r}（示例: https://x.com/jack/status/20 或 20）"
    )


def media_duration(src: Path) -> float | None:
    """用 ffprobe 读取时长（秒），失败返回 None（此函数不致命，缺失时静默降级）。"""
    override = os.environ.get("XDOWN_FFPROBE")
    if override and Path(override).expanduser().is_file():
        probe = str(Path(override).expanduser())
    else:
        probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return None


def read_clipboard() -> str:
    found_tool = False
    for cmd in (["pbpaste"], ["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            found_tool = True
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
                if out.strip():
                    return out.strip()
            except subprocess.CalledProcessError:
                continue
    if found_tool:
        raise XdownError(
            "剪贴板里没有可用的文本链接"
            "（如果你刚用 xdown 把文件放进了剪贴板，那里面是文件引用而不是文本）"
        )
    die("无法读取剪贴板（未找到 pbpaste / wl-paste / xclip）")


# --------------------------------------------------------------------------
# 元数据 / 直链获取 (syndication API, 无需登录)
# --------------------------------------------------------------------------


def syndication_token(tweet_id: str) -> str:
    """X syndication API 的 token (算法源自 react-tweet)。

    token = (id / 1e15 * PI).toString(36) 去掉 '0' 和 '.'。
    当前接口对 token 校验很宽松，但保持正确以免将来失效。
    """
    return _to_base36((int(tweet_id) / 1e15) * math.pi).replace("0", "").replace(".", "")


def _to_base36(x: float) -> str:
    """贴近 JS 的 (num).toString(36)：输出最短的、能精确 round-trip 回该 double
    的 base36 字符串。用 Fraction 做精确运算，逐位尝试精度直到能还原。"""
    from fractions import Fraction

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if x != x:  # NaN
        return "NaN"
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _to_base36(-x)
    if x == float("inf"):
        return "Infinity"

    exact = Fraction(x)
    int_part = int(exact)  # x > 0，向零取整即 floor
    frac = exact - int_part

    if int_part == 0:
        int_s = "0"
    else:
        s = []
        n = int_part
        while n:
            n, rem = divmod(n, 36)
            s.append(digits[rem])
        int_s = "".join(reversed(s))

    if frac == 0:
        return int_s

    # 尝试 p 位小数，取最短能还原成同一 double 的
    for p in range(1, 31):
        pow36 = 36 ** p
        scaled = frac * pow36
        n = int(scaled)  # floor
        rem = scaled - n
        if rem > Fraction(1, 2) or (rem == Fraction(1, 2) and n % 2 == 1):
            n += 1
        if n == pow36:  # 进位到整数部分
            return _to_base36(float(int_part + 1))
        # n 解码回小数（精确），再加整数部分 → float
        cand = Fraction(int_part) + Fraction(n, pow36)
        if float(cand) == x:
            frac_s = []
            m = n
            for _ in range(p):
                m, d = divmod(m, 36)
                frac_s.append(digits[d])
            return int_s + "." + "".join(reversed(frac_s))
    # 理论上不会到这里
    return int_s


def fetch_syndication(tweet_id: str) -> dict | None:
    """通过公开 syndication 接口拿推文 JSON（含视频直链）。失败返回 None。"""
    import urllib.parse
    import urllib.request

    query = urllib.parse.urlencode(
        {"id": tweet_id, "token": syndication_token(tweet_id), "lang": "en"}
    )
    url = f"{SYNDICATION_API}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"{C_DIM}syndication 接口不可用 ({e.__class__.__name__}: {e}){C_END}")
        return None
    # 404 时接口会返回一坨 HTML 错误页，json 解析已失败；这里再兜底检查
    if not isinstance(data, dict) or data.get("__typename") not in ("Tweet", "TweetWithVisibilityResults"):
        log(f"{C_DIM}syndication 返回了非推文数据（可能已删除/受保护）{C_END}")
        return None
    return data


def pick_best_variant(media: dict) -> str | None:
    info = media.get("video_info") or {}
    variants = [v for v in info.get("variants", []) if v.get("content_type") == "video/mp4"]
    if not variants:
        return None
    variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
    return variants[0].get("url")


def extract_media(tweet_id: str) -> tuple[list[dict], dict]:
    """返回 (media列表, 元信息)。media 每项含 url/type/width/height/duration。"""
    data = fetch_syndication(tweet_id) or {}
    meta = {
        "id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
        "author": (data.get("user") or {}).get("screen_name"),
        "author_name": (data.get("user") or {}).get("name"),
        "text": data.get("text"),
        "created_at": data.get("created_at"),
    }
    media_out: list[dict] = []
    details = data.get("mediaDetails") or []
    for m in details:
        if m.get("type") not in ("video", "animated_gif"):
            continue
        info = m.get("video_info") or {}
        url = pick_best_variant(m)
        if not url:
            continue
        media_out.append(
            {
                "url": url,
                "type": "gif" if m.get("type") == "animated_gif" else "video",
                "aspect_ratio": info.get("aspect_ratio"),
                "duration_ms": info.get("duration_millis"),
            }
        )
    return media_out, meta


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------


def download_with_ytdlp(url: str, out_tmpl: str, cookies_from_browser: str | None) -> None:
    ytdlp = which_or_die("yt-dlp", "安装: uv tool install yt-dlp  或  brew install yt-dlp")
    cmd = [
        ytdlp,
        "--no-playlist",
        "--no-warnings",
        "--progress",
        "-f",
        "bv*+ba/b",  # 尽量取最佳画质音画合并，失败则回退单文件
        "--merge-output-format",
        "mp4",
        "-o",
        out_tmpl,
        url,
    ]
    if cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", cookies_from_browser]
    log(f"{C_DIM}$ {' '.join(shlex_quote(c) for c in cmd)}{C_END}")
    subprocess.run(cmd, check=True)


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def download_direct(mp4_url: str, dest: Path, retries: int = 4) -> None:
    import time
    import urllib.request

    log(f"{C_DIM}直链下载: {mp4_url}{C_END}")
    req = urllib.request.Request(mp4_url, headers={"User-Agent": UA})
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f)
                tmp.replace(dest)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                wait = 1.5 * attempt
                log(f"{C_YEL}下载失败 ({e.__class__.__name__}: {e})，{wait:.1f}s 后重试 {attempt}/{retries - 1}{C_END}")
                dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
                time.sleep(wait)
    assert last is not None
    raise last


def ytdlp_download(
    page_url: str, base: str, outdir: Path, args: argparse.Namespace, sink: list[Path]
) -> bool:
    """用 yt-dlp 下载，成功后把文件追加进 sink，返回是否成功。"""
    before = {p for p in outdir.glob(f"{base}.*")}
    try:
        download_with_ytdlp(page_url, str(outdir / f"{base}.%(ext)s"), args.cookies_from_browser)
    except subprocess.CalledProcessError:
        return False
    media_exts = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
    new = sorted(
        (p for p in outdir.glob(f"{base}.*") if p not in before and p.suffix.lower() in media_exts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not new:
        new = sorted(
            (p for p in outdir.glob(f"{base}.*") if p.suffix.lower() in media_exts),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:1]
    for p in new:
        if p not in sink:
            sink.append(p)
    return bool(new)


# --------------------------------------------------------------------------
# GIF 转换
# --------------------------------------------------------------------------


def convert_to_gif(
    src: Path,
    dest: Path,
    fps: int,
    width: int,
    start: float | None,
    end: float | None,
    quality: str,
    dither: str,
    loop: int,
    max_size_mb: float | None = None,
    mpdecimate: bool = False,
) -> None:
    """转 GIF。若指定 max_size_mb，则自动降 fps / 宽度直到满足体积上限。"""
    max_colors = {"low": 128, "medium": 192, "high": 256}[quality]

    attempts: list[tuple[int, int]] = [(fps, width)]
    if max_size_mb:
        f, w = fps, width
        # 先衹降 fps（保留清晰度），fps 到底后再开始缩宽度，最后跳帧
        while len(attempts) < 14 and (f > 6 or w > 160):
            if f > 6:
                f = max(6, int(f * 0.8)) if f > 8 else 6
            elif w > 160:
                w = max(160, int(w * 0.85) // 2 * 2)
            if (f, w) not in attempts:
                attempts.append((f, w))

    for i, (f, w) in enumerate(attempts):
        if i:
            log(f"{C_YEL}体积超限，重试: {f}fps / {w}px{C_END}")
        _render_gif(src, dest, f, w, start, end, max_colors, dither, loop, mpdecimate)
        size_mb = dest.stat().st_size / 1024 / 1024
        if max_size_mb is None or size_mb <= max_size_mb:
            return
    log(
        f"{C_YEL}警告: 已尽量压缩仍为 {dest.stat().st_size / 1024 / 1024:.1f}MB"
        f"（目标 {max_size_mb}MB）。可试 --start/--end 裁短或 --dither none{C_END}"
    )


def _render_gif(
    src: Path,
    dest: Path,
    fps: int,
    width: int,
    start: float | None,
    end: float | None,
    max_colors: int,
    dither: str,
    loop: int,
    mpdecimate: bool,
) -> None:
    ffmpeg = which_or_die("ffmpeg", "安装: brew install ffmpeg  或  sudo port install ffmpeg")
    scale = f"scale={width}:-1:flags=lanczos"
    pre = ["mpdecimate"] if mpdecimate else []
    timing = (["-ss", str(start)] if start is not None else []) + (
        ["-to", str(end)] if end is not None else []
    )
    vf_palette = ",".join([*pre, f"fps={fps}", scale,
                           f"palettegen=max_colors={max_colors}:stats_mode=diff"])

    with tempfile.TemporaryDirectory(prefix="xdown-") as tmp:
        palette = Path(tmp) / "palette.png"
        # 滤镜图只保留 timing（在 -i 之前），mpdecimate/fps 放滤镜里以保持一致
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *timing,
             "-i", str(src), "-vf", vf_palette, str(palette)])

        lavfi = f"fps={fps},{scale}[x];[x][1:v]paletteuse=dither={dither}"
        if dither == "bayer":
            lavfi += ":bayer_scale=5:diff_mode=rectangle"
        run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *timing,
             "-i", str(src), "-i", str(palette), "-loop", str(loop),
             "-lavfi", lavfi, str(dest)])


def size_human(path: Path) -> str:
    n = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def process_one(
    raw: str,
    args: argparse.Namespace,
    outdir: Path,
    index: int,
) -> list[Path]:
    tweet_id, user = parse_target(raw)
    log(f"\n▶ 处理 {raw}  (id={tweet_id})", C_GRN)

    slug_user = user or "x"
    base = f"{slug_user}_{tweet_id}"
    if index > 0:
        base += f"_{index}"

    produced: list[Path] = []

    # 1) 先拿元数据 + 直链（快，且能判断是不是 GIF）
    media, meta = extract_media(tweet_id)
    if args.video_only:
        media = [m for m in media if m["type"] == "video"]
    if meta.get("text"):
        text = meta["text"]
        log(f"{C_DIM}{text[:120]}{'…' if len(text) > 120 else ''}{C_END}")

    downloaded: list[Path] = []
    if media:
        for i, m in enumerate(media):
            suffix = "" if len(media) == 1 else f"_{i + 1}"
            dest = outdir / f"{base}{suffix}.mp4"
            if args.dry_run:
                log(f"[dry-run] {m['type']} -> {dest}")
                continue
            if dest.exists() and not args.force:
                log(f"{C_YEL}已存在，跳过下载: {dest}{C_END}")
                downloaded.append(dest)
                continue
            try:
                download_direct(m["url"], dest)
                downloaded.append(dest)
            except Exception as e:  # noqa: BLE001
                log(f"{C_YEL}直链下载失败 ({e})，回退 yt-dlp…{C_END}")
                dest.unlink(missing_ok=True)
                if not ytdlp_download(meta["url"], base, outdir, args, downloaded):
                    raise
    else:
        # 2) 回退 yt-dlp（需要登录/受限内容也能用 cookies 跑）
        log(f"{C_DIM}syndication 无直链，回退 yt-dlp…{C_END}")
        if args.dry_run:
            log(f"[dry-run] yt-dlp -> {outdir / (base + '.mp4')}")
            return produced
        if not ytdlp_download(meta["url"], base, outdir, args, downloaded):
            raise XdownError(f"下载失败（无视频或受保护）: {raw}")

    # 3) 写元数据
    if not args.no_metadata and downloaded:
        meta_path = outdir / f"{base}.info.json"
        meta["media"] = media
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        produced.append(meta_path)

    produced.extend(downloaded)

    # 4) 转 GIF
    if args.gif and not args.dry_run:
        for src in downloaded:
            gif_path = src.with_suffix(".gif")
            dur = media_duration(src)
            log(
                f"{C_DIM}转 GIF: {src.name}"
                + (f" ({dur:.1f}s)" if dur else "")
                + f"  {args.fps}fps, w={args.width}, quality={args.quality}, dither={args.dither}"
                + (f", 上限 {args.max_size}MB" if args.max_size else "")
                + C_END
            )
            convert_to_gif(
                src,
                gif_path,
                fps=args.fps,
                width=args.width,
                start=args.start,
                end=args.end,
                quality=args.quality,
                dither=args.dither,
                loop=args.loop,
                max_size_mb=args.max_size,
                mpdecimate=args.mpdecimate,
            )
            produced.append(gif_path)
            if args.rm_video:
                src.unlink(missing_ok=True)
                produced.remove(src)

    return produced


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xdown",
        description="下载 X (Twitter) 视频，并可转成 GIF。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("urls", nargs="*", help="X/Twitter status 链接或纯 tweet ID")
    p.add_argument("--version", action="version", version=f"xdown {__version__}")
    p.add_argument(
        "-c",
        "--clipboard",
        action="store_true",
        help="从剪贴板读取链接（与 --to-clipboard 方向相反）",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="DIR",
        help="输出目录。不指定则默认下载到 ~/Downloads",
    )
    p.add_argument(
        "--to-clipboard",
        action="store_true",
        help="额外把产物放进系统剪贴板（可直接粘贴到聊天窗口）",
    )
    p.add_argument("-f", "--force", action="store_true", help="覆盖已存在文件")
    p.add_argument("--reveal", action="store_true", help="完成后在文件管理器里打开输出目录 (macOS: Finder)")

    g = p.add_argument_group("GIF 选项")
    g.add_argument("--gif", action="store_true", help="下载后转成 GIF")
    g.add_argument("--fps", type=int, default=12, help="GIF 帧率 (默认 12，10-15 较平衡)")
    g.add_argument("--width", type=int, default=480, help="GIF 宽度像素，高度自适应 (默认 480)")
    g.add_argument("--start", type=float, default=None, help="裁剪起点，单位秒")
    g.add_argument("--end", type=float, default=None, help="裁剪终点，单位秒")
    g.add_argument(
        "--quality",
        choices=["low", "medium", "high"],
        default="medium",
        help="调色板颜色数 low=128/medium=192/high=256 (默认 medium)",
    )
    g.add_argument(
        "--dither",
        choices=["bayer", "none"],
        default="bayer",
        help="抖动方式，bayer 体积小、none 更干净 (默认 bayer)",
    )
    g.add_argument("--loop", type=int, default=0, help="循环次数，0=无限 (默认 0)")
    g.add_argument(
        "--max-size",
        type=float,
        default=None,
        metavar="MB",
        help="GIF 体积上限(MB)，超出则自动降 fps/尺寸重试",
    )
    g.add_argument(
        "--mpdecimate",
        action="store_true",
        help="丢弃画面几乎未变的重复帧，可大幅减小体积",
    )
    g.add_argument("--rm-video", action="store_true", help="转 GIF 后删除原视频")

    m = p.add_argument_group("其他")
    m.add_argument("--video-only", action="store_true", help="只下载 video，忽略 animated_gif 类型的媒体")
    m.add_argument("--no-metadata", action="store_true", help="不写 .info.json")
    m.add_argument("--cookies-from-browser", metavar="BROWSER", default=None,
                   help="yt-dlp 回退时使用的浏览器 cookies (chrome/safari/firefox/edge)")
    m.add_argument("--dry-run", action="store_true", help="只显示将要做什么")
    return p


def reveal(path: Path) -> None:
    """在系统文件管理器里定位输出文件。"""
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(path)]
    elif shutil.which("xdg-open"):
        cmd = ["xdg-open", str(path if path.is_dir() else path.parent)]
    else:
        return
    try:
        subprocess.run(cmd, check=False)
    except Exception:  # noqa: BLE001
        pass


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def clipboard_target(produced: list[Path], has_gif: bool) -> Path | None:
    """从产物里挑要放进剪贴板的主文件：优先 GIF，其次视频；多个取最后一个
    （剪贴板会被后续操作覆盖，最后一个才符合直觉）。"""
    exts = (".gif",) if has_gif else (".gif", ".mp4", ".mkv", ".webm", ".mov", ".m4v")
    for p in reversed(produced):
        if p.suffix.lower() in exts:
            return p
    return None


def copy_to_clipboard(path: Path) -> str:
    """把文件放进系统剪贴板。返回所用方式描述，失败抛异常。

    放的是「文件引用」而不是路径文本：粘贴到 Finder = 复制文件，
    粘贴到微信/Slack 等聊天窗口 = 直接发送该文件。
    """
    if sys.platform == "darwin":
        script = f"set the clipboard to (POSIX file {_applescript_str(str(path))})"
        subprocess.run(
            ["osascript", "-e", script], check=True, capture_output=True, text=True
        )
        return "文件引用"

    if sys.platform == "win32":
        ps = f"Set-Clipboard -LiteralPath {_powershell_str(str(path))}"
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True
        )
        return "文件引用"

    # Linux
    uri = path.absolute().as_uri() + "\r\n"
    if shutil.which("wl-copy"):
        subprocess.run(
            ["wl-copy", "--type", "text/uri-list"], input=uri.encode(), check=True
        )
        return "文件引用 (wl-copy)"
    if shutil.which("xclip"):
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i"],
            input=uri.encode(),
            check=True,
        )
        return "文件引用 (xclip)"
    if shutil.which("xsel"):
        subprocess.run(
            ["xsel", "--clipboard", "--input"], input=str(path).encode(), check=True
        )
        return "路径文本 (xsel 不支持文件引用)"
    raise RuntimeError("未找到剪贴板工具（需要 wl-copy / xclip / xsel）")


def default_outdir() -> Path:
    """未指定 -o 时的默认落地目录：Downloads。

    优先尊重 XDG_DOWNLOAD_DIR（Linux），其次 ~/Downloads，都没有则退到当前目录。
    """
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    candidates: list[Path] = []
    if xdg:
        candidates.append(Path(xdg).expanduser())
    candidates.append(Path.home() / "Downloads")
    for c in candidates:
        if c.is_dir():
            return c
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.clipboard:
        try:
            args.urls.append(read_clipboard())
        except XdownError as e:
            log(f"错误: {e}", C_RED)
            return 1
    if not args.urls:
        build_parser().print_help(sys.stderr)
        return 2

    # 目录语义：-o > XDOWN_OUTPUT > 默认 Downloads
    clip_flag = bool(os.environ.get("XDOWN_OUTPUT"))
    chosen = args.output or (os.environ["XDOWN_OUTPUT"] if clip_flag else None)
    outdir = Path(chosen).expanduser() if chosen else default_outdir()
    outdir.mkdir(parents=True, exist_ok=True)
    outdir = outdir.resolve()

    clipboard_out = args.to_clipboard

    ok: list[Path] = []
    failed: list[str] = []
    for i, raw in enumerate(args.urls):
        try:
            ok.extend(process_one(raw, args, outdir, i if len(args.urls) > 1 else 0))
        except subprocess.CalledProcessError as e:
            log(f"命令失败 (exit {e.returncode}): {' '.join(e.cmd)}", C_RED)
            failed.append(raw)
        except XdownError as e:
            log(f"错误: {e}", C_RED)
            failed.append(raw)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"处理失败: {e}", C_RED)
            failed.append(raw)

    if ok:
        log(f"\n✔ 完成，输出到 {outdir}:")
        for p in ok:
            log(f"  {p.name}  ({size_human(p)})")
        if args.reveal:
            reveal(ok[0])

    if clipboard_out and ok:
        target = clipboard_target(ok, args.gif)
        if target is None:
            log(f"{C_YEL}没有可放进剪贴板的产物{C_END}")
        else:
            try:
                how = copy_to_clipboard(target)
                log(f"\n📋 已投入剪贴板 [{how}]: {target.name}")
                skipped = [p.name for p in ok if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".gif") and p != target]
                if skipped:
                    log(f"{C_DIM}（其他产物未进剪贴板: {', '.join(skipped)}）{C_END}")
                hint = f"文件在 {target.parent}。"
                log(f"{C_DIM}直接去目标窗口 Ctrl/Cmd+V 即可。{hint}{C_END}")
            except Exception as e:  # noqa: BLE001
                log(f"{C_YEL}写入剪贴板失败 ({e})，文件仍在 {target}{C_END}")
    if failed:
        log(f"\n✘ {len(failed)} 个失败: {', '.join(failed)}", C_RED)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已中断", C_YEL)
        sys.exit(130)
    except XdownError as e:
        log(f"错误: {e}", C_RED)
        sys.exit(1)