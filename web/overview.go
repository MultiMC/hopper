package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// overview is the part of overview.py's JSON that the pages show.
type overview struct {
	Time         string   `json:"time"`
	Host         string   `json:"host"`
	Code         string   `json:"code"`
	ReleaseBuild int      `json:"release_build"` // the pid holding its lock; 0: none
	Disk         disk     `json:"disk"`
	Setups       []setup  `json:"setups"`
	Laminar      *laminar `json:"laminar"` // nil: no Laminar here

	raw []byte // the JSON as overview.py wrote it, for /api/overview
}

type disk struct {
	Free  int64 `json:"free"`
	Total int64 `json:"total"`
}

type setup struct {
	Name  string `json:"name"`
	Live  *live  `json:"live"`
	Error string `json:"error"`
}

type live struct {
	Published        *published                     `json:"published"`
	Updater          *updater                       `json:"updater"` // nil: no new updater tree
	Channels         map[string]map[string][]string `json:"channels"`
	ChannelDownloads map[string]map[string]string   `json:"channel_downloads"`
	Website          map[string]string              `json:"website"`
}

type updater struct {
	Tags      []tag `json:"tags"`
	Withdrawn []int `json:"withdrawn"`
}

type tag struct {
	Tag       string     `json:"tag"`
	Build     int        `json:"build"`
	Downloads []download `json:"downloads"`
}

// download is a platform and its archive. URL empty: there's no archive to link.
type download struct {
	Platform string `json:"platform"`
	URL      string `json:"url"`
}

type published struct {
	Snapshot string `json:"snapshot"`
	Created  string `json:"created"`
	Kind     string `json:"kind"`
	About    string `json:"about"`
}

type laminar struct {
	Laminard unit           `json:"laminard"`
	Service  unit           `json:"service"`
	Trigger  int            `json:"trigger"` // the pid holding its lock; 0: not running
	Status   *triggerStatus `json:"status"`
	Jobs     []job          `json:"jobs"`
	Runs     []run          `json:"runs"`
}

type unit struct {
	ActiveState string `json:"ActiveState"`
}

type triggerStatus struct {
	PID      int    `json:"pid"`
	Interval int    `json:"interval"`
	Looked   string `json:"looked"`
	Problem  string `json:"problem"`
}

type job struct {
	Name    string `json:"name"`
	Trigger string `json:"trigger"`
	Problem string `json:"problem"`
	Seen    struct {
		Next string `json:"next"`
	} `json:"seen"`
	LastRun *run     `json:"last_run"`
	Running []string `json:"running"` // nil: laminard can't be asked
}

type run struct {
	Run     string `json:"run"` // job:number
	Job     string `json:"job"`
	Result  string `json:"result"`
	Started string `json:"started"`
	Seconds *int   `json:"seconds"`
	Reason  string `json:"reason"`
	// queued, and stops when its turn comes
	Cancelled bool `json:"cancelled"`
}

// Number is the run's number, for its URL.
func (r run) Number() string {
	_, number, _ := strings.Cut(r.Run, ":")
	return number
}

// Going says the run isn't finished: its log may still grow.
func (r run) Going() bool {
	return r.Result == "running" || r.Result == "queued"
}

// Cancellable: it's queued, and not cancelled yet.
func (r run) Cancellable() bool {
	return r.Result == "queued" && !r.Cancelled
}

// Source is what starts the job, short: "Launcher develop", "cron 0 12 * * *", "by hand".
func (j job) Source() string {
	if j.Trigger == "" {
		return "by hand"
	}
	if strings.HasPrefix(j.Trigger, "cron ") {
		return j.Trigger
	}
	fields := strings.Fields(j.Trigger)
	if len(fields) != 2 {
		return j.Trigger
	}
	repository := fields[0][strings.LastIndex(fields[0], "/")+1:]
	return strings.TrimSuffix(repository, ".git") + " " + fields[1]
}

// Unasked says laminard couldn't tell what's running.
func (j job) Unasked() bool {
	return j.Running == nil
}

// RunLinks splits running runs ("job:number") for their links.
func (j job) RunLinks() []run {
	links := make([]run, 0, len(j.Running))
	for _, name := range j.Running {
		links = append(links, run{Run: name, Job: j.Name})
	}
	return links
}

type channelRow struct {
	Channel  string
	Versions []channelVersion
}

type channelVersion struct {
	ID        string
	Platforms []download
}

// ChannelRows is the update channels in order, each one's newest builds first.
func (l live) ChannelRows() []channelRow {
	rows := make([]channelRow, 0, len(l.Channels))
	for channel, versions := range l.Channels {
		row := channelRow{Channel: channel}
		for id, platforms := range versions {
			platforms = slices.Clone(platforms)
			slices.Sort(platforms)
			downloads := make([]download, 0, len(platforms))
			for _, platform := range platforms {
				downloads = append(downloads, download{platform, l.ChannelDownloads[channel][platform]})
			}
			row.Versions = append(row.Versions, channelVersion{id, downloads})
		}
		sort.Slice(row.Versions, func(a, b int) bool {
			return versionNumber(row.Versions[a].ID) > versionNumber(row.Versions[b].ID)
		})
		rows = append(rows, row)
	}
	sort.Slice(rows, func(a, b int) bool { return rows[a].Channel < rows[b].Channel })
	return rows
}

// NewestDevelop is the develop channel's newest build, what a setup without a new updater
// tree releases.
func (l live) NewestDevelop() *channelVersion {
	for _, row := range l.ChannelRows() {
		if row.Channel == "develop" && len(row.Versions) > 0 {
			return &row.Versions[0]
		}
	}
	return nil
}

func versionNumber(id string) int {
	number, err := strconv.Atoi(id)
	if err != nil {
		return 0
	}
	return number
}

type triggerLook struct {
	Seconds int
	Late    bool
}

// TriggerLook is when the running trigger last looked, if it says. Late: three intervals ago.
func (o overview) TriggerLook() *triggerLook {
	l := o.Laminar
	if l == nil || l.Status == nil || l.Status.PID != l.Trigger {
		return nil
	}
	now, err1 := time.Parse(time.RFC3339, o.Time)
	looked, err2 := time.Parse(time.RFC3339, l.Status.Looked)
	if err1 != nil || err2 != nil {
		return nil
	}
	seconds := int(now.Sub(looked).Seconds())
	return &triggerLook{seconds, seconds > 3*l.Status.Interval}
}

// findRun is a run by job and number, among those the overview lists.
func (o overview) findRun(jobName, number string) *run {
	if o.Laminar == nil {
		return nil
	}
	name := jobName + ":" + number
	for i := range o.Laminar.Runs {
		if o.Laminar.Runs[i].Run == name {
			return &o.Laminar.Runs[i]
		}
	}
	for i := range o.Laminar.Jobs {
		if last := o.Laminar.Jobs[i].LastRun; last != nil && last.Run == name {
			return last
		}
	}
	return nil
}

// source gets the current overview.
type source func(ctx context.Context) (*overview, error)

// commandSource runs a command that prints the overview's JSON.
func commandSource(command []string) source {
	return func(ctx context.Context) (*overview, error) {
		ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		var stdout, stderr bytes.Buffer
		cmd := exec.CommandContext(ctx, command[0], command[1:]...)
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
		if err := cmd.Run(); err != nil {
			return nil, fmt.Errorf("%s: %w: %s", strings.Join(command, " "), err, strings.TrimSpace(stderr.String()))
		}
		return parseOverview(stdout.Bytes())
	}
}

func parseOverview(data []byte) (*overview, error) {
	var o overview
	if err := json.Unmarshal(data, &o); err != nil {
		return nil, fmt.Errorf("the overview isn't what overview.py writes: %w", err)
	}
	if o.Time == "" {
		return nil, errors.New("the overview isn't what overview.py writes: it has no time")
	}
	o.raw = data
	return &o, nil
}

// overviewCache keeps one overview for a moment, so that open pages share one run of the
// command. A failure isn't kept, and nothing older is shown in its place.
type overviewCache struct {
	source source
	ttl    time.Duration

	mu      sync.Mutex
	current *overview
	loaded  time.Time
}

func (c *overviewCache) get(ctx context.Context) (*overview, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.current != nil && time.Since(c.loaded) < c.ttl {
		return c.current, nil
	}
	c.current = nil
	current, err := c.source(ctx)
	if err != nil {
		return nil, err
	}
	c.current, c.loaded = current, time.Now()
	return current, nil
}
