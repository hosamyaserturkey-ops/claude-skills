# claude-skills

Personal collection of [Claude Code](https://claude.com/claude-code) skills.

## Skills

### `x-trader-scraper/`

Finds X (Twitter) users interested in prop firms / crypto trading via the Apify
tweet-scraper API and exports them to an Excel spreadsheet (handle, profile
URL, follower count, bio, verified status).

See [`x-trader-scraper/SKILL.md`](x-trader-scraper/SKILL.md) for setup and
usage.

## Using these skills

Either:

1. **Project-scoped**: copy the skill folder into `.claude/skills/<name>/` in
   any repo where you want it available.
2. **User-scoped**: copy the skill folder into `~/.claude/skills/<name>/` to
   make it available across all your Claude Code sessions.

Claude Code auto-discovers skills based on the YAML frontmatter at the top of
each `SKILL.md` and invokes them when a user's request matches the
description.
