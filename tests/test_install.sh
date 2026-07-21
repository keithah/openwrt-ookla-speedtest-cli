#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
installer="$repo_root/scripts/install.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' 0 HUP INT TERM

fail() {
	printf 'test_install: %s\n' "$*" >&2
	exit 1
}

[ -f "$installer" ] || fail "missing installer: $installer"

assert_line() {
	awk -v wanted="$2" '$0 == wanted { found = 1 } END { exit !found }' "$1" ||
		fail "missing line '$2' in $1"
}

assert_count() {
	actual=$(awk -v pattern="$2" '$0 ~ pattern { count++ } END { print count + 0 }' "$1")
	[ "$actual" -eq "$3" ] || fail "expected $3 matches for '$2' in $1, got $actual"
}

make_case() {
	case_root="$tmp/$1"
	mkdir -p "$case_root/bin" "$case_root/root/etc/opkg/keys"
	printf '%s' "$2" >"$case_root/root/etc/opkg/customfeeds.conf"
	: >"$case_root/commands"

	cat >"$case_root/bin/id" <<'EOF'
#!/bin/sh
[ "$1" = -u ] || exit 1
printf '%s\n' "${FAKE_UID:-0}"
EOF
	cat >"$case_root/bin/opkg" <<'EOF'
#!/bin/sh
printf 'opkg %s\n' "$*" >>"$FAKE_COMMAND_LOG"
if [ "$1" = print-architecture ]; then
	printf '%s\n' "${FAKE_ARCHITECTURES:-arch all 1}"
fi
EOF
	cat >"$case_root/bin/wget" <<'EOF'
#!/bin/sh
printf 'wget %s\n' "$*" >>"$FAKE_COMMAND_LOG"
EOF
	chmod +x "$case_root/bin/id" "$case_root/bin/opkg" "$case_root/bin/wget"
}

run_case() {
	case_root="$tmp/$1"
	shift
	env -i \
		PATH="$case_root/bin:$PATH" \
		OOKLA_ROOT="$case_root/root" \
		FAKE_COMMAND_LOG="$case_root/commands" \
		FAKE_UID=0 \
		FAKE_ARCHITECTURES='arch aarch64_cortex-a53 10' \
		"$@" /bin/sh "$installer"
}

expect_failure() {
	if "$@" >"$tmp/failure.out" 2>"$tmp/failure.err"; then
		fail "command unexpectedly succeeded: $*"
	fi
}

base_feeds='src/gz core https://downloads.example/core
# keep this comment and spacing
src/gz starwatch https://legacy.example/one
src/gz keithah https://legacy.example/two
src/gz extras https://downloads.example/extras
src/gz wattline https://legacy.example/three
src/gz keithah https://legacy.example/four
'

# Unsupported architectures must fail before writing either configuration or key.
make_case unsupported "$base_feeds"
chmod 0640 "$tmp/unsupported/root/etc/opkg/customfeeds.conf"
cp "$tmp/unsupported/root/etc/opkg/customfeeds.conf" "$tmp/unsupported/original"
expect_failure run_case unsupported FAKE_ARCHITECTURES='arch all 1'
cmp -s "$tmp/unsupported/original" "$tmp/unsupported/root/etc/opkg/customfeeds.conf" ||
	fail 'architecture rejection changed customfeeds.conf'
[ ! -e "$tmp/unsupported/root/etc/opkg/keys/f6c72c675c844b91" ] ||
	fail 'architecture rejection installed a key'
assert_line "$tmp/unsupported/commands" 'opkg print-architecture'
assert_count "$tmp/unsupported/commands" '^opkg update$' 0
assert_count "$tmp/unsupported/commands" '^opkg install ' 0

# Successful installation migrates only managed aliases and installs one package.
make_case supported "$base_feeds"
chmod 0640 "$tmp/supported/root/etc/opkg/customfeeds.conf"
run_case supported OOKLA_FEED_URL='https://feed.example/packages'
feeds="$tmp/supported/root/etc/opkg/customfeeds.conf"
assert_line "$feeds" 'src/gz core https://downloads.example/core'
assert_line "$feeds" '# keep this comment and spacing'
assert_line "$feeds" 'src/gz extras https://downloads.example/extras'
assert_line "$feeds" 'src/gz keithah https://feed.example/packages'
assert_count "$feeds" '^src/gz starwatch ' 0
assert_count "$feeds" '^src/gz wattline ' 0
assert_count "$feeds" '^src/gz keithah ' 1
[ "$(stat -c '%a' "$feeds")" = 640 ] || fail 'customfeeds.conf mode changed'

expected_key="$tmp/expected-key"
cat >"$expected_key" <<'EOF'
untrusted comment: Keith OpenWrt package feed
RWT2xyxnXIRLkZzbs1HvD+48GPkSqoNPCZVCOw49GUdTg2O7Cv9LzMtx
EOF
key="$tmp/supported/root/etc/opkg/keys/f6c72c675c844b91"
cmp -s "$expected_key" "$key" || fail 'installed public key differs from approved key'
assert_line "$tmp/supported/commands" 'opkg update'
assert_line "$tmp/supported/commands" 'opkg install ookla-speedtest-cli'
assert_count "$tmp/supported/commands" '^opkg install ' 1

# Repeating the installer must leave the managed files byte-for-byte unchanged.
cp "$feeds" "$tmp/feeds-after-first-run"
cp "$key" "$tmp/key-after-first-run"
run_case supported OOKLA_FEED_URL='https://feed.example/packages'
cmp -s "$tmp/feeds-after-first-run" "$feeds" || fail 'second run changed feed configuration'
cmp -s "$tmp/key-after-first-run" "$key" || fail 'second run changed public key'
assert_count "$feeds" '^src/gz keithah ' 1
assert_count "$tmp/supported/commands" '^opkg update$' 2
assert_count "$tmp/supported/commands" '^opkg install ookla-speedtest-cli$' 2
assert_count "$tmp/supported/commands" '^opkg install ' 2

printf '%s\n' 'installer tests passed'
