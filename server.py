#!/usr/bin/env python3
import requests
import time
import os
import re
import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup

DB_FILE = "inventory.db"
BASE_URL = "https://lxc.lazycat.wiki/cart"
FID = os.getenv('FID', '25')
LOG_FILE = "inventory.log"
DATA_DIR = "server_data"

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, DB_FILE)
LOG_FILE = os.path.join(DATA_DIR, LOG_FILE)

# TG 配置（从环境变量读取，如果未设置则使用默认值）
TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_CHAT_ID = os.getenv('TG_CHAT_ID', '')

# 内存缓存，存储上一次的库存状态
_inventory_cache = {}

def log_message(message):
    """记录日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(log_entry + '\n')

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

def get_servers_inventory():
    """获取所有服务器的库存信息"""
    url = f"{BASE_URL}?fid={FID}"
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        
        # 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        servers = {}
        
        # 找到所有 class="col-sm-6 col-md-4 col-lg-4 col-xl-4 col-xxl-3 d-flex" 的 div
        server_divs = soup.find_all('div', class_='col-sm-6 col-md-4 col-lg-4 col-xl-4 col-xxl-3 d-flex')
        
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
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TG 配置缺失，跳过发送")
        return False
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": message
    }
    
    try:
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            print("✅ TG 通知已发送")
            return True
        else:
            print(f"❌ TG 通知失败: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ TG 通知异常: {e}")
        return False

def monitor_inventory():
    """检查库存变化"""
    global _inventory_cache
    
    try:
        current_state = get_servers_inventory()

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
    
    # 从数据库加载缓存（只执行一次）
    log_message("📥 从数据库加载库存缓存")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT server_name, stock FROM inventory')
    for server_name, stock in cursor.fetchall():
        _inventory_cache[server_name] = stock
    conn.close()
    
    # 第一次执行，测量执行时间
    log_message("--- 第 1 次检查 ---")
    start_time = time.time()
    monitor_inventory()
    first_elapsed = time.time() - start_time
    
    # 根据第一次执行时间计算能跑多少次
    loop_count = max(1, 600 // int(max(first_elapsed, 1) + 1))  # +1 是为了留点余量
    log_message(f"📊 第一次执行耗时 {first_elapsed:.1f} 秒，本次运行将执行 {loop_count} 次检查")
    
    # 执行剩余的循环
    for i in range(1, loop_count):
        log_message(f"--- 第 {i+1} 次检查 ---")
        start_time = time.time()
        monitor_inventory()
        elapsed_time = time.time() - start_time
        log_message(f"⏳ 本次执行耗时 {elapsed_time:.1f} 秒")
    
    log_message(f"✅ 本次运行完成，共执行 {loop_count} 次检查")
