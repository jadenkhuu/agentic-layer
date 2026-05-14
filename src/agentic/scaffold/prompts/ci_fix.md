# CI fix loop — attempt {{TASK}}

CI is failing on the PR opened by this run. Your job is to fix the
failure and push to the same branch — re-running CI.

The first line of `{{TASK}}` says which attempt you're on. The rest is
the raw failure output from `gh pr checks`.

## Approach

1. Read the failure output. Identify the smallest change that addresses
   the root cause (not a symptom).
2. Make the fix in code. Run the relevant tests locally first.
3. Commit with a message that names the failure (e.g.
   `fix: handle empty manifest in deploy`).
4. `git push` to the same branch — CI re-runs automatically.

Write a short note about what you fixed to `{{OUTPUT}}` (≤ 200 words).
This will be appended to the PR comments by the watcher.
