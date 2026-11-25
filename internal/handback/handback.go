// Package handback gives files that lit created as root back to the user who
// owns the folder they were written into.
//
// A GitHub container job runs as root, but its workspace and temp folders
// belong to the runner's user on the host. Anything root leaves there cannot be
// removed by the runner, and the next job on that runner fails before it
// starts. lit never writes into the action folder; this covers the paths an
// operator names on the command line (--state, --repo, --out, --receipt, ...).
package handback

import (
	"path/filepath"
	"strings"
	"time"
)

// Session marks when a command started, so only entries changed during the
// command are handed back.
type Session struct {
	start time.Time
}

// Begin starts a session.
func Begin() *Session { return &Session{start: time.Now().Add(-2 * time.Second)} }

// skipValue lists options whose value is a folder lit works in rather than one it
// writes its own files into: the job's checkout, and the folder inside it.
var skipValue = map[string]bool{"--workdir": true, "--cwd": true}

// stop lists options after which every argument belongs to a command lit runs.
var stop = map[string]bool{"--test": true}

// Paths picks the arguments that may name files lit wrote: plain arguments and
// option values, up to --test.
func Paths(args []string) []string {
	var out []string
	skip := false
	for _, a := range args {
		if stop[a] {
			break
		}
		if skip {
			skip = false
			continue
		}
		if strings.HasPrefix(a, "-") {
			name, value, ok := strings.Cut(a, "=")
			if skipValue[name] {
				skip = !ok
				continue
			}
			if ok && value != "" {
				out = append(out, value)
			}
			continue
		}
		out = append(out, a)
	}
	clean := out[:0]
	for _, p := range out {
		abs, err := filepath.Abs(p)
		if err != nil || abs == filepath.Dir(abs) {
			continue // never walk a filesystem root
		}
		clean = append(clean, abs)
	}
	return clean
}
