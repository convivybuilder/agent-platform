-- THE DELIVERED URL GOES WHERE THIS FILE SAYS AND NOWHERE ELSE. The directory is
-- decided here and passed down as an argument, so nothing outside this file can
-- move it.
--
-- An earlier version read it from `HEADLESS_BROWSER_STATE` so the tests could
-- point it at a scratch directory, and justified that by saying a URL arriving
-- from LaunchServices carries no environment. That is true of the AppleEvent and
-- false of the process: `launchctl setenv NAME VALUE` writes into the per-session
-- launchd registry that every subsequently-launched GUI app reads, so any process
-- running as this user could have redirected every future OAuth code out of this
-- log with one call, no filesystem write and nothing restarted. It would also
-- have been silent, since a failed write here raises nothing anybody sees, which
-- is the "believes it worked" failure this handler exists to prevent.
--
-- The comment it carried claimed parity with the watchdog's own overridable
-- state. There is none: that process is started by launchd from a plist whose
-- EnvironmentVariables dict is what populates its environment, so moving it takes
-- a filesystem write to a LaunchAgent, which is a higher bar and leaves something
-- behind to find.
--
-- Testability did not need any of that. `record_to` takes the directory as a
-- parameter, the tests call it directly with a scratch path, and the deployed
-- path reads no environment at all.
on open location this_URL
	record_to(this_URL, (POSIX path of (path to home folder)) & ".local/state/headless-browser")
end open location

on record_to(this_URL, logDir)
	set logPath to logDir & "/urls.log"
	set ts to do shell script "date '+%Y-%m-%dT%H:%M:%S'"

	-- 700 on the directory and 600 on the file, because of what a line in here
	-- is. The URLs this records are the ones an application asked the system to
	-- open, and the reason this handler exists at all is Claude Code's OAuth
	-- flow: an authorization callback carries a code and a state parameter in
	-- its query string. That is a credential sitting in a text file.
	-- `umask 077` rather than a chmod afterwards, so the file is never briefly
	-- readable between being created and being tightened. The directory being
	-- 700 already covers that window today, which is exactly what makes it easy
	-- to reopen by reordering these lines later.
	--
	-- The truncation keeps only the most recent entries. Unbounded growth on a
	-- machine nobody watches is the same failure this whole change exists to
	-- fix, and it would keep every one-time code ever delivered here for the
	-- life of the machine. 200 lines is far more than anybody reads while
	-- logging in.
	--
	-- APPEND AND TRUNCATE UNDER ONE LOCK. A unique temp name stops two
	-- truncations clobbering each other but not the worse case: one handler
	-- appends a URL after another has already run `tail`, and that other one's
	-- rename then throws the new entry away. A lost line here is a login that
	-- cannot be completed, since this file is the only place the link appears.
	-- `mkdir` is the lock because macOS has no `flock`.
	--
	-- Waiting is bounded and giving up proceeds ANYWAY. A handler that cannot
	-- take the lock within five seconds writes the URL regardless: racing the
	-- truncation risks one line, and refusing to write loses the link outright,
	-- which is the failure that actually strands somebody.
	--
	-- IT RELEASES ONLY THE LOCK IT STILL OWNS, which is a stronger thing than
	-- releasing only a lock it once took, and the difference is a whole bug.
	-- Releasing unconditionally let a handler that gave up waiting remove a
	-- working handler's lock; reproduced in review as real loss, two URLs
	-- written and one survived, because the slow holder's rename put back a copy
	-- of the file it had read before the other handler appended.
	--
	-- Tracking "did I create it" fixes that door and leaves another open: a
	-- handler still working past the sixty-second staleness window has already
	-- had its lock renamed away by a successor, so the path now names somebody
	-- else's lock and releasing on a remembered boolean deletes theirs. The
	-- token is what closes it, the same way the watchdog compares the pid it
	-- wrote before its trap removes anything.
	--
	-- AND AN ORPHAN IS RECOVERED BY AGE, because the unconditional release was
	-- accidentally doing that job. A handler killed mid-write would otherwise
	-- leave a lock nobody removes, and every later handler would wait the full
	-- five seconds before writing unprotected — for the rest of the machine's
	-- life. Sixty seconds is far longer than appending a line and rewriting 200,
	-- and the takeover is a rename so that exactly one handler can win it.
	-- Freeing and acquiring are one step: clearing a stale lock on the last
	-- retry and only then running out of budget would leave this handler
	-- writing unprotected having just cleared the way for somebody else.
	do shell script "umask 077; d=" & quoted form of logDir & "; f=" & quoted form of logPath & ¬
		"; mkdir -p \"$d\"; chmod 700 \"$d\"" & ¬
		"; i=0; held=0; while [ $i -lt 50 ]; do" & ¬
		"   if mkdir \"$d/.lock\" 2>/dev/null; then held=1; echo $$ > \"$d/.lock/owner\"; break; fi;" & ¬
		"   age=$(( $(date +%s) - $(stat -f %m \"$d/.lock\" 2>/dev/null || date +%s) ));" & ¬
		"   if [ $age -gt 60 ] && mv \"$d/.lock\" \"$d/.stale.$$\" 2>/dev/null; then rm -rf \"$d/.stale.$$\";" & ¬
		"     if mkdir \"$d/.lock\" 2>/dev/null; then held=1; echo $$ > \"$d/.lock/owner\"; break; fi; fi;" & ¬
		"   i=$((i+1)); sleep 0.1; done" & ¬
		"; printf '%s  %s\\n' " & quoted form of ts & " " & quoted form of this_URL & " >> \"$f\"" & ¬
		"; t=$(mktemp \"$d/urls.XXXXXX\"); tail -n 200 \"$f\" > \"$t\" && mv \"$t\" \"$f\" || rm -f \"$t\"" & ¬
		"; if [ \"$held\" = 1 ] && [ \"$(cat \"$d/.lock/owner\" 2>/dev/null)\" = \"$$\" ]; then rm -rf \"$d/.lock\"; fi"
end record_to

on run
	do shell script "true"
end run
