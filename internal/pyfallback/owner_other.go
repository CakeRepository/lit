//go:build !unix

package pyfallback

import (
	"io/fs"
	"os"
	"strings"
)

func userTag() string {
	u := os.Getenv("USERNAME")
	if u == "" {
		return "user"
	}
	return strings.Map(func(r rune) rune {
		if r == '\\' || r == '/' || r == ':' {
			return '_'
		}
		return r
	}, u)
}

// private: the per-user temp directory on Windows is already the user's own.
func private(string, fs.FileInfo) error { return nil }
