import os, subprocess, time, concurrent.futures, requests
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_IP_FILE = "source-ip.txt"
SOURCE_M3U_FILE = "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"
LOG_FILE, TRIGGER_COUNTER_FILE = "log.txt", "trigger_counter.txt"
TARGET_REPO, TARGET_WORKFLOW, TARGET_BRANCH = "JE668/iptv-api", "main.yml", "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def live_print(content):
    print(content, flush=True)

# ===============================
# 2. 比对与联动逻辑
# ===============================

def has_data_changed(filename):
    live_print(f"::group::🕵️ 内容变动检测 - {filename}")
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current = sorted([l.strip() for l in f if l.strip()])
    try:
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

# ===============================
# 3. 核心：IP代表抽样测速
# ===============================

def fast_ip_probe(ip_port, url_list):
    """
    对单个 IP 进行抽样测试。
    最多尝试该 IP 的前 3 个频道，只要其中 1 个能正常返回视频数据流，即判定整个 IP 有效。
    """
    for test_url in url_list[:3]: # 最多测3个频道
        start_time = time.time()
        try:
            # 开启流式下载，设置 4 秒连接超时
            r = requests.get(test_url, stream=True, timeout=4)
            if r.status_code == 200:
                downloaded = 0
                # 尝试分块下载
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    downloaded += len(chunk)
                    # 只要能下到 128KB 数据，说明组播源正在源源不断输出视频流
                    if downloaded >= 1024 * 128:
                        elapsed = round(time.time() - start_time, 2)
                        return True, ip_port, f"  🟢 [顺畅] {ip_port:<21} | 获取数据成功 ({elapsed}s)"
                    
                    # 超过 5 秒还没下完 128K，强制中断，说明卡顿
                    if time.time() - start_time > 5:
                        break
        except:
            continue # 报错则尝试下一个频道
            
    # 如果 3 个频道全军覆没
    return False, ip_port, f"  🔴 [无流] {ip_port:<21} | 抽测3频道均无视频数据"

# ===============================
# 4. 运行逻辑
# ===============================

if __name__ == "__main__":
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    # 从 noncheck (未检测的全量文件) 中读取数据
    if os.path.exists(SOURCE_NONCHECK_FILE):
        with open(SOURCE_NONCHECK_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        
        if lines:
            ip_to_lines = {}
            ip_to_urls = {}
            for line in lines:
                try:
                    url = line.split(",", 1)[1]
                    ip_port = url.split("/")[2]
                    if ip_port not in ip_to_lines:
                        ip_to_lines[ip_port] = []
                        ip_to_urls[ip_port] =[]
                    ip_to_lines[ip_port].append(line)
                    ip_to_urls[ip_port].append(url)
                except: pass

            live_print(f"::group::🎬 开始按服务器抽测 (共 {len(ip_to_lines)} 个独立 IP)")
            
            valid_ips = set()
            logs =[]
            
            # 提升多线程并发到 50，加快探测速度
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
                futures =[ex.submit(fast_ip_probe, ip, urls) for ip, urls in ip_to_urls.items()]
                for f in concurrent.futures.as_completed(futures):
                    ok, ip, msg = f.result()
                    live_print(msg)
                    logs.append(msg.strip())
                    if ok:
                        valid_ips.add(ip)
            
            # 重建 M3U 文件 (只保留存活 IP 的行)
            valid_m3u_lines =[]
            for ip in valid_ips:
                valid_m3u_lines.extend(ip_to_lines[ip])
            
            # --- 新增日志：明确告知写入成功 ---
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"服务器抽测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
            live_print(f"\n  📝 成功覆写日志: {LOG_FILE}")
            
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_m3u_lines)))
            live_print(f"  📝 成功覆写纯净版链接: {SOURCE_M3U_FILE} (最终保留 {len(valid_m3u_lines)} 条)")
                
            live_print(f"\n✅ 抽测结束: 存活服务器 {len(valid_ips)} 个 | 过滤掉失效服务器 {len(ip_to_lines) - len(valid_ips)} 个")
            live_print("::endgroup::")

    # --- 联动处理 ---
    live_print("\n⚖️  ========== 联动决策报告 ==========")
    if is_forced: live_print(f"🚨[强制模式] 已连续 3 次未更新，执行周期性联动。")
    elif changed: live_print(f"✨ [更新模式] 检测到数据变动，执行联动推送。")
    else: live_print(f"⏭️  [跳过模式] 内容一致 (当前计数: {current_count}/3)。")

    if should_trigger and TRIGGER_TOKEN:
        live_print(f"::group::🔗 触发远程联动: {TARGET_REPO}")
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
        headers = {"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        try:
            r = requests.post(url, headers=headers, json={"ref": TARGET_BRANCH}, timeout=10)
            live_print(f"🎉 成功: 响应代码 {r.status_code}")
        except: live_print("❌ 联动请求失败")
        live_print("::endgroup::")
