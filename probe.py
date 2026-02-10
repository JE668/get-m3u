import os
import subprocess
import time
import concurrent.futures
import requests
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_IP_FILE = "source-ip.txt"    # 使用此文件作为变动比对基准
SOURCE_M3U_FILE = "source-m3u.txt"
LOG_FILE = "log.txt"
TRIGGER_COUNTER_FILE = "trigger_counter.txt"
TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TARGET_BRANCH = "master" 
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

# ===============================
# 2. 核心功能函数
# ===============================

def get_trigger_status(current_changed):
    """更新计数器并判定是否需要触发联动"""
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
    """
    对比逻辑：以排序后的 IP 列表为准
    """
    if not os.path.exists(filename): 
        return False
        
    with open(filename, 'r', encoding='utf-8') as f:
        # 读取并排序当前生成的 IP
        current_content = sorted([line.strip() for line in f if line.strip()])
    
    if not current_content:
        return False

    try:
        # 拉取远程信息
        subprocess.run(['git', 'fetch', 'origin', TARGET_BRANCH], capture_output=True)
        
        # 获取远程 master 分支上的 source-ip.txt
        cmd = ['git', 'show', f'origin/{TARGET_BRANCH}:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            
            print(f"📊 IP 列表比对:")
            print(f"   - 远程 IP 数量: {len(old_content)}")
            print(f"   - 本次 IP 数量: {len(current_content)}")
            
            if current_content == old_content:
                print(f"ℹ️ 比对结果: IP 列表完全一致。")
                return False
            else:
                print(f"🆕 比对结果: 发现 IP 变动。")
                return True
        else:
            print(f"🆕 比对结果: 远程不存在 {filename}，视为新资源。")
            return True
    except Exception as e:
        print(f"⚠️ 比对异常: {e}")
        return True

def fast_probe_stream(line):
    """极速检测"""
    if "," not in line: return False, line, ""
    name, url = line.split(",", 1)
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-select_streams', 'v:0', '-probesize', '1000000', '-analyzeduration', '1000000', '-i', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if result.returncode == 0 and "codec_type=video" in result.stdout:
            return True, line, f"   🟢 [有效] {name}"
        return False, line, f"   🔴 [无流] {name}"
    except: return False, line, f"   🟡 [超时] {name}"

def trigger_remote_action():
    if not TRIGGER_TOKEN: return
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
    headers = {"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.post(url, headers=headers, json={"ref": TARGET_BRANCH}, timeout=10)
        if r.status_code == 204: print("🎉 联动触发成功！")
    except: pass

# ===============================
# 3. 运行逻辑
# ===============================
if __name__ == "__main__":
    print(f"\n{'='*20} 启动探测与变动检查 {'='*20}")
    
    # --- 1. 基于 source-ip.txt 进行变动检查 ---
    changed = has_data_changed(SOURCE_IP_FILE)
    
    # 2. 更新计数器并决策
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # 3. 探测 source-m3u.txt
    if os.path.exists(SOURCE_M3U_FILE):
        with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            print(f"\n🎬 正在进行极速探测...")
            valid_results, log_entries = [], []
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(fast_probe_stream, l) for l in lines]
                for f in concurrent.futures.as_completed(futures):
                    success, line, log_msg = f.result()
                    log_entries.append(log_msg.strip())
                    if success: valid_results.append(line)

            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"探测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(log_entries)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_results)))

    # 4. 联动报告
    print(f"\n{'='*10} 联动决策报告 {'='*10}")
    if is_forced: print(f"🚨 [强制触发] 连续 {3} 次未更新，周期性推送。")
    elif changed: print(f"✨ [更新触发] IP 列表已变动，执行推送。")
    else: print(f"⏭️  [跳过联动] IP 列表无变化 (跳过计数: {current_count}/3)。")

    if should_trigger: trigger_remote_action()
