#!/bin/sh
# dreams-diary: append one line per commit to $HERMES_HOME/logs/git-commits.tsv.
# Columns: time  repo  sha  profile  source
#   source=hermes    the commit ran under a Hermes session (env var visible)
#   source=external  a plain shell / editor commit
# Logging must never block a commit, so every failure ends in exit 0.
# Installed by scripts/install_commit_hook.py; this file is copied, not linked.

{
    sha=$(git rev-parse --verify -q HEAD) || exit 0
    repo=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)")
    now=$(date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')

    if [ -n "${HERMES_SESSION_PROFILE:-}" ]; then
        profile=$HERMES_SESSION_PROFILE
        src=hermes
    elif [ -n "${HERMES_HOME:-}" ]; then
        profile=$(basename "$HERMES_HOME")
        src=hermes
    else
        profile=unset
        src=external
    fi

    if [ -n "${HERMES_HOME:-}" ]; then
        log_dir=$HERMES_HOME/logs
    else
        log_dir=$HOME/.hermes/logs
    fi

    mkdir -p "$log_dir" 2>/dev/null || exit 0
    # Tabs and newlines in a name would corrupt the TSV; replace them.
    printf '%s\t%s\t%s\t%s\t%s\n' "$now" "$repo" "$sha" \
        "$(printf '%s' "$profile" | tr '\t\n' '  ')" "$src" \
        >> "$log_dir/git-commits.tsv" 2>/dev/null
} 2>/dev/null

exit 0
