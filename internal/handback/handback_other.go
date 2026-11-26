//go:build !unix

package handback

// Finish does nothing where there is no root user.
func (s *Session) Finish([]string) error { return nil }
