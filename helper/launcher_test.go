package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestLauncherImmediateChildExit(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash required")
	}
	source, err := os.ReadFile("launcher.sh")
	if err != nil {
		t.Fatal(err)
	}
	for _, delayed := range []bool{false, true} {
		for _, tc := range []struct {
			name, target, helper string
			code                 int
		}{
			{"target-failure", "exit 3", "trap 'exit 0' TERM; while :; do sleep .02; done", 3},
			{"helper-failure", "trap 'exit 0' TERM; while :; do sleep .02; done", "exit 7", 7},
			{"target-success", "exit 0", "trap 'exit 0' TERM; while :; do sleep .02; done", 0},
			{"helper-success", "trap 'exit 0' TERM; while :; do sleep .02; done", "exit 0", 1},
		} {
			t.Run(tc.name+map[bool]string{false: "/immediate", true: "/already-exited"}[delayed], func(t *testing.T) {
				for attempt := 0; attempt < 10; attempt++ {
					dir := t.TempDir()
					helper, target, script := filepath.Join(dir, "helper"), filepath.Join(dir, "target"), filepath.Join(dir, "launcher")
					for path, text := range map[string]string{helper: tc.helper, target: tc.target} {
						if err := os.WriteFile(path, []byte("#!/bin/bash\n"+text+"\n"), 0700); err != nil {
							t.Fatal(err)
						}
					}
					launcher := string(source)
					if delayed {
						launcher = strings.Replace(launcher, "set +e\nwait -n", "set +e\nsleep .05\nwait -n", 1)
					}
					if err := os.WriteFile(script, []byte(launcher), 0700); err != nil {
						t.Fatal(err)
					}
					cmd := exec.Command("bash", script, "redis")
					cmd.Env = append(os.Environ(), "REDIS_HEALTH_BIN="+helper, "REDIS_STARTUP_SCRIPT="+target)
					cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
					if err := cmd.Start(); err != nil {
						t.Fatal(err)
					}
					done := make(chan error, 1)
					go func() { done <- cmd.Wait() }()
					select {
					case err := <-done:
						_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
						code := 0
						if err != nil {
							exit, ok := err.(*exec.ExitError)
							if !ok {
								t.Fatal(err)
							}
							code = exit.ExitCode()
						}
						if code != tc.code {
							t.Fatalf("attempt %d exit=%d expected=%d delayed=%t", attempt, code, tc.code, delayed)
						}
					case <-time.After(2 * time.Second):
						_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
						<-done
						t.Fatal("completed child was missed")
					}
				}
			})
		}
	}
}
