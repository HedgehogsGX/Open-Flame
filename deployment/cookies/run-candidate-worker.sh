#!/bin/sh
# Parse the fixed optional mapping without sourcing it or printing its values.
set +x
set -eu
unset ENV POSIXLY_CORRECT
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
export LC_ALL=C

MAPPING_FILE=/run/vdc-cookie-mapping.env
SOURCE_ROOT=/run/vdc-cookie-sources
x_ref=
youtube_ref=
bilibili_ref=
douyin_ref=
x_seen=0
youtube_seen=0
bilibili_seen=0
douyin_seen=0
seen_references=""

fail() {
    printf '%s\n' "Cookie runner validation failed for $1 ($2)." >&2
    exit 1
}

assign_reference() {
    platform=$1
    reference=$2
    seen_flag=$3
    [ "$seen_flag" = 0 ] || fail "$platform" "mapping key is duplicated"
    if [ -n "$reference" ]; then
        case "$reference" in
            [A-Za-z0-9]*) ;;
            *) fail "$platform" "opaque reference is invalid" ;;
        esac
        case "$reference" in
            *[!A-Za-z0-9_.-]*) fail "$platform" "opaque reference is invalid" ;;
        esac
        [ "${#reference}" -le 64 ] || fail "$platform" "opaque reference is invalid"
        case " $seen_references " in
            *" $reference "*) fail "$platform" "opaque reference is reused" ;;
        esac
        seen_references="$seen_references $reference"
    fi
}

[ "$#" -gt 0 ] || fail worker "candidate command is missing"
[ -r "$MAPPING_FILE" ] || fail mapping "mapping is unavailable"
[ -d "$SOURCE_ROOT" ] || fail root "source root is unavailable"
if ! { exec 3< "$MAPPING_FILE"; } 2>/dev/null; then
    fail mapping "mapping is unavailable"
fi

while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ""|\#*) continue ;;
        *=*) key=${line%%=*}; reference=${line#*=} ;;
        *) fail mapping "mapping syntax is invalid" ;;
    esac
    case "$key" in
        VDC_COOKIE_X_OPAQUE_REF)
            assign_reference x "$reference" "$x_seen"
            x_ref=$reference
            x_seen=1
            ;;
        VDC_COOKIE_YOUTUBE_OPAQUE_REF)
            assign_reference youtube "$reference" "$youtube_seen"
            youtube_ref=$reference
            youtube_seen=1
            ;;
        VDC_COOKIE_BILIBILI_OPAQUE_REF)
            assign_reference bilibili "$reference" "$bilibili_seen"
            bilibili_ref=$reference
            bilibili_seen=1
            ;;
        VDC_COOKIE_DOUYIN_OPAQUE_REF)
            assign_reference douyin "$reference" "$douyin_seen"
            douyin_ref=$reference
            douyin_seen=1
            ;;
        *) fail mapping "mapping key is unsupported" ;;
    esac
done <&3
exec 3<&-

for seen_flag in "$x_seen" "$youtube_seen" "$bilibili_seen" "$douyin_seen"; do
    [ "$seen_flag" = 1 ] || fail mapping "mapping key is missing"
done

validate_optional_source() {
    platform=$1
    reference=$2
    source_path=$3
    if [ -z "$reference" ]; then
        [ ! -e "$source_path" ] && [ ! -L "$source_path" ] || \
            fail "$platform" "unmapped source is present"
        return
    fi
    [ -f "$source_path" ] && [ ! -L "$source_path" ] || \
        fail "$platform" "mapped source is unavailable"
}

x_source=$SOURCE_ROOT/x/cookies.txt
youtube_source=$SOURCE_ROOT/youtube/cookies.txt
bilibili_source=$SOURCE_ROOT/bilibili/cookies.txt
douyin_source=$SOURCE_ROOT/douyin/cookies.txt

validate_optional_source x "$x_ref" "$x_source"
validate_optional_source youtube "$youtube_ref" "$youtube_source"
validate_optional_source bilibili "$bilibili_ref" "$bilibili_source"
validate_optional_source douyin "$douyin_ref" "$douyin_source"

[ -z "$x_ref" ] || set -- "$@" --cookie-source "x:$x_ref=$x_source"
[ -z "$youtube_ref" ] || set -- "$@" --cookie-source "youtube:$youtube_ref=$youtube_source"
[ -z "$bilibili_ref" ] || set -- "$@" --cookie-source "bilibili:$bilibili_ref=$bilibili_source"
[ -z "$douyin_ref" ] || set -- "$@" --cookie-source "douyin:$douyin_ref=$douyin_source"

exec "$@"
