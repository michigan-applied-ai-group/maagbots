"""The MAAG Discord bot. Run it with: python bot.py

One bot account speaks for every agent in agents/. Each agent posts through a
Discord webhook, which lets a single message use any name and avatar. That's
how 15 personas can share one bot instead of needing 15 bot accounts.
"""

import logging
import os
import re
import time

import anthropic
import discord
from dotenv import load_dotenv

from loader import Agent, load_agents

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("maagbots")

MODEL = "claude-opus-5"
HISTORY_LIMIT = 20          # how many recent messages an agent reads before replying
MAX_HOPS = 5            # agent-to-agent chains stop after this many replies
COOLDOWN_SECONDS = 60       # after an agent replies, the channel rests this long
QUIET_SECONDS = 30 * 60     # how long `!quiet` silences every agent
DISCORD_MAX_LENGTH = 2000   # Discord rejects longer messages
AMNESIA_MARKER = "🧠💨 The agents have forgotten everything above this line."

# Discord only sends message text to bots that ask for the "message content"
# intent. It also has to be switched on in the Developer Portal.
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
claude = anthropic.AsyncAnthropic()

agents: list[Agent] = []
webhooks: dict[int, discord.Webhook] = {}   # channel id -> our webhook there
chain_hop: dict[int, int] = {}              # channel id -> hop count of the last agent reply there
cooldown_until: dict[int, float] = {}       # channel id -> time the cooldown ends
quiet_until: dict[int, float] = {}          # channel id -> time `!quiet` ends
roles: dict[str, discord.Role] = {}         # agent handle -> its role (the club has one server)


def handle_of(agent: Agent) -> str:
    """An agent's handle is its filename: agents/skeptic.md is @skeptic."""
    return agent.source.removesuffix(".md").lower()


def roster_entry(agent: Agent) -> str:
    """How an agent is introduced to the other agents: handle, description, examples."""
    entry = f"@{handle_of(agent)}: {agent.description or '(no description)'}"
    for example in agent.examples:
        entry += f'\n  e.g. "{example}"'
    return entry


def pick_agent(message: discord.Message, channel_name: str) -> Agent | None:
    """Return the one agent that should answer this message, or None."""
    text = message.content.lower()
    for agent in agents:
        if channel_name not in agent.channels:
            continue
        if agent.name == message.author.name:
            continue  # an agent never answers itself
        # Personas aren't real Discord users, so they can't be @-mentioned the
        # normal way. Each agent gets a Discord role with its handle instead,
        # so it shows up when someone types "@". Picking the role from that
        # list and typing "@skeptic" as plain text both count as a mention.
        handle = handle_of(agent)
        role = roles.get(handle)
        picked_role = role is not None and role.mention in message.content
        typed_handle = re.search(rf"@{re.escape(handle)}\b", text)
        if agent.trigger == "mention" and (picked_role or typed_handle):
            return agent
    return None


async def continuing_agent(message: discord.Message, channel_name: str) -> Agent | None:
    """Find the agent a thread follow-up is meant for, if any.

    In a thread, the person who started it can keep talking to the agent who
    spoke last, without typing an @mention. Anyone else still has to mention
    an agent, so a thread full of people chatting doesn't trigger a reply to
    every message. Both facts are read from Discord itself, so this still
    works after the bot restarts.
    """
    thread = message.channel
    try:
        starter = await thread.parent.fetch_message(thread.id)
    except discord.HTTPException:
        return None  # this thread wasn't started from a message
    if starter.author.id != message.author.id:
        return None  # only the person who started the thread

    async for earlier in thread.history(limit=HISTORY_LIMIT, before=message):
        if earlier.webhook_id:  # agents post through webhooks
            for agent in agents:
                if agent.name == earlier.author.name and channel_name in agent.channels:
                    return agent
            return None  # the last agent to speak has since been removed
    return None


async def create_roles(guild: discord.Guild):
    """Make sure every agent has a mentionable role named after its handle.

    Nobody is ever given these roles, so mentioning one notifies no one.
    """
    existing = {role.name: role for role in guild.roles}
    for agent in agents:
        handle = handle_of(agent)
        try:
            role = existing.get(handle) or await guild.create_role(
                name=handle, mentionable=True, reason=f"maagbots agent {agent.name}"
            )
            if not role.mentionable:
                await role.edit(mentionable=True)
        except discord.Forbidden:
            log.error("Can't create the @%s role. Give the bot the Manage Roles permission.", handle)
            continue
        roles[handle] = role


async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Find this bot's webhook in the channel, creating it the first time."""
    if channel.id not in webhooks:
        existing = [w for w in await channel.webhooks() if w.user == client.user]
        webhooks[channel.id] = existing[0] if existing else await channel.create_webhook(name="maagbots")
    return webhooks[channel.id]


async def write_reply(agent: Agent, message: discord.Message, channel_name: str) -> str | None:
    """Ask Claude to reply as the agent. Returns None if there's nothing to say."""
    # Agents don't store memories. Their memory is whatever they read here.
    # Reading stops at the bot's last !amnesia marker, if there is one.
    history = []
    forgotten = False
    async for m in message.channel.history(limit=HISTORY_LIMIT):
        if m.author == client.user and m.content == AMNESIA_MARKER:
            forgotten = True
            break
        history.append(m)

    # A thread's history leaves out the message the thread was started from,
    # which is usually the idea everyone is discussing. Fetch it separately.
    if isinstance(message.channel, discord.Thread) and not forgotten and len(history) < HISTORY_LIMIT:
        try:
            history.append(await message.channel.parent.fetch_message(message.channel.id))
        except discord.HTTPException:
            pass  # this thread wasn't started from a message
    # clean_content turns role mentions like <@&1234> back into "@skeptic".
    transcript = "\n".join(
        f"{m.author.display_name}: {m.clean_content}" for m in reversed(history)
    )
    # Every agent is told who else is in the channel, using each agent's
    # description. This is how a router agent knows where to send a question,
    # so a vague description means your agent never gets picked.
    roster = "\n".join(
        roster_entry(other)
        for other in agents
        if other is not agent and channel_name in other.channels
    )
    prompt = (
        f"Other agents in #{channel_name}. Only tag one, by writing its @handle, "
        f"if your instructions tell you to:\n\n{roster or '(none)'}\n\n"
        f"Recent messages in #{channel_name}, oldest first:\n\n"
        f"{transcript}\n\n"
        f"The last message, from {message.author.display_name}, is addressed to you. "
        "Write your reply. Just the reply, no name prefix."
    )

    try:
        # If a safety check declines the request, `fallbacks` lets the API
        # retry it on another model instead of returning nothing.
        response = await claude.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=agent.system_prompt,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIError as e:
        log.error("Claude API error for %s: %s", agent.name, e)
        return None

    if response.stop_reason == "refusal":
        log.warning("%s declined to reply to message %s", agent.name, message.id)
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text[:DISCORD_MAX_LENGTH] or None


@client.event
async def on_ready():
    for guild in client.guilds:
        await create_roles(guild)
    log.info("Logged in as %s with %d agent(s) and %d role(s)", client.user, len(agents), len(roles))


@client.event
async def on_message(message: discord.Message):
    # Agents listen in text channels and in threads inside them. `channel` is
    # always the text channel: it decides which agents listen, and it's where
    # the webhook, the cooldown, and !quiet live.
    if isinstance(message.channel, discord.TextChannel):
        channel = message.channel
    elif isinstance(message.channel, discord.Thread) and isinstance(message.channel.parent, discord.TextChannel):
        channel = message.channel.parent
    else:
        return  # ignore DMs, forum posts, and everything else
    now = time.monotonic()

    # The kill switch. Anyone can use it, no permissions check.
    if message.content.strip() == "!quiet":
        quiet_until[channel.id] = now + QUIET_SECONDS
        await message.channel.send("🤫 The agents will stay quiet for 30 minutes.")
        return

    # Wipe the agents' memory of this channel or thread. Nothing is deleted:
    # the bot posts a marker, and agents never read past it.
    if message.content.strip() == "!amnesia":
        await message.channel.send(AMNESIA_MARKER)
        return

    # Work out how deep into an agent-to-agent chain this message is.
    # A human message is hop 0. Messages from other bots are ignored.
    our_webhook_ids = {w.id for w in webhooks.values()}
    if message.webhook_id in our_webhook_ids:
        hop = chain_hop.get(message.channel.id, 0)
    elif message.author.bot:
        return
    else:
        hop = 0

    if now < quiet_until.get(channel.id, 0):
        return
    if hop >= MAX_HOPS:
        return  # the chain has gone far enough

    # The cooldown only guards the main channel, where new threads start.
    # Inside a thread, people are already in a conversation with the agents.
    in_thread = isinstance(message.channel, discord.Thread)
    if hop == 0 and not in_thread and now < cooldown_until.get(channel.id, 0):
        return  # cooling down: stay silent rather than queueing

    agent = pick_agent(message, channel.name)
    if agent is None and hop == 0 and in_thread:
        agent = await continuing_agent(message, channel.name)
    if agent is None:
        return

    # Start the cooldown now, so a second message arriving while Claude is
    # thinking doesn't get its own reply.
    if not in_thread:
        cooldown_until[channel.id] = now + COOLDOWN_SECONDS

    reply = await write_reply(agent, message, channel.name)
    if reply is None:
        return

    # Each conversation lives in its own thread. A message in the main channel
    # gets a new thread started on it; a message already in a thread is
    # answered there.
    if isinstance(message.channel, discord.Thread):
        thread = message.channel
    else:
        thread = await message.create_thread(name=message.clean_content[:100])

    # When an agent writes "@judge", swap in the real role mention so it's
    # highlighted like any other mention.
    for handle, role in roles.items():
        reply = re.sub(rf"@{re.escape(handle)}\b", role.mention, reply, flags=re.IGNORECASE)

    # Record the hop count *before* sending. Discord can deliver our own message
    # back to on_message before send() even returns, and it needs the count.
    webhook = await get_webhook(channel)
    chain_hop[thread.id] = hop + 1
    await webhook.send(
        reply,
        username=agent.name,
        avatar_url=agent.avatar or None,
        thread=thread,
        allowed_mentions=discord.AllowedMentions.none(),  # agents never ping anyone
    )


if __name__ == "__main__":
    agents = load_agents()
    client.run(os.environ["DISCORD_TOKEN"], log_handler=None)
