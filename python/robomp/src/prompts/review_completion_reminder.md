You ended your turn before finishing the PR review.

PR: {{repo.full_name}}#{{issue.number}} — {{issue.title}}
Review workspace: `{{workspace.branch}}`

You already started the review, but you did NOT reach the terminal action.
The acceptable terminal actions for an incoming PR review are exactly one of:

1. `submit_pr_review` — submit the batched review summary. After successful
   `prepare_pr_review`, it generates inline comments from PR review payload state, not
   staged comments. Use `event="APPROVE"`, `event="REQUEST_CHANGES"`, or a justified
   `event="COMMENT"`.
2. `abort_task` — unrecoverable environment failure.

Review your candidate findings, TodoList, and prior tool calls, then continue from where you stopped. Do NOT re-classify unless the earlier classify call failed. If the helper reported `delegation_required: True` and `delegate_pr_review` has not run, call it before validation. If `prepare_pr_review` succeeded and `validate_pr_review` has not run, validate findings before submit. If validation ran, call `submit_pr_review`; do not stage comments first. If PR review helper was unavailable and you already staged COMMENT-only fallback comments, call `submit_pr_review` now. If you found no inline issues, call `submit_pr_review` with `APPROVE`, except for self-authored PRs where GitHub requires `COMMENT`.

You MUST end this turn by calling one of the two terminal tools listed above.
