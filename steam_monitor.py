#!/usr/bin/env python3
"""Fetch the three Steam China charts and save one CSV file per day."""

import csv
import html
import json
import re
import sys
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://store.steampowered.com"
USER_AGENT = "steam-monitor/1.0 (chart data collector)"
OUTPUT_DIR = Path(__file__).resolve().parent / "data"


class ChartTableParser(HTMLParser):
    """Extract the first chart table without depending on generated CSS names."""

    def __init__(self):
        super().__init__()
        self.in_chart_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.current_cell = []
        self.current_cell_attrs = {}
        self.current_row = []
        self.headers = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table" and not self.in_chart_table:
            self.in_chart_table = "cCE5WXLdwQ4-" in attributes.get("class", "")
            if self.in_chart_table:
                self.table_depth = 1
            return
        if not self.in_chart_table:
            return
        if tag == "table":
            self.table_depth += 1
        elif tag == "tr" and self.table_depth == 1:
            self.in_row = True
            self.current_row = []
        elif tag in ("th", "td") and self.in_row:
            self.in_cell = True
            self.current_cell = []
            self.current_cell_attrs = attributes
        elif tag == "a" and self.in_cell:
            href = attributes.get("href")
            if href:
                self.current_cell.append(("href", href))

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(("text", data))

    def handle_endtag(self, tag):
        if not self.in_chart_table:
            return
        if tag in ("th", "td") and self.in_cell:
            self.current_row.append((self.current_cell_attrs, self.current_cell))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                if not self.headers:
                    self.headers = [clean_text(cell) for _, cell in self.current_row]
                else:
                    self.rows.append(self.current_row)
            self.in_row = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_chart_table = False


def clean_text(parts):
    text = "".join(value for kind, value in parts if kind == "text")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def first_href(parts):
    return next((value for kind, value in parts if kind == "href"), "")


def fetch(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", "replace")


def fetch_app_details(appids):
    appids = sorted(set(str(appid) for appid in appids if str(appid).strip()))
    if not appids:
        return {}
    details = {}
    for appid in appids:
        query = appid
        url = f"https://store.steampowered.com/api/appdetails?appids={query}&cc=cn&l=schinese"
        payload = json.loads(fetch(url))
        for appid, result in payload.items():
            if isinstance(result, dict) and result.get("success"):
                details[str(appid)] = result.get("data", {})
    return details


def extract_rg_rank_payload(page, marker):
    pos = page.find(marker)
    if pos == -1:
        return None

    rg_pos = page.rfind("rgRanks", 0, pos)
    if rg_pos == -1:
        return None

    arr_start = page.find("[", rg_pos)
    if arr_start == -1:
        return None

    decoded = page[arr_start: pos + 20000]
    decoded = re.sub(r'\\+"', '"', decoded)
    arr_start = 0

    depth = 0
    in_string = False
    escaped = False
    arr_end = None
    for index in range(arr_start, len(decoded)):
        ch = decoded[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_end = index + 1
                    break
    if arr_end is None:
        return None
    try:
        return json.loads(decoded[arr_start:arr_end])
    except json.JSONDecodeError:
        return None


def parse_top_selling(url, page):
    parser = ChartTableParser()
    parser.feed(page)
    if not parser.headers or not parser.rows:
        raise RuntimeError(f"没有在页面中找到 top_selling 榜单表格: {url}")

    records = []
    for row in parser.rows[:100]:
        values = [clean_text(cell) for _, cell in row]
        link = first_href(row[2][1]) if len(row) > 2 else ""
        match = re.search(r"/app/(\d+)", urlparse(link).path)
        if not match or len(values) < 3:
            continue

        price = values[3] if len(values) > 3 else ""
        rank_change = values[4] if len(values) > 4 else ""
        change_cell = row[4] if len(row) > 4 else ({}, [])
        change_class = change_cell[0].get("class", "")
        change_direction = "up" if "Up" in change_class else "down" if "Down" in change_class else ""

        records.append({
            "chart_type": "top_selling",
            "rank": values[1] if len(values) > 1 else "",
            "name": values[2],
            "app_id": match.group(1),
            "price": price,
            "rank_change": rank_change,
            "rank_change_direction": change_direction,
            "extra_value": values[5] if len(values) > 5 else "",
            "current_players": "",
            "peak_players": "",
            "source_url": url,
        })
    return records


def parse_most_played(url, page):
    payload = extract_rg_rank_payload(page, "nConcurrentInGame")
    if not payload:
        raise RuntimeError(f"没有在页面中找到 most_played 榜单数据: {url}")

    appids = [item["itemKey"]["appid"] for item in payload[:100]]
    details = fetch_app_details(appids)
    records = []
    for item in payload[:100]:
        appid = item["itemKey"]["appid"]
        app_data = details.get(str(appid), {})
        price = ""
        price_overview = app_data.get("price_overview") or {}
        price = price_overview.get("final_formatted") or price_overview.get("initial_formatted") or ""

        records.append({
            "chart_type": "most_played",
            "rank": item.get("nRank", ""),
            "name": app_data.get("name", ""),
            "app_id": appid,
            "price": price,
            "rank_change": "",
            "rank_change_direction": "",
            "extra_value": "",
            "current_players": item.get("nConcurrentInGame", ""),
            "peak_players": item.get("nPeakInGame", ""),
            "source_url": url,
        })
    return records


def parse_weekly_top_sellers(url, page):
    payload = extract_rg_rank_payload(page, "nRankLastWeek")
    if not payload:
        raise RuntimeError(f"没有在页面中找到 weekly_top_sellers 榜单数据: {url}")

    appids = [item["itemKey"]["appid"] for item in payload[:100]]
    details = fetch_app_details(appids)
    records = []
    for item in payload[:100]:
        appid = item["itemKey"]["appid"]
        app_data = details.get(str(appid), {})
        price = ""
        price_overview = app_data.get("price_overview") or {}
        price = price_overview.get("final_formatted") or price_overview.get("initial_formatted") or ""

        last_week_rank = item.get("nRankLastWeek")
        rank_change = ""
        rank_change_direction = ""
        if last_week_rank is not None:
            rank_change = last_week_rank - item.get("nRank", 0)
            rank_change_direction = "up" if rank_change < 0 else "down" if rank_change > 0 else "same"

        records.append({
            "chart_type": "weekly_top_sellers",
            "rank": item.get("nRank", ""),
            "name": app_data.get("name", ""),
            "app_id": appid,
            "price": price,
            "rank_change": str(rank_change) if rank_change != "" else "",
            "rank_change_direction": rank_change_direction,
            "extra_value": "",
            "current_players": "",
            "peak_players": "",
            "last_week_rank": last_week_rank if last_week_rank is not None else "",
            "source_url": url,
        })
    return records


def weekly_url(live_page):
    match = re.search(
        r"https://store\.steampowered\.com/charts/topsellers/[^/]+/(\d{4}-\d{1,2}-\d{1,2})",
        live_page,
    )
    if not match:
        raise RuntimeError("无法从 Steam 页面找到当前周榜日期")
    return f"{BASE_URL}/charts/topsellers/CN/{match.group(1)}"


def main():
    live_url = f"{BASE_URL}/charts/topselling/CN"
    live_page = fetch(live_url)
    charts = [
        ("top_selling", live_url, live_page),
        ("most_played", f"{BASE_URL}/charts/mostplayed/CN", None),
        ("weekly_top_sellers", weekly_url(live_page), None),
    ]

    collected_at = datetime.now(timezone.utc).isoformat()
    all_records = []
    counts = {}

    for chart_type, url, page in charts:
        page = page if page is not None else fetch(url)
        if chart_type == "top_selling":
            records = parse_top_selling(url, page)
        elif chart_type == "most_played":
            records = parse_most_played(url, page)
        else:
            records = parse_weekly_top_sellers(url, page)

        counts[chart_type] = len(records)
        print(f"{chart_type}: 成功获取 {len(records)} 条")
        for record in records:
            record["collected_at"] = collected_at
            all_records.append(record)

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / f"{date.today().isoformat()}.csv"
    fields = [
        "collected_at", "chart_type", "rank", "name", "app_id", "price",
        "rank_change", "rank_change_direction", "extra_value", "current_players",
        "peak_players", "last_week_rank", "source_url",
    ]

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for record in all_records:
            row = {field: record.get(field, "") for field in fields}
            writer.writerow(row)

    print(f"完成：总共保存 {len(all_records)} 条记录到 {output_path}")
    for key, count in counts.items():
        print(f"- {key}: {count}")


if __name__ == "__main__":
    main()