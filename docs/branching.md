# Branching convention — stacked branches

The build plan is strictly sequential: Step 4 cannot work without Step 3's
cleaning module, Step 7 cannot work without Step 6's database. So branches
are **stacked** — each step branches off the previous step, not off `main`.

```
main
 └── step-01-skeleton
      └── step-02-data
           └── step-03-exploration
                └── step-04-cleaning
                     └── step-05-tests
                          └── step-06-database
                               └── step-07-queries
                                    └── step-08-dashboard
                                         └── step-09-deploy
                                              └── step-10-readme
```

Phase 2 (Azure) stacks the same way off `main` once Phase 1 has merged:
`azure-01-account` → `azure-02-guardrails` → … → `azure-12-runbook`.

## Why stacked and not parallel

Parallel feature branches off `main` are the right default when work is
independent. Here it isn't. If `step-04-cleaning` branched off `main`, it
would not contain the notebook findings from Step 3 that define its own
spec, and every PR would show conflicts against work that logically came
first.

Stacking costs one thing: when a lower branch changes, everything above it
must be rebased. `--update-refs` makes that one command — see below.

## Starting a step

```bash
git checkout step-03-exploration          # the step you just finished
git pull                                  # make sure it is current
git checkout -b step-04-cleaning          # branch off it, not off main
```

## Opening the PR

The base branch is the previous step, **not** `main`:

```bash
git push -u origin step-04-cleaning
gh pr create --base step-03-exploration --head step-04-cleaning \
  --title "Step 4 — Cleaning module" \
  --body "Implements plan Step 4. Depends on #<PR number for step 3>."
```

Always name the dependency in the PR body. Reviewers need to know which
diff to read first, and future-you needs to know why the base looks odd.

## Restacking after a change lower down

The one real cost of stacking. If you amend `step-03-exploration` after
`step-04-cleaning` already branched off it:

```bash
git checkout step-10-readme               # the TOP of the stack
git rebase --update-refs step-03-exploration
git push --force-with-lease --all origin
```

`--update-refs` (Git 2.38+) moves every intermediate branch pointer in the
stack, so you rebase once instead of ten times.

Use `--force-with-lease`, never plain `--force`. It refuses to overwrite
commits you have not seen, which is the difference between rewriting your
own history and destroying someone else's.

## Merging

Merge bottom-up, one at a time: `step-01` into `main` first, then `step-02`,
and so on. GitHub automatically retargets a PR's base when that base is
merged and deleted, so the stack collapses toward `main` on its own as you
go.

Do not squash-merge a branch that has children still stacked on it. Squashing
rewrites the commit the children are based on, and every child will show
phantom conflicts. Use a merge commit or rebase-merge until the stack above
is gone.

## Rules

- `main` always works. Every merge into it is a step that met its plan
  acceptance criterion.
- One plan step per branch. If a step turns out to be two things, split the
  plan first, not the branch.
- Branch names are `step-NN-slug`, zero-padded, so they sort correctly.
- Never branch off `main` mid-stack. That silently drops the work below and
  is the main way this convention gets broken by accident.
