#!/bin/sh
# Install Ookla Speedtest CLI from the project-maintained signed opkg feed.
set -eu

feed_url="${OOKLA_FEED_URL:-https://keithah.github.io/openwrt-packages}"
target_root="${OOKLA_ROOT:-/}"
feeds_file="$target_root/etc/opkg/customfeeds.conf"
keys_dir="$target_root/etc/opkg/keys"
feed_key_file="$keys_dir/f6c72c675c844b91"
feed_key='untrusted comment: Keith OpenWrt package feed
RWT2xyxnXIRLkZzbs1HvD+48GPkSqoNPCZVCOw49GUdTg2O7Cv9LzMtx'

fail() {
	printf 'ookla-speedtest-cli installer: %s\n' "$*" >&2
	exit 1
}

[ "$(id -u)" = 0 ] || fail 'must be run as root'
command -v opkg >/dev/null 2>&1 || fail 'opkg is required'
command -v wget >/dev/null 2>&1 || fail 'wget is required'

if ! architectures=$(opkg print-architecture); then
	fail 'could not determine package architectures'
fi
if ! printf '%s\n' "$architectures" |
	awk '$2 == "aarch64_cortex-a53" { found = 1 } END { exit !found }'; then
	fail 'this installer requires aarch64_cortex-a53'
fi

[ -d "$target_root/etc/opkg" ] || fail "missing $target_root/etc/opkg"
[ -d "$keys_dir" ] || fail "missing $keys_dir"
[ -f "$feeds_file" ] || : >"$feeds_file"

feeds_dir=$(dirname "$feeds_file")
tmp_file=''
trim_file=''
key_tmp=''

cleanup() {
	[ -z "$tmp_file" ] || rm -f "$tmp_file" || :
	[ -z "$trim_file" ] || rm -f "$trim_file" || :
	[ -z "$key_tmp" ] || rm -f "$key_tmp" || :
}

handle_signal() {
	status=$1
	trap - 0 HUP INT TERM
	cleanup
	exit "$status"
}

trap 'cleanup' 0
trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

tmp_file=$(mktemp "$feeds_dir/.customfeeds.conf.XXXXXX")
trim_file=$(mktemp "$feeds_dir/.customfeeds.conf.trim.XXXXXX")
key_tmp=$(mktemp "$keys_dir/.keithah-key.XXXXXX")

printf '%s\n' "$feed_key" >"$key_tmp"
chmod 0644 "$key_tmp"
mv "$key_tmp" "$feed_key_file"

# Preserve every unrelated record while replacing managed feed entries with one.
# The neutral entry comes first so an unrelated unterminated final record can
# remain at EOF without joining the new entry.
printf 'src/gz keithah %s\n' "$feed_url" >"$tmp_file"
awk '$1 == "src/gz" && ($2 == "starwatch" || $2 == "wattline" || $2 == "keithah") { next } { print }' \
	"$feeds_file" >>"$tmp_file"

# awk terminates every emitted record. If the original final record was both
# retained and unterminated, remove only that synthesized final newline.
input_size=$(wc -c <"$feeds_file")
trim_final_newline=no
if [ "$input_size" -gt 0 ]; then
	dd if="$feeds_file" of="$trim_file" bs=1 skip=$((input_size - 1)) count=1 2>/dev/null
	last_byte_newlines=$(wc -l <"$trim_file")
	if [ "$last_byte_newlines" -eq 0 ] &&
		awk 'END { exit ($1 == "src/gz" && ($2 == "starwatch" || $2 == "wattline" || $2 == "keithah")) }' \
			"$feeds_file"; then
		trim_final_newline=yes
	fi
fi
if [ "$trim_final_newline" = yes ]; then
	output_size=$(wc -c <"$tmp_file")
	dd if="$tmp_file" of="$trim_file" bs=1 count=$((output_size - 1)) 2>/dev/null
	mv "$trim_file" "$tmp_file"
else
	rm -f "$trim_file"
fi

if metadata=$(stat -c '%a %u %g' "$feeds_file" 2>/dev/null); then
	set -- $metadata
	chmod "$1" "$tmp_file"
	chown "$2:$3" "$tmp_file" 2>/dev/null || fail 'could not preserve feed file ownership'
elif metadata=$(stat -f '%Lp %u %g' "$feeds_file" 2>/dev/null); then
	set -- $metadata
	chmod "$1" "$tmp_file"
	chown "$2:$3" "$tmp_file" 2>/dev/null || fail 'could not preserve feed file ownership'
fi
mv "$tmp_file" "$feeds_file"
trap - 0 HUP INT TERM

opkg update
opkg install ookla-speedtest-cli

printf '%s\n' 'Installed Ookla Speedtest CLI. Run speedtest to review and accept the Ookla EULA.'
