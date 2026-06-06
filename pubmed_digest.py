#!/usr/bin/env python3
"""
Medical Paper Daily Digest
毎朝PubMedから最新医学論文を取得し、アブストラクトを日本語訳してメール配信するツール
"""

import os
import html as html_module
import smtplib
import datetime
import time
import requests
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

load_dotenv()

# ─── 設定 ────────────────────────────────────────────────────────────────────
KEYWORDS = [k.strip() for k in os.getenv(
    "PUBMED_KEYWORDS",
    "ERCP,EUS,pancreatic cancer,bile duct cancer,bile duct stone"
).split(",")]

MAX_PAPERS_PER_KEYWORD = 1   # キーワードあたりの最大取得数
NCBI_API_KEY  = os.getenv("NCBI_API_KEY", "")
EMAIL_FROM    = os.getenv("EMAIL_FROM", "")
EMAIL_TO      = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT") or "587")

# 月曜は週末分も含めるため3日分遡る
TODAY = datetime.date.today()
DAYS_BACK = 3 if TODAY.weekday() == 0 else 1

# カードヘッダーのグラデーションをキーワードごとに変える
GRADIENTS = [
    ("136deg", "#1e3a8a", "#7c3aed"),
    ("136deg", "#0f766e", "#1d4ed8"),
    ("136deg", "#9d174d", "#c2410c"),
    ("136deg", "#6d28d9", "#be185d"),
    ("136deg", "#065f46", "#0e7490"),
]


# ─── PubMed 取得 ──────────────────────────────────────────────────────────────
def search_pubmed(keyword: str, max_results: int = MAX_PAPERS_PER_KEYWORD) -> list[str]:
    """PubMedをキーワード検索し、PMIDリストを返す"""
    date_from = (TODAY - datetime.timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")
    date_to   = TODAY.strftime("%Y/%m/%d")
    params = {
        "db": "pubmed",
        "term": (
            f"({keyword}[Title/Abstract]) "
            f"AND (\"{date_from}\"[Date - Publication]:\"{date_to}\"[Date - Publication])"
        ),
        "retmax": max_results,
        "retmode": "json",
        "sort": "date",
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY

    resp = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def fetch_paper_details(pmid: str) -> dict | None:
    """PMIDから論文詳細（タイトル・要旨・著者・雑誌名）を取得する"""
    params = {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY

    resp = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=params,
        timeout=15,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    article = root.find(".//Article")
    if article is None:
        return None

    title = article.findtext(".//ArticleTitle", default="").strip()

    abstract_parts = article.findall(".//AbstractText")
    if not abstract_parts:
        return None  # 要旨なしはスキップ
    abstract = " ".join(
        (f"[{p.get('Label')}] " if p.get("Label") else "") + (p.text or "")
        for p in abstract_parts
    ).strip()

    authors = []
    for a in article.findall(".//Author")[:4]:
        last = a.findtext("LastName", "")
        fore = a.findtext("ForeName", "")
        if last:
            authors.append(f"{last} {fore}".strip())

    journal  = article.findtext(".//Journal/Title", default="")
    year     = article.findtext(".//PubDate/Year", default="")
    month    = article.findtext(".//PubDate/Month", default="")

    return {
        "pmid":     pmid,
        "title":    title,
        "abstract": abstract,
        "authors":  authors,
        "journal":  journal,
        "pub_date": f"{year} {month}".strip(),
        "url":      f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


# ─── 翻訳 ─────────────────────────────────────────────────────────────────────
def translate_to_japanese(text: str) -> str:
    """Google翻訳でテキストを日本語に翻訳する"""
    if not text:
        return ""
    try:
        translator = GoogleTranslator(source="en", target="ja")
        # GoogleTranslator の上限（約5000文字）を超える場合は分割して翻訳
        limit = 4500
        if len(text) <= limit:
            return translator.translate(text)
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)]
        return "".join(translator.translate(chunk) for chunk in chunks)
    except Exception as e:
        print(f"  ⚠️ 翻訳エラー: {e}")
        return text  # 失敗時は原文を返す


def prepare_paper(paper: dict) -> dict:
    """論文のタイトルとアブストラクトを日本語に翻訳する"""
    title_ja    = translate_to_japanese(paper["title"])
    abstract_ja = translate_to_japanese(paper["abstract"])
    return {**paper, "title_ja": title_ja, "abstract_ja": abstract_ja}


# ─── HTML 生成ヘルパー ────────────────────────────────────────────────────────
def safe(text: str) -> str:
    return html_module.escape(str(text))


# ─── HTML カード生成 ──────────────────────────────────────────────────────────
def generate_html_card(paper: dict, index: int) -> str:
    grad_deg, c1, c2 = GRADIENTS[index % len(GRADIENTS)]
    grad = f"linear-gradient({grad_deg}, {c1}, {c2})"

    title_ja    = safe(paper.get("title_ja", paper.get("title", "タイトル不明")))
    title_en    = safe(paper.get("title", ""))
    abstract_ja = safe(paper.get("abstract_ja", paper.get("abstract", "")))
    authors     = paper.get("authors", [])
    journal     = safe(paper.get("journal", ""))
    pub_date    = safe(paper.get("pub_date", ""))
    url         = paper.get("url", "#")
    pmid        = paper.get("pmid", "")

    author_str = "、".join(authors[:3]) + ("ら" if len(authors) > 3 else "")

    return f"""
    <div class="paper-card">

      <!-- ══ ヘッダー ══ -->
      <div class="card-header" style="background: {grad};">
        <div class="card-icon">📄</div>
        <h2 class="card-title">{title_ja}</h2>
        <p class="card-title-en">{title_en}</p>
        <div class="card-meta">
          <span class="meta-authors">✍️ {author_str}</span>
          <span class="meta-sep">｜</span>
          <span class="meta-journal">📖 {journal}</span>
          <span class="meta-sep">｜</span>
          <span class="meta-date">📅 {pub_date}</span>
        </div>
        <div class="pmid-label">PMID: {pmid}</div>
      </div>

      <!-- ══ ボディ ══ -->
      <div class="card-body">

        <!-- アブストラクト（日本語訳） -->
        <div class="section sec-abstract">
          <div class="sec-header">
            <div class="sec-icon-wrap sec-icon-blue">📋</div>
            <h3 class="sec-title" style="color:#1d4ed8;">アブストラクト（日本語訳）</h3>
          </div>
          <p class="sec-text">{abstract_ja}</p>
        </div>

        <!-- フッター -->
        <div class="card-footer">
          <a href="{url}" class="pubmed-btn" target="_blank">
            PubMedで原文を読む <span class="btn-arrow">→</span>
          </a>
        </div>

      </div><!-- /card-body -->
    </div><!-- /paper-card -->
    """


# ─── メール全体 HTML 生成 ─────────────────────────────────────────────────────
def generate_email_html(papers: list[dict]) -> str:
    date_str = TODAY.strftime("%Y年%-m月%-d日")
    weekday  = ["月", "火", "水", "木", "金", "土", "日"][TODAY.weekday()]
    count    = len(papers)

    cards_html = "\n".join(
        generate_html_card(p, i) for i, p in enumerate(papers)
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>医学論文ダイジェスト {date_str}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Noto Sans JP', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
      background: #eef2f7;
      color: #1e293b;
      line-height: 1.7;
      -webkit-text-size-adjust: 100%;
    }}

    .page-wrapper {{
      max-width: 680px;
      margin: 0 auto;
      padding: 20px 16px 40px;
    }}

    /* ── トップヘッダー ── */
    .top-header {{
      background: linear-gradient(136deg, #0f172a, #1e3a8a);
      border-radius: 24px;
      padding: 32px 28px;
      text-align: center;
      margin-bottom: 28px;
      position: relative;
      overflow: hidden;
    }}
    .top-header::before {{
      content: '';
      position: absolute;
      top: -50%; left: -50%;
      width: 200%; height: 200%;
      background: radial-gradient(circle at 70% 30%, rgba(99,102,241,0.25) 0%, transparent 60%);
      pointer-events: none;
    }}
    .top-logo  {{ font-size: 36px; margin-bottom: 8px; }}
    .top-title {{
      font-size: 22px; font-weight: 900; color: #ffffff;
      letter-spacing: 0.04em; margin-bottom: 6px;
    }}
    .top-date {{ font-size: 14px; color: rgba(255,255,255,0.75); margin-bottom: 16px; }}
    .top-count-badge {{
      display: inline-block;
      background: rgba(255,255,255,0.15);
      border: 1px solid rgba(255,255,255,0.3);
      color: #ffffff; font-size: 13px; font-weight: 700;
      padding: 6px 20px; border-radius: 50px; letter-spacing: 0.03em;
    }}

    /* ── ペーパーカード ── */
    .paper-card {{
      background: #ffffff;
      border-radius: 22px;
      overflow: hidden;
      box-shadow: 0 8px 40px rgba(15,23,42,0.10), 0 2px 8px rgba(15,23,42,0.06);
      margin-bottom: 32px;
    }}

    /* ── カードヘッダー ── */
    .card-header {{
      padding: 28px 24px 24px;
      color: white;
    }}
    .card-icon {{
      font-size: 44px;
      line-height: 1;
      margin-bottom: 12px;
      filter: drop-shadow(0 4px 8px rgba(0,0,0,0.3));
    }}
    .card-title {{
      font-size: 19px; font-weight: 800; line-height: 1.45;
      margin-bottom: 10px;
      text-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}
    .card-title-en {{
      font-size: 12px; opacity: 0.75; line-height: 1.5; margin-bottom: 14px;
    }}
    .card-meta {{ font-size: 12px; opacity: 0.88; line-height: 1.9; }}
    .meta-sep {{ opacity: 0.5; margin: 0 4px; }}
    .pmid-label {{
      margin-top: 10px; font-size: 11px; opacity: 0.6; font-family: monospace;
    }}

    /* ── カードボディ ── */
    .card-body {{ padding: 0 20px 24px; }}

    /* ── セクション ── */
    .section {{
      padding: 20px 18px;
      border-radius: 14px;
      margin-top: 16px;
    }}
    .sec-abstract {{ background: #eff6ff; border-left: 5px solid #3b82f6; }}

    .sec-header {{
      display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
    }}
    .sec-icon-wrap {{
      width: 34px; height: 34px; border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 17px; flex-shrink: 0;
    }}
    .sec-icon-blue {{ background: #dbeafe; }}
    .sec-title {{ font-size: 14px; font-weight: 800; letter-spacing: 0.04em; }}
    .sec-text {{ font-size: 14px; line-height: 1.75; color: #334155; }}

    /* ── カードフッター ── */
    .card-footer {{ margin-top: 20px; padding-top: 18px; border-top: 2px solid #f1f5f9; }}

    .pubmed-btn {{
      display: block; width: 100%; text-align: center;
      background: linear-gradient(135deg, #2563eb, #6366f1);
      color: #ffffff !important; text-decoration: none;
      font-size: 15px; font-weight: 800; padding: 14px 28px;
      border-radius: 50px; letter-spacing: 0.04em;
      box-shadow: 0 4px 16px rgba(37,99,235,0.35);
    }}
    .btn-arrow {{ font-size: 18px; font-weight: 900; margin-left: 4px; }}

    /* ── ページフッター ── */
    .page-footer {{
      text-align: center; padding: 24px 0 8px;
      color: #94a3b8; font-size: 12px; line-height: 1.8;
    }}

    @media (max-width: 480px) {{
      .page-wrapper {{ padding: 12px 10px 32px; }}
      .card-title  {{ font-size: 16px; }}
      .top-title   {{ font-size: 18px; }}
    }}
  </style>
</head>
<body>
  <div class="page-wrapper">

    <!-- トップヘッダー -->
    <div class="top-header">
      <div class="top-logo">🧬</div>
      <div class="top-title">医学論文ダイジェスト</div>
      <div class="top-date">{date_str}（{weekday}曜日）</div>
      <span class="top-count-badge">本日の新着 {count} 本</span>
    </div>

    <!-- 論文カード群 -->
    {cards_html}

    <!-- ページフッター -->
    <div class="page-footer">
      <p>🔬 PubMed から自動取得、Google翻訳でアブストラクトを日本語化</p>
      <p>情報は参考目的です。診療判断は必ず原著をご確認ください。</p>
      <p style="margin-top:6px; color:#cbd5e1;">Medical Paper Daily Digest</p>
    </div>

  </div>
</body>
</html>"""


# ─── メール送信 ───────────────────────────────────────────────────────────────
def send_email(html_content: str, paper_count: int) -> None:
    date_str  = TODAY.strftime("%Y年%-m月%-d日")
    weekday   = ["月", "火", "水", "木", "金", "土", "日"][TODAY.weekday()]
    subject   = f"🧬 医学論文ダイジェスト｜{date_str}（{weekday}）| 新着 {paper_count} 本"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"✅ メール送信完了 → {EMAIL_TO}（{paper_count}本）")


# ─── メイン ──────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"🔍 PubMed 検索開始（{TODAY}、{DAYS_BACK}日分）")
    print(f"   キーワード: {KEYWORDS}")

    seen_pmids: set[str] = set()
    raw_papers: list[dict] = []

    for keyword in KEYWORDS:
        print(f"\n  → '{keyword}' を検索中...")
        pmids = search_pubmed(keyword)
        print(f"     {len(pmids)}件ヒット: {pmids}")
        for pmid in pmids:
            if pmid in seen_pmids:
                print(f"     PMID {pmid} は重複のためスキップ")
                continue
            seen_pmids.add(pmid)
            time.sleep(0.35)  # NCBI APIレート制限対策
            paper = fetch_paper_details(pmid)
            if paper:
                raw_papers.append(paper)
                print(f"     ✓ PMID {pmid}: {paper['title'][:60]}...")

    if not raw_papers:
        print("⚠️ 新着論文が見つかりませんでした。メール送信をスキップします。")
        return

    print(f"\n🌐 {len(raw_papers)}本の論文を日本語に翻訳中...")
    translated: list[dict] = []
    for i, paper in enumerate(raw_papers, 1):
        print(f"  ({i}/{len(raw_papers)}) 翻訳中: PMID {paper['pmid']}")
        try:
            result = prepare_paper(paper)
            translated.append(result)
        except Exception as e:
            print(f"  ⚠️ 翻訳エラー (PMID {paper['pmid']}): {e}")
        time.sleep(0.3)

    if not translated:
        print("⚠️ 翻訳できた論文がありません。処理を終了します。")
        return

    print(f"\n🎨 HTMLメール生成中（{len(translated)}本）...")
    email_html = generate_email_html(translated)

    print("📧 メール送信中...")
    send_email(email_html, len(translated))
    print("\n🎉 完了！")


if __name__ == "__main__":
    main()
