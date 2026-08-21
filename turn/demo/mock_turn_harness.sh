#!/usr/bin/env bash
set -euo pipefail

# Real process-level harness fixture for test-mode E2E runs only. Turn starts
# this through the terminal transport, supplies the initial prompt through a
# private control file, and writes the atomic handoff file.

if [[ "${1:-}" == "--reconnect" ]]; then
  session_id="${2:?session id is required for reconnect}"
  prompt="${*:3}"
  printf 'mock-turn: resumed session %s\n' "$session_id"
  printf 'mock-turn: follow-up prompt received (%s bytes)\n' "${#prompt}"
  handoff_kind="${TURN_HANDOFF_KIND:-result}"
  handoff_file="${TURN_HANDOFF_FILE:?TURN_HANDOFF_FILE is required for reconnect}"
  generated_prompt="${TURN_MOCK_GENERATED_PROMPT:-}"
  case "$handoff_kind" in
    verification)
      payload='{"decision":"APPROVE","summary":"The follow-up review accepts the corrected work.","findings":[],"required_changes":[],"evidence_refs":[]}'
      printf '%s\n' "$payload" | turn agent verify --stdin
      ;;
    result)
      if [[ "$generated_prompt" == *"MOCK_RERUN"* ]]; then
        payload='{"outcome":"COMPLETE","summary":"Fresh follow-up pass complete.","artifacts":[{"name":"second-pass","content":"new output"}]}'
      else
        payload='{"outcome":"COMPLETE","summary":"The requested correction is complete.","artifacts":[{"name":"corrected","content":"corrected output"}]}'
      fi
      printf '%s\n' "$payload" | turn agent submit --kind result --stdin
      ;;
    plan)
      printf '%s\n' '{"nodes":[]}' | turn agent submit --kind plan --stdin
      ;;
    *)
      printf 'mock-turn: unsupported reconnect handoff kind: %s\n' "$handoff_kind" >&2
      exit 2
      ;;
  esac
  printf 'mock-turn: cli submission accepted (kind=%s)\n' "$handoff_kind"
  printf 'mock-turn: process exiting 0\n'
  exit 0
fi

if [[ -z "${TURN_HANDOFF_KIND:-}" ]]; then
  printf 'mock-turn: capability probe\n'
  exit 0
fi

handoff_kind="$TURN_HANDOFF_KIND"
handoff_file="${TURN_HANDOFF_FILE:?TURN_HANDOFF_FILE is required}"
exit_file="${TURN_MOCK_EXIT_FILE:-}"
write_exit_status() {
  status=$?
  if [[ -n "$exit_file" ]]; then
    printf '%s\n' "$status" > "$exit_file"
  fi
}
trap write_exit_status EXIT
prompt="${*:-}"
if [[ -n "${TURN_INITIAL_PROMPT_FILE:-}" ]]; then
  prompt="$(cat "$TURN_INITIAL_PROMPT_FILE")"
fi

printf 'mock-turn: process started (kind=%s)\n' "$handoff_kind"
printf 'mock-turn: initial prompt received (%s bytes)\n' "${#prompt}"

case "$handoff_kind" in
  plan)
    if [[ -n "${TURN_MOCK_PLAN_FILE:-}" && -f "$TURN_MOCK_PLAN_FILE" ]]; then
      payload="$(cat "$TURN_MOCK_PLAN_FILE")"
    else
      payload='{"nodes":[{"key":"work","objective":"Create the tiny greeting app","executor":"mock","generated_prompt":"MOCK_COMPLETE_GREETING"},{"key":"review","objective":"Verify the tiny greeting app","executor":"mock","agent_type":"verifier","generated_prompt":"MOCK_VERIFY_REJECT","follows":["work"]},{"key":"release","objective":"Publish the tiny greeting app","executor":"mock","generated_prompt":"MOCK_COMPLETE_RELEASE","follows":["review"]}]}'
    fi
    ;;
  verification)
    if [[ "$prompt" == *"MOCK_VERIFY_REJECT_THEN_APPROVE"* && "${TURN_MOCK_ATTEMPT:-1}" != "1" ]]; then
      payload='{"decision":"APPROVE","summary":"The corrected work is accepted on the second review.","findings":[],"required_changes":[],"evidence_refs":[]}'
    else
      payload='{"decision":"REJECT","summary":"The work needs one correction before acceptance.","findings":["The process-level mock intentionally rejects this first review."],"required_changes":["Apply the requested correction before the next review."],"evidence_refs":[]}'
    fi
    ;;
  result)
    attempt="${TURN_MOCK_ATTEMPT:-1}"
    if [[ "$prompt" == *"MOCK_EXPANDED_A"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Expanded part A complete."}'
    elif [[ "$prompt" == *"MOCK_EXPANDED_B"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Expanded part B complete."}'
    elif [[ "$prompt" == *"MOCK_EXPAND"* ]]; then
      payload='{"outcome":"EXPAND","summary":"Expansion produced two child tasks.","children":{"nodes":[{"key":"part-a","objective":"Complete expanded part A","executor":"mock","generated_prompt":"MOCK_EXPANDED_A"},{"key":"part-b","objective":"Complete expanded part B","executor":"mock","generated_prompt":"MOCK_EXPANDED_B","follows":["part-a"]}]}}'
    elif [[ "$prompt" == *"MOCK_RERUN"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"COMPLETE","summary":"First pass complete.","artifacts":[{"name":"first-pass","content":"old output"}]}'
    elif [[ "$prompt" == *"MOCK_RERUN"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Fresh pass complete.","artifacts":[{"name":"second-pass","content":"new output"}]}'
    elif [[ "$prompt" == *"MOCK_FAIL_ONCE"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"FAIL","summary":"The process failed on the first attempt.","error":"Intentional mock process failure.","retry_recommended":true}'
    elif [[ "$prompt" == *"MOCK_FAIL_ONCE"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Retry recovered successfully.","artifacts":[{"name":"recovered","content":"retry output"}]}'
    elif [[ "$prompt" == *"MOCK_BLOCK_ONCE"* && "$attempt" == "1" ]]; then
      payload='{"outcome":"BLOCK","summary":"Waiting for the user choice.","missing_inputs":[{"id":"choice","label":"Choose a path","kind":"decision","description":"Any value unblocks this process fixture."}]}'
    elif [[ "$prompt" == *"MOCK_BLOCK_ONCE"* ]]; then
      payload='{"outcome":"COMPLETE","summary":"Input received; task continued."}'
    elif [[ "$prompt" == *"MOCK_DELAYED"* ]]; then
      printf 'mock-turn: waiting for cancellation\n'
      sleep 5
      payload='{"outcome":"COMPLETE","summary":"This task only completes if it is not stopped."}'
    else
      payload='{"outcome":"COMPLETE","summary":"The process-level harness completed the greeting task.","artifacts":[{"kind":"text","name":"greeting.txt","content":"Hello from the process-level harness.\n"}]}'
    fi
    ;;
  *)
    printf 'mock-turn: unsupported handoff kind: %s\n' "$handoff_kind" >&2
    exit 2
    ;;
esac

if [[ "$handoff_kind" == "verification" ]]; then
  printf '%s\n' "$payload" | turn agent verify --stdin
else
  printf '%s\n' "$payload" | turn agent submit --kind "$handoff_kind" --stdin
fi
printf 'mock-turn: cli submission accepted (kind=%s)\n' "$handoff_kind"
printf 'mock-turn: process exiting 0\n'
