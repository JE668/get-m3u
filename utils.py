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
