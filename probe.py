import os, subprocess, time, concurrent.futures, requests
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_IP_FILE, SOURCE_M3U_FILE = "source-ip.txt", "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"
LOG_FILE, TRIGGER_COUNTER_FILE = "log.txt", "trigger_counter.txt"
TARGET_REPO, TARGET_BRANCH = "JE668/iptv-api", "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def live_print(content):
    print(content, flush=True)

def has_data_changed(filename):
    live_print(f"::group::🕵️ 内容变动检测 - {filename}")
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current = sorted([l.strip() for l in f if l.strip()])
    
    try:
        # 与本地 Git HEAD (上次提交的版本) 对比
        cmd = ['git', 'show', f'HEAD:{filename}']
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if res.returncode == 0:
            old = sorted([l.strip() for l in res.stdout.splitlines() if l.strip()])
            live_print(f"  📊 历史行数: {len(old)} | 当前行数: {len(current)}")
            if current == old:
                live_print("  ℹ️ 结论: 内容无变动。")
                live_print("::endgroup::"); return False
            live_print("  🆕 结论: 发现内容更新！")
        else: live_print("  🆕 结论: 首次创建文件。")
    except: live_print("  ⚠️ 比对异常，默认视为有变动。")
    live_print("::endgroup::"); return True

def get_trigger_status(changed):
    count = 0
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f: count = int(f.read().strip())
        except: pass
    
    forced = False
    if changed: count = 0; should = True
    else:
        count += 1
        if count >= 3: should = True; count = 0; forced = True
        else: should = False
    
    with open(TRIGGER_COUNTER_FILE, 'w', encoding='utf-8') as f: f.write(str(count))
    return should, count, forced

def fast_probe(line):
    name, url = line.split(",", 1)
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-select_streams', 'v:0', '-probesize', '1000000', '-analyzeduration', '1000000', '-i', url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if res.returncode == 0 and "codec_type=video" in res.stdout:
            return True, line, f"  🟢 [有效] | {name}"
        return False, line, f"  🔴 [无流] | {name}"
    except: return False, line, f"  🟡 [超时] | {name}"

if __name__ == "__main__":
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    if os.path.exists(SOURCE_M3U_FILE):
        with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            live_print(f"::group::🎬 开始极速探测 ({len(lines)}条)")
            valid, logs = [], []
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
                futures = [ex.submit(fast_probe, l) for l in lines]
                for f in concurrent.futures.as_completed(futures):
                    ok, line, msg = f.result()
                    live_print(msg); logs.append(msg.strip())
                    if ok: valid.append(line)
            
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"探测报告 | {datetime.now()}\n" + "\n".join(sorted(logs)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid)))
            live_print(f"✅ 探测结束: 有效链接 {len(valid)} 条"); live_print("::endgroup::")

    live_print("\n⚖️  ========== 联动决策报告 ==========")
    if is_forced: live_print(f"🚨 [强制模式] 已连续 3 次未更新，执行周期性联动。")
    elif changed: live_print(f"✨ [更新模式] 检测到数据变动，执行联动推送。")
    else: live_print(f"⏭️  [跳过模式] 内容一致 (当前计数: {current_count}/3)。")

    if should_trigger and TRIGGER_TOKEN:
        live_print(f"::group::🔗 触发远程联动: {TARGET_REPO}")
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/main.yml/dispatches"
        headers = {"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        try:
            r = requests.post(url, headers=headers, json={"ref": TARGET_BRANCH}, timeout=10)
            live_print(f"🎉 成功: 响应代码 {r.status_code}")
        except: live_print("❌ 联动请求失败")
        live_print("::endgroup::")
