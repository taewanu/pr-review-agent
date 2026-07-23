# shellcheck shell=bash
# Test-only prelude, sourced after lib.sh in a bash snippet.
#
# The gh-backed helpers wrap their gh call in run_with_app_token (ADR 0036),
# which mints an installation token before running the command. A unit test of
# the helper's gh/jq pipeline has no App key or network, so it stubs the mint:
# _gh_token hands back a fixed token, and PRA_APP_ID/PRA_INSTALLATION_ID stand
# in for what an entry point's app_auth_init would set. run_with_app_token's
# allowlist and fail-closed check still run, so the wrapper stays on the tested
# path; only the credential is faked.
# Read by the sourcing test snippet's wrapped gh calls, invisible to shellcheck.
# shellcheck disable=SC2034
PRA_APP_ID=1
# shellcheck disable=SC2034
PRA_INSTALLATION_ID=2
_gh_token() { _GH_TOKEN_VALUE="test-token"; }
