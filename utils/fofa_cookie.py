"""
FOFA Cookie 自动续期工具

功能：
1. 检测当前 cookie 是否有效
2. 使用 headless browser 自动登录获取新 cookie
3. 更新 GitHub Secret

使用方法：
    # 仅检测 cookie 有效性
    python -m utils.fofa_cookie --check

    # 自动续期（需要 headless browser 依赖）
    python -m utils.fofa_cookie --renew

    # 更新 GitHub Secret（需要 gh CLI）
    python -m utils.fofa_cookie --update-secret

环境变量：
    FOFA_USER: FOFA 用户名
    FOFA_PASS: FOFA 密码
    GITHUB_TOKEN: GitHub Personal Access Token
"""

import os
import sys
import time
import subprocess
import requests
from typing import Optional, Tuple

from utils import live_print

FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci&filter_type=last_month"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── Cookie 文件路径（本地缓存） ──
COOKIE_FILE = "data/fofa_cookie.txt"


def _summary(text):
    """写入 GitHub Actions Step Summary（本地运行时静默跳过）"""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


def check_cookie_workflow() -> int:
    """供 GitHub Actions 调用的检查入口：检测 + 输出 Step Summary。

    始终返回 0（不阻断 CI）——任何失败都由 main.py 的爬虫降级兜底。
    """
    cookie = os.environ.get("FOFA_COOKIE", "")
    if not cookie:
        live_print("⚠️ FOFA_COOKIE 未设置")
        _summary("## ⚠️ FOFA Cookie 未设置")
        _summary("")
        _summary("❌ **FOFA_COOKIE Secret 不存在**，将使用爬虫模式")
        _summary("")
        _summary("🔧 **解决方法:**")
        _summary("1. 登录 https://fofa.info")
        _summary("2. F12 → Network → 复制 Cookie 头")
        _summary("3. 创建 Secret: https://github.com/JE668/get-m3u/settings/secrets/actions")
        return 0

    is_valid, msg = check_cookie_valid(cookie)

    if is_valid:
        live_print(f"✅ {msg}")
        _summary("## ✅ FOFA Cookie 健康")
        _summary("")
        _summary("Cookie 状态正常，将使用 FOFA + 爬虫双模式")
    elif "429" in msg:
        live_print(f"⚠️ {msg}")
        _summary("## ⚠️ FOFA 限流")
        _summary("")
        _summary(f"Cookie 状态: {msg} (Too Many Requests)")
        _summary("")
        _summary("💡 **提示:**")
        _summary("- Cookie 本身有效，只是请求过于频繁被限流")
        _summary("- 等待 10-15 分钟后重试即可恢复")
        _summary("- 本次将自动降级为爬虫模式（无 FOFA 依赖）")
    else:
        live_print(f"⚠️ {msg}")
        _summary("## ⚠️ FOFA Cookie 已过期或异常")
        _summary("")
        _summary(f"Cookie 状态: {msg}")
        _summary("")
        _summary("🔧 **手动更新方法:**")
        _summary("1. 登录 https://fofa.info")
        _summary("2. F12 → Network → 复制 Cookie")
        _summary("3. 更新 Secret: https://github.com/JE668/get-m3u/settings/secrets/actions")
        _summary("")
        _summary("💡 **提示:** 即使 Cookie 过期，main.py 会自动降级为爬虫模式")
    return 0


def check_cookie_valid(cookie: str) -> Tuple[bool, str]:
    """
    检测 FOFA cookie 是否有效
    
    返回：(is_valid, message)
    """
    if not cookie:
        return False, "Cookie 为空"
    
    try:
        headers = {**HEADERS, "Cookie": cookie}
        r = requests.get(FOFA_URL, headers=headers, timeout=15)
        
        if r.status_code == 200:
            # 检查是否包含登录页面重定向
            if "login" in r.url.lower() or "signin" in r.url.lower():
                return False, "Cookie 已过期（跳转到登录页）"
            
            # 检查响应内容是否包含搜索结果
            if "udpxy" in r.text.lower() or "result" in r.text.lower():
                return True, "Cookie 有效"
            else:
                return False, "Cookie 可能已过期（响应异常）"
        elif r.status_code == 403:
            return False, "Cookie 已过期（403 Forbidden）"
        elif r.status_code == 401:
            return False, "Cookie 已过期（401 Unauthorized）"
        elif r.status_code == 429:
            return False, "HTTP 429（请求过于频繁，Cookie 本身可能仍有效）"
        else:
            return False, f"HTTP {r.status_code}"
            
    except requests.RequestException as e:
        return False, f"请求失败: {e}"


def login_to_fofa(username: str, password: str) -> Optional[str]:
    """
    使用 headless browser 登录 FOFA 并提取 cookie
    
    依赖：
    - playwright 或 selenium
    - chromium 浏览器
    
    返回：cookie 字符串，失败返回 None
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        live_print("❌ 未安装 playwright，请运行: pip install playwright && playwright install chromium")
        return None
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # 访问登录页
            page.goto("https://fofa.info/account/login", wait_until="networkidle", timeout=30000)
            
            # 填写登录表单
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            
            # 等待登录完成
            page.wait_for_url("**/dashboard**", timeout=30000)
            
            # 提取 cookie
            cookies = page.context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            
            browser.close()
            
            # 验证新 cookie
            is_valid, msg = check_cookie_valid(cookie_str)
            if is_valid:
                live_print(f"✅ FOFA 登录成功，新 cookie 有效")
                return cookie_str
            else:
                live_print(f"⚠️ 登录成功但 cookie 验证失败: {msg}")
                return cookie_str
                
    except Exception as e:
        live_print(f"❌ FOFA 登录失败: {e}")
        return None


def update_github_secret(cookie: str) -> bool:
    """
    更新 GitHub Secret（FOFA_COOKIE）
    
    需要：
    - gh CLI 已安装并认证
    - GITHUB_TOKEN 环境变量（可选，gh CLI 已认证则不需要）
    
    返回：是否成功
    """
    try:
        # 检查 gh CLI 是否可用
        result = subprocess.run(["which", "gh"], capture_output=True, text=True)
        if result.returncode != 0:
            live_print("❌ gh CLI 未安装，无法自动更新 GitHub Secret")
            live_print("   请手动更新: https://github.com/JE668/get-m3u/settings/secrets/actions")
            return False
        
        # 使用 gh CLI 更新 secret
        env = os.environ.copy()
        env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
        
        result = subprocess.run(
            ["gh", "secret", "set", "FOFA_COOKIE", "-r", "JE668/get-m3u", "-b", cookie],
            capture_output=True,
            text=True,
            env=env
        )
        
        if result.returncode == 0:
            live_print("✅ GitHub Secret 已更新")
            return True
        else:
            live_print(f"❌ GitHub Secret 更新失败: {result.stderr}")
            return False
            
    except Exception as e:
        live_print(f"❌ 更新 GitHub Secret 异常: {e}")
        return False


def save_cookie_local(cookie: str) -> None:
    """保存 cookie 到本地文件"""
    try:
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            f.write(cookie)
        live_print(f"📝 Cookie 已保存到 {COOKIE_FILE}")
    except OSError as e:
        live_print(f"⚠️ Cookie 保存失败: {e}")


def load_cookie_local() -> Optional[str]:
    """从本地文件加载 cookie"""
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except OSError:
        pass
    return None


def renew_cookie_auto() -> Optional[str]:
    """
    自动续期 cookie 的主流程
    
    1. 检查环境变量中的 cookie
    2. 如果无效，尝试本地缓存
    3. 如果仍无效，尝试自动登录
    4. 更新 GitHub Secret
    """
    # 1. 检查当前环境变量中的 cookie
    current_cookie = os.environ.get("FOFA_COOKIE", "")
    if current_cookie:
        is_valid, msg = check_cookie_valid(current_cookie)
        live_print(f"🔍 当前 Cookie 状态: {msg}")
        if is_valid:
            return current_cookie
    
    # 2. 尝试本地缓存
    local_cookie = load_cookie_local()
    if local_cookie and local_cookie != current_cookie:
        is_valid, msg = check_cookie_valid(local_cookie)
        if is_valid:
            live_print(f"📦 使用本地缓存 Cookie: {msg}")
            return local_cookie
    
    # 3. 尝试自动登录
    username = os.environ.get("FOFA_USER", "")
    password = os.environ.get("FOFA_PASS", "")
    
    if username and password:
        live_print("🔄 尝试自动登录 FOFA...")
        new_cookie = login_to_fofa(username, password)
        if new_cookie:
            save_cookie_local(new_cookie)
            update_github_secret(new_cookie)
            return new_cookie
    else:
        live_print("⚠️ 未设置 FOFA_USER/FOFA_PASS，无法自动登录")
    
    return None


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="FOFA Cookie 自动续期工具")
    parser.add_argument("--check", action="store_true", help="仅检测 cookie 有效性")
    parser.add_argument("--workflow", action="store_true",
                        help="CI 模式：检测 + 输出 Step Summary（永不阻断，恒返回 0）")
    parser.add_argument("--renew", action="store_true", help="自动续期 cookie")
    parser.add_argument("--update-secret", action="store_true", help="更新 GitHub Secret")

    args = parser.parse_args()

    cookie = os.environ.get("FOFA_COOKIE", "")

    if args.workflow:
        sys.exit(check_cookie_workflow())
    if args.check:
        if not cookie:
            print("❌ FOFA_COOKIE 环境变量未设置")
            sys.exit(1)
        is_valid, msg = check_cookie_valid(cookie)
        print(f"{'✅' if is_valid else '❌'} {msg}")
        sys.exit(0 if is_valid else 1)
    
    elif args.renew:
        new_cookie = renew_cookie_auto()
        if new_cookie:
            print(f"✅ Cookie 已更新: {new_cookie[:20]}...")
            sys.exit(0)
        else:
            print("❌ Cookie 续期失败")
            sys.exit(1)
    
    elif args.update_secret:
        if not cookie:
            cookie = load_cookie_local()
        if cookie:
            success = update_github_secret(cookie)
            sys.exit(0 if success else 1)
        else:
            print("❌ 无可用 cookie")
            sys.exit(1)
    
    else:
        # 默认：检测 + 续期
        cookie = os.environ.get("FOFA_COOKIE", "")
        if cookie:
            is_valid, msg = check_cookie_valid(cookie)
            print(f"当前 Cookie: {msg}")
            if not is_valid:
                print("尝试自动续期...")
                new_cookie = renew_cookie_auto()
                if new_cookie:
                    print(f"✅ Cookie 已更新")
                    sys.exit(0)
                else:
                    print("❌ 续期失败，请手动更新 FOFA_COOKIE secret")
                    sys.exit(1)
        else:
            print("❌ FOFA_COOKIE 未设置")
            sys.exit(1)


if __name__ == "__main__":
    main()
