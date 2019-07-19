__version__ = "3.1.0"

import asyncio
import logging
import os
import re
import sys
import typing

from datetime import datetime
from types import SimpleNamespace

import discord
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView

import isodate

from aiohttp import ClientSession
from emoji import UNICODE_EMOJI
from motor.motor_asyncio import AsyncIOMotorClient

try:
    from colorama import init

    init()
except ImportError:
    pass

from core.clients import ApiClient, PluginDatabaseClient
from core.config import ConfigManager
from core.utils import human_join, strtobool
from core.models import PermissionLevel, ModmailLogger
from core.thread import ThreadManager
from core.time import human_timedelta


logger: ModmailLogger = logging.getLogger("Modmail")
logger.__class__ = ModmailLogger

logger.setLevel(logging.INFO)

ch = logging.StreamHandler(stream=sys.stdout)
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(filename)s[%(lineno)d] - %(levelname)s: %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)


class FileFormatter(logging.Formatter):
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def format(self, record):
        record.msg = self.ansi_escape.sub("", record.msg)
        return super().format(record)


temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")
if not os.path.exists(temp_dir):
    os.mkdir(temp_dir)


class ModmailBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=None)  # implemented in `get_prefix`
        self._session = None
        self._api = None
        self._connected = asyncio.Event()
        self.start_time = datetime.utcnow()

        self.config = ConfigManager(self)
        self.config.populate_cache()

        self.threads = ThreadManager(self)

        self._configure_logging()

        mongo_uri = self.config["mongo_uri"]
        if mongo_uri is None:
            raise ValueError("A Mongo URI is necessary for the bot to function.")

        self.db = AsyncIOMotorClient(mongo_uri).modmail_bot
        self.plugin_db = PluginDatabaseClient(self)

        self.metadata_loop = None

        self._load_extensions()

    @property
    def uptime(self) -> str:
        now = datetime.utcnow()
        delta = now - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)

        fmt = "{h}h {m}m {s}s"
        if days:
            fmt = "{d}d " + fmt

        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    def _configure_logging(self):
        level_text = self.config["log_level"].upper()
        logging_levels = {
            "CRITICAL": logging.CRITICAL,
            "ERROR": logging.ERROR,
            "WARNING": logging.WARNING,
            "INFO": logging.INFO,
            "DEBUG": logging.DEBUG,
        }

        log_file_name = self.token.split(".")[0]
        ch_debug = logging.FileHandler(
            os.path.join(temp_dir, f"{log_file_name}.log"), mode="a+"
        )

        ch_debug.setLevel(logging.DEBUG)
        formatter_debug = FileFormatter(
            "%(asctime)s %(filename)s[%(lineno)d] - %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        ch_debug.setFormatter(formatter_debug)
        logger.addHandler(ch_debug)

        log_level = logging_levels.get(level_text)
        if log_level is None:
            log_level = self.config.remove("log_level")

        logger.line()
        if log_level is not None:
            logger.setLevel(log_level)
            ch.setLevel(log_level)
            logger.info("Logging level: %s", level_text)
        else:
            logger.info("Invalid logging level set.")
            logger.warning("Using default logging level: INFO.")

    @property
    def version(self) -> str:
        return __version__

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            self._session = ClientSession(loop=self.loop)
        return self._session

    @property
    def api(self):
        if self._api is None:
            self._api = ApiClient(self)
        return self._api

    async def get_prefix(self, message=None):
        return [self.prefix, f"<@{self.user.id}> ", f"<@!{self.user.id}> "]

    def _load_extensions(self):
        """Adds commands automatically"""
        logger.line()
        logger.info("┌┬┐┌─┐┌┬┐┌┬┐┌─┐┬┬")
        logger.info("││││ │ │││││├─┤││")
        logger.info("┴ ┴└─┘─┴┘┴ ┴┴ ┴┴┴─┘")
        logger.info("v%s", __version__)
        logger.info("Authors: kyb3r, fourjr, Taaku18")
        logger.line()

        for file in os.listdir("cogs"):
            if not file.endswith(".py"):
                continue
            cog = f"cogs.{file[:-3]}"
            logger.info("Loading %s.", cog)
            try:
                self.load_extension(cog)
            except Exception:
                logger.exception("Failed to load %s.", cog)

    def run(self, *args, **kwargs):
        try:
            self.loop.run_until_complete(self.start(self.token))
        except KeyboardInterrupt:
            pass
        except discord.LoginFailure:
            logger.critical("Invalid token")
        except Exception:
            logger.critical("Fatal exception", exc_info=True)
        finally:
            self.loop.run_until_complete(self.logout())
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            try:
                self.loop.run_until_complete(
                    asyncio.gather(*asyncio.all_tasks(self.loop))
                )
            except asyncio.CancelledError:
                logger.debug("All pending tasks has been cancelled.")
            finally:
                self.loop.run_until_complete(self.session.close())
                logger.error(" - Shutting down bot - ")

    async def is_owner(self, user: discord.User) -> bool:
        owners = self.config["owners"]
        if owners is not None:
            if user.id in set(map(int, str(owners).split(","))):
                return True
        return await super().is_owner(user)

    @property
    def log_channel(self) -> typing.Optional[discord.TextChannel]:
        channel_id = self.config["log_channel_id"]
        if channel_id is not None:
            channel = self.get_channel(int(channel_id))
            if channel is not None:
                return channel
            self.config.remove("log_channel_id")
        if self.main_category is not None:
            try:
                channel = self.main_category.channels[0]
                self.config["log_channel_id"] = channel.id
                logger.debug("No log channel set, however, one was found. Setting...")
                return channel
            except IndexError:
                pass
        logger.info(
            "No log channel set, set one with `%ssetup` or "
            "`%sconfig set log_channel_id <id>`.",
            self.prefix,
            self.prefix,
        )
        return None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def wait_for_connected(self) -> None:
        await self.wait_until_ready()
        await self._connected.wait()
        await self.config.wait_until_ready()

    @property
    def snippets(self) -> typing.Dict[str, str]:
        return self.config["snippets"]

    @property
    def aliases(self) -> typing.Dict[str, str]:
        return self.config["aliases"]

    @property
    def token(self) -> str:
        token = self.config["token"]
        if token is None:
            raise ValueError("TOKEN must be set, this is your bot token.")
        return token

    @property
    def guild_id(self) -> typing.Optional[int]:
        guild_id = self.config["guild_id"]
        if guild_id is not None:
            try:
                return int(str(guild_id))
            except ValueError:
                raise ValueError("Invalid guild_id set.")
        return None

    @property
    def guild(self) -> typing.Optional[discord.Guild]:
        """
        The guild that the bot is serving
        (the server where users message it from)
        """
        return discord.utils.get(self.guilds, id=self.guild_id)

    @property
    def modmail_guild(self) -> typing.Optional[discord.Guild]:
        """
        The guild that the bot is operating in
        (where the bot is creating threads)
        """
        modmail_guild_id = self.config["modmail_guild_id"]
        if modmail_guild_id is None:
            return self.guild
        guild = discord.utils.get(self.guilds, id=int(modmail_guild_id))
        if guild is not None:
            return guild
        self.config.remove("modmail_guild_id")
        logger.error("Invalid modmail_guild_id set.")
        return self.guild

    @property
    def using_multiple_server_setup(self) -> bool:
        return self.modmail_guild != self.guild

    @property
    def main_category(self) -> typing.Optional[discord.CategoryChannel]:
        if self.modmail_guild is not None:
            category_id = self.config["main_category_id"]
            if category_id is not None:
                cat = discord.utils.get(
                    self.modmail_guild.categories, id=int(category_id)
                )
                if cat is not None:
                    return cat
                self.config.remove("main_category_id")
            cat = discord.utils.get(self.modmail_guild.categories, name="Modmail")
            if cat is not None:
                self.config["main_category_id"] = cat.id
                logger.debug("No main category set, however, one was found. Setting...")
                return cat
        return None

    @property
    def blocked_users(self) -> typing.Dict[str, str]:
        return self.config["blocked"]

    @property
    def blocked_whitelisted_users(self) -> typing.List[str]:
        return self.config["blocked_whitelist"]

    @property
    def prefix(self) -> str:
        return str(self.config["prefix"])

    @property
    def mod_color(self) -> int:
        color = self.config["mod_color"]
        try:
            return int(color.lstrip("#"), base=16)
        except ValueError:
            logger.error("Invalid mod_color provided.")
        return int(self.config.remove("mod_color").lstrip("#"), base=16)

    @property
    def recipient_color(self) -> int:
        color = self.config["recipient_color"]
        try:
            return int(color.lstrip("#"), base=16)
        except ValueError:
            logger.error("Invalid recipient_color provided.")
        return int(self.config.remove("recipient_color").lstrip("#"), base=16)

    @property
    def main_color(self) -> int:
        color = self.config["main_color"]
        try:
            return int(color.lstrip("#"), base=16)
        except ValueError:
            logger.error("Invalid main_color provided.")
        return int(self.config.remove("main_color").lstrip("#"), base=16)

    async def on_connect(self):
        logger.line()
        try:
            await self.validate_database_connection()
        except Exception:
            return await self.logout()

        logger.line()
        logger.info("Connected to gateway.")
        await self.config.refresh()
        await self.setup_indexes()
        self._connected.set()

    async def setup_indexes(self):
        """Setup text indexes so we can use the $search operator"""
        coll = self.db.logs
        index_name = "messages.content_text_messages.author.name_text_key_text"

        index_info = await coll.index_information()

        # Backwards compatibility
        old_index = "messages.content_text_messages.author.name_text"
        if old_index in index_info:
            logger.info("Dropping old index: %s", old_index)
            await coll.drop_index(old_index)

        if index_name not in index_info:
            logger.info('Creating "text" index for logs collection.')
            logger.info("Name: %s", index_name)
            await coll.create_index(
                [
                    ("messages.content", "text"),
                    ("messages.author.name", "text"),
                    ("key", "text"),
                ]
            )

    async def on_ready(self):
        """Bot startup, sets uptime."""

        # Wait until config cache is populated with stuff from db and on_connect ran
        await self.wait_for_connected()

        logger.line()
        logger.info("Client ready.")
        logger.line()
        logger.info("Logged in as: %s", self.user)
        logger.info("User ID: %s", self.user.id)
        logger.info("Prefix: %s", self.prefix)
        logger.info("Guild Name: %s", self.guild.name if self.guild else "Invalid")
        logger.info("Guild ID: %s", self.guild.id if self.guild else "Invalid")
        logger.line()

        await self.threads.populate_cache()

        # closures
        closures = self.config["closures"]
        logger.info("There are %d thread(s) pending to be closed.", len(closures))

        for recipient_id, items in tuple(closures.items()):
            after = (
                datetime.fromisoformat(items["time"]) - datetime.utcnow()
            ).total_seconds()
            if after < 0:
                after = 0

            thread = await self.threads.find(recipient_id=int(recipient_id))

            if not thread:
                # If the channel is deleted
                self.config["closures"].pop(recipient_id)
                await self.config.update()
                continue

            await thread.close(
                closer=self.get_user(items["closer_id"]),
                after=after,
                silent=items["silent"],
                delete_channel=items["delete_channel"],
                message=items["message"],
                auto_close=items.get("auto_close", False),
            )

        logger.line()

        self.metadata_loop = tasks.Loop(
            self.post_metadata,
            seconds=0,
            minutes=0,
            hours=1,
            count=None,
            reconnect=True,
            loop=None,
        )
        self.metadata_loop.before_loop(self.before_post_metadata)
        self.metadata_loop.after_loop(self.after_post_metadata)
        self.metadata_loop.start()

    async def convert_emoji(self, name: str) -> str:
        ctx = SimpleNamespace(bot=self, guild=self.modmail_guild)
        converter = commands.EmojiConverter()

        if name not in UNICODE_EMOJI:
            try:
                name = await converter.convert(ctx, name.strip(":"))
            except commands.BadArgument:
                logger.warning("%s is not a valid emoji.", name)
                raise
        return name

    async def retrieve_emoji(self) -> typing.Tuple[str, str]:

        sent_emoji = self.config["sent_emoji"]
        blocked_emoji = self.config["blocked_emoji"]

        if sent_emoji != "disable":
            try:
                sent_emoji = await self.convert_emoji(sent_emoji)
            except commands.BadArgument:
                logger.warning("Removed sent emoji (%s).", sent_emoji)
                sent_emoji = self.config.remove("sent_emoji")

        if blocked_emoji != "disable":
            try:
                blocked_emoji = await self.convert_emoji(blocked_emoji)
            except commands.BadArgument:
                logger.warning("Removed blocked emoji (%s).", blocked_emoji)
                blocked_emoji = self.config.remove("blocked_emoji")

        await self.config.update()
        return sent_emoji, blocked_emoji

    async def _process_blocked(self, message: discord.Message) -> bool:
        sent_emoji, blocked_emoji = await self.retrieve_emoji()

        if str(message.author.id) in self.blocked_whitelisted_users:
            if str(message.author.id) in self.blocked_users:
                self.blocked_users.pop(str(message.author.id))
                await self.config.update()

            if sent_emoji != "disable":
                try:
                    await message.add_reaction(sent_emoji)
                except (discord.HTTPException, discord.InvalidArgument):
                    pass

            return False

        now = datetime.utcnow()

        account_age = self.config["account_age"]
        guild_age = self.config["guild_age"]

        if account_age is None:
            account_age = isodate.Duration()
        if guild_age is None:
            guild_age = isodate.Duration()

        if not isinstance(account_age, isodate.Duration):
            try:
                account_age = isodate.parse_duration(account_age)
            except isodate.ISO8601Error:
                logger.warning(
                    "The account age limit needs to be a "
                    "ISO-8601 duration formatted duration string "
                    'greater than 0 days, not "%s".',
                    str(account_age),
                )
                account_age = self.config.remove("account_age")

        if not isinstance(guild_age, isodate.Duration):
            try:
                guild_age = isodate.parse_duration(guild_age)
            except isodate.ISO8601Error:
                logger.warning(
                    "The guild join age limit needs to be a "
                    "ISO-8601 duration formatted duration string "
                    'greater than 0 days, not "%s".',
                    str(guild_age),
                )
                guild_age = self.config.remove("guild_age")

        reason = self.blocked_users.get(str(message.author.id)) or ""
        min_guild_age = min_account_age = now

        try:
            min_account_age = message.author.created_at + account_age
        except ValueError:
            logger.warning("Error with 'account_age'.", exc_info=True)
            self.config.remove("account_age")

        try:
            joined_at = getattr(message.author, "joined_at", None)
            if joined_at is not None:
                min_guild_age = joined_at + guild_age
        except ValueError:
            logger.warning("Error with 'guild_age'.", exc_info=True)
            self.config.remove("guild_age")

        if min_account_age > now:
            # User account has not reached the required time
            reaction = blocked_emoji
            changed = False
            delta = human_timedelta(min_account_age)

            if str(message.author.id) not in self.blocked_users:
                new_reason = (
                    f"System Message: New Account. Required to wait for {delta}."
                )
                self.blocked_users[str(message.author.id)] = new_reason
                changed = True

            if reason.startswith("System Message: New Account.") or changed:
                await message.channel.send(
                    embed=discord.Embed(
                        title="Message not sent!",
                        description=f"Your must wait for {delta} "
                        f"before you can contact {self.user.mention}.",
                        color=discord.Color.red(),
                    )
                )

        elif min_guild_age > now:
            # User has not stayed in the guild for long enough
            reaction = blocked_emoji
            changed = False
            delta = human_timedelta(min_guild_age)

            if str(message.author.id) not in self.blocked_users:
                new_reason = (
                    f"System Message: Recently Joined. Required to wait for {delta}."
                )
                self.blocked_users[str(message.author.id)] = new_reason
                changed = True

            if reason.startswith("System Message: Recently Joined.") or changed:
                await message.channel.send(
                    embed=discord.Embed(
                        title="Message not sent!",
                        description=f"Your must wait for {delta} "
                        f"before you can contact {self.user.mention}.",
                        color=discord.Color.red(),
                    )
                )

        elif str(message.author.id) in self.blocked_users:
            reaction = blocked_emoji
            if reason.startswith("System Message: New Account.") or reason.startswith(
                "System Message: Recently Joined."
            ):
                # Met the age limit already, otherwise it would've been caught by the previous if's
                reaction = sent_emoji
                self.blocked_users.pop(str(message.author.id))
            else:
                end_time = re.search(r"%(.+?)%$", reason)
                if end_time is not None:
                    after = (
                        datetime.fromisoformat(end_time.group(1)) - now
                    ).total_seconds()
                    if after <= 0:
                        # No longer blocked
                        reaction = sent_emoji
                        self.blocked_users.pop(str(message.author.id))
        else:
            reaction = sent_emoji

        await self.config.update()
        if reaction != "disable":
            try:
                await message.add_reaction(reaction)
            except (discord.HTTPException, discord.InvalidArgument):
                pass
        return str(message.author.id) in self.blocked_users

    async def process_modmail(self, message: discord.Message) -> None:
        """Processes messages sent to the bot."""
        await self.wait_for_connected()

        blocked = await self._process_blocked(message)
        if not blocked:
            thread = await self.threads.find_or_create(message.author)
            await thread.send(message)

    async def get_context(self, message, *, cls=commands.Context):
        """
        Returns the invocation context from the message.
        Supports getting the prefix from database as well as command aliases.
        """
        await self.wait_for_connected()

        view = StringView(message.content)
        ctx = cls(prefix=None, view=view, bot=self, message=message)

        if self._skip_check(message.author.id, self.user.id):
            return ctx

        ctx.thread = await self.threads.find(channel=ctx.channel)

        prefixes = await self.get_prefix()

        invoked_prefix = discord.utils.find(view.skip_string, prefixes)
        if invoked_prefix is None:
            return ctx

        invoker = view.get_word().lower()

        # Check if there is any aliases being called.
        alias = self.aliases.get(invoker)
        if alias is not None:
            ctx._alias_invoked = True  # pylint: disable=W0212
            len_ = len(f"{invoked_prefix}{invoker}")
            view = StringView(f"{alias}{ctx.message.content[len_:]}")
            ctx.view = view
            invoker = view.get_word()

        ctx.invoked_with = invoker
        ctx.prefix = self.prefix  # Sane prefix (No mentions)
        ctx.command = self.all_commands.get(invoker)

        return ctx

    async def update_perms(
        self, name: typing.Union[PermissionLevel, str], value: int, add: bool = True
    ) -> None:
        if isinstance(name, PermissionLevel):
            permissions = self.config["level_permissions"]
            name = name.name
        else:
            permissions = self.config["command_permissions"]
        if name not in permissions:
            if add:
                permissions[name] = [value]
        else:
            if add:
                if value not in permissions[name]:
                    permissions[name].append(value)
            else:
                if value in permissions[name]:
                    permissions[name].remove(value)
        logger.info("Updating permissions for %s, %s (add=%s).", name, value, add)
        await self.config.update()

    async def on_message(self, message):
        await self.wait_for_connected()

        if message.type == discord.MessageType.pins_add and message.author == self.user:
            await message.delete()

        if message.author.bot:
            return

        if isinstance(message.channel, discord.DMChannel):
            return await self.process_modmail(message)

        prefix = self.prefix

        if message.content.startswith(prefix):
            cmd = message.content[len(prefix) :].strip()
            if cmd in self.snippets:
                thread = await self.threads.find(channel=message.channel)
                snippet = self.snippets[cmd]
                if thread:
                    snippet = snippet.format(recipient=thread.recipient)
                message.content = f"{prefix}reply {snippet}"

        ctx = await self.get_context(message)
        if ctx.command:
            return await self.invoke(ctx)

        thread = await self.threads.find(channel=ctx.channel)
        if thread is not None:
            try:
                reply_without_command = strtobool(self.config["reply_without_command"])
            except ValueError:
                reply_without_command = self.config.remove("reply_without_command")

            if reply_without_command:
                await thread.reply(message)
            else:
                await self.api.append_log(message, type_="internal")
        elif ctx.invoked_with:
            exc = commands.CommandNotFound(
                'Command "{}" is not found'.format(ctx.invoked_with)
            )
            self.dispatch("command_error", ctx, exc)

    async def on_typing(self, channel, user, _):
        await self.wait_for_connected()

        if user.bot:
            return

        async def _void(*_args, **_kwargs):
            pass

        if isinstance(channel, discord.DMChannel):
            if await self._process_blocked(
                SimpleNamespace(
                    author=user, channel=SimpleNamespace(send=_void), add_reaction=_void
                )
            ):
                return

            try:
                user_typing = strtobool(self.config["user_typing"])
            except ValueError:
                user_typing = self.config.remove("user_typing")
            if not user_typing:
                return

            thread = await self.threads.find(recipient=user)

            if thread:
                await thread.channel.trigger_typing()
        else:
            try:
                mod_typing = strtobool(self.config["mod_typing"])
            except ValueError:
                mod_typing = self.config.remove("mod_typing")
            if not mod_typing:
                return

            thread = await self.threads.find(channel=channel)
            if thread is not None and thread.recipient:
                if await self._process_blocked(
                    SimpleNamespace(
                        author=thread.recipient,
                        channel=SimpleNamespace(send=_void),
                        add_reaction=_void,
                    )
                ):
                    return
                await thread.recipient.trigger_typing()

    async def on_raw_reaction_add(self, payload):
        user = self.get_user(payload.user_id)
        if user.bot:
            return

        channel = self.get_channel(payload.channel_id)
        if not channel:  # dm channel not in internal cache
            _thread = await self.threads.find(recipient=user)
            if not _thread:
                return
            channel = await _thread.recipient.create_dm()

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return

        reaction = payload.emoji

        close_emoji = await self.convert_emoji(self.config["close_emoji"])

        if isinstance(channel, discord.DMChannel):
            if str(reaction) == str(close_emoji):  # closing thread
                thread = await self.threads.find(recipient=user)
                ts = message.embeds[0].timestamp if message.embeds else None
                if thread and ts == thread.channel.created_at:
                    # the reacted message is the corresponding thread creation embed
                    try:
                        recipient_thread_close = strtobool(
                            self.config["recipient_thread_close"]
                        )
                    except ValueError:
                        recipient_thread_close = self.config.remove(
                            "recipient_thread_close"
                        )
                    if recipient_thread_close:
                        await thread.close(closer=user)
        else:
            if not message.embeds:
                return
            message_id = str(message.embeds[0].author.url).split("/")[-1]
            if message_id.isdigit():
                thread = await self.threads.find(channel=message.channel)
                channel = thread.recipient.dm_channel
                if not channel:
                    channel = await thread.recipient.create_dm()
                async for msg in channel.history():
                    if msg.id == int(message_id):
                        await msg.add_reaction(reaction)

    async def on_guild_channel_delete(self, channel):
        if channel.guild != self.modmail_guild:
            return

        audit_logs = self.modmail_guild.audit_logs()
        entry = await audit_logs.find(lambda e: e.target.id == channel.id)
        mod = entry.user

        if mod == self.user:
            return

        if isinstance(channel, discord.CategoryChannel):
            if self.main_category.id == channel.id:
                self.config.remove("main_category_id")
                await self.config.update()
            return

        if not isinstance(channel, discord.TextChannel):
            return

        if self.log_channel is None or self.log_channel.id == channel.id:
            self.config.remove("log_channel_id")
            await self.config.update()
            return

        thread = await self.threads.find(channel=channel)
        if not thread:
            return

        await thread.close(closer=mod, silent=True, delete_channel=False)

    async def on_member_remove(self, member):
        thread = await self.threads.find(recipient=member)
        if thread:
            embed = discord.Embed(
                description="The recipient has left the server.",
                color=discord.Color.red(),
            )
            await thread.channel.send(embed=embed)

    async def on_member_join(self, member):
        thread = await self.threads.find(recipient=member)
        if thread:
            embed = discord.Embed(
                description="The recipient has joined the server.", color=self.mod_color
            )
            await thread.channel.send(embed=embed)

    async def on_message_delete(self, message):
        """Support for deleting linked messages"""
        if message.embeds and not isinstance(message.channel, discord.DMChannel):
            message_id = str(message.embeds[0].author.url).split("/")[-1]
            if message_id.isdigit():
                thread = await self.threads.find(channel=message.channel)

                channel = thread.recipient.dm_channel

                async for msg in channel.history():
                    if msg.embeds and msg.embeds[0].author:
                        url = str(msg.embeds[0].author.url)
                        if message_id == url.split("/")[-1]:
                            return await msg.delete()

    async def on_bulk_message_delete(self, messages):
        await discord.utils.async_all(self.on_message_delete(msg) for msg in messages)

    async def on_message_edit(self, before, after):
        if before.author.bot:
            return
        if isinstance(before.channel, discord.DMChannel):
            thread = await self.threads.find(recipient=before.author)
            async for msg in thread.channel.history():
                if msg.embeds:
                    embed = msg.embeds[0]
                    matches = str(embed.author.url).split("/")
                    if matches and matches[-1] == str(before.id):
                        embed.description = after.content
                        await msg.edit(embed=embed)
                        await self.api.edit_message(str(after.id), after.content)
                        break

    async def on_error(self, event_method, *args, **kwargs):
        logger.error("Ignoring exception in %s.", event_method)
        logger.error("Unexpected exception:", exc_info=sys.exc_info())

    async def on_command_error(self, context, exception):
        if isinstance(exception, commands.BadUnionArgument):
            msg = "Could not find the specified " + human_join(
                [c.__name__ for c in exception.converters]
            )
            await context.trigger_typing()
            await context.send(
                embed=discord.Embed(color=discord.Color.red(), description=msg)
            )

        elif isinstance(exception, commands.BadArgument):
            await context.trigger_typing()
            await context.send(
                embed=discord.Embed(
                    color=discord.Color.red(), description=str(exception)
                )
            )
        elif isinstance(exception, commands.CommandNotFound):
            logger.warning("CommandNotFound: %s", exception)
        elif isinstance(exception, commands.MissingRequiredArgument):
            await context.send_help(context.command)
        elif isinstance(exception, commands.CheckFailure):
            for check in context.command.checks:
                if not await check(context) and hasattr(check, "fail_msg"):
                    await context.send(
                        embed=discord.Embed(
                            color=discord.Color.red(), description=check.fail_msg
                        )
                    )
            logger.warning("CheckFailure: %s", exception)
        else:
            logger.error("Unexpected exception:", exc_info=exception)

    @staticmethod
    def overwrites(ctx: commands.Context) -> dict:
        """Permission overwrites for the guild."""
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True),
        }

        for role in ctx.guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True)
        return overwrites

    async def validate_database_connection(self):
        try:
            await self.db.command("buildinfo")
        except Exception as exc:
            logger.critical("Something went wrong while connecting to the database.")
            message = f"{type(exc).__name__}: {str(exc)}"
            logger.critical(message)

            if "ServerSelectionTimeoutError" in message:
                logger.critical(
                    "This may have been caused by not whitelisting "
                    "IPs correctly. Make sure to whitelist all "
                    "IPs (0.0.0.0/0) https://i.imgur.com/mILuQ5U.png"
                )

            if "OperationFailure" in message:
                logger.critical(
                    "This is due to having invalid credentials in your MONGO_URI."
                )
                logger.critical(
                    "Recheck the username/password and make sure to url encode them. "
                    "https://www.urlencoder.io/"
                )
            raise
        else:
            logger.info("Successfully connected to the database.")

    async def post_metadata(self):
        owner = (await self.application_info()).owner
        data = {
            "owner_name": str(owner),
            "owner_id": owner.id,
            "bot_id": self.user.id,
            "bot_name": str(self.user),
            "avatar_url": str(self.user.avatar_url),
            "guild_id": self.guild_id,
            "guild_name": self.guild.name,
            "member_count": len(self.guild.members),
            "uptime": (datetime.utcnow() - self.start_time).total_seconds(),
            "latency": f"{self.ws.latency * 1000:.4f}",
            "version": self.version,
            "selfhosted": True,
            "last_updated": str(datetime.utcnow()),
        }

        async with self.session.post("https://api.modmail.tk/metadata", json=data):
            logger.debug("Uploading metadata to Modmail server.")

    async def before_post_metadata(self):
        logger.info("Starting metadata loop.")
        await self.wait_for_connected()
        if not self.guild:
            self.metadata_loop.cancel()

    async def after_post_metadata(self):
        logger.info("Metadata loop has been cancelled.")


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass

    bot = ModmailBot()
    bot.run()
