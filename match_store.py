"""
マッチング申請の状態を管理するモジュール。

【以前との違い】
以前は申請をbotのメモリ上だけで管理していたため、Railwayの再起動・デプロイのたびに
承認待ちの申請がすべて消え、承認ボタンが「期限切れ」になっていた。
今はスプレッドシートの「マッチング申請」シート（無ければ自動作成）に記録するので、
再起動しても申請は消えない。

【申請のルール】
- 有効期限は申請から7日間（REQUEST_EXPIRY_SECONDS）
- 3日たっても未対応なら、相手に1回だけリマインドする（REMIND_AFTER_SECONDS）
- 承認待ちの間は同じ相手に申請し直せない（重複防止）
- 期限が切れたら、同じ相手にもう一度申請できる

【シートの列】
申請ID / 申請者DiscordID / 相手DiscordID / 申請日時 / 状態 / チャンネルID / メッセージID / リマインド済み
状態は「承認待ち」「承認」「拒否」「期限切れ」「取消」のいずれか。
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

REQUEST_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7日間
REMIND_AFTER_SECONDS = 3 * 24 * 60 * 60  # 3日たったらリマインド

STATUS_PENDING = "承認待ち"
STATUS_APPROVED = "承認"
STATUS_REJECTED = "拒否"
STATUS_EXPIRED = "期限切れ"
STATUS_CANCELLED = "取消"

REQUEST_SHEET_NAME = os.getenv("MATCH_REQUEST_SHEET_NAME", "マッチング申請")
_HEADERS = ["申請ID", "申請者DiscordID", "相手DiscordID", "申請日時", "状態", "チャンネルID", "メッセージID", "リマインド済み"]

# 日本時間（サーバーのタイムゾーン設定に左右されないよう固定で+9時間）
_JST = timezone(timedelta(hours=9))
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_lock = threading.RLock()
_requests: dict[str, dict] | None = None  # request_id -> 申請内容（シートの行番号つき）
_sheet_cache: dict = {"sheet": None}
_matched_pairs: set[frozenset] = set()  # このプロセスで成立したマッチング（シートの記録の補助）


# ---------------------------------------------------------------------------
# スプレッドシートとのやりとり
# ---------------------------------------------------------------------------

def _with_retry(func, *args, **kwargs):
    """Sheets APIの回数制限（429）のときだけ、少し待って再試行する。"""
    for attempt in range(4):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as error:
            if "429" not in str(error) or attempt == 3:
                raise
            time.sleep(20)


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
        return spreadsheet.worksheet(REQUEST_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        # シートが無ければ自動で作成し、見出し行を入れる
        sheet = spreadsheet.add_worksheet(title=REQUEST_SHEET_NAME, rows=1000, cols=len(_HEADERS))
        sheet.append_row(_HEADERS)
        return sheet


def _get_sheet():
    if _sheet_cache["sheet"] is None:
        _sheet_cache["sheet"] = _with_retry(_open_sheet)
    return _sheet_cache["sheet"]


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, _JST).strftime(_TIME_FORMAT)


def _parse_time(text: str) -> float:
    try:
        return datetime.strptime(text.strip(), _TIME_FORMAT).replace(tzinfo=_JST).timestamp()
    except ValueError:
        return 0.0  # 読めない日時は「とても古い申請」として扱う（＝期限切れ）


def _load_locked():
    """シートから全申請を読み込み直す（起動直後と、行のずれを見つけたとき）。"""
    global _requests
    values = _with_retry(_get_sheet().get_all_values)
    loaded: dict[str, dict] = {}
    for row_number, row in enumerate(values[1:], start=2):
        row = list(row) + [""] * (len(_HEADERS) - len(row))
        request_id = row[0].strip()
        if not request_id:
            continue
        loaded[request_id] = {
            "request_id": request_id,
            "row": row_number,
            "requester_id": row[1].strip(),
            "target_id": row[2].strip(),
            "created_at": _parse_time(row[3]),
            "status": row[4].strip() or STATUS_PENDING,
            "channel_id": row[5].strip(),
            "message_id": row[6].strip(),
            "reminded": row[7].strip().upper() == "TRUE",
        }
    _requests = loaded


def _ensure_loaded_locked():
    if _requests is None:
        _load_locked()


def _save_locked(request_id: str):
    """申請の状態（状態・チャンネルID・メッセージID・リマインド済み）をシートに書き戻す。"""
    req = _requests.get(request_id)
    if req is None:
        return
    sheet = _get_sheet()

    # 誰かがシートの行を消して行番号がずれていないか確認し、ずれていたら読み込み直す
    current = _with_retry(sheet.acell, f"A{req['row']}").value
    if (current or "").strip() != request_id:
        saved = dict(req)
        _load_locked()
        req = _requests.get(request_id)
        if req is None:
            return  # シートから行ごと消されていた
        for key in ("status", "channel_id", "message_id", "reminded"):
            req[key] = saved[key]

    _with_retry(
        sheet.update,
        range_name=f"E{req['row']}:H{req['row']}",
        values=[[req["status"], req["channel_id"], req["message_id"], "TRUE" if req["reminded"] else ""]],
        value_input_option="RAW",
    )


def _effective_status(req: dict, now: float | None = None) -> str:
    """7日を過ぎた「承認待ち」は、シートの更新を待たずに「期限切れ」として扱う。"""
    now = time.time() if now is None else now
    if req["status"] == STATUS_PENDING and now - req["created_at"] >= REQUEST_EXPIRY_SECONDS:
        return STATUS_EXPIRED
    return req["status"]


def _public(req: dict) -> dict:
    data = dict(req)
    data["status"] = _effective_status(req)
    data["expires_at"] = req["created_at"] + REQUEST_EXPIRY_SECONDS
    return data


# ---------------------------------------------------------------------------
# 申請の作成・参照・確定
# ---------------------------------------------------------------------------

def preload():
    """起動時にシートを読み込んでおく（最初のボタン操作で待たせないため）。"""
    with _lock:
        _ensure_loaded_locked()


def has_pending_request(requester_id: str, target_id: str) -> bool:
    with _lock:
        _ensure_loaded_locked()
        return any(
            req["requester_id"] == str(requester_id)
            and req["target_id"] == str(target_id)
            and _effective_status(req) == STATUS_PENDING
            for req in _requests.values()
        )


def create_request(requester_id: str, target_id: str) -> str | None:
    """
    新しいマッチング申請を作成してシートに記録する。
    同じ相手への申請が承認待ち（7日以内）の場合は None を返す（重複防止）。
    期限切れ・拒否済みの相手には、もう一度申請できる。
    """
    with _lock:
        _ensure_loaded_locked()
        if has_pending_request(requester_id, target_id):
            return None

        request_id = uuid.uuid4().hex
        now = time.time()
        row_values = [request_id, str(requester_id), str(target_id), _format_time(now), STATUS_PENDING, "", "", ""]
        response = _with_retry(
            _get_sheet().append_rows, [row_values], value_input_option="RAW", table_range="A1"
        )

        # 追記された行番号を返り値から読み取る（読めなければ読み込み直す）
        updated_range = (response or {}).get("updates", {}).get("updatedRange", "")
        match = re.search(r"!A(\d+)", updated_range)
        if match:
            _requests[request_id] = {
                "request_id": request_id,
                "row": int(match.group(1)),
                "requester_id": str(requester_id),
                "target_id": str(target_id),
                "created_at": now,
                "status": STATUS_PENDING,
                "channel_id": "",
                "message_id": "",
                "reminded": False,
            }
        else:
            _load_locked()
        return request_id


def set_request_message(request_id: str, channel_id: int, message_id: int):
    """承認チャンネルに投稿したメッセージの場所を記録する（リマインドと期限切れの表示に使う）。"""
    with _lock:
        _ensure_loaded_locked()
        req = _requests.get(request_id)
        if req is None:
            return
        req["channel_id"] = str(channel_id)
        req["message_id"] = str(message_id)
        _save_locked(request_id)


def get_request(request_id: str) -> dict | None:
    """
    申請の内容を返す（見つからなければ None）。
    戻り値の "status" で、承認待ち・承認・拒否・期限切れ・取消のどれかが分かる。
    """
    with _lock:
        _ensure_loaded_locked()
        req = _requests.get(request_id)
        return _public(req) if req else None


def resolve_request(request_id: str, status: str = STATUS_CANCELLED) -> dict | None:
    """
    承認待ちの申請を確定させる（承認・拒否・取消）。
    すでに確定済み・期限切れの場合は何もせず None を返す（ボタンの二重押し対策）。
    """
    with _lock:
        _ensure_loaded_locked()
        req = _requests.get(request_id)
        if req is None or _effective_status(req) != STATUS_PENDING:
            return None
        req["status"] = status
        _save_locked(request_id)
        return _public(req)


def find_pending_request(requester_id: str, target_id: str) -> dict | None:
    """申請者→相手の、承認待ち中の申請を1件返す（なければ None）。取り消しボタン用。"""
    with _lock:
        _ensure_loaded_locked()
        for req in _requests.values():
            if (
                req["requester_id"] == str(requester_id)
                and req["target_id"] == str(target_id)
                and _effective_status(req) == STATUS_PENDING
            ):
                return _public(req)
        return None


def pending_targets_for(requester_id: str) -> set[str]:
    """指定した申請者が、現在承認待ち中の相手IDの集合を返す（会員一覧ページの「申請中」表示用）。"""
    with _lock:
        _ensure_loaded_locked()
        return {
            req["target_id"]
            for req in _requests.values()
            if req["requester_id"] == str(requester_id) and _effective_status(req) == STATUS_PENDING
        }


# ---------------------------------------------------------------------------
# リマインド・期限切れの処理（botが定期的に呼び出す）
# ---------------------------------------------------------------------------

def pending_requests() -> list[dict]:
    """承認待ち（期限内）の申請をすべて返す（承認チャンネルの移行処理用）。"""
    now = time.time()
    with _lock:
        _ensure_loaded_locked()
        return [_public(req) for req in _requests.values() if _effective_status(req, now) == STATUS_PENDING]


def requests_to_remind() -> list[dict]:
    """3日以上たっていて、まだリマインドしていない承認待ちの申請。"""
    now = time.time()
    with _lock:
        _ensure_loaded_locked()
        return [
            _public(req)
            for req in _requests.values()
            if _effective_status(req, now) == STATUS_PENDING
            and not req["reminded"]
            and now - req["created_at"] >= REMIND_AFTER_SECONDS
        ]


def mark_reminded(request_id: str):
    with _lock:
        _ensure_loaded_locked()
        req = _requests.get(request_id)
        if req is None:
            return
        req["reminded"] = True
        _save_locked(request_id)


def requests_to_expire() -> list[dict]:
    """7日を過ぎたのに、シート上はまだ「承認待ち」になっている申請。"""
    now = time.time()
    with _lock:
        _ensure_loaded_locked()
        return [
            _public(req)
            for req in _requests.values()
            if req["status"] == STATUS_PENDING and now - req["created_at"] >= REQUEST_EXPIRY_SECONDS
        ]


def mark_expired(request_id: str):
    with _lock:
        _ensure_loaded_locked()
        req = _requests.get(request_id)
        if req is None or req["status"] != STATUS_PENDING:
            return
        req["status"] = STATUS_EXPIRED
        _save_locked(request_id)


# ---------------------------------------------------------------------------
# マッチング成立の記録（「マッチング中」表示用）
# ---------------------------------------------------------------------------

def mark_matched(user_a: str, user_b: str):
    with _lock:
        _matched_pairs.add(frozenset({str(user_a), str(user_b)}))


def unmark_matched(user_a: str, user_b: str):
    with _lock:
        _matched_pairs.discard(frozenset({str(user_a), str(user_b)}))


def matched_targets_for(user_id: str) -> set[str]:
    """指定した会員とマッチング成立済みの相手IDの集合（「マッチング履歴」シート＋このプロセスでの成立分）。"""
    import match_records  # 遅延importで循環参照を避ける

    user_id = str(user_id)
    try:
        pairs = match_records.get_all_matched_pairs()
    except Exception:  # noqa: BLE001 - シートが読めなくても一覧ページ自体は表示する
        pairs = set()
    with _lock:
        pairs = set(pairs) | _matched_pairs
    return {other for pair in pairs if user_id in pair for other in pair if other != user_id}
