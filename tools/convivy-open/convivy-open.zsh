# Convivy open helpers — add to ~/.zshrc:  source ~/.local/bin/convivy-open.zsh
# These wrap the `convivy-open` CLI so a path the bot posted can drop you
# straight into the file/dir from inside an SSH/tmux session.

# cve <ref> — open the resolved file in $EDITOR
cve() { convivy-open -e "$1"; }

# cvd <ref> — cd into the resolved file's directory (a child process can't
#             change the parent shell's cwd, so this must be a function)
cvd() { local d; d="$(convivy-open -d "$1")" || return; cd "$d"; }

# cvl <ref> — page the resolved file
cvl() { convivy-open -l "$1"; }
