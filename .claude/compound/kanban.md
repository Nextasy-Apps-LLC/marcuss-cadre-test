# Kanban moves — GitHub Project 6 (Nextasy-Apps-LLC)

Board: https://github.com/orgs/Nextasy-Apps-LLC/projects/6
Status columns: **Backlog → In Progress → In Review → Done**

Every compound skill that changes an issue's stage uses this recipe. There are
two paths; try them in order and always tell the user which one ran.

## Path 1 — `gh api graphql` (local sessions with an authenticated gh)

Requires the `project` scope (`gh auth refresh -s project` once).

**Resolve the project + Status field + option ids** (run once per session; the
ids are stable but cheap to re-resolve — never hardcode stale ids into commits):

```bash
gh api graphql -f query='
  query { organization(login: "Nextasy-Apps-LLC") {
    projectV2(number: 6) {
      id
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  } }'
```

**Add an issue to the project** (needed once per issue; returns the item id):

```bash
ISSUE_NODE_ID=$(gh api repos/<owner>/<repo>/issues/<n> --jq .node_id)
gh api graphql -f query='
  mutation($project: ID!, $content: ID!) {
    addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
      item { id }
    }
  }' -f project=<PROJECT_ID> -f content="$ISSUE_NODE_ID"
```

**Find the item id of an issue already on the board:**

```bash
gh api graphql -f query='
  query { repository(owner: "<owner>", name: "<repo>") {
    issue(number: <n>) {
      projectItems(first: 10) { nodes { id project { number } } }
    }
  } }'
# take the node whose project.number == 6
```

**Set the Status column:**

```bash
gh api graphql -f query='
  mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
    updateProjectV2ItemFieldValue(input: {
      projectId: $project, itemId: $item, fieldId: $field,
      value: {singleSelectOptionId: $option}
    }) { projectV2Item { id } }
  }' -f project=<PROJECT_ID> -f item=<ITEM_ID> -f field=<STATUS_FIELD_ID> -f option=<OPTION_ID>
```

## Path 2 — label fallback (remote sessions without gh)

The GitHub MCP server has no Projects-v2 tools, so when `gh` is unavailable:

1. Apply the matching label to the issue instead: `status:backlog`,
   `status:in-progress`, or `status:in-review` (remove the previous `status:*`
   label). Done needs no label — closing the issue triggers the board's
   "item closed → Done" automation.
2. Say explicitly in your reply that the board column was NOT moved and the
   label is standing in for it, so Marcus (or the next local session) can
   reconcile the board.

## Backstop automations (configured on the board by Marcus, not by skills)

- **Auto-add to project** → new repo issues land in Backlog.
- **Item closed** → moves to Done.

Skills must not rely on automations for In Progress / In Review — those two
moves are always Path 1 or Path 2.
