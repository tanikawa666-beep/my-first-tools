#!/usr/bin/env python3
"""
Medical Paper Daily Digest
毎朝PubMedから最新医学論文を取得し、美麗なHTML図解メールで配信するツール
"""

import os
import json
import re
import html as html_module
import smtplib
import datetime
import time
import requests
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ─── 設定 ────────────────────────────────────────────────────────────────────
KEYWORDS = [k.strip() for k in os.getenv(
    "PUBMED_KEYWORDS",
    "ERCP,EUS,pancreatic cancer,bile duct cancer,bile duct stone"
).split(",")]

MAX_PAPERS_PER_KEYWORD = 1   # キーワードあたりの最大取得数
NCBI_API_KEY  = os.getenv("NCBI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMAIL_FROM    = os.getenv("EMAIL_FROM", "")
EMAIL_TO      = os.getenv("EMAIL_TO", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))

client = OpenAI(api_key=OPENAI_API_KEY)

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


# ─── OpenAI 分析 ──────────────────────────────────────────────────────────────
def analyze_paper(paper: dict) -> dict:
    """GPT-4o で論文を解析・日本語構造化する"""
    prompt = f"""以下の英語医学論文を日本語で詳細に分析してください。

【タイトル】{paper['title']}
【要旨】{paper['abstract']}

以下のJSON形式で返してください（すべての値は日本語）:
{{
  "title_ja": "論文タイトルの正確な日本語訳",
  "study_type": "研究デザイン（例: ランダム化比較試験・メタ分析・コホート研究・症例対照研究・横断研究・症例報告）",
  "medical_field": "医学分野（例: 循環器内科・腫瘍学・神経科学）",
  "emoji": "研究内容を表す絵文字1つ（医療系が望ましい）",
  "background": "背景・研究目的を2〜3文で分かりやすく説明",
  "methods": "研究方法・デザインを2〜3文で説明",
  "population": "研究対象者の情報（例: 成人2型糖尿病患者1,234名、平均年齢62歳）",
  "intervention": "介入・比較内容（観察研究の場合は暴露因子）",
  "primary_outcome": "主要アウトカム指標（1文）",
  "key_findings": [
    "主要な発見1（必ず具体的な数値・統計を含む）",
    "主要な発見2（必ず具体的な数値・統計を含む）",
    "主要な発見3（必ず具体的な数値・統計を含む）"
  ],
  "conclusion": "研究の結論・まとめを2〜3文で説明",
  "clinical_significance": "この研究が臨床現場に与えるインパクト・意義（1〜2文）",
  "limitations": "研究の限界・注意点（1〜2文）",
  "keywords_ja": ["キーワード1", "キーワード2", "キーワード3"],
  "impact_score": 1〜5の整数（臨床的重要度: 5が最高）,
  "evidence_level": "高|中|低"
}}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    result = json.loads(response.choices[0].message.content)
    result.update({
        "pmid":    paper["pmid"],
        "authors": paper["authors"],
        "journal": paper["journal"],
        "pub_date": paper["pub_date"],
        "url":     paper["url"],
    })
    return result


# ─── HTML 生成ヘルパー ────────────────────────────────────────────────────────
def highlight_numbers(text: str) -> str:
    """数値・統計値を赤くハイライトする"""
    return re.sub(
        r"(\d+(?:[.,]\d+)?(?:\s*(?:%|倍|名|例|件|mg|kg|mmHg|年|ヶ月|週|日|時間|割|p\s*[=<>]\s*0\.\d+|HR\s*\d|OR\s*\d|RR\s*\d|CI\s*\d)))",
        r'<span class="num">\1</span>',
        text,
    )


def stars(score: int) -> str:
    """インパクトスコアを星アイコンで返す"""
    filled = "★" * max(0, min(5, score))
    empty  = "☆" * (5 - max(0, min(5, score)))
    return f'<span class="star-filled">{filled}</span><span class="star-empty">{empty}</span>'


def evidence_badge(level: str) -> str:
    css = {"高": "ev-high", "中": "ev-mid", "低": "ev-low"}.get(level, "ev-mid")
    return f'<span class="ev-badge {css}">エビデンス: {level}</span>'


def safe(text: str) -> str:
    return html_module.escape(str(text))


# ─── HTML カード生成 ──────────────────────────────────────────────────────────
def generate_html_card(paper: dict, index: int) -> str:
    grad_deg, c1, c2 = GRADIENTS[index % len(GRADIENTS)]
    grad = f"linear-gradient({grad_deg}, {c1}, {c2})"

    title_ja   = safe(paper.get("title_ja", paper.get("title", "タイトル不明")))
    study_type = safe(paper.get("study_type", ""))
    field      = safe(paper.get("medical_field", ""))
    emoji      = paper.get("emoji", "🔬")
    background = highlight_numbers(safe(paper.get("background", "")))
    methods    = highlight_numbers(safe(paper.get("methods", "")))
    population = highlight_numbers(safe(paper.get("population", "")))
    interv     = highlight_numbers(safe(paper.get("intervention", "")))
    outcome    = highlight_numbers(safe(paper.get("primary_outcome", "")))
    conclusion = highlight_numbers(safe(paper.get("conclusion", "")))
    clin_sig   = highlight_numbers(safe(paper.get("clinical_significance", "")))
    limits     = highlight_numbers(safe(paper.get("limitations", "")))
    findings   = paper.get("key_findings", [])
    keywords   = paper.get("keywords_ja", [])
    impact     = int(paper.get("impact_score", 3))
    ev_level   = paper.get("evidence_level", "中")
    authors    = paper.get("authors", [])
    journal    = safe(paper.get("journal", ""))
    pub_date   = safe(paper.get("pub_date", ""))
    url        = paper.get("url", "#")
    pmid       = paper.get("pmid", "")

    author_str = "、".join(authors[:3]) + ("ら" if len(authors) > 3 else "")

    finding_items = ""
    for f in findings[:5]:
        finding_items += f"""
        <div class="finding-item">
          <span class="find-check">✓</span>
          <span class="find-text">{highlight_numbers(safe(f))}</span>
        </div>"""

    keyword_tags = "".join(
        f'<span class="kw-tag">{safe(k)}</span>' for k in keywords
    )

    population_block = ""
    if population:
        population_block = f"""
        <div class="pop-box">
          <span class="pop-icon">👥</span>
          <span class="pop-text"><strong>対象:</strong> {population}</span>
        </div>"""

    intervention_block = ""
    if interv:
        intervention_block = f"""
        <div class="interv-box">
          <span class="interv-icon">💊</span>
          <span class="interv-text"><strong>介入/比較:</strong> {interv}</span>
        </div>"""

    outcome_block = ""
    if outcome:
        outcome_block = f"""
        <div class="outcome-box">
          <span class="outcome-icon">🎯</span>
          <span class="outcome-text"><strong>主要アウトカム:</strong> {outcome}</span>
        </div>"""

    limits_block = ""
    if limits:
        limits_block = f"""
      <div class="limits-section">
        <div class="limits-header">
          <span class="limits-icon">⚠️</span>
          <span class="limits-label">研究の限界</span>
        </div>
        <p class="limits-text">{limits}</p>
      </div>"""

    return f"""
    <div class="paper-card">

      <!-- ══ ヘッダー ══ -->
      <div class="card-header" style="background: {grad};">
        <div class="header-top-row">
          <span class="study-badge">{study_type}</span>
          <span class="field-badge">{field}</span>
        </div>
        <div class="card-emoji">{emoji}</div>
        <h2 class="card-title">{title_ja}</h2>
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

        <!-- 背景・目的 -->
        <div class="section sec-bg">
          <div class="sec-header">
            <div class="sec-icon-wrap sec-icon-blue">📋</div>
            <h3 class="sec-title" style="color:#1d4ed8;">背景・研究目的</h3>
          </div>
          <p class="sec-text">{background}</p>
        </div>

        <!-- 方法 -->
        <div class="section sec-methods">
          <div class="sec-header">
            <div class="sec-icon-wrap sec-icon-purple">🔬</div>
            <h3 class="sec-title" style="color:#6d28d9;">研究方法</h3>
          </div>
          <p class="sec-text">{methods}</p>
          {population_block}
          {intervention_block}
          {outcome_block}
        </div>

        <!-- 主要な発見 -->
        <div class="section sec-results">
          <div class="sec-header">
            <div class="sec-icon-wrap sec-icon-green">📊</div>
            <h3 class="sec-title" style="color:#065f46;">主要な発見</h3>
          </div>
          <div class="findings-list">
            {finding_items}
          </div>
        </div>

        <!-- 結論 -->
        <div class="section sec-conclusion">
          <div class="sec-header">
            <div class="sec-icon-wrap sec-icon-amber">💡</div>
            <h3 class="sec-title" style="color:#92400e;">結論</h3>
          </div>
          <p class="sec-text">{conclusion}</p>
        </div>

        <!-- 臨床的意義 -->
        <div class="clinical-sig-box">
          <div class="clin-header">
            <span class="clin-icon">🏥</span>
            <span class="clin-label">臨床的意義・インパクト</span>
          </div>
          <p class="clin-text">{clin_sig}</p>
        </div>

        {limits_block}

        <!-- フッター -->
        <div class="card-footer">
          <div class="footer-row-1">
            <div class="impact-wrap">
              <span class="impact-label">臨床的重要度</span>
              <span class="impact-stars">{stars(impact)}</span>
              <span class="impact-num">{impact}/5</span>
            </div>
            {evidence_badge(ev_level)}
          </div>
          <div class="kw-row">{keyword_tags}</div>
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
    /* ── リセット ── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    /* ── ベース ── */
    body {{
      font-family: 'Noto Sans JP', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
      background: #eef2f7;
      color: #1e293b;
      line-height: 1.7;
      -webkit-text-size-adjust: 100%;
    }}

    /* ── ページラッパー ── */
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
    .top-logo {{
      font-size: 36px;
      margin-bottom: 8px;
    }}
    .top-title {{
      font-size: 22px;
      font-weight: 900;
      color: #ffffff;
      letter-spacing: 0.04em;
      margin-bottom: 6px;
    }}
    .top-date {{
      font-size: 14px;
      color: rgba(255,255,255,0.75);
      margin-bottom: 16px;
    }}
    .top-count-badge {{
      display: inline-block;
      background: rgba(255,255,255,0.15);
      border: 1px solid rgba(255,255,255,0.3);
      color: #ffffff;
      font-size: 13px;
      font-weight: 700;
      padding: 6px 20px;
      border-radius: 50px;
      letter-spacing: 0.03em;
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
      position: relative;
    }}
    .header-top-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .study-badge {{
      display: inline-block;
      background: rgba(255,255,255,0.20);
      border: 1.5px solid rgba(255,255,255,0.45);
      border-radius: 50px;
      padding: 4px 14px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .field-badge {{
      display: inline-block;
      background: rgba(255,255,255,0.10);
      border: 1.5px solid rgba(255,255,255,0.30);
      border-radius: 50px;
      padding: 4px 14px;
      font-size: 11px;
      font-weight: 600;
    }}
    .card-emoji {{
      font-size: 52px;
      line-height: 1;
      margin-bottom: 14px;
      filter: drop-shadow(0 4px 8px rgba(0,0,0,0.3));
    }}
    .card-title {{
      font-size: 19px;
      font-weight: 800;
      line-height: 1.45;
      margin-bottom: 16px;
      text-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}
    .card-meta {{
      font-size: 12px;
      opacity: 0.88;
      line-height: 1.9;
    }}
    .meta-sep {{ opacity: 0.5; margin: 0 4px; }}
    .pmid-label {{
      margin-top: 10px;
      font-size: 11px;
      opacity: 0.6;
      font-family: monospace;
    }}

    /* ── カードボディ ── */
    .card-body {{
      padding: 0 20px 24px;
    }}

    /* ── セクション共通 ── */
    .section {{
      padding: 20px 18px;
      border-radius: 14px;
      margin-top: 16px;
    }}
    .sec-bg      {{ background: #eff6ff; border-left: 5px solid #3b82f6; }}
    .sec-methods {{ background: #f5f3ff; border-left: 5px solid #8b5cf6; }}
    .sec-results {{ background: #f0fdf4; border-left: 5px solid #10b981; }}
    .sec-conclusion {{ background: #fffbeb; border-left: 5px solid #f59e0b; }}

    .sec-header {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }}
    .sec-icon-wrap {{
      width: 34px; height: 34px;
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      font-size: 17px;
      flex-shrink: 0;
    }}
    .sec-icon-blue   {{ background: #dbeafe; }}
    .sec-icon-purple {{ background: #ede9fe; }}
    .sec-icon-green  {{ background: #d1fae5; }}
    .sec-icon-amber  {{ background: #fef3c7; }}

    .sec-title {{
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0.04em;
    }}
    .sec-text {{
      font-size: 14px;
      line-height: 1.75;
      color: #334155;
    }}

    /* ── ポップボックス群 ── */
    .pop-box, .interv-box, .outcome-box {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 10px;
      font-size: 13px;
      margin-top: 10px;
    }}
    .pop-box     {{ background: #e0f2fe; }}
    .interv-box  {{ background: #fae8ff; }}
    .outcome-box {{ background: #dcfce7; }}
    .pop-icon, .interv-icon, .outcome-icon {{ font-size: 16px; flex-shrink: 0; margin-top: 1px; }}
    .pop-text, .interv-text, .outcome-text {{ color: #1e293b; line-height: 1.6; }}

    /* ── 発見リスト ── */
    .findings-list {{ margin-top: 4px; }}
    .finding-item {{
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 12px 14px;
      background: #ffffff;
      border: 1.5px solid #d1fae5;
      border-radius: 12px;
      margin-bottom: 8px;
    }}
    .finding-item:last-child {{ margin-bottom: 0; }}
    .find-check {{
      font-size: 16px;
      font-weight: 900;
      color: #059669;
      flex-shrink: 0;
      margin-top: 1px;
    }}
    .find-text {{
      font-size: 14px;
      line-height: 1.7;
      color: #1e293b;
    }}

    /* ── 数値ハイライト ── */
    .num {{
      font-weight: 800;
      color: #dc2626;
      font-size: 108%;
      letter-spacing: -0.01em;
    }}

    /* ── 臨床的意義 ── */
    .clinical-sig-box {{
      margin-top: 16px;
      padding: 18px 18px 18px 16px;
      background: linear-gradient(135deg, #fef3c7, #fde68a);
      border-radius: 14px;
      border-left: 5px solid #d97706;
    }}
    .clin-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
    }}
    .clin-icon {{ font-size: 18px; }}
    .clin-label {{
      font-size: 13px;
      font-weight: 800;
      color: #92400e;
      letter-spacing: 0.04em;
    }}
    .clin-text {{
      font-size: 14px;
      line-height: 1.75;
      color: #451a03;
    }}

    /* ── 研究の限界 ── */
    .limits-section {{
      margin-top: 12px;
      padding: 14px 16px;
      background: #fafafa;
      border: 1.5px dashed #d1d5db;
      border-radius: 12px;
    }}
    .limits-header {{
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
    }}
    .limits-icon {{ font-size: 15px; }}
    .limits-label {{
      font-size: 12px;
      font-weight: 700;
      color: #6b7280;
      letter-spacing: 0.04em;
    }}
    .limits-text {{
      font-size: 13px;
      color: #4b5563;
      line-height: 1.6;
    }}

    /* ── カードフッター ── */
    .card-footer {{
      margin-top: 20px;
      padding-top: 18px;
      border-top: 2px solid #f1f5f9;
    }}
    .footer-row-1 {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }}
    .impact-wrap {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .impact-label {{
      font-size: 11px;
      color: #64748b;
      font-weight: 600;
      letter-spacing: 0.04em;
    }}
    .impact-stars {{
      font-size: 18px;
      line-height: 1;
    }}
    .star-filled {{ color: #f59e0b; }}
    .star-empty  {{ color: #d1d5db; }}
    .impact-num {{
      font-size: 12px;
      color: #94a3b8;
      font-weight: 700;
    }}

    .ev-badge {{
      font-size: 12px;
      font-weight: 700;
      padding: 4px 14px;
      border-radius: 50px;
      letter-spacing: 0.04em;
    }}
    .ev-high {{ background: #dcfce7; color: #166534; border: 1.5px solid #86efac; }}
    .ev-mid  {{ background: #fef9c3; color: #854d0e; border: 1.5px solid #fde047; }}
    .ev-low  {{ background: #fee2e2; color: #991b1b; border: 1.5px solid #fca5a5; }}

    .kw-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-bottom: 18px;
    }}
    .kw-tag {{
      background: #eef2ff;
      color: #4338ca;
      font-size: 12px;
      font-weight: 600;
      padding: 4px 12px;
      border-radius: 50px;
      border: 1px solid #c7d2fe;
    }}

    .pubmed-btn {{
      display: block;
      width: 100%;
      text-align: center;
      background: linear-gradient(135deg, #2563eb, #6366f1);
      color: #ffffff !important;
      text-decoration: none;
      font-size: 15px;
      font-weight: 800;
      padding: 14px 28px;
      border-radius: 50px;
      letter-spacing: 0.04em;
      box-shadow: 0 4px 16px rgba(37,99,235,0.35);
      transition: all 0.2s;
    }}
    .btn-arrow {{
      font-size: 18px;
      font-weight: 900;
      margin-left: 4px;
    }}

    /* ── フッター ── */
    .page-footer {{
      text-align: center;
      padding: 24px 0 8px;
      color: #94a3b8;
      font-size: 12px;
      line-height: 1.8;
    }}

    /* ── レスポンシブ ── */
    @media (max-width: 480px) {{
      .page-wrapper {{ padding: 12px 10px 32px; }}
      .card-title {{ font-size: 16px; }}
      .top-title  {{ font-size: 18px; }}
      .card-emoji {{ font-size: 42px; }}
      .footer-row-1 {{ flex-direction: column; align-items: flex-start; }}
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
      <p>🔬 PubMed × OpenAI GPT-4o で自動生成</p>
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

    print(f"\n📝 {len(raw_papers)}本の論文をOpenAI GPT-4oで分析中...")
    analyzed: list[dict] = []
    for i, paper in enumerate(raw_papers, 1):
        print(f"  ({i}/{len(raw_papers)}) 分析中: PMID {paper['pmid']}")
        try:
            result = analyze_paper(paper)
            analyzed.append(result)
        except Exception as e:
            print(f"  ⚠️ 分析エラー (PMID {paper['pmid']}): {e}")
        time.sleep(0.5)

    if not analyzed:
        print("⚠️ 分析できた論文がありません。処理を終了します。")
        return

    print(f"\n🎨 HTMLメール生成中（{len(analyzed)}本）...")
    email_html = generate_email_html(analyzed)

    print("📧 メール送信中...")
    send_email(email_html, len(analyzed))
    print("\n🎉 完了！")


if __name__ == "__main__":
    main()
