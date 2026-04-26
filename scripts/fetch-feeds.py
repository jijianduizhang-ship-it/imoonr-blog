#!/usr/bin/env python3
"""
RSS 聚合脚本 - 增量抓取订阅源，生成 feeds.json
用法: python3 fetch-feeds.py [--limit N] [--feeds-file FILE]
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

TZ_SH = timezone(timedelta(hours=8))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# HTTP 请求头
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RSS Aggregator/1.0; "
        "+https://blog.imoons.cn)"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, "
        "application/xml, text/xml, */*"
    ),
}

MAX_ITEMS_PER_FEED = 5   # 每个源最多抓几条
FETCH_TIMEOUT = 8         # 请求超时（秒）
REQUEST_DELAY = 0.3        # 请求间隔（秒）


# ---------------------------------------------------------------------------
# RSS/Atom 解析
# ---------------------------------------------------------------------------

def parse_date(date_str: str) -> str | None:
    """把各种日期格式转成 YYYY-MM-DD HH:MM:SS，没有则返回 None。"""
    if not date_str:
        return None
    date_str = date_str.strip()
    # 尝试多种格式
    fmt_list = [
        "%a, %d %b %Y %H:%M:%S %z",   # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",  # RFC 822 (无时区)
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in fmt_list:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_SH)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def parse_feed_content(content: bytes, limit: int = 5) -> tuple[list[dict], str]:
    """解析 XML 内容，返回 (items, feed_title)。"""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning("XML 解析失败: %s", e)
        return [], ""

    # 检测格式
    tag = root.tag.lower()
    if "rss" in tag or "rdf" in tag:
        return parse_rss(root, limit)
    elif "feed" in tag:
        return parse_atom(root, limit)
    else:
        # 尝试子节点
        for child in root:
            ct = child.tag.lower()
            if "rss" in ct or "rdf" in ct:
                return parse_rss(child)
            if "feed" in ct:
                return parse_atom(child)
        logger.warning("无法识别的 XML 格式: %s", tag)
        return [], ""


def parse_rss(root: ET.Element, limit: int = 5) -> tuple[list[dict], str]:
    """解析 RSS 2.0"""
    channel = root.find("channel")
    if channel is None:
        return [], ""
    feed_title = channel.findtext("title", "").strip()

    items = []
    for item in channel.findall("item")[:limit]:
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        pub_date = item.findtext("pubDate") or item.findtext("dc:date", "")
        desc = item.findtext("description", "")[:200]

        if not title or not link:
            continue

        parsed_date = parse_date(pub_date) if pub_date else None
        items.append({
            "title": title,
            "link": link,
            "published": parsed_date or "",
            "description": desc,
        })

    return items, feed_title


def parse_atom(root: ET.Element, limit: int = 5) -> tuple[list[dict], str]:
    """解析 Atom"""
    feed_title = (
        root.findtext("title", "") or root.findtext("atom:title", "")
    ).strip()

    items = []
    for entry in root.findall("entry")[:limit]:
        title = (
            entry.findtext("title", "") or entry.findtext("atom:title", "")
        ).strip()
        link_el = entry.find("link")
        link = link_el.get("href", "") if link_el is not None else ""
        # 也可能是 <link href="..." rel="alternate"/>
        if not link:
            for lk in entry.findall("link"):
                rel = lk.get("rel", "alternate")
                if rel == "alternate":
                    link = lk.get("href", "")
                    break

        pub_date = (
            entry.findtext("published") or
            entry.findtext("updated") or
            entry.findtext("atom:published", "") or
            entry.findtext("atom:updated", "")
        )

        if not title or not link:
            continue

        parsed_date = parse_date(pub_date) if pub_date else None
        items.append({
            "title": title,
            "link": link,
            "published": parsed_date or "",
            "description": "",
        })

    return items, feed_title


# ---------------------------------------------------------------------------
# Favicon 下载
# ---------------------------------------------------------------------------

def favicon_filename(url: str) -> str:
    """从 URL 生成 favicon 文件名（MD5 哈希）"""
    domain = urlparse(url).netloc
    key = hashlib.md5(domain.encode()).hexdigest()[:12]
    return f"{domain.replace('.', '_')}_{key}.ico"


def download_favicon(session: requests.Session, feed_url: str, output_dir: Path) -> str | None:
    """下载 favicon，返回相对路径（如 /feeds/example_com_xxx.ico），失败返回 None。"""
    parsed = urlparse(feed_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    favicon_path = output_dir / "feeds"

    fname = favicon_filename(feed_url)
    fpath = favicon_path / fname

    # 已有则跳过
    if fpath.exists():
        return f"/feeds/{fname}"

    # 尝试多个位置
    candidates = [
        f"{base}/favicon.ico",
        f"{base}/apple-touch-icon.png",
        f"{base}/apple-touch-icon-precomposed.png",
    ]

    for url in candidates:
        try:
            r = session.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 0:
                content = r.content
                # 检查是否是图片
                if content[:4] == b"\x89PNG":
                    fname = fname.replace(".ico", ".png")
                    fpath = favicon_path / fname
                favicon_path.mkdir(parents=True, exist_ok=True)
                fpath.write_bytes(content)
                logger.debug("下载 favicon: %s -> %s", url, fname)
                return f"/feeds/{fname}"
        except Exception:
            continue

    # 用 Google Favicon API 做兜底
    try:
        google_url = (
            f"https://www.google.com/s2/favicons?"
            f"domain={parsed.netloc}&sz=64"
        )
        r = session.get(google_url, timeout=FETCH_TIMEOUT)
        if r.status_code == 200 and len(r.content) > 0:
            favicon_path.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(r.content)
            return f"/feeds/{fname}"
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def fetch_feed(
    session: requests.Session,
    url: str,
    feed_name: str,
    output_dir: Path,
    limit: int = 5,
    do_favicon: bool = True,
) -> list[dict]:
    """抓取单个订阅源，返回 items 列表。"""
    try:
        r = session.get(url, timeout=FETCH_TIMEOUT, headers=HEADERS)
        if not r.ok:
            logger.warning("请求失败 [%s] %s: HTTP %d", feed_name, url, r.status_code)
            return []
        r.encoding = r.apparent_encoding or "utf-8"
        items, detected_name = parse_feed_content(r.content, limit)

        if not items:
            logger.warning("未解析到条目 [%s] %s", feed_name, url)
            return []

        # favicon
        favicon = None
        if do_favicon:
            favicon = download_favicon(session, url, output_dir)

        if not detected_name:
            detected_name = feed_name

        result = []
        for item in items:
            result.append({
                "title": item["title"],
                "link": item["link"],
                "published": item["published"],
                "name": detected_name,
                "favicon": favicon,
            })

        logger.info(
            "✓ [%s] %s: 抓取 %d 条",
            feed_name,
            url,
            len(result),
        )
        return result

    except Exception as e:
        logger.error("✗ [%s] %s 异常: %s", feed_name, url, e)
        return []


def load_existing(path: Path) -> tuple[set[str], list[dict]]:
    """加载已有的 feeds.json，返回 (link_set, items)。"""
    if not path.exists():
        return set(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("items", [])
        link_set = {item["link"] for item in items if item.get("link")}
        return link_set, items
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("feeds.json 解析失败 (%s)，重新生成", e)
        return set(), []


def main():
    parser = argparse.ArgumentParser(description="RSS 聚合脚本")
    parser.add_argument(
        "--rss-file",
        default=os.path.join(os.path.dirname(__file__), "../public/data/rss.txt"),
        help="订阅源列表文件（每行一个 URL）",
    )
    parser.add_argument(
        "--feeds-file",
        default=os.path.join(os.path.dirname(__file__), "../public/data/feeds.json"),
        help="输出 feeds.json 路径",
    )
    parser.add_argument(
        "--feeds-dir",
        default=os.path.join(os.path.dirname(__file__), "../public"),
        help="public 目录路径（用于存放 favicon）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ITEMS_PER_FEED,
        help=f"每个源最多抓几条（默认 {MAX_ITEMS_PER_FEED}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制全量抓取，忽略已有数据",
    )
    parser.add_argument(
        "--no-favicon",
        action="store_true",
        help="跳过 favicon 下载（加快速度）",
    )
    args = parser.parse_args()

    rss_file = Path(args.rss_file).expanduser()
    feeds_file = Path(args.feeds_file).expanduser()
    feeds_dir = Path(args.feeds_dir).expanduser()

    # 读取订阅源列表
    if not rss_file.exists():
        logger.error("订阅源文件不存在: %s", rss_file)
        sys.exit(1)

    urls = [
        line.strip()
        for line in rss_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    logger.info("加载 %d 个订阅源", len(urls))

    # 加载已有数据
    existing_links, existing_items = load_existing(feeds_file)
    is_incremental = not args.force and bool(existing_links)
    logger.info(
        "已有 %d 条记录，模式: %s",
        len(existing_items),
        "增量" if is_incremental else "全量",
    )

    session = requests.Session()
    all_items = []
    new_links = set()

    for i, line in enumerate(urls, 1):
        line = line.strip()
        if not line:
            continue
        # 行格式: URL 或 "名称 URL"
        parts = line.split(None, 1)
        if len(parts) == 2:
            name, url = parts
        else:
            url = parts[0]
            name = urlparse(url).netloc

        items = fetch_feed(session, url, name, feeds_dir, args.limit, not args.no_favicon)
        for item in items:
            link = item["link"]
            if is_incremental and link in existing_links:
                continue   # 增量：跳过已有
            if link not in new_links:
                new_links.add(link)
                all_items.append(item)

        if i < len(urls):
            time.sleep(REQUEST_DELAY)

    logger.info("本次新增 %d 条", len(all_items))

    if not all_items and not existing_items:
        logger.warning("没有任何内容，退出")
        sys.exit(0)

    # 合并
    merged_map = {item["link"]: item for item in existing_items}
    for item in all_items:
        merged_map[item["link"]] = item

    # 按 published 降序排列（无日期的排最后）
    merged = sorted(
        merged_map.values(),
        key=lambda x: x.get("published") or "0000",
        reverse=True,
    )

    # 写入
    feeds_file.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "generated_at": datetime.now(TZ_SH).strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(merged),
        "items": merged,
    }
    feeds_file.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("已保存到 %s（总计 %d 条）", feeds_file, len(merged))


if __name__ == "__main__":
    main()
