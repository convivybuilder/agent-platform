# test fixture for outbound-guard: never run
import subprocess

subprocess.run(["osascript", "-e",
                'tell application "Messages" to send "hello" to buddy "+15555550123"'])
