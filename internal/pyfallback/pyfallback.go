// Package pyfallback runs a command that has not moved to Go yet with the
// Python lit embedded in the binary.
//
// The Python files are copied once into a private folder in the OS temp
// directory and run from there with "python3 -I -B". Nothing is written into
// the checkout, the action folder, HOME or RUNNER_TEMP: on a runner those are
// shared with the host, and a container job running as root that leaves files
// there (Python's __pycache__, on Nov 25, 2026) stops the runner from cleaning
// up before the next job.
package pyfallback

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
)

// Digest is a stable hash of every embedded file's path and bytes.
func Digest(files fs.FS) (string, error) {
	names, err := list(files)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	for _, name := range names {
		data, err := fs.ReadFile(files, name)
		if err != nil {
			return "", err
		}
		fmt.Fprintf(h, "%s\x00%d\x00", name, len(data))
		h.Write(data)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func list(files fs.FS) ([]string, error) {
	var names []string
	err := fs.WalkDir(files, ".", func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			names = append(names, name)
		}
		return nil
	})
	sort.Strings(names)
	return names, err
}

// Extract copies the embedded files into base/lit-py-<digest>-<uid> and returns
// that folder. An existing folder is reused only when this user owns it, nobody
// else can write to it, and its files still hash to the same digest; otherwise
// a fresh copy replaces it. The copy is made beside the target and renamed into
// place, so a concurrent lit never sees half a copy.
func Extract(files fs.FS, base string) (string, error) {
	digest, err := Digest(files)
	if err != nil {
		return "", err
	}
	dir := filepath.Join(base, fmt.Sprintf("lit-py-%s-%s", digest[:16], userTag()))
	if ok, _ := intact(dir, digest); ok {
		return dir, nil
	}
	tmp, err := os.MkdirTemp(base, "lit-py-tmp-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmp)
	names, err := list(files)
	if err != nil {
		return "", err
	}
	for _, name := range names {
		data, err := fs.ReadFile(files, name)
		if err != nil {
			return "", err
		}
		target := filepath.Join(tmp, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return "", err
		}
		if err := os.WriteFile(target, data, 0o600); err != nil {
			return "", err
		}
	}
	// Replace a folder that failed the checks; another lit may have won the race.
	if _, err := os.Lstat(dir); err == nil {
		if ok, _ := intact(dir, digest); ok {
			return dir, nil
		}
		if err := os.RemoveAll(dir); err != nil {
			return "", fmt.Errorf("lit: cannot replace %s: %w", dir, err)
		}
	}
	if err := os.Rename(tmp, dir); err != nil {
		if ok, _ := intact(dir, digest); ok {
			return dir, nil
		}
		return "", err
	}
	if ok, err := intact(dir, digest); !ok {
		return "", fmt.Errorf("lit: copy at %s failed its check: %v", dir, err)
	}
	return dir, nil
}

func intact(dir, digest string) (bool, error) {
	info, err := os.Lstat(dir)
	if err != nil {
		return false, err
	}
	if !info.IsDir() {
		return false, errors.New("not a directory")
	}
	if err := private(dir, info); err != nil {
		return false, err
	}
	got, err := Digest(os.DirFS(dir))
	if err != nil {
		return false, err
	}
	if got != digest {
		return false, errors.New("contents changed")
	}
	return true, nil
}

var versionLine = regexp.MustCompile(`^Python (\d+)\.(\d+)`)

// Python finds an interpreter: LIT_PYTHON if set, else python3, else python.
// It must be Python 3.12 or later.
func Python() (string, error) {
	candidates := []string{"python3", "python"}
	if v := os.Getenv("LIT_PYTHON"); v != "" {
		candidates = []string{v}
	}
	var tried []string
	for _, c := range candidates {
		p, err := exec.LookPath(c)
		if err != nil {
			tried = append(tried, c+" (not found)")
			continue
		}
		out, err := exec.Command(p, "--version").CombinedOutput()
		m := versionLine.FindSubmatch(out)
		if err != nil || m == nil {
			tried = append(tried, c+" (no version)")
			continue
		}
		major, _ := strconv.Atoi(string(m[1]))
		minor, _ := strconv.Atoi(string(m[2]))
		if major > 3 || (major == 3 && minor >= 12) {
			return p, nil
		}
		tried = append(tried, fmt.Sprintf("%s (Python %d.%d)", c, major, minor))
	}
	return "", fmt.Errorf("lit: this command still runs on Python 3.12 or later until it moves to Go; tried %v (set LIT_PYTHON to choose one)", tried)
}

const launcher = "import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'lit'; " +
	"from concurrent_merge.cli import main; sys.exit(main())"

// Command builds the Python process for args. dir is the folder Extract made.
func Command(python, dir string, args []string) *exec.Cmd {
	cmd := exec.Command(python, append([]string{"-I", "-B", "-c", launcher, dir}, args...)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	// -B already stops bytecode; the variable also covers any Python the command starts.
	cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1")
	return cmd
}
