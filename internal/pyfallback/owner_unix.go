//go:build unix

package pyfallback

import (
	"fmt"
	"io/fs"
	"os"
	"strconv"
	"syscall"
)

func userTag() string { return strconv.Itoa(os.Getuid()) }

// private refuses a folder another user owns or can write to: the temp
// directory is shared, and lit runs what it finds there.
func private(dir string, info fs.FileInfo) error {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("cannot read the owner of %s", dir)
	}
	if int(st.Uid) != os.Getuid() {
		return fmt.Errorf("%s belongs to uid %d", dir, st.Uid)
	}
	if info.Mode().Perm()&0o022 != 0 {
		return fmt.Errorf("%s can be written by others", dir)
	}
	return nil
}
