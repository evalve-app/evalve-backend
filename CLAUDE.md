## Agent skills

### Issue tracker

Work lives in Shortcut, workspace `team-4-1`: epic → story (the spec) → sub-tasks (the tickets). Use the Shortcut MCP connector, not GitHub issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

The glossary and the ADRs of Evalve are in the repo `evalve-deploy`, not here: `../evalve-deploy/GLOSSARY.md` and `../evalve-deploy/docs/adr/`. Read them before work on a story that names a term or an ADR. See `docs/agents/domain.md`.

### Tests and the OpenAPI file

The commands are in `README.md`, section "Tests". Regenerate `openapi/evalve-backend.json` after a change to a route or to a model of the API.

### Git

Stage files by name, after you read `git status`. A different session can leave changes in the working tree.

Name the branch `<name>/sc-<id>/<short-slug>` and put `[sc-<id>]` in the PR title, so that Shortcut attaches the work to the story.
