//go:build unix

package handback

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
)

// Finish hands back, under each path, every entry that root created or changed
// since Begin. Each goes to the owner of the nearest folder above the path that
// root does not own; a path with no such folder (root's own area) is left as it
// is. It does nothing unless lit runs as root.
func (s *Session) Finish(paths []string) error {
	if os.Geteuid() != 0 {
		return nil
	}
	var errs []error
	for _, p := range paths {
		if _, err := os.Lstat(p); err != nil {
			continue
		}
		uid, gid, top, ok := owner(filepath.Dir(p))
		if !ok {
			continue
		}
		// Folders lit made on the way to p, such as a/b for --out a/b/c.json.
		for dir := filepath.Dir(p); dir != top; dir = filepath.Dir(dir) {
			if s.created(dir) {
				if err := os.Lchown(dir, uid, gid); err != nil {
					errs = append(errs, err)
				}
			}
		}
		err := filepath.WalkDir(p, func(name string, d fs.DirEntry, err error) error {
			if err != nil {
				return nil // vanished or unreadable: nothing of ours to give back
			}
			if !s.created(name) {
				return nil
			}
			if err := os.Lchown(name, uid, gid); err != nil {
				errs = append(errs, err)
			}
			return nil
		})
		if err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// created reports whether name is root's and changed since Begin.
func (s *Session) created(name string) bool {
	info, err := os.Lstat(name)
	if err != nil {
		return false
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && st.Uid == 0 && !info.ModTime().Before(s.start)
}

// owner returns the nearest existing folder at or above dir that root does not
// own, and its owner.
func owner(dir string) (uid, gid int, top string, ok bool) {
	for {
		info, err := os.Lstat(dir)
		if err == nil {
			if st, ok := info.Sys().(*syscall.Stat_t); ok && st.Uid != 0 {
				return int(st.Uid), int(st.Gid), dir, true
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return 0, 0, "", false
		}
		dir = parent
	}
}
