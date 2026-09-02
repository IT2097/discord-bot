"""
Googleスプレッドシートから会員一覧を読み込むモジュール。

【前提とするスプレッドシートの形式】
1行目をヘッダー行として、以下の列名を想定しています（列の順番は自由）。
  - DiscordID   : 会員のDiscordユーザーID（数字のみ。開発者モードでコピーしたID）
  - 名前（本名） : Webページ上に表示する名前
  - 年齢・性別・都道府県・市町村・業種・事業 / 活動内容 : 一覧に表示する属性情報

ヘッダー名が違う場合は下の HEADER_* 定数を書き換えてください。

【1人が複数行にまたがるシートへの対応】
「事業/活動内容ごとに行が分かれていて、同じ人が複数行に登場する」形式のシート
（例: 渡辺大智さんが4行に分かれている等）に対応しています。
DiscordIDは同じ人のどれか1行にだけ入力すればOKで、「名前（本名）」が同じ行は
自動的に1人分としてまとめられ、業種・事業/活動内容はまとめて一覧で保持されます。
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
HEADER_PREFECTURE = "都道府県"
HEADER_CITY = "市町村"
HEADER_BUSINESS_TYPE = "業種"
HEADER_BUSINESS_CONTENT = "事業 / 活動内容"
HEADER_URL_1 = "URL①"
HEADER_URL_2 = "URL②"
HEADER_URL_3 = "URL③"
CACHE_SECONDS = 300  # スプレッドシートを毎回読みに行かず、5分間だけ結果をキャッシュする

# get_all_records() に明示的に渡す想定ヘッダー。
# これを渡すことで、他の列の見出しが空欄・重複していてもエラーにならない
# （逆にここに書いた列の見出しは、スプレッドシート上で必ず一致させる必要がある）。
_EXPECTED_HEADERS = [
    HEADER_DISCORD_ID,
    HEADER_NAME,
    HEADER_AGE,
    HEADER_GENDER,
    HEADER_PREFECTURE,
    HEADER_CITY,
    HEADER_BUSINESS_TYPE,
    HEADER_BUSINESS_CONTENT,
    HEADER_URL_1,
    HEADER_URL_2,
    HEADER_URL_3,
]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0}


def _get_client():
    """
    サービスアカウントの認証情報から gspread クライアントを作成する。

    環境変数 GOOGLE_SERVICE_ACCOUNT_JSON に、サービスアカウントのJSONキーの
    中身をそのまま（1行の文字列として）設定しておく想定です。
    """
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError(
            "環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。"
        )

    import json

    # strict=False: 環境変数への貼り付け時に生の改行が混入していても許容する
    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    return gspread.authorize(creds)


def _fetch_members_from_sheet():
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    sheet_name = os.getenv("SHEET_NAME", "会員")

    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    client = _get_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    # expected_headers を渡すことで、他の列の見出しが空欄・重複していても
    # エラーにならないようにする（1行目をヘッダーとして辞書のリストを取得）
    rows = sheet.get_all_records(expected_headers=_EXPECTED_HEADERS)

    # 「名前（本名）」をキーに、複数行にまたがる情報を1人分にまとめる。
    # DiscordIDは同じ人のどこか1行にだけ入っていればよいので、
    # 先に名前でグルーピングしてから、最後にDiscordIDが無い人だけ除外する。
    members_by_name: dict[str, dict] = {}

    for row in rows:
        name = str(row.get(HEADER_NAME, "")).strip()
        if not name:
            continue  # 名前が空の行（見出しだけの行など）はスキップ

        entry = members_by_name.get(name)
        if entry is None:
            entry = {
                "name": name,
                "discord_id": "",
                "age": str(row.get(HEADER_AGE, "")).strip(),
                "gender": str(row.get(HEADER_GENDER, "")).strip(),
                "prefecture": str(row.get(HEADER_PREFECTURE, "")).strip(),
                "city": str(row.get(HEADER_CITY, "")).strip(),
                "businesses": [],
                "urls": [],
            }
            members_by_name[name] = entry

        discord_id = str(row.get(HEADER_DISCORD_ID, "")).strip()
        if discord_id and not entry["discord_id"]:
            entry["discord_id"] = discord_id

        business_type = str(row.get(HEADER_BUSINESS_TYPE, "")).strip()
        business_content = str(row.get(HEADER_BUSINESS_CONTENT, "")).strip()
        if business_type or business_content:
            entry["businesses"].append({"type": business_type, "content": business_content})

        for header in (HEADER_URL_1, HEADER_URL_2, HEADER_URL_3):
            url = str(row.get(header, "")).strip()
            if url and url not in entry["urls"]:
                entry["urls"].append(url)

    # DiscordIDが1行も見つからなかった人は、Discordと紐付けできないため一覧から除外する
    members = [entry for entry in members_by_name.values() if entry["discord_id"]]

    return members


def get_members(force_refresh: bool = False):
    """
    会員一覧を返す。CACHE_SECONDS以内の再取得はキャッシュを返す。
    """
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


def find_member(discord_id: str) -> dict | None:
    """DiscordIDからプロフィール全体（名前・年齢・性別・都道府県・市町村・業種等）を引く。"""
    for member in get_members():
        if member["discord_id"] == str(discord_id):
            return member
    return None
