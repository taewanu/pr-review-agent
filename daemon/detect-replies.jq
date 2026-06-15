# Select the Operator-reply threads reply-pr.sh still needs to act on (#39).
#
# Input: the PR's inline review comments (gh `pulls/{n}/comments`, --paginate).
# Args: --arg login       the daemon/operator gh login (parent finding must be ours).
#       --arg provenance  the Provenance tag (lib.sh PROVENANCE_TAG, the single
#                         source), so a daemon-authored comment is never mistaken
#                         for an Operator reply (#153).
# Output: [{parent_finding:{...}, operator_reply:{...}}] for each unaddressed reply.
#
# Extracted from reply-pr.sh so the selection is unit-testable
# (tests/test_detect_replies.py); the inline-but-untested form let the `_Fixed:_`
# self-reply bug (#153) ship.

. as $all
# Operator-reply IDs we have already acked, read from prior Reply sentinels
# (a body-bearing reply for fix claims and questions, or an acknowledgment
# parent finding body).
| [.[] | (.body // "") | scan("pr-review-agent:(?:addressed|reply):([0-9]+)") | .[0]]
  as $addressed
| (map({key: (.id | tostring), value: .}) | from_entries) as $by_id
| map(
    # Bind .id to $cur first; piping into $addressed shifts `.` to the array,
    # so a naked `.id` inside `index(...)` would fail.
    . as $cur
    | select(
        $cur.in_reply_to_id != null
        # parent must be our finding
        and ($by_id[($cur.in_reply_to_id | tostring)].user.login == $login)
        # exclude our own comments: a reply ack carries a reply sentinel, but a
        # `_Fixed:_` note carries only the fix sentinel — both carry the Provenance
        # tag, so that is the reliable own-comment gate (#153).
        and (($cur.body // "") | contains($provenance) | not)
        # exclude replies already in the addressed-set
        and (($addressed | index($cur.id | tostring)) | not)
      )
    | . as $op
    | ($by_id[($op.in_reply_to_id | tostring)]) as $parent
    | ($parent.line // $parent.original_line) as $endL
    | ($parent.start_line // $parent.original_start_line // $endL) as $startL
    | {
        parent_finding: {
          comment_id: ($parent.id | tostring),
          path: $parent.path,
          line: $startL,
          end_line: $endL,
          body: $parent.body
        },
        operator_reply: {
          comment_id: ($op.id | tostring),
          body: $op.body
        }
      }
  )
