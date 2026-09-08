"""
マッチング成立履歴を、スプレッドシートの別シート（タブ）に永続化するモジュール。

match_store.py はBotのメモリ上でしか状態を持たないため、Railwayの再起動・
再デプロイのたびに「すでにマッチング済み」の情報が消えてしまう。
こちらは同じスプレッドシート内に新しいシート（デフォルト名「マッチング履歴」）を
作り、そこにマッチング成立ペアを記録することで、再起動をまたいでも
「マッチング中」の判定が保たれるようにする。

サービスアカウントは既存のスプレッドシートにすでに編集者として共有済みなので、
このためだけに追加の共有設定は不要（同じスプレッドシート内にタブが増えるだけ）。
"""

from __future__ import annotations

import os
import time
import threading
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MATCH_HISTORY_SHEET_NAME = os.getenv("MATCH_HISTORY_SHEET_NAME", "マッチング履歴")
_HEADERS = ["申請者DiscordID", "相手DiscordID", "マッチ日時"]

CACHE_SECONDS = 30  # 読み取りは短時間だけキャッシュし、Sheets APIへの呼び出しを抑える

_lock = threading.Lock()
_cache = {"pairs": None, "fetched_at": 0.0}


def _get_client():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    import json

    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_WRITE_SCOPES)
    return gspread.authorize(creds)


def _get_worksheet():
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        return spreadsheet.worksheet(MATCH_HISTORY_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        # シートが無ければ自動で作成し、見出し行を入れる
        sheet = spreadsheet.add_worksheet(
            title=MATCH_HISTORY_SHEET_NAME, rows=1000, cols=len(_HEADERS)
        )
        sheet.append_row(_HEADERS)
        return sheet


def _fetch_pairs_from_sheet() -> set[frozenset]:
    sheet = _get_worksheet()
    rows = sheet.get_all_records(expected_headers=_HEADERS)

    pairs: set[frozenset] = set()
    for row in rows:
        a = str(row.get(_HEADERS[0], "")).strip()
        b = str(row.get(_HEADERS[1], "")).strip()
        if a and b:
            pairs.add(frozenset({a, b}))
    return pairs


def get_all_matched_pairs(force_refresh: bool = False) -> set[frozenset]:
    """マッチング成立済みの全ペアを返す（短時間キャッシュ付き）。"""
    with _lock:
        now = time.time()
        is_stale = (now - _cache["fetched_at"]) > CACHE_SECONDS

        if force_refresh or is_stale or _cache["pairs"] is None:
            _cache["pairs"] = _fetch_pairs_from_sheet()
            _cache["fetched_at"] = now

        return set(_cache["pairs"])


def append_match(user_a: str, user_b: str) -> None:
    """
    マッチング成立を履歴シートに追記する。
    すでに記録済みのペアであれば何もしない（重複防止）。
    """
    pair = frozenset({str(user_a), str(user_b)})

    if pair in get_all_matched_pairs():
        return

    sheet = _get_worksheet()
    sheet.append_row([str(user_a), str(user_b), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

    with _lock:
        if _cache["pairs"] is not None:
            _cache["pairs"].add(pair)
