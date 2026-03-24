# codex-review

![codex-review banner](banner.png)

**Automatic code review for Claude Code, powered by OpenAI Codex CLI.**

Every time Claude writes a plan or changes code, Codex reviews it as a second opinion — like having a senior engineer look over Claude's shoulder.

> There are other Codex review plugins out there ([claude-review-loop](https://github.com/hamelsmu/claude-review-loop), [agent-peer-review](https://github.com/jcputney/agent-peer-review), [codex-skill](https://github.com/cathrynlavery/codex-skill)). What makes this one different is **multi-round review tracking** — when you fix issues and re-review, Codex sees its own previous findings and verifies they were actually addressed. This was battle-tested across hundreds of reviews on a production codebase before being extracted into a plugin.

## Why this exists

Most review plugins fire once and forget. In practice, the real value is in the **review loop**:

```
Round 1: Codex finds 6 issues
    → You fix 4 of them
Round 2: Codex sees its previous findings + your fixes
    → Confirms 4 addressed, flags 2 remaining + 1 new regression
Round 3: Clean
```

Without round tracking, round 2 starts from scratch — it doesn't know what it already found. This plugin tracks review scope (which files), round number, and archives previous findings so Codex can verify its own work was addressed.

## Features

- **Multi-round review tracking** — Scope-hashed rounds with previous findings fed back to Codex
- **Durable review history** — Findings archived to `.claude/reviews/` (survives between sessions)
- **Three review modes** — Plan review, implementation review, and PR review
- **Summary preview** — First 3 lines of findings shown in hook context (Claude sees the headline immediately)
- **Circuit breaker** — Blocks session exit if review pending (allows override on second attempt)
- **Configurable** — Model, reasoning effort, file paths, extensions — all via `.codex-review.json`
- **"Skip codex"** — Say it to bypass the review gate when you need to move fast

## How it compares

| Feature | Other plugins | codex-review |
|---------|--------------|--------------|
| Auto-trigger on file changes | Yes | Yes |
| Plan review | Some | Yes |
| Implementation review | Some | Yes — targeted (≤5 files) or diff-based (>5 files) |
| PR review | No | Yes — `gh pr diff` integration |
| Multi-round tracking | **No** | **Yes — scope-hashed, feeds previous findings back** |
| Review history | `/tmp` (lost) | `.claude/reviews/` (durable, versioned) |
| Summary preview | No | Yes — first 3 lines in context |
| Configurable model | Basic | `gpt-5.4` default, 6 reasoning effort levels |
| Project config file | No | `.codex-review.json` with full customization |
| Circuit breaker | Some | Yes — block once, allow on retry |

## How it works

```
You write code with Claude Code
        |
        v
Hook detects file changes ───> Marks review as pending
        |
        v
Claude runs: codex exec --full-auto "Review these changes..."
        |
        v
Codex reviews independently ───> Returns findings
        |
        v
Claude evaluates findings ───> Fixes issues or explains why not
        |
        v
Review cleared ───> You can continue or exit
```

## Prerequisites

1. **Claude Code** — [Install Claude Code](https://claude.ai/code)
2. **Codex CLI** — OpenAI's coding agent

```bash
npm install -g @openai/codex
```

3. **OpenAI API key** — Set in your environment

```bash
export OPENAI_API_KEY=sk-...
```

## Installation

```bash
# Add this marketplace
/plugin marketplace add https://github.com/hugotomita1201/codex-review

# Install the plugin
/plugin install codex-review
```

Or manually clone into your plugins directory:

```bash
git clone https://github.com/hugotomita1201/codex-review ~/.claude/plugins/codex-review
```

## Usage

### Automatic (default)

Just code normally. When you change files, the hook will:

1. Detect the change
2. Classify it (plan vs code)
3. Tell Claude to run a Codex review
4. Claude runs `codex exec` with the appropriate review prompt
5. Claude reads the output and addresses findings

### Manual

Type `/codex-review` in Claude Code to trigger a review manually.

### Skip review

Say **"skip codex"** in your message to bypass the review gate for the current task.

## Configuration

Create `.codex-review.json` in your project root to customize behavior:

```json
{
  "planPaths": [".claude/plans/"],
  "ignorePaths": [".claude/", ".git/", "node_modules/", "dist/"],
  "codeExtensions": [".js", ".jsx", ".ts", ".tsx", ".py", ".go", ".rs"],
  "promptPaths": ["/prompts/"],
  "timeout": 120,
  "autoReview": true,
  "circuitBreaker": true,
  "model": "gpt-5.4",
  "reasoningEffort": "xhigh"
}
```

All fields are optional — sensible defaults are used for anything not specified.

| Field | Default | Description |
|-------|---------|-------------|
| `planPaths` | `[".claude/plans/"]` | Directories containing plan files |
| `ignorePaths` | `[".claude/", ".git/", "node_modules/", ...]` | Paths to ignore |
| `codeExtensions` | 30+ extensions | File extensions to track |
| `promptPaths` | `["/prompts/"]` | Paths containing prompt files (treated as code) |
| `timeout` | `120` | Codex execution timeout in seconds |
| `autoReview` | `true` | Automatically trigger reviews on file changes |
| `circuitBreaker` | `true` | Block session exit if review pending |
| `model` | `"gpt-5.4"` | Which model Codex uses (gpt-5.4, gpt-5.1-codex, gpt-4.1, o4-mini) |
| `reasoningEffort` | `"xhigh"` | Reasoning effort level (none, minimal, low, medium, high, xhigh) |

## Review modes

### Plan review

Triggered when files in `.claude/plans/` change. Codex checks for:
- Wrong assumptions
- Missing edge cases
- Integration gaps
- Operational risks

### Implementation review

Triggered when code files change. Two strategies:

- **5 or fewer files** — Targeted review: Codex reads current file content directly
- **More than 5 files** — Scoped diff: Codex reviews `git diff` output

Codex checks for:
- Correctness bugs
- Integration mismatches
- Race conditions
- Error handling gaps
- Security issues

## State file

The plugin stores review state at `.claude/reviews/status.json` in your project. Add this to your `.gitignore`:

```
.claude/reviews/
```

## Circuit breaker

When you try to exit Claude Code with pending reviews:

1. **First attempt** — Blocked with message: "Codex review still pending"
2. **Second attempt** — Allowed through (you're the boss)

Say **"skip codex"** to bypass immediately.

## How Codex connects

Codex CLI is a separate coding agent from OpenAI. It runs locally on your machine and:

1. Reads your project files (within the sandbox)
2. Analyzes code independently from Claude
3. Returns findings as structured text

Claude and Codex never share context — Codex gets a fresh view every time, which is what makes it valuable as a second opinion.

**Required setup:**
```bash
# Install Codex CLI
npm install -g @openai/codex

# Set your OpenAI API key
export OPENAI_API_KEY=sk-your-key-here

# Verify it works
codex --version
```

## License

MIT
