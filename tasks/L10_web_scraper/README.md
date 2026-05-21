# L10 - Universal Web Scraper Framework

httpx + BeautifulSoup4 驱动的通用爬虫框架，YAML配置驱动，支持自动翻页、字段提取、JSON/CSV导出。

## 快速使用

```bash
python scraper.py examples/hackernews.yaml -o output -f json -v
python scraper.py examples/lobsters.yaml -o output -f csv
```

## YAML配置格式

```yaml
name: my_scraper          # 任务名（用于输出文件名）
url: https://example.com  # 起始URL
base_url: https://example.com  # 翻页拼接基准
js_render: false          # true=用chrome-headless-shell(CDP)
timeout: 20               # 请求超时秒
delay: 1.5                # 页间延迟秒
max_pages: 3              # 最大翻页数

parse:
  container: "div.item"   # 条目容器CSS选择器
  next_page: "a.next"     # 下一页链接CSS选择器
  fields:
    title: "h2.title a"   # 字段名: CSS选择器
    url: "h2.title a"     # 自动提取href属性
    score: "span.points"  # 文本内容
```

## 特性
- **纯HTTP模式**: httpx+BS4，轻量快速
- **JS渲染模式**: chrome-headless-shell + CDP，处理SPA
- **自动翻页**: next_page选择器自动发现下一页
- **多格式导出**: JSON / CSV
- **请求延迟**: 避免被封
- **详细日志**: -v 开启DEBUG

## 输出
文件名格式: `{name}_{timestamp}.json`

## 依赖
```
httpx
beautifulsoup4
pyyaml
```
