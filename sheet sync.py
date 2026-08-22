"""
Discordサーバーのメンバー一覧と、スプレッドシートの「名前（本名）」列を
自動で突き合わせてDiscordIDを埋めるためのモジュール。

【前提】
- サービスアカウントに、このスプレッドシートを「編集者」権限で共有していること
  （読み取り専用の閲覧権限では書き込みができません）

【マッチングの考え方】
シートの「名前（本名）」はスペース区切りで「姓　名」のように入っている想定です。
Discordの表示名（ニックネーム）が完全に一致していなくても、姓・名の両方が
表示名の中に含まれていれば同一人物とみなしてマッチングします
（例：シート「山田　太郎」、Discord表示名「山田 太郎（東京）」でもマッチする）。
これにより、絵文字や肩書きなどの装飾がニックネームに付いていても拾えるように
しています。ただし該当する候補が複数人いる場合は、誤爆を避けるため
自動入力せずスキップします（手動確認が必要）。

【安全設計】
- すでにDiscordIDが入力済みのセルは絶対に上書きしません（空のセルにしか書き込みません）
- 該当候補が複数人いる場合は、誤爆を避けるため自動入力せずスキップします
- 一致しなかった分は「あとで確認してほしいリスト」として返します
"""

from __future__ import annotations

import json
import os

import gspread
from google.oauth2.service_account import Credentials

import sheets_client  # HEADER_DISCORD_ID / HEADER_NAME を共有

_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _normalize(name: str) -> str:
    """全角/半角スペースの違いなどを吸収するための正規化。"""
    return name.replace("\u3000", "").replace(" ", "").strip()


def _split_name_parts(name: str) -> list[str]:
    """
    「名前（本名）」を、スペース（全角/半角）区切りで姓・名などのパーツに分割する。
    区切りが無ければ、名前全体を1つのパーツとして扱う。
    """
    normalized = name.replace("\u3000", " ")
    parts = [_normalize(p) for p in normalized.split(" ") if p.strip()]
    return parts if parts else [_normalize(name)]


def _find_matching_discord_id(
    sheet_name: str, guild_members: list[tuple[str, str]]
) -> tuple[str | None, bool]:
    """
    sheet_name（姓・名などスペース区切り）の各パーツが、すべて含まれている
    Discordメンバーを探す。

    戻り値: (見つかったdiscord_id、または見つからなければNone, 複数人が該当したか)
    """
    parts = _split_name_parts(sheet_name)
    if not parts:
        return None, False

    matched_ids: set[str] = set()

    for discord_id, display_name in guild_members:
        normalized_display = _normalize(display_name)
        if not normalized_display:
            continue
        if all(part in normalized_display for part in parts):
            matched_ids.add(discord_id)

    if len(matched_ids) == 1:
        return next(iter(matched_ids)), False
    if len(matched_ids) > 1:
        return None, True
    return None, False


def _get_write_client():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    info = json.loads(raw_json, strict=False)
    creds = Credentials.from_service_account_info(info, scopes=_WRITE_SCOPES)
    return gspread.authorize(creds)


def sync_discord_ids(guild_members: list[tuple[str, str]]) -> dict:
    """
    guild_members: [(discord_id, display_name), ...] Bot以外のサーバーメンバー一覧

    戻り値:
      matched               : 自動入力した件数
      ambiguous_names       : 候補が複数人いたためスキップした名前
      unmatched_sheet_names : シートにあるがDiscordで一致しなかった名前
      unmatched_discord_names: Discordにいるがシートで一致しなかった表示名
    """
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    sheet_name = os.getenv("SHEET_NAME", "会員")

    if not spreadsheet_id:
        raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")

    client = _get_write_client()
    sheet = client.open_by_key(spreadsheet_id).worksheet(sheet_name)

    headers = sheet.row_values(1)
    try:
        name_col = headers.index(sheets_client.HEADER_NAME) + 1
        id_col = headers.index(sheets_client.HEADER_DISCORD_ID) + 1
    except ValueError as error:
        raise RuntimeError(
            f"シートに「{sheets_client.HEADER_NAME}」または「{sheets_client.HEADER_DISCORD_ID}」"
            "という見出しの列が見つかりませんでした。"
        ) from error

    all_values = sheet.get_all_values()

    updates = []
    used_discord_ids: set[str] = set()
    matched_sheet_names: set[str] = set()
    ambiguous_names: set[str] = set()

    for row_idx, row in enumerate(all_values[1:], start=2):  # 1行目はヘッダー
        existing_id = row[id_col - 1].strip() if len(row) >= id_col else ""
        sheet_name_value = row[name_col - 1].strip() if len(row) >= name_col else ""

        if existing_id or not sheet_name_value:
            continue  # 入力済み、または名前が空の行はスキップ

        discord_id, is_ambiguous = _find_matching_discord_id(sheet_name_value, guild_members)

        if is_ambiguous:
            ambiguous_names.add(sheet_name_value)
            continue

        if discord_id:
            updates.append(
                {"range": gspread.utils.rowcol_to_a1(row_idx, id_col), "values": [[discord_id]]}
            )
            used_discord_ids.add(discord_id)
            matched_sheet_names.add(sheet_name_value)

    if updates:
        sheet.batch_update(updates)

    sheet_name_values = {
        row[name_col - 1].strip()
        for row in all_values[1:]
        if len(row) >= name_col and row[name_col - 1].strip()
    }

    unmatched_sheet_names = sorted(
        n for n in sheet_name_values if n not in matched_sheet_names and n not in ambiguous_names
    )

    unmatched_discord_names = sorted(
        display_name
        for discord_id, display_name in guild_members
        if discord_id not in used_discord_ids
    )

    return {
        "matched": len(updates),
        "ambiguous_names": sorted(ambiguous_names),
        "unmatched_sheet_names": unmatched_sheet_names,
        "unmatched_discord_names": unmatched_discord_names,
    }
