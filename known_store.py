"""
会員一覧ページの「知り合いチェック」を保存するモジュール。

すでに知り合いの相手にチェックを付けておくと、その相手への「マッチング希望を送る」ボタンが
押せなくなる（誤送信の防止）。チェックはスプレッドシートの「知り合いチェック」シート
（無ければ自動作成）に保存するので、スマホ・パソコンなど、どの端末で開いても同じ状態になる。

【シートの列】
自分DiscordID / 相手DiscordID / 登録日時
1行＝「自分」が「相手」にチェックを付けている、という意味。チェックを外すと行を削除する。
"""

from __future__ import annotations

import json
import os
import threading
import time

import gspread
from google.oauth2.service_account import Credentials

from match_store import _format_time, _with_retry

KNOWN_SHEET_NAME = os.getenv("KNOWN_SHEET_NAME", "知り合いチェック")
_HEADERS = ["自分DiscordID", "相手DiscordID", "登録日時"]

_lock = threading.RLock()
_pairs: dict[tuple[str, str], int] | None = None  # (自分, 相手) -> シートの行番号
_sheet_cache: dict = {"sheet": None}


def _open_sheet():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not raw_json or not spreadsheet_id:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON または SPREADSHEET_ID が設定されていません。")
    creds = Credentials.from_service_account_info(
        json.loads(raw_json, strict=False),
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet(KNOWN_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        # シートが無ければ自動で作成し、見出し行を入れる
        sheet = spreadsheet.add_worksheet(title=KNOWN_SHEET_NAME, rows=1000, cols=len(_HEADERS))
        sheet.append_row(_HEADERS)
        return sheet


def _get_sheet():
    if _sheet_cache["sheet"] is None:
        _sheet_cache["sheet"] = _with_retry(_open_sheet)
    return _sheet_cache["sheet"]


def _load_locked():
    """シートから全チェックを読み込み直す（起動後の最初の利用時と、行を削除したあと）。"""
    global _pairs
    values = _with_retry(_get_sheet().get_all_values)
    loaded: dict[tuple[str, str], int] = {}
    for row_number, row in enumerate(values[1:], start=2):
        row = list(row) + [""] * (len(_HEADERS) - len(row))
        me, other = row[0].strip().lstrip("'"), row[1].strip().lstrip("'")
        if me and other:
            loaded.setdefault((me, other), row_number)
    _pairs = loaded


def _ensure_loaded_locked():
    if _pairs is None:
        _load_locked()


def known_targets_for(user_id: str) -> set[str]:
    """指定した会員がチェックを付けている相手IDの集合。"""
    user_id = str(user_id)
    with _lock:
        _ensure_loaded_locked()
        return {other for (me, other) in _pairs if me == user_id}


def set_known(user_id: str, target_id: str, known: bool) -> bool:
    """チェックを付ける（known=True）／外す（known=False）。変更後の状態を返す。"""
    key = (str(user_id), str(target_id))
    with _lock:
        _ensure_loaded_locked()
        if known:
            if key in _pairs:
                return True
            # 長いIDが数値に変換されて桁が崩れないよう、先頭に ' を付けて文字列として書き込む
            row_values = [f"'{key[0]}", f"'{key[1]}", _format_time(time.time())]
            _with_retry(
                _get_sheet().append_rows, [row_values], value_input_option="USER_ENTERED", table_range="A1"
            )
            _load_locked()  # 追記された行番号を正しく持つため読み込み直す
            return True

        if key not in _pairs:
            return False
        sheet = _get_sheet()
        # 同じ組み合わせが複数行あっても全部消す（下の行から消すと行番号がずれない）
        values = _with_retry(sheet.get_all_values)
        rows = [
            i
            for i, row in enumerate(values[1:], start=2)
            if len(row) >= 2 and row[0].strip().lstrip("'") == key[0] and row[1].strip().lstrip("'") == key[1]
        ]
        for row_number in sorted(rows, reverse=True):
            _with_retry(sheet.delete_rows, row_number)
        _load_locked()
        return False
