"""Annual economic-indicator research report. Offline synthetic example by default.

Reads World Bank WDI-shaped JSON pages, checks that the paginated response is
complete, keeps null / absent / zero distinct, rebases CPI to a common year,
computes descriptive statistics, and writes CSV, JSON, an HTML report with an
SVG chart and a Japanese Markdown report together with provenance metadata.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE / "demo"
API_ROOT = "https://api.worldbank.org/v2/country/"
CPI, INFLATION, GDP_GROWTH = "FP.CPI.TOTL", "FP.CPI.TOTL.ZG", "NY.GDP.MKTP.KD.ZG"
SERIES = {
    CPI: ("消費者物価指数", "index (source-defined base; WDI publishes 2010=100)"),
    INFLATION: ("インフレ率", "annual %"),
    GDP_GROWTH: ("実質GDP成長率", "annual %"),
}
DOCS = [
    ("World Bank Indicator API Queries", "https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries"),
    ("World Bank API Basic Call Structures", "https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures"),
    ("World Bank CPI metadata", "https://databank.worldbank.org/metadataglossary/world-development-indicators/series/FP.CPI.TOTL"),
]
ORIGINS = ("synthetic_fixture", "world_bank_live")
ISO3 = re.compile(r"[A-Z]{3}")
MAX_COUNTRIES = 8
MAX_PAGES = 20
MAX_SPAN_YEARS = 50
MAX_RESPONSE_BYTES = 5_000_000
STAT_FIELDS = ["n_observed", "n_expected", "mean", "median", "stdev", "min", "min_year", "max", "max_year"]


def finite(value):
    """Return a float, None for an explicit null, or raise for anything else."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid numeric observation")
    return float(value)


def page_meta(envelope):
    """Return (page, pages, total) from a World Bank [metadata, data] envelope."""
    if not (isinstance(envelope, list) and len(envelope) == 2
            and isinstance(envelope[0], dict) and isinstance(envelope[1], list)):
        raise ValueError("API error or unexpected envelope")
    try:
        page, pages, total = (int(envelope[0][key]) for key in ("page", "pages", "total"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("pagination metadata missing or not an integer") from None
    if not 1 <= pages <= MAX_PAGES or not 1 <= page <= pages or total < 0:
        raise ValueError("pagination bounds")
    return page, pages, total


def parse_pages(pages):
    """Validate a complete paginated response and return sorted observations."""
    if not isinstance(pages, list) or not pages:
        raise ValueError("no pages")
    seen_pages, raw, expected = set(), [], None
    for envelope in pages:
        page, count, total = page_meta(envelope)
        if expected is None:
            expected = (count, total)
        if (count, total) != expected or page in seen_pages:
            raise ValueError("pagination inconsistent")
        seen_pages.add(page)
        raw.extend(envelope[1])
    if seen_pages != set(range(1, expected[0] + 1)) or len(raw) != expected[1]:
        raise ValueError("partial response")
    rows, seen = [], set()
    for item in raw:
        indicator = item.get("indicator") if isinstance(item, dict) else None
        country = item.get("countryiso3code") if isinstance(item, dict) else None
        series = indicator.get("id") if isinstance(indicator, dict) else None
        year = str(item.get("date", "")) if isinstance(item, dict) else ""
        if (not isinstance(country, str) or not ISO3.fullmatch(country)
                or series not in SERIES or not re.fullmatch(r"[0-9]{4}", year)):
            raise ValueError("country/indicator/annual date invalid")
        key = (country, series, int(year))
        if key in seen:
            raise ValueError("duplicate observation")
        seen.add(key)
        rows.append({"country": country, "indicator": series, "year": int(year), "value": finite(item.get("value"))})
    return sorted(rows, key=lambda r: (r["country"], r["indicator"], r["year"]))


def rebase(values, year):
    """Rebase an index so that `year` = 100. The base must be observed and positive."""
    base = values.get(year)
    if base is None or base <= 0 or any(v is not None and v <= 0 for v in values.values()):
        raise ValueError("CPI requires positive observed base")
    return {y: None if v is None else v / base * 100 for y, v in sorted(values.items())}


def yoy(values):
    """Percent change versus the immediately preceding year only; gaps are never bridged."""
    return {
        y: None if v is None or values.get(y - 1) is None or values[y - 1] <= 0 else (v / values[y - 1] - 1) * 100
        for y, v in sorted(values.items())
    }


def pp(current, previous):
    """Difference of two rates in percentage points."""
    return None if current is None or previous is None else current - previous


def describe(values):
    """Descriptive statistics over observed values only (no imputation)."""
    observed = [(y, v) for y, v in sorted(values.items()) if v is not None]
    numbers = [v for _, v in observed]
    stats = {"n_observed": len(numbers), "n_expected": len(values)}
    if not numbers:
        return {**stats, **{k: None for k in STAT_FIELDS[2:]}}
    low = min(observed, key=lambda item: item[1])
    high = max(observed, key=lambda item: item[1])
    return {
        **stats,
        "mean": statistics.fmean(numbers),
        "median": statistics.median(numbers),
        "stdev": statistics.stdev(numbers) if len(numbers) > 1 else None,
        "min": low[1], "min_year": low[0],
        "max": high[1], "max_year": high[0],
    }


def annualized_change(values, start, end):
    """Compound annual % change between the window endpoints; None unless both are observed."""
    first, last = values.get(start), values.get(end)
    if first is None or last is None or first <= 0 or last <= 0 or end <= start:
        return None
    return ((last / first) ** (1 / (end - start)) - 1) * 100


def analyze(rows, countries, start, end, base):
    if (not isinstance(countries, list) or not countries or len(countries) != len(set(countries))
            or any(not isinstance(c, str) or not ISO3.fullmatch(c) for c in countries)):
        raise ValueError("invalid country list")
    if not all(isinstance(v, int) for v in (start, end, base)) or not start <= base <= end or end - start > MAX_SPAN_YEARS:
        raise ValueError("invalid analysis scope")
    lookup = {(r["country"], r["indicator"], r["year"]): r["value"] for r in rows}
    expected = {(c, i, y) for c in countries for i in SERIES for y in range(start, end + 1)}
    if len(lookup) != len(rows) or any(k not in expected for k in lookup):
        raise ValueError("unexpected or duplicate observations")
    years = range(start, end + 1)
    grid, missing = [], []
    for c in countries:
        for i in SERIES:
            for y in years:
                key = (c, i, y)
                status = "absent" if key not in lookup else "null" if lookup[key] is None else "observed"
                grid.append({"country": c, "indicator": i, "year": y, "value": lookup.get(key), "unit": SERIES[i][1], "status": status})
                if status != "observed":
                    missing.append({"country": c, "indicator": i, "year": y, "reason": status})
    derived, summary = {}, []
    for c in countries:
        cpi = {y: lookup.get((c, CPI, y)) for y in years}
        rate = {y: lookup.get((c, INFLATION, y)) for y in years}
        rebased = rebase(cpi, base)
        cpi_yoy = yoy(cpi)
        gaps = {y: pp(rate[y], cpi_yoy[y]) for y in years}
        checked = [abs(v) for v in gaps.values() if v is not None]
        derived[c] = {
            "cpi_rebased": rebased,
            "cpi_yoy_percent": cpi_yoy,
            "inflation_change_pp": {y: pp(rate[y], rate.get(y - 1)) for y in years},
            "published_minus_cpi_yoy_pp": gaps,
            "max_abs_published_minus_cpi_yoy_pp": max(checked) if checked else None,
            "cpi_annualized_change_percent": annualized_change(cpi, start, end),
        }
        summary.append({"country": c, "indicator": CPI, "unit": f"index ({base}=100)", **describe(rebased)})
        for i in (INFLATION, GDP_GROWTH):
            summary.append({"country": c, "indicator": i, "unit": SERIES[i][1], **describe({y: lookup.get((c, i, y)) for y in years})})
    return {
        "grid": grid, "missing": missing, "derived": derived, "summary": summary,
        "expected_cells": len(grid), "observed_cells": len(grid) - len(missing), "base_year": base,
    }


def fetch_live(countries, start, end, transport=None):
    """Fetch WDI (source=2) pages for explicit ISO3 countries and years. `transport` allows offline tests."""
    if (not countries or len(countries) > MAX_COUNTRIES or len(set(countries)) != len(countries)
            or any(not isinstance(c, str) or not ISO3.fullmatch(c) for c in countries)):
        raise ValueError("invalid ISO3 countries")
    if not 1960 <= start <= end <= datetime.now(timezone.utc).year or end - start > MAX_SPAN_YEARS:
        raise ValueError("invalid annual range")
    base_url = API_ROOT + ";".join(countries) + "/indicator/" + ";".join(SERIES)

    def default_get(url):
        with urlopen(Request(url, headers={"User-Agent": "EconomicIndicatorResearchLab/1.0"}), timeout=25) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("response limit")
        return json.loads(data.decode("utf-8"))

    get = transport or default_get
    pages, urls = [], []
    for page in range(1, MAX_PAGES + 1):
        url = base_url + "?" + urlencode({"source": 2, "format": "json", "date": f"{start}:{end}", "per_page": 1000, "page": page})
        envelope = get(url)
        urls.append(url)
        returned, count, _ = page_meta(envelope)
        if returned != page:
            raise ValueError("API returned a different page than requested")
        pages.append(envelope)
        if page == count:
            break
    parse_pages(pages)
    return {
        "data_origin": "world_bank_live", "countries": countries, "start": start, "end": end,
        "pages": pages, "request_urls": urls,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "provider_last_updated": pages[0][0].get("lastupdated"),
    }


def fmt(v):
    return "欠損" if v is None else f"{v:.2f}"


def with_year(value, year):
    return fmt(value) if year is None else f"{fmt(value)} ({year})"


def rounded(value, digits=4):
    """Round floats in nested JSON-like data so outputs are readable and stable."""
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: rounded(v, digits) for k, v in value.items()}
    if isinstance(value, list):
        return [rounded(v, digits) for v in value]
    return value


def chart(derived, countries, base):
    years = sorted(derived[countries[0]]["cpi_rebased"])
    vals = [v for c in countries for v in derived[c]["cpi_rebased"].values() if v is not None]
    lo, hi = min(vals + [100]) - 2, max(vals + [100]) + 2

    def x_of(n):
        return 70 + n / max(1, len(years) - 1) * 830

    def y_of(v):
        return 260 - (v - lo) / (hi - lo) * 200

    parts = ['<svg viewBox="0 0 980 330" role="img" aria-label="CPI rebased; missing years remain gaps">']
    for v in [lo, (lo + hi) / 2, hi]:
        parts.append(f'<path d="M70,{y_of(v):.2f} H900" stroke="#344958"/><text x="12" y="{y_of(v) + 4:.2f}" fill="#adbfcc" font-size="14">{v:.1f}</text>')
    for n, y in enumerate(years):
        parts.append(f'<text x="{x_of(n):.2f}" y="295" text-anchor="middle" fill="#adbfcc" font-size="14">{y}</text>')
    for j, c in enumerate(countries):
        color = ["#39c7bd", "#eeac67", "#8fa4ff", "#de91bf"][j % 4]
        segment = []

        def flush():
            if segment:
                parts.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="3"/>')
                segment.clear()

        for n, y in enumerate(years):
            v = derived[c]["cpi_rebased"][y]
            if v is None:
                flush()
                continue
            segment.append(f"{x_of(n):.2f},{y_of(v):.2f}")
            parts.append(f'<circle cx="{x_of(n):.2f}" cy="{y_of(v):.2f}" r="4" fill="{color}"><title>{html.escape(c)} {y}: {v:.2f}</title></circle>')
        flush()
        parts.append(f'<text x="{70 + j * 150}" y="28" fill="{color}" font-size="15">{html.escape(c)}</text>')
    parts.append(f'<text x="70" y="326" fill="#adbfcc" font-size="12">{base}=100. Missing observations remain gaps.</text></svg>')
    return "".join(parts)


def write_bytes(path, data):
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_text(path, text):
    """Write UTF-8 with LF endings on every OS so outputs and hashes are reproducible."""
    return write_bytes(path, text.encode("utf-8"))


def summary_rows(result):
    for s in result["summary"]:
        cpi_change = result["derived"][s["country"]]["cpi_annualized_change_percent"] if s["indicator"] == CPI else None
        yield {**rounded(s), "label": SERIES[s["indicator"]][0], "annualized_change_percent": rounded(cpi_change)}


def write_report(payload, out, base):
    if not isinstance(payload, dict) or payload.get("data_origin") not in ORIGINS:
        raise ValueError("unknown data origin")
    if any(key not in payload for key in ("pages", "countries", "start", "end")):
        raise ValueError("payload missing pages/countries/start/end")
    result = analyze(parse_pages(payload["pages"]), payload["countries"], payload["start"], payload["end"], base)
    origin_id = payload["data_origin"]
    demo = origin_id == "synthetic_fixture"
    origin = "SYNTHETIC DEMO｜架空の国・架空の数値" if demo else "WORLD BANK｜公開統計の取得時点スナップショット"
    limit = "このデモから実際の国の景気や投資判断について結論は出せません。" if demo else "国ごとの定義・推計方法・改定時期の差を確認してください。取得後の改定は反映されません。"
    countries, latest = payload["countries"], payload["end"]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)

    snapshot_sha = write_text(out / "raw_snapshot.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    prov = {
        "data_origin": origin_id, "snapshot_file": "raw_snapshot.json", "snapshot_sha256": snapshot_sha,
        "fetched_at": payload.get("fetched_at"), "provider_last_updated": payload.get("provider_last_updated"),
        "request_urls": payload.get("request_urls", []), "methodology_sources": DOCS,
        "base_year": base, "start": payload["start"], "end": latest,
    }
    write_text(out / "provenance.json", json.dumps(prov, ensure_ascii=False, indent=2) + "\n")
    quality = rounded({k: v for k, v in result.items() if k not in ("grid", "summary")})
    write_text(out / "quality.json", json.dumps(quality, ensure_ascii=False, indent=2) + "\n")

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["data_origin", "country", "indicator", "year", "value", "unit", "status"])
    writer.writeheader()
    writer.writerows({**r, "data_origin": origin_id} for r in result["grid"])
    write_bytes(out / "indicators.csv", buffer.getvalue().encode("utf-8-sig"))

    stats = list(summary_rows(result))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["data_origin", "country", "indicator", "label", "unit", *STAT_FIELDS, "annualized_change_percent"])
    writer.writeheader()
    writer.writerows({**s, "data_origin": origin_id} for s in stats)
    write_bytes(out / "summary.csv", buffer.getvalue().encode("utf-8-sig"))

    cards = []
    for c in countries:
        v = {r["indicator"]: r["value"] for r in result["grid"] if r["country"] == c and r["year"] == latest}
        cards.append(
            f'<article><h3>{html.escape(c)} · {latest}</h3>'
            f'<p>実質GDP成長率 <strong>{fmt(v[GDP_GROWTH])}{"" if v[GDP_GROWTH] is None else "%"}</strong></p>'
            f'<p>インフレ率 <strong>{fmt(v[INFLATION])}{"" if v[INFLATION] is None else "%"}</strong></p>'
            f'<p>CPI（{base}=100） <strong>{fmt(result["derived"][c]["cpi_rebased"][latest])}</strong></p></article>'
        )
    table = "".join(
        f'<tr><td>{html.escape(r["country"])}</td><td>{SERIES[r["indicator"]][0]}</td><td>{r["year"]}</td>'
        f'<td>{fmt(r["value"])}</td><td>{html.escape(r["unit"])}</td><td>{r["status"]}</td></tr>'
        for r in result["grid"]
    )
    stats_table = "".join(
        f'<tr><td>{html.escape(s["country"])}</td><td>{s["label"]}</td><td>{html.escape(s["unit"])}</td>'
        f'<td>{s["n_observed"]}/{s["n_expected"]}</td><td>{fmt(s["mean"])}</td><td>{fmt(s["median"])}</td><td>{fmt(s["stdev"])}</td>'
        f'<td>{with_year(s["min"], s["min_year"])}</td><td>{with_year(s["max"], s["max_year"])}</td></tr>'
        for s in stats
    )
    checks = "".join(
        f'<li>{html.escape(c)}：CPI年平均変化率（{payload["start"]}→{latest}）{fmt(result["derived"][c]["cpi_annualized_change_percent"])}%。'
        f'公表インフレ率とCPI前年比の最大差 {fmt(result["derived"][c]["max_abs_published_minus_cpi_yoy_pp"])}ポイント。</li>'
        for c in countries
    )
    sources = "".join(f'<li><a href="{html.escape(u, quote=True)}">{html.escape(t)}</a></li>' for t, u in DOCS)
    body = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Economic Indicator Research Lab</title>
<style>body{{background:#101d28;color:#edf4f6;font-family:system-ui,'Yu Gothic',sans-serif;max-width:1120px;margin:48px auto;padding:0 24px;line-height:1.65}}h1{{font-size:36px;letter-spacing:-1px;margin:12px 0}}h2{{font-size:23px}}.label{{color:#39c7bd;font-size:13px;letter-spacing:2px}}.notice{{border-left:4px solid #eeac67;background:#23303a;padding:14px 20px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px;margin:28px 0}}article,.chart{{background:#1b2c39;border:1px solid #344958;border-radius:12px;padding:22px}}article p{{display:flex;justify-content:space-between;gap:20px}}strong{{color:#39c7bd}}.chart{{padding:20px}}svg{{width:100%;height:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{border-bottom:1px solid #344958;padding:9px;text-align:left}}a{{color:#75d6d0}}.muted{{color:#adbfcc}}details{{margin-top:26px}}footer{{margin-top:40px;color:#adbfcc}}</style>
<p class="label">ECONOMIC DATA / RESEARCH / REPRODUCIBILITY</p><h1>経済指標を、出典から読み解く。</h1><p class="muted">自主制作 · {payload["start"]}–{latest} 年次データ</p>
<div class="notice"><b>{origin}</b><br>{limit}</div><div class="cards">{"".join(cards)}</div>
<section class="chart"><h2>物価指数の推移｜{base}=100</h2><p class="muted">同じ基準年・同じ軸で比較。欠損部分の線はつなぎません。指数から国間の物価水準は比較しません。</p>{chart(result["derived"], countries, base)}</section>
<h2>記述統計（観測値のみ・補完なし）</h2><p class="muted">率の平均は各年の単純平均で、複利の平均成長率ではありません。標本標準偏差は観測値2件以上の場合のみ。</p>
<table><tr><th>国</th><th>指標</th><th>単位</th><th>観測数</th><th>平均</th><th>中央値</th><th>標準偏差</th><th>最小（年）</th><th>最大（年）</th></tr>{stats_table}</table>
<ul>{checks}</ul>
<h2>データ品質と計算上の区別</h2><p>{result["expected_cells"]}観測枠のうち{result["observed_cells"]}件が観測済み、{len(result["missing"])}件が欠損。未掲載とnullを区別し、ゼロ補完をしません。</p>
<ul><li>率の差はパーセントポイント。2%から3%は+1ポイントです。</li><li>前年比は直前年がある場合のみ計算。欠損年をまたぎません。</li><li>CPIから計算した前年比と公表インフレ率は別系列として保存し、差を確認するだけで置き換えません。</li><li>実質GDP成長率と名目GDP金額を混ぜません。説明的比較から因果関係や予測は主張しません。</li></ul>
<h2>再現できる調査資料</h2><p><a href="indicators.csv">CSV</a> · <a href="summary.csv">記述統計CSV</a> · <a href="quality.json">品質検査・派生値</a> · <a href="provenance.json">出典・取得情報</a> · <a href="raw_snapshot.json">入力スナップショット</a></p>
<h2>方法・定義の一次資料</h2><ul>{sources}</ul><p class="muted">上の資料はAPIと指標定義の参照です。{"デモの架空数値の出典ではありません。" if demo else "数値の取得URLはprovenance.jsonに保存しています。"}</p>
<details><summary>全観測値を見る（{origin}）</summary><table><tr><th>国</th><th>指標</th><th>年</th><th>値</th><th>単位</th><th>状態</th></tr>{table}</table></details>
<footer>自主制作サンプル｜経済統計の集計・比較・可視化・出典管理。資格や受託実績を示すものではありません。</footer></html>
"""
    write_text(out / "report.html", body)

    lines = [f"# 経済統計レポート（{origin}）", "", f"対象：{', '.join(countries)} / {payload['start']}–{latest}年", "", limit, "", "## 観察", ""]
    for c in countries:
        d = result["derived"][c]
        value = d["cpi_rebased"][latest]
        lines.append(f"- {c}の{latest}年CPI（{base}=100）は{fmt(value)}。基準年からの変化は{fmt(None if value is None else value - 100)}%。"
                     f"{payload['start']}→{latest}年の年平均変化率は{fmt(d['cpi_annualized_change_percent'])}%。")
    lines += ["", "## 記述統計（観測値のみ・補完なし）", "",
              "| 国 | 指標 | 単位 | 観測数 | 平均 | 中央値 | 標準偏差 | 最小（年） | 最大（年） |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in stats:
        lines.append(f"| {s['country']} | {s['label']} | {s['unit']} | {s['n_observed']}/{s['n_expected']} | {fmt(s['mean'])} | {fmt(s['median'])} "
                     f"| {fmt(s['stdev'])} | {with_year(s['min'], s['min_year'])} | {with_year(s['max'], s['max_year'])} |")
    lines += ["", "率の平均は各年の単純平均です。標準偏差は標本標準偏差です。", "", "## 品質と限界", "",
              f"{result['expected_cells']}観測枠中{len(result['missing'])}件が欠損。補完せず対象年をそろえます。", ""]
    for c in countries:
        lines.append(f"- {c}：公表インフレ率とCPIから計算した前年比の最大差は{fmt(result['derived'][c]['max_abs_published_minus_cpi_yoy_pp'])}ポイント（両方が観測された年のみ）。")
    lines += ["", "指数の水準は国間の物価の高さを示しません。比較から因果関係・将来予測は導きません。", "", "## 方法・定義の出典", ""]
    lines += [f"- [{t}]({u})" for t, u in DOCS]
    write_text(out / "report.md", "\n".join(lines) + "\n")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", type=Path, default=HERE / "fixtures" / "demo.json",
                    help="payload JSON (the bundled synthetic fixture, or a saved raw_snapshot.json)")
    ap.add_argument("--live", action="store_true", help="fetch public World Bank WDI data (network)")
    ap.add_argument("--countries", default="JPN,USA", help="comma-separated ISO3 codes for --live")
    ap.add_argument("--start", type=int, default=2020)
    ap.add_argument("--end", type=int, default=2024)
    ap.add_argument("--base-year", type=int, default=2020)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default: demo/ for synthetic data only)")
    a = ap.parse_args(argv)
    out = a.out or DEMO_DIR
    synthetic_only = out.resolve() == DEMO_DIR.resolve()
    try:
        if a.live and synthetic_only:
            raise ValueError("demo/ holds the synthetic demo only; pass --out for live World Bank data")
        if a.live:
            if not a.start <= a.base_year <= a.end:
                raise ValueError("--base-year must be within --start..--end")
            countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
            payload = fetch_live(countries, a.start, a.end)
        else:
            payload = json.loads(a.fixture.read_text(encoding="utf-8"))
        if synthetic_only and (not isinstance(payload, dict) or payload.get("data_origin") != "synthetic_fixture"):
            raise ValueError("demo/ holds the synthetic demo only; pass --out for live or saved World Bank data")
        write_report(payload, out, a.base_year)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "OK", "data_origin": payload["data_origin"], "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
