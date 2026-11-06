package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"fmt"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"time"
)

//go:embed templates static
var files embed.FS

// recent is how many runs the overview page lists; /runs has all that overview.py gives.
const recent = 12

var (
	jobName   = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
	runNumber = regexp.MustCompile(`^[0-9]+$`)
)

type server struct {
	overviews *overviewCache
	builds    command
	laminar   string
	pages     map[string]*template.Template
	static    fs.FS
}

func newServer(src source, builds command, laminar string) (*server, error) {
	funcs := template.FuncMap{
		"duration":  duration,
		"shortTime": shortTime,
		"size":      size,
		"recent": func(runs []run) []run {
			return runs[:min(len(runs), recent)]
		},
	}
	pages := map[string]*template.Template{}
	for _, name := range []string{"overview", "oldlayout", "builds", "runs", "run", "error"} {
		page, err := template.New("layout.html").Funcs(funcs).ParseFS(files,
			"templates/layout.html", "templates/runrow.html", "templates/"+name+".html")
		if err != nil {
			return nil, err
		}
		pages[name] = page
	}
	static, err := fs.Sub(files, "static")
	if err != nil {
		return nil, err
	}
	return &server{&overviewCache{source: src, ttl: time.Second}, builds, laminar, pages, static}, nil
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", s.withOverview("overview", s.overviewData))
	mux.HandleFunc("GET /old-layout", s.withOverview("oldlayout", s.overviewData))
	mux.HandleFunc("GET /runs", s.withOverview("runs", s.overviewData))
	mux.HandleFunc("GET /run/{job}/{number}", s.withOverview("run", s.runData))
	mux.HandleFunc("GET /run/{job}/{number}/log", s.handleLog)
	mux.HandleFunc("GET /api/overview", s.handleOverviewAPI)
	mux.HandleFunc("GET /builds/{setup}", s.handleBuilds)
	mux.HandleFunc("GET /api/builds/{setup}", s.handleBuildsAPI)
	mux.HandleFunc("POST /builds/{setup}/check", s.handleMove("check"))
	mux.HandleFunc("POST /builds/{setup}/queue", s.handleMove("queue"))
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServerFS(s.static)))
	// There's no login: nothing else stops another site from making a browser post here
	guarded := http.NewCrossOriginProtection().Handler(mux)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		guarded.ServeHTTP(w, r)
	})
}

// pageData is what the templates get. Refresh: the page reloads its <main> by itself.
type pageData struct {
	Refresh  bool
	Overview *overview
	Error    string
	// the run page
	Job, Number string
	Run         *run
	// the builds page
	Builds *buildList
}

// dataFunc fills in a page's own data; a non-zero status means there's no such page.
type dataFunc func(r *http.Request, data *pageData) int

func (s *server) withOverview(page string, fill dataFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		data := pageData{Refresh: true}
		if status := fill(r, &data); status != 0 {
			s.render(w, status, "error", pageData{Error: http.StatusText(status)})
			return
		}
		o, err := s.overviews.get(r.Context())
		if err != nil {
			slog.Error("overview", "err", err)
			// Refresh: it comes back by itself when the overview does
			s.render(w, http.StatusBadGateway, "error", pageData{Refresh: true, Error: err.Error()})
			return
		}
		data.Overview = o
		if data.Job != "" {
			data.Run = o.findRun(data.Job, data.Number)
		}
		s.render(w, http.StatusOK, page, data)
	}
}

func (s *server) overviewData(*http.Request, *pageData) int {
	return 0
}

func (s *server) runData(r *http.Request, data *pageData) int {
	data.Job, data.Number = r.PathValue("job"), r.PathValue("number")
	if !validRun(data.Job, data.Number) {
		return http.StatusNotFound
	}
	return 0
}

func validRun(job, number string) bool {
	return jobName.MatchString(job) && runNumber.MatchString(number)
}

func (s *server) render(w http.ResponseWriter, status int, page string, data pageData) {
	// Into a buffer: a template that fails shows as an error, not half a page
	var out bytes.Buffer
	if err := s.pages[page].Execute(&out, data); err != nil {
		slog.Error("render", "page", page, "err", err)
		http.Error(w, "the page can't be rendered", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	w.Write(out.Bytes())
}

// handleOverviewAPI serves overview.py's JSON as it wrote it. If it can't be had: 502 and
// {"error": why}, not an older one.
func (s *server) handleOverviewAPI(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	o, err := s.overviews.get(r.Context())
	if err != nil {
		slog.Error("overview", "err", err)
		w.WriteHeader(http.StatusBadGateway)
		json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
		return
	}
	w.Write(o.raw)
}

// handleLog passes a run's log through from Laminar's web server: all of it for a finished
// run; for a running one, what it has so far and more as it comes, until the run ends.
func (s *server) handleLog(w http.ResponseWriter, r *http.Request) {
	job, number := r.PathValue("job"), r.PathValue("number")
	if !validRun(job, number) {
		http.NotFound(w, r)
		return
	}
	target, err := url.Parse(s.laminar + "/log/" + job + "/" + number)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	proxy := httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) {
			out.Out.URL, out.Out.Host = target, target.Host
			out.Out.Header.Set("Accept-Encoding", "identity")
		},
		FlushInterval: -1, // every chunk as it comes
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			slog.Error("log", "url", target.String(), "err", err)
			http.Error(w, fmt.Sprintf("can't read the log from Laminar: %v", err), http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, r)
}

// duration is as overview.py writes it: 6m31s, 2h05m. Nil: not started.
func duration(seconds *int) string {
	if seconds == nil {
		return ""
	}
	if *seconds < 3600 {
		return fmt.Sprintf("%dm%02ds", *seconds/60, *seconds%60)
	}
	return fmt.Sprintf("%dh%02dm", *seconds/3600, *seconds%3600/60)
}

// shortTime is "2026-10-27T12:00:06+01:00" as "10-27 12:00", as overview.py writes it.
func shortTime(text string) string {
	if len(text) < 16 {
		return ""
	}
	return text[5:10] + " " + text[11:16]
}

func size(n int64) string {
	value := float64(n)
	for _, unit := range []string{"B", "K", "M"} {
		if value < 1024 {
			return fmt.Sprintf("%.0f%s", value, unit)
		}
		value /= 1024
	}
	if value < 1024 {
		return fmt.Sprintf("%.1fG", value)
	}
	return fmt.Sprintf("%.1fT", value/1024)
}
