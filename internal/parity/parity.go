// Package parity runs one lit command twice, once with the Python lit and once
// with the Go lit, on two copies of the same inputs, and reports every way the
// two runs differ (CakeRepository/lit#35).
//
// A command moves to Go only when its parity cases match. What is compared:
// the exit code, stdout and stderr, every file left in the folder (JSON files
// parsed, so key order does not matter), and the refs of every Git repository
// in it. What is allowed to differ, because it differs between any two runs:
// the folder itself, the random part of a batch-XXXXXXXX run folder, ISO 8601
// times, durations in "seconds" fields, and the contents of test logs (*.log),
// which hold the test command's own output. Batch commits are made at
// SOURCE_DATE_EPOCH, which parity pins, so commit IDs must match exactly.
package parity

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// Side is one of the two runs.
type Side struct {
	Name   string // "python" or "go"
	Dir    string
	Code   int
	Stdout string
	Stderr string
	Files  map[string]string // normalized path -> normalized contents
}

// Options configure a comparison.
type Options struct {
	Self   string   // the lit binary to run on both sides
	Source string   // the inputs, copied once per side
	Args   []string // the lit command and its arguments
	Epoch  string   // SOURCE_DATE_EPOCH for both sides
	Ignore []string // extra JSON keys whose values may differ
	Go     bool     // whether Args[0] has a Go implementation
	Env    []string // environment for both sides; nil means os.Environ()
}

// Report is the outcome of a comparison.
type Report struct {
	Python, Go *Side
	Diffs      []string
	Work       string // the folder holding both copies
}

// Run copies Source twice, runs the command in each copy and compares.
func Run(o Options) (*Report, error) {
	work, err := os.MkdirTemp("", "lit-parity-")
	if err != nil {
		return nil, err
	}
	r := &Report{Work: work}
	impls := []string{"python", "go"}
	if !o.Go {
		impls[1] = "python"
	}
	sides := make([]*Side, 2)
	for i, name := range []string{"python", "go"} {
		dir := filepath.Join(work, name)
		if err := CopyTree(o.Source, dir); err != nil {
			return r, fmt.Errorf("copy inputs: %w", err)
		}
		s := &Side{Name: name, Dir: dir}
		var out, errb bytes.Buffer
		cmd := exec.Command(o.Self, o.Args...)
		cmd.Dir = dir
		env := o.Env
		if env == nil {
			env = os.Environ()
		}
		cmd.Env = append(append([]string{}, env...), "SOURCE_DATE_EPOCH="+o.Epoch, "LIT_IMPL="+impls[i])
		cmd.Stdout, cmd.Stderr = &out, &errb
		if err := cmd.Run(); err != nil {
			exit, ok := err.(*exec.ExitError)
			if !ok {
				return r, err
			}
			s.Code = exit.ExitCode()
		}
		renames, err := renameRuns(dir)
		if err != nil {
			return r, err
		}
		n := normalizer{dir: dir, renames: renames, ignore: o.Ignore}
		s.Stdout, s.Stderr = n.text(out.String()), n.text(errb.String())
		if s.Files, err = n.tree(); err != nil {
			return r, err
		}
		sides[i] = s
	}
	r.Python, r.Go = sides[0], sides[1]
	r.Diffs = Compare(r.Python, r.Go)
	return r, nil
}

// Compare lists every difference between two finished sides.
func Compare(a, b *Side) []string {
	var diffs []string
	if a.Code != b.Code {
		diffs = append(diffs, fmt.Sprintf("exit code: %s %d, %s %d", a.Name, a.Code, b.Name, b.Code))
	}
	if a.Stdout != b.Stdout {
		diffs = append(diffs, "stdout differs:\n"+excerpt(a, b, a.Stdout, b.Stdout))
	}
	if a.Stderr != b.Stderr {
		diffs = append(diffs, "stderr differs:\n"+excerpt(a, b, a.Stderr, b.Stderr))
	}
	paths := map[string]bool{}
	for p := range a.Files {
		paths[p] = true
	}
	for p := range b.Files {
		paths[p] = true
	}
	sorted := make([]string, 0, len(paths))
	for p := range paths {
		sorted = append(sorted, p)
	}
	sort.Strings(sorted)
	for _, p := range sorted {
		x, inA := a.Files[p]
		y, inB := b.Files[p]
		switch {
		case !inB:
			diffs = append(diffs, fmt.Sprintf("%s: only %s wrote it", p, a.Name))
		case !inA:
			diffs = append(diffs, fmt.Sprintf("%s: only %s wrote it", p, b.Name))
		case x != y:
			diffs = append(diffs, p+" differs:\n"+excerpt(a, b, x, y))
		}
	}
	return diffs
}

func excerpt(a, b *Side, x, y string) string {
	xl, yl := strings.Split(x, "\n"), strings.Split(y, "\n")
	i := 0
	for i < len(xl) && i < len(yl) && xl[i] == yl[i] {
		i++
	}
	line := func(l []string) string {
		if i < len(l) {
			return l[i]
		}
		return "(end)"
	}
	return fmt.Sprintf("  line %d\n  %s: %s\n  %s: %s", i+1, a.Name, line(xl), b.Name, line(yl))
}

// CopyTree copies src to dst, keeping modes and symlinks.
func CopyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		switch {
		case d.IsDir():
			return os.MkdirAll(target, info.Mode().Perm()|0o700)
		case d.Type()&fs.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		case d.Type().IsRegular():
			return copyFile(path, target, info.Mode().Perm())
		}
		return nil // sockets, devices and pipes are not inputs
	})
}

func copyFile(src, dst string, mode fs.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode|0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

var runFolder = regexp.MustCompile(`^batch-[A-Za-z0-9_]{8}$`)

// renameRuns gives each batch-XXXXXXXX folder, oldest first, a stable name
// (batch-1, batch-2, ...) so the two sides line up, and returns the renames.
func renameRuns(dir string) (map[string]string, error) {
	renames := map[string]string{}
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || !d.IsDir() {
			return err
		}
		if isGitDir(path) {
			return filepath.SkipDir
		}
		entries, err := os.ReadDir(path)
		if err != nil {
			return err
		}
		type run struct {
			name string
			when int64
		}
		var runs []run
		for _, e := range entries {
			if e.IsDir() && runFolder.MatchString(e.Name()) {
				info, err := e.Info()
				if err != nil {
					return err
				}
				runs = append(runs, run{e.Name(), info.ModTime().UnixNano()})
			}
		}
		sort.Slice(runs, func(i, j int) bool {
			if runs[i].when != runs[j].when {
				return runs[i].when < runs[j].when
			}
			return runs[i].name < runs[j].name
		})
		for i, r := range runs {
			stable := fmt.Sprintf("batch-%d", i+1)
			renames[r.name] = stable
			if err := os.Rename(filepath.Join(path, r.name), filepath.Join(path, stable)); err != nil {
				return err
			}
		}
		return nil
	})
	return renames, err
}

func isGitDir(path string) bool {
	head, err := os.Stat(filepath.Join(path, "HEAD"))
	if err != nil || head.IsDir() {
		return false
	}
	objects, err := os.Stat(filepath.Join(path, "objects"))
	return err == nil && objects.IsDir()
}

type normalizer struct {
	dir     string
	renames map[string]string
	ignore  []string
}

var isoTime = regexp.MustCompile(`\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?\b`)

func (n normalizer) text(s string) string {
	dirs := []string{n.dir}
	if real, err := filepath.EvalSymlinks(n.dir); err == nil && real != n.dir {
		dirs = append(dirs, real)
	}
	for _, d := range dirs {
		s = strings.ReplaceAll(s, d, "<dir>")
	}
	for from, to := range n.renames {
		s = strings.ReplaceAll(s, from, to)
	}
	return isoTime.ReplaceAllString(s, "<time>")
}

// tree reads every file and Git repository under the side's folder.
func (n normalizer) tree() (map[string]string, error) {
	files := map[string]string{}
	err := filepath.WalkDir(n.dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(n.dir, path)
		rel = filepath.ToSlash(rel)
		if d.IsDir() {
			if isGitDir(path) {
				refs, err := gitRefs(path)
				if err != nil {
					return err
				}
				files[rel+"/(refs)"] = n.text(refs)
				return filepath.SkipDir
			}
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 {
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			files[rel] = "symlink " + n.text(link)
			return nil
		}
		if !d.Type().IsRegular() {
			return nil
		}
		if strings.HasSuffix(rel, ".log") {
			files[rel] = "(test log)"
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if strings.HasSuffix(rel, ".json") {
			var v any
			if json.Unmarshal(data, &v) == nil {
				v = n.json(v, "")
				out, _ := json.MarshalIndent(v, "", " ")
				files[rel] = string(out)
				return nil
			}
		}
		files[rel] = n.text(string(data))
		return nil
	})
	return files, err
}

func (n normalizer) json(v any, key string) any {
	switch t := v.(type) {
	case map[string]any:
		for k, x := range t {
			t[k] = n.json(x, k)
		}
		return t
	case []any:
		for i, x := range t {
			t[i] = n.json(x, key)
		}
		return t
	case float64:
		if key == "seconds" || strings.HasSuffix(key, "_seconds") || n.ignored(key) {
			return "<varies>"
		}
		return t
	case string:
		if n.ignored(key) {
			return "<varies>"
		}
		return n.text(t)
	default:
		if n.ignored(key) {
			return "<varies>"
		}
		return t
	}
}

func (n normalizer) ignored(key string) bool {
	for _, k := range n.ignore {
		if k == key {
			return true
		}
	}
	return false
}

func gitRefs(dir string) (string, error) {
	cmd := exec.Command("git", "for-each-ref", "--format=%(refname) %(objectname)")
	cmd.Env = append(os.Environ(), "GIT_DIR="+dir)
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("git refs of %s: %w", dir, err)
	}
	head, _ := os.ReadFile(filepath.Join(dir, "HEAD"))
	return "HEAD " + strings.TrimSpace(string(head)) + "\n" + string(out), nil
}
