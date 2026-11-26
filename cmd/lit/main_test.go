package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// build compiles the binary once per test run.
func build(t *testing.T) string {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "lit")
	out, err := exec.Command("go", "build", "-o", bin, ".").CombinedOutput()
	if err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	return bin
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestVersion(t *testing.T) {
	out, err := exec.Command(build(t), "version").Output()
	if err != nil || !strings.HasPrefix(string(out), "lit ") || !strings.Contains(string(out), "embedded python") {
		t.Fatalf("%v %q", err, out)
	}
}

// TestRunsTodaysLit drives a real batch through the embedded Python and checks
// that nothing was written next to the binary's source or into the checkout.
func TestRunsTodaysLit(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not installed")
	}
	bin := build(t)
	work := t.TempDir()
	src := filepath.Join(work, "src")
	os.Mkdir(src, 0o755)
	git(t, src, "init", "-q", "-b", "main")
	os.WriteFile(filepath.Join(src, "a.txt"), []byte("a\n"), 0o644)
	git(t, src, "add", ".")
	git(t, src, "commit", "-qm", "base")
	base := git(t, src, "rev-parse", "HEAD")
	git(t, src, "checkout", "-qb", "pr")
	os.WriteFile(filepath.Join(src, "b.txt"), []byte("b\n"), 0o644)
	git(t, src, "add", ".")
	git(t, src, "commit", "-qm", "add b")
	head := git(t, src, "rev-parse", "HEAD")
	git(t, work, "clone", "-q", "--bare", src, "batch.git")

	snapshot, _ := json.Marshal(map[string]any{
		"repository": "example/repo", "branch": "main", "base": base,
		"changes": []map[string]any{{"number": 1, "author": "octocat", "head": head}},
	})
	os.WriteFile(filepath.Join(work, "queue.json"), snapshot, 0o644)

	before := tree(t, "../..")
	cmd := exec.Command(bin, "run", "queue.json", "--repo", "batch.git", "--state", "state",
		"--test", "test", "-f", "b.txt")
	cmd.Dir = work
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("lit run: %v\n%s", err, out)
	}
	receipt := strings.TrimSpace(string(out))
	var result struct{ Tested []int }
	data, err := os.ReadFile(receipt[strings.LastIndex(receipt, "\n")+1:])
	if err != nil || json.Unmarshal(data, &result) != nil || len(result.Tested) != 1 {
		t.Fatalf("receipt %q: %v %s", receipt, err, data)
	}
	if after := tree(t, "../.."); after != before {
		t.Fatal("lit wrote into its own source tree (bytecode or other files)")
	}
}

// tree lists every file under root except .git, for a before/after comparison.
func tree(t *testing.T, root string) string {
	t.Helper()
	var b strings.Builder
	filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && d.Name() == ".git" {
			return filepath.SkipDir
		}
		b.WriteString(p + "\n")
		return nil
	})
	return b.String()
}
