#!/usr/bin/env python3
import requests
import time
import os
import re
import sqlite3
import json
import subprocess
import signal
import socket
import math
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote

DB_FILE = "inventory.db"
BASE_URL = "https://lxc.lazycat.wiki/cart"
FID = os.getenv('FID', '25')
LOG_FILE = "inventory.log"
DATA_DIR = "server_data"
SOCKS_PORT = 51080


TIME_INT=1800

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, DB_FILE)
LOG_FILE = os.path.join(DATA_DIR, LOG_FILE)

# TG 配置（从环境变量读取，如果未设置则使用默认值）
TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '')
HY2_PROXY_URL = os.getenv('HY2_PROXY_URL', '')

# 内存缓存，存储上一次的库存状态
_inventory_cache = {}
_tg_message_count = 0  # TG 消息计数

def log_message(message):
    """记录日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(log_entry + '\n')

class Hy2Proxy:
    """Hysteria2 代理管理器"""
    def __init__(self, url: str):
        self.url = url
        self.proc = None

    def start(self) -> bool:
        log_message("📡 启动 Hysteria2…")

        u = self.url.replace("hysteria2://", "").replace("hy2://", "")
        parsed = urlparse("scheme://" + u)
        params = parse_qs(parsed.query)

        # 处理 insecure 参数（支持 insecure 和 allowInsecure）
        insecure_val = params.get("insecure", params.get("allowInsecure", ["0"]))[0]
        insecure = insecure_val == "1"

        cfg = {
            "server": f"{parsed.hostname}:{parsed.port}",
            "auth": unquote(parsed.username),
            "tls": {
                "sni": params.get("sni", [parsed.hostname])[0],
                "insecure": insecure,
                "alpn": params.get("alpn", ["h3"]),
            },
            "socks5": {"listen": f"127.0.0.1:{SOCKS_PORT}"}
        }

        cfg_path = "/tmp/hy2.json"
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)

        try:
            self.proc = subprocess.Popen(
                ["hysteria", "client", "-c", cfg_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except FileNotFoundError:
            log_message("❌ hysteria 命令未找到，请先安装 Hysteria2")
            return False

        for _ in range(12):
            time.sleep(1)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", SOCKS_PORT)) == 0:
                    log_message("✅ Hy2 SOCKS5 已就绪")
                    return True
        return False

    def stop(self):
        if self.proc:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            log_message("🛑 Hy2 已停止")

    @property
    def proxy(self):
        return f"socks5://127.0.0.1:{SOCKS_PORT}"

def get_proxy_manager():
    """根据环境变量判断是否需要使用代理"""
    if HY2_PROXY_URL:
        return Hy2Proxy(HY2_PROXY_URL)
    return None

def start_proxy_with_retry(max_retries=3):
    """启动代理，失败时重试"""
    if not HY2_PROXY_URL:
        log_message("⚠️ 未配置代理 URL，使用直连模式")
        return None, None
    
    proxy_manager = get_proxy_manager()
    proxy_url = None
    
    if not proxy_manager:
        log_message("⚠️ 代理管理器初始化失败，使用直连模式")
        return None, None
    
    for attempt in range(1, max_retries + 1):
        log_message(f"🔄 尝试启动代理 ({attempt}/{max_retries})...")
        if proxy_manager.start():
            proxy_url = proxy_manager.proxy
            log_message(f"✅ 代理已启动：{proxy_url}")
            return proxy_manager, proxy_url
        else:
            if attempt < max_retries:
                log_message(f"⏳ 等待 5 秒后重试...")
                time.sleep(5)
            else:
                log_message("⚠️ 代理启动失败，继续使用直连模式")
    
    return None, None

def check_ip(proxy: str = None) -> str:
    """检查落地 IP，明确指出是否使用了代理"""
    try:
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}
        r = requests.get(
            "http://ip-api.com/json/?fields=status,query,countryCode",
            proxies=proxies,
            timeout=30
        ).json()
        if r.get("status") == "success":
            masked_ip = mask_ip(r['query'])
            ip_str = f"{masked_ip} ({r['countryCode']})"
            mode = "✅ 代理" if proxy else "⚠️ 直连"
            return f"{ip_str} [{mode}]"
    except Exception:
        pass
    mode = "✅ 代理" if proxy else "⚠️ 直连"
    return f"未知 IP [{mode}]"

def mask_ip(ip: str) -> str:
    """脱敏 IP 地址"""
    return ip.rsplit(".", 1)[0] + ".***"

def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_name TEXT NOT NULL UNIQUE,
            stock INTEGER NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_previous_stock(server_name, use_cache=True):
    """获取上一次的库存"""
    # 优先从缓存读取
    if use_cache and server_name in _inventory_cache:
        return _inventory_cache[server_name]
    
    # 缓存中没有，从数据库读取
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT stock FROM inventory WHERE server_name = ?', (server_name,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def update_stock(server_name, stock):
    """更新库存"""
    # 更新内存缓存
    _inventory_cache[server_name] = stock
    
    # 更新数据库
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO inventory (server_name, stock) VALUES (?, ?)
        ON CONFLICT(server_name) DO UPDATE SET stock = ?, updated_at = CURRENT_TIMESTAMP
    ''', (server_name, stock, stock))
    conn.commit()
    conn.close()

def get_servers_inventory(proxy_url=None):
    """获取所有服务器的库存信息"""
    url = f"{BASE_URL}?fid={FID}"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Referer': 'https://lxc.lazycat.wiki/',
        }
        
        # 使用传入的代理
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        
        response = requests.get(url, headers=headers, timeout=10, proxies=proxies)
        response.encoding = 'utf-8'
        
        # 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        servers = {}
        
        # 找到所有 class="col-sm-6 col-md-4 col-lg-4 col-xl-4 col-xxl-3 d-flex" 的 div
        server_divs = soup.find_all('div', class_='col-sm-6 col-md-4 col-lg-4 col-xl-4 col-xxl-3 d-flex')
        
        if not server_divs:
            log_message(f"⚠️ 未找到服务器 div，HTML 长度: {len(response.text)}")
        
        for div in server_divs:
            try:
                # 提取服务器名称 <h4>
                h4 = div.find('h4')
                if not h4:
                    continue
                server_name = h4.get_text(strip=True)
                
                # 提取库存 <p class="card-text">库存： 0</p>
                p_tags = div.find_all('p', class_='card-text')
                stock = 0
                for p in p_tags:
                    text = p.get_text(strip=True)
                    if '库存' in text:
                        # 提取数字
                        match = re.search(r'库存[：:]\s*(\d+)', text)
                        if match:
                            stock = int(match.group(1))
                            log_message(f"  ✅网站获取到 {server_name}: 库存 {stock}")
                        break
                
                servers[server_name] = stock
            except Exception as e:
                print(f"⚠️ 解析服务器信息失败: {e}")
                continue
        
        return servers
    except Exception as e:
        print(f"❌ 获取库存失败: {e}")
        return None

def send_tg_notification(message):
    """发送 TG 通知"""
    global _tg_message_count
    
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TG 配置缺失，跳过发送")
        return False
    
    # 检查是否超过限制
    if _tg_message_count > 3:
        log_message("⚠️ TG 消息已达到限制（3 条），跳过发送以避免风控")
        return False
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": message
    }
    
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            _tg_message_count += 1
            log_message(f"✅ TG 通知已发送 ({_tg_message_count}/3)")
            return True
        else:
            log_message(f"❌ TG 通知失败: {response.status_code}")
            return False
    except Exception as e:
        log_message(f"❌ TG 通知异常: {e}")
        return False

def monitor_inventory(proxy_url=None):
    """检查库存变化"""
    global _inventory_cache
    
    try:
        current_state = get_servers_inventory(proxy_url)

        if current_state is None:
            log_message(f"❌ 获取库存失败")
            return

        log_message(f"✅ 当前库存: {current_state}")

        # 检查库存变化
        changes = []

        for server_name, current_stock in current_state.items():
            previous_stock = _inventory_cache.get(server_name)

            # 第一次执行或库存从 0 变到非 0，或从非 0 变到 0 时发送通知
            if previous_stock is None:
                # 第一次执行，记录初始库存
                if current_stock > 0:
                    changes.append(f"📊 {server_name}: 库存 {current_stock}")
                    log_message(f"  📊 {server_name}: 初始库存 {current_stock}")
                    update_stock(server_name, current_stock)
                else:
                    log_message(f"  ℹ️ {server_name}: 初始库存 0（无货）")
                    update_stock(server_name, current_stock)
            elif previous_stock == 0 and current_stock > 0:
                # 从 0 变到 > 0（有货了）
                changes.append(f"🎉 {server_name}: 0 → {current_stock} (有货了！)")
                log_message(f"  🎉 {server_name}: 0 → {current_stock} (有货了！)")
                update_stock(server_name, current_stock)
            elif previous_stock > 0 and current_stock == 0:
                # 从 > 0 变到 0（售罄了）
                changes.append(f"❌ {server_name}: {previous_stock} → 0 (售罄了)")
                log_message(f"  ❌ {server_name}: {previous_stock} → 0 (售罄了)")
                update_stock(server_name, current_stock)
            else:
                # 库存无变化
                log_message(f"  ℹ️ {server_name}: 库存 {current_stock}（无变化）")

        # 如果有变化，发送通知
        if changes:
            message = f"📢 库存变化通知\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            message += "\n".join(changes)
            message += f"\n\n链接: {BASE_URL}?fid={FID}"
            send_tg_notification(message)
            log_message(f"📢 发送通知: {len(changes)} 条变化")
        else:
            log_message("ℹ️ 无库存变化")

    except Exception as e:
        log_message(f"❌ 监控异常: {e}")

if __name__ == "__main__":
    init_db()  # 初始化数据库
    
    # 启动代理（带重试）
    proxy_manager, proxy_url = start_proxy_with_retry()
    
    # 检查 IP 信息
    log_message(f"🔍 正在检查 IP 信息（使用代理: {bool(proxy_url)})...")
    ip_info = check_ip(proxy_url)
    log_message(f"🌐 IP 信息：{ip_info}")
    
    # 从数据库加载缓存（只执行一次）
    log_message("📥 从数据库加载库存到内存缓存")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT server_name, stock FROM inventory')
    for server_name, stock in cursor.fetchall():
        _inventory_cache[server_name] = stock
        log_message(f"  📦 {server_name}: 库存 {stock}")
    conn.close()
    
    try:
        # 第一次执行，测量执行时间
        log_message("--- 第 1 次检查 ---")
        start_time = time.time()
        monitor_inventory(proxy_url)
        first_elapsed = time.time() - start_time
        
        # 根据第一次执行时间计算预估循环次数
        interval = math.ceil(max(first_elapsed, 1))  # 向上取整
        estimated_loop_count = max(1, TIME_INT // interval)
        log_message(f"📊 第一次执行耗时 {first_elapsed:.1f} 秒，执行间隔 {interval} 秒，预估执行 {estimated_loop_count} 次检查")
        
        # 累计运行时间
        total_elapsed = first_elapsed
        loop_count = 1
        
        # 第一次执行后的等待
        wait_time = interval - first_elapsed
        if wait_time > 0:
            log_message(f"⏳ 等待 {wait_time:.1f} 秒...")
            time.sleep(wait_time)
            total_elapsed = interval
        
        # 执行循环，直到累计时间 >= 1800 秒
        while total_elapsed < TIME_INT:
            loop_count += 1
            log_message(f"--- 第 {loop_count} 次检查，已累计运行 {total_elapsed:.1f} 秒 ---")
            start_time = time.time()
            monitor_inventory(proxy_url)
            elapsed_time = time.time() - start_time
            log_message(f"⏳ 本次执行耗时 {elapsed_time:.1f} 秒")
            
            # 计算需要等待的时间
            wait_time = interval - elapsed_time
            if wait_time > 0:
                log_message(f"⏳ 等待 {wait_time:.1f} 秒...")
                time.sleep(wait_time)
                total_elapsed += interval
            else:
                # 执行时间超过间隔，按向上取整计算
                total_elapsed += math.ceil(elapsed_time)
            
            log_message(f"📊 累计运行时间: {total_elapsed:.1f} 秒")
        
        log_message(f"✅ 本次运行完成，共执行 {loop_count} 次检查，累计运行时间 {total_elapsed:.1f} 秒")
    finally:
        # 停止代理
        if proxy_manager:
            proxy_manager.stop()
