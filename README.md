# OpenReview AgentKit

Use your agent to read OpenReview reviews, draft author responses, and submit replies you approve.

## Install

**Claude Code or Codex**

```bash
npx skills add https://github.com/Yubel426/openreview-agentkit --skill openreview-author --agent claude-code codex --global
```

Keep only the agent you use after `--agent`. Live use requires Python 3.9 or later and access to the OpenReview API.

**Claude.ai or ChatGPT**

From a checkout of this repo, run `python3 scripts/package_skill.py`. Upload `dist/openreview-author.zip` under **Customize → Skills** in Claude or **Plugins → Skills** in ChatGPT if your workspace allows uploads.

## Use

Select `/openreview-author` in Claude Code or `$openreview-author` in Codex. Try one of these prompts:

- “Read and summarize the reviews at [OpenReview forum URL].”
- “Draft a reply to Reviewer 2 at [OpenReview forum URL]. Show me the reply and who can see it before submitting.”

If the agent asks you to sign in, run the login command it gives you in your own terminal. Approve the exact reply before it is posted. If your hosted chat cannot reach OpenReview, provide review text you are allowed to share for drafting.
