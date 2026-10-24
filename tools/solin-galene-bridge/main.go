// solin-galene-bridge connects Solin to a Galene SFU.
//
//	publish   reads framed H.264/Opus/PCM from stdin and publishes it over WHIP
//	receive   joins a group over Galene's WebSocket protocol, plays the
//	          remote participants' audio on an output device and forwards
//	          part-holders' video to local RTP slots
//	devices   lists playback devices as JSON
//	version   prints the version
//
// Logs go to stderr; machine-readable status goes to stdout as JSON lines.
// See README.md for the protocol details.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"sync"
	"time"
)

var version = "dev"

// Exit codes.
const (
	exitOK     = 0
	exitError  = 1 // fatal runtime error
	exitUsage  = 2 // bad command line
	exitAuth   = 3 // the token was refused (expired, wrong group, no permission)
	exitDevice = 4 // the audio device could not be opened or disappeared
)

// exitError carries a process exit code out of a subcommand.
type codedError struct {
	code int
	err  error
}

func (e *codedError) Error() string { return e.err.Error() }
func (e *codedError) Unwrap() error { return e.err }

func withCode(code int, err error) error { return &codedError{code: code, err: err} }

// emitter writes one JSON object per line to stdout.
type emitter struct {
	mu  sync.Mutex
	enc *json.Encoder

	lastState string
}

func newEmitter() *emitter { return &emitter{enc: json.NewEncoder(os.Stdout)} }

func (e *emitter) emit(v any) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.enc.Encode(v); err != nil {
		log.Printf("stdout: %v", err)
	}
}

// state emits a state event when the state changes.
func (e *emitter) state(s string) {
	e.mu.Lock()
	if s == e.lastState {
		e.mu.Unlock()
		return
	}
	e.lastState = s
	e.mu.Unlock()
	log.Printf("state: %s", s)
	e.emit(map[string]string{"event": "state", "state": s})
}

// backoff yields retry delays doubling from min to max.
type backoff struct {
	min, max, cur time.Duration
}

func newBackoff(min, max time.Duration) *backoff { return &backoff{min: min, max: max} }

func (b *backoff) Next() time.Duration {
	if b.cur == 0 {
		b.cur = b.min
	} else if b.cur *= 2; b.cur > b.max {
		b.cur = b.max
	}
	return b.cur
}

func (b *backoff) Reset() { b.cur = 0 }

func usage() {
	fmt.Fprintf(os.Stderr, `usage: solin-galene-bridge <command> [flags]

commands:
  publish   --whip URL --token T     publish framed media from stdin over WHIP
  receive   --ws URL --group G --token T   play remote audio from a Galene group
  devices                            list playback devices (JSON)
  version                            print the version

Run "solin-galene-bridge <command> -h" for the command's flags.
`)
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.SetPrefix("solin-galene-bridge: ")
	if len(os.Args) < 2 {
		usage()
		os.Exit(exitUsage)
	}
	var err error
	switch cmd, args := os.Args[1], os.Args[2:]; cmd {
	case "publish":
		err = runPublish(args)
	case "receive":
		err = runReceive(args)
	case "devices":
		err = runDevices(args)
	case "version", "--version", "-version":
		fmt.Println(version)
	case "help", "-h", "--help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", cmd)
		usage()
		os.Exit(exitUsage)
	}
	if err != nil {
		log.Printf("fatal: %v", err)
		code := exitError
		var ce *codedError
		if errors.As(err, &ce) {
			code = ce.code
		}
		os.Exit(code)
	}
}
