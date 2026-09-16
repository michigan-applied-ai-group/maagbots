"""Reads every agent file in agents/ and turns it into an Agent.

Each agent is a markdown file: YAML frontmatter between two `---` lines, then
the system prompt. A broken file is logged and skipped, so one bad PR can't
take down every other agent.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent / "agents"
TRIGGERS = {"mention", "keyword", "unanswered_question", "scheduled"}


@dataclass
class Agent:
    name: str
    owner: str
    channels: list[str]
    trigger: str
    system_prompt: str
    description: str = ""  # one line saying what the agent is for; routers read it
    examples: list[str] = field(default_factory=list)  # sample requests it handles well
    avatar: str = ""
    enabled: bool = True
    source: str = ""  # which file this agent came from, for error messages


class AgentFileError(Exception):
    """Raised when an agent file breaks the contract. The message says how."""


def parse_agent_file(path: Path) -> Agent:
    text = path.read_text(encoding="utf-8")

    # The file must start with a `---` line, and a second `---` line ends the
    # frontmatter. Everything after that is the system prompt.
    parts = text.split("\n---", 1)
    if not text.startswith("---") or len(parts) < 2:
        raise AgentFileError("frontmatter must start and end with a '---' line")
    frontmatter = parts[0].removeprefix("---")
    system_prompt = parts[1].split("\n", 1)[-1].strip()

    try:
        meta = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as e:
        raise AgentFileError(f"frontmatter isn't valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise AgentFileError("frontmatter should be 'key: value' lines")

    for field in ("name", "owner", "channels", "trigger"):
        if not meta.get(field):
            raise AgentFileError(f"missing required field '{field}'")
    if not isinstance(meta["channels"], list):
        raise AgentFileError("'channels' should be a list, like [ideas]")
    if not isinstance(meta.get("examples", []), list):
        raise AgentFileError(
            "'examples' should be a list, with one '- \"example request\"' line per example"
        )
    if meta["trigger"] not in TRIGGERS:
        raise AgentFileError(
            f"'trigger' is '{meta['trigger']}', but must be one of: "
            + ", ".join(sorted(TRIGGERS))
        )
    if not system_prompt:
        raise AgentFileError("there's no system prompt below the frontmatter")

    return Agent(
        name=str(meta["name"]),
        owner=str(meta["owner"]),
        channels=[str(c) for c in meta["channels"]],
        trigger=meta["trigger"],
        system_prompt=system_prompt,
        description=str(meta.get("description") or "").strip(),
        examples=[str(e).strip() for e in meta.get("examples") or []],
        avatar=str(meta.get("avatar") or ""),
        enabled=bool(meta.get("enabled", True)),
        source=path.name,
    )


def load_agents(directory: Path = AGENTS_DIR) -> list[Agent]:
    """Load every enabled agent. Bad files are logged and skipped."""
    agents = []
    for path in sorted(directory.glob("*.md")):
        try:
            agent = parse_agent_file(path)
        except Exception as e:
            log.error("Skipping agents/%s: %s", path.name, e)
            continue
        if agent.enabled:
            agents.append(agent)
            log.info("Loaded %s from agents/%s", agent.name, path.name)
            if not agent.description:
                log.warning(
                    "agents/%s has no 'description', so a router agent can't "
                    "know when to send questions to it",
                    path.name,
                )
        else:
            log.info("agents/%s is disabled, skipping", path.name)
    return agents
