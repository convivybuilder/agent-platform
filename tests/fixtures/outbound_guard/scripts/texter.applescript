-- test fixture for outbound-guard: never run
tell application "Messages"
	send "hello" to buddy "+15555550123"
end tell
