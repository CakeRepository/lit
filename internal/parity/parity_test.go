package parity

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// run uses sh in place of lit: the script is the "command" both sides run.
func run(t *testing.T, script string, setup func(dir string)) *Report {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("uses sh")
	}
	src := t.TempDir()
	if setup != nil {
		setup(src)
	}
	r, err := Run(Options{Self: "sh", Source: src, Args: []string{"-c", script}, Epoch: "1700000000"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(r.Work) })
	return r
}

func TestCatchesOutputThatDiffersBetweenRuns(t *testing.T) {
	r := run(t, `mkdir out; od -An -N8 -tx1 /dev/urandom > out/value.txt; od -An -N8 -tx1 /dev/urandom`, nil)
	text := strings.Join(r.Diffs, "\n")
	if !strings.Contains(text, "out/value.txt differs") || !strings.Contains(text, "stdout differs") {
		t.Fatalf("missed a difference: %q", r.Diffs)
	}
}

func TestCatchesAFileOnlyOneSideWrote(t *testing.T) {
	r := run(t, `[ "$(basename "$PWD")" = go ] && echo x > extra.txt; true`, nil)
	if len(r.Diffs) != 1 || !strings.Contains(r.Diffs[0], "extra.txt: only go wrote it") {
		t.Fatalf("%q", r.Diffs)
	}
}

func TestCatchesAnExitCodeThatDiffers(t *testing.T) {
	r := run(t, `[ "$(basename "$PWD")" = go ] && exit 3; exit 0`, nil)
	if len(r.Diffs) != 1 || r.Diffs[0] != "exit code: python 0, go 3" {
		t.Fatalf("%q", r.Diffs)
	}
}

// What differs between any two runs is allowed to: the folder, the random
// part of a run folder, times, durations and test logs.
func TestIgnoresWhatVariesBetweenAnyTwoRuns(t *testing.T) {
	r := run(t, `set -e
		d=state/runs/batch-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
		mkdir -p "$d"
		printf '{"receipt":"%s","started":"%s","seconds":%s,"unit_seconds":%s.5,"tested":[1]}' \
			"$PWD/$d/result.json" "$(date -u +%Y-%m-%dT%H:%M:%S.%N+00:00)" "$$" "$$" > "$d/result.json"
		od -An -N8 -tx1 /dev/urandom > "$d/abc.log"
		echo "$PWD/$d/result.json"`, nil)
	if len(r.Diffs) != 0 {
		t.Fatalf("%q", r.Diffs)
	}
	if _, ok := r.Python.Files["state/runs/batch-1/result.json"]; !ok {
		t.Fatalf("run folder not renamed: %v", r.Python.Files)
	}
	if r.Python.Stdout != "<dir>/state/runs/batch-1/result.json\n" {
		t.Fatalf("%q", r.Python.Stdout)
	}
}

func TestComparesJSONByValueNotLayout(t *testing.T) {
	r := run(t, `if [ "$(basename "$PWD")" = go ]; then echo '{"b":2,"a":1}' > r.json; else printf '{\n "a": 1,\n "b": 2\n}' > r.json; fi`, nil)
	if len(r.Diffs) != 0 {
		t.Fatalf("%q", r.Diffs)
	}
	r = run(t, `if [ "$(basename "$PWD")" = go ]; then echo '{"a":1,"b":3}' > r.json; else echo '{"a":1,"b":2}' > r.json; fi`, nil)
	if len(r.Diffs) != 1 || !strings.Contains(r.Diffs[0], "r.json differs") {
		t.Fatalf("%q", r.Diffs)
	}
}

// Commits made at SOURCE_DATE_EPOCH get the same IDs on both sides, so their
// refs are compared exactly; a commit made at the wall-clock time is caught.
func TestComparesGitRefs(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	setup := func(dir string) {
		cmd := exec.Command("git", "init", "-q", "--bare", filepath.Join(dir, "repo.git"))
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("%v %s", err, out)
		}
	}
	commit := `export GIT_DIR=repo.git GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x
		export GIT_AUTHOR_DATE="@$WHEN +0000" GIT_COMMITTER_DATE="@$WHEN +0000"
		tree=$(git mktree </dev/null)
		git update-ref refs/heads/main "$(echo batch | git commit-tree "$tree")"`
	if r := run(t, `WHEN=$SOURCE_DATE_EPOCH; `+commit, setup); len(r.Diffs) != 0 {
		t.Fatalf("%q", r.Diffs)
	}
	r := run(t, `WHEN=$(od -An -N2 -tu2 /dev/urandom | tr -d ' '); `+commit, setup)
	if len(r.Diffs) != 1 || !strings.Contains(r.Diffs[0], "repo.git/(refs) differs") {
		t.Fatalf("%q", r.Diffs)
	}
}

func TestCopyTreeKeepsSymlinksAndModes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks")
	}
	src, dst := t.TempDir(), filepath.Join(t.TempDir(), "copy")
	os.WriteFile(filepath.Join(src, "run.sh"), []byte("#!/bin/sh\n"), 0o755)
	os.Symlink("run.sh", filepath.Join(src, "link"))
	if err := CopyTree(src, dst); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(dst, "run.sh"))
	if err != nil || info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("%v %v", info, err)
	}
	if link, err := os.Readlink(filepath.Join(dst, "link")); err != nil || link != "run.sh" {
		t.Fatalf("%q %v", link, err)
	}
}
