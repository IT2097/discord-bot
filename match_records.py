"""
マッチング成立履歴を、スプレッドシートの別シート（タブ）に永続化するモジュール。

match_store.py はBotのメモリ上でしか状態を持たないため、Railwayの再起動・
再デプロイのたびに「すでにマッチング済み」の情報が消えてしまう。
こちらは同じスプレッドシート内に新しいシート（デフォルト名「マッチング履歴」）を
作り、そこにマッチング成立ペアを記録することで、再起動をまたいでも
「マッチング中」の判定が保たれるようにする。

サービスアカウントは既存のスプレッドシートにすでに編集者として共有済みなので、
このためだけに追加の共有設定は不要（同じスプレッドシート内にタブが増えるだけ）。

【Sheets APIの読み込み回数制限（1分あたり60回）への対策】
- シート（Worksheet）オブジェクトを使い回し、毎回スプレッドシートを開き直さない
- 複数ペアをまとめて書き込む append_matches() を用意（!マッチング復元 で使用）
- 429（回数制限）エラーが出たら、少し待って自動で再試行する
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

# 429（回数制限）エラー時の再試行設定
_RETRY_COUNT = 4
_RETRY_WAIT_SECONDS = 20

_lock = threading.Lock()
_cache = {"pairs": None, "fetched_at": 0.0}
_worksheet_lock = threading.Lock()
_worksheet_cache: dict = {"sheet": None}


def _get_client():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    import json

    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_WRITE_SCOPES)
    return gspread.authorize(creds)


def _is_rate_limited(error: Exception) -> bool:
    return isinstance(error, gspread.exceptions.APIError) and "429" in str(error)


def _with_retry(func, *args, **kwargs):
    """429（回数制限）エラーのときだけ、少し待ってから再試行する。"""
    for attempt in range(_RETRY_COUNT):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as error:
            if not _is_rate_limited(error) or attempt == _RETRY_COUNT - 1:
                raise
            print(f"Sheets APIの回数制限に達したため、{_RETRY_WAIT_SECONDS}秒待って再試行します…")
            time.sleep(_RETRY_WAIT_SECONDS)


def _open_worksheet():
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


def _get_worksheet():
    """
    シートオブジェクトを使い回す（毎回スプレッドシートを開き直すと、
    それだけで読み込み回数を消費してしまうため）。
    """
    with _worksheet_lock:
        if _worksheet_cache["sheet"] is None:
            _worksheet_cache["sheet"] = _with_retry(_open_worksheet)
        return _worksheet_cache["sheet"]


def _reset_worksheet_cache():
    with _worksheet_lock:
        _worksheet_cache["sheet"] = None


def _fetch_pairs_from_sheet() -> set[frozenset]:
    sheet = _get_worksheet()
    try:
        rows = _with_retry(sheet.get_all_records, expected_headers=_HEADERS)
    except gspread.exceptions.APIError as error:
        if _is_rate_limited(error):
            raise
        # シートが削除・作り直しされた等で古いオブジェクトが使えない場合は開き直す
        _reset_worksheet_cache()
        sheet = _get_worksheet()
        rows = _with_retry(sheet.get_all_records, expected_headers=_HEADERS)

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
    append_matches([(user_a, user_b)])


def append_matches(pairs: list[tuple[str, str]]) -> int:
    """
    複数のマッチング成立ペアを、1回の書き込みでまとめて履歴シートに追記する。
    すでに記録済みのペア・同じペアの重複は除く。追記した件数を返す。
    """
    existing = get_all_matched_pairs(force_refresh=True)

    rows = []
    added: set[frozenset] = set()
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for user_a, user_b in pairs:
        pair = frozenset({str(user_a), str(user_b)})
        if len(pair) != 2 or pair in existing or pair in added:
            continue
        rows.append([str(user_a), str(user_b), now_text])
        added.add(pair)

    if not rows:
        return 0

    sheet = _get_worksheet()
    # RAW：DiscordIDのような長い数字が指数表記・桁落ちしないよう、そのまま文字として書き込む
    _with_retry(sheet.append_rows, rows, value_input_option="RAW")

    with _lock:
        if _cache["pairs"] is not None:
            _cache["pairs"].update(added)

    return len(rows)


def remove_match(user_a: str, user_b: str) -> bool:
    """
    指定した2人のマッチング成立記録を履歴シートから削除する（!マッチング解除 用）。
    削除した場合は True、記録が見つからなかった場合は False を返す。
    """
    target = frozenset({str(user_a), str(user_b)})
    sheet = _get_worksheet()
    all_values = _with_retry(sheet.get_all_values)

    # 下の行から消していく（上から消すと行番号がずれるため）
    rows_to_delete = [
        row_idx
        for row_idx, row in enumerate(all_values[1:], start=2)
        if len(row) >= 2 and frozenset({row[0].strip(), row[1].strip()}) == target
    ]
    for row_idx in reversed(rows_to_delete):
        _with_retry(sheet.delete_rows, row_idx)

    with _lock:
        if _cache["pairs"] is not None:
            _cache["pairs"].discard(target)

    return bool(rows_to_delete)
