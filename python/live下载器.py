#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TVBox 直播源聚合下载器。

功能：
1. 扫描 tvbox/ 下所有接口 JSON，从 lives 数组提取有 name+url 的全部条目
2. 不限制 type，不做任何过滤，URL 去重
3. 模拟 TVBox 环境下载（UA 伪装、指纹），失败重试
4. 套壳自动展开（最多5层），最终层写出播放列表文件
5. 文件名净化：只保留中文/英文/数字，后缀不受影响
6. livelist.txt 第一列与磁盘真实文件名完全对齐
7. 不写伴随 txt 文件，UA 为空输出 null
"""

import ipaddress
import json
import re
import time
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urlunparse

# ---------- 请求配置（模拟 TVBox 环境） ----------
TVBOX_UAS = [
    "okhttp/3.12.13",
    "Mozilla/5.0 (Linux; Android 9; Pixel 3 XL Build/PQ3A.190801.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.120 Mobile Safari/537.36",
    "VLC/3.0.16 LibVLC/3.0.16",
    "AppleCoreMedia/1.0.0.19C56 (iPhone; U; CPU OS 15_2 like Mac OS X; en_us)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "bingcha/1.1 (mianfeifenxiang)",
]
TVBOX_HEADERS = {
    "X-Requested-With": "com.fongmi.android.tv",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ---------- 路径配置 ----------
REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = REPO_ROOT / "tvbox"
OUTPUT_LIVE_DIR = SCAN_DIR / "live"
AGGREGATE_JSON = SCAN_DIR / "海量直播线路.json"
LIVELIST_PATH = REPO_ROOT / "livelist.txt"

DOWNLOAD_TIMEOUT = 25
MAX_RETRIES = 3
MAX_UNWRAP_DEPTH = 5
TODAY = datetime.now().strftime("%Y%m%d")
DEBUG = False


# ---------- 工具函数 ----------
def normalize_url(url):
    try:
        p = urlparse(url.strip())
        return urlunparse(p._replace(
            scheme=p.scheme.lower(), netloc=p.netloc.lower(),
            path=p.path.rstrip("/"), fragment=""))
    except Exception:
        return url.strip()


def format_file_size(n):
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def get_unique_name(base, used):
    if base not in used:
        used.add(base)
        return base
    idx = 1
    while True:
        name = f"{base}{idx}线"
        if name not in used:
            used.add(name)
            return name
        idx += 1


def sanitize_filename(name):
    """只净化 stem，保留后缀。stem 只保留中文/英文/数字。"""
    p = Path(name)
    stem = p.stem
    suffix = p.suffix
    clean_stem = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', stem)
    clean_stem = clean_stem.strip()
    if not clean_stem:
        clean_stem = "live"
    return clean_stem + suffix


def ensure_suffix(name):
    """无后缀补 .txt"""
    if Path(name).suffix:
        return name
    return name + ".txt"


# ---------- 下载相关 ----------
def build_headers(ua):
    headers = dict(TVBOX_HEADERS)
    headers["User-Agent"] = (
        ua.strip() if ua and isinstance(ua, str) and ua.strip()
        else TVBOX_UAS[int(time.time()) % len(TVBOX_UAS)]
    )
    return headers


def fetch_url(url, ua, timeout=DOWNLOAD_TIMEOUT):
    """模拟 TVBox 指纹请求，带重试。"""
    headers = build_headers(ua)
    last_exc = None
    for retry in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout,
                                 allow_redirects=True, verify=True)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_exc = e
            if DEBUG and retry == MAX_RETRIES - 1:
                import traceback
                traceback.print_exc()
            if retry < MAX_RETRIES - 1:
                time.sleep(1)
    raise last_exc


def parse_urls_from_text(text):
    """从文本中提取 HTTP/HTTPS URL。"""
    urls = []
    seen = set()
    for raw in re.findall(r'https?://\S+', text):
        u = raw.strip().strip('"').strip("'").rstrip(',').rstrip(')').rstrip(';')
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
    return urls


def derive_filename(base_name, url):
    """由最终 URL 决定输出文件名（带后缀）。"""
    path = urlparse(url).path
    fname = Path(path).name
    if not fname:
        return ensure_suffix(base_name)
    stem = Path(fname).stem
    suffix = Path(fname).suffix
    if stem and suffix:
        return f"{base_name}{suffix}"
    return ensure_suffix(base_name)


# ---------- 核心：下载单个直播源 ----------
def download_one(live, _chain=None):
    """
    返回值：(ok, size, disk_filename, final_url)
    disk_filename: 磁盘上真实写出的播放列表文件名（已净化）
    """
    name = live["name"]
    orig_url = live["url"]
    ua = live.get("ua", "")
    _chain = _chain or []

    target = orig_url if not _chain else _chain[-1]

    # 下载
    try:
        content = fetch_url(target, ua)
    except Exception:
        return False, 0, "", target

    text = content.decode("utf-8", errors="replace")
    urls = parse_urls_from_text(text)

    # 套壳展开：只有1条URL且不同于原始请求
    if len(urls) == 1 and urls[0] != orig_url and urls[0] not in _chain:
        if DEBUG:
            print(f"      [unwrap] {target} -> {urls[0]}")
        new_chain = _chain + [urls[0]]
        if len(new_chain) <= MAX_UNWRAP_DEPTH:
            ok, size, sub_filename, sub_url = download_one(
                {"name": name, "url": orig_url, "ua": ua}, new_chain)
            if ok:
                return ok, size, sub_filename, sub_url

    # 当前层就是最终有播放列表内容的一层
    size = len(content)
    final_url = target

    # 由最终 URL 决定后缀，拼接基础名称
    raw_filename = derive_filename(name, final_url)
    # 净化文件名（只去stem特殊字符，保留后缀）
    disk_filename = sanitize_filename(raw_filename)
    disk_filename = ensure_suffix(disk_filename)

    # 写出播放列表文件
    OUTPUT_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_LIVE_DIR / disk_filename
    with open(out_path, "wb") as f:
        f.write(b"#EXTM3U\n")
        f.write(f'#EXTINF:-1 tvg-name="{name}",{name}\n'.encode("utf-8"))
        f.write(content)

    return True, size, disk_filename, final_url


# ---------- 扫描与聚合 ----------
def scan_interfaces():
    print("\n[1/4] 扫描接口文件，提取 lives ...")
    all_lives = []
    used_urls = set()

    if not SCAN_DIR.exists():
        print(f"  目录不存在: {SCAN_DIR}")
        return all_lives

    for json_file in SCAN_DIR.glob("*.json"):
        if json_file.name == AGGREGATE_JSON.name:
            continue
        source = json_file.stem
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            print(f"  跳过 {json_file.name}: {e}")
            continue

        lives = data.get("lives", [])
        if not isinstance(lives, list):
            continue

        valid = 0
        for item in lives:
            if not isinstance(item, dict):
                continue
            item_name = item.get("name", "").strip()
            item_url = item.get("url", "").strip()
            # 有名称且有URL，全部提取
            if not item_name or not item_url:
                continue
            if not item_url.startswith(("http://", "https://")):
                continue

            norm = normalize_url(item_url)
            if norm in used_urls:
                continue
            used_urls.add(norm)

            all_lives.append({
                "name": item_name,
                "url": item_url,
                "ua": item.get("ua", ""),
                "source": source,
            })
            valid += 1
        print(f"  {json_file.name}: {valid} 条")

    print(f"  合计（去重后）: {len(all_lives)}")
    return all_lives


def aggregate(lives):
    print("\n[2/4] 聚合命名 ...")
    used_names = set()
    aggregated = []

    for item in lives:
        base = item["name"]
        item["name"] = get_unique_name(base, used_names)
        aggregated.append(item)
        if len(aggregated) <= 10:
            print(f"      {len(aggregated)}. {item['name']}  <=  {item['url'][:60]}")
    if len(aggregated) > 10:
        print(f"      ... 共 {len(aggregated)} 条")

    OUTPUT_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with open(AGGREGATE_JSON, "w", encoding="utf-8") as f:
        json.dump({"lives": aggregated}, f, ensure_ascii=False, indent=2)
    print(f"  索引 -> {AGGREGATE_JSON}")

    return aggregated


# ---------- 批量下载 ----------
def batch_download(lives):
    print(f"\n[3/4] 下载直播源 ...")
    print(f"  输出目录: {OUTPUT_LIVE_DIR}")
    results = {}  # name -> (ok, size, disk_filename, final_url)
    fail = []

    for idx, live in enumerate(lives, 1):
        ok, size, disk_filename, final_url = download_one(live)
        results[live["name"]] = (ok, size, disk_filename, final_url)
        status = "ok" if ok else "FAIL"
        size_str = format_file_size(size) if ok else "0B"
        print(f"  [{idx}/{len(lives)}] {live['name']} {status} ({size_str})")
        if not ok:
            fail.append(live["name"])

    ok_count = sum(1 for v in results.values() if v[0])
    print(f"\n  完成: {ok_count}/{len(lives)} 成功")
    if fail:
        print(f"  失败: {', '.join(fail)}")

    return results


# ---------- 生成 livelist.txt ----------
def generate_livelist(lives, results):
    print("\n[4/4] 生成/合并 livelist.txt ...")

    # 读取旧记录
    old_records = {}
    if LIVELIST_PATH.exists():
        with open(LIVELIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split("|")
                    old_records[parts[0]] = line

    # 构建新记录
    new_records = {}
    for live in lives:
        name = live["name"]
        if name not in results or not results[name][0]:
            continue
        _, size, disk_filename, final_url = results[name]
        source = Path(live['source']).stem
        ua = (live.get("ua") or "").strip()
        ua_field = ua if ua else "null"

        # 第一列 = 磁盘真实文件名，直接复用，不做二次计算
        line = f"{disk_filename}|{TODAY}|{format_file_size(size)}|{live['url']}|{source}|{ua_field}|"
        new_records[disk_filename] = (name, line)

    # 合并：新记录覆盖同名（按原始名称匹配），旧记录中未覆盖的保留
    old_by_raw_name = {}
    for line in old_records.values():
        parts = line.split("|")
        # 旧记录中无法反推原始名称，按文件名stem匹配
        old_by_raw_name[Path(parts[0]).stem] = line

    final_lines = [new_records[n][1] for n in new_records]
    covered = {new_records[n][0] for n in new_records}
    for stem, line in old_by_raw_name.items():
        if stem not in covered:
            final_lines.append(line)

    with open(LIVELIST_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))

    print(f"  -> {LIVELIST_PATH}")
    preserved = max(0, len(old_records) - len(new_records))
    print(f"  更新: {len(new_records)}, 保留旧记录: {preserved}")


# ---------- 主入口 ----------
def main():
    start = time.time()
    import argparse
    ap = argparse.ArgumentParser(description="TVBox 直播源聚合器")
    ap.add_argument("--debug", action="store_true", help="输出详细调试日志")
    ap.add_argument("--force", action="store_true", help="强制执行")
    args = ap.parse_args()
    global DEBUG
    DEBUG = args.debug

    print("=" * 60)
    print("TVBox Live aggregator")
    print("=" * 60)

    lives = scan_interfaces()
    if not lives:
        print("\n没有有效的直播源，退出")
        return

    aggregated = aggregate(lives)
    results = batch_download(aggregated)
    generate_livelist(aggregated, results)

    print("\n" + "=" * 60)
    print(f"完成，耗时 {time.time() - start:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
