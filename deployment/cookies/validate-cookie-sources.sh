#!/bin/sh
# Host metadata preflight. Cookie files are never opened, hashed, or printed.
set +x
set -eu

unset ENV POSIXLY_CORRECT
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
export LC_ALL=C
umask 077

MAX_COOKIE_BYTES=16777216
MAX_MAPPING_BYTES=4096
EXPECTED_ROOT_UID=0
EXPECTED_ROOT_GID=10001
EXPECTED_ROOT_MODE=750
EXPECTED_PARENT_UID=0
EXPECTED_PARENT_GID=0
EXPECTED_PARENT_MODE=755
EXPECTED_UID=0
EXPECTED_GID=10001
EXPECTED_DIRECTORY_MODE=750
EXPECTED_FILE_MODE=440
EXPECTED_DATA_UID=10001
EXPECTED_DATA_GID=10001
EXPECTED_DATA_MODE=750
EXPECTED_SOCKET_UID=10001
EXPECTED_SOCKET_GID=10001
EXPECTED_SOCKET_MODE=770
seen_identities=""
seen_references=""
x_ref=
youtube_ref=
bilibili_ref=
douyin_ref=
x_seen=0
youtube_seen=0
bilibili_seen=0
douyin_seen=0
x_validated_path=
youtube_validated_path=
bilibili_validated_path=
douyin_validated_path=
x_validated_snapshot=
youtube_validated_snapshot=
bilibili_validated_snapshot=
douyin_validated_snapshot=

fail() {
    printf '%s\n' "Cookie deployment metadata validation failed for $1 ($2)." >&2
    exit 1
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || fail host "required metadata tool unavailable"
}

canonical_path() {
    label=$1
    candidate=$2
    case "$candidate" in
        /*) ;;
        *) fail "$label" "path is not absolute" ;;
    esac
    canonical=$(realpath -e -- "$candidate" 2>/dev/null) || \
        fail "$label" "path is unavailable"
    [ "$canonical" = "$candidate" ] || fail "$label" "path is not canonical"
    [ ! -L "$candidate" ] || fail "$label" "path is a link"
}

paths_overlap() {
    first=$1
    second=$2
    if [ "$first" = / ] || [ "$second" = / ]; then
        return 0
    fi
    [ "$first" != "$second" ] || return 0
    case "$first" in
        "$second"/*) return 0 ;;
    esac
    case "$second" in
        "$first"/*) return 0 ;;
    esac
    return 1
}

reject_extended_acl() {
    label=$1
    candidate=$2
    acl=$(getfacl --absolute-names --numeric --omit-header -- "$candidate" 2>/dev/null) || \
        fail "$label" "ACL metadata is unavailable"
    acl_user=0
    acl_group=0
    acl_other=0
    while IFS= read -r acl_line || [ -n "$acl_line" ]; do
        case "$acl_line" in
            "") continue ;;
            user::[r-][w-][x-]) acl_user=$((acl_user + 1)) ;;
            group::[r-][w-][x-]) acl_group=$((acl_group + 1)) ;;
            other::[r-][w-][x-]) acl_other=$((acl_other + 1)) ;;
            *) fail "$label" "extended or default ACL is not allowed" ;;
        esac
    done <<EOF
$acl
EOF
    [ "$acl_user" = 1 ] && [ "$acl_group" = 1 ] && [ "$acl_other" = 1 ] || \
        fail "$label" "base ACL metadata is invalid"
}

validate_root_owned_ancestor_chain() {
    label=$1
    ancestor=$2
    while :; do
        canonical_path "$label" "$ancestor"
        metadata=$(stat -Lc '%F|%u|%a' -- "$ancestor" 2>/dev/null) || \
            fail "$label" "ancestor metadata is unavailable"
        old_ifs=$IFS
        IFS='|'
        read -r file_type owner_uid permissions <<EOF
$metadata
EOF
        IFS=$old_ifs
        [ "$file_type" = directory ] || fail "$label" "ancestor is not a directory"
        [ "$owner_uid" = 0 ] || fail "$label" "ancestor owner is invalid"
        case "$permissions" in
            ''|*[!0-7]*) fail "$label" "ancestor permissions are invalid" ;;
        esac
        [ $((0$permissions & 022)) -eq 0 ] || \
            fail "$label" "ancestor is group or world writable"
        reject_extended_acl "$label" "$ancestor"
        [ "$ancestor" = / ] && break
        ancestor=${ancestor%/*}
        [ -n "$ancestor" ] || ancestor=/
    done
}

read_directory_metadata() {
    label=$1
    directory=$2
    expected_uid=$3
    expected_gid=$4
    expected_mode=$5
    canonical_path "$label" "$directory"
    metadata=$(stat -Lc '%F|%u|%g|%a' -- "$directory" 2>/dev/null) || \
        fail "$label" "directory metadata is unavailable"
    old_ifs=$IFS
    IFS='|'
    read -r file_type owner_uid owner_gid permissions <<EOF
$metadata
EOF
    IFS=$old_ifs
    [ "$file_type" = directory ] || fail "$label" "path is not a directory"
    [ "$owner_uid" = "$expected_uid" ] || fail "$label" "directory owner is invalid"
    [ "$owner_gid" = "$expected_gid" ] || fail "$label" "directory group is invalid"
    [ "$permissions" = "$expected_mode" ] || fail "$label" "directory permissions are invalid"
    reject_extended_acl "$label" "$directory"
}

read_file_metadata() {
    label=$1
    source_path=$2
    maximum=$3
    canonical_path "$label" "$source_path"
    metadata=$(stat -Lc '%F|%u|%g|%a|%h|%s|%d:%i|%y|%z' -- "$source_path" 2>/dev/null) || \
        fail "$label" "file metadata is unavailable"
    old_ifs=$IFS
    IFS='|'
    read -r file_type owner_uid owner_gid permissions link_count file_size identity modified changed <<EOF
$metadata
EOF
    IFS=$old_ifs
    [ "$file_type" = "regular file" ] || fail "$label" "path is not a regular file"
    [ "$owner_uid" = "$EXPECTED_UID" ] || fail "$label" "file owner is invalid"
    [ "$owner_gid" = "$EXPECTED_GID" ] || fail "$label" "file group is invalid"
    [ "$permissions" = "$EXPECTED_FILE_MODE" ] || fail "$label" "file permissions are invalid"
    [ "$link_count" = 1 ] || fail "$label" "file has multiple hard links"
    [ "$file_size" -gt 0 ] 2>/dev/null || fail "$label" "file size is invalid"
    [ "$file_size" -le "$maximum" ] 2>/dev/null || fail "$label" "file size is invalid"
    reject_extended_acl "$label" "$source_path"
    file_snapshot=$identity:$file_size:$modified:$changed
}

validate_reference() {
    platform=$1
    reference=$2
    if [ -z "$reference" ]; then
        return
    fi
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
}

assign_reference() {
    platform=$1
    reference=$2
    seen_flag=$3
    [ "$seen_flag" = 0 ] || fail "$platform" "mapping key is duplicated"
    validate_reference "$platform" "$reference"
}

parse_mapping() {
    before_snapshot=$file_snapshot
    if ! { exec 3< "$VDC_COOKIE_MAPPING_FILE"; } 2>/dev/null; then
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
    read_file_metadata mapping "$VDC_COOKIE_MAPPING_FILE" "$MAX_MAPPING_BYTES"
    [ "$file_snapshot" = "$before_snapshot" ] || fail mapping "mapping changed during validation"
}

reject_unexpected_entries() {
    label=$1
    directory=$2
    allowed_next=$3
    if [ "$allowed_next" = 1 ]; then
        unexpected=$(find "$directory" -mindepth 1 -maxdepth 1 \
            ! -name cookies.txt ! -name cookies.txt.next -print -quit 2>/dev/null) || \
            fail "$label" "directory entries cannot be inspected"
    else
        unexpected=$(find "$directory" -mindepth 1 -maxdepth 1 \
            ! -name cookies.txt -print -quit 2>/dev/null) || \
            fail "$label" "directory entries cannot be inspected"
    fi
    [ -z "$unexpected" ] || fail "$label" "directory contains an unexpected entry"
}

validate_platform() {
    platform=$1
    reference=$2
    platform_directory=$VDC_COOKIE_SOURCE_ROOT/$platform

    if [ "$VDC_COOKIE_STAGING_PLATFORM" = "$platform" ] && \
        [ -z "$reference" ]; then
        fail "$platform" "staging requires a mapped platform"
    fi
    if [ -z "$reference" ] && [ ! -e "$platform_directory" ] && \
        [ ! -L "$platform_directory" ]; then
        return
    fi
    read_directory_metadata "$platform" "$platform_directory" \
        "$EXPECTED_UID" "$EXPECTED_GID" "$EXPECTED_DIRECTORY_MODE"

    allowed_next=0
    source_path=$platform_directory/cookies.txt
    if [ "$VDC_COOKIE_STAGING_PLATFORM" = "$platform" ]; then
        source_path=$platform_directory/cookies.txt.next
        allowed_next=1
    fi
    reject_unexpected_entries "$platform" "$platform_directory" "$allowed_next"

    if [ -z "$reference" ]; then
        [ ! -e "$source_path" ] && [ ! -L "$source_path" ] || \
            fail "$platform" "unmapped source is present"
        return
    fi
    read_file_metadata "$platform" "$source_path" "$MAX_COOKIE_BYTES"
    case " $seen_identities " in
        *" $identity "*) fail "$platform" "source is shared across platforms" ;;
    esac
    seen_identities="$seen_identities $identity"
    case "$platform" in
        x)
            x_validated_path=$source_path
            x_validated_snapshot=$file_snapshot
            ;;
        youtube)
            youtube_validated_path=$source_path
            youtube_validated_snapshot=$file_snapshot
            ;;
        bilibili)
            bilibili_validated_path=$source_path
            bilibili_validated_snapshot=$file_snapshot
            ;;
        douyin)
            douyin_validated_path=$source_path
            douyin_validated_snapshot=$file_snapshot
            ;;
    esac
}

revalidate_source_snapshot() {
    platform=$1
    source_path=$2
    expected_snapshot=$3
    [ -n "$source_path" ] || return 0
    read_file_metadata "$platform" "$source_path" "$MAX_COOKIE_BYTES"
    [ "$file_snapshot" = "$expected_snapshot" ] || \
        fail "$platform" "source changed during validation"
}

require_tool find
require_tool getfacl
require_tool realpath
require_tool stat

: "${VDC_COOKIE_SOURCE_ROOT:?set the Cookie source root}"
: "${VDC_COOKIE_MAPPING_FILE:?set the Cookie mapping file}"
: "${VDC_DATA_ROOT:?set the application data root}"
: "${VDC_SOCKET_ROOT:?set the Unix socket root}"
VDC_COOKIE_STAGING_PLATFORM=${VDC_COOKIE_STAGING_PLATFORM:-}
case "$VDC_COOKIE_STAGING_PLATFORM" in
    ""|x|youtube|bilibili|douyin) ;;
    *) fail staging "platform is unsupported" ;;
esac

source_root_parent=${VDC_COOKIE_SOURCE_ROOT%/*}
[ -n "$source_root_parent" ] || source_root_parent=/
validate_root_owned_ancestor_chain root-ancestor "$source_root_parent"
read_directory_metadata root-parent "$source_root_parent" \
    "$EXPECTED_PARENT_UID" "$EXPECTED_PARENT_GID" "$EXPECTED_PARENT_MODE"
read_directory_metadata root "$VDC_COOKIE_SOURCE_ROOT" \
    "$EXPECTED_ROOT_UID" "$EXPECTED_ROOT_GID" "$EXPECTED_ROOT_MODE"
data_root_parent=${VDC_DATA_ROOT%/*}
[ -n "$data_root_parent" ] || data_root_parent=/
validate_root_owned_ancestor_chain data-ancestor "$data_root_parent"
read_directory_metadata data-root "$VDC_DATA_ROOT" \
    "$EXPECTED_DATA_UID" "$EXPECTED_DATA_GID" "$EXPECTED_DATA_MODE"
socket_root_parent=${VDC_SOCKET_ROOT%/*}
[ -n "$socket_root_parent" ] || socket_root_parent=/
validate_root_owned_ancestor_chain socket-ancestor "$socket_root_parent"
read_directory_metadata socket-root "$VDC_SOCKET_ROOT" \
    "$EXPECTED_SOCKET_UID" "$EXPECTED_SOCKET_GID" "$EXPECTED_SOCKET_MODE"
script_path=$(realpath -e -- "$0" 2>/dev/null) || \
    fail repository "validator identity is unavailable"
script_directory=${script_path%/*}
deployment_directory=${script_directory%/*}
repository_root=${deployment_directory%/*}
canonical_path repository "$repository_root"
for protected_root in "$VDC_DATA_ROOT" "$VDC_SOCKET_ROOT" "$repository_root"; do
    if paths_overlap "$VDC_COOKIE_SOURCE_ROOT" "$protected_root" || \
        paths_overlap "$VDC_COOKIE_MAPPING_FILE" "$protected_root"; then
        fail isolation "Cookie deployment path overlaps a protected root"
    fi
done
unexpected_root=$(find "$VDC_COOKIE_SOURCE_ROOT" -mindepth 1 -maxdepth 1 \
    ! -name x ! -name youtube ! -name bilibili ! -name douyin \
    -print -quit 2>/dev/null) || fail root "directory entries cannot be inspected"
[ -z "$unexpected_root" ] || fail root "directory contains an unexpected entry"
case "$VDC_COOKIE_MAPPING_FILE" in
    "$VDC_COOKIE_SOURCE_ROOT"/*) fail mapping "mapping must stay outside source root" ;;
esac
mapping_parent=${VDC_COOKIE_MAPPING_FILE%/*}
[ -n "$mapping_parent" ] || mapping_parent=/
mapping_parent_parent=${mapping_parent%/*}
[ -n "$mapping_parent_parent" ] || mapping_parent_parent=/
validate_root_owned_ancestor_chain mapping-ancestor "$mapping_parent_parent"
read_directory_metadata mapping-parent "$mapping_parent_parent" \
    "$EXPECTED_PARENT_UID" "$EXPECTED_PARENT_GID" "$EXPECTED_PARENT_MODE"
read_directory_metadata mapping "$mapping_parent" \
    "$EXPECTED_UID" "$EXPECTED_GID" "$EXPECTED_DIRECTORY_MODE"
read_file_metadata mapping "$VDC_COOKIE_MAPPING_FILE" "$MAX_MAPPING_BYTES"
parse_mapping

validate_platform x "$x_ref"
validate_platform youtube "$youtube_ref"
validate_platform bilibili "$bilibili_ref"
validate_platform douyin "$douyin_ref"

revalidate_source_snapshot x "$x_validated_path" "$x_validated_snapshot"
revalidate_source_snapshot youtube \
    "$youtube_validated_path" "$youtube_validated_snapshot"
revalidate_source_snapshot bilibili \
    "$bilibili_validated_path" "$bilibili_validated_snapshot"
revalidate_source_snapshot douyin \
    "$douyin_validated_path" "$douyin_validated_snapshot"

if [ -n "$VDC_COOKIE_STAGING_PLATFORM" ]; then
    printf '%s\n' "Cookie deployment metadata validated for one same-directory staging source."
else
    printf '%s\n' "Cookie deployment metadata validated; configured sources remain optional."
fi
