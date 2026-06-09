#!/usr/bin/env bash
# Status line: model | repo | approx context | session cost
# Reads Claude Code's session JSON on stdin. Defensive: any missing field is
# simply omitted. Context figure is a rough estimate from transcript size.
# NOTE: the status line renders in the CLI / desktop terminal; the web UI may
# not display it. The UserPromptSubmit hook works everywhere regardless.
input=$(cat)

get() { printf '%s' "$input" | jq -r "$1 // empty" 2>/dev/null; }

model=$(get '.model.display_name'); [ -z "$model" ] && model=$(get '.model.id'); [ -z "$model" ] && model="Claude"
dir=$(get '.workspace.current_dir'); [ -z "$dir" ] && dir=$(get '.cwd')
tp=$(get '.transcript_path')
cost=$(get '.cost.total_cost_usd')

line="$model"
[ -n "$dir" ] && line="$line | $(basename "$dir")"

if [ -n "$tp" ] && [ -f "$tp" ]; then
  bytes=$(wc -c < "$tp" 2>/dev/null | tr -d ' ')
  if [ -n "$bytes" ]; then
    est=$(( bytes / 4 / 1000 ))
    flag=""; [ "$est" -ge 120 ] && flag=" ⚠️/clear"
    line="$line | ctx~${est}k${flag}"
  fi
fi

[ -n "$cost" ] && line="$line | \$$(printf '%.2f' "$cost" 2>/dev/null || printf '%s' "$cost")"

printf '%s' "$line"
