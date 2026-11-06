// Command hopper-web shows what daemon/overview.py gathers and Laminar's logs in the browser.
// It holds no logic of its own: the data is overview.py's JSON, a log is passed through from
// Laminar's web server as it grows, and actions on builds are checked and queued by
// daemon/builds.py.
//
//	main.go      configuration, server setup, startup
//	overview.go  overview.py's JSON, and what the pages need from it
//	pages.go     the pages (templates/, static/), the log stream and /api/overview
//	builds.go    daemon/builds.py's JSON, a setup's builds page and the actions on them
//
// Configuration is read from the environment:
//
//	LISTEN        default 127.0.0.1:8092; Caddy goes in front
//	OVERVIEW      the command that prints the overview, run without a shell; default
//	              daemon/overview.py, from the hopper checkout
//	BUILDS        the command for a setup's builds, likewise; default daemon/builds.py
//	LAMINAR_HTTP  Laminar's web server, default http://127.0.0.1:8091
//
// The only routes that aren't GET are the actions on a setup's builds (moving tags,
// withdrawing, pruning): POST /builds/<setup>/check, which changes nothing, and /queue, which
// queues the Laminar job that does it.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	env := func(key, def string) string {
		if v := os.Getenv(key); v != "" {
			return v
		}
		return def
	}
	listen := env("LISTEN", "127.0.0.1:8092")
	command := strings.Fields(env("OVERVIEW", "daemon/overview.py"))
	laminar := strings.TrimRight(env("LAMINAR_HTTP", "http://127.0.0.1:8091"), "/")

	builds := strings.Fields(env("BUILDS", "daemon/builds.py"))

	s, err := newServer(commandSource(command), runCommand(builds), laminar)
	if err != nil {
		slog.Error("init", "err", err)
		os.Exit(1)
	}

	// No write timeout: a running build's log stays open, and can be quiet for minutes
	srv := &http.Server{
		Addr:              listen,
		Handler:           s.routes(),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		// Followed logs would hold a graceful shutdown up until their runs end
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if srv.Shutdown(shutdownCtx) != nil {
			srv.Close()
		}
	}()

	slog.Info("listening", "addr", listen, "overview", command, "laminar", laminar)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("serve", "err", err)
		os.Exit(1)
	}
}
