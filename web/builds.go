package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os/exec"
	"strings"
	"time"
)

// buildList is the part of `daemon/builds.py list`'s JSON that the builds page shows.
type buildList struct {
	Setup     string         `json:"setup"`
	HasTree   bool           `json:"has_tree"`
	CanTag    bool           `json:"can_tag"`
	Serial    int            `json:"serial"`
	Tags      map[string]int `json:"tags"`
	Withdrawn []int          `json:"withdrawn"`
	Builds    []build        `json:"builds"`
}

type build struct {
	Build     int        `json:"build"`
	Commit    string     `json:"commit"`
	CommitURL string     `json:"commit_url"`
	Subject   string     `json:"subject"`
	Built     string     `json:"built"`
	Platforms []download `json:"platforms"`
	Tags      []string   `json:"tags"`
}

// Short is the commit as the page shows it.
func (b build) Short() string {
	return b.Commit[:min(len(b.Commit), 10)]
}

// command runs daemon/builds.py with these arguments and returns what it printed. Its error
// is the reason it gave.
type command func(ctx context.Context, args ...string) ([]byte, error)

func runCommand(program []string) command {
	return func(ctx context.Context, args ...string) ([]byte, error) {
		ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		var stdout, stderr bytes.Buffer
		cmd := exec.CommandContext(ctx, program[0], append(program[1:len(program):len(program)], args...)...)
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		if err := cmd.Run(); err != nil {
			return nil, reason(err, stderr.String())
		}
		return stdout.Bytes(), nil
	}
}

// reason is what a refusal says: the script's own "error: ..." line, if it has one.
func reason(err error, stderr string) error {
	lines := strings.Split(strings.TrimSpace(stderr), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if text, found := strings.CutPrefix(lines[i], "error: "); found {
			return errors.New(text)
		}
	}
	return fmt.Errorf("%w: %s", err, strings.TrimSpace(stderr))
}

func (s *server) buildList(ctx context.Context, setup string) ([]byte, *buildList, error) {
	data, err := s.builds(ctx, "list", setup)
	if err != nil {
		return nil, nil, err
	}
	var list buildList
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, nil, fmt.Errorf("the builds aren't what daemon/builds.py writes: %w", err)
	}
	return data, &list, nil
}

// knownSetup says the name is a setup the overview lists, the only ones that reach a command.
func (s *server) knownSetup(ctx context.Context, name string) (*overview, bool, error) {
	o, err := s.overviews.get(ctx)
	if err != nil {
		return nil, false, err
	}
	for _, setup := range o.Setups {
		if setup.Name == name {
			return o, true, nil
		}
	}
	return o, false, nil
}

// handleBuilds is a setup's builds. It doesn't refresh itself: that would drop a selection.
func (s *server) handleBuilds(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("setup")
	o, known, err := s.knownSetup(r.Context(), name)
	if err == nil && !known {
		s.render(w, http.StatusNotFound, "error", pageData{Overview: o, Error: http.StatusText(http.StatusNotFound)})
		return
	}
	var list *buildList
	if err == nil {
		_, list, err = s.buildList(r.Context(), name)
	}
	if err != nil {
		slog.Error("builds", "setup", name, "err", err)
		s.render(w, http.StatusBadGateway, "error", pageData{Overview: o, Error: err.Error()})
		return
	}
	s.render(w, http.StatusOK, "builds", pageData{Overview: o, Builds: list})
}

func (s *server) handleBuildsAPI(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("setup")
	_, known, err := s.knownSetup(r.Context(), name)
	if err == nil && !known {
		writeError(w, http.StatusNotFound, errors.New("no such setup"))
		return
	}
	var data []byte
	if err == nil {
		data, _, err = s.buildList(r.Context(), name)
	}
	if err != nil {
		slog.Error("builds", "setup", name, "err", err)
		writeError(w, http.StatusBadGateway, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(data)
}

// handleMove is the review before a tag move ("check") and the move itself ("queue"), each
// answered with daemon/builds.py's JSON, or 409 and {"error": why} if it refuses. The script
// decides everything; this only makes sure that what reaches it is what it expects.
func (s *server) handleMove(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("setup")
		_, known, err := s.knownSetup(r.Context(), name)
		if err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		if !known {
			writeError(w, http.StatusNotFound, errors.New("no such setup"))
			return
		}
		serial := r.PostFormValue("serial")
		if !runNumber.MatchString(serial) {
			writeError(w, http.StatusBadRequest, errors.New("no serial"))
			return
		}
		args := []string{action, name, "--serial", serial}
		for _, tag := range []string{"testing", "stable"} {
			value := r.PostFormValue(tag)
			if value == "" {
				continue
			}
			if !runNumber.MatchString(value) {
				writeError(w, http.StatusBadRequest, fmt.Errorf("not a build: %q", value))
				return
			}
			args = append(args, tag, value)
		}
		if len(args) == 4 {
			writeError(w, http.StatusBadRequest, errors.New("no tag to move"))
			return
		}
		data, err := s.builds(r.Context(), args...)
		if err != nil {
			slog.Error("move", "args", args, "err", err)
			writeError(w, http.StatusConflict, err)
			return
		}
		slog.Info("move", "args", args)
		w.Header().Set("Content-Type", "application/json")
		w.Write(data)
	}
}

func writeError(w http.ResponseWriter, status int, err error) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
}
