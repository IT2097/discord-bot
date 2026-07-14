import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


class MatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="マッチングする",
        style=discord.ButtonStyle.primary,
        custom_id="persistent_match_button"
    )
    async def match_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        # 先に応答を保留して「インタラクション失敗」を防ぐ
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        message = interaction.message
        user = interaction.user

        if guild is None or message is None:
            await interaction.followup.send(
                "サーバー内で実行してください。",
                ephemeral=True
            )
            return

        # !setupを実行した人は、募集メッセージ内の最初のメンション
        if not message.mentions:
            await interaction.followup.send(
                "募集を作成した会員が確認できません。もう一度 !setup を実行してください。",
                ephemeral=True
            )
            return

        owner = message.mentions[0]

        if user.id == owner.id:
            await interaction.followup.send(
                "自分のボタンは押せません。",
                ephemeral=True
            )
            return

        bot_member = guild.me

        if bot_member is None:
            await interaction.followup.send(
                "Bot情報を取得できませんでした。",
                ephemeral=True
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            owner: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            ),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True
            )
        }

        try:
            channel = await guild.create_text_channel(
                name=f"{owner.display_name}-{user.display_name}",
                overwrites=overwrites
            )

            await channel.send(
                f"{owner.mention} と {user.mention} のマッチングルームです！"
            )

            await interaction.followup.send(
                f"マッチングルームを作成しました：{channel.mention}",
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "Botに「チャンネルを管理」の権限がありません。",
                ephemeral=True
            )

        except discord.HTTPException as error:
            await interaction.followup.send(
                f"チャンネル作成に失敗しました：{error}",
                ephemeral=True
            )


@bot.command()
async def setup(ctx: commands.Context):
    await ctx.send(
        f"{ctx.author.mention} のマッチング募集👇",
        view=MatchView()
    )


@bot.event
async def on_ready():
    # Railway再起動後も既存ボタンを反応させる
    bot.add_view(MatchView())
    print(f"ログインしました: {bot.user}")


token = os.getenv("TOKEN")

if not token:
    raise RuntimeError("RailwayのVariablesにTOKENが設定されていません。")

bot.run(token)
