// Package lit holds the parts of lit that the Go binary carries inside it.
package lit

import "embed"

// Python is today's Python lit. The binary runs it for every command that has
// not moved to Go yet (see cmd/lit and internal/pyfallback). Files are named by
// pattern so that bytecode, build output and editor files are never embedded.
//
//go:embed concurrent_merge/*.py concurrent_merge/readers/*.py concurrent_merge/ps_nodes.ps1
//go:embed concurrent_merge/dotnet/*.cs concurrent_merge/dotnet/*.csproj
var Python embed.FS
