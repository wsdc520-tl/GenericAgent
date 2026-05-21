#!/usr/bin/env python3
"""
L10 - Universal Web Scraper Framework
httpx + BeautifulSoup4 为核心, chrome-headless-shell 为JS渲染备选
YAML配置驱动, 支持自动翻页、字段提取、JSON/CSV导出
"""
import argparse, json, csv, os, sys, time, re, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
import yaml
from bs4 import BeautifulSoup

# ── chrome-headless-shell fallback ──
SHELL_PATH = os.path.expanduser(
    "~/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scraper")


# ─────────────────────── Fetcher ───────────────────────

class Fetcher:
    """Unified fetcher: httpx (fast) or chrome-headless-shell (JS render)."""

    def __init__(self, use_js: bool = False, timeout: int = 20, proxy: str = None):
        self.use_js = use_js
        self.timeout = timeout
        self.proxy = proxy
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        self.client = httpx.Client(headers=headers, timeout=timeout,
                                    follow_redirects=True, proxy=proxy)

    def fetch(self, url: str) -> str:
        if self.use_js:
            return self._fetch_js(url)
        return self._fetch_http(url)

    def _fetch_http(self, url: str) -> str:
        log.info(f"[httpx] GET {url}")
        r = self.client.get(url)
        r.raise_for_status()
        return r.text

    def _fetch_js(self, url: str) -> str:
        import subprocess
        if not os.path.exists(SHELL_PATH):
            raise FileNotFoundError(f"chrome-headless-shell not found: {SHELL_PATH}")
        log.info(f"[headless-shell] GET {url}")
        cmd = [SHELL_PATH, "--headless", "--disable-gpu", "--no-sandbox",
               "--disable-dev-shm-usage", "--dump-dom", url]
        if self.proxy:
            cmd += [f"--proxy-server={self.proxy}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(f"headless-shell failed: {r.stderr[:200]}")
        return r.stdout

    def close(self):
        self.client.close()


# ─────────────────────── Parser ───────────────────────

class Parser:
    """Extract structured data from HTML using CSS selectors."""

    @staticmethod
    def parse(html: str, config: dict) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        container_sel = config.get("container", "body")
        containers = soup.select(container_sel)
        log.info(f"Found {len(containers)} containers with selector '{container_sel}'")

        fields = config.get("fields", {})
        items = []
        for c in containers:
            item = {}
            for name, sel in fields.items():
                el = c.select_one(sel) if isinstance(sel, str) else None
                if el:
                    # Extract text or href
                    if name.endswith("_url") and el.get("href"):
                        item[name] = el.get("href", "").strip()
                    elif name.endswith("_src") and el.get("src"):
                        item[name] = el.get("src", "").strip()
                    else:
                        item[name] = el.get_text(strip=True)
                else:
                    item[name] = None
            # skip empty items
            if any(v for v in item.values()):
                items.append(item)
        return items

    @staticmethod
    def next_page(html: str, next_sel: str) -> Optional[str]:
        """Find next page URL, return None if no more pages."""
        soup = BeautifulSoup(html, "lxml")
        a = soup.select_one(next_sel)
        if a and a.get("href"):
            return a["href"]
        return None


# ─────────────────────── Scraper (orchestrator) ───────────────────────

class Scraper:
    def __init__(self, config: dict, out_dir: str = "output"):
        self.config = config
        self.name = config.get("name", "scrape")
        self.base_url = config.get("base_url", "")
        js = config.get("js_render", False)
        timeout = config.get("timeout", 20)
        proxy = config.get("proxy")
        max_pages = config.get("max_pages", 1)
        delay = config.get("delay", 1.0)
        self.fetcher = Fetcher(use_js=js, timeout=timeout, proxy=proxy)
        self.max_pages = max_pages
        self.delay = delay
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> list[dict]:
        start_url = self.config["url"]
        if not start_url.startswith("http"):
            start_url = self.base_url + start_url
        parse_cfg = self.config.get("parse", {})
        next_sel = parse_cfg.get("next_page")

        all_items = []
        url = start_url
        for page in range(1, self.max_pages + 1):
            log.info(f"=== Page {page}/{self.max_pages}: {url} ===")
            html = self.fetcher.fetch(url)
            items = Parser.parse(html, parse_cfg)
            log.info(f"Extracted {len(items)} items")
            all_items.extend(items)

            if not next_sel or page >= self.max_pages:
                break
            next_url = Parser.next_page(html, next_sel)
            if not next_url:
                log.info("No next page link found, stopping")
                break
            if not next_url.startswith("http"):
                next_url = self.base_url + next_url
            url = next_url
            time.sleep(self.delay)

        return all_items

    def export(self, items: list[dict], fmt: str = "json"):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = self.out_dir / f"{self.name}_{ts}.{fmt}"
        if fmt == "json":
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            if not items:
                log.warning("No items to export")
                return out_file
            with open(out_file, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=items[0].keys())
                w.writeheader()
                w.writerows(items)
        else:
            raise ValueError(f"Unknown format: {fmt}")
        log.info(f"Exported {len(items)} items to {out_file}")
        return out_file

    def close(self):
        self.fetcher.close()


# ─────────────────────── CLI ───────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg


def main():
    ap = argparse.ArgumentParser(description="L10 Universal Web Scraper")
    ap.add_argument("config", help="YAML config file")
    ap.add_argument("-o", "--output", default="output", help="Output directory")
    ap.add_argument("-f", "--format", default="json", choices=["json", "csv"])
    ap.add_argument("-j", "--js", action="store_true", help="Force JS rendering")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger("scraper").setLevel(logging.DEBUG)

    cfg = load_config(args.config)
    if args.js:
        cfg["js_render"] = True

    scraper = Scraper(cfg, out_dir=args.output)
    try:
        items = scraper.run()
        out_file = scraper.export(items, fmt=args.format)
        print(f"\n✅ Done! {len(items)} items → {out_file}")
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
