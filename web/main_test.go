package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"slices"
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
	s, err := newServer(func(context.Context) (*overview, error) { return o, nil }, fakeBuilds(nil), fakeBuilds(nil), laminarURL)
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
	wantAll(t, body, "launcher-files2:24", "launcher-files2:23", "6m31s",
		`action="/run/tag-files2/3/cancel"`, `<span class="dim">cancelled</span>`)
	if strings.Contains(body, `action="/run/tag-files2/2/cancel"`) || strings.Contains(body, `action="/run/launcher-files2/24/cancel"`) {
		t.Error("a run that's cancelled already or running can be cancelled")
	}
}

func TestCancel(t *testing.T) {
	server, calls := moveServer(t)
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.PostForm(server.URL+"/run/tag-files2/3/cancel", nil)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusSeeOther || response.Header.Get("Location") != "/run/tag-files2/3" {
		t.Errorf("status %d, to %q", response.StatusCode, response.Header.Get("Location"))
	}
	if len(*calls) != 1 || strings.Join((*calls)[0], " ") != "cancel tag-files2 3" {
		t.Errorf("calls %v", *calls)
	}

	status, body := postForm(t, server.URL+"/run/launcher-files2/24/cancel", nil)
	if status != http.StatusConflict {
		t.Errorf("a refusal: status %d", status)
	}
	wantAll(t, body, "Not cancelled", "has started already")

	*calls = nil
	for _, path := range []string{"/run/a%2Fb/1/cancel", "/run/meta/x/cancel", "/run/meta/-1/cancel"} {
		if status, _ := postForm(t, server.URL+path, nil); status != http.StatusNotFound {
			t.Errorf("%s: status %d", path, status)
		}
	}
	if len(*calls) != 0 {
		t.Errorf("a bad request reached the script: %v", *calls)
	}
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

// Only the tag moves take anything but GET.
func TestOnlyGet(t *testing.T) {
	url := testServer(t, "http://unused").URL
	for _, path := range []string{"/", "/old-layout", "/runs", "/run/meta/1", "/run/meta/1/log", "/api/overview", "/builds/files2", "/api/builds/files2"} {
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
	}, fakeBuilds(nil), fakeBuilds(nil), "http://unused")
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

const buildsFixture = `{
  "setup": "files2", "has_tree": true, "can_act": true, "serial": 21, "cap_mb": 4096,
  "tags": {"latest": 5043, "testing": 5035, "stable": 5034},
  "withdrawn": [5026],
  "builds": [
    {"build": 5043, "commit": "5ec639ce79a0c544281a", "commit_url": "https://git.example/commit/5ec6", "subject": "GH-4659 <play time>", "built": "2026-11-06T17:46:46+01:00",
     "platforms": [{"platform": "lin64", "url": "https://files2.example/latest-lin64.tar.gz"}], "tags": ["latest"]},
    {"build": 5035, "commit": "aaaaaaaaaaaaaaaaaaaa", "commit_url": "https://git.example/commit/aaaa", "subject": null, "built": "2026-11-05T10:00:00+01:00",
     "platforms": [{"platform": "lin64", "url": null}], "tags": ["testing"]},
    {"build": 5034, "commit": "bbbbbbbbbbbbbbbbbbbb", "commit_url": "https://git.example/commit/bbbb", "subject": null, "built": "2026-11-05T09:00:00+01:00",
     "platforms": [], "tags": ["stable"], "archived": true},
    {"build": 5030, "commit": "cccccccccccccccccccc", "commit_url": "https://git.example/commit/cccc", "subject": null, "built": "2026-11-04T09:00:00+01:00",
     "platforms": [], "tags": []}
  ]
}`

// fakeBuilds answers like daemon/builds.py and records the calls; refusal is what it refuses with.
func fakeBuilds(calls *[][]string) command {
	return func(_ context.Context, args ...string) ([]byte, error) {
		if calls != nil {
			*calls = append(*calls, args)
		}
		switch args[0] {
		case "list":
			if args[1] == "broken" {
				return nil, errors.New("bad <tree>")
			}
			if args[1] == "production" {
				return []byte(`{"setup": "production", "has_tree": false, "can_act": false, "serial": null, "tags": null, "withdrawn": [],
				  "builds": [{"build": 4307, "commit": "dddddddddddd", "commit_url": "https://git.example/d", "subject": "x", "built": "2026-11-06T17:51:25+01:00", "platforms": [], "tags": ["develop"]}]}`), nil
			}
			return []byte(buildsFixture), nil
		case "check":
			return []byte(`{"moved": ["testing"]}`), nil
		case "queue":
			if slices.Contains(args, "99") {
				return nil, errors.New("the tags have changed")
			}
			return []byte(`{"run": "tag-files2:3"}`), nil
		case "cancel":
			if args[2] == "24" {
				return nil, errors.New("launcher-files2:24 has started already")
			}
			return []byte(`{"cancelled": "tag-files2:3"}`), nil
		}
		return nil, errors.New("no such command")
	}
}

func moveServer(t *testing.T) (*httptest.Server, *[][]string) {
	t.Helper()
	o, err := parseOverview(fixture(t))
	if err != nil {
		t.Fatal(err)
	}
	calls := &[][]string{}
	s, err := newServer(func(context.Context) (*overview, error) { return o, nil }, fakeBuilds(calls), fakeBuilds(calls), "http://unused")
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(s.routes())
	t.Cleanup(server.Close)
	return server, calls
}

func TestBuildsPage(t *testing.T) {
	server, _ := moveServer(t)
	status, body := get(t, server.URL+"/builds/files2")
	if status != http.StatusOK {
		t.Fatalf("status %d: %s", status, body)
	}
	wantAll(t, body,
		"5ec639ce79", `href="https://git.example/commit/5ec6"`, "GH-4659 &lt;play time&gt;",
		`class="tag latest"`, `href="https://files2.example/latest-lin64.tar.gz"`,
		`data-setup="files2"`, `data-serial="21"`, `data-testing="5035"`, `data-stable="5034"`,
		`value="5035" aria-label="Testing: build 5035" checked`,
		`value="5034" aria-label="Stable: build 5034" checked`,
		`value="5030" aria-label="Stable: build 5030" disabled`, // older: tags only move forward
		`value="5034" aria-label="Testing: build 5034" disabled`,
		"Withdrawn", "5026", `id="review"`, `id="review-confirm"`, `id="apply" disabled`,
		`data-withdraw="5043"`, `data-withdraw="5030"`, ">archived<", `id="prune-cap" type="number" min="0" step="1" value="4096"`,
	)
	if strings.Contains(body, `data-withdraw="5034"`) {
		t.Error("the Stable build can be withdrawn")
	}
	if strings.Contains(body, "data-refresh") {
		t.Error("the page refreshes itself, which would drop a selection")
	}

	_, production := get(t, server.URL+"/builds/production")
	wantAll(t, production, "4307", "no new updater tree", `class="tag develop"`)
	for _, absent := range []string{`type="radio"`, `id="review"`, "builds.js", "data-withdraw", `id="prune"`} {
		if strings.Contains(production, absent) {
			t.Errorf("a setup without a tree has %q", absent)
		}
	}
	if status, _ := get(t, server.URL+"/builds/nosuch"); status != http.StatusNotFound {
		t.Errorf("no such setup: status %d", status)
	}
	status, broken := get(t, server.URL+"/builds/broken")
	if status != http.StatusBadGateway || !strings.Contains(broken, "bad &lt;tree&gt;") {
		t.Errorf("broken: status %d: %s", status, broken)
	}
	if status, api := get(t, server.URL+"/api/builds/files2"); status != http.StatusOK || api != buildsFixture {
		t.Errorf("the API: status %d", status)
	}
}

func postForm(t *testing.T, url string, values url.Values) (int, string) {
	t.Helper()
	response, err := http.PostForm(url, values)
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

func TestActions(t *testing.T) {
	server, calls := moveServer(t)
	for _, c := range []struct {
		step   string
		values url.Values
		want   string
	}{
		{"check", url.Values{"serial": {"21"}, "action": {"tag"}, "testing": {"5043"}}, "check files2 --serial 21 tag testing 5043"},
		{"queue", url.Values{"serial": {"21"}, "action": {"tag"}, "testing": {"5043"}, "stable": {"5035"}}, "queue files2 --serial 21 tag testing 5043 stable 5035"},
		{"check", url.Values{"serial": {"21"}, "action": {"remove"}, "build": {"5030"}}, "check files2 --serial 21 remove 5030"},
		{"queue", url.Values{"serial": {"21"}, "action": {"remove"}, "build": {"5030"}}, "queue files2 --serial 21 remove 5030"},
		{"check", url.Values{"serial": {"21"}, "action": {"prune"}}, "check files2 --serial 21 prune"},
		{"queue", url.Values{"serial": {"21"}, "action": {"prune"}, "cap_mb": {"0"}}, "queue files2 --serial 21 prune 0"},
	} {
		*calls = nil
		if status, body := postForm(t, server.URL+"/builds/files2/"+c.step, c.values); status != http.StatusOK {
			t.Errorf("%s: status %d: %s", c.want, status, body)
		}
		if len(*calls) != 1 || strings.Join((*calls)[0], " ") != c.want {
			t.Errorf("calls %v, want %s", *calls, c.want)
		}
	}

	status, body := postForm(t, server.URL+"/builds/files2/queue", url.Values{"serial": {"21"}, "action": {"tag"}, "testing": {"99"}})
	if status != http.StatusConflict || !strings.Contains(body, "the tags have changed") {
		t.Errorf("a refusal: status %d: %s", status, body)
	}

	*calls = nil
	for _, c := range []struct {
		path   string
		values url.Values
		status int
	}{
		{"files2/queue", url.Values{"action": {"tag"}, "testing": {"5043"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"tag"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"nosuch"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21; rm"}, "action": {"tag"}, "testing": {"5043"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"tag"}, "testing": {"--dry-run"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"remove"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"remove"}, "build": {"--serial"}}, http.StatusBadRequest},
		{"files2/queue", url.Values{"serial": {"21"}, "action": {"prune"}, "cap_mb": {"-1"}}, http.StatusBadRequest},
		{"nosuch/queue", url.Values{"serial": {"21"}, "action": {"prune"}}, http.StatusNotFound},
	} {
		if status, body := postForm(t, server.URL+"/builds/"+c.path, c.values); status != c.status {
			t.Errorf("%s %v: status %d: %s", c.path, c.values, status, body)
		}
	}
	if len(*calls) != 0 {
		t.Errorf("a bad request reached the script: %v", *calls)
	}
}

// There's no login: another site can't make a browser move the tags.
func TestCrossOriginRefused(t *testing.T) {
	server, calls := moveServer(t)
	request, err := http.NewRequest("POST", server.URL+"/builds/files2/queue", strings.NewReader("serial=21&action=tag&testing=5043"))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	request.Header.Set("Sec-Fetch-Site", "cross-site")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Errorf("status %d", response.StatusCode)
	}
	if len(*calls) != 0 {
		t.Errorf("it reached the script: %v", *calls)
	}
}
