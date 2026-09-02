"""
会員一覧＋マッチング申請ボタンを表示するWebページ。

ログインは「Discordでログイン」のOAuth2を使用し、
ログインしたユーザー自身のDiscordIDを申請者として扱います
（＝なりすまし防止のため、ページ上でユーザーを選択させるのではなく、
Discordログインで本人確認をしています）。
"""

import os
import secrets

import requests
from flask import Flask, redirect, render_template, request, session, url_for, flash

import sheets_client
import match_store

DISCORD_API_BASE = "https://discord.com/api"

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")  # 例: https://xxxx.up.railway.app/callback
GUILD_ID = os.getenv("GUILD_ID")


def create_app(bot, run_coro):
    """
    bot: discord.ext.commands.Bot のインスタンス（承認チャンネルへの投稿などに使う）
    run_coro: Botのイベントループ上でコルーチンを実行し、結果を返すヘルパー関数
              （Flaskは別スレッドで動いているため、Bot側の処理はこれ経由で呼び出す）
    """
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

    def _missing_config():
        problems = []
        if not CLIENT_ID:
            problems.append("DISCORD_CLIENT_ID")
        if not CLIENT_SECRET:
            problems.append("DISCORD_CLIENT_SECRET")
        if not REDIRECT_URI:
            problems.append("DISCORD_REDIRECT_URI")
        if not GUILD_ID:
            problems.append("GUILD_ID")
        return problems

    @app.route("/")
    def index():
        missing = _missing_config()
        if missing:
            return render_template("error.html", message=f"未設定の環境変数があります: {', '.join(missing)}")

        if "discord_id" not in session:
            return render_template("login.html")

        my_id = session["discord_id"]
        my_name = session.get("discord_name", "")

        try:
            members = sheets_client.get_members()
        except Exception as error:  # noqa: BLE001 - ユーザーにそのままエラー内容を見せる
            return render_template("error.html", message=f"会員一覧の取得に失敗しました：{error}")

        pending = match_store.pending_targets_for(my_id)
        matched = match_store.matched_targets_for(my_id)

        other_members = [m for m in members if m["discord_id"] != str(my_id)]

        return render_template(
            "index.html",
            my_name=my_name,
            members=other_members,
            pending=pending,
            matched=matched,
        )

    @app.route("/login")
    def login():
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state

        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "none",
        }
        query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        return redirect(f"{DISCORD_API_BASE}/oauth2/authorize?{query}")

    @app.route("/callback")
    def callback():
        error = request.args.get("error")
        if error:
            return render_template("error.html", message="ログインがキャンセルされました。")

        code = request.args.get("code")
        state = request.args.get("state")

        if not code or not state or state != session.get("oauth_state"):
            return render_template("error.html", message="ログインに失敗しました（state不一致）。もう一度お試しください。")

        token_res = requests.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )

        if token_res.status_code != 200:
            return render_template("error.html", message="Discordとの認証に失敗しました。")

        access_token = token_res.json().get("access_token")

        user_res = requests.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )

        if user_res.status_code != 200:
            return render_template("error.html", message="ユーザー情報の取得に失敗しました。")

        user_json = user_res.json()
        discord_id = user_json["id"]
        discord_name = user_json.get("global_name") or user_json.get("username")

        # 対象サーバーのメンバーかどうかをBot経由で確認する
        try:
            is_member = run_coro(_is_guild_member(bot, discord_id))
        except Exception:
            is_member = False

        if not is_member:
            return render_template(
                "error.html",
                message="対象サーバーのメンバーとして確認できませんでした。サーバーに参加してから再度お試しください。",
            )

        session["discord_id"] = discord_id
        session["discord_name"] = discord_name
        return redirect(url_for("index"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/request/<target_id>", methods=["POST"])
    def create_request(target_id):
        if "discord_id" not in session:
            return {"ok": False, "message": "ログインしてください。"}, 401

        my_id = session["discord_id"]

        if str(my_id) == str(target_id):
            return {"ok": False, "message": "自分自身には申請できません。"}, 400

        request_id = match_store.create_request(my_id, target_id)
        if request_id is None:
            return {"ok": False, "message": "この相手にはすでに申請中です。"}, 409

        try:
            run_coro(_notify_match_request(bot, my_id, target_id, request_id))
        except Exception as error:  # noqa: BLE001
            match_store.resolve_request(request_id)
            return {"ok": False, "message": f"申請の送信に失敗しました：{error}"}, 500

        return {"ok": True, "message": "マッチング申請を送信しました！"}

    return app


async def _is_guild_member(bot, discord_id: str) -> bool:
    guild = bot.get_guild(int(GUILD_ID))
    if guild is None:
        return False
    try:
        await guild.fetch_member(int(discord_id))
        return True
    except Exception:
        return False


async def _notify_match_request(bot, requester_id: str, target_id: str, request_id: str):
    import match_views

    await match_views.send_match_request_to_channel(bot, requester_id, target_id, request_id)
