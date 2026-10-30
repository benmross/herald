# Session context

Herald adds its rules to the selected first-party CLI; it does not own the
entire request. Separate harness content, shared operating rules, personal
constraints, session facts and task-specific retrieval when auditing size.

## Assembly

| Layer | Claude Code | Codex |
|---|---|---|
| Harness | Native system prompt, tools and runtime reminders | Native system/developer instructions, tools and runtime reminders |
| Operating rules | Generated `CLAUDE.md` | `AGENTS.md`, or generated `CLAUDE.md` through the configured fallback |
| Session card | First user input, after static rules and Telegram preamble | Same |
| Memory outside Herald | Claude's auto-memory index, when enabled | Does not inherit Claude's auto-memory index |
| User turn | Telegram wrapper, local sent time, changes and request | Same |

`lib/herald/constitution.py` concatenates `config/constitution.md`, the install
mode and `ledger/identity/constitution.md`. `herald constitution` regenerates
`CLAUDE.md`. Keep rules engine-independent; use adapters for loading and tool
mechanics. Do not remove Herald's authorization or privacy rules just because a
harness currently contains a similar generic instruction.

`bin/herald-telegram:orientation_for` builds the card on a new session;
`session_input` places it after the static Telegram preamble and before the
request, in the first user input. The card includes current facts, not schema inventories,
SQL examples, personal histories or general instructions. Per-turn changes carry
new evidence separately. Direct sessions without an injected card may read
`ledger/state/orientation.md` once. An old card is not current evidence.

On resume, the first input remains in the engine transcript; the card is not
regenerated or appended again. Legacy sessions keep passing their original card
through Claude's append-system-prompt or Codex's developer instructions, preserving
their original prefix. A missing-transcript retry starts with a fresh user-input
card. No stored-data migration is needed: the existing nullable orientation field
continues to mean a system card; new sessions leave it null.

Both providers cache matching prefixes. Moving session-specific facts after
static rules increases the reusable prefix across fresh sessions. It does not
guarantee a cache hit: the first-party CLI controls cache boundaries and routing.
Measure cached-input counts before claiming an improvement. Anthropic's cache
hierarchy is tools, system, messages, regardless of capture display order.
See [OpenAI caching](https://developers.openai.com/api/docs/guides/prompt-caching)
and [Anthropic caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

When enabled, Claude loads its own `MEMORY.md` index automatically; Herald does
not copy it into the prompt. A user's install may disable it after reconciling
unique knowledge into the ledger.

## Size and ownership

Shared startup rules contain authorization boundaries, privacy, evidence
standards and retrieval entry points. Personal startup rules contain essential
medical/institutional restrictions, standing exceptions and communication
preferences. Put procedures in skills, background in notes, and incident
histories and rationale in journals or git.

Codex's native instruction-file budget defaults to 32 KiB. A long generated
constitution can lose its personal section at the end. Both Herald Codex
transports set `project_doc_max_bytes=65536` as a read ceiling, not a target.
Keep actual instructions well below it. Global and nested instructions also
matter; a higher ceiling is no substitute for inspecting a real loaded prompt.
See [official instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

The harness generates tool schemas, but Claude's supported `--tools` flag can
select its built-in tool set. `--allowedTools` controls permissions and is not
the equivalent selector. MCP and plugin configuration can add context separately.
Native `--system-prompt` can replace Claude's default system prompt, but doing
so also removes its default guidance. Prefer reducing Herald-owned duplication
first. See [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

Claude auto-memory is a separate store. Before disabling it for Herald, preserve
any unique facts and restrictions in the ledger. Use a per-project or
per-invocation setting rather than disabling it for the user's unrelated work.
See [Claude memory documentation](https://code.claude.com/docs/en/memory).

## Evaluation

Record serialized bytes and word counts separately from model tokens. Pretty
printed captures include JSON syntax and escaping. A token approximation is not
an exact tokenizer measurement, a quota calculation or a latency benchmark.

Compare fresh-session captures on both engines after changes. Check that the
personal rules arrive in full, no duplicate constitution is injected, source
warnings survive, and a quiet session does not read every identity file. Test
retrieval and action boundaries, not whether an answer repeats the prompt.

Escalation was removed: cycles, setup and `herald think` use the chosen model
and effort. `--escalate` and the `think(escalate=...)` argument no longer exist.
Old escalation configuration keys are inert; historical migrations retain their
original upgrade behavior. Extensions calling the removed argument must remove it.
