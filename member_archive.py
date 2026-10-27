"""
サーバーを抜けた（キック・自主退会どちらも）会員の行を、
「入力シート」から「退会者」シートへ移し替えるモジュール。

【流れ】
1. 入力シートで、DiscordID列（見出し「DiscordID」）が一致する行を探す
2. その行のA列の番号と同じ番号の行を、すべて対象にする
   （1人が複数行に分かれていて、DiscordIDが1行にしか入っていない場合も全部移すため）
3. 退会者シートの末尾に追記する（C列にDiscordIDを入れる）
4. 追記が成功してから、入力シートの対象行を削除する
   （削除の直前に入力シートを読み直し、行番号のずれで別の人の行を消さないようにする）

【列の対応】
  入力シート                         → 退会者シート
  A 番号                             → A
  B 名前（本名）                     → B
  L DiscordID                        → C（オプチャ/DiscordID）
  年齢・性別・都道府県①・市町村①   → D・E・F・G
  業種・事業 / 活動内容              → H・I
  URL①・URL②・URL③                  → J・K・L（入力シートに列が無ければ空欄）
  都道府県②・市町村② 〜 ⑤          → M〜T
入力シートの列は見出し名で探すので、入力シートの列の順番が変わっても動く。
URL・都道府県②〜⑤の列は、入力シートに無ければ空欄のまま移す。
"""

from __future__ import annotations

import json
import os
import time
import threading

import gspread
from google.oauth2.service_account import Credentials

import sheets_client

_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

INPUT_SHEET_NAME = os.getenv("SHEET_NAME", "入力シート")
WITHDRAWN_SHEET_NAME = os.getenv("WITHDRAWN_SHEET_NAME", "退会者")

# 退会者シートに書き込む順番（入力シートの見出し名）
_COPY_HEADERS_AFTER_ID = [
    sheets_client.HEADER_AGE,
    sheets_client.HEADER_GENDER,
    sheets_client.HEADER_PREFECTURE,
    sheets_client.HEADER_CITY,
    sheets_client.HEADER_BUSINESS_TYPE,
    sheets_client.HEADER_BUSINESS_CONTENT,
    sheets_client.HEADER_URL_1,
    sheets_client.HEADER_URL_2,
    sheets_client.HEADER_URL_3,
    *[header for pair in sheets_client.LOCATION_HEADERS[1:] for header in pair],
]
# 入力シートに無くてもよい列（無ければ空欄で移す）
_OPTIONAL_HEADERS = {
    sheets_client.HEADER_URL_1,
    sheets_client.HEADER_URL_2,
    sheets_client.HEADER_URL_3,
    *[header for pair in sheets_client.LOCATION_HEADERS[1:] for header in pair],
}

_RETRY_COUNT = 4
_RETRY_WAIT_SECONDS = 20

# 同時に2人が抜けた時などに、行の削除が重ならないようにする
_archive_lock = threading.Lock()


def _get_spreadsheet():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_WRITE_SCOPES)
    return gspread.authorize(creds).open_by_key(spreadsheet_id)


def _with_retry(func, *args, **kwargs):
    """Sheets APIの回数制限（429）のときだけ、少し待って再試行する。"""
    for attempt in range(_RETRY_COUNT):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as error:
            if "429" not in str(error) or attempt == _RETRY_COUNT - 1:
                raise
            time.sleep(_RETRY_WAIT_SECONDS)


def _col_index(header_row: list[str], header: str) -> int:
    normalized = [str(h).strip() for h in header_row]
    if header not in normalized:
        raise RuntimeError(
            f"「{INPUT_SHEET_NAME}」シートの1行目に「{header}」という見出しが見つかりませんでした。"
        )
    return normalized.index(header)


def _cell(row: list[str], index: int) -> str:
    return str(row[index]).strip() if index < len(row) else ""


def _find_target_rows(values: list[list[str]], discord_id: str) -> list[int]:
    """
    退会者の行のインデックス（values内の0始まり。0は見出し行なので1以降）を返す。
    DiscordIDが一致した行のA列の番号と同じ番号の行をすべて対象にする。
    A列の番号が空の場合は、名前（本名）が同じ行を対象にする。
    """
    header = values[0]
    id_col = _col_index(header, sheets_client.HEADER_DISCORD_ID)
    name_col = _col_index(header, sheets_client.HEADER_NAME)

    matched = [i for i in range(1, len(values)) if _cell(values[i], id_col) == discord_id]
    if not matched:
        return []

    numbers = {_cell(values[i], 0) for i in matched if _cell(values[i], 0)}
    names = {_cell(values[i], name_col) for i in matched if _cell(values[i], name_col)}

    targets = set(matched)
    for i in range(1, len(values)):
        if numbers and _cell(values[i], 0) in numbers:
            targets.add(i)
        elif not numbers and names and _cell(values[i], name_col) in names:
            targets.add(i)
    return sorted(targets)


def archive_member(discord_id: str) -> dict:
    """
    指定したDiscordIDの会員を、入力シートから退会者シートへ移す。
    戻り値: {"found": bool, "name": str, "moved": 移した行数}
    """
    discord_id = str(discord_id).strip()

    with _archive_lock:
        spreadsheet = _with_retry(_get_spreadsheet)
        input_sheet = _with_retry(spreadsheet.worksheet, INPUT_SHEET_NAME)
        withdrawn_sheet = _with_retry(spreadsheet.worksheet, WITHDRAWN_SHEET_NAME)

        values = _with_retry(input_sheet.get_all_values)
        target_indices = _find_target_rows(values, discord_id)
        if not target_indices:
            return {"found": False, "name": "", "moved": 0}

        header = values[0]
        name_col = _col_index(header, sheets_client.HEADER_NAME)
        normalized_header = [str(h).strip() for h in header]
        other_cols = [
            None if (h in _OPTIONAL_HEADERS and h not in normalized_header) else _col_index(header, h)
            for h in _COPY_HEADERS_AFTER_ID
        ]
        name = _cell(values[target_indices[0]], name_col)

        new_rows = []
        for i in target_indices:
            row = values[i]
            new_rows.append(
                [
                    _cell(row, 0),
                    _cell(row, name_col),
                    # 先頭の ' で文字として扱わせる（長い数字が 9.7E+17 のように桁落ちするのを防ぐ）
                    f"'{discord_id}",
                    *[_cell(row, c) if c is not None else "" for c in other_cols],
                ]
            )

        # ① 先に退会者シートへ追記する（ここで失敗したら入力シートは消さない）
        _with_retry(
            withdrawn_sheet.append_rows,
            new_rows,
            value_input_option="USER_ENTERED",
            table_range="A1",
        )

        # ② 削除の直前に読み直して、行番号を確定させる
        #    （追記中に誰かが入力シートで行を挿入・削除していても、別の人の行を消さないため）
        values = _with_retry(input_sheet.get_all_values)
        target_indices = _find_target_rows(values, discord_id)

        if target_indices:
            # 下の行から消す（上から消すと行番号がずれるため）。1回のリクエストでまとめて削除する
            requests = [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": input_sheet.id,
                            "dimension": "ROWS",
                            "startIndex": i,
                            "endIndex": i + 1,
                        }
                    }
                }
                for i in sorted(target_indices, reverse=True)
            ]
            _with_retry(spreadsheet.batch_update, {"requests": requests})

    # 会員一覧ページにすぐ反映させる
    try:
        sheets_client.get_members(force_refresh=True)
    except Exception:  # noqa: BLE001 - 反映が5分遅れるだけなので無視する
        pass

    return {"found": True, "name": name, "moved": len(new_rows)}
