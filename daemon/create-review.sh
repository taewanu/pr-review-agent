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
FILE_LEVEL=""
UNANCHORED=""
DROPPED_COMBO=0
APP_ID=""
INSTALLATION_ID=""
APP_SLUG=""

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
    --file-level)
      FILE_LEVEL="$2"
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
    --app-slug)
      APP_SLUG="$2"
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
[[ -n "$FILE_LEVEL" ]] || {
  log_err "missing --file-level"
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
# Checked here rather than beside the POST below, because the file-level comments
# post before the review body is even rendered and need the same credentials.
if [[ $DRY_RUN -eq 0 ]]; then
  [[ -n "$APP_ID" ]] || {
    log_err "create-review.sh: missing --app-id"
    exit 1
  }
  [[ -n "$INSTALLATION_ID" ]] || {
    log_err "create-review.sh: missing --installation-id"
    exit 1
  }
fi

WORK="$(mktemp -d -t pr-review-post.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

summary="$(cat "$SUMMARY_FILE")"

# Per-finding degradation note (ADR 0005). Surfaces dropped findings so the
# operator sees the redaction in the body rather than only in stderr logs.
dropped_note=""
if [[ "$DROPPED_COMBO" -gt 0 ]]; then
  noun="findings"
  if [[ "$DROPPED_COMBO" -eq 1 ]]; then noun="finding"; fi
  dropped_note=$'\n\n'"_${DROPPED_COMBO} ${noun} dropped (forbidden severity×type combo)._"
fi

# Review footer (ADR 0036 decisions 3, 4a): a single App-attributed sign-off,
# rendered by lib.sh from the App slug and this SHA. Slug comes from --app-slug
# (the bot's GraphQL login, threaded from review-pr.sh); a dry-run without it
# falls to the plain fork line.
footer="$(render_review_footer "$APP_SLUG" "$HEAD_SHA")"

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

# A comment body per ADR 0002: type-first header, then the agent's body (bold lead
# + optional bullets). No provenance tag: the bot's own login carries who-wrote-this
# now (ADR 0036), retiring the body-text marker. One jq expression for both comment
# surfaces, inline and file-level, so the two cannot drift apart. Spliced into each
# program with '"$VAR"' so both stay single-quoted and their literal `"` need no
# shell escaping.
# shellcheck disable=SC2016  # $typ/$sev are jq's own variables, bound by --argjson
COMMENT_BODY_JQ='"_" + ($typ[.type] // "❓") + " " + .type + "_ | _"
  + ($sev[.severity] // "❓") + " " + .severity + "_" + "\n\n" + .body'

# File-level findings (ADR 0040): the PR touches the file but no line verified, so
# the finding gets a thread on the file rather than a note in the review body. The
# batched reviews endpoint takes no `subject_type`, so each one is its own POST and
# the review below is no longer the tick's single atomic write. A call that fails
# hands its finding back to the body render, which is where it would have gone
# before this existed, so no finding is lost to a failed request.
FILE_LEVEL_FAILED="$WORK/file-level-failed.json"
printf '[]' >"$FILE_LEVEL_FAILED"

post_file_level_comments() {
  local total i payload out failures
  total="$(jq 'length' "$FILE_LEVEL")"
  [[ "$total" -gt 0 ]] || return 0
  # commit_id is required by the endpoint, so without a head sha there is nothing
  # to post against and every finding falls back to the body.
  if [[ -z "$HEAD_SHA" ]]; then
    cp "$FILE_LEVEL" "$FILE_LEVEL_FAILED"
    log_err "no --head-sha: ${total} file-level comment(s) rendered in the review body instead"
    return 0
  fi
  log_info "posting ${total} file-level comment(s) to ${OWNER}/${REPO}#${NUMBER}"
  failures="$WORK/file-level-failures.jsonl"
  : >"$failures"
  for ((i = 0; i < total; i++)); do
    payload="$WORK/file-level-${i}.json"
    out="$WORK/file-level-${i}.out"
    # shellcheck disable=SC2016  # $commit_id is jq's own variable, bound by --arg
    jq --argjson sev "$SEV_EMOJI" --argjson typ "$TYPE_EMOJI" --arg commit_id "$HEAD_SHA" \
      ".[$i]"' | {path, commit_id: $commit_id, subject_type: "file", body: ('"$COMMENT_BODY_JQ"')}' \
      "$FILE_LEVEL" >"$payload"
    if ! run_with_app_token "$APP_ID" "$INSTALLATION_ID" \
      gh api \
      --method POST \
      "repos/${OWNER}/${REPO}/pulls/${NUMBER}/comments" \
      --input "$payload" >"$out" 2>&1; then
      log_err "file-level comment POST failed: $(<"$out")"
      jq -c ".[$i]" "$FILE_LEVEL" >>"$failures"
    fi
  done
  jq -sc '.' "$failures" >"$FILE_LEVEL_FAILED"
}

# Findings the review body carries: those on files the PR never touched, plus any
# file-level comment whose POST failed above. A dry-run makes no call, so it shows
# the file-level findings here too rather than dropping them from the one payload
# it prints.
BODY_FINDINGS="$WORK/body-findings.json"
if [[ $DRY_RUN -eq 1 ]]; then
  jq -s 'add' "$UNANCHORED" "$FILE_LEVEL" >"$BODY_FINDINGS"
else
  post_file_level_comments
  jq -s 'add' "$UNANCHORED" "$FILE_LEVEL_FAILED" >"$BODY_FINDINGS"
fi
printf 'file_level_failed=%s\n' "$(jq 'length' "$FILE_LEVEL_FAILED")" >&2

# Render body findings into a Markdown section appended to the review body.
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
' "$BODY_FINDINGS")"

body_with_additional="${summary}${dropped_note}${additional}${footer}${sentinel}"

# Build inline comment payloads. Range findings (end_line > line) use
# {start_line, start_side, line, side, body}; single-line uses {line, side, body}.
comments_json="$(jq --argjson sev "$SEV_EMOJI" --argjson typ "$TYPE_EMOJI" '
  map(
    {
      path: .path,
      side: "RIGHT",
      body: ('"$COMMENT_BODY_JQ"')
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

log_info "submitting review to ${OWNER}/${REPO}#${NUMBER}"

# Write the payload to a file and pass --input <file> rather than piping it in:
# `printf | run_with_app_token gh` runs gh on the right side of a pipe (a
# subshell), and a mint there would land in that doomed subshell. The file form
# keeps the wrapped gh in the main shell. gh api writes the response body to
# stdout and a status line to stderr; 2>&1 captures both so a failure body is
# logged (a wrapper log_err on a failed mint lands here too, and is surfaced).
out_file="$WORK/review.out"
payload_file="$WORK/review.json"
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
