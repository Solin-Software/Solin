package main

import (
	"errors"
	"fmt"
	"os"
	"strings"
)

// tokenSource yields the Galene token for each connection attempt: either a
// fixed --token, or the contents of --token-file re-read on every attempt so
// the supervisor can hand over a fresh short-lived token without restarting
// the process.
type tokenSource struct {
	token string
	file  string
}

func (t tokenSource) refreshable() bool { return t.file != "" }

func (t tokenSource) get() (string, error) {
	if t.file == "" {
		return t.token, nil
	}
	b, err := os.ReadFile(t.file)
	if err != nil {
		return "", fmt.Errorf("token file: %w", err)
	}
	tok := strings.TrimSpace(string(b))
	if tok == "" {
		return "", errors.New("token file is empty")
	}
	return tok, nil
}
