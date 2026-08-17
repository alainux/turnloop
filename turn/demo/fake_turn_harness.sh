#!/usr/bin/env bash
set -euo pipefail

# Real process-level harness fixture for test-mode E2E runs only. Turn starts
# this through the terminal transport, supplies the initial prompt through a
# private control file, and writes the atomic handoff file.

if [[ "${1:-}" == "--reconnect" ]]; then
  session_id="${2:?session id is required for reconnect}"
  prompt="${*:3}"
  printf 'fake-turn: resumed session %s\n' "$session_id"
  printf 'fake-turn: follow-up prompt received (%s bytes)\n' "${#prompt}"
  exit 0
fi

if [[ -z "${TURN_HANDOFF_KIND:-}" ]]; then
  printf 'fake-turn: capability probe\n'
  exit 0
fi

handoff_kind="$TURN_HANDOFF_KIND"
handoff_file="${TURN_HANDOFF_FILE:?TURN_HANDOFF_FILE is required}"
prompt="${*:-}"
if [[ -n "${TURN_INITIAL_PROMPT_FILE:-}" ]]; then
  prompt="$(cat "$TURN_INITIAL_PROMPT_FILE")"
fi

printf 'fake-turn: process started (kind=%s)\n' "$handoff_kind"
printf 'fake-turn: initial prompt received (%s bytes)\n' "${#prompt}"

case "$handoff_kind" in
  plan)
    if [[ -n "${TURN_FAKE_PLAN_FILE:-}" && -f "$TURN_FAKE_PLAN_FILE" ]]; then
      payload="$(cat "$TURN_FAKE_PLAN_FILE")"
    else
      payload='{"nodes":[{"key":"work","objective":"Create the tiny greeting app","executor":"fake","generated_prompt":"FAKE_COMPLETE_GREETING"},{"key":"review","objective":"Verify the tiny greeting app","executor":"fake","agent_type":"verifier","generated_prompt":"FAKE_VERIFY_REJECT","depends_on":["work"]},{"key":"release","objective":"Publish the tiny greeting app","executor":"fake","generated_prompt":"FAKE_COMPLETE_RELEASE","depends_on":["review"]}]}'
    fi
    ;;
  verification)
    payload='{"decision":"REJECT","summary":"The greeting needs one correction before release.","findings":["The process-level harness intentionally rejects this review."],"required_changes":["Apply the requested greeting correction."],"evidence_refs":[]}'
    ;;
  result)
    attempt="${TURN_FAKE_ATTEMPT:-1}"
    if [[ "$prompt" == *"FAKE_EXPANDED_A"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Expanded part A complete."}'
    elif [[ "$prompt" == *"FAKE_EXPANDED_B"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Expanded part B complete."}'
    elif [[ "$prompt" == *"FAKE_EXPAND"* ]]; then
      payload='{"outcome":"EXPAND","summary":"Expansion produced two child tasks.","children":{"nodes":[{"key":"part-a","objective":"Complete expanded part A","executor":"fake","generated_prompt":"FAKE_EXPANDED_A"},{"key":"part-b","objective":"Complete expanded part B","executor":"fake","generated_prompt":"FAKE_EXPANDED_B","depends_on":["part-a"]}]}}'
    elif [[ "$prompt" == *"FAKE_RERUN"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"COMPLETE","summary":"First pass complete.","artifacts":[{"name":"first-pass","content":"old output"}]}'
    elif [[ "$prompt" == *"FAKE_RERUN"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Fresh pass complete.","artifacts":[{"name":"second-pass","content":"new output"}]}'
    elif [[ "$prompt" == *"FAKE_FAIL_ONCE"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"FAIL","summary":"The process failed on the first attempt.","error":"Intentional fake process failure.","retry_recommended":true}'
    elif [[ "$prompt" == *"FAKE_FAIL_ONCE"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Retry recovered successfully.","artifacts":[{"name":"recovered","content":"retry output"}]}'
    elif [[ "$prompt" == *"FAKE_BLOCK_ONCE"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"BLOCK","summary":"Waiting for the user choice.","missing_inputs":[{"id":"choice","label":"Choose a path","kind":"decision","description":"Any value unblocks this process fixture."}]}'
    elif [[ "$prompt" == *"FAKE_BLOCK_ONCE"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Input received; task continued."}'
    elif [[ "$prompt" == *"FAKE_DELAYED"* ]]; then
      printf 'fake-turn: waiting for cancellation\n'
      sleep 5
      payload='{"outcome":"COMPLETE","summary":"This task only completes if it is not stopped."}'
    else
      payload='{"outcome":"COMPLETE","summary":"The process-level harness completed the greeting task.","artifacts":[{"kind":"text","name":"greeting.txt","content":"Hello from the process-level harness.\n"}]}'
    fi
    ;;
  *)
    printf 'fake-turn: unsupported handoff kind: %s\n' "$handoff_kind" >&2
    exit 2
    ;;
esac

temporary_file="${handoff_file}.tmp.$$"
printf '%s\n' "$payload" > "$temporary_file"
mv "$temporary_file" "$handoff_file"
printf 'fake-turn: handoff written; exiting 0\n'
