#!/bin/sh
# dreams-diary: append one line per commit to $HERMES_HOME/logs/git-commits/<YYYY-MM-DD>.tsv.
# Columns: time  repo  sha  profile
# Only commits made under a Hermes profile are recorded (HERMES_HOME set).
# A commit without HERMES_HOME writes nothing, so the diary's material reads
# the day's file and treats "absent" as "not recorded for this profile".
# The hook only appends. Old day files are removed by the material step, never here.
# Logging must never block a commit, so every failure ends in exit 0.
# Installed by scripts/install_commit_hook.py; this file is copied, not linked.

{
    [ -n "${HERMES_HOME:-}" ] || exit 0

    sha=$(git rev-parse --verify -q HEAD) || exit 0
    repo=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)")
    now=$(date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')
    day=$(date +%Y-%m-%d)

    if [ -n "${HERMES_SESSION_PROFILE:-}" ]; then
        profile=$HERMES_SESSION_PROFILE
    else
        profile=$(basename "$HERMES_HOME")
    fi

    log_dir=$HERMES_HOME/logs/git-commits
    mkdir -p "$log_dir" 2>/dev/null || exit 0
    # Tabs and newlines in a name would corrupt the TSV; replace them.
    printf '%s\t%s\t%s\t%s\n' "$now" "$repo" "$sha" \
        "$(printf '%s' "$profile" | tr '\t\n' '  ')" \
        >> "$log_dir/$day.tsv" 2>/dev/null
} 2>/dev/null

exit 0
