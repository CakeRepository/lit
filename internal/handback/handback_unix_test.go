//go:build unix

package handback

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func uidOf(t *testing.T, p string) uint32 {
	t.Helper()
	info, err := os.Lstat(p)
	if err != nil {
		t.Fatal(err)
	}
	return info.Sys().(*syscall.Stat_t).Uid
}

func TestFinishGivesRootsNewFilesToTheFolderOwner(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root; CI's container job covers it")
	}
	work := t.TempDir()
	if err := os.Chown(work, 4321, 4321); err != nil {
		t.Fatal(err)
	}
	old := filepath.Join(work, "old-root-file")
	os.WriteFile(old, nil, 0o644)
	past := mustPast(t, old)

	s := Begin()
	state := filepath.Join(work, "state")
	os.MkdirAll(filepath.Join(state, "runs", "batch-1"), 0o755)
	os.WriteFile(filepath.Join(state, "runs", "batch-1", "result.json"), []byte("{}"), 0o644)
	out := filepath.Join(work, "a", "b", "out.json")
	os.MkdirAll(filepath.Dir(out), 0o755)
	os.WriteFile(out, nil, 0o644)
	if err := s.Finish(Paths([]string{"run", "--state", state, "--out", out, old})); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{state, filepath.Join(state, "runs", "batch-1", "result.json"), out,
		filepath.Join(work, "a"), filepath.Join(work, "a", "b")} {
		if uid := uidOf(t, p); uid != 4321 {
			t.Errorf("%s still uid %d", p, uid)
		}
	}
	if uid := uidOf(t, past); uid != 0 {
		t.Errorf("a file root made before the command was given away")
	}
}

func TestFinishLeavesRootsOwnArea(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root")
	}
	work := t.TempDir() // root's own
	s := Begin()
	f := filepath.Join(work, "x")
	os.WriteFile(f, nil, 0o644)
	if err := s.Finish([]string{f}); err != nil {
		t.Fatal(err)
	}
	if uid := uidOf(t, f); uid != 0 {
		t.Fatalf("root's file given to %d", uid)
	}
}

// mustPast backdates a file so it predates the session.
func mustPast(t *testing.T, p string) string {
	t.Helper()
	info, _ := os.Stat(p)
	then := info.ModTime().Add(-1e10)
	if err := os.Chtimes(p, then, then); err != nil {
		t.Fatal(err)
	}
	return p
}
