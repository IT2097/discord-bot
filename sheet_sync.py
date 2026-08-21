"""
Discordサーバーのメンバー一覧と、スプレッドシートの「名前（本名）」列を
自動で突き合わせてDiscordIDを埋めるためのモジュール。

【前提】
- サービスアカウントに、このスプレッドシートを「編集者」権限で共有していること
  （読み取り専用の閲覧権限では書き込みができません）

【安全設計】
- すでにDiscordIDが入力済みのセルは絶対に上書きしません（空のセルにしか書き込みません）
- 同じ名前のDiscordメンバーが複数いる場合は、誤爆を避けるため自動入力せずスキップします
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


def _get_write_client():
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

    info = json.loads(raw_json)
    creds = Credentials.from_service_account_info(info, scopes=_WRITE_SCOPES)
    return gspread.authorize(creds)


def sync_discord_ids(guild_members: list[tuple[str, str]]) -> dict:
    """
    guild_members: [(discord_id, display_name), ...] Bot以外のサーバーメンバー一覧

    戻り値:
      matched               : 自動入力した件数
      ambiguous_names       : 同名が複数いたためスキップした名前
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

    # 正規化した表示名 -> discord_id のマップを作成（同名が複数いたらambiguousとして除外）
    name_to_id: dict[str, str] = {}
    ambiguous_keys: set[str] = set()

    for discord_id, display_name in guild_members:
        key = _normalize(display_name)
        if not key:
            continue
        if key in name_to_id and name_to_id[key] != discord_id:
            ambiguous_keys.add(key)
        else:
            name_to_id[key] = discord_id

    updates = []
    used_discord_ids: set[str] = set()
    matched_name_keys: set[str] = set()

    for row_idx, row in enumerate(all_values[1:], start=2):  # 1行目はヘッダー
        existing_id = row[id_col - 1].strip() if len(row) >= id_col else ""
        sheet_name_value = row[name_col - 1].strip() if len(row) >= name_col else ""

        if existing_id or not sheet_name_value:
            continue  # 入力済み、または名前が空の行はスキップ

        key = _normalize(sheet_name_value)
        if key in ambiguous_keys:
            continue

        discord_id = name_to_id.get(key)
        if discord_id:
            updates.append(
                {"range": gspread.utils.rowcol_to_a1(row_idx, id_col), "values": [[discord_id]]}
            )
            used_discord_ids.add(discord_id)
            matched_name_keys.add(key)

    if updates:
        sheet.batch_update(updates)

    sheet_name_values = {
        row[name_col - 1].strip()
        for row in all_values[1:]
        if len(row) >= name_col and row[name_col - 1].strip()
    }

    unmatched_sheet_names = sorted(
        n for n in sheet_name_values if _normalize(n) not in matched_name_keys
    )

    unmatched_discord_names = sorted(
        display_name
        for discord_id, display_name in guild_members
        if discord_id not in used_discord_ids
    )

    return {
        "matched": len(updates),
        "ambiguous_names": sorted(ambiguous_keys),
        "unmatched_sheet_names": unmatched_sheet_names,
        "unmatched_discord_names": unmatched_discord_names,
    }
