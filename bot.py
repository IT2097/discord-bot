from __future__ import annotations

import os
import asyncio
import io
import threading

import discord
from discord.ext import commands

import match_views
import match_store
import match_records
import sheet_sync
import sheets_client
import web_app

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Botのイベントループへの参照。on_ready で確定させる。
bot_loop: asyncio.AbstractEventLoop | None = None

# 会員向けの固定URL（会員情報の確認・マッチング申請ページ）を投稿するチャンネル名
MATCHING_ROOM_CHANNEL_NAME = os.getenv("MATCHING_ROOM_CHANNEL_NAME", "マッチングルーム")
WEB_APP_URL = os.getenv("WEB_APP_URL")

# 新規メンバー参加時に「id同期」の結果を投稿するチャンネル名
ID_SYNC_LOG_CHANNEL_NAME = os.getenv("ID_SYNC_LOG_CHANNEL_NAME", "id同期ログ")

# DMが送れないメンバーのために、本名登録ボタンを常設しておくチャンネル名
NAME_REGISTER_CHANNEL_NAME = os.getenv("NAME_REGISTER_CHANNEL_NAME", "名前登録")

# 参加直後に自動付与し、本名登録が終わったら自動で外すロール名
# （このロールには、事前にDiscord側で「名前登録チャンネル以外は見られない」
# 権限設定をしておく必要があります）
UNVERIFIED_ROLE_NAME = os.getenv("UNVERIFIED_ROLE_NAME", "未登録")


async def _remove_unverified_role(member: discord.Member):
    """本名登録が完了したメンバーから「未登録」ロールを外す。"""
    role = discord.utils.get(member.guild.roles, name=UNVERIFIED_ROLE_NAME)
    if role is None:
        return
    if role in member.roles:
        try:
            await member.remove_roles(role, reason="本名登録が完了したため")
        except discord.Forbidden:
            print(f"「{UNVERIFIED_ROLE_NAME}」ロールを外す権限がBotにありません。")


def run_coro(coro, timeout: int = 15):
    """
    Flask（別スレッド）からBotの非同期処理を呼び出すためのヘルパー。
    Bot側の処理が終わるまでブロックして結果を返す。
    """
    if bot_loop is None:
        raise RuntimeError("Botがまだ起動していません。")
    future = asyncio.run_coroutine_threadsafe(coro, bot_loop)
    return future.result(timeout=timeout)


# --- 旧来の「!setup」コマンド（チャンネル内でボタンを押して即マッチング）も引き続き利用可能 ---
class MatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="マッチングする",
        style=discord.ButtonStyle.primary,
        custom_id="persistent_match_button",
    )
    async def match_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        message = interaction.message
        user = interaction.user

        if guild is None or message is None:
            await interaction.followup.send("サーバー内で実行してください。", ephemeral=True)
            return

        if not message.mentions:
            await interaction.followup.send(
                "募集を作成した会員が確認できません。もう一度 !setup を実行してください。",
                ephemeral=True,
            )
            return

        owner = message.mentions[0]

        if user.id == owner.id:
            await interaction.followup.send("自分のボタンは押せません。", ephemeral=True)
            return

        try:
            channel = await match_views.create_match_channel(guild, owner, user)
            await interaction.followup.send(
                f"マッチングルームを作成しました：{channel.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「チャンネルを管理」の権限がありません。", ephemeral=True
            )
        except discord.HTTPException as error:
            await interaction.followup.send(f"チャンネル作成に失敗しました：{error}", ephemeral=True)


@bot.command()
async def setup(ctx: commands.Context):
    await ctx.send(f"{ctx.author.mention} のマッチング募集👇", view=MatchView())


async def run_id_sync(guild: discord.Guild, send):
    """
    サーバーメンバーのDiscordニックネームとスプレッドシートの「名前（本名）」を
    自動で突き合わせ、一致した分だけDiscordIDをシートに書き込む。

    send: 結果メッセージを送るための非同期関数（ctx.send や channel.send を渡す）。
    「!id同期」コマンドと、新規メンバー参加時の自動実行の両方から呼び出される。
    """
    guild_members = [(str(m.id), m.display_name) for m in guild.members if not m.bot]

    try:
        result = await asyncio.to_thread(sheet_sync.sync_discord_ids, guild_members)
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await send(f"エラーが発生しました：{error}")
        return

    lines = [f"✅ {result['matched']}件、DiscordIDを自動入力しました。"]

    if result["ambiguous_names"]:
        lines.append("")
        lines.append("⚠️ 同名のDiscordメンバーが複数いたため、自動判定をスキップしました（手動確認してください）：")
        lines.append("、".join(result["ambiguous_names"]))

    if result["unmatched_sheet_names"]:
        lines.append("")
        lines.append("📋 シートにあるがDiscordで一致しなかった名前：")
        lines.append("、".join(result["unmatched_sheet_names"]))

    if result["unmatched_discord_names"]:
        lines.append("")
        lines.append("👤 Discordにいるがシートで一致しなかった表示名：")
        lines.append("、".join(result["unmatched_discord_names"]))

    message = "\n".join(lines)

    if len(message) <= 1900:
        await send(message)
    else:
        # Discordの1メッセージ2000文字制限を超える場合はテキストファイルで送る
        buffer = io.StringIO(message)
        await send(
            "結果が長くなったのでファイルに出力しました。",
            file=discord.File(fp=buffer, filename="id同期結果.txt"),
        )


@bot.command(name="id同期")
@commands.has_permissions(administrator=True)
async def sync_ids(ctx: commands.Context):
    """管理者がチャンネルで「!id同期」と打った時に手動で実行するコマンド。"""
    await ctx.send("Discordメンバーとスプレッドシートを突き合わせています…（少し時間がかかります）")
    await run_id_sync(ctx.guild, ctx.send)


@bot.command(name="マッチング解除")
@commands.has_permissions(administrator=True)
async def unmatch(ctx: commands.Context, member_a: discord.Member, member_b: discord.Member):
    """
    2人のマッチング成立記録を取り消す（テストのやり直し用）管理者向けコマンド。
    使い方: !マッチング解除 @ユーザー1 @ユーザー2
    ※作成済みのプライベートなマッチングルーム自体は削除されないので、
      不要であれば手動でチャンネルを削除してください。
    """
    match_store.unmark_matched(str(member_a.id), str(member_b.id))

    try:
        removed = await asyncio.to_thread(
            match_records.remove_match, str(member_a.id), str(member_b.id)
        )
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await ctx.send(f"エラーが発生しました：{error}")
        return

    if removed:
        await ctx.send(f"✅ {member_a.mention} と {member_b.mention} のマッチング記録を削除しました。")
    else:
        await ctx.send(
            f"ℹ️ 履歴シートには記録が見つかりませんでしたが、"
            f"メモリ上の「マッチング中」の記録は削除しました（{member_a.mention} と {member_b.mention}）。"
        )


@bot.command(name="マッチング復元")
@commands.has_permissions(administrator=True)
async def restore_matches(ctx: commands.Context):
    """
    既存の「2人だけが見えるプライベートなテキストチャンネル」をスキャンして、
    マッチング履歴（match_records）にまだ記録されていないペアを書き戻す管理者向けコマンド。
    今回の仕組みを導入する前に成立していたマッチングを拾い直すためのもの。
    """
    await ctx.send("既存のマッチングルームをスキャンしています…（少し時間がかかります）")

    guild = ctx.guild
    found_pairs: set[frozenset] = set()

    for channel in guild.text_channels:
        member_ids = []
        default_denied = False

        for target, overwrite in channel.overwrites.items():
            if target == guild.default_role:
                if overwrite.view_channel is False:
                    default_denied = True
                continue
            if isinstance(target, discord.Member) and not target.bot:
                if overwrite.view_channel is True:
                    member_ids.append(str(target.id))

        # 「@everyoneは見えない」かつ「個人が明示的に2人だけ見える」チャンネルを
        # プライベートなマッチングルームとみなす
        if default_denied and len(member_ids) == 2:
            found_pairs.add(frozenset(member_ids))

    if not found_pairs:
        await ctx.send("それらしいプライベートルームは見つかりませんでした。")
        return

    added = 0
    try:
        existing_pairs = await asyncio.to_thread(match_records.get_all_matched_pairs)
        for pair in found_pairs:
            if pair in existing_pairs:
                continue
            member_a, member_b = tuple(pair)
            await asyncio.to_thread(match_records.append_match, member_a, member_b)
            added += 1
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await ctx.send(f"エラーが発生しました：{error}")
        return

    await ctx.send(
        f"✅ プライベートルームを{len(found_pairs)}件検出し、"
        f"うち{added}件を新たにマッチング履歴へ追加しました。"
    )


class NameModal(discord.ui.Modal, title="本名を登録"):
    """
    「名前を登録する」ボタンを押すと開くフォーム。姓・名を入力してもらい、
    サーバー上のニックネームに反映させたうえでid同期を実行する。
    """

    last_name = discord.ui.TextInput(label="姓", placeholder="例：山田", max_length=20)
    first_name = discord.ui.TextInput(label="名", placeholder="例：太郎", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = os.getenv("GUILD_ID")
        if not guild_id:
            await interaction.followup.send(
                "サーバー設定（GUILD_ID）が未設定のため、登録できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            await interaction.followup.send(
                "サーバー情報が取得できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send(
                "サーバーのメンバーとして確認できませんでした。サーバーに参加してから再度お試しください。",
                ephemeral=True,
            )
            return

        # ニックネームの表記は姓名の間に空白を入れない
        full_name = f"{self.last_name.value.strip()}{self.first_name.value.strip()}"

        try:
            await member.edit(nick=full_name)
        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「ニックネームの管理」権限がないため、名前を変更できませんでした。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"「{full_name}」として登録しました！会員情報を確認しています…",
            ephemeral=True,
        )

        # すでにスプレッドシートに同名の行があるか、その場で同期を試みる
        channel = discord.utils.get(guild.text_channels, name=ID_SYNC_LOG_CHANNEL_NAME)

        async def send(*args, **kwargs):
            if channel is not None:
                await channel.send(*args, **kwargs)

        await run_id_sync(guild, send)

        profile = sheets_client.find_member(str(member.id), force_refresh=True)

        if profile is not None:
            # 既存の行と連携できたので、これで登録完了
            await _remove_unverified_role(member)
            await interaction.followup.send(
                "既存の会員情報と連携できました！登録は完了です。", ephemeral=True
            )
            return

        # 既存の行が見つからなかった新規会員には、続けてプロフィールを入力してもらう
        await interaction.followup.send(
            "会員情報が見つからなかったため、続けてプロフィールを入力してください。\n"
            "まずは年齢と性別を選んで「次へ」を押してください。",
            ephemeral=True,
            view=ProfileBasicsView(member_id=member.id, name=full_name),
        )


AGE_OPTIONS = ["10代", "20代", "30代", "40代", "50代", "60代", "70代以上"]
GENDER_OPTIONS = ["男性", "女性", "その他"]


class ProfileBasicsView(discord.ui.View):
    """
    新規会員のプロフィール入力（1/2）。年齢・性別をプルダウンで選んでもらい、
    「次へ」で都道府県・市町村・業種・事業内容の入力フォーム（2/2）に進む。
    """

    def __init__(self, member_id: int, name: str):
        super().__init__(timeout=600)
        self.member_id = member_id
        self.name = name
        self.selected_age: str | None = None
        self.selected_gender: str | None = None

    @discord.ui.select(
        placeholder="年齢を選択",
        options=[discord.SelectOption(label=age) for age in AGE_OPTIONS],
    )
    async def select_age(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_age = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="性別を選択",
        options=[discord.SelectOption(label=gender) for gender in GENDER_OPTIONS],
    )
    async def select_gender(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_gender = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.primary)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "この登録フォームはあなた宛てではありません。", ephemeral=True
            )
            return

        if not self.selected_age or not self.selected_gender:
            await interaction.response.send_message(
                "年齢と性別の両方を選択してから「次へ」を押してください。", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            ProfileDetailsModal(
                member_id=self.member_id,
                name=self.name,
                age=self.selected_age,
                gender=self.selected_gender,
            )
        )


class ProfileDetailsModal(discord.ui.Modal, title="プロフィール入力（続き）"):
    """
    新規会員のプロフィール入力（2/2）。都道府県・市町村・業種・事業内容を入力してもらい、
    完了したらスプレッドシートに新しい行として書き込む。
    """

    prefecture = discord.ui.TextInput(label="都道府県", placeholder="例：北海道", max_length=10)
    city = discord.ui.TextInput(label="市町村", placeholder="例：札幌市", max_length=20)
    business_type = discord.ui.TextInput(label="業種", placeholder="例：飲食サービス", max_length=30)
    business_content = discord.ui.TextInput(
        label="事業/活動内容",
        style=discord.TextStyle.paragraph,
        placeholder="例：カフェの経営",
        max_length=200,
    )

    def __init__(self, member_id: int, name: str, age: str, gender: str):
        super().__init__()
        self.member_id = member_id
        self.name = name
        self.age = age
        self.gender = gender

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 二重登録防止：この間に別の経路ですでに登録が完了していないか念のため確認する
        existing = sheets_client.find_member(str(self.member_id), force_refresh=True)
        if existing is not None:
            await interaction.followup.send(
                "すでに会員情報が登録されているようです。登録は完了しています。",
                ephemeral=True,
            )
            return

        profile = {
            "name": self.name,
            "discord_id": str(self.member_id),
            "age": self.age,
            "gender": self.gender,
            "prefecture": self.prefecture.value.strip(),
            "city": self.city.value.strip(),
            "business_type": self.business_type.value.strip(),
            "business_content": self.business_content.value.strip(),
        }

        try:
            await asyncio.to_thread(sheet_sync.append_new_member_row, profile)
        except Exception as error:  # noqa: BLE001 - 本人にそのままエラー内容を見せる
            await interaction.followup.send(
                f"スプレッドシートへの登録に失敗しました：{error}", ephemeral=True
            )
            return

        # 直接書き込んだ内容を、キャッシュを待たずにすぐ一覧へ反映させる
        sheets_client.get_members(force_refresh=True)

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild is not None:
                try:
                    member = await guild.fetch_member(self.member_id)
                    await _remove_unverified_role(member)
                except discord.NotFound:
                    pass

        await interaction.followup.send(
            "プロフィールの登録が完了しました！ありがとうございます。", ephemeral=True
        )


class NameRegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="名前を登録する",
        style=discord.ButtonStyle.primary,
        custom_id="persistent_name_register_button",
    )
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NameModal())


@bot.event
async def on_member_join(member: discord.Member):
    """
    新しいメンバーがサーバーに参加したら、
    1. 「未登録」ロールを付与する（名前登録が終わるまで他のチャンネルを見せないため）
    2. 本名登録フォーム（DM）を送る。DMが送れない場合は「名前登録」チャンネルで案内する
    3. 自動で「id同期」も実行する（すでにシートに名前がある人を拾うため）
    """
    guild = member.guild

    unverified_role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
    if unverified_role is not None:
        try:
            await member.add_roles(unverified_role, reason="本名登録が完了するまでの制限用")
        except discord.Forbidden:
            print(f"「{UNVERIFIED_ROLE_NAME}」ロールを付与する権限がBotにありません。")
    else:
        print(f"「{UNVERIFIED_ROLE_NAME}」という名前のロールが見つかりませんでした。")

    try:
        await member.send(
            f"🎉 {guild.name} へようこそ！\n"
            "会員情報と連携するために、まずは本名を登録してください。",
            view=NameRegisterView(),
        )
    except discord.Forbidden:
        # DMを閉じているユーザーには、サーバー内の「名前登録」チャンネルで案内する
        register_channel = discord.utils.get(guild.text_channels, name=NAME_REGISTER_CHANNEL_NAME)
        if register_channel is not None:
            await register_channel.send(
                f"{member.mention} さん、ようこそ！DMが送れなかったため、こちらから本名を登録してください👇"
            )

    channel = discord.utils.get(guild.text_channels, name=ID_SYNC_LOG_CHANNEL_NAME)

    async def send(*args, **kwargs):
        if channel is not None:
            await channel.send(*args, **kwargs)
        else:
            print(f"「{ID_SYNC_LOG_CHANNEL_NAME}」チャンネルが見つからないため、id同期の結果を送信できませんでした。")

    if channel is not None:
        await channel.send(f"👋 {member.mention} さんが参加しました。DiscordIDの自動突き合わせを行います…")

    await run_id_sync(guild, send)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    メンバーのニックネームが変更されたら、自動で「id同期」を実行する。
    名前登録フォーム経由の変更・本人がDiscordの設定から手動で変更した場合の
    どちらも検知する（ロールの変更など、ニックネーム以外の更新は対象外）。
    """
    if before.nick == after.nick:
        return

    guild = after.guild
    channel = discord.utils.get(guild.text_channels, name=ID_SYNC_LOG_CHANNEL_NAME)

    async def send(*args, **kwargs):
        if channel is not None:
            await channel.send(*args, **kwargs)
        else:
            print(f"「{ID_SYNC_LOG_CHANNEL_NAME}」チャンネルが見つからないため、id同期の結果を送信できませんでした。")

    if channel is not None:
        await channel.send(
            f"✏️ {after.mention} さんのニックネームが変更されました。DiscordIDの自動突き合わせを行います…"
        )

    await run_id_sync(guild, send)


async def post_matching_room_link():
    """
    「マッチングルーム」チャンネルに、会員情報確認・マッチング申請ページの
    固定URLを投稿する。すでに投稿済み（直近の履歴にBot自身の同じURL投稿がある）
    場合は再投稿しない（Railway再起動のたびにスパムしないため）。
    """
    guild_id = os.getenv("GUILD_ID")

    if not guild_id or not WEB_APP_URL:
        print("GUILD_ID または WEB_APP_URL が未設定のため、リンク投稿をスキップしました。")
        return

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        print("マッチングルームへの投稿に失敗：Botが指定サーバーに参加していません。")
        return

    channel = discord.utils.get(guild.text_channels, name=MATCHING_ROOM_CHANNEL_NAME)
    if channel is None:
        print(f"「{MATCHING_ROOM_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりませんでした。")
        return

    async for message in channel.history(limit=20):
        if message.author.id == bot.user.id and WEB_APP_URL in message.content:
            return  # すでに投稿済み

    await channel.send(f"🔗 会員情報の確認・マッチング申請はこちらから！\n{WEB_APP_URL}")


async def post_name_register_button():
    """
    「名前登録」チャンネルに、本名登録ボタンを投稿する
    （DMを受け取れないメンバーのための導線）。すでに投稿済みなら再投稿しない。
    """
    guild_id = os.getenv("GUILD_ID")

    if not guild_id:
        return

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return

    channel = discord.utils.get(guild.text_channels, name=NAME_REGISTER_CHANNEL_NAME)
    if channel is None:
        print(f"「{NAME_REGISTER_CHANNEL_NAME}」という名前のテキストチャンネルが見つかりませんでした。")
        return

    async for message in channel.history(limit=20):
        if message.author.id == bot.user.id and message.components:
            return  # すでに投稿済み

    await channel.send("本名を登録するには、下のボタンを押してください👇", view=NameRegisterView())


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    # Railway再起動後も既存ボタンを反応させる
    bot.add_view(MatchView())
    bot.add_view(NameRegisterView())
    print(f"ログインしました: {bot.user}")

    await post_matching_room_link()
    await post_name_register_button()


def start_web_server():
    app = web_app.create_app(bot, run_coro)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


token = os.getenv("TOKEN")

if not token:
    raise RuntimeError("RailwayのVariablesにTOKENが設定されていません。")

# WebサーバーはBotとは別スレッドで動かす（Botのループはメインスレッドで動かし続ける）
web_thread = threading.Thread(target=start_web_server, daemon=True)
web_thread.start()

bot.run(token)
