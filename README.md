# OpenReview AgentKit

Use your agent to read OpenReview reviews, draft author responses, and submit replies you approve.

## Install

**Claude Code or Codex**

```bash
npx skills add https://github.com/Yubel426/openreview-agentkit
```

Live use requires Python 3.9 or later and access to the OpenReview API.

## Use

Try one of these prompts:

- “List my OpenReview submissions.”
- “Read and summarize the reviews at [OpenReview forum URL].”
- “Draft a reply to Reviewer 2 at [OpenReview forum URL]. Show me the reply and who can see it before submitting.”

If the agent asks you to sign in, run the login command it gives you in your own terminal. Approve the exact reply before it is posted.
