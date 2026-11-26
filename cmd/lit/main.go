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
	"strings"
	"syscall"

	lit "github.com/CakeRepository/lit"
	"github.com/CakeRepository/lit/internal/handback"
	"github.com/CakeRepository/lit/internal/parity"
	"github.com/CakeRepository/lit/internal/pyfallback"
)

// version is set at release time with -ldflags "-X main.version=v0.1.0".
var version = "dev"

func main() { os.Exit(run(os.Args[1:])) }

// native holds the commands written in Go. Every other command runs on the
// embedded Python. A command joins this table once its parity cases match.
var native = map[string]func(args []string) int{}

func run(args []string) int {
	if len(args) == 1 && (args[0] == "version" || args[0] == "--version") {
		fmt.Println(versionLine())
		return 0
	}
	if len(args) > 0 && args[0] == "parity" {
		return parityCommand(args[1:])
	}
	// LIT_IMPL=python forces the Python lit; lit parity sets it on one side.
	if len(args) > 0 && os.Getenv("LIT_IMPL") != "python" {
		if cmd, ok := native[args[0]]; ok {
			return cmd(args[1:])
		}
	}
	return fallback(args)
}

const parityUsage = `usage: lit parity [--ignore KEY]... [--epoch N] [--keep] [--] COMMAND [ARGS...]

Runs a lit command with the Python lit and with the Go lit, each in its own
copy of the current folder, and fails if they differ in exit code, output,
files written or Git refs. A command not in Go yet runs on Python both times,
which checks that it gives the same result twice.`

func parityCommand(args []string) int {
	opts := parity.Options{Epoch: "1700000000"}
	keep := false
	for len(args) > 0 && strings.HasPrefix(args[0], "-") {
		switch flag := args[0]; {
		case flag == "--":
			args = args[1:]
			goto done
		case flag == "--keep":
			keep, args = true, args[1:]
		case (flag == "--ignore" || flag == "--epoch") && len(args) > 1:
			if flag == "--ignore" {
				opts.Ignore = append(opts.Ignore, args[1])
			} else {
				opts.Epoch = args[1]
			}
			args = args[2:]
		default:
			fmt.Fprintln(os.Stderr, parityUsage)
			return 2
		}
	}
done:
	if len(args) == 0 || args[0] == "parity" {
		fmt.Fprintln(os.Stderr, parityUsage)
		return 2
	}
	self, err := os.Executable()
	if err == nil {
		opts.Source, err = os.Getwd()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "lit parity:", err)
		return 2
	}
	opts.Self, opts.Args = self, args
	_, opts.Go = native[args[0]]
	report, err := parity.Run(opts)
	if err != nil {
		fmt.Fprintln(os.Stderr, "lit parity:", err)
		if report != nil {
			fmt.Fprintln(os.Stderr, "lit parity: both runs are in", report.Work)
		}
		return 2
	}
	if len(report.Diffs) > 0 {
		for _, d := range report.Diffs {
			fmt.Println(d)
		}
		fmt.Printf("lit parity: %s differs in %d ways; both runs are in %s\n", args[0], len(report.Diffs), report.Work)
		return 1
	}
	if keep {
		fmt.Println("lit parity: both runs are in", report.Work)
	} else {
		os.RemoveAll(report.Work)
	}
	if opts.Go {
		fmt.Printf("lit parity: %s matches between Python and Go (exit %d)\n", args[0], report.Python.Code)
	} else {
		fmt.Printf("lit parity: %s is not in Go yet, so Python ran twice; the runs match (exit %d)\n", args[0], report.Python.Code)
	}
	return 0
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
