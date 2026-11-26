package pyfallback

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"testing/fstest"
)

var sample = fstest.MapFS{
	"concurrent_merge/__init__.py":         {Data: []byte("")},
	"concurrent_merge/cli.py":              {Data: []byte("def main():\n    return 0\n")},
	"concurrent_merge/readers/__init__.py": {Data: []byte("")},
}

func TestExtractReusesAnIntactCopy(t *testing.T) {
	base := t.TempDir()
	dir, err := Extract(sample, base)
	if err != nil {
		t.Fatal(err)
	}
	stamp := filepath.Join(dir, "concurrent_merge", "cli.py")
	before, _ := os.Stat(stamp)
	again, err := Extract(sample, base)
	if err != nil || again != dir {
		t.Fatalf("second extract: %v %q", err, again)
	}
	after, _ := os.Stat(stamp)
	if !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("an intact copy was rewritten")
	}
	entries, _ := os.ReadDir(base)
	if len(entries) != 1 {
		t.Fatalf("left scratch folders behind: %v", entries)
	}
}

func TestExtractReplacesAChangedCopy(t *testing.T) {
	base := t.TempDir()
	dir, err := Extract(sample, base)
	if err != nil {
		t.Fatal(err)
	}
	cli := filepath.Join(dir, "concurrent_merge", "cli.py")
	if err := os.WriteFile(cli, []byte("import os; os.system('evil')\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Bytecode or any other stray file also counts as a change.
	if err := os.WriteFile(filepath.Join(dir, "concurrent_merge", "extra.pyc"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Extract(sample, base); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(cli)
	if string(got) != "def main():\n    return 0\n" {
		t.Fatalf("tampered copy kept: %q", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "concurrent_merge", "extra.pyc")); !os.IsNotExist(err) {
		t.Fatal("stray file kept")
	}
}

func TestExtractRefusesAFolderOthersCanWrite(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("per-user temp folder")
	}
	base := t.TempDir()
	dir, err := Extract(sample, base)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o777); err != nil {
		t.Fatal(err)
	}
	if ok, _ := intact(dir, mustDigest(t)); ok {
		t.Fatal("a world-writable copy passed")
	}
	if _, err := Extract(sample, base); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(dir)
	if info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("fresh copy is %v", info.Mode().Perm())
	}
}

func TestDigestCoversNamesAndBytes(t *testing.T) {
	a := mustDigest(t)
	renamed := fstest.MapFS{
		"concurrent_merge/__init__.py":         sample["concurrent_merge/__init__.py"],
		"concurrent_merge/cli2.py":             sample["concurrent_merge/cli.py"],
		"concurrent_merge/readers/__init__.py": sample["concurrent_merge/readers/__init__.py"],
	}
	b, err := Digest(renamed)
	if err != nil || a == b {
		t.Fatalf("digest ignores names: %v", err)
	}
}

func mustDigest(t *testing.T) string {
	t.Helper()
	d, err := Digest(sample)
	if err != nil {
		t.Fatal(err)
	}
	return d
}
