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
TRIGGER_COUNTER_FILE = "trigger_counter.txt"  # 新增：记录连续跳过次数的文件
TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

# ===============================
# 2. 核心功能函数
# ===============================

def get_trigger_status(current_changed):
    """
    更新计数器并判定是否需要触发联动
    返回值: (should_trigger, current_count, is_forced)
    """
    # 1. 读取旧计数
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f:
                count = int(f.read().strip())
        except:
            count = 0
    else:
        count = 0

    forced = False
    if current_changed:
        # 数据有变动，直接触发，计数器归零
        count = 0
        should_trigger = True
    else:
        # 数据无变动，计数器自增
        count += 1
        if count >= 3:
            # 达到3次，强制触发，计数器归零
            should_trigger = True
            count = 0
            forced = True
        else:
            # 未达到3次，不触发
            should_trigger = False

    # 2. 保存新计数
    with open(TRIGGER_COUNTER_FILE, 'w', encoding='utf-8') as f:
        f.write(str(count))
    
    return should_trigger, count, forced

def has_data_changed(filename):
    """对比内容是否与仓库版本一致"""
    if not os.path.exists(filename):
        return False

    with open(filename, 'r', encoding='utf-8') as f:
        current_content = sorted([line.strip() for line in f if line.strip()])
    
    if not current_content:
        return False

    try:
        # 获取远程 origin/main 分支内容 (请根据你的分支名修改 main/master)
        cmd = ['git', 'show', f'origin/main:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            if current_content == old_content:
                print(f"ℹ️  内容检测: {filename} 未发生变动。")
                return False
            else:
                print(f"🆕 内容检测: {filename} 已发生变动。")
                return True
        else:
            print(f"🆕 内容检测: 远程不存在 {filename}，视为新文件。")
            return True
    except Exception as e:
        print(f"⚠️  比对异常: {e}")
        return True

def fast_probe_stream(line):
    """极速探测：仅判断视频流是否存在"""
    if "," not in line: return False, line, "无效行"
    name, url = line.split(",", 1)
    start_time = time.time()
    cmd = [
        'ffprobe', '-v', 'error', 
        '-show_streams', '-select_streams', 'v:0', 
        '-probesize', '1000000', 
        '-analyzeduration', '1000000', 
        '-i', url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        elapsed = round(time.time() - start_time, 2)
        if result.returncode == 0 and "codec_type=video" in result.stdout:
            return True, line, f"   🟢 [有效] {name} | {elapsed}s"
        else:
            return False, line, f"   🔴 [无流] {name} | {elapsed}s"
    except subprocess.TimeoutExpired:
        return False, line, f"   🟡 [超时] {name}"
    except:
        return False, line, f"   ❌ [异常] {name}"

# ===============================
# 3. 运行逻辑
# ===============================

if __name__ == "__main__":
    print(f"\n{'='*20} 启动探测与计数检查 {'='*20}")
    
    # 1. 检查数据变动情况
    changed = has_data_changed(SOURCE_NONCHECK_FILE)
    
    # 2. 更新计数器并获取触发决策
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # 3. 执行探测
    if not os.path.exists(SOURCE_M3U_FILE):
        print("❌ 错误: 找不到 source-m3u.txt"); exit()

    with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
        lines = [l.strip() for l in f if "," in l]

    if lines:
        print(f"🎬 共 {len(lines)} 条链接，开始多线程极速探测...")
        valid_results, log_entries = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(fast_probe_stream, l) for l in lines]
            for f in concurrent.futures.as_completed(futures):
                success, line, log_msg = f.result()
                print(log_msg)
                log_entries.append(log_msg.strip())
                if success: valid_results.append(line)

        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"探测报告 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-" * 50 + "\n")
            f.write("\n".join(sorted(log_entries)))

        with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(valid_results)))
        print(f"✅ 探测完成，保留 {len(valid_results)} 条有效链接。")

    # 4. 联动推送逻辑
    print(f"\n{'='*10} 联动状态报告 {'='*10}")
    if is_forced:
        print(f"🚨 [强制触发] 数据已连续 {3} 次未变动，执行周期性强制推送。")
    elif changed:
        print(f"✨ [正常触发] 检测到数据更新，执行推送。")
    else:
        print(f"⏭️  [跳过联动] 数据未变动 (当前连续跳过次数: {current_count}/3)。")

    if should_trigger and TRIGGER_TOKEN:
        print(f"🚀 正在发送联动信号至: {TARGET_REPO}")
        try:
            url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
            r = requests.post(
                url, 
                headers={"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                json={"ref": "main"}
            )
            if r.status_code == 204:
                print(f"   🎉 联动信号发送成功！")
            else:
                print(f"   ⚠️ 联动失败: {r.status_code}")
        except Exception as e:
            print(f"   ⚠️ 异常: {e}")
