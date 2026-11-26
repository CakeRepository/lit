// Command lit is lit as one binary.
//
// Commands move from Python to Go one at a time (CakeRepository/lit#35). A
// command that has not moved yet runs the Python lit embedded in this binary,
// so the binary already does everything concurrent-merge does.
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"runtime/debug"
	"syscall"

	lit "github.com/CakeRepository/lit"
	"github.com/CakeRepository/lit/internal/handback"
	"github.com/CakeRepository/lit/internal/pyfallback"
)

// version is set at release time with -ldflags "-X main.version=v0.1.0".
var version = "dev"

func main() { os.Exit(run(os.Args[1:])) }

func run(args []string) int {
	if len(args) == 1 && (args[0] == "version" || args[0] == "--version") {
		fmt.Println(versionLine())
		return 0
	}
	return fallback(args)
}

func versionLine() string {
	v := version
	if v == "dev" {
		if info, ok := debug.ReadBuildInfo(); ok {
			for _, s := range info.Settings {
				if s.Key == "vcs.revision" && len(s.Value) >= 12 {
					v = "dev-" + s.Value[:12]
				}
			}
		}
	}
	digest, err := pyfallback.Digest(lit.Python)
	if err != nil {
		digest = "unreadable"
	}
	return fmt.Sprintf("lit %s %s/%s (embedded python %s)", v, runtime.GOOS, runtime.GOARCH, digest[:min(12, len(digest))])
}

// fallback runs args with the embedded Python lit.
func fallback(args []string) int {
	session := handback.Begin()
	defer func() {
		if err := session.Finish(handback.Paths(args)); err != nil {
			fmt.Fprintln(os.Stderr, "lit: could not give some files back to their folder's owner:", err)
		}
	}()
	python, err := pyfallback.Python()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	dir, err := pyfallback.Extract(lit.Python, os.TempDir())
	if err != nil {
		fmt.Fprintln(os.Stderr, "lit: could not prepare the embedded Python:", err)
		return 2
	}
	cmd := pyfallback.Command(python, dir, args)
	if err := cmd.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "lit:", err)
		return 2
	}
	// Ctrl-C reaches the whole process group, so the child sees it on its own;
	// a SIGTERM sent to lit alone is passed on. Either way lit waits for the child.
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)
	go func() {
		for s := range signals {
			if s != os.Interrupt && cmd.Process != nil {
				_ = cmd.Process.Signal(s)
			}
		}
	}()
	return exitCode(cmd.Wait())
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		if status, ok := exit.Sys().(syscall.WaitStatus); ok && status.Signaled() {
			return 128 + int(status.Signal())
		}
		return exit.ExitCode()
	}
	fmt.Fprintln(os.Stderr, "lit:", err)
	return 2
}
