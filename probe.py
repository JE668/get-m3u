import os
import subprocess
import time
import concurrent.futures
import requests
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_M3U_FILE = "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"
LOG_FILE = "log.txt"
TRIGGER_COUNTER_FILE = "trigger_counter.txt"
TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TARGET_BRANCH = "master"  # <--- 已改为你的实际分支名
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

# ===============================
# 2. 核心功能函数
# ===============================

def get_trigger_status(current_changed):
    """更新计数器逻辑"""
    count = 0
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f:
                count = int(f.read().strip())
        except: count = 0

    forced = False
    if current_changed:
        count = 0
        should_trigger = True
    else:
        count += 1
        if count >= 3:
            should_trigger = True
            count = 0
            forced = True
        else:
            should_trigger = False

    with open(TRIGGER_COUNTER_FILE, 'w', encoding='utf-8') as f:
        f.write(str(count))
    return should_trigger, count, forced

def has_data_changed(filename):
    """内容比对逻辑：对比远程仓库 master 分支"""
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current_content = sorted([line.strip() for line in f if line.strip()])
    if not current_content: return False

    try:
        # 强制与远程 master 分支上的旧文件比对
        cmd = ['git', 'show', f'origin/{TARGET_BRANCH}:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            if current_content == old_content:
                print(f"ℹ️  内容检测: {filename} 与远程 master 分支一致。")
                return False
            else:
                print(f"🆕 内容检测: {filename} 较远程分支有变动。")
                return True
        return True # 远程不存在则视为有变动
    except: return True

def fast_probe_stream(line):
    """极速探测"""
    name, url = line.split(",", 1)
    # 使用 1MB/1s 采样，快速判断
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-select_streams', 'v:0', '-probesize', '1000000', '-analyzeduration', '1000000', '-i', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if result.returncode == 0 and "codec_type=video" in result.stdout:
            return True, line, f"   🟢 [有效] {name}"
        return False, line, f"   🔴 [无流] {name}"
    except: return False, line, f"   🟡 [超时] {name}"

def trigger_remote_action():
    """发送联动信号"""
    if not TRIGGER_TOKEN:
        print("⚠️ 未发现 PAT_TOKEN，联动跳过。")
        return
    
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
    headers = {
        "Authorization": f"token {TRIGGER_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "IPTV-Trigger-Script"
    }
    data = {"ref": TARGET_BRANCH}
    
    print(f"🚀 正在触发 {TARGET_REPO} 的 {TARGET_WORKFLOW} (分支: {TARGET_BRANCH})...")
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code == 204:
            print("🎉 成功：目标仓库 Action 已被唤醒！")
        else:
            print(f"❌ 触发失败 ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"⚠️ 联动请求异常: {e}")

# ===============================
# 3. 运行逻辑
# ===============================
if __name__ == "__main__":
    print(f"\n{'='*20} 启动探测与计数检查 {'='*20}")
    
    # 1. 检查数据变动并计算触发决策
    changed = has_data_changed(SOURCE_NONCHECK_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # 2. 执行 ffprobe 探测
    if os.path.exists(SOURCE_M3U_FILE):
        with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            print(f"🎬 共 {len(lines)} 条链接，执行极速检测...")
            valid_results, log_entries = [], []
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(fast_probe_stream, l) for l in lines]
                for f in concurrent.futures.as_completed(futures):
                    success, line, log_msg = f.result()
                    print(log_msg)
                    log_entries.append(log_msg.strip())
                    if success: valid_results.append(line)

            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"探测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(log_entries)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_results)))

    # 3. 联动决策输出
    print(f"\n{'='*10} 联动决策报告 {'='*10}")
    if is_forced:
        print(f"🚨 [强制触发] 已连续 {3} 次未变动，执行周期性联动。")
    elif changed:
        print(f"✨ [更新触发] 检测到数据变动，执行联动。")
    else:
        print(f"⏭️  [跳过联动] 数据未变动 (当前跳过计数: {current_count}/3)。")

    if should_trigger:
        trigger_remote_action()
