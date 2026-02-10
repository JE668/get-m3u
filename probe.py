import os, subprocess, time, concurrent.futures, requests
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_IP_FILE, SOURCE_M3U_FILE, SOURCE_NONCHECK_FILE = "source-ip.txt", "source-m3u.txt", "source-m3u-noncheck.txt"
LOG_FILE, TRIGGER_COUNTER_FILE = "log.txt", "trigger_counter.txt"
TARGET_REPO, TARGET_WORKFLOW, TARGET_BRANCH = "JE668/iptv-api", "main.yml", "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def log_section(name, icon="🔸"):
    print(f"\n{icon} {'='*15} {name} {'='*15}")

def has_data_changed(filename):
    log_section("内容变动检测", "🕵️")
    if not os.path.exists(filename): 
        print(f"  ❌ 错误: 本地文件 {filename} 不存在")
        return False
        
    with open(filename, 'r', encoding='utf-8') as f:
        current_content = sorted([line.strip() for line in f if line.strip()])
    
    if not current_content:
        print(f"  ⚠️ 警告: 本次生成的 {filename} 为空")
        return False

    try:
        # 在 GitHub Action 中，HEAD 指向的是本次运行前仓库里的版本
        # main.py 修改了文件但还没 commit，所以 HEAD 里的还是旧的
        cmd = ['git', 'show', f'HEAD:{filename}']
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        
        if result.returncode == 0:
            old_content = sorted([line.strip() for line in result.stdout.splitlines() if line.strip()])
            print(f"  📊 历史版本行数: {len(old_content)} | 本次生成行数: {len(current_content)}")
            
            if current_content == old_content:
                print(f"  ℹ️ 结论: 内容与仓库版本完全一致，无需联动。")
                return False
            else:
                print(f"  🆕 结论: 发现内容变动！")
                return True
        else:
            # 如果 HEAD 里找不到（比如新添加的文件），则视为有变动
            print(f"  🆕 结论: HEAD 记录中不存在 {filename} (可能是新文件)，视为有变动。")
            # 打印一下具体的错误信息，方便调试
            if result.stderr:
                print(f"     [Git Debug]: {result.stderr.strip()}")
            return True
    except Exception as e:
        print(f"  ⚠️ 比对过程出现异常 (默认判定为有变动): {e}")
        return True

def get_trigger_status(current_changed):
    count = 0
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f:
                count = int(f.read().strip())
        except: pass
    
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

def fast_probe_stream(line):
    if "," not in line: return False, line, ""
    name, url = line.split(",", 1)
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-select_streams', 'v:0', '-probesize', '1000000', '-analyzeduration', '1000000', '-i', url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if result.returncode == 0 and "codec_type=video" in result.stdout:
            return True, line, f"  🟢 [有效] | {name}"
        return False, line, f"  🔴 [无流] | {name}"
    except:
        return False, line, f"  🟡 [超时] | {name}"

if __name__ == "__main__":
    # --- 1. 检测变动 ---
    changed = has_data_changed(SOURCE_IP_FILE)
    
    # --- 2. 状态决策 ---
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # --- 3. 探测环节 ---
    if os.path.exists(SOURCE_M3U_FILE):
        with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            log_section(f"开始极速探测 ({len(lines)}条)", "🎬")
            valid_results, log_entries = [], []
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(fast_probe_stream, l) for l in lines]
                for f in concurrent.futures.as_completed(futures):
                    success, line, log_msg = f.result()
                    print(log_msg)
                    log_entries.append(log_msg.strip())
                    if success:
                        valid_results.append(line)

            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"探测报告 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n" + "\n".join(sorted(log_entries)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_results)))
            print(f"✅ 探测结束: 保留 {len(valid_results)} 条有效链接")

    # --- 4. 报告与联动 ---
    log_section("联动决策报告", "⚖️")
    if is_forced:
        print(f"🚨 [强制模式] 连续 {3} 次未更新，执行周期性强制推送。")
    elif changed:
        print(f"✨ [更新模式] 检测到数据变动，执行联动推送。")
    else:
        print(f"⏭️  [跳过模式] 内容一致，暂不触发 (当前计数: {current_count}/3)。")

    if should_trigger and TRIGGER_TOKEN:
        log_section("触发远程联动", "🔗")
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
        headers = {
            "Authorization": f"token {TRIGGER_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "IPTV-Trigger-Script"
        }
        try:
            r = requests.post(url, headers=headers, json={"ref": TARGET_BRANCH}, timeout=10)
            if r.status_code == 204:
                print(f"🎉 成功: {TARGET_REPO} 的 Action 已被激活！")
            else:
                print(f"❌ 失败 ({r.status_code}): {r.text}")
        except Exception as e:
            print(f"⚠️ 联动请求发生异常: {e}")
