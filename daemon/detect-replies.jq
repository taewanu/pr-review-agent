# Select the Operator-reply threads reply-pr.sh still needs to act on (#39).
#
# Input: the PR's inline review comments (gh `pulls/{n}/comments`, --paginate).
# Args: --arg login  the bot's REST login (`<slug>[bot]`); the parent finding
#                    must be ours. A reply is excluded when its own author is a
#                    Bot (user.type), which covers our own acks and other bots
#                    (ADR 0036 decisions 8, 9), replacing the old body-text
#                    Provenance self-exclusion that produced the #153 false drop.
# Output: [{parent_finding:{...}, operator_reply:{...}}] for each unaddressed reply.
#
# Extracted from reply-pr.sh so the selection is unit-testable
# (tests/test_detect_replies.py); the inline-but-untested form let the #153
# self-reply bug ship (a daemon reply ack mistaken for an Operator reply).

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
        # answer only non-bot replies (ADR 0036 decision 9). This also excludes our
        # own acks, which post as the bot: under a distinct bot login authorship
        # separates the daemon's reply from a human's, so no body-text tag is needed
        # (the #153 self-reply false drop is gone with it).
        and (($cur.user.type // "") != "Bot")
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
