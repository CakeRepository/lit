package handback

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestPathsStopsAtTheTestCommandAndSkipsTheCheckout(t *testing.T) {
	wd, _ := os.Getwd()
	got := Paths([]string{
		"run", "queue.json", "--repo", "batch.git", "--state=st", "--workdir", "src",
		"--cwd=sub", "--jobs", "4", "--test", "bash", "-c", "rm -rf out",
	})
	want := []string{"run", "queue.json", "batch.git", "st", "4"}
	for i := range want {
		want[i] = filepath.Join(wd, want[i])
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v\nwant %v", got, want)
	}
}

func TestPathsNeverNamesAFilesystemRoot(t *testing.T) {
	if got := Paths([]string{"/", "--out=/"}); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}
