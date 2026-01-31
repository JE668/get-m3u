import os, subprocess, json, time, concurrent.futures, requests
from datetime import datetime

SOURCE_M3U_FILE, LOG_FILE = "source-m3u.txt", "log.txt"
TARGET_REPO, TRIGGER_TOKEN = "JE668/iptv-api", os.environ.get("PAT_TOKEN", "")

def probe_stream(line):
    name, url = line.split(",", 1)
    start = time.time()
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-i", url]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=8)
        elapsed = round(time.time() - start, 2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            if v:
                res_str = f"{v.get('width')}x{v.get('height')}"
                return True, line, f"   🟢 [成功] {name} | {res_str} | {elapsed}s"
        return False, line, f"   🔴 [失败] {name} | 无视频流"
    except:
        return False, line, f"   🟡 [超时] {name} | 8s未响应"

if __name__ == "__main__":
    print(f"\n{'='*20} FFMPEG 深度探测 {'='*20}")
    if not os.path.exists(SOURCE_M3U_FILE):
        print("❌ 错误: 找不到 source-m3u.txt"); exit()

    with open(SOURCE_M3U_FILE, encoding="utf-8") as f: 
        lines = [l.strip() for l in f if "," in l]

    if not lines:
        print("⚠️  待测链接为空，跳过探测"); exit()

    print(f"🎬 开始探测 {len(lines)} 条链接，请稍候...")
    valid_lines, logs, success_count = [], [], 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(probe_stream, l) for l in lines]
        for f in concurrent.futures.as_completed(futures):
            success, line, log_msg = f.result()
            print(log_msg)
            logs.append(log_msg.strip())
            if success:
                valid_lines.append(line)
                success_count += 1

    # 结果归档
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"探测报告 | 时间: {datetime.now()}\n{'='*50}\n")
        f.write("\n".join(sorted(logs)))
    
    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(valid_lines)))

    print(f"\n📈 探测总结:")
    print(f"   - 总测试数: {len(lines)}")
    print(f"   - 成功通过: {success_count}")
    print(f"   - 过滤比例: {round((1 - success_count/len(lines))*100, 1)}%")

    if valid_lines and TRIGGER_TOKEN:
        print(f"\n🚀 正在发送联动信号至 {TARGET_REPO}...")
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/main.yml/dispatches"
        r = requests.post(url, headers={"Authorization": f"token {TRIGGER_TOKEN}"}, json={"ref": "main"})
        print(f"   API 响应状态: {r.status_code}")
