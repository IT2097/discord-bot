from __future__ import annotations

import os
import asyncio
import io
import threading

import discord
from discord.ext import commands

import match_views
import sheet_sync
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


@bot.command(name="id同期")
@commands.has_permissions(administrator=True)
async def sync_ids(ctx: commands.Context):
    """
    サーバーメンバーのDiscordニックネームとスプレッドシートの「名前（本名）」を
    自動で突き合わせ、一致した分だけDiscordIDをシートに書き込む管理者向けコマンド。
    """
    await ctx.send("Discordメンバーとスプレッドシートを突き合わせています…（少し時間がかかります）")

    guild_members = [(str(m.id), m.display_name) for m in ctx.guild.members if not m.bot]

    try:
        result = await asyncio.to_thread(sheet_sync.sync_discord_ids, guild_members)
    except Exception as error:  # noqa: BLE001 - 管理者にそのままエラー内容を見せる
        await ctx.send(f"エラーが発生しました：{error}")
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
        await ctx.send(message)
    else:
        # Discordの1メッセージ2000文字制限を超える場合はテキストファイルで送る
        buffer = io.StringIO(message)
        await ctx.send(
            "結果が長くなったのでファイルに出力しました。",
            file=discord.File(fp=buffer, filename="id同期結果.txt"),
        )


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


@bot.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_running_loop()

    # Railway再起動後も既存ボタンを反応させる
    bot.add_view(MatchView())
    print(f"ログインしました: {bot.user}")

    await post_matching_room_link()


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
