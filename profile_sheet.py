"""
Discordの「プロフィール編集」から、スプレッドシート（入力シート）の会員情報を書き換えるモジュール。

【入力シートの形式（sheets_client.py と同じ考え方）】
- 1人が「業種・事業内容」の数だけ複数行に分かれている
- 同じ人の行は「名前（本名）」が同じ。DiscordIDは最初の1行にだけ入っている
- A列の番号などは数式で自動計算しているので、botは値を書き込まない

【このモジュールでできること】
- 名前・年齢・性別・都道府県・市町村の変更（その人の全部の行をまとめて書き換える）
- 業種と事業内容の追加（その人の最後の行のすぐ下に1行入れる）
- 業種と事業内容の修正・削除（削除で最後の1件にはできない。DiscordIDの入った行を消すときは、IDを残る行へ移す）
- 都道府県の表記ゆれの一括統一（管理者コマンド用）

書き込みの直前に必ずシートを読み直し、行がずれていないかを確かめてから書き込む。
"""

from __future__ import annotations

import json
import os
import threading
import time

import gspread
from google.oauth2.service_account import Credentials

import prefectures
import sheets_client

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 項目名（botの中での呼び方）→ 入力シートの見出し
FIELD_HEADERS = {
    "name": sheets_client.HEADER_NAME,
    "age": sheets_client.HEADER_AGE,
    "gender": sheets_client.HEADER_GENDER,
    "prefecture": sheets_client.HEADER_PREFECTURE,
    "city": sheets_client.HEADER_CITY,
}
FIELD_LABELS = {"name": "名前", "age": "年齢", "gender": "性別", "prefecture": "都道府県", "city": "市町村"}

_lock = threading.Lock()
_sheet_cache: dict = {"sheet": None}


class ProfileError(Exception):
    """本人にそのまま見せてよいエラー（見つからない・行がずれた・重複など）。"""


# ---------------------------------------------------------------------------
# シートの読み書きの下準備
# ---------------------------------------------------------------------------

def _with_retry(func, *args, **kwargs):
    """Sheets APIの回数制限（429）のときだけ、少し待って再試行する。"""
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as error:
            if "429" not in str(error) or attempt == 3:
                raise
            time.sleep(15)


def _open_sheet():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not raw_json or not spreadsheet_id:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON または SPREADSHEET_ID が設定されていません。")
    creds = Credentials.from_service_account_info(json.loads(raw_json, strict=False), scopes=_SCOPES)
    spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(os.getenv("SHEET_NAME", "会員"))


def _get_sheet():
    if _sheet_cache["sheet"] is None:
        _sheet_cache["sheet"] = _with_retry(_open_sheet)
    return _sheet_cache["sheet"]


def _clean(value) -> str:
    return str(value or "").strip()


def _clean_id(value) -> str:
    return _clean(value).lstrip("'")


class _Table:
    """シート全体を読み込んだもの。列番号は1始まり、行番号はシート上の行番号。"""

    def __init__(self, values: list[list[str]]):
        self.values = values
        headers = [_clean(h) for h in (values[0] if values else [])]
        self.cols: dict[str, int] = {}
        for header in (
            sheets_client.HEADER_DISCORD_ID,
            sheets_client.HEADER_NAME,
            sheets_client.HEADER_AGE,
            sheets_client.HEADER_GENDER,
            sheets_client.HEADER_PREFECTURE,
            sheets_client.HEADER_CITY,
            sheets_client.HEADER_BUSINESS_TYPE,
            sheets_client.HEADER_BUSINESS_CONTENT,
        ):
            if header not in headers:
                raise RuntimeError(f"入力シートに「{header}」という見出しの列が見つかりません。")
            self.cols[header] = headers.index(header) + 1

    def cell(self, row: int, header: str) -> str:
        line = self.values[row - 1] if 0 < row <= len(self.values) else []
        col = self.cols[header]
        return _clean(line[col - 1]) if len(line) >= col else ""

    def rows(self):
        return range(2, len(self.values) + 1)

    def member_rows(self, discord_id: str) -> tuple[str, list[int]]:
        """DiscordIDの入った行から名前を調べ、同じ名前の行をすべて返す。"""
        discord_id = str(discord_id)
        name = None
        for row in self.rows():
            if _clean_id(self.cell(row, sheets_client.HEADER_DISCORD_ID)) == discord_id:
                name = self.cell(row, sheets_client.HEADER_NAME)
                break
        if not name:
            raise ProfileError("会員情報が見つかりませんでした。先に「名前登録」チャンネルで登録してください。")
        rows = [r for r in self.rows() if self.cell(r, sheets_client.HEADER_NAME) == name]
        return name, rows

    def business(self, row: int) -> tuple[str, str]:
        return (
            self.cell(row, sheets_client.HEADER_BUSINESS_TYPE),
            self.cell(row, sheets_client.HEADER_BUSINESS_CONTENT),
        )


def _read_table() -> _Table:
    return _Table(_with_retry(_get_sheet().get_all_values))


def _a1(row: int, col: int) -> str:
    return gspread.utils.rowcol_to_a1(row, col)


def _write_cells(updates: list[dict]):
    if updates:
        # RAW：入力した文字をそのまま書き込む（数字が勝手に変換されないように）
        _with_retry(_get_sheet().batch_update, updates, value_input_option="RAW")


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------

def get_profile(discord_id: str) -> dict:
    """
    編集画面に出すための、本人のプロフィールを返す。
    businesses の各要素には、シート上の行番号（row）と、書き込み前の確認に使う元の値が入る。
    """
    table = _read_table()
    name, rows = table.member_rows(discord_id)
    first = rows[0]
    businesses = []
    for row in rows:
        biz_type, content = table.business(row)
        if biz_type or content:
            businesses.append({"row": row, "type": biz_type, "content": content})
    return {
        "name": name,
        "age": table.cell(first, sheets_client.HEADER_AGE),
        "gender": table.cell(first, sheets_client.HEADER_GENDER),
        "prefecture": table.cell(first, sheets_client.HEADER_PREFECTURE),
        "city": table.cell(first, sheets_client.HEADER_CITY),
        "businesses": businesses,
    }


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------

def update_fields(discord_id: str, changes: dict) -> dict:
    """
    名前・年齢・性別・都道府県・市町村を、その人の全部の行でまとめて書き換える。
    戻り値は {項目: (変更前, 変更後)}（実際に変わったものだけ）。
    """
    with _lock:
        table = _read_table()
        name, rows = table.member_rows(discord_id)
        first = rows[0]

        diffs = {}
        for key, new_value in changes.items():
            header = FIELD_HEADERS[key]
            new_value = _clean(new_value)
            old_value = table.cell(first, header)
            if new_value != old_value:
                diffs[key] = (old_value, new_value)

        if not diffs:
            return {}

        if "name" in diffs:
            new_name = diffs["name"][1]
            if not new_name:
                raise ProfileError("名前を空にすることはできません。")
            # 同じ名前の別の人がいると、一覧で2人が1人分にまとまってしまうので止める
            others = {
                table.cell(r, sheets_client.HEADER_NAME) for r in table.rows() if r not in rows
            }
            if new_name in others:
                raise ProfileError(
                    f"「{new_name}」という名前の会員がすでにいるため、変更できませんでした。"
                    "名字と名前の間にスペースを入れるなど、少し変えてお試しください。"
                )

        updates = []
        for key, (_old, new_value) in diffs.items():
            col = table.cols[FIELD_HEADERS[key]]
            for row in rows:
                updates.append({"range": _a1(row, col), "values": [[new_value]]})
        _write_cells(updates)
        return diffs


def add_business(discord_id: str, business_type: str, content: str) -> int:
    """
    その人の最後の行のすぐ下に1行入れ、業種と事業内容を書き込む。
    名前・年齢・性別・都道府県・市町村は同じ人の他の行からコピーする（DiscordIDは入れない）。
    A列などの数式の列には書き込まない。戻り値は追加した行番号。
    """
    business_type, content = _clean(business_type), _clean(content)
    if not business_type and not content:
        raise ProfileError("業種と事業内容が空です。")

    with _lock:
        table = _read_table()
        _name, rows = table.member_rows(discord_id)
        first, last = rows[0], max(rows)

        # 最初の行が業種・事業内容とも空なら、新しい行は作らずそこに書き込む
        if len(rows) == 1 and table.business(first) == ("", ""):
            target = first
        else:
            target = last + 1
            sheet = _get_sheet()
            _with_retry(
                sheet.spreadsheet.batch_update,
                {
                    "requests": [
                        {
                            "insertDimension": {
                                "range": {
                                    "sheetId": sheet.id,
                                    "dimension": "ROWS",
                                    "startIndex": last,  # 0始まり：last行目の直後
                                    "endIndex": last + 1,
                                },
                                # 上の行（同じ人の行）の書式・入力規則を引き継ぐ
                                "inheritFromBefore": True,
                            }
                        }
                    ]
                },
            )

        values = {
            sheets_client.HEADER_BUSINESS_TYPE: business_type,
            sheets_client.HEADER_BUSINESS_CONTENT: content,
        }
        if target != first:
            for header in FIELD_HEADERS.values():
                values[header] = table.cell(first, header)

        _write_cells(
            [
                {"range": _a1(target, table.cols[header]), "values": [[value]]}
                for header, value in values.items()
                if value
            ]
        )
        return target


def _check_business_row(table: _Table, discord_id: str, row: int, expected: tuple[str, str]) -> list[int]:
    """行がまだ本人のもので、内容も開いたときのままかを確かめる（ずれていたら止める）。"""
    _name, rows = table.member_rows(discord_id)
    if row not in rows or table.business(row) != expected:
        raise ProfileError(
            "スプレッドシートの内容が変わっていたため、保存しませんでした。"
            "お手数ですが、もう一度「プロフィールを編集する」から開き直してください。"
        )
    return rows


def update_business(discord_id: str, row: int, expected: tuple[str, str], business_type: str, content: str):
    """業種と事業内容を1件書き換える。expected は編集画面を開いたときの (業種, 事業内容)。"""
    business_type, content = _clean(business_type), _clean(content)
    if not business_type and not content:
        raise ProfileError("業種と事業内容が空です。消したい場合は「業種を削除」を使ってください。")
    with _lock:
        table = _read_table()
        _check_business_row(table, discord_id, row, expected)
        _write_cells(
            [
                {"range": _a1(row, table.cols[sheets_client.HEADER_BUSINESS_TYPE]), "values": [[business_type]]},
                {"range": _a1(row, table.cols[sheets_client.HEADER_BUSINESS_CONTENT]), "values": [[content]]},
            ]
        )


def delete_business(discord_id: str, row: int, expected: tuple[str, str]):
    """
    業種と事業内容を1件削除する（その行ごと消す）。最後の1件は消せない。
    DiscordIDが入っている行を消すときは、先にIDを残る行へ移す。
    """
    with _lock:
        table = _read_table()
        rows = _check_business_row(table, discord_id, row, expected)
        business_rows = [r for r in rows if table.business(r) != ("", "")]
        if len(business_rows) <= 1:
            raise ProfileError("業種と事業内容は最低1件必要なため、最後の1件は削除できません。修正を使ってください。")

        id_col = table.cols[sheets_client.HEADER_DISCORD_ID]
        if _clean_id(table.cell(row, sheets_client.HEADER_DISCORD_ID)):
            heir = next(r for r in rows if r != row)
            _write_cells([{"range": _a1(heir, id_col), "values": [[str(discord_id)]]}])

        sheet = _get_sheet()
        _with_retry(sheet.delete_rows, row)


# ---------------------------------------------------------------------------
# 都道府県の表記ゆれの一括統一（管理者コマンド「!都道府県統一」用）
# ---------------------------------------------------------------------------

def plan_prefecture_fix() -> dict:
    """
    入力シートの都道府県を調べ、そろえる予定の一覧を返す（まだ書き込まない）。
      changes    : [(行番号, 名前, 今の値, 直した値)]
      unresolved : [(行番号, 名前, 今の値)]  都道府県と判断できなかったもの（手で直してもらう）
    """
    table = _read_table()
    changes, unresolved = [], []
    for row in table.rows():
        current = table.cell(row, sheets_client.HEADER_PREFECTURE)
        if not current:
            continue
        name = table.cell(row, sheets_client.HEADER_NAME)
        fixed = prefectures.normalize_prefecture(current)
        if fixed is None:
            unresolved.append((row, name, current))
        elif fixed != current:
            changes.append((row, name, current, fixed))
    return {"changes": changes, "unresolved": unresolved}


def apply_prefecture_fix(changes: list[tuple]) -> int:
    """plan_prefecture_fix の changes を書き込む。行の内容が変わっていたものは飛ばす。戻り値は書き換えた件数。"""
    with _lock:
        table = _read_table()
        col = table.cols[sheets_client.HEADER_PREFECTURE]
        updates = []
        for row, name, current, fixed in changes:
            if (
                table.cell(row, sheets_client.HEADER_NAME) == name
                and table.cell(row, sheets_client.HEADER_PREFECTURE) == current
            ):
                updates.append({"range": _a1(row, col), "values": [[fixed]]})
        _write_cells(updates)
        return len(updates)
