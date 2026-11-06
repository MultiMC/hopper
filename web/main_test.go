package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T) []byte {
	t.Helper()
	data, err := os.ReadFile("testdata/overview.json")
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// testServer serves the fixture's overview, with Laminar at laminarURL.
func testServer(t *testing.T, laminarURL string) *httptest.Server {
	t.Helper()
	o, err := parseOverview(fixture(t))
	if err != nil {
		t.Fatal(err)
	}
	s, err := newServer(func(context.Context) (*overview, error) { return o, nil }, laminarURL)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(s.routes())
	t.Cleanup(server.Close)
	return server
}

func get(t *testing.T, url string) (int, string) {
	t.Helper()
	response, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, string(body)
}

func wantAll(t *testing.T, body string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(body, want) {
			t.Errorf("the page doesn't say %q", want)
		}
	}
}

func TestOverviewPage(t *testing.T) {
	status, body := get(t, testServer(t, "http://unused").URL+"/")
	if status != http.StatusOK {
		t.Fatalf("status %d: %s", status, body)
	}
	wantAll(t, body,
		"builder", "2254abe", "production",
		// a setup without a new updater tree: the newest develop build, with its archives
		`<a href="https://files.example/downloads/mmc-develop-lin64.tar.gz">lin64</a>, <a href="https://files.example/downloads/mmc-develop-win32.zip">win32</a>`,
		// one with: its tags, with theirs
		`<a href="https://files2.example/downloads/updater/mmc-latest-win32-5.15.2.zip">win32-5.15.2</a>`,
		"5026, 5027",
		"Can't read its files",      // the broken setup still shows, next to the other
		"Launcher develop",          // what starts the job, short
		"cron 0 * * * *", "by hand", // the other two kinds
		"bad schedule",                   // a job's problem
		"laminard can't be asked",        // running: null
		`href="/run/launcher-files2/24"`, // a running run links to its log
		"36 files published", "looked 36s ago",
		"trigger: &lt;new&gt; commit", // escaped
	)
	if !(strings.Index(body, "5043") < strings.Index(body, "5035") && strings.Index(body, "5035") < strings.Index(body, "5034")) {
		t.Error("the tags aren't latest, testing, stable")
	}
	// The current layout's other builds are on /old-layout
	for _, old := range []string{"3926", "3674"} {
		if strings.Contains(body, old) {
			t.Errorf("the page says %q", old)
		}
	}
}

func TestOldLayoutPage(t *testing.T) {
	status, body := get(t, testServer(t, "http://unused").URL+"/old-layout")
	if status != http.StatusOK {
		t.Fatalf("status %d: %s", status, body)
	}
	wantAll(t, body,
		"production", "files2", "Can't read its files",
		`<a href="https://files.example/downloads/mmc-stable-lin32.tar.gz">lin32</a>`,
		"3926", // no archive: named, not linked
	)
	if strings.Contains(body, ">osx64</a>") {
		t.Error("a build without an archive has a link")
	}
	if strings.Index(body, "4306") > strings.Index(body, "3926") {
		t.Error("the newest build doesn't come first")
	}
}

func TestOverviewAPI(t *testing.T) {
	response, err := http.Get(testServer(t, "http://unused").URL + "/api/overview")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || response.Header.Get("Content-Type") != "application/json" {
		t.Errorf("status %d, %s", response.StatusCode, response.Header.Get("Content-Type"))
	}
	if !bytes.Equal(body, fixture(t)) {
		t.Error("it isn't overview.py's JSON as it wrote it")
	}
}

func TestRunsPage(t *testing.T) {
	status, body := get(t, testServer(t, "http://unused").URL+"/runs")
	if status != http.StatusOK {
		t.Fatalf("status %d: %s", status, body)
	}
	wantAll(t, body, "launcher-files2:24", "launcher-files2:23", "6m31s")
}

func TestRunPage(t *testing.T) {
	url := testServer(t, "http://unused").URL
	_, running := get(t, url+"/run/launcher-files2/24")
	wantAll(t, running, `class="result running"`, `id="follow" checked`, `data-url="/run/launcher-files2/24/log"`)
	_, finished := get(t, url+"/run/launcher-files2/23")
	wantAll(t, finished, `class="result success"`)
	if strings.Contains(finished, `id="follow" checked`) {
		t.Error("a finished run's log follows")
	}
	// A run older than the overview's list still shows its log
	status, old := get(t, url+"/run/meta/1")
	if status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	wantAll(t, old, `data-url="/run/meta/1/log"`)
}

func TestBadRunNames(t *testing.T) {
	url := testServer(t, "http://unused").URL
	for _, path := range []string{"/run/a%2Fb/1", "/run/meta/x", "/run/meta/x/log", "/run/a%2Fb/1/log", "/run/..%2F..%2Fetc/1/log"} {
		status, _ := get(t, url+path)
		if status != http.StatusNotFound {
			t.Errorf("%s: status %d", path, status)
		}
	}
}

// Only reading: nothing but GET gets anywhere.
func TestOnlyGet(t *testing.T) {
	url := testServer(t, "http://unused").URL
	for _, path := range []string{"/", "/old-layout", "/runs", "/run/meta/1", "/run/meta/1/log", "/api/overview"} {
		response, err := http.Post(url+path, "text/plain", nil)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("POST %s: status %d", path, response.StatusCode)
		}
	}
}

// A log comes through as it's written, not when the run ends.
func TestLogStreams(t *testing.T) {
	more := make(chan struct{})
	laminar := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/log/meta/7" {
			http.NotFound(w, r)
			return
		}
		io.WriteString(w, "first\n")
		w.(http.Flusher).Flush()
		<-more
		io.WriteString(w, "second\n")
	}))
	defer laminar.Close()
	defer func() {
		select {
		case <-more:
		default:
			close(more)
		}
	}()

	response, err := http.Get(testServer(t, laminar.URL).URL + "/run/meta/7/log")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()

	buffer := make([]byte, 64)
	got := make(chan string, 1)
	go func() {
		n, _ := response.Body.Read(buffer)
		got <- string(buffer[:n])
	}()
	select {
	case text := <-got:
		if text != "first\n" {
			t.Fatalf("read %q", text)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the first part didn't come through while the run went on")
	}
	close(more)
	rest, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if string(rest) != "second\n" {
		t.Fatalf("read %q", rest)
	}
}

func TestLogProblems(t *testing.T) {
	laminar := httptest.NewServer(http.NotFoundHandler())
	url := laminar.URL
	if status, _ := get(t, testServer(t, url).URL+"/run/meta/9/log"); status != http.StatusNotFound {
		t.Errorf("no such run: status %d", status)
	}
	laminar.Close()
	status, body := get(t, testServer(t, url).URL+"/run/meta/9/log")
	if status != http.StatusBadGateway || !strings.Contains(body, "can't read the log") {
		t.Errorf("Laminar away: status %d: %s", status, body)
	}
}

// A failed overview is shown as that; the last good one isn't shown in its place.
func TestOverviewFailure(t *testing.T) {
	o, err := parseOverview(fixture(t))
	if err != nil {
		t.Fatal(err)
	}
	var failure error
	s, err := newServer(func(context.Context) (*overview, error) {
		if failure != nil {
			return nil, failure
		}
		return o, nil
	}, "http://unused")
	if err != nil {
		t.Fatal(err)
	}
	s.overviews.ttl = 0
	server := httptest.NewServer(s.routes())
	defer server.Close()

	if status, _ := get(t, server.URL+"/"); status != http.StatusOK {
		t.Fatalf("status %d", status)
	}
	failure = errors.New("overview.py <failed>")
	status, body := get(t, server.URL+"/")
	if status != http.StatusBadGateway {
		t.Errorf("status %d", status)
	}
	wantAll(t, body, "overview.py &lt;failed&gt;", "data-refresh")
	if strings.Contains(body, "2254abe") {
		t.Error("an old overview shows after a failure")
	}
	response, err := http.Get(server.URL + "/api/overview")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var answer struct{ Error string }
	if err := json.NewDecoder(response.Body).Decode(&answer); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusBadGateway || answer.Error != "overview.py <failed>" {
		t.Errorf("the API: status %d, %+v", response.StatusCode, answer)
	}
}

func TestOverviewCache(t *testing.T) {
	calls := 0
	c := overviewCache{source: func(context.Context) (*overview, error) {
		calls++
		return &overview{}, nil
	}, ttl: time.Hour}
	for i := 0; i < 3; i++ {
		if _, err := c.get(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 1 {
		t.Errorf("%d calls", calls)
	}
}

func TestCommandSource(t *testing.T) {
	if _, err := commandSource([]string{"false"})(context.Background()); err == nil {
		t.Error("a failing command isn't a failure")
	}
	if _, err := commandSource([]string{"echo", "not json"})(context.Background()); err == nil {
		t.Error("something else than the overview isn't a failure")
	}
	if _, err := commandSource([]string{"echo", "{}"})(context.Background()); err == nil {
		t.Error("an empty overview isn't a failure")
	}
	if _, err := commandSource([]string{"cat", "testdata/overview.json"})(context.Background()); err != nil {
		t.Error(err)
	}
}

func TestFormats(t *testing.T) {
	seconds := func(n int) *int { return &n }
	for _, c := range []struct {
		seconds *int
		want    string
	}{{nil, ""}, {seconds(391), "6m31s"}, {seconds(7500), "2h05m"}} {
		if got := duration(c.seconds); got != c.want {
			t.Errorf("duration: %q, want %q", got, c.want)
		}
	}
	if got := shortTime("2026-10-27T12:00:06+01:00"); got != "10-27 12:00" {
		t.Errorf("shortTime: %q", got)
	}
	if got := size(6733687156736); got != "6.1T" {
		t.Errorf("size: %q", got)
	}
}
