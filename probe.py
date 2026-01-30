import os, subprocess, json, time, concurrent.futures, requests
from datetime import datetime

SOURCE_M3U_FILE, LOG_FILE = "source-m3u.txt", "log.txt"
TARGET_REPO, TRIGGER_TOKEN = "JE668/iptv-api", os.environ.get("PAT_TOKEN", "")

def probe_stream(line):
    if "," not in line: return False, line, "无效行"
    name, url = line.split(",", 1)
    start = time.time()
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-i", url]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=8)
        elapsed = round(time.time() - start, 2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            if any(s.get("codec_type") == "video" for s in data.get("streams", [])):
                return True, line, f"[{name}] {url} | 成功 | {elapsed}s"
        return False, line, f"[{name}] {url} | 失败 | 无流"
    except: return False, line, f"[{name}] {url} | 失败 | 超时"

if __name__ == "__main__":
    if not os.path.exists(SOURCE_M3U_FILE): 
        print(f"❌ 找不到 {SOURCE_M3U_FILE}")
        exit()
        
    with open(SOURCE_M3U_FILE, encoding="utf-8") as f: 
        lines = [l.strip() for l in f if "," in l]
    
    if not lines: exit()

    print(f"🎬 探测 {len(lines)} 条链接...")
    valid_lines, logs = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(probe_stream, l) for l in lines]
        for f in concurrent.futures.as_completed(futures):
            success, line, log_msg = f.result()
            logs.append(log_msg)
            if success: valid_lines.append(line)

    # 写 log.txt
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"探测时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
    
    # 更新 source-m3u.txt (仅保留成功条目)
    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(valid_lines)))

    print(f"✅ 探测完成，有效链接: {len(valid_lines)} 条")
    
    # 联动触发
    if valid_lines and TRIGGER_TOKEN:
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/main.yml/dispatches"
        requests.post(url, headers={"Authorization": f"token {TRIGGER_TOKEN}"}, json={"ref": "main"})
