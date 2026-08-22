"""
Googleスプレッドシートから会員一覧を読み込むモジュール。

【前提とするスプレッドシートの形式】
1行目をヘッダー行として、以下の列名を想定しています（列の順番は自由）。
  - DiscordID   : 会員のDiscordユーザーID（数字のみ。開発者モードでコピーしたID）
  - 名前（本名） : Webページ上に表示する名前

ヘッダー名が違う場合は下の HEADER_DISCORD_ID / HEADER_NAME を書き換えてください。

【1人が複数行にまたがるシートへの対応】
「事業/活動内容ごとに行が分かれていて、同じ人が複数行に登場する」形式のシート
（例: 渡辺大智さんが4行に分かれている等）にも対応しています。
DiscordIDは同じ人のどれか1行にだけ入力すればOKで、同じDiscordIDを持つ行は
自動的に1人分として1件にまとめられます（重複除去）。
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
CACHE_SECONDS = 300  # スプレッドシートを毎回読みに行かず、5分間だけ結果をキャッシュする

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
    rows = sheet.get_all_records()  # 1行目をヘッダーとして辞書のリストを取得

    members = []
    seen_ids = set()

    for row in rows:
        discord_id = str(row.get(HEADER_DISCORD_ID, "")).strip()
        name = str(row.get(HEADER_NAME, "")).strip()

        if not discord_id or not name:
            # DiscordIDか名前が空の行はスキップ（事業内容だけの行、未入力の行など）
            continue

        if discord_id in seen_ids:
            # 同じ人の別の事業/活動の行なのでスキップ（1人1件にまとめる）
            continue

        seen_ids.add(discord_id)
        members.append({"discord_id": discord_id, "name": name})

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
