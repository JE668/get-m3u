"""get-m3u 公共工具模块"""
import os, sys, tempfile

SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "")

def live_print(content):
    print(content, flush=True, file=sys.stderr)

def write_summary(content):
    """写入 GitHub Actions Job Summary（Markdown 格式，仅 GitHub 环境生效）"""
    if SUMMARY_FILE:
        try:
            with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError:
            pass

def log_section(name, icon="🔹"):
    """打印阶段分割线标题"""
    live_print(f"\n{icon} {'='*15} {name} {'='*15}")

def atomic_write(filepath, content):
    """原子化写入：先写临时文件再 rename，防止中途崩溃产生残缺文件"""
    dir_path = os.path.dirname(filepath) or '.'
    tmp = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                      dir=dir_path, delete=False, suffix='.tmp')
    try:
        tmp.write(content)
        tmp.close()
        os.replace(tmp.name, filepath)
    except Exception:
        try: os.unlink(tmp.name)
        except OSError: pass
        raise


def parse_rtp_entries(rtp_file):
    """读取 RTP 模板文件，返回 [(name, suffix), ...]，suffix 形如 '239.77.1.234:5146'。

    供 main.py 与 probe.py 共用，避免两处重复解析逻辑。
    rtp_file 不存在或无可解析行时返回空列表。
    """
    entries = []
    if not os.path.exists(rtp_file):
        return entries
    with open(rtp_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "," not in line:
                continue
            try:
                name, r_url = line.split(",", 1)
                suffix = r_url.split("://")[1]  # "rtp://239.77.1.234:5146" -> "239.77.1.234:5146"
                entries.append((name, suffix))
            except (ValueError, IndexError):
                continue
    return entries


def build_m3u(rtp_entries, hostports):
    """由 RTP 条目与 hostport 集合拼出标准 M3U 行列表（含 #EXTM3U 头）。

    - rtp_entries: parse_rtp_entries() 的返回值 [(name, suffix), ...]
    - hostports: 可迭代的 'ip:port' 字符串
    返回如 ["#EXTM3U", "#EXTINF:-1,频道名", "http://ip:port/rtp/suffix", ...]
    """
    lines = ["#EXTM3U"]
    for hp in sorted(hostports):
        for name, suffix in rtp_entries:
            lines.append(f"#EXTINF:-1,{name}")
            lines.append(f"http://{hp}/rtp/{suffix}")
    return lines
