# CONTEXT.md — maagbots

Project brief for the Discord agent bot. Read this first.

## What this is

**maagbots** — the Discord agents built by **MAAG (Michigan Applied AI Group)**,
a ~15-person student AI interest group at the University of Michigan School of
Information. The group meets informally on Monday nights and wants to build
something collaboratively. Not everyone in the group codes.

MAAG is student-run. It is not affiliated with or endorsed by the University of
Michigan or UMSI. Keep this line in the README.

The bot hosts **multiple agent personas** in one Discord server. Students add
their own agents over time — that is the point of the project, not a side
feature.

## Design goals, in priority order

1. **Adding an agent must be easy enough for a non-coder.** Adding an agent =
   adding one markdown file and opening a PR. No Python required to contribute a
   persona. If a design choice makes the codebase cleaner but makes contributing
   harder, choose the contributor.
2. **The code will be read by students.** Favor obvious over clever. Comment the
   Discord-specific parts, which are the unfamiliar bits for most of them.
3. **Agents should be able to talk to each other** — one proposes, another
   critiques. This is the thing that makes it feel agentic rather than a
   chatbot. It is also the thing most likely to blow up the channel, so see
   "Rate limiting" below.
4. Features come last. A boring bot with a clean `agents/` contract is a
   success; a clever bot nobody can extend is not.

## Architecture

- **One Python process.** `discord.py`, Claude API via the `anthropic` SDK.
- **Personas are posted through Discord webhooks**, not separate bot
  registrations. One webhook per channel, reused; each message overrides
  `username` and `avatar_url` per persona. This is how 15 agents exist without
  15 apps in the Developer Portal.
- Requires the **Message Content** privileged intent (enable in the Developer
  Portal — without it, message content arrives empty).
- Bot permissions needed: Read Messages / View Channels, Send Messages, Manage
  Webhooks, Read Message History.
- Runs locally on the maintainer's laptop for now. Don't add deployment
  infrastructure unless asked.
- Secrets in `.env`, loaded with `python-dotenv`. `.env` is gitignored;
  `.env.example` is committed.

## The `agents/` contract

Each agent is one markdown file in `agents/`, with YAML frontmatter and the
system prompt as the body:

```markdown
---
name: The Skeptic
avatar: https://example.com/skeptic.png
owner: "@discord-handle"
channels: [ideas]
trigger: mention | keyword | unanswered_question | scheduled
enabled: true
---

You are the club's resident skeptic. When someone posts a plan, you find the
assumption it rests on and name it. You are never mean and never vague...
```

Loader reads the whole directory at startup. Bad frontmatter should log a clear
error naming the file and keep the other agents running — a student's broken PR
must not take the bot down.

**Every agent has an `owner`.** Unowned agents rot; owned ones get maintained.

## Rate limiting (non-negotiable)

A channel where four agents reply to every message dies within a week.

- Max **one** agent reply per human message.
- Agent-to-agent chains capped at **2 hops**, then the chain stops.
- Per-channel cooldown (start at 60s) — while cooling down, agents stay silent
  rather than queueing.
- A kill switch any member can use. `!quiet` for 30 minutes, no permissions
  check.

## The two starter agents

Seed these, then hand the pattern to the students.

- **Utility agent.** Catches people up on a busy channel (summarize the last N
  messages, attributed to who said what), and answers questions that have sat
  unanswered for ~10 minutes. Establishes that the bots are worth having.
- **Personality agent — "The Skeptic".** Critiques plans posted in `#ideas`.
  Strong voice. This is the one that makes students want to build their own.

Club vibe to write toward: a secret club that obviously isn't secret. Mysterious
but comical. Serious about the work, light about itself.

## Out of scope for now

Slash commands, a database, a web dashboard, hosting, per-user memory. Ask
before adding any of these.
