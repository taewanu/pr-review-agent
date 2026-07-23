#!/usr/bin/env bash
# create-review.sh — create and submit the review via gh api POST /reviews. Every
# review submits immediately as a COMMENT under the bot identity (ADR 0036
# decision 6): no pending draft, no own-vs-others fork.

set -euo pipefail

# shellcheck source=daemon/lib.sh disable=SC1091
source "$(dirname "$0")/lib.sh"

DRY_RUN=0
HEAD_SHA=""
HEAD_REPO_URL=""
OWNER=""
REPO=""
NUMBER=""
SUMMARY_FILE=""
ANCHORED=""
UNANCHORED=""
DROPPED_COMBO=0
APP_ID=""
INSTALLATION_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner)
      OWNER="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --number)
      NUMBER="$2"
      shift 2
      ;;
    --summary-file)
      SUMMARY_FILE="$2"
      shift 2
      ;;
    --anchored)
      ANCHORED="$2"
      shift 2
      ;;
    --unanchored)
      UNANCHORED="$2"
      shift 2
      ;;
    --head-sha)
      HEAD_SHA="$2"
      shift 2
      ;;
    --head-repo-url)
      HEAD_REPO_URL="$2"
      shift 2
      ;;
    --dropped-combo)
      DROPPED_COMBO="$2"
      shift 2
      ;;
    --app-id)
      APP_ID="$2"
      shift 2
      ;;
    --installation-id)
      INSTALLATION_ID="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      log_err "create-review.sh: unknown arg: $1"
      exit 1
      ;;
  esac
done

[[ -n "$OWNER" ]] || {
  log_err "missing --owner"
  exit 1
}
[[ -n "$REPO" ]] || {
  log_err "missing --repo"
  exit 1
}
[[ -n "$NUMBER" ]] || {
  log_err "missing --number"
  exit 1
}
[[ -n "$SUMMARY_FILE" ]] || {
  log_err "missing --summary-file"
  exit 1
}
[[ -n "$ANCHORED" ]] || {
  log_err "missing --anchored"
  exit 1
}
[[ -n "$UNANCHORED" ]] || {
  log_err "missing --unanchored"
  exit 1
}
if ! [[ "$DROPPED_COMBO" =~ ^[0-9]+$ ]]; then
  log_err "--dropped-combo must be a non-negative integer (got: $DROPPED_COMBO)"
  exit 1
fi

# Project identity for the footer/banner, derived from this checkout's git
# remote. Any fork running from a normal clone gets its own identity with
# zero config.
derive_project_identity "$(dirname "$0")/.."
project_url="$PROJECT_URL"
project_name="$PROJECT_NAME"

summary="$(cat "$SUMMARY_FILE")"

# Auto-prepend a preview-release banner while pyproject.toml's version stays on 0.x.x.
# Disappears automatically once the project ships a 1.0+ version.
banner=""
pyproject="$(dirname "$0")/../pyproject.toml"
if [[ -r "$pyproject" ]]; then
  version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$pyproject" | head -1)"
  if [[ "$version" =~ ^0\. ]]; then
    banner="_${project_name} v${version} (preview release). [Report a problem](${project_url}/issues)._"$'\n\n---\n\n'
  fi
fi

# Per-finding degradation note (ADR 0005). Surfaces dropped findings so the
# operator sees the redaction in the body rather than only in stderr logs.
dropped_note=""
if [[ "$DROPPED_COMBO" -gt 0 ]]; then
  noun="findings"
  if [[ "$DROPPED_COMBO" -eq 1 ]]; then noun="finding"; fi
  dropped_note=$'\n\n'"_${DROPPED_COMBO} ${noun} dropped (forbidden severity×type combo)._"
fi

# Review footer per ADR 0010 (the posted-format contract; ADR 0001 D3 decides
# one pending review per tick, not this string). Identity from
# derive_project_identity above. The leading `\n\n---\n\n` detaches the footer
# from whatever ends the body (summary, dropped-note, `## Findings outside the diff`,
# or nothing). Review-level, so it carries attribution + next action, not the
# item-level Provenance tag.
# Every review submits immediately as a COMMENT under the bot (ADR 0036 decision
# 6), so the action line is post-hoc (edit) rather than pre-submit. It says
# "edit", not "delete": GitHub rejects deleting a submitted review, so an unwanted
# review can only be edited or have its comments hidden. Footer content (App-owner
# attribution, rotating pool) is reworked separately; this keeps it single.
footer=$'\n\n---\n\n🤖 _Auto-submitted by ['"${project_name}"']('"${project_url}"'). Edit as needed._'

# Dedup sentinel per ADR 0006. Encodes the reviewed SHA so the next tick can
# parse it from `gh api .../reviews` and skip same-SHA re-reviews / scope the
# diff to <sentinel_sha>..HEAD. ASCII-only payload survives GitHub's markdown
# sanitizer; operator login comes from `review.user.login` on the API response
# and is not repeated here. Omitted when HEAD_SHA is unset (dry-run / tests
# without --head-sha) so the body stays clean rather than emitting a half
# sentinel that the parser would never match.
sentinel=""
if [[ -n "$HEAD_SHA" ]]; then
  sentinel=$'\n'"<!-- pr-review-agent:sha:${HEAD_SHA} -->"
fi

# Severity and type emoji maps per ADR 0002. Single-sourced here so the
# outside-the-diff render and the inline-comments render stay in lockstep.
SEV_EMOJI='{"important":"🔴","nit":"🟡","pre_existing":"🟣"}'
TYPE_EMOJI='{"bug":"🐛","refactor":"🔧","polish":"✨","intent":"🔀"}'

# Render unanchored findings into a Markdown section appended to the review body.
# `## Findings outside the diff` is the canonical relocation surface per ADR 0005.
# Item shape (ADR 0010 §2, #132): `_<type-emoji> <type>_ | _<severity-emoji>
# <severity>_: <location>`. The badge leads (triage parity with the Inline
# comment); the location follows after a colon, mirroring the reply verdict's
# colon-into-link (ADR 0010 #106).
# Bodies sit inside the outer `- ` list item, so every line is 2-space indented
# (gsub on internal newlines) to keep bullets and follow-up paragraphs nested.
# Relocated findings are unanchored to the diff, so the location code span is
# their only pointer back to the source. Link it to the file at the head commit
# (fork-correct via --head-repo-url, targeting the repo where head_sha lives) so
# it matches the reply blob link and the status-comment SHA links. The visible
# label stays the `path:line[-end]` code span; only the wrapping `[ ]( )` is
# added. Degrades to the bare code span when either head arg is empty (dry-run /
# no --head-sha), mirroring the sentinel's omit-when-unset rule above.
additional="$(jq -r \
  --argjson sev "$SEV_EMOJI" --argjson typ "$TYPE_EMOJI" \
  --arg head_repo_url "$HEAD_REPO_URL" --arg head_sha "$HEAD_SHA" '
  ($head_repo_url != "" and $head_sha != "") as $linkable
  | if length == 0 then ""
  else "\n\n## Findings outside the diff\n\n" + (
    map(
      (("`" + .path + ":" + (.line | tostring) +
        (if .end_line and .end_line != .line then "-" + (.end_line | tostring) else "" end) +
        "`") as $label
      | ("#L" + (.line | tostring) +
        (if .end_line and .end_line != .line then "-L" + (.end_line | tostring) else "" end)) as $frag
      | if $linkable then
          "[" + $label + "](" + $head_repo_url + "/blob/" + $head_sha + "/" + .path + $frag + ")"
        else $label end) as $location
      | "- _" +
      ($typ[.type] // "❓") + " " + .type +
      "_ | _" +
      ($sev[.severity] // "❓") + " " + .severity +
      "_: " + $location +
      "\n\n  " + (.body | gsub("\n(?<c>[^\n])"; "\n  \(.c)"))
    ) | join("\n\n")
  )
  end
' "$UNANCHORED")"

body_with_additional="${banner}${summary}${dropped_note}${additional}${footer}${sentinel}"

# Build inline comment payloads. Range findings (end_line > line) use
# {start_line, start_side, line, side, body}; single-line uses {line, side, body}.
# Inline body format per ADR 0002: type-first header, then the agent's body
# (bold lead + optional bullets), then the Provenance tag. An Inline comment is
# item-level, so it carries the tag (who wrote it), not a draft-status footer
# (ADR 0010); $PROVENANCE_TAG is sourced from lib.sh, shared with the Status
# comment and matched against create_reply.py's MARKER by a test.
comments_json="$(jq --argjson sev "$SEV_EMOJI" --argjson typ "$TYPE_EMOJI" --arg marker "$PROVENANCE_TAG" '
  map(
    {
      path: .path,
      side: "RIGHT",
      body: (
        "_" + ($typ[.type] // "❓") + " " + .type +
        "_ | _" +
        ($sev[.severity] // "❓") + " " + .severity + "_" +
        "\n\n" + .body +
        "\n\n" + $marker
      )
    }
    + (
      if .end_line and .end_line > .line then
        {start_line: .line, start_side: "RIGHT", line: .end_line}
      else
        {line: .line}
      end
    )
  )
' "$ANCHORED")"

# An `event` of COMMENT creates and submits the review in one call, with no
# pending stage (ADR 0036 decision 6).
payload="$(jq -n \
  --arg body "$body_with_additional" \
  --argjson comments "$comments_json" \
  --arg commit_id "$HEAD_SHA" \
  '{
    body: $body,
    comments: $comments,
    event: "COMMENT"
  }
  + (if $commit_id == "" then {} else {commit_id: $commit_id} end)')"

if [[ $DRY_RUN -eq 1 ]]; then
  printf '%s\n' "$payload"
  exit 0
fi

[[ -n "$APP_ID" ]] || {
  log_err "create-review.sh: missing --app-id"
  exit 1
}
[[ -n "$INSTALLATION_ID" ]] || {
  log_err "create-review.sh: missing --installation-id"
  exit 1
}

log_info "submitting review to ${OWNER}/${REPO}#${NUMBER}"

# Write the payload to a file and pass --input <file> rather than piping it in:
# `printf | run_with_app_token gh` runs gh on the right side of a pipe (a
# subshell), and a mint there would land in that doomed subshell. The file form
# keeps the wrapped gh in the main shell. gh api writes the response body to
# stdout and a status line to stderr; 2>&1 captures both so a failure body is
# logged (a wrapper log_err on a failed mint lands here too, and is surfaced).
out_file="$(mktemp -t pr-review-post.XXXXXX)"
payload_file="$(mktemp -t pr-review-payload.XXXXXX)"
trap 'rm -f "$out_file" "$payload_file"' EXIT
printf '%s' "$payload" >"$payload_file"

if ! run_with_app_token "$APP_ID" "$INSTALLATION_ID" \
  gh api \
  --method POST \
  "repos/${OWNER}/${REPO}/pulls/${NUMBER}/reviews" \
  --input "$payload_file" >"$out_file" 2>&1; then
  printf 'category=post-failed\n' >&2
  cat "$out_file" >&2
  exit 1
fi
cat "$out_file"
