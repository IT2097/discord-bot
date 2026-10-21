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


# 都道府県の並び順（全国地方公共団体コード順＝北海道から沖縄まで）
PREFECTURE_ORDER = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県",
    "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]


def _prefecture_sort_key(name: str):
    """「北海道」「東京」「東京都」のように「都・府・県」の有無がばらばらでも、正しい順番に並べる。"""
    text = name.strip()
    for index, prefecture in enumerate(PREFECTURE_ORDER):
        short = prefecture if prefecture == "北海道" else prefecture[:-1]
        if text in (prefecture, short):
            return (0, index, text)
    return (1, 0, text)  # 都道府県名として読めないものは最後


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

        # クライアント側での検索・絞り込み用に、名前＋業種＋事業内容をまとめた
        # 検索用テキストを各会員に持たせておく
        for member in other_members:
            business_text = " ".join(
                f"{biz.get('type', '')} {biz.get('content', '')}"
                for biz in member.get("businesses", [])
            )
            member["search_blob"] = f"{member['name']} {business_text}".strip().lower()

        # Discordで設定しているアイコン画像のURLを付ける（未設定・サーバーにいない人は名前の1文字目を表示）
        try:
            avatar_urls = run_coro(_avatar_urls(bot, [m["discord_id"] for m in other_members]))
        except Exception:  # noqa: BLE001 - アイコンが取れなくても一覧自体は表示する
            avatar_urls = {}
        for member in other_members:
            member["avatar_url"] = avatar_urls.get(member["discord_id"])

        ages = sorted({m["age"] for m in other_members if m.get("age")})
        genders = sorted({m["gender"] for m in other_members if m.get("gender")})
        # 都道府県は北から順（北海道→沖縄）に並べる。一覧にない書き方（「札幌」など）は最後に回す
        prefectures = sorted(
            {m["prefecture"] for m in other_members if m.get("prefecture")},
            key=_prefecture_sort_key,
        )

        return render_template(
            "index.html",
            my_name=my_name,
            members=other_members,
            pending=pending,
            matched=matched,
            ages=ages,
            genders=genders,
            prefectures=prefectures,
        )

    @app.route("/login")
    def login():
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state

        # 「別のアカウントでログインし直す」から来た場合は、Discordの確認画面を必ず表示する
        # （確認画面に今のアカウントが表示され、「あなたではありませんか？」から切り替えられる）
        switch_account = request.args.get("switch") == "1"

        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "consent" if switch_account else "none",
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
            membership = run_coro(_check_guild_membership(bot, discord_id))
        except Exception:  # noqa: BLE001 - botの起動中・応答なしなど
            membership = "unavailable"

        if membership == "unavailable":
            return render_template(
                "error.html",
                message="ただいまシステムの準備中です。1〜2分ほど待ってから、もう一度お試しください。",
                actions=[{"label": "もう一度ログインする", "href": url_for("login"), "primary": True}],
            )

        if membership == "not_member":
            # 運営がRailwayのログで確認できるように記録しておく
            print(f"[ログイン拒否] サーバーにいないアカウント：{discord_name}（ID：{discord_id}）")

            avatar_hash = user_json.get("avatar")
            avatar_url = (
                f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png?size=96"
                if avatar_hash
                else None
            )
            return render_template(
                "error.html",
                message="このDiscordアカウントは、リジェのサーバーに参加していません。",
                account={
                    "name": discord_name,
                    "username": user_json.get("username", ""),
                    "id": discord_id,
                    "avatar_url": avatar_url,
                },
                hints=[
                    "Discordのアプリと、このブラウザでは、ログインしているアカウントが別々です。",
                    "上のアカウントが、いつもアプリで使っているアカウントと違う場合は、下のボタンから切り替えてください。",
                    "切り替えの画面で「あなたではありませんか？」を押すと、別のアカウントでログインできます。",
                ],
                actions=[
                    {"label": "別のアカウントでログインし直す", "href": url_for("login", switch=1), "primary": True},
                ],
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

    @app.route("/request/<target_id>/cancel", methods=["POST"])
    def cancel_request(target_id):
        if "discord_id" not in session:
            return {"ok": False, "message": "ログインしてください。"}, 401

        my_id = session["discord_id"]

        request = match_store.find_pending_request(my_id, target_id)
        if request is None:
            if str(target_id) in match_store.matched_targets_for(my_id):
                return {"ok": False, "state": "matched", "message": "相手がすでに承認しているため、取り消せませんでした。"}, 409
            return {"ok": False, "state": "open", "message": "取り消せる申請がありません（すでに断られたか、期限が切れています）。"}, 409

        # 先に「取消」にしておくことで、削除中に相手が承認ボタンを押しても承認されないようにする
        if match_store.resolve_request(request["request_id"], match_store.STATUS_CANCELLED) is None:
            return {"ok": False, "state": "matched", "message": "相手がちょうど返事をしたため、取り消せませんでした。"}, 409

        try:
            run_coro(_delete_request_notice(bot, request))
        except Exception as error:  # noqa: BLE001 - 申請の取り消し自体は済んでいる
            print(f"取り消した申請のメッセージ削除に失敗しました：{error}")

        return {"ok": True, "state": "open", "message": "マッチング申請を取り消しました。"}

    return app


async def _avatar_urls(bot, discord_ids: list[str]) -> dict[str, str]:
    """
    会員のDiscordアイコン画像のURLを返す（botが持っているサーバーのメンバー情報から取るので、通信は発生しない）。
    サーバー用のアイコンがあればそれを、なければ普段のアイコンを使う。
    アイコンを設定していない人と、サーバーにいない人は含めない。
    """
    guild = bot.get_guild(int(GUILD_ID))
    if guild is None:
        return {}
    urls: dict[str, str] = {}
    for discord_id in discord_ids:
        try:
            member = guild.get_member(int(discord_id))
        except (TypeError, ValueError):
            continue
        if member is None or (member.guild_avatar is None and member.avatar is None):
            continue
        urls[str(discord_id)] = member.display_avatar.replace(size=128, static_format="png").url
    return urls


async def _check_guild_membership(bot, discord_id: str) -> str:
    """
    対象サーバーのメンバーかどうかを確認する。
      "member"      … サーバーにいる
      "not_member"  … サーバーにいない（別アカウントでログインしている可能性が高い）
      "unavailable" … botの起動直後などで確認できなかった（時間をおけば通る）
    """
    import discord

    guild = bot.get_guild(int(GUILD_ID))
    if guild is None:
        return "unavailable"
    if guild.get_member(int(discord_id)) is not None:
        return "member"
    try:
        await guild.fetch_member(int(discord_id))
        return "member"
    except discord.NotFound:
        return "not_member"
    except Exception:  # noqa: BLE001 - 通信エラーなど
        return "unavailable"


async def _notify_match_request(bot, requester_id: str, target_id: str, request_id: str):
    import match_views

    await match_views.send_match_request_to_channel(bot, requester_id, target_id, request_id)


async def _delete_request_notice(bot, request: dict):
    import match_views

    await match_views.delete_cancelled_request_message(bot, request)
