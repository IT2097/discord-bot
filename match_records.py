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
# D列の「ルームID」は、マッチングルーム（チャンネルまたはスレッド）のID。
# 以前の記録は空欄のこともある（その場合はチャンネルの権限から2人を判断する）
_HEADERS = ["申請者DiscordID", "相手DiscordID", "マッチ日時", "ルームID"]

CACHE_SECONDS = 30  # 読み取りは短時間だけキャッシュし、Sheets APIへの呼び出しを抑える

# 429（回数制限）エラー時の再試行設定
_RETRY_COUNT = 4
_RETRY_WAIT_SECONDS = 20

_lock = threading.Lock()
_cache = {"rows": None, "fetched_at": 0.0}
_worksheet_lock = threading.Lock()
_worksheet_cache: dict = {"sheet": None, "header_checked": False}


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
        sheet = _worksheet_cache["sheet"]
        if not _worksheet_cache["header_checked"]:
            # 以前からあるシートには「ルームID」の見出しが無いので、D1に足しておく
            header = _with_retry(sheet.row_values, 1)
            if len(header) < 4 or not str(header[3]).strip():
                if sheet.col_count < 4:
                    _with_retry(sheet.add_cols, 4 - sheet.col_count)
                _with_retry(sheet.update, range_name="D1", values=[[_HEADERS[3]]])
            _worksheet_cache["header_checked"] = True
        return sheet


def _reset_worksheet_cache():
    with _worksheet_lock:
        _worksheet_cache["sheet"] = None
        _worksheet_cache["header_checked"] = False


def _clean(value) -> str:
    return str(value or "").strip().lstrip("'")


def _fetch_rows_from_sheet() -> list[dict]:
    """履歴シートの全行を [{"row", "a", "b", "room_id"}] で返す（row はシート上の行番号）。"""
    sheet = _get_worksheet()
    try:
        values = _with_retry(sheet.get_all_values)
    except gspread.exceptions.APIError as error:
        if _is_rate_limited(error):
            raise
        # シートが削除・作り直しされた等で古いオブジェクトが使えない場合は開き直す
        _reset_worksheet_cache()
        sheet = _get_worksheet()
        values = _with_retry(sheet.get_all_values)

    rows = []
    for row_number, row in enumerate(values[1:], start=2):
        row = list(row) + [""] * (4 - len(row))
        a, b = _clean(row[0]), _clean(row[1])
        if a and b:
            rows.append({"row": row_number, "a": a, "b": b, "room_id": _clean(row[3])})
    return rows


def _get_rows(force_refresh: bool = False) -> list[dict]:
    with _lock:
        now = time.time()
        if force_refresh or _cache["rows"] is None or (now - _cache["fetched_at"]) > CACHE_SECONDS:
            _cache["rows"] = _fetch_rows_from_sheet()
            _cache["fetched_at"] = now
        return [dict(r) for r in _cache["rows"]]


def _invalidate():
    with _lock:
        _cache["rows"] = None


def get_all_matched_pairs(force_refresh: bool = False) -> set[frozenset]:
    """マッチング成立済みの全ペアを返す（短時間キャッシュ付き）。"""
    return {frozenset({r["a"], r["b"]}) for r in _get_rows(force_refresh)}


def get_all_records(force_refresh: bool = False) -> list[dict]:
    """履歴の全行（a, b, room_id, row）。"""
    return _get_rows(force_refresh)


def append_match(user_a: str, user_b: str, room_id: str | int = "") -> None:
    """
    マッチング成立を履歴シートに追記する。
    すでに記録済みのペアなら、ルームIDだけ書き込む（空欄・別のルームのときは新しいルームIDにする）。
    """
    append_matches([(user_a, user_b, room_id)])


def append_matches(pairs: list[tuple]) -> int:
    """
    複数のマッチング成立ペアを、1回の書き込みでまとめて履歴シートに追記する。
    各要素は (a, b) または (a, b, ルームID)。すでに記録済みのペアは、ルームIDが空欄なら埋める。
    追記した件数を返す。
    """
    rows = _get_rows(force_refresh=True)
    by_pair = {frozenset({r["a"], r["b"]}): r for r in rows}

    new_rows = []
    room_updates = []
    added: set[frozenset] = set()
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in pairs:
        user_a, user_b = str(item[0]), str(item[1])
        room_id = str(item[2]) if len(item) > 2 and item[2] else ""
        pair = frozenset({user_a, user_b})
        if len(pair) != 2 or pair in added:
            continue
        existing = by_pair.get(pair)
        if existing is not None:
            if room_id and existing["room_id"] != room_id:
                room_updates.append({"range": f"D{existing['row']}", "values": [[room_id]]})
            continue
        new_rows.append([user_a, user_b, now_text, room_id])
        added.add(pair)

    sheet = _get_worksheet()
    if room_updates:
        _with_retry(sheet.batch_update, room_updates, value_input_option="RAW")
    if new_rows:
        # RAW：DiscordIDのような長い数字が指数表記・桁落ちしないよう、そのまま文字として書き込む
        _with_retry(sheet.append_rows, new_rows, value_input_option="RAW", table_range="A1")
    if new_rows or room_updates:
        _invalidate()
    return len(new_rows)


def set_room(user_a: str, user_b: str, room_id: str | int) -> None:
    """2人の記録にルームIDを書き込む（記録が無ければ追加する）。ルームの移行用。"""
    append_matches([(user_a, user_b, room_id)])


def pair_for_room(room_id: str | int) -> list[str] | None:
    """ルームIDから2人のDiscordIDを返す（見つからなければ None）。"""
    room_id = str(room_id)
    for r in _get_rows():
        if r["room_id"] == room_id:
            return [r["a"], r["b"]]
    return None


def rooms_for_member(member_id: str | int) -> list[dict]:
    """その人が入っている記録（ルームIDがあるものだけ）。"""
    member_id = str(member_id)
    return [r for r in _get_rows() if r["room_id"] and member_id in (r["a"], r["b"])]


def _delete_sheet_rows(row_numbers: list[int]):
    sheet = _get_worksheet()
    # 下の行から消していく（上から消すと行番号がずれるため）
    for row_number in sorted(set(row_numbers), reverse=True):
        _with_retry(sheet.delete_rows, row_number)
    _invalidate()


def remove_match(user_a: str, user_b: str) -> bool:
    """
    指定した2人のマッチング成立記録を履歴シートから削除する（!マッチング解除 用）。
    削除した場合は True、記録が見つからなかった場合は False を返す。
    """
    target = frozenset({str(user_a), str(user_b)})
    rows = [r["row"] for r in _get_rows(force_refresh=True) if frozenset({r["a"], r["b"]}) == target]
    if rows:
        _delete_sheet_rows(rows)
    return bool(rows)


def remove_for_deleted_room(room_id: str | int, pair: list[str] | None = None) -> list[list[str]]:
    """
    マッチングルームが削除されたときに、その記録を消す。
    - ルームIDが一致する記録
    - pair が分かっていて、ルームIDが空欄の記録（以前の形式）
    別のルーム（移行先のスレッドなど）のIDが入っている記録は消さない。
    消した記録の2人のリストを返す。
    """
    room_id = str(room_id)
    target = frozenset(pair) if pair and len(pair) == 2 else None
    matched = [
        r
        for r in _get_rows(force_refresh=True)
        if r["room_id"] == room_id
        or (target is not None and not r["room_id"] and frozenset({r["a"], r["b"]}) == target)
    ]
    if matched:
        _delete_sheet_rows([r["row"] for r in matched])
    return [[r["a"], r["b"]] for r in matched]


def remove_rows(row_numbers: list[int]) -> None:
    """指定した行番号の記録を消す（!マッチング履歴整理 用）。"""
    if row_numbers:
        _delete_sheet_rows(row_numbers)
