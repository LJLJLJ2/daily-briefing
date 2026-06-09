#!/usr/bin/env python3
"""
AI 日报自动推送 — GitHub Actions 版本
每日抓取 HN + Reddit + GitHub 热门 AI 内容，调用 DeepSeek 翻译总结，
生成 briefing.html，由 Actions 自动提交到仓库根目录，GitHub Pages 对外服务。
"""

import json
import os
import re
import smtplib
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone
from email.mime.text import MIMEText

# === 配置 ===
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "briefing.html")

HN_TOP_N = 50
REDDIT_LIMIT = 25
TOP_NEWS = 10
TOP_REPOS = 3
REQUEST_TIMEOUT = 15
AI_API_TIMEOUT = 60

# === AI 内容关键词 ===
AI_KEYWORDS = re.compile(
    r"AI|LLM|GPT|Claude|Gemini|Llama|DeepSeek|Mistral|Qwen|"
    r"open.source|release|launch|benchmark|fine.tun|"
    r"agent|RAG|embedding|transformer|diffusion|"
    r"Mixture.of.Experts|RLHF|DPO|quantiz|"
    r"text.to.image|text.to.video|speech.to.text|"
    r"reasoning.model|chain.of.thought|function.call",
    re.IGNORECASE,
)

NOISE_KEYWORDS = re.compile(
    r"\b(?:stock|IPO|funding|rais(?:es|ed|ing)\s*\d+\s*million|"
    r"valuation|layoff|laying.off|hiring|acquir(?:es|ed|ing)|"
    r"revenue|quarterly.earnings|profit|share.price)\b",
    re.IGNORECASE,
)

AI_API_URL = "https://api.deepseek.com/v1/chat/completions"
AI_MODEL = "deepseek-chat"


def fetch_json(url, headers=None):
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WARN] fetch failed: {url} — {e}")
        return None


# ═══ Hacker News ═══

def fetch_hn():
    ids = fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not ids:
        return []
    items = []
    for item_id in ids[:HN_TOP_N]:
        item = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if item and item.get("type") == "story" and item.get("title"):
            items.append(item)
    return items


# ═══ Reddit ═══

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; daily-briefing/1.0)",
    "Accept": "application/json",
}


def fetch_reddit(subreddit):
    import xml.etree.ElementTree as ET
    url = f"https://www.reddit.com/r/{subreddit}/hot/.rss?limit={REDDIT_LIMIT}"
    try:
        req = urllib.request.Request(url, headers=REDDIT_HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode()
    except Exception as e:
        print(f"[WARN] fetch failed: {url} — {e}")
        return []
    posts = []
    try:
        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            title = title_el.text if title_el is not None else ""
            link = link_el.get("href") if link_el is not None else ""
            if title and link:
                posts.append({"title": title, "url": link, "score": 0, "num_comments": 0})
    except ET.ParseError as e:
        print(f"[WARN] RSS parse error for r/{subreddit}: {e}")
    return posts


# ═══ GitHub ═══

def fetch_github():
    yesterday = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    all_repos = []
    for query in [
        f"topic:artificial-intelligence+created:>={yesterday}",
        f"topic:machine-learning+created:>={yesterday}",
        f"stars:>50+created:>={yesterday}+topic:llm",
    ]:
        url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=5"
        data = fetch_json(url)
        if data:
            for repo in data.get("items", []):
                all_repos.append({
                    "full_name": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "description": repo.get("description") or "",
                    "stars": repo.get("stargazers_count", 0),
                    "language": repo.get("language") or "",
                })
    seen = set()
    unique = []
    for r in all_repos:
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            unique.append(r)
    unique.sort(key=lambda r: r["stars"], reverse=True)
    return unique


# ═══ 过滤 & 排序 ═══

def is_ai_relevant(title):
    if NOISE_KEYWORDS.search(title):
        return False
    return bool(AI_KEYWORDS.search(title))


def extract_gh_links(text):
    if not text:
        return set()
    urls = re.findall(r"github\.com/([\w.-]+/[\w.-]+)", str(text))
    return {u.rstrip("/") for u in urls}


def build_raw_items(hn_items, reddit_posts):
    items = []
    seen_gh = set()
    for post in hn_items:
        title = post.get("title", "")
        if not is_ai_relevant(title):
            continue
        url = post.get("url") or f"https://news.ycombinator.com/item?id={post['id']}"
        items.append({
            "source": "HN",
            "title": title,
            "url": url,
            "score": post.get("score", 0),
            "comments": post.get("descendants", 0),
            "source_label": f"HN 🔥{post.get('score', 0)}",
        })
        seen_gh |= extract_gh_links(post.get("url", ""))
    for post in reddit_posts:
        title = post.get("title", "")
        if not is_ai_relevant(title):
            continue
        url = post.get("url", "")
        score = post.get("score", 0)
        items.append({
            "source": "Reddit",
            "title": title,
            "url": url,
            "score": score,
            "comments": post.get("num_comments", 0),
            "source_label": f"Reddit 👍{score}" if score else "Reddit",
        })
        seen_gh |= extract_gh_links(post.get("url", ""))
    items.sort(key=lambda x: x["score"], reverse=True)
    deduped = []
    seen_titles = set()
    for item in items:
        norm = re.sub(r"\s+", " ", item["title"].lower()).strip()
        if norm not in seen_titles:
            seen_titles.add(norm)
            deduped.append(item)
    return deduped[:TOP_NEWS], seen_gh


def build_repo_items(github_repos, exclude_names=set()):
    items = []
    for r in github_repos:
        if r["full_name"] in exclude_names:
            continue
        items.append({
            "full_name": r["full_name"],
            "url": r["url"],
            "description": r["description"][:120] if r["description"] else "",
            "stars": r["stars"],
            "language": r["language"],
        })
    return items[:TOP_REPOS]


# ═══ AI 翻译 + 总结 ═══

def call_ai_enrich(news, repos):
    prompt_parts = ["请为以下每条内容生成三个字段，用 JSON 格式返回。"]
    for i, n in enumerate(news):
        prompt_parts.append(f"\n[新闻{i}]\n标题: {n['title']}\n")
    for i, r in enumerate(repos):
        prompt_parts.append(f"\n[项目{i}]\n名称: {r['full_name']}\n描述: {r['description']}\n")
    prompt_parts.append("""
请严格按以下 JSON 格式返回（不要包含其他文字）：
{
  "news_0": {"title_cn": "中文标题", "summary": "一句话概括内容要点", "outlook": "一句话说明该技术或事件的未来应用前景"},
  "project_0": {"name_cn": "中文译名", "summary": "一句话概括项目是什么、做什么", "outlook": "一句话说明该项目在实际业务中能怎么应用"}
}

要求：
- title_cn / name_cn: 不超过 30 个中文字，简洁达意
- summary: 80~120 个中文字，用 2~3 句话概括：这个东西是什么、解决了什么问题、有什么关键突破或亮点
- outlook: 60~80 个中文字，说明该技术或工具在实际业务中能怎么用、对行业有什么潜在影响
- 语言：全部使用简体中文
""")
    prompt = "\n".join(prompt_parts)
    body = json.dumps({
        "model": AI_MODEL,
        "max_tokens": 4096,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "你是一个 AI 技术新闻编辑，擅长用简洁中文概括技术内容和判断应用前景。"},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(AI_API_URL, data=body)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=AI_API_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[WARN] AI API call failed: {e}")
        return None
    text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    json_match = re.search(r"\{[\s\S]*\}", text)
    if not json_match:
        print(f"[WARN] AI response not valid JSON: {text[:300]}")
        return None
    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        print(f"[WARN] AI JSON parse failed: {text[:300]}")
        return None


def apply_enrich(news, repos, enrich_data):
    if not enrich_data:
        return
    for i, n in enumerate(news):
        key = f"news_{i}"
        if key in enrich_data:
            e = enrich_data[key]
            n["title_cn"] = e.get("title_cn", "")
            n["summary"] = e.get("summary", "")
            n["outlook"] = e.get("outlook", "")
    for i, r in enumerate(repos):
        key = f"project_{i}"
        if key in enrich_data:
            e = enrich_data[key]
            r["name_cn"] = e.get("name_cn", "")
            r["summary"] = e.get("summary", "")
            r["outlook"] = e.get("outlook", "")


# ═══ HTML 生成 ═══

def build_html(news, repos):
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    news_rows = []
    for i, n in enumerate(news, 1):
        title_cn = n.get("title_cn", "")
        summary = n.get("summary", "")
        outlook = n.get("outlook", "")
        title_line = n["title"]
        if title_cn:
            title_line += f"（{title_cn}）"
        extra = ""
        if summary:
            extra += f'<div class="summary">📌 {summary}</div>'
        if outlook:
            extra += f'<div class="outlook">🔭 应用前景：{outlook}</div>'
        news_rows.append(f"""
        <tr>
          <td class="rank">{i}</td>
          <td>
            <a href="{n['url']}" class="title-link">{title_line}</a>
            <span class="source-tag">{n['source_label']}</span>
            {extra}
          </td>
        </tr>""")
    repo_rows = []
    for i, r in enumerate(repos, 1):
        name_cn = r.get("name_cn", "")
        summary = r.get("summary", "")
        outlook = r.get("outlook", "")
        lang_tag = f'<span class="lang-tag">{r["language"]}</span>' if r["language"] else ""
        name_line = r["full_name"]
        if name_cn:
            name_line += f"（{name_cn}）"
        extra = ""
        if summary:
            extra += f'<div class="summary">📌 {summary}</div>'
        if outlook:
            extra += f'<div class="outlook">🔭 应用前景：{outlook}</div>'
        repo_rows.append(f"""
        <tr>
          <td class="rank">{i}</td>
          <td>
            <a href="{r['url']}" class="title-link">{name_line}</a>
            {lang_tag}
            <span class="stars">⭐ {r['stars']:,}</span>
            <div class="desc">{r['description']}</div>
            {extra}
          </td>
        </tr>""")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 日报 · {date.today()}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:"PingFang SC","Heiti SC","STHeiti","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#2c3e50;line-height:1.8}}
  .hero{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);color:#fff;padding:36px 5% 28px;text-align:center;margin-bottom:24px}}
  .hero h1{{font-size:1.7em;font-weight:800;margin-bottom:4px}}
  .hero p{{color:rgba(255,255,255,0.5);font-size:.85em}}
  .container{{max-width:780px;margin:0 auto;padding:0 4%}}
  .card{{background:#fff;border-radius:10px;padding:22px 26px;box-shadow:0 1px 3px rgba(0,0,0,0.05);margin-bottom:20px}}
  .card h2{{color:#0f3460;font-size:1.15em;margin-bottom:14px;border-left:4px solid #e94560;padding-left:10px}}
  table{{width:100%;border-collapse:collapse}}
  td{{padding:10px 10px;border-bottom:1px solid #eee;font-size:.88em;vertical-align:top}}
  .rank{{width:28px;text-align:center;font-weight:700;color:#e94560;font-size:1em;padding-top:14px}}
  a.title-link{{color:#1a1a2e;text-decoration:none;font-weight:600}}
  a.title-link:hover{{color:#e94560;text-decoration:underline}}
  .source-tag{{display:inline-block;background:#f0f0f0;color:#777;font-size:.72em;padding:1px 7px;border-radius:4px;margin-left:6px;white-space:nowrap}}
  .lang-tag{{display:inline-block;background:#e8f4f8;color:#0f3460;font-size:.72em;padding:1px 7px;border-radius:4px;margin-left:6px}}
  .stars{{color:#f39c12;font-weight:700;font-size:.85em;margin-left:6px;white-space:nowrap}}
  .desc{{color:#666;font-size:.82em;margin-top:2px}}
  .summary{{color:#555;font-size:.84em;margin-top:4px;line-height:1.5}}
  .outlook{{color:#17677d;font-size:.82em;margin-top:2px;line-height:1.5;font-style:italic}}
  .footer{{text-align:center;padding:24px 5%;color:#95a5a6;font-size:.78em}}
  .empty-note{{color:#999;font-style:italic;padding:10px 0}}
  .ai-badge{{display:inline-block;background:#e94560;color:#fff;font-size:.7em;padding:0 6px;border-radius:3px;margin-left:4px;vertical-align:middle}}
</style>
</head>
<body>
<div class="hero">
  <h1>AI 日报 <span style="font-size:.55em;vertical-align:middle;opacity:.7">AI 增强版</span></h1>
  <p>Hacker News + Reddit + GitHub Trending · AI 翻译摘要 · 自动推送 · {now_str}</p>
</div>
<div class="container">

<div class="card">
  <h2>🔥 今日 AI 十大热门</h2>
  <table>
    {"".join(news_rows) if news else '<p class="empty-note">今日暂无符合条件的 AI 内容</p>'}
  </table>
</div>

<div class="card">
  <h2>⭐ GitHub 热门开源项目 Top {TOP_REPOS}</h2>
  <table>
    {"".join(repo_rows) if repos else '<p class="empty-note">今日暂无符合条件的项目</p>'}
  </table>
</div>

<div class="footer">
  <p>数据源: Hacker News API · Reddit RSS · GitHub Search API | AI 增强: DeepSeek</p>
  <p>自动推送 · GitHub Actions · 生成时间: {now_str}</p>
</div>
</div>
</body>
</html>"""


# ═══ 邮件发送 ═══

SMTP_CONFIG = {
    "server": "smtp.qq.com",
    "port": 465,
    "sender": "1026489389@qq.com",
    "password": os.environ.get("QQ_SMTP_PASSWORD", ""),
    "receiver": "1026489389@qq.com",
}


def send_email(html):
    if not SMTP_CONFIG["password"]:
        print("[SKIP] QQ_SMTP_PASSWORD not set, skip email")
        return
    now_beijing = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    subject = f"AI 日报 · {now_beijing:%Y-%m-%d}"
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_CONFIG["sender"]
    msg["To"] = SMTP_CONFIG["receiver"]
    try:
        server = smtplib.SMTP_SSL(SMTP_CONFIG["server"], SMTP_CONFIG["port"], timeout=15)
        server.login(SMTP_CONFIG["sender"], SMTP_CONFIG["password"])
        server.sendmail(SMTP_CONFIG["sender"], [SMTP_CONFIG["receiver"]], msg.as_string())
        server.quit()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 邮件已发送")
    except Exception as e:
        print(f"[WARN] 邮件发送失败: {e}")


# ═══ 主流程 ═══

def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始抓取数据...")

    hn_items = fetch_hn()
    reddit_posts = fetch_reddit("MachineLearning") + fetch_reddit("LocalLLaMA")
    github_repos = fetch_github()

    news, exclude_gh = build_raw_items(hn_items, reddit_posts)
    repos = build_repo_items(github_repos, exclude_names=exclude_gh)

    if not news and not repos:
        print("无内容，生成空日报")
        html = f"<p>今日暂无 AI 相关新闻和项目。</p>"
    else:
        print(f"数据抓取完成: {len(news)} 条新闻, {len(repos)} 个项目")
        if news or repos:
            print("调用 AI 翻译总结...")
            enrich_data = call_ai_enrich(news, repos)
            apply_enrich(news, repos, enrich_data)
        html = build_html(news, repos)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已写入 {OUTPUT_FILE} ({len(html)} 字符)")

    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour == 10 and (news or repos):
        send_email(html)
    else:
        print(f"[SKIP] 邮件仅在 UTC 10:00 发送 (当前 UTC {utc_hour}:00)")


if __name__ == "__main__":
    main()
