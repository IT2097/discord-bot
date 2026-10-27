"""
Googleスプレッドシートから会員一覧を読み込むモジュール。

【前提とするスプレッドシートの形式】
1行目をヘッダー行として、以下の列名を想定しています（列の順番は自由）。
  - DiscordID          : 会員のDiscordユーザーID
  - 名前（本名）        : Webページ上に表示する名前
  - 年齢・性別          : 一覧に表示する属性情報
  - 都道府県①・市町村① 〜 都道府県⑤・市町村⑤ : 活動地域（最大5か所。①が主な地域）
  - 業種・事業 / 活動内容 : 1行に1件
  - URL①〜③（任意。列が無くても動く）

ヘッダー名が違う場合は下の HEADER_* 定数を書き換えてください。

【1人が複数行にまたがるシートへの対応】
「業種・事業内容ごとに行が分かれていて、同じ人が複数行に登場する」形式に対応しています。
「名前（本名）」が同じ行は1人分としてまとめ、業種・事業内容は一覧で保持します。
都道府県①〜⑤・市町村①〜⑤は、同じ人のどの行にも同じ値が入っている想定で、最初の行から読みます。
"""

from __future__ import annotations

import os
import time
import threading

import gspread
from google.oauth2.service_account import Credentials

# --- 設定（必要に応じて変更してください） ---
HEADER_DISCORD_ID = "DiscordID"
HEADER_NAME = "名前（本名）"
HEADER_AGE = "年齢"
HEADER_GENDER = "性別"
HEADER_BUSINESS_TYPE = "業種"
HEADER_BUSINESS_CONTENT = "事業 / 活動内容"
HEADER_URL_1 = "URL①"
HEADER_URL_2 = "URL②"
HEADER_URL_3 = "URL③"

# 活動地域（都道府県と市町村のセット）。①が主な地域
MAX_LOCATIONS = 5
_CIRCLED = "①②③④⑤"
LOCATION_HEADERS = [(f"都道府県{_CIRCLED[i]}", f"市町村{_CIRCLED[i]}") for i in range(MAX_LOCATIONS)]
# 以前の1か所だけの列名（「都道府県」「市町村」）を使っているシートにも対応する
_LEGACY_LOCATION_HEADERS = ("都道府県", "市町村")
HEADER_PREFECTURE, HEADER_CITY = LOCATION_HEADERS[0]

CACHE_SECONDS = 300  # スプレッドシートを毎回読みに行かず、5分間だけ結果をキャッシュする

# 業種のプルダウン選択肢を読み込む「設定用」シートの名前・見出し名
BUSINESS_TYPE_SETTINGS_SHEET_NAME = os.getenv("BUSINESS_TYPE_SETTINGS_SHEET_NAME", "設定用")
BUSINESS_TYPE_SETTINGS_HEADER = os.getenv("BUSINESS_TYPE_SETTINGS_HEADER", "業種1")

# 必ず必要な列（無いとエラー）。URLや都道府県②〜⑤は無くても動く
_REQUIRED_HEADERS = [
    HEADER_DISCORD_ID,
    HEADER_NAME,
    HEADER_AGE,
    HEADER_GENDER,
    HEADER_BUSINESS_TYPE,
    HEADER_BUSINESS_CONTENT,
]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0}
_business_type_cache = {"data": None, "fetched_at": 0.0}


def _get_client():
    """
    サービスアカウントの認証情報から gspread クライアントを作成する。
    環境変数 GOOGLE_SERVICE_ACCOUNT_JSON に、サービスアカウントのJSONキーの中身をそのまま入れておく。
    """
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    import json

    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    return gspread.authorize(creds)


def location_columns(headers: list[str]) -> list[tuple[int | None, int | None]]:
    """
    見出し行から、都道府県①〜⑤・市町村①〜⑤の列番号（0始まり。無ければ None）を返す。
    ①が無いシートでは、以前の「都道府県」「市町村」の列を①として扱う。
    """
    normalized = [str(h).strip() for h in headers]

    def find(name):
        return normalized.index(name) if name in normalized else None

    columns = [(find(p), find(c)) for p, c in LOCATION_HEADERS]
    if columns[0] == (None, None):
        columns[0] = (find(_LEGACY_LOCATION_HEADERS[0]), find(_LEGACY_LOCATION_HEADERS[1]))
    return columns


def read_locations(row: list[str], columns) -> list[dict]:
    """1行から、入っている活動地域だけを順番どおりに取り出す。"""
    locations = []
    for pref_col, city_col in columns:
        pref = str(row[pref_col]).strip() if pref_col is not None and pref_col < len(row) else ""
        city = str(row[city_col]).strip() if city_col is not None and city_col < len(row) else ""
        if pref or city:
            locations.append({"prefecture": pref, "city": city})
    return locations


def _fetch_members_from_sheet():
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    sheet_name = os.getenv("SHEET_NAME", "会員")

    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    client = _get_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    values = sheet.get_all_values()
    if not values:
        return []

    headers = [str(h).strip() for h in values[0]]
    missing = [h for h in _REQUIRED_HEADERS if h not in headers]
    if missing:
        raise RuntimeError(f"「{sheet_name}」シートの1行目に、次の見出しが見つかりません：{'、'.join(missing)}")

    col = {h: headers.index(h) for h in headers if h}
    loc_columns = location_columns(headers)

    def cell(row, header):
        index = col.get(header)
        return str(row[index]).strip() if index is not None and index < len(row) else ""

    # 「名前（本名）」をキーに、複数行にまたがる情報を1人分にまとめる。
    members_by_name: dict[str, dict] = {}

    for row in values[1:]:
        name = cell(row, HEADER_NAME)
        if not name:
            continue  # 名前が空の行はスキップ

        entry = members_by_name.get(name)
        if entry is None:
            locations = read_locations(row, loc_columns)
            first = locations[0] if locations else {"prefecture": "", "city": ""}
            entry = {
                "name": name,
                "discord_id": "",
                "age": cell(row, HEADER_AGE),
                "gender": cell(row, HEADER_GENDER),
                # 以前からの呼び方（主な地域＝①）も残しておく
                "prefecture": first["prefecture"],
                "city": first["city"],
                "locations": locations,
                "businesses": [],
                "urls": [],
            }
            members_by_name[name] = entry

        discord_id = cell(row, HEADER_DISCORD_ID).lstrip("'")
        if discord_id and not entry["discord_id"]:
            entry["discord_id"] = discord_id

        business_type = cell(row, HEADER_BUSINESS_TYPE)
        business_content = cell(row, HEADER_BUSINESS_CONTENT)
        if business_type or business_content:
            entry["businesses"].append({"type": business_type, "content": business_content})

        for header in (HEADER_URL_1, HEADER_URL_2, HEADER_URL_3):
            url = cell(row, header)
            if url and url not in entry["urls"]:
                entry["urls"].append(url)

    # DiscordIDが1行も見つからなかった人は、Discordと紐付けできないため一覧から除外する
    return [entry for entry in members_by_name.values() if entry["discord_id"]]


def format_locations(locations: list[dict], separator: str = "、") -> str:
    """活動地域を「北海道札幌市、東京都港区」のような1行の文字にする。"""
    return separator.join(f"{loc['prefecture']}{loc['city']}" for loc in locations if loc["prefecture"] or loc["city"])


def get_members(force_refresh: bool = False):
    """会員一覧を返す。CACHE_SECONDS以内の再取得はキャッシュを返す。"""
    with _lock:
        now = time.time()
        is_stale = (now - _cache["fetched_at"]) > CACHE_SECONDS

        if force_refresh or is_stale or _cache["data"] is None:
            _cache["data"] = _fetch_members_from_sheet()
            _cache["fetched_at"] = now

        return list(_cache["data"])


def find_member_name(discord_id: str) -> str | None:
    """DiscordIDから名前を引く（見つからなければNone）。"""
    for member in get_members():
        if member["discord_id"] == str(discord_id):
            return member["name"]
    return None


def find_member(discord_id: str, force_refresh: bool = False) -> dict | None:
    """
    DiscordIDからプロフィール全体（名前・年齢・性別・活動地域・業種等）を引く。
    force_refresh=True で、キャッシュを無視して最新の内容を取得する。
    """
    for member in get_members(force_refresh=force_refresh):
        if member["discord_id"] == str(discord_id):
            return member
    return None


def _fetch_business_type_options() -> list[str]:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        sheet = spreadsheet.worksheet(BUSINESS_TYPE_SETTINGS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound as error:
        raise RuntimeError(
            f"「{BUSINESS_TYPE_SETTINGS_SHEET_NAME}」という名前のシートが見つかりませんでした。"
        ) from error

    import unicodedata

    def _norm(text: str) -> str:
        # 全角/半角（「業種１」と「業種1」など）や空白の違いを吸収して比較する
        return "".join(unicodedata.normalize("NFKC", str(text)).split())

    header_row = sheet.row_values(1)
    target = _norm(BUSINESS_TYPE_SETTINGS_HEADER)
    normalized_headers = [_norm(h) for h in header_row]

    if target not in normalized_headers:
        found = "、".join(h for h in header_row if str(h).strip()) or "（1行目が空です）"
        raise RuntimeError(
            f"「{BUSINESS_TYPE_SETTINGS_SHEET_NAME}」シートの1行目に"
            f"「{BUSINESS_TYPE_SETTINGS_HEADER}」という見出しの列が見つかりませんでした。"
            f"（1行目にある見出し：{found}）"
        )

    col_index = normalized_headers.index(target) + 1
    column_values = sheet.col_values(col_index)[1:]  # 1行目（見出し）を除く
    return [value.strip() for value in column_values if value.strip()]


def get_business_type_options(force_refresh: bool = False) -> list[str]:
    """「設定用」シートの「業種1」列から、業種のプルダウン選択肢を取得する（短時間キャッシュ付き）。"""
    with _lock:
        now = time.time()
        is_stale = (now - _business_type_cache["fetched_at"]) > CACHE_SECONDS

        if force_refresh or is_stale or _business_type_cache["data"] is None:
            _business_type_cache["data"] = _fetch_business_type_options()
            _business_type_cache["fetched_at"] = now

        return list(_business_type_cache["data"])
