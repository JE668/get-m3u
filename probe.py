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
    对比逻辑：严格比对排序后的内容。
    增加 Git Fetch 确保远程分支可见。
    """
    if not os.path.exists(filename): 
        print(f"⚠️ 文件 {filename} 不存在")
        return False
        
    with open(filename, 'r', encoding='utf-8') as f:
        current_content = sorted([line.strip() for line in f if line.strip()])
    
    if not current_content:
        print(f"⚠️ 文件 {filename} 为空")
        return False

    try:
        # 在 Action 环境中，显式拉取远程分支信息，确保 origin/master 可用
        subprocess.run(['git', 'fetch', 'origin', TARGET_BRANCH], capture_output=True)
        
        # 获取远程 master 分支上的内容
        cmd = ['git', 'show', f'origin/{TARGET_BRANCH}:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            
            # 调试信息：输出行数对比
            print(f"📊 内容比对详细日志:")
            print(f"   - 远程版本行数: {len(old_content)}")
            print(f"   - 本次生成行数: {len(current_content)}")
            
            if current_content == old_content:
                print(f"ℹ️ 检测结果: 内容完全一致，未发生实质变动。")
                return False
            else:
                # 找出差异（调试用）
                diff_count = abs(len(current_content) - len(old_content))
                print(f"🆕 检测结果: 内容存在差异 (行数差异: {diff_count})。")
                return True
        else:
            print(f"🆕 检测结果: 远程分支不存在该文件，视为首次发布。")
            return True
    except Exception as e:
        print(f"⚠️ 比对过程出现异常: {e}")
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
    if not TRIGGER_TOKEN:
        print("⚠️ 未发现 PAT_TOKEN，联动跳过。")
        return
    url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
    headers = {"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.post(url, headers=headers, json={"ref": TARGET_BRANCH}, timeout=10)
        if r.status_code == 204:
            print("🎉 成功：目标仓库 Action 已唤醒！")
        else:
            print(f"❌ 触发失败 ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"⚠️ 联动异常: {e}")

# ===============================
# 3. 运行逻辑
# ===============================
if __name__ == "__main__":
    print(f"\n{'='*20} 启动探测与联动检查 {'='*20}")
    
    # 1. 检查数据变动情况
    changed = has_data_changed(SOURCE_NONCHECK_FILE)
    
    # 2. 计算触发状态
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # 3. 执行探测并更新 source-m3u.txt
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
                    print(log_msg)
                    log_entries.append(log_msg.strip())
                    if success: valid_results.append(line)

            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"探测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(log_entries)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_results)))

    # 4. 最终决策报告
    print(f"\n{'='*10} 联动决策报告 {'='*10}")
    if is_forced:
        print(f"🚨 [强制模式] 数据连续 {3} 次未变动，执行周期性强制推送。")
    elif changed:
        print(f"✨ [更新模式] 数据内容发生变动，执行推送。")
    else:
        print(f"⏭️  [跳过模式] 内容一致，暂不联动 (当前跳过计数: {current_count}/3)。")

    if should_trigger:
        trigger_remote_action()
