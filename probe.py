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
TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

# ===============================
# 2. 核心功能函数
# ===============================

def has_data_changed(filename):
    """
    检查生成的文件内容是否与仓库中已有的内容不同
    通过比对排序后的内容，确保只有在 IP 或频道变动时才触发
    """
    if not os.path.exists(filename):
        return False

    # 读取本次生成的并排序
    with open(filename, 'r', encoding='utf-8') as f:
        current_content = sorted([line.strip() for line in f if line.strip()])
    
    if not current_content:
        return False

    # 尝试从 Git 获取上一次提交的版本内容
    try:
        # 获取远程 origin/main 分支上的该文件内容
        # 注意：Action 执行 checkout 时通常会 fetch
        cmd = ['git', 'show', f'origin/main:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            if current_content == old_content:
                print(f"ℹ️  内容比对: {filename} 与上版本完全一致。")
                return False
            else:
                print(f"🆕 内容比对: {filename} 已发生变动。")
                return True
        else:
            # 如果文件在远程不存在，视为有变动（新文件）
            print(f"🆕 内容比对: 远程仓库不存在 {filename}，视为首次更新。")
            return True
    except Exception as e:
        print(f"⚠️  比对异常 (默认视为有变动): {e}")
        return True

def fast_probe_stream(line):
    """极速探测：ffprobe 仅判断视频流是否存在"""
    if "," not in line: return False, line, "无效行"
    name, url = line.split(",", 1)
    
    start_time = time.time()
    # 极低探测阈值：1MB/1s 快速识别 UDPXY 转发状态
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
        return False, line, f"   🟡 [超时] {name} | 6s"
    except:
        return False, line, f"   ❌ [异常] {name}"

# ===============================
# 3. 运行逻辑
# ===============================

if __name__ == "__main__":
    print(f"\n{'='*20} 启动极速探测与联动检查 {'='*20}")
    
    # --- 1. 变动检测 (核心需求) ---
    should_trigger = has_data_changed(SOURCE_NONCHECK_FILE)

    # --- 2. 探测环节 ---
    if not os.path.exists(SOURCE_M3U_FILE):
        print("❌ 错误: 找不到 source-m3u.txt"); exit()

    with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
        lines = [l.strip() for l in f if "," in l]

    if not lines:
        print("⚠️ 待测列表为空，停止探测。")
    else:
        print(f"🎬 共 {len(lines)} 条链接，多线程极速探测中...")
        valid_results = []
        log_entries = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(fast_probe_stream, l) for l in lines]
            for f in concurrent.futures.as_completed(futures):
                success, line, log_msg = f.result()
                print(log_msg)
                log_entries.append(log_msg.strip())
                if success:
                    valid_results.append(line)

        # 写入探测报告和 m3u
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"探测报告 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-" * 50 + "\n")
            f.write("\n".join(sorted(log_entries)))

        with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(valid_results)))
        
        print(f"✅ 探测完成，保留 {len(valid_results)} 条有效链接。")

    # --- 3. 联动判定 ---
    if should_trigger and TRIGGER_TOKEN:
        print(f"\n🚀 检测到源数据变动，正在触发联动: {TARGET_REPO}")
        try:
            url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
            r = requests.post(
                url, 
                headers={"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                json={"ref": "main"}
            )
            if r.status_code == 204:
                print(f"   🎉 联动触发成功！")
            else:
                print(f"   ⚠️ 联动失败: {r.status_code} - {r.text}")
        except Exception as e:
            print(f"   ⚠️ 联动请求异常: {e}")
    else:
        if not should_trigger:
            print("\n⏭️  跳过联动：数据内容未发生实质变动。")
        else:
            print("\n⏭️  跳过联动：未配置 TRIGGER_TOKEN。")
