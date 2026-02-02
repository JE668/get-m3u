import os
import subprocess
import json
import time
import concurrent.futures
import requests
import socket
from urllib.parse import urlparse
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
SOURCE_M3U_FILE = "source-m3u.txt"
LOG_FILE = "log.txt"
TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

# IP 信息缓存，防止重复请求 API 导致封禁
IP_CACHE = {}

# ===============================
# 2. 核心功能函数
# ===============================

def get_ip_info(url):
    """获取 IP 的地理位置和运营商信息"""
    try:
        hostname = urlparse(url).hostname
        ip = socket.gethostbyname(hostname)
        if ip in IP_CACHE:
            return IP_CACHE[ip]
        
        # 频率控制：ip-api 限制每分钟45次，设置 1.5s 间隔
        time.sleep(1.5)
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
        if res.get('status') == 'success':
            info = f"{res.get('city','未知')} | {res.get('isp','未知')}"
            IP_CACHE[ip] = info
            return info
    except:
        pass
    return "未知位置 | 未知网络"

def probe_stream_detail(url):
    """使用 ffprobe 获取流详情（分辨率、编码）"""
    # 模拟你提供的程序：增加探测缓存大小设置，提高探测成功率
    cmd = [
        'ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', 
        '-select_streams', 'v:0', '-probesize', '5000000', 
        '-analyzeduration', '5000000', '-i', url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if 'streams' in data and len(data['streams']) > 0:
                v = data['streams'][0]
                return f"{v.get('width','?')}x{v.get('height','?')}"
    except:
        pass
    return None

def test_link_quality(line):
    """
    全方位测试链接质量:
    1. 响应延迟 (Latency)
    2. 下载带宽 (Speed)
    3. 视频详情 (ffprobe)
    4. 地理位置 (Geolocation)
    """
    if "," not in line: return False, line, "无效行"
    name, url = line.split(",", 1)
    
    try:
        # --- 测延迟 (Latency) ---
        start_time = time.time()
        # allow_redirects=True 处理某些跳转源
        resp = requests.get(url, stream=True, timeout=8, allow_redirects=True)
        latency = int((time.time() - start_time) * 1000)
        
        # --- 测速度 (Speed) ---
        # 下载 2 秒钟的数据来计算带宽
        total_data = 0
        speed_start = time.time()
        for chunk in resp.iter_content(chunk_size=1024*256):
            total_data += len(chunk)
            if time.time() - speed_start > 2: # 测速 2 秒
                break
        duration = time.time() - speed_start
        speed = round((total_data * 8) / (duration * 1024 * 1024), 2)
        resp.close()

        # --- 测视频详情 ---
        resolution = probe_stream_detail(url)
        if not resolution:
            return False, line, f"❌ {name} | 失败 | 无法解析视频流"

        # --- 获取地理位置 ---
        geo_info = get_ip_info(url)
        
        log_msg = f"✅ {name} | {resolution} | 延迟:{latency}ms | 速度:{speed}Mbps | {geo_info}"
        return True, line, log_msg

    except Exception as e:
        return False, line, f"❌ {name} | 失败 | 连接错误: {str(e)}"

# ===============================
# 3. 运行逻辑
# ===============================

if __name__ == "__main__":
    print(f"\n{'='*20} 启动深度质量探测 {'='*20}")
    
    if not os.path.exists(SOURCE_M3U_FILE):
        print(f"❌ 找不到文件: {SOURCE_M3U_FILE}")
        exit()

    with open(SOURCE_M3U_FILE, encoding="utf-8") as f:
        lines = [l.strip() for l in f if "," in l]

    if not lines:
        print("⚠️ 待测列表为空。")
        exit()

    print(f"🎬 共 {len(lines)} 条链接，采用多线程测速与探测...")
    valid_results = []
    log_entries = []

    # GitHub Actions 环境下建议 5-8 线程，防止瞬间带宽过载
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(test_link_quality, l) for l in lines]
        for f in concurrent.futures.as_completed(futures):
            success, line, log_msg = f.result()
            print(log_msg)
            log_entries.append(log_msg)
            if success:
                valid_results.append(line)

    # 结果持久化
    print(f"\n{'='*20} 探测结果汇总 {'='*20}")
    print(f"📊 总数: {len(lines)} | 有效: {len(valid_results)} | 失败: {len(lines)-len(valid_results)}")

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"探测报告 | 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 60 + "\n")
        f.write("\n".join(sorted(log_entries)))

    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(valid_results)))

    # 如果有成功结果，则触发联动
    if valid_results and TRIGGER_TOKEN:
        print(f"🚀 正在触发远程联动: {TARGET_REPO}")
        try:
            dispatch_url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
            r = requests.post(
                dispatch_url, 
                headers={"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"},
                json={"ref": "main"}
            )
            print(f"   API 响应: {r.status_code}")
        except:
            print("   ⚠️ 联动请求失败")
