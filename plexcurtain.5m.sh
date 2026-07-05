#!/bin/bash
# <xbar.title>Plexcurtain</xbar.title>
# <xbar.desc>Hide/show extra Plex libraries via plexcurtain.py</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

PLEXCURTAIN="/Users/pjdoland/Desktop/Repos/plexcurtain/plexcurtain.py"
PY="/opt/homebrew/bin/python3"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

notify_on_error() {
    local out
    out=$("$PY" "$PLEXCURTAIN" "$@" 2>&1)
    if [[ $? -ne 0 ]]; then
        local msg
        msg=$(echo "$out" | tail -1 | tr -d '"')
        osascript -e "display notification \"$msg\" with title \"Plex\"" >/dev/null 2>&1
    fi
}

case "$1" in
    toggle|apply)
        notify_on_error "$1"
        exit 0
        ;;
    check|uncheck)
        "$PY" "$PLEXCURTAIN" "$1" "$2" >/dev/null 2>&1
        exit 0
        ;;
esac

state=$("$PY" "$PLEXCURTAIN" status --swiftbar 2>/dev/null | head -1)
sections=$("$PY" "$PLEXCURTAIN" list-sections 2>/dev/null)

if [[ "$state" == "HIDDEN" ]]; then
    echo "| sfimage=eye.slash sfsize=15"
    echo "---"
    echo "Extra libraries are hidden | sfimage=lock.fill"
    echo "Show extra libraries | bash='$SELF' param1=toggle terminal=false refresh=true sfimage=eye"
    # offer reconciliation if the selection changed while hidden; never list names here
    drift=0
    while IFS=$'\t' read -r name checked st; do
        [[ -z "$name" ]] && continue
        [[ "$checked" == "1" && "$st" == "visible" ]] && drift=1
        [[ "$checked" == "0" && "$st" == "hidden" ]] && drift=1
    done <<< "$sections"
    if [[ $drift -eq 1 ]]; then
        echo "Apply selection changes (restarts Plex) | bash='$SELF' param1=apply terminal=false refresh=true sfimage=arrow.triangle.2.circlepath"
    fi
    echo "Selection is editable while visible | disabled=true sfimage=info.circle"
else
    echo "| sfimage=eye sfsize=15"
    echo "---"
    echo "Extra libraries are visible | sfimage=lock.open"
    echo "Hide extra libraries | bash='$SELF' param1=toggle terminal=false refresh=true sfimage=eye.slash"
    echo "Select libraries to hide | sfimage=checklist"
    while IFS=$'\t' read -r name checked st; do
        [[ -z "$name" ]] && continue
        if [[ "$checked" == "1" ]]; then
            action="uncheck"; mark="✓"
        else
            action="check"; mark="○"
        fi
        suffix=""
        [[ "$st" == "missing" ]] && suffix=" (not on server)"
        echo "-- $mark $name$suffix | bash='$SELF' param1=$action param2=\"$name\" terminal=false refresh=true"
    done <<< "$sections"
fi
echo "---"
echo "Open Plex | href=http://localhost:32400/web sfimage=play.rectangle"
echo "Refresh state | refresh=true sfimage=arrow.clockwise"
