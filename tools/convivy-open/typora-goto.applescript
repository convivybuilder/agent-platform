-- ConvivyTyporaGoto — the System-Events half of convivy-open's line-jump.
--
-- Compiled into ~/Applications/ConvivyTyporaGoto.app by install-laptop.sh.
-- typora-goto (zsh) opens the doc in Typora, writes the search snippet to
-- /tmp/convivy-typora-find, then launches THIS app; the app reads that file,
-- activates Typora, and drives Find (⌘F) to scroll there.
--
-- The snippet comes via a temp file, NOT a launch argument: AppleScript applets
-- don't reliably receive argv from `open --args` (they use Apple Events), so an
-- arg-passed snippet arrived empty and the app returned before ever calling
-- System Events — which is why no Accessibility prompt appeared. Reading a file
-- sidesteps that entirely.
--
-- Living in an app bundle (not the launchd daemon) is what makes Accessibility
-- work: macOS attributes the System-Events control to this app's stable identity.

on run
	set theSnippet to ""
	try
		set theSnippet to (do shell script "/bin/cat /tmp/convivy-typora-find")
	end try
	if theSnippet is "" then return
	tell application "Typora" to activate
	delay 0.3
	tell application "System Events"
		tell process "Typora"
			keystroke "f" using command down
			delay 0.15
			keystroke theSnippet
			delay 0.05
			key code 36
			delay 0.2
			key code 53
		end tell
	end tell
end run
